# Production direct-first transport

OSIA selects exactly one authoritative mobile application transport in this
order:

1. secure private-LAN WebSocket (`lan_direct_v1`);
2. data-only direct WebRTC (`webrtc_direct_v1`);
3. the existing endpoint-encrypted OSIA relay.

LAN and WebRTC are enabled for every new installation. The relay stays
connected as an endpoint-encrypted signaling path while direct transports are
being prepared and is explicitly committed only after both pre-activation
direct attempts fail. A selected transport owns the process-wide application
lease and the single local bridge. OSIA never resends a turn, changes transport,
or falls back while an application turn is active.

"WebRTC enabled for everyone" means that every official installation attempts
it automatically. Direct traversal cannot succeed through every NAT or firewall;
failure is expected, bounded, cleaned up, and followed by relay commitment.

## Production ICE configuration

The production v1 STUN-only configuration is exactly:

```text
stun:stun.cloudflare.com:3478
```

Port 53 is not a primary or default endpoint. TURN is empty by default. The PC
is the source of trusted runtime ICE configuration. It validates the URL and
sends it to its paired mobile only inside the existing authenticated relay E2E
control state. Mobile never trusts ICE configuration from mDNS TXT,
unauthenticated relay metadata, arbitrary UI input, or a remote configuration
URL. Optional TURN records remain possible only through the protected
`OSIA_WEBRTC_ICE_SERVERS_JSON` process environment; credentials are never saved
to settings or source.

Due diligence was re-verified on **2026-07-14** against these official primary
sources:

- Cloudflare Realtime, **"TURN Service"**:
  <https://developers.cloudflare.com/realtime/turn/> — documents
  `stun.cloudflare.com`, primary UDP port 3478, alternate port 53, and warns
  against using port 53 by itself.
- Cloudflare Realtime, **"FAQ"**:
  <https://developers.cloudflare.com/realtime/turn/faq/> — states that the STUN
  service is free and unlimited and distinguishes STUN Binding from credentialed
  TURN allocations.
- Cloudflare Realtime, **"Generate Credentials"**:
  <https://developers.cloudflare.com/realtime/turn/generate-credentials/> — shows
  the STUN record without a username or credential and separately requires
  short-lived credentials for TURN.
- Cloudflare, **"Website and Online Services Terms of Use"** (effective
  2025-08-01): <https://www.cloudflare.com/policies/terms/> — our due-diligence
  reading found no prohibition on documented, low-rate STUN Binding use; abuse,
  disruption, limit circumvention, and unlawful use remain prohibited.

The endpoint is documented as an ordinary Cloudflare Realtime service, not as a
temporary or test-only endpoint. It is nevertheless a third-party, as-available
dependency: Cloudflare may change, limit, or discontinue it, and OSIA does not
claim an availability SLA. STUN failure never blocks OSIA as a whole.

The startup health probe sends bounded, unauthenticated RFC 5389 Binding
requests with no attributes across the endpoint's resolved addresses. It never
sends Allocate, credentials, or application data to STUN. Health is reported
only as `healthy`, `unavailable`, `timeout`, or `dns_unavailable`, plus elapsed
time, IPv4/IPv6 category, `srflx`, and UDP. Raw addresses and ports are not
reported.

## Privacy

The STUN operator can observe the source network address, source port, request
timing, and protocol metadata. STUN does not receive OSIA conversations, tool
payloads, files, model output, TTS media, OSIA E2E plaintext, SDP, or application
DataChannel payload. WebRTC peers necessarily learn the ICE network candidates
needed to establish their direct connection.

OSIA logs and the health command do not retain raw SDP, candidate IP addresses,
candidate ports, ICE usernames/passwords, pairing identities, or certificate
pins. Candidate evidence is reduced to local/remote type (`host`, `srflx`, or
`prflx`) and transport protocol. Relay remains available whenever direct
traversal fails.

## LAN discovery and first run

The PC selects a private IPv4 interface, automatically creates an ECDSA P-256
private key and matching self-signed certificate with that exact IP as its only
SAN, validates the pair through the runtime TLS loader, and advertises
`_osia._tcp.local.` only after WSS port 8766 is bound. A network-address change
causes a staged, validated, atomic certificate rotation; a failed replacement
rolls back to the last complete valid pair. mDNS contains only routing metadata
and a random installation alias, never a token, pin, key, or pairing proof.

On Windows, `install.bat` requests elevation once to create the **OSIA Secure
LAN Direct** inbound rule for TCP 8766, the selected Python program, the
`Private` profile, and `LocalSubnet` only. No Public-profile or wildcard rule is
created, and normal runtime does not request elevation. `uninstall.bat` removes
the rule and startup tasks but deliberately preserves pairing and user data.

## Migration and kill switches

`transport_defaults_schema = 1` performs the one-time upgrade from the former
materialized false defaults. Pre-schema installations receive LAN enabled,
WebRTC enabled, and the production STUN URL once. The marker is written with the
settings in the same atomic replacement. Afterward, explicit stored false values
remain respected across restarts. Invalid settings are preserved for recovery
and never cause secret stores or pairing identity to be rewritten.

Independent emergency environment overrides remain available:

```text
OSIA_LAN_DIRECT_V1=0
OSIA_WEBRTC_DIRECT_V1=0
```

Either direct path can be disabled without disabling the other or the relay.
Setting both to zero produces relay-only emergency mode. The public relay has no
production user-facing kill switch; development-only routing overrides remain
separate.

## Operations and endpoint replacement

Run a sanitized bounded check with:

```powershell
venv\Scripts\python.exe -m osia.stun_health
```

Troubleshooting order:

1. confirm Windows network category is Private and the scoped firewall rule is
   present;
2. confirm `_osia._tcp.local.` discovery and private-interface port 8766;
3. run the sanitized STUN health check;
4. allow bounded WebRTC failure and confirm one explicit relay commitment.

To replace the production endpoint, update `PRODUCTION_STUN_URL`, increment
`TRANSPORT_DEFAULTS_SCHEMA_VERSION` so existing installations migrate once,
repeat official-source/privacy review, run DNS and credential-free Binding tests
from Windows and Android Wi-Fi/mobile data, rerun the full transport matrix, and
ship both PC and mobile release artifacts together. Never silently substitute a
different public provider.

The implementation is cross-platform on Windows, macOS, and Linux. Platform
installation mechanics differ, but the authenticated ICE contract, direct-first
selection, lease authority, E2E DataChannel, and relay fallback semantics are
identical.
