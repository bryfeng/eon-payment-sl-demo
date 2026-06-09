import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PACKAGE_ROOT = ROOT.parent / "eon-marketplace-stack" / "packages"
for PACKAGE in ("eon-protocol-schemas", "eon-amm-framework", "eon-settlement-framework"):
    package_path = PACKAGE_ROOT / PACKAGE
    if package_path.exists():
        sys.path.insert(0, str(package_path))

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
from payment_plugin import PAYMENT_PLUGIN, PaymentSLPlugin  # noqa: E402
from verifier import _state_after_envelope, verify_envelope  # noqa: E402
from verifier_engine import PluginRegistry, VerifierEngine, VerifierStore  # noqa: E402
from verifier_engine.eon_data import encode_bundle_payload, payload_header  # noqa: E402

try:
    from eon_amm import AMMPlugin, AMMState  # noqa: E402
    from eon_amm.framework import expected_asset_movements  # noqa: E402
    from eon_settlement import SettlementContext, SettlementPlugin, SettlementState  # noqa: E402
except (ImportError, ModuleNotFoundError):  # pragma: no cover - standalone payment_sl checkout
    AMMPlugin = None
    AMMState = None
    expected_asset_movements = None
    SettlementContext = None
    SettlementPlugin = None
    SettlementState = None


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

    def test_engine_ingests_registered_child_from_bundle_event(self):
        envelope = self._valid_envelope()
        child_payload = bytes.fromhex(envelope["payload_hex"])
        bundle_payload = encode_bundle_payload(
            bundle_id="ab" * 32,
            children=[
                b"\x99\x99\x99\x99\x00\x01ignored",
                child_payload,
            ],
        )
        event = {
            "cursor": "devnet:bundle:0:0",
            "network_id": "devnet",
            "height": 1,
            "tx_hash": "0xbundle",
            "tx_index": 0,
            "output_index": 0,
            "utxo_id": "0xbundleutxo",
            "owner": "0xowner",
            "amount": "1",
            "data_scalars": payload_bytes_to_scalar_hex(bundle_payload),
        }

        with tempfile.TemporaryDirectory() as tmp:
            store = VerifierStore(Path(tmp) / "verifier.sqlite")
            engine = VerifierEngine(
                store,
                PluginRegistry([PAYMENT_PLUGIN]),
                {PAYMENT_PLUGIN.sl_id.hex(): {"issuer_vk": "issuer_vk"}},
            )

            result = engine.ingest_event(event)
            checkpoint = store.load_checkpoint(PAYMENT_PLUGIN.sl_id, next(iter(PAYMENT_PLUGIN.supported_versions)))
            log = store.list_verification_log(PAYMENT_PLUGIN.sl_id)

        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["bundle_id"], "ab" * 32)
        self.assertEqual(result["children"][0]["state_hash"], envelope["new_state_hash"])
        self.assertEqual(checkpoint["state_hash"], envelope["new_state_hash"])
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["event_key"], result["event_key"])

    def test_engine_retries_previously_ignored_bundle_when_plugin_is_registered(self):
        envelope = self._valid_envelope()
        bundle_payload = encode_bundle_payload(
            bundle_id="cd" * 32,
            children=[bytes.fromhex(envelope["payload_hex"])],
        )
        event = {
            "cursor": "devnet:bundle-retry:0:0",
            "network_id": "devnet",
            "height": 1,
            "tx_hash": "0xbundleretry",
            "tx_index": 0,
            "output_index": 0,
            "data_scalars": payload_bytes_to_scalar_hex(bundle_payload),
        }

        with tempfile.TemporaryDirectory() as tmp:
            store = VerifierStore(Path(tmp) / "verifier.sqlite")
            first_engine = VerifierEngine(store, PluginRegistry([]))
            ignored = first_engine.ingest_event(event)

            second_engine = VerifierEngine(
                store,
                PluginRegistry([PAYMENT_PLUGIN]),
                {PAYMENT_PLUGIN.sl_id.hex(): {"issuer_vk": "issuer_vk"}},
            )
            accepted = second_engine.ingest_event(event)
            checkpoint = store.load_checkpoint(PAYMENT_PLUGIN.sl_id, next(iter(PAYMENT_PLUGIN.supported_versions)))

        self.assertTrue(ignored["ignored"], ignored)
        self.assertIn("no registered semantic-layer children", ignored["message"])
        self.assertTrue(accepted["accepted"], accepted)
        self.assertEqual(checkpoint["state_hash"], envelope["new_state_hash"])

    @unittest.skipUnless(AMMPlugin and SettlementPlugin, "marketplace framework packages unavailable")
    def test_engine_verifies_marketplace_bundle_children_with_settlement_and_amm_context(self):
        version = PAYMENT_PLUGIN.version
        spx_plugin = PaymentSLPlugin(b"\x00\x01\x10\x01", version)
        usdc_plugin = PaymentSLPlugin(b"\x00\x01\x10\x02", version)
        amm_plugin = AMMPlugin(b"\x00\x04\x00\x01", version)
        settlement_plugin = SettlementPlugin(b"\x00\x03\x00\x01", version)
        provider_vk = "provider_vk"
        provider = hash_vk(provider_vk)
        pool_id = "pool-spx-usdc"
        spx_ref = {"sl_id": spx_plugin.sl_id.hex(), "version": version.hex(), "asset_id": "SPX"}
        usdc_ref = {"sl_id": usdc_plugin.sl_id.hex(), "version": version.hex(), "asset_id": "USDC"}

        def event_for_payload(payload: bytes, suffix: str, height: int = 1):
            return {
                "cursor": f"devnet:{height}:{suffix}:0",
                "network_id": "devnet",
                "height": height,
                "tx_hash": f"0x{suffix}",
                "tx_index": 0,
                "output_index": 0,
                "data_scalars": payload_bytes_to_scalar_hex(payload),
            }

        def context_transition(transition: dict):
            if "actions" in transition:
                return transition
            return {**transition, "actions": transition["actions_applied"]}

        spx_state, spx_seed = spx_plugin.build_payload(
            State(issuer_vk="spx_issuer"),
            [
                {
                    "type": "register_asset",
                    "sender_vk": "spx_issuer",
                    "nonce": 1,
                    "asset_id": "SPX",
                    "symbol": "SPX",
                    "asset_name": "SPX",
                },
                {
                    "type": "mint",
                    "sender_vk": "spx_issuer",
                    "nonce": 2,
                    "asset_id": "SPX",
                    "to": provider,
                    "amount": 1_000,
                },
            ],
            sequence=1,
        )
        usdc_state, usdc_seed = usdc_plugin.build_payload(
            State(issuer_vk="usdc_issuer"),
            [
                {
                    "type": "register_asset",
                    "sender_vk": "usdc_issuer",
                    "nonce": 1,
                    "asset_id": "USDC",
                    "symbol": "USDC",
                    "asset_name": "USDC",
                },
                {
                    "type": "mint",
                    "sender_vk": "usdc_issuer",
                    "nonce": 2,
                    "asset_id": "USDC",
                    "to": provider,
                    "amount": 2_000,
                },
            ],
            sequence=1,
        )

        amm_state = AMMState()
        settlement_state = SettlementState()
        create_bundle_id = "01" * 32
        create_action = {
            "type": "create_pool",
            "nonce": 1,
            "pool_id": pool_id,
            "asset_a": spx_ref,
            "asset_b": usdc_ref,
            "fee_bps": 30,
        }
        amm_state, create_amm_payload = amm_plugin.build_payload(
            amm_state,
            [create_action],
            sequence=1,
        )
        create_amm_transition = amm_plugin.parse_payload(create_amm_payload)
        create_settlement_context = SettlementContext(
            bundle_id=create_bundle_id,
            child_transitions=[create_amm_transition],
            height=2,
        )
        settlement_state, create_settlement_payload = settlement_plugin.build_payload(
            settlement_state,
            {
                "type": "settle_bundle",
                "nonce": 1,
                "bundle_id": create_bundle_id,
                "settlement_id": "create-pool",
                "expected_legs": [],
                "fees": [],
                "matcher_id": "test",
            },
            sequence=1,
            context=create_settlement_context,
        )
        create_bundle = encode_bundle_payload(
            bundle_id=create_bundle_id,
            children=[create_settlement_payload, create_amm_payload],
        )

        liquidity_bundle_id = "02" * 32
        liquidity_action = {
            "type": "add_liquidity",
            "nonce": 2,
            "pool_id": pool_id,
            "provider": provider,
            "amount_a": 100,
            "amount_b": 200,
            "min_lp_shares": 1,
            "asset_a_leg_id": "spx-deposit",
            "asset_b_leg_id": "usdc-deposit",
        }
        liquidity_action["asset_movements"] = expected_asset_movements(liquidity_action, amm_state)
        amm_next, liquidity_amm_payload = amm_plugin.build_payload(
            amm_state,
            [liquidity_action],
            sequence=2,
        )
        liquidity_amm_transition = amm_plugin.parse_payload(liquidity_amm_payload)
        asset_context = SimpleNamespace(
            bundle_id=liquidity_bundle_id,
            child_transitions=[liquidity_amm_transition],
            height=3,
        )
        spx_next, spx_child = spx_plugin.build_payload(
            spx_state,
            [
                {
                    "type": "pool_deposit",
                    "sender_vk": provider_vk,
                    "nonce": 3,
                    "asset_id": "SPX",
                    "pool_id": pool_id,
                    "leg_id": "spx-deposit",
                    "owner": provider,
                    "amount": 100,
                }
            ],
            sequence=2,
            bundle_id=liquidity_bundle_id,
            context=asset_context,
        )
        usdc_next, usdc_child = usdc_plugin.build_payload(
            usdc_state,
            [
                {
                    "type": "pool_deposit",
                    "sender_vk": provider_vk,
                    "nonce": 3,
                    "asset_id": "USDC",
                    "pool_id": pool_id,
                    "leg_id": "usdc-deposit",
                    "owner": provider,
                    "amount": 200,
                }
            ],
            sequence=2,
            bundle_id=liquidity_bundle_id,
            context=asset_context,
        )
        spx_transition = spx_plugin.parse_payload(spx_child)
        usdc_transition = usdc_plugin.parse_payload(usdc_child)
        settlement_context = SettlementContext(
            bundle_id=liquidity_bundle_id,
            child_transitions=[
                liquidity_amm_transition,
                context_transition(spx_transition),
                context_transition(usdc_transition),
            ],
            height=3,
        )
        settlement_state, settlement_child = settlement_plugin.build_payload(
            settlement_state,
            {
                "type": "settle_bundle",
                "nonce": 2,
                "bundle_id": liquidity_bundle_id,
                "settlement_id": "add-liquidity",
                "expected_legs": [
                    {
                        "sl_id": spx_plugin.sl_id.hex(),
                        "leg_id": "spx-deposit",
                        "pool_id": pool_id,
                        "asset_id": "SPX",
                        "from_addr": provider,
                        "to": f"pool:{pool_id}",
                        "amount": 100,
                    },
                    {
                        "sl_id": usdc_plugin.sl_id.hex(),
                        "leg_id": "usdc-deposit",
                        "pool_id": pool_id,
                        "asset_id": "USDC",
                        "from_addr": provider,
                        "to": f"pool:{pool_id}",
                        "amount": 200,
                    },
                ],
                "fees": [],
                "matcher_id": "test",
            },
            sequence=2,
            context=settlement_context,
        )
        liquidity_bundle = encode_bundle_payload(
            bundle_id=liquidity_bundle_id,
            children=[settlement_child, liquidity_amm_payload, spx_child, usdc_child],
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = VerifierStore(Path(tmp) / "verifier.sqlite")
            engine = VerifierEngine(
                store,
                PluginRegistry([settlement_plugin, amm_plugin, spx_plugin, usdc_plugin]),
                {
                    spx_plugin.sl_id.hex(): {"issuer_vk": "spx_issuer"},
                    usdc_plugin.sl_id.hex(): {"issuer_vk": "usdc_issuer"},
                },
            )

            self.assertTrue(engine.ingest_event(event_for_payload(spx_seed, "spxseed"))["accepted"])
            self.assertTrue(engine.ingest_event(event_for_payload(usdc_seed, "usdcseed"))["accepted"])
            self.assertTrue(engine.ingest_event(event_for_payload(create_bundle, "createpool", 2))["accepted"])
            result = engine.ingest_event(event_for_payload(liquidity_bundle, "liquidity", 3))
            spx_checkpoint = store.load_checkpoint(spx_plugin.sl_id, version)
            usdc_checkpoint = store.load_checkpoint(usdc_plugin.sl_id, version)
            amm_checkpoint = store.load_checkpoint(amm_plugin.sl_id, version)
            settlement_checkpoint = store.load_checkpoint(settlement_plugin.sl_id, version)

        self.assertTrue(result["accepted"], result)
        self.assertEqual({child["sl_id"] for child in result["children"]}, {
            settlement_plugin.sl_id.hex(),
            amm_plugin.sl_id.hex(),
            spx_plugin.sl_id.hex(),
            usdc_plugin.sl_id.hex(),
        })
        self.assertEqual(spx_checkpoint["state_hash"], spx_next.state_hash())
        self.assertEqual(usdc_checkpoint["state_hash"], usdc_next.state_hash())
        self.assertEqual(amm_checkpoint["state_hash"], amm_next.state_hash())
        self.assertEqual(settlement_checkpoint["state_hash"], settlement_state.state_hash())
        verified_spx = State.from_dict(spx_checkpoint["state"])
        verified_usdc = State.from_dict(usdc_checkpoint["state"])
        self.assertEqual(verified_spx.get_balance(provider, "SPX"), 900)
        self.assertEqual(verified_spx.get_pool_escrow(pool_id, "SPX"), 100)
        self.assertEqual(verified_usdc.get_balance(provider, "USDC"), 1_800)
        self.assertEqual(verified_usdc.get_pool_escrow(pool_id, "USDC"), 200)

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
