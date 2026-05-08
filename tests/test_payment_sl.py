import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import (  # noqa: E402
    Action,
    ActionType,
    PayloadDecodeError,
    State,
    hash_vk,
    parse_data_field_payload,
    process_batch,
)
from devnet_adapter import (  # noqa: E402
    envelope_from_scalars,
    payload_bytes_to_scalar_hex,
    scalar_hex_to_payload_bytes,
)
from verifier import _state_after_envelope, verify_envelope  # noqa: E402


class PaymentSLTests(unittest.TestCase):
    def _valid_envelope(self):
        issuer = "issuer_vk"
        user = "alice_vk"
        addr = hash_vk(user)
        state = State(issuer_vk=issuer)
        action = Action(ActionType.MINT, issuer, 1, to=addr, amount=100)
        _new_state, result = process_batch(state, [action])
        return {
            "prev_state": state.to_dict(),
            "sequence": result.sequence,
            "prev_state_hash": result.prev_state_hash,
            "new_state_hash": result.new_state_hash,
            "actions_applied": [action.to_dict()],
            "payload_hex": result.data_field_payload().hex(),
        }

    def test_envelope_verifies(self):
        valid, msg = verify_envelope(self._valid_envelope())
        self.assertTrue(valid, msg)

    def test_envelope_produces_verified_state(self):
        envelope = self._valid_envelope()
        state = _state_after_envelope(envelope)
        self.assertEqual(state.state_hash(), envelope["new_state_hash"])
        self.assertEqual(state.total_supply, 100)

    def test_payload_tampering_is_rejected(self):
        envelope = self._valid_envelope()
        payload_hex = envelope["payload_hex"]
        envelope["payload_hex"] = payload_hex[:-1] + (
            "0" if payload_hex[-1] != "0" else "1"
        )

        valid, msg = verify_envelope(envelope)
        self.assertFalse(valid)
        self.assertIn("payload_hex", msg)

    def test_sequence_tampering_is_rejected(self):
        envelope = self._valid_envelope()
        envelope["sequence"] = 2

        valid, msg = verify_envelope(envelope)
        self.assertFalse(valid)
        self.assertIn("payload_hex", msg)

    def test_payload_parser_round_trip(self):
        envelope = self._valid_envelope()
        decoded = parse_data_field_payload(bytes.fromhex(envelope["payload_hex"]))
        self.assertEqual(decoded["sequence"], envelope["sequence"])
        self.assertEqual(decoded["prev_state_hash"], envelope["prev_state_hash"])
        self.assertEqual(decoded["new_state_hash"], envelope["new_state_hash"])
        self.assertEqual(decoded["actions_applied"], envelope["actions_applied"])

    def test_devnet_scalar_framing_round_trip(self):
        envelope = self._valid_envelope()
        payload = bytes.fromhex(envelope["payload_hex"])
        scalars = payload_bytes_to_scalar_hex(payload)
        self.assertEqual(scalar_hex_to_payload_bytes(scalars), payload)

        prev_state = State.from_dict(envelope["prev_state"])
        from_scalars = envelope_from_scalars(scalars, prev_state)
        self.assertEqual(from_scalars, envelope)

    def test_adapter_rejects_wrong_previous_state(self):
        envelope = self._valid_envelope()
        payload = bytes.fromhex(envelope["payload_hex"])
        wrong_state = State(issuer_vk="other_issuer")

        with self.assertRaises(PayloadDecodeError):
            envelope_from_scalars(payload_bytes_to_scalar_hex(payload), wrong_state)


if __name__ == "__main__":
    unittest.main()
