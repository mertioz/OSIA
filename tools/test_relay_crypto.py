#!/usr/bin/env python3
"""Cross-language relay E2E crypto vector tests."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from osia.relay_crypto import (  # noqa: E402
    MOBILE_TO_PC,
    PC_TO_MOBILE,
    OutboundSequence,
    RelayCryptoError,
    RelayReplayError,
    RelaySessionContext,
    RelaySessionKeys,
    SequenceReplayGuard,
    b64url_decode,
    b64url_encode,
    canonical_json,
    decrypt_frame,
    derive_e2e_psk,
    derive_pair_secret,
    derive_session_keys,
    encrypt_frame,
    handshake_confirmation_tag,
    x25519_public_key,
)
from osia.relay_connector import relay_pair_credentials  # noqa: E402


VECTOR_PATH = ROOT / "tools" / "fixtures" / "relay_v2_vectors.json"


class RelayCryptoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.vector = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))
        cls.pc_private_key = bytes.fromhex(cls.vector["pc_private_key_hex"])
        cls.mobile_private_key = bytes.fromhex(cls.vector["mobile_private_key_hex"])
        cls.pc_public_key = x25519_public_key(cls.pc_private_key)
        cls.mobile_public_key = x25519_public_key(cls.mobile_private_key)
        cls.context = RelaySessionContext(
            pair_id=cls.vector["pair_id"],
            pc_public_key=cls.pc_public_key,
            mobile_public_key=cls.mobile_public_key,
            pc_nonce_base=bytes.fromhex(cls.vector["pc_nonce_base_hex"]),
            mobile_nonce_base=bytes.fromhex(cls.vector["mobile_nonce_base_hex"]),
        )
        cls.pc_keys = derive_session_keys(
            token=cls.vector["token"],
            local_private_key=cls.pc_private_key,
            peer_public_key=cls.mobile_public_key,
            context=cls.context,
        )
        cls.mobile_keys = derive_session_keys(
            token=cls.vector["token"],
            local_private_key=cls.mobile_private_key,
            peer_public_key=cls.pc_public_key,
            context=cls.context,
        )

    def test_derive_same_session_keys_and_vector_ids(self):
        self.assertEqual(b64url_encode(self.pc_public_key), self.vector["pc_public_key_b64"])
        self.assertEqual(
            b64url_encode(self.mobile_public_key),
            self.vector["mobile_public_key_b64"],
        )
        self.assertEqual(
            derive_pair_secret(self.vector["token"]).hex(),
            self.vector["relay_proof_hex"],
        )
        self.assertEqual(
            hashlib.sha256(self.vector["relay_proof_hex"].encode("utf-8")).hexdigest(),
            self.vector["pair_id"],
        )
        self.assertEqual(
            relay_pair_credentials(self.vector["token"]),
            (self.vector["pair_id"], self.vector["relay_proof_hex"]),
        )
        self.assertEqual(self.context.transcript(), self.vector["transcript"])
        self.assertEqual(
            b64url_encode(self.context.transcript_bytes()),
            self.vector["transcript_utf8_b64"],
        )
        self.assertEqual(
            b64url_encode(derive_e2e_psk(self.vector["token"])),
            self.vector["e2e_psk_b64"],
        )
        self.assertEqual(
            hashlib.sha256(self.context.transcript_bytes()).hexdigest(),
            self.vector["transcript_sha256_hex"],
        )
        self.assertEqual(self.pc_keys.kid, self.mobile_keys.kid)
        self.assertEqual(self.pc_keys.kid, self.vector["kid"])
        self.assertEqual(
            self.pc_keys.pc_to_mobile_key.hex(),
            self.vector["pc_to_mobile_key_hex"],
        )
        self.assertEqual(
            self.pc_keys.mobile_to_pc_key.hex(),
            self.vector["mobile_to_pc_key_hex"],
        )
        self.assertEqual(
            handshake_confirmation_tag(
                token=self.vector["token"], context=self.context, sender_role="pc"
            ),
            self.vector["pc_ready_tag"],
        )
        self.assertEqual(
            handshake_confirmation_tag(
                token=self.vector["token"], context=self.context, sender_role="mobile"
            ),
            self.vector["mobile_ready_tag"],
        )

    def test_pc_encrypt_vector_decrypts(self):
        envelope = self._encrypt_pc_to_mobile(seq=4)
        self.assertEqual(envelope, self.vector["pc_to_mobile_envelope"])
        self.assertEqual(
            decrypt_frame(envelope, keys=self.mobile_keys, direction=PC_TO_MOBILE),
            self.vector["plaintext_frame"],
        )

    def test_mobile_vector_decryptable_by_pc(self):
        self.assertEqual(
            decrypt_frame(
                self.vector["mobile_to_pc_envelope"],
                keys=self.pc_keys,
                direction=MOBILE_TO_PC,
            ),
            self.vector["plaintext_frame"],
        )

    def test_large_chunk_vector_matches_cross_language_ciphertext(self):
        template = self.vector["large_frame_template"]
        frame = {
            "type": template["type"],
            "client_request_id": template["client_request_id"],
            "turn_id": template["turn_id"],
            "conversation_id": template["conversation_id"],
            "content": template["content_char"] * template["content_count"],
        }
        expected = self.vector["large_pc_to_mobile"]
        envelope = encrypt_frame(
            frame,
            keys=self.pc_keys,
            direction=PC_TO_MOBILE,
            pair_id=self.vector["pair_id"],
            role="pc",
            seq=expected["seq"],
            priority=expected["priority"],
            final=expected["final"],
        )
        ciphertext = envelope["ciphertext"]
        self.assertEqual(envelope["nonce"], expected["nonce"])
        self.assertEqual(len(ciphertext), expected["ciphertext_chars"])
        self.assertEqual(
            hashlib.sha256(ciphertext.encode("ascii")).hexdigest(),
            expected["ciphertext_sha256_hex"],
        )
        pieces = [
            ciphertext[offset : offset + 40960]
            for offset in range(0, len(ciphertext), 40960)
        ]
        self.assertEqual(len(pieces), expected["chunk_count"])
        self.assertEqual(len(pieces[0]), expected["first_chunk_chars"])
        self.assertEqual(len(pieces[-1]), expected["last_chunk_chars"])

    def test_large_mobile_chunk_vector_matches_cross_language_ciphertext(self):
        template = self.vector["large_frame_template"]
        frame = {
            "type": template["type"],
            "client_request_id": template["client_request_id"],
            "turn_id": template["turn_id"],
            "conversation_id": template["conversation_id"],
            "content": template["content_char"] * template["content_count"],
        }
        expected = self.vector["large_mobile_to_pc"]
        envelope = encrypt_frame(
            frame,
            keys=self.mobile_keys,
            direction=MOBILE_TO_PC,
            pair_id=self.vector["pair_id"],
            role="mobile",
            seq=expected["seq"],
            priority=expected["priority"],
            final=expected["final"],
        )
        ciphertext = envelope["ciphertext"]
        self.assertEqual(envelope["nonce"], expected["nonce"])
        self.assertEqual(len(ciphertext), expected["ciphertext_chars"])
        self.assertEqual(
            hashlib.sha256(ciphertext.encode("ascii")).hexdigest(),
            expected["ciphertext_sha256_hex"],
        )
        pieces = [
            ciphertext[offset : offset + 40960]
            for offset in range(0, len(ciphertext), 40960)
        ]
        self.assertEqual(len(pieces), expected["chunk_count"])
        self.assertEqual(len(pieces[0]), expected["first_chunk_chars"])
        self.assertEqual(len(pieces[-1]), expected["last_chunk_chars"])

    def test_changed_transcript_field_changes_keys_and_rejects_vector(self):
        changed_context = RelaySessionContext(
            pair_id=self.context.pair_id,
            pc_public_key=self.context.pc_public_key,
            mobile_public_key=self.context.mobile_public_key,
            pc_nonce_base=self.context.pc_nonce_base,
            mobile_nonce_base=b"\xb0\xb1\xb2\xb4",
        )
        changed_keys = derive_session_keys(
            token=self.vector["token"],
            local_private_key=self.pc_private_key,
            peer_public_key=self.mobile_public_key,
            context=changed_context,
        )
        self.assertNotEqual(changed_keys.kid, self.pc_keys.kid)
        self.assertNotEqual(
            handshake_confirmation_tag(
                token=self.vector["token"],
                context=changed_context,
                sender_role="pc",
            ),
            self.vector["pc_ready_tag"],
        )
        with self.assertRaises(RelayCryptoError):
            decrypt_frame(
                self.vector["pc_to_mobile_envelope"],
                keys=changed_keys,
                direction=PC_TO_MOBILE,
            )
        self.assertTrue(
            {"device_id", "session_id", "from", "to", "to_session_id"}.isdisjoint(
                changed_context.transcript()
            )
        )

    def test_wrong_token_fails(self):
        wrong = derive_session_keys(
            token="wrong-token",
            local_private_key=self.pc_private_key,
            peer_public_key=self.mobile_public_key,
            context=self.context,
        )
        self.assertNotEqual(wrong.kid, self.pc_keys.kid)
        with self.assertRaises(RelayCryptoError):
            decrypt_frame(
                self.vector["pc_to_mobile_envelope"],
                keys=wrong,
                direction=PC_TO_MOBILE,
            )

        wrong_same_kid = RelaySessionKeys(
            pc_to_mobile_key=wrong.pc_to_mobile_key,
            mobile_to_pc_key=wrong.mobile_to_pc_key,
            pc_nonce_base=wrong.pc_nonce_base,
            mobile_nonce_base=wrong.mobile_nonce_base,
            kid=self.pc_keys.kid,
        )
        with self.assertRaises(RelayCryptoError):
            decrypt_frame(
                self.vector["pc_to_mobile_envelope"],
                keys=wrong_same_kid,
                direction=PC_TO_MOBILE,
            )

    def test_wrong_aad_fails(self):
        envelope = copy.deepcopy(self.vector["pc_to_mobile_envelope"])
        envelope["priority"] = "normal"
        with self.assertRaises(RelayCryptoError):
            decrypt_frame(envelope, keys=self.mobile_keys, direction=PC_TO_MOBILE)

    def test_wrong_role_and_pair_fail(self):
        for field, value in (("role", "mobile"), ("pair_id", "0" * 64)):
            with self.subTest(field=field):
                envelope = copy.deepcopy(self.vector["pc_to_mobile_envelope"])
                envelope[field] = value
                with self.assertRaises(RelayCryptoError):
                    decrypt_frame(
                        envelope, keys=self.mobile_keys, direction=PC_TO_MOBILE
                    )

    def test_legacy_identity_field_is_rejected(self):
        envelope = copy.deepcopy(self.vector["pc_to_mobile_envelope"])
        envelope["device_id"] = "pc-stable-id"
        with self.assertRaises(RelayCryptoError):
            decrypt_frame(envelope, keys=self.mobile_keys, direction=PC_TO_MOBILE)

    def test_tampered_ciphertext_fails(self):
        envelope = copy.deepcopy(self.vector["pc_to_mobile_envelope"])
        ciphertext = bytearray(b64url_decode(envelope["ciphertext"]))
        ciphertext[-1] ^= 0x01
        envelope["ciphertext"] = b64url_encode(bytes(ciphertext))
        with self.assertRaises(RelayCryptoError):
            decrypt_frame(envelope, keys=self.mobile_keys, direction=PC_TO_MOBILE)

    def test_replayed_sequence_rejected(self):
        guard = SequenceReplayGuard()
        envelope = self.vector["pc_to_mobile_envelope"]
        decrypt_frame(
            envelope,
            keys=self.mobile_keys,
            direction=PC_TO_MOBILE,
            replay_guard=guard,
            replay_key="pc",
        )
        with self.assertRaises(RelayReplayError):
            decrypt_frame(
                envelope,
                keys=self.mobile_keys,
                direction=PC_TO_MOBILE,
                replay_guard=guard,
                replay_key="pc",
            )

    def test_same_sequence_not_allowed_for_normal_encryptor(self):
        sequence = OutboundSequence()
        self._encrypt_pc_to_mobile(outbound_sequence=sequence)
        with self.assertRaises(RelayReplayError):
            self._encrypt_pc_to_mobile(seq=1, outbound_sequence=sequence)

    def test_canonical_json_is_stable(self):
        value = {"z": [{"b": 2, "a": 1}], "a": True, "m": None}
        self.assertEqual(
            canonical_json(value).decode("utf-8"),
            '{"a":true,"m":null,"z":[{"a":1,"b":2}]}',
        )

    def _encrypt_pc_to_mobile(
        self,
        *,
        seq: int | None = None,
        outbound_sequence: OutboundSequence | None = None,
    ) -> dict[str, object]:
        return encrypt_frame(
            self.vector["plaintext_frame"],
            keys=self.pc_keys,
            direction=PC_TO_MOBILE,
            pair_id=self.vector["pair_id"],
            role="pc",
            priority="critical",
            final=False,
            seq=seq,
            outbound_sequence=outbound_sequence,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
