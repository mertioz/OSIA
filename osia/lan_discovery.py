"""Private-IPv4 DNS-SD advertisement for the feature-gated LAN gateway."""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import tempfile

import psutil

from osia.config import CONFIG_DIR


SERVICE_TYPE = "_osia._tcp.local."
PRODUCT_VERSION = "1.0.0"
_ALIAS_FILE = Path(CONFIG_DIR) / "lan_direct_identity.json"
_ALIAS_RE = re.compile(r"^[a-f0-9]{16}$")


def _private_ipv4(value: str) -> str | None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    private_ranges = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    )
    if not isinstance(address, ipaddress.IPv4Address):
        return None
    if not any(address in network for network in private_ranges):
        return None
    return str(address)


def select_private_ipv4() -> str:
    """Choose the routed private IPv4 without assuming an OS interface name."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route_probe:
            # UDP connect selects the default route without sending a packet.
            route_probe.connect(("192.0.2.1", 9))
            routed = _private_ipv4(str(route_probe.getsockname()[0]))
            if routed:
                return routed
    except OSError:
        pass

    candidates: set[str] = set()
    stats = psutil.net_if_stats()
    for name, addresses in psutil.net_if_addrs().items():
        interface = stats.get(name)
        if interface is not None and not interface.isup:
            continue
        for address in addresses:
            if address.family != socket.AF_INET:
                continue
            private = _private_ipv4(str(address.address or ""))
            if private:
                candidates.add(private)
    if not candidates:
        raise RuntimeError("no private IPv4 address is available for LAN discovery")
    return sorted(candidates, key=lambda item: tuple(int(part) for part in item.split(".")))[0]


def load_or_create_instance_alias(path: Path = _ALIAS_FILE) -> str:
    """Return the stable random, non-secret alias used to select one PC."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        alias = str(payload.get("instance_alias") or "")
        if payload.get("version") == 1 and _ALIAS_RE.fullmatch(alias):
            return alias
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
        pass

    alias = secrets.token_hex(8)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    staged = Path(staged_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {"version": 1, "instance_alias": alias},
                handle,
                ensure_ascii=True,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(staged, 0o600)
        os.replace(staged, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass
    return alias


class LanDiscoveryAdvertiser:
    """Register and unregister exactly one bounded DNS-SD service instance."""

    def __init__(self, host: str, port: int, alias: str) -> None:
        self.host = _private_ipv4(host) or ""
        if not self.host:
            raise ValueError("DNS-SD host must be a private IPv4 address")
        if not _ALIAS_RE.fullmatch(alias):
            raise ValueError("invalid LAN instance alias")
        self.port = int(port)
        self.alias = alias
        self._zeroconf = None
        self._service = None

    def start(self) -> None:
        if self._zeroconf is not None:
            return
        from zeroconf import IPVersion, ServiceInfo, Zeroconf

        service = ServiceInfo(
            SERVICE_TYPE,
            f"OSIA-{self.alias}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(self.host)],
            port=self.port,
            properties={
                b"protocol": b"1",
                b"product_version": PRODUCT_VERSION.encode("ascii"),
                b"instance": self.alias.encode("ascii"),
            },
            server=f"osia-{self.alias}.local.",
        )
        zeroconf = Zeroconf(
            interfaces=[self.host],
            ip_version=IPVersion.V4Only,
        )
        try:
            zeroconf.register_service(service, allow_name_change=False)
        except Exception:
            zeroconf.close()
            raise
        self._service = service
        self._zeroconf = zeroconf

    def close(self) -> None:
        zeroconf = self._zeroconf
        service = self._service
        self._zeroconf = None
        self._service = None
        if zeroconf is None:
            return
        try:
            if service is not None:
                zeroconf.unregister_service(service)
        finally:
            zeroconf.close()
