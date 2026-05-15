import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api  # noqa: E402
from core import SL_ID, hash_vk  # noqa: E402


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        api.configure_storage(Path(self.tmp.name))
        self.client = TestClient(api.app)

    def tearDown(self):
        api.configure_storage()
        self.tmp.cleanup()

    def _init(self):
        response = self.client.post(
            "/operator/init",
            json={"issuer_vk": "issuer_vk"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _wallet(self, label, vk):
        response = self.client.post(
            "/wallets",
            json={"label": label, "vk": vk},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _operator_wallet(self, label="Issuer Operator", vk="issuer_operator_vk"):
        response = self.client.post(
            "/wallets",
            json={"label": label, "vk": vk, "kind": "sl_operator"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_root_liveness_message(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["message"],
            "EON Payment SL Playground API is live.",
        )
        self.assertEqual(response.json()["health"], "/health")

    def test_full_mint_verify_transfer_flow(self):
        self._init()
        alice = self._wallet("Alice", "alice_vk")
        bob = self._wallet("Bob", "bob_vk")

        response = self.client.post(
            "/actions/mint",
            json={"to_address": alice["address"], "amount": 100},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["pending_count"], 1)

        response = self.client.post("/operator/batch")
        self.assertEqual(response.status_code, 200, response.text)
        batch = response.json()["batch"]
        self.assertTrue(response.json()["batched"])
        self.assertEqual(batch["sequence"], 1)
        self.assertEqual(batch["applied"], 1)
        self.assertIn("payload_hex", batch)
        self.assertIn("data_scalars", batch)

        response = self.client.post("/verifier/accept-latest-batch")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sequence"], 1)

        response = self.client.get(f"/balances/{alice['address']}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["balance"], 100)
        self.assertEqual(response.json()["source"], "verifier")

        response = self.client.post(
            "/actions/transfer",
            json={
                "from_address": alice["address"],
                "to_address": bob["address"],
                "amount": 40,
                "vk": "alice_vk",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.post("/operator/batch")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["batch"]["sequence"], 2)

        response = self.client.post("/verifier/accept-latest-batch")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sequence"], 2)

        alice_balance = self.client.get(f"/balances/{alice['address']}").json()
        bob_balance = self.client.get(f"/balances/{bob['address']}").json()
        self.assertEqual(alice_balance["balance"], 60)
        self.assertEqual(bob_balance["balance"], 40)

    def test_transfer_rejects_vk_mismatch(self):
        self._init()
        alice = self._wallet("Alice", "alice_vk")
        bob = self._wallet("Bob", "bob_vk")

        response = self.client.post(
            "/actions/transfer",
            json={
                "from_address": alice["address"],
                "to_address": bob["address"],
                "amount": 1,
                "vk": "not_alice_vk",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("vk does not match", response.json()["detail"])

    def test_no_pending_batch_is_noop(self):
        self._init()

        response = self.client.post("/operator/batch")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["batched"])

    def test_wallet_registration_can_accept_address_only(self):
        self._init()
        address = hash_vk("external_vk")

        response = self.client.post(
            "/wallets",
            json={"label": "External", "address": address},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["address"], address)
        self.assertEqual(response.json()["kind"], "user")
        self.assertFalse(response.json()["derived_from_vk"])

    def test_wallet_registration_stores_kind_before_runtime_init(self):
        operator = self._operator_wallet()

        response = self.client.get("/wallets")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(operator["kind"], "sl_operator")
        self.assertEqual(response.json()["wallets"][0]["kind"], "sl_operator")

    def test_semantic_layer_records_can_be_created_and_listed(self):
        operator = self._operator_wallet()

        response = self.client.post(
            "/semantic-layers",
            json={
                "name": "Payment SL",
                "sl_id": SL_ID.hex(),
                "version": "0001",
                "operator_wallet_address": operator["address"],
                "issuer_vk_ref": f"local:{operator['address']}",
                "operator_vk_ref": f"local:{operator['address']}",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["name"], "Payment SL")
        self.assertEqual(response.json()["operator_wallet_address"], operator["address"])

        response = self.client.get("/semantic-layers")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["semantic_layers"]), 1)
        self.assertEqual(
            response.json()["semantic_layers"][0]["operator_wallet_address"],
            operator["address"],
        )

    def test_semantic_layer_rejects_invalid_operator_address(self):
        response = self.client.post(
            "/semantic-layers",
            json={
                "name": "Broken SL",
                "sl_id": SL_ID.hex(),
                "version": "0001",
                "operator_wallet_address": "not_hex",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("address must", response.json()["detail"])

    def test_semantic_layer_requires_operator_wallet_kind(self):
        user = self._wallet("Alice", "alice_vk")

        response = self.client.post(
            "/semantic-layers",
            json={
                "name": "Payment SL",
                "sl_id": SL_ID.hex(),
                "version": "0001",
                "operator_wallet_address": user["address"],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("kind=sl_operator", response.json()["detail"])

    def test_sqlite_state_persists_after_reconfigure(self):
        self._init()
        alice = self._wallet("Alice", "alice_vk")
        db_path = Path(self.tmp.name) / "payment_sl.sqlite"

        self.assertTrue(db_path.exists())

        api.configure_storage(db_path=db_path)
        fresh_client = TestClient(api.app)

        response = fresh_client.get("/config")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["initialized"])

        response = fresh_client.get(f"/wallets/{alice['address']}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["label"], "Alice")

    def test_verifier_ingests_normalized_base_event(self):
        self._init()
        alice = self._wallet("Alice", "alice_vk")

        response = self.client.post(
            "/actions/mint",
            json={"to_address": alice["address"], "amount": 100},
        )
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.post("/operator/batch")
        self.assertEqual(response.status_code, 200, response.text)
        batch = response.json()["batch"]

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
            "data_scalars": batch["data_scalars"],
        }
        response = self.client.post("/verifier/ingest-event", json=event)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["accepted"])

        response = self.client.get(f"/verifier/state?sl_id={SL_ID.hex()}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"]["total_supply"], 100)

        response = self.client.get(f"/verifier/log?sl_id={SL_ID.hex()}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["log"]), 1)

        response = self.client.get("/verifier/events")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["events"]), 1)


if __name__ == "__main__":
    unittest.main()
