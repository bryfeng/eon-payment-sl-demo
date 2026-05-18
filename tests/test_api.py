import json
import os
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
        self.env_backup = {
            "EON_DEVNET_SUBMIT_CMD": os.environ.get("EON_DEVNET_SUBMIT_CMD"),
            "EON_DEVNET_API_URL": os.environ.get("EON_DEVNET_API_URL"),
            "EON_OPERATOR_WALLET_FILE": os.environ.get("EON_OPERATOR_WALLET_FILE"),
            "EON_KEY_ENCRYPTION_SECRET": os.environ.get("EON_KEY_ENCRYPTION_SECRET"),
        }
        api.configure_storage(Path(self.tmp.name))
        self.client = TestClient(api.app)

    def tearDown(self):
        for key, value in self.env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
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

    def _account_json(self, address="0x" + "1" * 64):
        return {
            "account_type": "normal",
            "address": address,
            "rng_seed": "0x" + "2" * 64,
            "version": 2,
        }

    def _base_layer_account(self, operator=None, label="Payment SL Poster"):
        if operator is None:
            operator = self._operator_wallet()
        os.environ["EON_KEY_ENCRYPTION_SECRET"] = "test encryption secret"
        response = self.client.post(
            "/base-layer/accounts",
            json={
                "label": label,
                "owner_wallet_address": operator["address"],
                "account_json": self._account_json(),
            },
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

    def test_base_layer_account_registration_encrypts_key_material(self):
        operator = self._operator_wallet()
        account = self._base_layer_account(operator)

        self.assertEqual(account["owner_wallet_address"], operator["address"])
        self.assertEqual(account["eon_address"], "0x" + "1" * 64)
        self.assertNotIn("account_json", account)
        self.assertNotIn("encrypted_account_json", account)

        stored = api.STORE.get_base_layer_account(account["id"], include_secret=True)
        self.assertIsNotNone(stored)
        self.assertIn("encrypted_account_json", stored)
        self.assertNotIn("rng_seed", stored["encrypted_account_json"])

        response = self.client.get("/base-layer/accounts")
        self.assertEqual(response.status_code, 200, response.text)
        listed = response.json()["accounts"][0]
        self.assertEqual(listed["id"], account["id"])
        self.assertNotIn("account_json", listed)
        self.assertNotIn("encrypted_account_json", listed)

    def test_base_layer_account_requires_registered_owner(self):
        os.environ["EON_KEY_ENCRYPTION_SECRET"] = "test encryption secret"

        response = self.client.post(
            "/base-layer/accounts",
            json={
                "label": "Orphan",
                "owner_wallet_address": hash_vk("missing"),
                "account_json": self._account_json(),
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("owner wallet", response.json()["detail"])

    def test_base_layer_account_pool_imports_prefunded_account(self):
        os.environ["EON_KEY_ENCRYPTION_SECRET"] = "test encryption secret"

        response = self.client.post(
            "/base-layer/account-pool",
            json={
                "label": "Prefunded Poster 1",
                "account_json": self._account_json("0x" + "3" * 64),
                "funding_tx_hash": "0xfunding",
                "funded_amount": "1000000",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        account = response.json()
        self.assertEqual(account["label"], "Prefunded Poster 1")
        self.assertEqual(account["eon_address"], "0x" + "3" * 64)
        self.assertEqual(account["status"], "available")
        self.assertEqual(account["funding_tx_hash"], "0xfunding")
        self.assertNotIn("account_json", account)
        self.assertNotIn("encrypted_account_json", account)

        response = self.client.get("/base-layer/account-pool")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["counts"]["available"], 1)
        self.assertEqual(response.json()["counts"]["total"], 1)

        status = self.client.get("/devnet/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertTrue(status.json()["account_generator_configured"])

    def test_base_layer_account_pool_rejects_duplicate_eon_address(self):
        os.environ["EON_KEY_ENCRYPTION_SECRET"] = "test encryption secret"
        payload = {
            "label": "Prefunded Poster",
            "account_json": self._account_json("0x" + "3" * 64),
        }
        response = self.client.post("/base-layer/account-pool", json=payload)
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.post("/base-layer/account-pool", json=payload)

        self.assertEqual(response.status_code, 409)
        self.assertIn("already exists", response.json()["detail"])

    def test_base_layer_account_allocation_assigns_available_pool_account(self):
        operator = self._operator_wallet()
        os.environ["EON_KEY_ENCRYPTION_SECRET"] = "test encryption secret"
        pool_response = self.client.post(
            "/base-layer/account-pool",
            json={
                "label": "Prefunded Poster",
                "account_json": self._account_json("0x" + "3" * 64),
            },
        )
        self.assertEqual(pool_response.status_code, 200, pool_response.text)

        response = self.client.post(
            "/base-layer/accounts/generate",
            json={
                "label": "Assigned Poster",
                "owner_wallet_address": operator["address"],
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        assigned = response.json()
        self.assertEqual(assigned["owner_wallet_address"], operator["address"])
        self.assertEqual(assigned["label"], "Assigned Poster")
        self.assertEqual(assigned["eon_address"], "0x" + "3" * 64)
        self.assertNotIn("account_json", assigned)

        stored = api.STORE.get_base_layer_account(assigned["id"], include_secret=True)
        self.assertIsNotNone(stored)
        self.assertNotIn("rng_seed", stored["encrypted_account_json"])

        pool = self.client.get("/base-layer/account-pool")
        self.assertEqual(pool.status_code, 200, pool.text)
        pool_account = pool.json()["accounts"][0]
        self.assertEqual(pool_account["status"], "assigned")
        self.assertEqual(pool_account["assigned_base_layer_account_id"], assigned["id"])

        status = self.client.get("/devnet/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["base_layer_account_count"], 1)
        self.assertFalse(status.json()["account_generator_configured"])

    def test_base_layer_account_allocation_requires_operator_wallet(self):
        user = self._wallet("Alice", "alice_vk")
        os.environ["EON_KEY_ENCRYPTION_SECRET"] = "test encryption secret"
        response = self.client.post(
            "/base-layer/account-pool",
            json={
                "label": "Prefunded Poster",
                "account_json": self._account_json("0x" + "3" * 64),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.post(
            "/base-layer/accounts/generate",
            json={
                "label": "Assigned Poster",
                "owner_wallet_address": user["address"],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("kind=sl_operator", response.json()["detail"])

    def test_base_layer_account_allocation_requires_available_pool_account(self):
        operator = self._operator_wallet()

        response = self.client.post(
            "/base-layer/accounts/generate",
            json={
                "label": "Assigned Poster",
                "owner_wallet_address": operator["address"],
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("temporarily unavailable", response.json()["detail"])

    def test_semantic_layer_records_can_be_created_and_listed(self):
        operator = self._operator_wallet()
        base_account = self._base_layer_account(operator)

        response = self.client.post(
            "/semantic-layers",
            json={
                "name": "Payment SL",
                "sl_id": SL_ID.hex(),
                "version": "0001",
                "operator_wallet_address": operator["address"],
                "base_layer_account_id": base_account["id"],
                "issuer_vk_ref": f"local:{operator['address']}",
                "operator_vk_ref": f"local:{operator['address']}",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["name"], "Payment SL")
        self.assertEqual(response.json()["operator_wallet_address"], operator["address"])
        self.assertEqual(response.json()["base_layer_account_id"], base_account["id"])

        response = self.client.get("/semantic-layers")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["semantic_layers"]), 1)
        self.assertEqual(
            response.json()["semantic_layers"][0]["operator_wallet_address"],
            operator["address"],
        )
        self.assertEqual(
            response.json()["semantic_layers"][0]["base_layer_account_id"],
            base_account["id"],
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

    def test_semantic_layer_rejects_base_account_owned_by_different_operator(self):
        operator = self._operator_wallet("Operator A", "operator_a_vk")
        other_operator = self._operator_wallet("Operator B", "operator_b_vk")
        base_account = self._base_layer_account(other_operator)

        response = self.client.post(
            "/semantic-layers",
            json={
                "name": "Payment SL",
                "sl_id": SL_ID.hex(),
                "version": "0001",
                "operator_wallet_address": operator["address"],
                "base_layer_account_id": base_account["id"],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("must belong to operator wallet", response.json()["detail"])

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

    def test_devnet_submission_requires_configured_submitter(self):
        self._init()
        alice = self._wallet("Alice", "alice_vk")
        self.client.post(
            "/actions/mint",
            json={"to_address": alice["address"], "amount": 100},
        )
        self.client.post("/operator/batch")

        previous = os.environ.pop("EON_DEVNET_SUBMIT_CMD", None)
        try:
            response = self.client.get("/devnet/status")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["enabled"])

            response = self.client.post("/devnet/submit-latest-batch", json={})
            self.assertEqual(response.status_code, 503)
            self.assertIn("not configured", response.json()["detail"])
        finally:
            if previous is not None:
                os.environ["EON_DEVNET_SUBMIT_CMD"] = previous

    def test_devnet_submission_records_tx_metadata(self):
        self._init()
        operator = self._operator_wallet()
        base_account = self._base_layer_account(operator)
        response = self.client.post(
            "/semantic-layers",
            json={
                "name": "Payment SL",
                "sl_id": SL_ID.hex(),
                "version": "0001",
                "operator_wallet_address": operator["address"],
                "base_layer_account_id": base_account["id"],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        alice = self._wallet("Alice", "alice_vk")
        self.client.post(
            "/actions/mint",
            json={"to_address": alice["address"], "amount": 100},
        )
        batch_response = self.client.post("/operator/batch")
        self.assertEqual(batch_response.status_code, 200, batch_response.text)
        payload_hex = batch_response.json()["batch"]["payload_hex"]

        script = Path(self.tmp.name) / "mock_submitter.py"
        script.write_text(
            "\n".join(
                [
                    "import json, os, sys",
                    "request = json.load(sys.stdin)",
                    "assert request['sequence'] == 1",
                    "assert request['payload_hex']",
                    "assert request['data_scalars']",
                    "wallet_file = os.environ.get('EON_OPERATOR_WALLET_FILE')",
                    "assert wallet_file",
                    "with open(wallet_file, encoding='utf-8') as handle:",
                    "    account = json.load(handle)",
                    "assert account['address'] == '" + self._account_json()["address"] + "'",
                    "assert account['rng_seed'] == '" + self._account_json()["rng_seed"] + "'",
                    "print(json.dumps({",
                    "  'response': 'ok',",
                    "  'tx_hash': '0xtxhash',",
                    "  'utxo_id': '0xutxo',",
                    "  'spent_utxo': '0xspent',",
                    "  'owner': '0xowner',",
                    "  'output_index': 0,",
                    "  'amount': '1'",
                    "}))",
                ]
            )
        )

        previous_cmd = os.environ.get("EON_DEVNET_SUBMIT_CMD")
        previous_url = os.environ.get("EON_DEVNET_API_URL")
        previous_wallet_file = os.environ.pop("EON_OPERATOR_WALLET_FILE", None)
        os.environ["EON_DEVNET_SUBMIT_CMD"] = f"{sys.executable} {script}"
        os.environ["EON_DEVNET_API_URL"] = "https://eon.zk524.com"
        try:
            status = self.client.get("/devnet/status")
            self.assertEqual(status.status_code, 200, status.text)
            self.assertTrue(status.json()["enabled"])
            self.assertEqual(
                status.json()["active_base_layer_account_id"],
                base_account["id"],
            )

            response = self.client.post("/devnet/submit-latest-batch", json={})
        finally:
            if previous_cmd is None:
                os.environ.pop("EON_DEVNET_SUBMIT_CMD", None)
            else:
                os.environ["EON_DEVNET_SUBMIT_CMD"] = previous_cmd
            if previous_url is None:
                os.environ.pop("EON_DEVNET_API_URL", None)
            else:
                os.environ["EON_DEVNET_API_URL"] = previous_url
            if previous_wallet_file is None:
                os.environ.pop("EON_OPERATOR_WALLET_FILE", None)
            else:
                os.environ["EON_OPERATOR_WALLET_FILE"] = previous_wallet_file

        self.assertEqual(response.status_code, 200, response.text)
        submission = response.json()["devnet_submission"]
        self.assertEqual(submission["status"], "submitted")
        self.assertEqual(submission["sequence"], 1)
        self.assertEqual(submission["tx_hash"], "0xtxhash")
        self.assertEqual(submission["payload_hex"], payload_hex)

        response = self.client.get("/operator/batches")
        self.assertEqual(response.status_code, 200, response.text)
        recorded = response.json()["batches"][0]["devnet_submission"]
        self.assertEqual(recorded["tx_hash"], "0xtxhash")

        response = self.client.post("/devnet/submit-latest-batch", json={})
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
