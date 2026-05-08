import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import Action, ActionType, State, hash_vk, process_batch  # noqa: E402
from verifier import _verify_payload  # noqa: E402


class PaymentSLTests(unittest.TestCase):
    def test_payload_matches_block_fields(self):
        issuer = "issuer_vk"
        user = "alice_vk"
        addr = hash_vk(user)
        state = State(issuer_vk=issuer)
        action = Action(ActionType.MINT, issuer, 1, to=addr, amount=100)
        _new_state, result = process_batch(state, [action])

        block = {
            "block_number": 1,
            "prev_state_hash": result.prev_state_hash,
            "new_state_hash": result.new_state_hash,
            "actions_applied": [action.to_dict()],
            "actions_rejected": [],
            "payload_hex": result.data_field_payload().hex(),
        }

        _verify_payload(block, "block_001")

    def test_payload_tampering_is_rejected(self):
        issuer = "issuer_vk"
        user = "alice_vk"
        addr = hash_vk(user)
        state = State(issuer_vk=issuer)
        action = Action(ActionType.MINT, issuer, 1, to=addr, amount=100)
        _new_state, result = process_batch(state, [action])

        payload_hex = result.data_field_payload().hex()
        tampered_payload_hex = payload_hex[:-1] + (
            "0" if payload_hex[-1] != "0" else "1"
        )
        block = {
            "block_number": 1,
            "prev_state_hash": result.prev_state_hash,
            "new_state_hash": result.new_state_hash,
            "actions_applied": [action.to_dict()],
            "actions_rejected": [],
            "payload_hex": tampered_payload_hex,
        }

        with self.assertRaises(SystemExit):
            _verify_payload(block, "block_001")


if __name__ == "__main__":
    unittest.main()
