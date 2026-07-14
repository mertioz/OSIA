# WebRTC direct transport

OSIA selects one authoritative mobile application path in this order: private
LAN, data-only WebRTC, then the existing encrypted public relay. The relay stays
connected as an endpoint-encrypted signaling channel while either direct path is
being prepared. It never receives plaintext SDP, ICE candidates, pairing
material, or application frames.

The PC is always the offerer and the mobile app is always the answerer. Each
attempt creates a new ICE/DTLS/SCTP peer connection with one ordered, reliable
DataChannel (`osia-app-v1`, protocol `osia.e2e.v1`). After DTLS opens, endpoints
perform a fresh pairing-token-authenticated X25519/HKDF/HMAC handshake and use
ChaCha20-Poly1305 with monotonic replay guards for every OSIA frame. The local
application bridge and transport lease are acquired only after this handshake.

## ICE configuration

Non-secret STUN URLs are read from the bounded `webrtc_stun_urls` settings list.
Optional TURN records may be supplied only through the protected
`OSIA_WEBRTC_ICE_SERVERS_JSON` process environment; TURN usernames and
credentials must never be written to settings, source, or logs. The validated
configuration is synchronized to mobile only inside the authenticated relay E2E
session.

The tracked fresh-install default deliberately keeps `webrtc_direct_v1` off and
contains no public STUN URL. Production remote-direct activation therefore
remains gated on an OSIA-controlled endpoint or explicit approval of a trusted
operator. LAN direct is enabled by default. Both paths retain independent
environment kill switches (`OSIA_LAN_DIRECT_V1` and
`OSIA_WEBRTC_DIRECT_V1`). TURN is not enabled by default.

During supervised development validation, a temporary STUN URL may be placed in
protected local settings after a byte-exact backup. It must be removed by
restoring that backup, and the selected candidate pair must be `host`, `srflx`,
or `prflx` on both endpoints—never `relay`.
