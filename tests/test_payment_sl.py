import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


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
    verify_batch,
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

    def _event_for_envelope(self, envelope, suffix="1"):
        return {
            "cursor": f"devnet:{suffix}:0:0",
            "network_id": "devnet",
            "height": int(suffix),
            "tx_hash": f"0xtx{suffix}",
            "tx_index": 0,
            "output_index": 0,
            "utxo_id": f"0xutxo{suffix}",
            "owner": "0xowner",
            "amount": "1",
            "data_scalars": payload_bytes_to_scalar_hex(bytes.fromhex(envelope["payload_hex"])),
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

    def test_register_asset_mint_transfer_round_trip(self):
        issuer = "issuer_vk"
        alice = hash_vk("alice_vk")
        bob = hash_vk("bob_vk")
        state = State(issuer_vk=issuer)
        actions = [
            Action(
                ActionType.REGISTER_ASSET,
                issuer,
                1,
                asset_id="BSTK",
                symbol="BSTK",
                asset_name="bStocks",
                asset_type="equity",
            ),
            Action(ActionType.MINT, issuer, 2, asset_id="BSTK", to=alice, amount=100),
            Action(
                ActionType.TRANSFER,
                "alice_vk",
                3,
                asset_id="BSTK",
                from_addr=alice,
                to=bob,
                amount=40,
            ),
        ]

        next_state, result = process_batch(state, actions)
        decoded = parse_data_field_payload(result.data_field_payload())

        self.assertEqual(decoded["actions_applied"], [action.to_dict() for action in actions])
        self.assertEqual(next_state.get_balance(alice, "BSTK"), 60)
        self.assertEqual(next_state.get_balance(bob, "BSTK"), 40)
        self.assertEqual(next_state.get_total_supply("BSTK"), 100)
        self.assertEqual(next_state.total_supply, 0)

    def test_pool_escrow_actions_require_matching_amm_movements(self):
        issuer = "issuer_vk"
        alice_vk = "alice_vk"
        alice = hash_vk(alice_vk)
        pool_id = "pool-spx-usd"
        bundle_id = "11" * 32
        state = State(issuer_vk=issuer)
        seeded, _seed_result = process_batch(
            state,
            [
                Action(
                    ActionType.REGISTER_ASSET,
                    issuer,
                    1,
                    asset_id="SPX",
                    symbol="SPX",
                    asset_name="SPX",
                ),
                Action(ActionType.MINT, issuer, 2, asset_id="SPX", to=alice, amount=1_000),
            ],
        )
        movements = [
            {
                "kind": "swap_in",
                "leg_id": "spx-in",
                "pool_id": pool_id,
                "sl_id": PAYMENT_PLUGIN.sl_id.hex(),
                "version": PAYMENT_PLUGIN.version.hex(),
                "asset_id": "SPX",
                "address": alice,
                "amount": 100,
            },
            {
                "kind": "swap_out",
                "leg_id": "spx-out",
                "pool_id": pool_id,
                "sl_id": PAYMENT_PLUGIN.sl_id.hex(),
                "version": PAYMENT_PLUGIN.version.hex(),
                "asset_id": "SPX",
                "address": alice,
                "amount": 25,
            },
        ]
        context = SimpleNamespace(
            bundle_id=bundle_id,
            child_transitions=[{"sl_id": "00040001", "actions": [{"asset_movements": movements}]}],
        )
        seeded.credit_pool_escrow(pool_id, 40, "SPX")

        next_state, result = process_batch(
            seeded,
            [
                Action(
                    ActionType.POOL_SWAP_IN,
                    alice_vk,
                    3,
                    asset_id="SPX",
                    pool_id=pool_id,
                    leg_id="spx-in",
                    trader=alice,
                    amount=100,
                    bundle_id=bundle_id,
                ),
                Action(
                    ActionType.POOL_SWAP_OUT,
                    alice_vk,
                    4,
                    asset_id="SPX",
                    pool_id=pool_id,
                    leg_id="spx-out",
                    trader=alice,
                    amount=25,
                    bundle_id=bundle_id,
                ),
            ],
            context=context,
        )

        self.assertEqual(result.rejected, [])
        self.assertEqual(next_state.get_balance(alice, "SPX"), 925)
        self.assertEqual(next_state.get_pool_escrow(pool_id, "SPX"), 115)

        valid, msg = verify_batch(seeded, result.actions, result.new_state_hash, context=context)
        self.assertTrue(valid, msg)

    def test_pool_escrow_rejects_unmatched_amm_movement(self):
        issuer = "issuer_vk"
        alice_vk = "alice_vk"
        alice = hash_vk(alice_vk)
        state = State(issuer_vk=issuer)
        seeded, _seed_result = process_batch(
            state,
            [
                Action(
                    ActionType.REGISTER_ASSET,
                    issuer,
                    1,
                    asset_id="SPX",
                    symbol="SPX",
                    asset_name="SPX",
                ),
                Action(ActionType.MINT, issuer, 2, asset_id="SPX", to=alice, amount=1_000),
            ],
        )

        _next_state, result = process_batch(
            seeded,
            [
                Action(
                    ActionType.POOL_SWAP_IN,
                    alice_vk,
                    3,
                    asset_id="SPX",
                    pool_id="pool-spx-usd",
                    leg_id="spx-in",
                    trader=alice,
                    amount=100,
                    bundle_id="11" * 32,
                )
            ],
            context=SimpleNamespace(bundle_id="11" * 32, child_transitions=[]),
        )

        self.assertEqual(result.applied, 0)
        self.assertIn("not authorized by AMM movement", result.rejected[0][1])

    def test_custom_asset_hash_survives_sorted_json_round_trip(self):
        issuer = "issuer_vk"
        alice = hash_vk("alice_vk")
        state = State(issuer_vk=issuer)
        actions = [
            Action(
                ActionType.REGISTER_ASSET,
                issuer,
                1,
                asset_id="BSTK",
                symbol="BSTK",
                asset_name="bStocks",
                decimals=0,
                asset_type="equity",
                metadata={},
            ),
            Action(ActionType.MINT, issuer, 2, asset_id="BSTK", to=alice, amount=100),
        ]

        next_state, _result = process_batch(state, actions)
        persisted = json.loads(
            json.dumps(next_state.to_dict(), separators=(",", ":"), sort_keys=True)
        )
        rehydrated = State.from_dict(persisted)

        self.assertEqual(rehydrated.state_hash(), next_state.state_hash())

    def test_devnet_scalar_framing_round_trip(self):
        envelope = self._valid_envelope()
        payload = bytes.fromhex(envelope["payload_hex"])
        scalars = payload_bytes_to_scalar_hex(payload)
        self.assertTrue(all(int(scalar, 16) < 0x40000000 for scalar in scalars[1:]))
        self.assertEqual(scalar_hex_to_payload_bytes(scalars), payload)

        prev_state = State.from_dict(envelope["prev_state"])
        from_scalars = envelope_from_scalars(scalars, prev_state)
        self.assertEqual(from_scalars, envelope)

    def test_devnet_scalar_framing_preserves_high_bytes(self):
        payload = bytes(range(256)) + b"\xff" * 97
        scalars = payload_bytes_to_scalar_hex(payload)

        self.assertEqual(scalar_hex_to_payload_bytes(scalars), payload)
        self.assertTrue(all(int(scalar, 16) < 0x40000000 for scalar in scalars[1:]))

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
        event = self._event_for_envelope(envelope)

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

    def test_engine_defers_future_sequence_without_poisoning_event(self):
        issuer = "issuer_vk"
        alice = hash_vk("alice_vk")
        genesis = State(issuer_vk=issuer)
        state_one, result_one = process_batch(
            genesis,
            [Action(ActionType.MINT, issuer, 1, to=alice, amount=100)],
            sequence=1,
        )
        _state_two, result_two = process_batch(
            state_one,
            [Action(ActionType.MINT, issuer, 2, to=alice, amount=50)],
            sequence=2,
        )
        envelope_one = {
            "prev_state": genesis.to_dict(),
            "sequence": result_one.sequence,
            "prev_state_hash": result_one.prev_state_hash,
            "new_state_hash": result_one.new_state_hash,
            "actions_applied": [action.to_dict() for action in result_one.actions],
            "payload_hex": result_one.data_field_payload().hex(),
        }
        envelope_two = {
            "prev_state": state_one.to_dict(),
            "sequence": result_two.sequence,
            "prev_state_hash": result_two.prev_state_hash,
            "new_state_hash": result_two.new_state_hash,
            "actions_applied": [action.to_dict() for action in result_two.actions],
            "payload_hex": result_two.data_field_payload().hex(),
        }

        with tempfile.TemporaryDirectory() as tmp:
            store = VerifierStore(Path(tmp) / "verifier.sqlite")
            engine = VerifierEngine(
                store,
                PluginRegistry([PAYMENT_PLUGIN]),
                {PAYMENT_PLUGIN.sl_id.hex(): {"issuer_vk": issuer}},
            )

            deferred = engine.ingest_event(self._event_for_envelope(envelope_two, "2"))
            first = engine.ingest_event(self._event_for_envelope(envelope_one, "1"))
            second = engine.ingest_event(self._event_for_envelope(envelope_two, "2"))
            checkpoint = store.load_checkpoint(PAYMENT_PLUGIN.sl_id, next(iter(PAYMENT_PLUGIN.supported_versions)))

        self.assertTrue(deferred["deferred"], deferred)
        self.assertFalse(deferred["stored"], deferred)
        self.assertTrue(first["accepted"], first)
        self.assertTrue(second["accepted"], second)
        self.assertEqual(checkpoint["state_hash"], envelope_two["new_state_hash"])

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

    def test_malformed_payment_event_does_not_block_later_valid_event(self):
        envelope = self._valid_envelope()
        malformed_payload = PAYMENT_PLUGIN.sl_id + next(iter(PAYMENT_PLUGIN.supported_versions)) + b"bad"
        malformed_event = {
            "cursor": "devnet:bad:0:0",
            "network_id": "devnet",
            "height": 1,
            "tx_hash": "0xbad",
            "tx_index": 0,
            "output_index": 0,
            "data_scalars": payload_bytes_to_scalar_hex(malformed_payload),
        }

        with tempfile.TemporaryDirectory() as tmp:
            store = VerifierStore(Path(tmp) / "verifier.sqlite")
            engine = VerifierEngine(
                store,
                PluginRegistry([PAYMENT_PLUGIN]),
                {PAYMENT_PLUGIN.sl_id.hex(): {"issuer_vk": "issuer_vk"}},
            )

            ignored = engine.ingest_event(malformed_event)
            accepted = engine.ingest_event(self._event_for_envelope(envelope))
            checkpoint = store.load_checkpoint(PAYMENT_PLUGIN.sl_id, next(iter(PAYMENT_PLUGIN.supported_versions)))

        self.assertTrue(ignored["ignored"], ignored)
        self.assertIn("could not parse semantic-layer payload", ignored["message"])
        self.assertTrue(accepted["accepted"], accepted)
        self.assertEqual(checkpoint["state_hash"], envelope["new_state_hash"])


if __name__ == "__main__":
    unittest.main()
