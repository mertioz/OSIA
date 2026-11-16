# PC–Mobile direct-first transport

## Selection and ownership

The current coordinator selects one authoritative application transport in this order:

1. authenticated private-LAN WebSocket;
2. data-only direct WebRTC;
3. the endpoint-encrypted OSIA relay.

LAN and WebRTC are enabled by default for new installations. The relay remains available for authenticated E2E signaling while direct paths are prepared. A relay application transport is committed after the bounded pre-activation direct attempts fail.

The selected transport owns the process-wide application lease and single local bridge. The coordinator does not resend an active turn or switch/fall back during that turn. Connection setup bounds are transport-operation bounds, not global Chat objective limits.

Implementation: [transport_coordinator.py](../osia/transport_coordinator.py), [lan_gateway.py](../osia/lan_gateway.py), [webrtc_transport.py](../osia/webrtc_transport.py), [webrtc_signaling.py](../osia/webrtc_signaling.py) and [relay_connector.py](../osia/relay_connector.py).

## ICE configuration

[config.py](../osia/config.py) owns PRODUCTION_STUN_URL and TRANSPORT_DEFAULTS_SCHEMA_VERSION. The current default STUN record is:

    stun:stun.cloudflare.com:3478

TURN is empty by default. The PC validates runtime ICE configuration and sends it to its paired Mobile inside authenticated relay E2E control state. mDNS TXT and unauthenticated relay metadata are routing information, not trusted ICE configuration.

Optional TURN records are read only through the protected OSIA_WEBRTC_ICE_SERVERS_JSON process environment. Credentials are not copied into settings, source or logs. Direct traversal can fail under NAT/firewall restrictions; the coordinator handles that bounded failure and relay commitment.

## LAN discovery and certificates

[lan_discovery.py](../osia/lan_discovery.py) advertises _osia._tcp.local. after the private WSS listener is bound on port 8766. The PC selects a private IPv4 interface, creates an ECDSA P-256 key/self-signed certificate for that IP and validates the pair through its TLS loader.

Address changes use staged certificate rotation with rollback to the last complete valid pair if replacement fails. mDNS exposes routing metadata and a random installation alias rather than tokens, certificate pins, keys or pairing proofs.

On Windows, the setup path creates the existing OSIA Secure LAN Direct inbound rule for TCP 8766, the selected program, Private profile and LocalSubnet. Normal runtime does not request elevation. Uninstall removes its integration while preserving pairing and user data. [Windows distribution](windows-installer.md) describes the packaged/source entry points.

## Defaults and emergency overrides

The transport_defaults_schema setting controls the one-time migration of earlier defaults. The marker and settings are written atomically. After migration, explicit stored false choices remain respected; invalid settings do not cause secret stores or pairing identity to be rewritten.

The independent process overrides are:

    OSIA_LAN_DIRECT_V1=0
    OSIA_WEBRTC_DIRECT_V1=0

Either direct path can be disabled independently. Both disabled select the relay-only path. Development-only routing overrides remain separate from the production configuration.

## Privacy and observability

The STUN operator can observe source network addresses/ports, timing and protocol metadata. It does not receive OSIA E2E plaintext, conversations, files, model output, SDP or application DataChannel payloads. WebRTC peers learn the ICE candidates needed for their connection.

OSIA's reduced diagnostics omit raw SDP, candidate addresses/ports, ICE credentials, pairing identity and certificate pins. Candidate evidence uses type/protocol categories rather than raw network endpoints. The third-party STUN endpoint has no OSIA availability guarantee; unavailable STUN does not disable the rest of OSIA.

[stun_health.py](../osia/stun_health.py) sends bounded unauthenticated Binding requests without credentials or application data and returns sanitized health/category/timing information. From the source environment on Windows:

    ./venv/Scripts/python.exe -m osia.stun_health

## Troubleshooting and coordinated changes

Check the private network/firewall scope and LAN listener/discovery first, then the sanitized STUN probe and the direct-attempt/relay commitment state. Observe the selected transport's exact lease rather than creating a competing local bridge.

Changing the production endpoint involves its config constant and migration marker, trusted PC-to-Mobile configuration and the platform transport implementation. Build/publication coordination follows [AGENTS.md](../AGENTS.md) and the [Drive installer workflow](https://drive.google.com/file/d/11nRvwHVxn0JzaZbKqE8Mzy6pEyVVBGK4/view); this guide does not grant publication or substitute an arbitrary provider.
