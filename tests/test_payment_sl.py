import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import Action, ActionType, State, hash_vk, process_batch  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
