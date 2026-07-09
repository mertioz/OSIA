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
    x25519_public_key,
)


VECTOR_PATH = ROOT / "tools" / "fixtures" / "relay_e2e_vectors.json"


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
            pc_device_id=cls.vector["pc_device_id"],
            mobile_device_id=cls.vector["mobile_device_id"],
            pc_session_id=cls.vector["pc_session_id"],
            mobile_session_id=cls.vector["mobile_session_id"],
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
            b64url_encode(derive_pair_secret(self.vector["token"])),
            self.vector["pair_secret_b64"],
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

    def test_pc_encrypt_vector_decrypts(self):
        envelope = self._encrypt_pc_to_mobile(seq=1)
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
            replay_key="pc-test-device",
        )
        with self.assertRaises(RelayReplayError):
            decrypt_frame(
                envelope,
                keys=self.mobile_keys,
                direction=PC_TO_MOBILE,
                replay_guard=guard,
                replay_key="pc-test-device",
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
            from_device_id=self.vector["pc_device_id"],
            to_device_id=self.vector["mobile_device_id"],
            session_id=self.vector["pc_session_id"],
            priority="critical",
            final=False,
            seq=seq,
            outbound_sequence=outbound_sequence,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
