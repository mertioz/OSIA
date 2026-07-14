"""Bounded, privacy-safe health classification for configured STUN endpoints."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import secrets
import socket
import struct
import threading
import time
from typing import Sequence

import dns.exception
import dns.resolver


_STUN_URL = re.compile(
    r"^stun:(?P<host>\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+)(?::(?P<port>[0-9]{1,5}))?$"
)
_BINDING_REQUEST = 0x0001
_BINDING_SUCCESS = 0x0101
_MAGIC_COOKIE = 0x2112A442


@dataclass(frozen=True)
class StunHealthResult:
    status: str
    elapsed_ms: int
    address_family: str
    candidate_type: str
    protocol: str = "udp"


def _result(status: str, started: float, family: str = "none") -> StunHealthResult:
    return StunHealthResult(
        status=status,
        elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
        address_family=family,
        candidate_type="srflx" if status == "healthy" else "none",
    )


def _resolve_numeric_addresses(
    hostname: str,
    port: int,
    timeout: float,
) -> list[tuple[int, tuple]]:
    resolver = dns.resolver.Resolver()
    addresses: list[tuple[int, tuple]] = []
    deadline = time.monotonic() + timeout
    for record_type, family in (("A", socket.AF_INET), ("AAAA", socket.AF_INET6)):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("DNS deadline expired")
        try:
            answers = resolver.resolve(hostname, record_type, lifetime=remaining)
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            continue
        except (dns.exception.Timeout, dns.resolver.LifetimeTimeout) as exc:
            if addresses:
                break
            raise TimeoutError("DNS lookup timed out") from exc
        except dns.exception.DNSException:
            continue
        for answer in answers:
            address = str(answer)
            sockaddr = (address, port) if family == socket.AF_INET else (
                address,
                port,
                0,
                0,
            )
            addresses.append((family, sockaddr))
    return addresses


def classify_stun_url(url: str, *, timeout: float = 3.0) -> StunHealthResult:
    """Send one credential-free STUN Binding request and return no address data."""

    started = time.monotonic()
    match = _STUN_URL.fullmatch(url.strip()) if isinstance(url, str) else None
    if match is None:
        return _result("unavailable", started)
    host = match.group("host").strip("[]")
    port = int(match.group("port") or "3478")
    if not 1 <= port <= 65535:
        return _result("unavailable", started)
    try:
        addresses = _resolve_numeric_addresses(host, port, timeout)
    except TimeoutError:
        return _result("timeout", started)
    if not addresses:
        return _result("dns_unavailable", started)

    transaction_id = secrets.token_bytes(12)
    request = struct.pack("!HHI12s", _BINDING_REQUEST, 0, _MAGIC_COOKIE, transaction_id)
    deadline = started + timeout
    saw_timeout = False
    for family, sockaddr in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            saw_timeout = True
            break
        family_name = "ipv4" if family == socket.AF_INET else "ipv6"
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.settimeout(remaining)
                sock.sendto(request, sockaddr)
                response, _ = sock.recvfrom(2048)
        except socket.timeout:
            saw_timeout = True
            continue
        except OSError:
            continue
        if len(response) < 20:
            continue
        message_type, body_length, cookie, response_id = struct.unpack(
            "!HHI12s", response[:20]
        )
        if (
            message_type == _BINDING_SUCCESS
            and cookie == _MAGIC_COOKIE
            and response_id == transaction_id
            and body_length <= len(response) - 20
        ):
            return _result("healthy", started, family_name)
    return _result("timeout" if saw_timeout else "unavailable", started)


_probe_lock = threading.Lock()
_probe_started = False
_latest_result: StunHealthResult | None = None


def start_stun_health_probe(urls: Sequence[str], *, timeout: float = 3.0) -> bool:
    """Start at most one non-blocking process health probe."""

    global _probe_started
    first_stun = next((url for url in urls if str(url).startswith("stun:")), "")
    if not first_stun:
        return False
    with _probe_lock:
        if _probe_started:
            return False
        _probe_started = True

    def run() -> None:
        global _latest_result
        result = classify_stun_url(first_stun, timeout=timeout)
        with _probe_lock:
            _latest_result = result
        print(
            "[OSIA WebRTC] STUN health="
            f"{result.status} family={result.address_family} "
            f"candidate={result.candidate_type} protocol={result.protocol} "
            f"elapsed_ms={result.elapsed_ms}"
        )

    threading.Thread(target=run, name="osia-stun-health", daemon=True).start()
    return True


def latest_stun_health() -> StunHealthResult | None:
    with _probe_lock:
        return _latest_result


def main() -> int:
    """Run the configured health check while emitting only sanitized fields."""

    from dataclasses import asdict

    from osia.webrtc_signaling import load_webrtc_ice_servers

    records = load_webrtc_ice_servers()
    urls = [
        str(url)
        for record in records
        for url in record.get("urls", [])
        if str(url).startswith("stun:")
    ]
    result = (
        classify_stun_url(urls[0])
        if urls
        else StunHealthResult("unavailable", 0, "none", "none")
    )
    print(json.dumps(asdict(result), sort_keys=True, separators=(",", ":")))
    return 0 if result.status == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
