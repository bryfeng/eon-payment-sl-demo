import sys
import tempfile
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
from payment_plugin import PAYMENT_PLUGIN  # noqa: E402
from verifier import _state_after_envelope, verify_envelope  # noqa: E402
from verifier_engine import PluginRegistry, VerifierEngine, VerifierStore  # noqa: E402
from verifier_engine.eon_data import payload_header  # noqa: E402


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

    def test_plugin_registry_dispatches_payment_sl(self):
        registry = PluginRegistry([PAYMENT_PLUGIN])

        plugin = registry.get(*payload_header(bytes.fromhex(self._valid_envelope()["payload_hex"])))

        self.assertIs(plugin, PAYMENT_PLUGIN)

    def test_engine_ingests_payment_event_and_is_idempotent(self):
        envelope = self._valid_envelope()
        event = {
            "cursor": "devnet:1:0:0",
            "network_id": "devnet",
            "height": 1,
            "tx_hash": "0xtx",
            "tx_index": 0,
            "output_index": 0,
            "utxo_id": "0xutxo",
            "owner": "0xowner",
            "amount": "1",
            "data_scalars": payload_bytes_to_scalar_hex(bytes.fromhex(envelope["payload_hex"])),
        }

        with tempfile.TemporaryDirectory() as tmp:
            store = VerifierStore(Path(tmp) / "verifier.sqlite")
            engine = VerifierEngine(
                store,
                PluginRegistry([PAYMENT_PLUGIN]),
                {PAYMENT_PLUGIN.sl_id.hex(): {"issuer_vk": "issuer_vk"}},
            )

            first = engine.ingest_event(event)
            second = engine.ingest_event(event)
            checkpoint = store.load_checkpoint(PAYMENT_PLUGIN.sl_id, next(iter(PAYMENT_PLUGIN.supported_versions)))

        self.assertTrue(first["accepted"], first)
        self.assertTrue(second["duplicate"], second)
        self.assertEqual(checkpoint["state_hash"], envelope["new_state_hash"])

    def test_unknown_sl_id_event_is_stored_not_verified(self):
        payload = b"\xff\xff\xff\xff\x00\x01ignored"
        event = {
            "cursor": "devnet:1:0:0",
            "network_id": "devnet",
            "height": 1,
            "tx_hash": "0xunknown",
            "tx_index": 0,
            "output_index": 0,
            "data_scalars": payload_bytes_to_scalar_hex(payload),
        }

        with tempfile.TemporaryDirectory() as tmp:
            store = VerifierStore(Path(tmp) / "verifier.sqlite")
            engine = VerifierEngine(store, PluginRegistry([PAYMENT_PLUGIN]))

            result = engine.ingest_event(event)
            events = store.list_base_events()
            log = store.list_verification_log(PAYMENT_PLUGIN.sl_id)

        self.assertTrue(result["ignored"], result)
        self.assertEqual(len(events), 1)
        self.assertEqual(log, [])


if __name__ == "__main__":
    unittest.main()
