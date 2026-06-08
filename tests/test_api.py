import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import api  # noqa: E402
from core import SL_ID, VERSION, hash_vk  # noqa: E402


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env_backup = {
            "EON_DEVNET_SUBMIT_CMD": os.environ.get("EON_DEVNET_SUBMIT_CMD"),
            "EON_DEVNET_API_URL": os.environ.get("EON_DEVNET_API_URL"),
            "EON_OPERATOR_WALLET_FILE": os.environ.get("EON_OPERATOR_WALLET_FILE"),
            "EON_KEY_ENCRYPTION_SECRET": os.environ.get("EON_KEY_ENCRYPTION_SECRET"),
            "BASE_LAYER_API_URL": os.environ.get("BASE_LAYER_API_URL"),
            "BASE_LAYER_API_KEY": os.environ.get("BASE_LAYER_API_KEY"),
            "BASE_LAYER_TRANSFER_RECIPIENT": os.environ.get("BASE_LAYER_TRANSFER_RECIPIENT"),
            "BASE_LAYER_TRANSFER_FEE": os.environ.get("BASE_LAYER_TRANSFER_FEE"),
            "BASE_LAYER_TRANSFER_AMOUNT": os.environ.get("BASE_LAYER_TRANSFER_AMOUNT"),
        }
        for key in (
            "BASE_LAYER_API_URL",
            "BASE_LAYER_API_KEY",
            "BASE_LAYER_TRANSFER_RECIPIENT",
            "BASE_LAYER_TRANSFER_FEE",
            "BASE_LAYER_TRANSFER_AMOUNT",
        ):
            os.environ.pop(key, None)
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

    def _mock_base_layer_api(
        self,
        expected_key="base-secret",
        wallet_address="0x" + "9" * 64,
    ):
        calls = {"transfers": [], "utxos": []}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def _send_json(self, status, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    self._send_json(
                        200,
                        {
                            "status": "ok",
                            "service": "eon-base-layer-api",
                            "wallet_configured": True,
                        },
                    )
                    return
                if self.path == "/wallet/address":
                    self._send_json(
                        200,
                        {
                            "address": wallet_address,
                            "address_bech32": "eon1mock",
                            "account_type": "normal",
                        },
                    )
                    return
                if self.path.startswith("/utxos"):
                    self._send_json(200, calls["utxos"])
                    return
                self._send_json(404, {"error": "not found"})

            def do_POST(self):
                if self.path != "/transactions/transfer":
                    self._send_json(404, {"error": "not found"})
                    return
                if self.headers.get("x-api-key") != expected_key:
                    self._send_json(401, {"error": "unauthorized"})
                    return
                content_length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(content_length).decode("utf-8"))
                calls["transfers"].append(body)
                calls["utxos"].append(
                    {
                        "id": f"0xutxo{len(calls['utxos']) + 1}",
                        "tx_hash": "0xbasehash",
                        "output_index": len(calls["utxos"]),
                        "amount": body["amount"],
                        "owner": body["recipient"],
                        "data": body["data"],
                    }
                )
                self._send_json(
                    200,
                    {
                        "hash": "0xbasehash",
                        "submitted": True,
                    },
                )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()

        self.addCleanup(cleanup)
        return f"http://127.0.0.1:{server.server_port}", calls

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

        response = self.client.get(
            f"/balances/{alice['address']}?source=operator&sl_id={SL_ID.hex()}&version={VERSION.hex()}"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["balance"], 100)

        response = self.client.post("/verifier/accept-latest-batch")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sequence"], 1)

        response = self.client.get(f"/balances/{alice['address']}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["balance"], 100)
        self.assertEqual(response.json()["source"], "verifier")
        self.assertEqual(response.json()["sl_id"], SL_ID.hex())
        self.assertEqual(response.json()["version"], VERSION.hex())

        response = self.client.get(
            f"/balances/{alice['address']}?source=verifier&sl_id={SL_ID.hex()}&version={VERSION.hex()}"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["balance"], 100)

        response = self.client.get(
            f"/balances/{alice['address']}?source=verifier&sl_id=00010002&version={VERSION.hex()}"
        )
        self.assertEqual(response.status_code, 404)

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

    def test_accept_latest_batch_catches_up_unverified_sequences(self):
        self._init()
        alice = self._wallet("Alice", "alice_vk")
        bob = self._wallet("Bob", "bob_vk")

        response = self.client.post(
            "/actions/mint",
            json={"to_address": alice["address"], "amount": 100},
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post("/operator/batch")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["batch"]["sequence"], 1)

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
        self.assertEqual(response.json()["accepted_sequences"], [1, 2])

        batches = self.client.get("/operator/batches")
        self.assertEqual(batches.status_code, 200, batches.text)
        self.assertEqual(batches.json()["batches"][0]["verification_source"], "local_replay")
        self.assertEqual(batches.json()["batches"][0]["effective_status"], "verified")
        self.assertFalse(batches.json()["batches"][0]["devnet_backed"])

        alice_balance = self.client.get(f"/balances/{alice['address']}").json()
        bob_balance = self.client.get(f"/balances/{bob['address']}").json()
        self.assertEqual(alice_balance["balance"], 60)
        self.assertEqual(bob_balance["balance"], 40)

        response = self.client.post("/verifier/accept-latest-batch")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sequence"], 2)
        self.assertEqual(response.json()["accepted_sequences"], [])

    def test_semantic_layer_runtime_state_is_scoped_by_layer(self):
        alice = self._wallet("Alice", "alice_vk")
        sl_a = SL_ID.hex()
        sl_b = "00010002"
        version = VERSION.hex()

        response = self.client.post(
            "/operator/init",
            json={"issuer_vk": "issuer_a", "sl_id": sl_a, "version": version},
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post(
            "/operator/init",
            json={"issuer_vk": "issuer_b", "sl_id": sl_b, "version": version},
        )
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.post(
            "/actions/mint",
            json={
                "to_address": alice["address"],
                "amount": 100,
                "sl_id": sl_a,
                "version": version,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post(
            "/actions/mint",
            json={
                "to_address": alice["address"],
                "amount": 500,
                "sl_id": sl_b,
                "version": version,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        pending_a = self.client.get(f"/pending?sl_id={sl_a}&version={version}")
        pending_b = self.client.get(f"/pending?sl_id={sl_b}&version={version}")
        self.assertEqual(pending_a.status_code, 200, pending_a.text)
        self.assertEqual(pending_b.status_code, 200, pending_b.text)
        self.assertEqual(len(pending_a.json()["pending"]), 1)
        self.assertEqual(len(pending_b.json()["pending"]), 1)
        all_pending = self.client.get("/pending/all")
        self.assertEqual(all_pending.status_code, 200, all_pending.text)
        self.assertEqual(all_pending.json()["count"], 2)
        self.assertEqual(
            [(item["sl_id"], item["action"]["amount"]) for item in all_pending.json()["pending"]],
            [(sl_a, 100), (sl_b, 500)],
        )

        response = self.client.post(f"/operator/batch?sl_id={sl_a}&version={version}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["batch"]["sl_id"], sl_a)
        response = self.client.post(f"/operator/batch?sl_id={sl_b}&version={version}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["batch"]["sl_id"], sl_b)

        balance_a = self.client.get(
            f"/balances/{alice['address']}?source=operator&sl_id={sl_a}&version={version}"
        )
        balance_b = self.client.get(
            f"/balances/{alice['address']}?source=operator&sl_id={sl_b}&version={version}"
        )
        self.assertEqual(balance_a.status_code, 200, balance_a.text)
        self.assertEqual(balance_b.status_code, 200, balance_b.text)
        self.assertEqual(balance_a.json()["balance"], 100)
        self.assertEqual(balance_b.json()["balance"], 500)

        response = self.client.post(f"/verifier/accept-latest-batch?sl_id={sl_a}&version={version}")
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post(f"/verifier/accept-latest-batch?sl_id={sl_b}&version={version}")
        self.assertEqual(response.status_code, 200, response.text)

        verified_a = self.client.get(
            f"/balances/{alice['address']}?source=verifier&sl_id={sl_a}&version={version}"
        )
        verified_b = self.client.get(
            f"/balances/{alice['address']}?source=verifier&sl_id={sl_b}&version={version}"
        )
        self.assertEqual(verified_a.status_code, 200, verified_a.text)
        self.assertEqual(verified_b.status_code, 200, verified_b.text)
        self.assertEqual(verified_a.json()["balance"], 100)
        self.assertEqual(verified_b.json()["balance"], 500)

        batches_a = self.client.get(f"/operator/batches?sl_id={sl_a}&version={version}")
        batches_b = self.client.get(f"/operator/batches?sl_id={sl_b}&version={version}")
        self.assertEqual(len(batches_a.json()["batches"]), 1)
        self.assertEqual(len(batches_b.json()["batches"]), 1)

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

    def test_batch_sequence_recovers_from_stale_runtime_counter(self):
        self._init()
        alice = self._wallet("Alice", "alice_vk")

        response = self.client.post(
            "/actions/mint",
            json={"to_address": alice["address"], "amount": 100},
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post("/operator/batch")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["batch"]["sequence"], 1)

        with api.STORE.connect() as conn:
            conn.execute(
                """
                UPDATE sl_runtime_configs
                SET next_sequence = 1
                WHERE sl_id = ? AND version = ?
                """,
                (SL_ID.hex(), VERSION.hex()),
            )

        response = self.client.get("/operator/state")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["next_batch_sequence"], 2)

        response = self.client.post(
            "/actions/mint",
            json={"to_address": alice["address"], "amount": 25},
        )
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post("/operator/batch")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["batch"]["sequence"], 2)
        self.assertEqual(response.json()["operator_state"]["total_supply"], 125)

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
        self.assertEqual(account["purpose"], "sl_operator")
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
        self.assertEqual(listed["purpose"], "sl_operator")
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
        self.assertEqual(assigned["purpose"], "sl_operator")
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

    def test_base_layer_account_generation_supports_user_wallets(self):
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
                "label": "Alice Base Account",
                "owner_wallet_address": user["address"],
                "purpose": "user_wallet",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        assigned = response.json()
        self.assertEqual(assigned["owner_wallet_address"], user["address"])
        self.assertEqual(assigned["purpose"], "user_wallet")
        self.assertEqual(assigned["label"], "Alice Base Account")

    def test_base_layer_account_generation_rejects_purpose_kind_mismatch(self):
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
                "label": "Wrong Purpose",
                "owner_wallet_address": user["address"],
                "purpose": "sl_operator",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("purpose must be user_wallet", response.json()["detail"])

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
        self.assertEqual(response.json()["semantic_layers"][0]["assets"], [])

    def test_semantic_layer_assets_can_be_registered_and_minted(self):
        operator = self._operator_wallet()
        sl_id = "00010002"
        version = VERSION.hex()
        response = self.client.post(
            "/semantic-layers",
            json={
                "name": "bStocks",
                "sl_id": sl_id,
                "version": version,
                "operator_wallet_address": operator["address"],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.post(
            "/operator/init",
            json={
                "issuer_vk": "issuer_vk",
                "sl_id": sl_id,
                "version": version,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.post(
            f"/semantic-layers/{sl_id}/assets?version={version}",
            json={
                "asset_id": "bstk",
                "symbol": "BSTK",
                "name": "bStocks",
                "decimals": 0,
                "asset_type": "equity",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["asset"]["asset_id"], "BSTK")
        self.assertEqual(response.json()["queued_registration"]["type"], "register_asset")

        alice = self._wallet("Alice", "alice_vk")
        response = self.client.post(
            "/actions/mint",
            json={
                "to_address": alice["address"],
                "amount": 100,
                "asset_id": "BSTK",
                "sl_id": sl_id,
                "version": version,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        pending = self.client.get(f"/pending?sl_id={sl_id}&version={version}")
        self.assertEqual(pending.status_code, 200, pending.text)
        self.assertEqual([item["type"] for item in pending.json()["pending"]], ["register_asset", "mint"])
        self.assertEqual([item["nonce"] for item in pending.json()["pending"]], [1, 2])

        response = self.client.post(f"/operator/batch?sl_id={sl_id}&version={version}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["batch"]["applied"], 2)
        state = response.json()["operator_state"]
        self.assertEqual(state["total_supply_by_asset"]["BSTK"], 100)

        response = self.client.get(
            f"/balances/{alice['address']}?source=operator&sl_id={sl_id}&version={version}&asset_id=BSTK"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["asset_id"], "BSTK")
        self.assertEqual(response.json()["balance"], 100)

        response = self.client.post(f"/verifier/accept-latest-batch?sl_id={sl_id}&version={version}")
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.get(
            f"/balances/{alice['address']}?source=verifier&sl_id={sl_id}&version={version}&asset_id=BSTK"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["balance"], 100)

        bob = self._wallet("Bob", "bob_vk")
        response = self.client.post(
            "/actions/transfer",
            json={
                "from_address": alice["address"],
                "to_address": bob["address"],
                "amount": 40,
                "asset_id": "BSTK",
                "vk": "alice_vk",
                "sl_id": sl_id,
                "version": version,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.post(f"/operator/batch?sl_id={sl_id}&version={version}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["batch"]["sequence"], 2)

        response = self.client.post(f"/verifier/accept-latest-batch?sl_id={sl_id}&version={version}")
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.get(
            f"/balances/{alice['address']}?source=verifier&sl_id={sl_id}&version={version}&asset_id=BSTK"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["balance"], 60)

        response = self.client.get(
            f"/balances/{bob['address']}?source=verifier&sl_id={sl_id}&version={version}&asset_id=BSTK"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["balance"], 40)

    def test_semantic_layer_record_hydrates_existing_runtime_metadata(self):
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

        response = self.client.get("/config")
        self.assertEqual(response.status_code, 200, response.text)
        runtimes = response.json()["runtimes"]
        self.assertEqual(len(runtimes), 1)
        self.assertEqual(runtimes[0]["operator_wallet_address"], operator["address"])
        self.assertEqual(runtimes[0]["base_layer_account_id"], base_account["id"])
        self.assertEqual(runtimes[0]["next_sequence"], 1)

    def test_semantic_layer_workbench_state_projects_runtime_payload_state(self):
        self._init()
        operator = self._operator_wallet()
        base_account = self._base_layer_account(operator)
        alice = self._wallet("Alice", "alice_vk")

        response = self.client.post(
            "/semantic-layers",
            json={
                "name": "Payment SL",
                "sl_id": SL_ID.hex(),
                "version": VERSION.hex(),
                "operator_wallet_address": operator["address"],
                "base_layer_account_id": base_account["id"],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.post(
            "/actions/mint",
            json={"to_address": alice["address"], "amount": 4200},
        )
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.post("/operator/batch")
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.get(
            f"/semantic-layers/workbench-state?sl_id={SL_ID.hex()}&version={VERSION.hex()}&wallet_address={alice['address']}"
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["health"]["initialized"])
        self.assertEqual(body["selected_layer"]["effective_record"]["name"], "Payment SL")
        self.assertEqual(body["selected_layer"]["base_layer_account"]["id"], base_account["id"])
        self.assertEqual(body["selected_layer"]["assets"][0]["asset_id"], "PAYMENT")
        self.assertTrue(body["selected_layer"]["runtime_initialized"])
        self.assertEqual(body["runtime"]["operator_state"]["state"]["total_supply"], 4200)
        self.assertEqual(body["runtime"]["latest_payload"]["sequence"], 1)
        self.assertEqual(body["runtime"]["balances"][alice["address"]]["operator"]["balance"], 4200)

    def test_semantic_layer_workbench_state_resolves_operator_signer_fallback(self):
        operator = self._operator_wallet()
        base_account = self._base_layer_account(operator)
        sl_id = "00010003"

        response = self.client.post(
            "/semantic-layers",
            json={
                "name": "Payment SL",
                "sl_id": sl_id,
                "version": VERSION.hex(),
                "operator_wallet_address": operator["address"],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.get(
            f"/semantic-layers/workbench-state?sl_id={sl_id}&version={VERSION.hex()}"
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["selected_layer"]["runtime_initialized"])
        self.assertEqual(body["selected_layer"]["signer_status"], "ready")
        self.assertEqual(body["selected_layer"]["base_layer_account"]["id"], base_account["id"])
        self.assertEqual(body["selected_layer"]["assets"][0]["asset_id"], "PAYMENT")
        self.assertEqual(body["runtime"]["pending_actions"], [])
        self.assertIsNone(body["runtime"]["operator_state"])

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

    def test_verifier_sync_records_existing_batch_verification(self):
        self._init()
        alice = self._wallet("Alice", "alice_vk")
        self.client.post(
            "/actions/mint",
            json={"to_address": alice["address"], "amount": 100},
        )
        batch_response = self.client.post("/operator/batch")
        self.assertEqual(batch_response.status_code, 200, batch_response.text)
        batch = batch_response.json()["batch"]

        base_url, calls = self._mock_base_layer_api()
        calls["utxos"].append(
            {
                "id": "0xutxo1",
                "tx_hash": "0xtx1",
                "output_index": 0,
                "amount": batch["data_len"],
                "owner": "0xposter",
                "data": batch["data_scalars"],
            }
        )
        os.environ["BASE_LAYER_API_URL"] = base_url

        response = self.client.post(
            "/verifier/sync",
            json={
                "sl_id": SL_ID.hex(),
                "version": VERSION.hex(),
                "posting_owner": "0xposter",
                "expected_sequence": batch["sequence"],
                "expected_state_hash": batch["new_state_hash"],
                "timeout_seconds": 0,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["verified"])
        self.assertEqual(response.json()["batch"]["status"], "verified")

        response = self.client.get("/operator/batches")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["batches"][0]["status"], "verified")
        self.assertEqual(response.json()["batches"][0]["verification_source"], "devnet_utxo")
        self.assertEqual(response.json()["batches"][0]["verification_label"], "Devnet UTXO")
        self.assertEqual(response.json()["batches"][0]["event_key"], "devnet:utxo:0xutxo1:0")
        self.assertEqual(response.json()["batches"][0]["utxo_id"], "0xutxo1")
        self.assertEqual(response.json()["batches"][0]["verification_tx_hash"], "0xtx1")
        self.assertTrue(response.json()["batches"][0]["devnet_backed"])

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

    def test_devnet_submission_rejects_placeholder_submitter(self):
        self._init()
        alice = self._wallet("Alice", "alice_vk")
        self.client.post(
            "/actions/mint",
            json={"to_address": alice["address"], "amount": 100},
        )
        self.client.post("/operator/batch")

        previous = os.environ.get("EON_DEVNET_SUBMIT_CMD")
        os.environ["EON_DEVNET_SUBMIT_CMD"] = (
            "cargo run --quiet --manifest-path /path/to/eon-sdk/Cargo.toml "
            "--example post_payment_sl_payload"
        )
        try:
            response = self.client.get("/devnet/status")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["enabled"])
            self.assertFalse(response.json()["submitter_configured"])
            self.assertTrue(response.json()["submitter_command_configured"])
            self.assertIn("/path/to", response.json()["submitter_error"])

            response = self.client.post("/devnet/submit-latest-batch", json={})
            self.assertEqual(response.status_code, 503)
            self.assertIn("misconfigured", response.json()["detail"])
        finally:
            if previous is None:
                os.environ.pop("EON_DEVNET_SUBMIT_CMD", None)
            else:
                os.environ["EON_DEVNET_SUBMIT_CMD"] = previous

    def test_devnet_submission_posts_to_base_layer_api(self):
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
        batch = batch_response.json()["batch"]
        self.assertEqual(batch["status"], "batched")

        base_url, calls = self._mock_base_layer_api()
        os.environ.pop("EON_DEVNET_SUBMIT_CMD", None)
        os.environ["BASE_LAYER_API_URL"] = base_url
        os.environ["BASE_LAYER_API_KEY"] = "base-secret"
        os.environ["BASE_LAYER_TRANSFER_FEE"] = "2"

        status = self.client.get("/devnet/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertTrue(status.json()["enabled"])
        self.assertEqual(status.json()["submitter"], "base_layer_api")
        self.assertTrue(status.json()["base_layer_api_reachable"])
        self.assertTrue(status.json()["base_layer_wallet_configured"])

        response = self.client.post("/devnet/submit-latest-batch", json={})
        self.assertEqual(response.status_code, 200, response.text)

        self.assertEqual(len(calls["transfers"]), 1)
        transfer = calls["transfers"][0]
        self.assertEqual(transfer["recipient"], base_account["eon_address"])
        self.assertEqual(transfer["amount"], batch["data_len"])
        self.assertEqual(transfer["fee"], 2)
        self.assertEqual(transfer["data"], batch["data_scalars"])

        submission = response.json()["devnet_submission"]
        self.assertEqual(submission["status"], "submitted")
        self.assertEqual(submission["submitter"], "base_layer_api")
        self.assertEqual(submission["tx_hash"], "0xbasehash")
        self.assertEqual(submission["owner"], base_account["eon_address"])
        self.assertEqual(submission["payload_hex"], batch["payload_hex"])
        self.assertEqual(response.json()["batch"]["status"], "verified")

        verification = response.json()["verification"]
        self.assertTrue(verification["verified"])
        self.assertEqual(verification["status"], "verified")
        self.assertEqual(verification["checkpoint"]["sequence"], batch["sequence"])
        self.assertEqual(verification["checkpoint"]["state_hash"], batch["new_state_hash"])

        response = self.client.get(f"/verifier/state?sl_id={SL_ID.hex()}&version=0001")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"]["state_hash"], batch["new_state_hash"])

    def test_devnet_submission_verifies_manifest_asset_genesis(self):
        operator = self._operator_wallet()
        base_account = self._base_layer_account(operator)
        sl_id = "00010002"
        version = VERSION.hex()
        asset = {
            "asset_id": "SPX",
            "symbol": "SPX",
            "name": "SP500",
            "decimals": 5,
            "asset_type": "fungible",
        }
        response = self.client.post(
            "/semantic-layers",
            json={
                "name": "RWA-issuer",
                "sl_id": sl_id,
                "version": version,
                "operator_wallet_address": operator["address"],
                "base_layer_account_id": base_account["id"],
                "assets": [asset],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.post(
            "/operator/init",
            json={
                "issuer_vk": "issuer_vk",
                "sl_id": sl_id,
                "version": version,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        alice = self._wallet("Alice", "alice_vk")
        response = self.client.post(
            "/actions/mint",
            json={
                "to_address": alice["address"],
                "amount": 100,
                "asset_id": "SPX",
                "sl_id": sl_id,
                "version": version,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

        response = self.client.post(f"/operator/batch?sl_id={sl_id}&version={version}")
        self.assertEqual(response.status_code, 200, response.text)
        batch = response.json()["batch"]
        self.assertEqual(batch["applied"], 1)

        base_url, _calls = self._mock_base_layer_api()
        os.environ.pop("EON_DEVNET_SUBMIT_CMD", None)
        os.environ["BASE_LAYER_API_URL"] = base_url
        os.environ["BASE_LAYER_API_KEY"] = "base-secret"

        response = self.client.post(
            "/devnet/submit-latest-batch",
            json={"sl_id": sl_id, "version": version},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["batch"]["status"], "verified")
        self.assertTrue(response.json()["verification"]["verified"])

        response = self.client.get(f"/verifier/state?sl_id={sl_id}&version={version}")
        self.assertEqual(response.status_code, 200, response.text)
        state = response.json()["state"]
        self.assertEqual(state["state_hash"], batch["new_state_hash"])
        self.assertEqual(state["total_supply_by_asset"]["SPX"], 100)

    def test_devnet_submission_can_target_historical_batch(self):
        self._init()
        alice = self._wallet("Alice", "alice_vk")
        bob = self._wallet("Bob", "bob_vk")
        self.client.post(
            "/actions/mint",
            json={"to_address": alice["address"], "amount": 100},
        )
        first_response = self.client.post("/operator/batch")
        self.assertEqual(first_response.status_code, 200, first_response.text)
        first_batch = first_response.json()["batch"]
        self.client.post(
            "/actions/mint",
            json={"to_address": bob["address"], "amount": 50},
        )
        second_response = self.client.post("/operator/batch")
        self.assertEqual(second_response.status_code, 200, second_response.text)
        second_batch = second_response.json()["batch"]

        base_url, calls = self._mock_base_layer_api()
        os.environ.pop("EON_DEVNET_SUBMIT_CMD", None)
        os.environ["BASE_LAYER_API_URL"] = base_url
        os.environ["BASE_LAYER_API_KEY"] = "base-secret"

        response = self.client.post(
            "/devnet/submit-latest-batch",
            json={
                "sequence": first_batch["sequence"],
                "wait_for_verifier": False,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sequence"], first_batch["sequence"])
        self.assertEqual(response.json()["batch"]["sequence"], first_batch["sequence"])
        self.assertEqual(calls["transfers"][0]["data"], first_batch["data_scalars"])

        response = self.client.post(
            "/devnet/submit-latest-batch",
            json={
                "sequence": second_batch["sequence"],
                "wait_for_verifier": False,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sequence"], second_batch["sequence"])
        self.assertEqual(response.json()["batch"]["sequence"], second_batch["sequence"])
        self.assertEqual(calls["transfers"][1]["data"], second_batch["data_scalars"])

    def test_devnet_submission_can_use_base_layer_api_wallet_as_recipient(self):
        self._init()
        alice = self._wallet("Alice", "alice_vk")
        self.client.post(
            "/actions/mint",
            json={"to_address": alice["address"], "amount": 100},
        )
        self.client.post("/operator/batch")

        wallet_address = "0x" + "9" * 64
        base_url, calls = self._mock_base_layer_api(wallet_address=wallet_address)
        os.environ.pop("EON_DEVNET_SUBMIT_CMD", None)
        os.environ["BASE_LAYER_API_URL"] = base_url
        os.environ["BASE_LAYER_API_KEY"] = "base-secret"

        status = self.client.get("/devnet/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertTrue(status.json()["enabled"])
        self.assertEqual(status.json()["base_layer_wallet_address"], wallet_address)

        response = self.client.post("/devnet/submit-latest-batch", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(calls["transfers"][0]["recipient"], wallet_address)
        self.assertEqual(response.json()["verification"]["status"], "verified")

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
        self.assertEqual(response.json()["batches"][0]["status"], "submitted")

        response = self.client.post("/devnet/submit-latest-batch", json={})
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
