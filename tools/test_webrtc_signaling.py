"""Deterministic validation tests for the endpoint-only WebRTC contract."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from osia.webrtc_signaling import (
    MAX_CANDIDATES_PER_EVENT,
    MAX_SDP_BYTES,
    WebRtcSignalingError,
    capability_frame,
    candidate_dedup_key,
    ice_configuration_id,
    load_webrtc_ice_servers,
    make_signal,
    strip_sdp_candidates,
    validate_candidate,
    validate_data_only_sdp,
    validate_ice_servers,
    validate_signal,
)


DATA_SDP = (
    "v=0\r\n"
    "o=- 1 1 IN IP4 127.0.0.1\r\n"
    "s=-\r\n"
    "t=0 0\r\n"
    "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
    "c=IN IP4 0.0.0.0\r\n"
    "a=mid:0\r\n"
    "a=sctp-port:5000\r\n"
    "a=candidate:1 1 udp 2130706431 192.168.1.2 50000 typ host\r\n"
    "a=end-of-candidates\r\n"
)


class WebRtcSignalingTests(unittest.TestCase):
    def test_production_stun_default_is_exact_deduplicated_and_credential_free(self) -> None:
        production = "stun:stun.cloudflare.com:3478"
        with mock.patch(
            "osia.webrtc_signaling.load_settings",
            return_value={"webrtc_stun_urls": [production, production]},
        ):
            loaded = load_webrtc_ice_servers()
        self.assertEqual(loaded, [{"urls": [production]}])
        self.assertNotIn("username", loaded[0])
        self.assertNotIn("credential", loaded[0])

        for invalid in (
            "stun:stun.cloudflare.com:0",
            "stun:stun.cloudflare.com:65536",
            "stun://stun.cloudflare.com:3478",
            "stun:stun.cloudflare.com:3478?transport=udp",
            "turn:turn.cloudflare.com:3478",
            "turns:turn.cloudflare.com:5349?transport=tcp",
        ):
            with self.subTest(url=invalid), self.assertRaises(WebRtcSignalingError):
                validate_ice_servers([{"urls": [invalid]}], allow_turn=False)

        with self.assertRaises(WebRtcSignalingError):
            validate_ice_servers(
                [{"urls": [production], "username": "u", "credential": "p"}],
                allow_turn=False,
            )

        with self.assertRaises(WebRtcSignalingError):
            validate_ice_servers(
                [{"urls": [f"stun:stun-{index}.example:3478"]} for index in range(5)],
                allow_turn=False,
            )

    def test_configuration_is_bounded_deterministic_and_secret_safe(self) -> None:
        stun = [{"urls": ["stun:stun.example.test:3478"]}]
        normalized = validate_ice_servers(stun, allow_turn=False)
        first = capability_frame(True, normalized)
        second = capability_frame(True, normalized)
        self.assertEqual(first, second)
        self.assertEqual(first["configuration_id"], ice_configuration_id(True, normalized))
        self.assertNotIn("username", str(first))
        self.assertNotIn("credential", str(first))

        with self.assertRaises(WebRtcSignalingError):
            validate_ice_servers(
                [{"urls": ["turn:turn.example.test:3478"], "username": "u", "credential": "p"}],
                allow_turn=False,
            )
        turn = validate_ice_servers(
            [{"urls": "turns:turn.example.test:5349?transport=tcp", "username": "u", "credential": "p"}],
            allow_turn=True,
        )
        self.assertEqual(turn[0]["username"], "u")

    def test_turn_is_loaded_only_from_protected_environment(self) -> None:
        value = (
            '[{"urls":["turn:turn.example.test:3478?transport=udp"],'
            '"username":"protected-user","credential":"protected-secret"}]'
        )
        with mock.patch.dict(os.environ, {"OSIA_WEBRTC_ICE_SERVERS_JSON": value}):
            loaded = load_webrtc_ice_servers()
        self.assertEqual(loaded[0]["credential"], "protected-secret")
        with mock.patch.dict(os.environ, {"OSIA_WEBRTC_ICE_SERVERS_JSON": "not-json"}):
            with self.assertRaises(WebRtcSignalingError):
                load_webrtc_ice_servers()

    def test_signal_requires_exact_roles_attempt_generation_sequence_and_ttl(self) -> None:
        now = int(time.time())
        signal = make_signal(
            "webrtc_prepare",
            attempt_id="a" * 32,
            generation=7,
            source_role="mobile",
            sequence=1,
            payload={"configuration_id": "b" * 32},
            clock=lambda: now,
        )
        self.assertEqual(
            validate_signal(signal, destination_role="pc", clock=lambda: now),
            signal,
        )
        for field, value in (
            ("attempt_id", "wrong"),
            ("generation", 0),
            ("sequence", 0),
            ("destination_role", "mobile"),
        ):
            changed = {**signal, field: value}
            with self.subTest(field=field), self.assertRaises(WebRtcSignalingError):
                validate_signal(changed, destination_role="pc", clock=lambda: now)
        expired = {**signal, "created_at": now - 31}
        with self.assertRaises(WebRtcSignalingError):
            validate_signal(expired, destination_role="pc", clock=lambda: now)

    def test_data_only_sdp_is_stripped_and_media_or_oversize_is_rejected(self) -> None:
        stripped, candidates = strip_sdp_candidates(DATA_SDP)
        self.assertNotIn("candidate:", stripped)
        self.assertNotIn("end-of-candidates", stripped)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(validate_data_only_sdp(stripped), stripped)
        with self.assertRaises(WebRtcSignalingError):
            validate_data_only_sdp(DATA_SDP + "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n")
        with self.assertRaises(WebRtcSignalingError):
            validate_data_only_sdp("v=0\n" + ("x" * MAX_SDP_BYTES))

    def test_candidate_validation_dedup_and_batch_bounds(self) -> None:
        candidate = {
            "candidate": "candidate:1 1 udp 2130706431 192.168.1.2 50000 typ host",
            "sdp_mid": "0",
            "sdp_mline_index": 0,
        }
        self.assertEqual(validate_candidate(candidate), candidate)
        self.assertEqual(candidate_dedup_key(candidate), candidate_dedup_key(dict(candidate)))
        for changed in (
            {**candidate, "candidate": "candidate:1 2 udp 1 1.2.3.4 9 typ host"},
            {**candidate, "candidate": "candidate:1 1 udp 1 1.2.3.4 0 typ host"},
            {**candidate, "candidate": "candidate:1 1 udp 1 1.2.3.4 9 typ invalid"},
        ):
            with self.assertRaises(WebRtcSignalingError):
                validate_candidate(changed)
        with self.assertRaises(WebRtcSignalingError):
            make_signal(
                "webrtc_ice_candidates",
                attempt_id="a" * 32,
                generation=1,
                source_role="pc",
                sequence=1,
                payload={"candidates": [candidate] * (MAX_CANDIDATES_PER_EVENT + 1)},
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
