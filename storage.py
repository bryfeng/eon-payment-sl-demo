"""
SQLite persistence for the hosted Payment SL playground API.

The command-line demo tools still use the JSON helpers in core.py. The hosted
API uses this module so Railway can keep one shared demo world on a persistent
volume while preserving the same public endpoint shape.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import core


def default_db_path() -> Path:
    explicit = os.environ.get("PAYMENT_SL_DB_PATH")
    if explicit:
        return Path(explicit)

    railway_volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if railway_volume:
        return Path(railway_volume) / "payment_sl.sqlite"

    return Path(__file__).parent / "data" / "payment_sl.sqlite"


DEFAULT_DB_PATH = default_db_path()


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _loads(value: str) -> Any:
    return json.loads(value)


class SQLiteStorage:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def configure(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            self._ensure_schema(conn)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        with self.connect():
            pass

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv (
              key TEXT PRIMARY KEY,
              value_json TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS states (
              scope TEXT PRIMARY KEY,
              state_json TEXT NOT NULL,
              state_hash TEXT NOT NULL,
              nonce INTEGER NOT NULL,
              total_supply INTEGER NOT NULL,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              CHECK (scope IN ('operator', 'verifier'))
            );

            CREATE TABLE IF NOT EXISTS wallets (
              address TEXT PRIMARY KEY,
              label TEXT NOT NULL,
              kind TEXT NOT NULL DEFAULT 'user',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS base_layer_accounts (
              id TEXT PRIMARY KEY,
              owner_wallet_address TEXT NOT NULL,
              label TEXT NOT NULL,
              eon_address TEXT NOT NULL,
              encrypted_account_json TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(owner_wallet_address) REFERENCES wallets(address)
            );

            CREATE TABLE IF NOT EXISTS base_layer_account_pool (
              id TEXT PRIMARY KEY,
              label TEXT NOT NULL,
              eon_address TEXT NOT NULL UNIQUE,
              encrypted_account_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'available',
              assigned_base_layer_account_id TEXT,
              funding_tx_hash TEXT,
              funded_amount TEXT,
              balance_last_checked TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              assigned_at TEXT,
              CHECK (status IN ('available', 'reserved', 'assigned', 'disabled', 'drained'))
            );

            CREATE TABLE IF NOT EXISTS semantic_layers (
              sl_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              version TEXT NOT NULL,
              operator_wallet_address TEXT NOT NULL,
              base_layer_account_id TEXT,
              issuer_vk_ref TEXT,
              operator_vk_ref TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(operator_wallet_address) REFERENCES wallets(address),
              FOREIGN KEY(base_layer_account_id) REFERENCES base_layer_accounts(id)
            );

            CREATE TABLE IF NOT EXISTS pending_actions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              action_json TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS operator_batches (
              sequence INTEGER PRIMARY KEY,
              record_json TEXT NOT NULL,
              payload_hex TEXT NOT NULL,
              prev_state_hash TEXT NOT NULL,
              new_state_hash TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS verifier_log (
              sequence INTEGER PRIMARY KEY,
              entry_json TEXT NOT NULL,
              payload_hex TEXT NOT NULL,
              prev_state_hash TEXT NOT NULL,
              new_state_hash TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        wallet_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(wallets)").fetchall()
        }
        if "kind" not in wallet_columns:
            conn.execute(
                "ALTER TABLE wallets ADD COLUMN kind TEXT NOT NULL DEFAULT 'user'"
            )
        semantic_layer_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(semantic_layers)").fetchall()
        }
        if "base_layer_account_id" not in semantic_layer_columns:
            conn.execute("ALTER TABLE semantic_layers ADD COLUMN base_layer_account_id TEXT")

    def _record_devnet_submission(
        self,
        conn: sqlite3.Connection,
        sequence: int,
        submission: dict,
    ) -> dict:
        row = conn.execute(
            "SELECT record_json FROM operator_batches WHERE sequence = ?",
            (sequence,),
        ).fetchone()
        if row is None:
            raise KeyError(f"batch {sequence} not found")

        record = _loads(row["record_json"])
        record["devnet_submission"] = submission
        conn.execute(
            """
            UPDATE operator_batches
            SET record_json = ?
            WHERE sequence = ?
            """,
            (_dumps(record), sequence),
        )
        return record

    def _put_json(self, conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute(
            """
            INSERT INTO kv (key, value_json, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
              value_json = excluded.value_json,
              updated_at = CURRENT_TIMESTAMP
            """,
            (key, _dumps(value)),
        )

    def _get_json(
        self,
        conn: sqlite3.Connection,
        key: str,
        default: Optional[Any] = None,
    ) -> Any:
        row = conn.execute(
            "SELECT value_json FROM kv WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return default
        return _loads(row["value_json"])

    def _put_state(
        self,
        conn: sqlite3.Connection,
        scope: str,
        state: core.State,
    ) -> None:
        conn.execute(
            """
            INSERT INTO states (
              scope, state_json, state_hash, nonce, total_supply, updated_at
            )
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(scope) DO UPDATE SET
              state_json = excluded.state_json,
              state_hash = excluded.state_hash,
              nonce = excluded.nonce,
              total_supply = excluded.total_supply,
              updated_at = CURRENT_TIMESTAMP
            """,
            (
                scope,
                _dumps(state.to_dict()),
                state.state_hash(),
                state.nonce,
                state.total_supply,
            ),
        )

    def _get_state(
        self,
        conn: sqlite3.Connection,
        scope: str,
    ) -> Optional[core.State]:
        row = conn.execute(
            "SELECT state_json FROM states WHERE scope = ?",
            (scope,),
        ).fetchone()
        if row is None:
            return None
        return core.State.from_dict(_loads(row["state_json"]))

    def reset(self) -> None:
        with self.connect() as conn:
            for table in (
                "semantic_layers",
                "base_layer_accounts",
                "base_layer_account_pool",
                "verifier_log",
                "operator_batches",
                "pending_actions",
                "wallets",
                "states",
                "kv",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'pending_actions'")

    def initialize(self, issuer_vk: str) -> core.State:
        genesis = core.State(issuer_vk=issuer_vk)
        config = {
            "issuer_vk": issuer_vk,
            "sl_id": core.SL_ID.hex(),
            "version": core.VERSION.hex(),
        }

        with self.connect() as conn:
            for table in (
                "verifier_log",
                "operator_batches",
                "pending_actions",
                "states",
                "kv",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'pending_actions'")
            self._put_json(conn, "sl_config", config)
            self._put_json(conn, "operator_meta", {"next_sequence": 1})
            self._put_state(conn, "operator", genesis)

        return genesis

    def is_initialized(self) -> bool:
        with self.connect() as conn:
            has_config = self._get_json(conn, "sl_config") is not None
            has_state = self._get_state(conn, "operator") is not None
        return bool(has_config and has_state)

    def load_sl_config(self) -> Optional[dict]:
        with self.connect() as conn:
            return self._get_json(conn, "sl_config")

    def load_operator_state(self) -> Optional[core.State]:
        with self.connect() as conn:
            return self._get_state(conn, "operator")

    def load_verified_state(self) -> Optional[core.State]:
        with self.connect() as conn:
            return self._get_state(conn, "verifier")

    def load_pending(self) -> list:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT action_json FROM pending_actions ORDER BY id"
            ).fetchall()
        return [_loads(row["action_json"]) for row in rows]

    def pending_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM pending_actions").fetchone()
        return int(row["count"])

    def append_pending(self, action_dict: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO pending_actions (action_json) VALUES (?)",
                (_dumps(action_dict),),
            )

    def next_batch_sequence(self) -> int:
        with self.connect() as conn:
            meta = self._get_json(conn, "operator_meta", {"next_sequence": 1})
        return int(meta.get("next_sequence", 1))

    def next_nonce(self) -> int:
        state = self.load_operator_state()
        if state is None:
            raise RuntimeError("operator state is not initialized")
        return state.nonce + self.pending_count() + 1

    def upsert_wallet(self, label: str, address: str, kind: str = "user") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO wallets (address, label, kind, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(address) DO UPDATE SET
                  label = excluded.label,
                  kind = excluded.kind,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (address, label, kind),
            )

    def list_wallets(self) -> list:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT address, label, kind FROM wallets ORDER BY label, address"
            ).fetchall()
        return [
            {
                "label": row["label"],
                "address": row["address"],
                "kind": row["kind"],
            }
            for row in rows
        ]

    def get_wallet(self, address: str) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT address, label, kind FROM wallets WHERE address = ?",
                (address,),
            ).fetchone()
        if row is None:
            return None
        return {
            "label": row["label"],
            "address": row["address"],
            "kind": row["kind"],
        }

    def create_base_layer_account(self, record: dict) -> dict:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO base_layer_accounts (
                  id,
                  owner_wallet_address,
                  label,
                  eon_address,
                  encrypted_account_json,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    record["id"],
                    record["owner_wallet_address"],
                    record["label"],
                    record["eon_address"],
                    record["encrypted_account_json"],
                ),
            )
        return self.get_base_layer_account(record["id"], include_secret=False)

    def import_base_layer_pool_account(self, record: dict) -> dict:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO base_layer_account_pool (
                  id,
                  label,
                  eon_address,
                  encrypted_account_json,
                  status,
                  funding_tx_hash,
                  funded_amount,
                  balance_last_checked,
                  updated_at
                )
                VALUES (?, ?, ?, ?, 'available', ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    record["id"],
                    record["label"],
                    record["eon_address"],
                    record["encrypted_account_json"],
                    record.get("funding_tx_hash"),
                    record.get("funded_amount"),
                    record.get("balance_last_checked"),
                ),
            )
        return self.get_base_layer_pool_account(record["id"])

    def list_base_layer_pool_accounts(self) -> list:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  id,
                  label,
                  eon_address,
                  status,
                  assigned_base_layer_account_id,
                  funding_tx_hash,
                  funded_amount,
                  balance_last_checked,
                  created_at,
                  updated_at,
                  assigned_at
                FROM base_layer_account_pool
                ORDER BY
                  CASE status
                    WHEN 'available' THEN 0
                    WHEN 'reserved' THEN 1
                    WHEN 'assigned' THEN 2
                    WHEN 'disabled' THEN 3
                    ELSE 4
                  END,
                  created_at,
                  label,
                  id
                """
            ).fetchall()
        return [self._pool_row_to_dict(row) for row in rows]

    def get_base_layer_pool_account(self, account_id: str) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                  id,
                  label,
                  eon_address,
                  status,
                  assigned_base_layer_account_id,
                  funding_tx_hash,
                  funded_amount,
                  balance_last_checked,
                  created_at,
                  updated_at,
                  assigned_at
                FROM base_layer_account_pool
                WHERE id = ?
                """,
                (account_id,),
            ).fetchone()
        if row is None:
            return None
        return self._pool_row_to_dict(row)

    def base_layer_account_pool_counts(self) -> dict:
        counts = {
            "available": 0,
            "reserved": 0,
            "assigned": 0,
            "disabled": 0,
            "drained": 0,
            "total": 0,
        }
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM base_layer_account_pool
                GROUP BY status
                """
            ).fetchall()
        for row in rows:
            status = row["status"]
            count = int(row["count"])
            counts[status] = count
            counts["total"] += count
        return counts

    def allocate_base_layer_account(
        self,
        owner_wallet_address: str,
        label: Optional[str] = None,
    ) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                  id,
                  label,
                  eon_address,
                  encrypted_account_json
                FROM base_layer_account_pool
                WHERE status = 'available'
                ORDER BY created_at, label, id
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None

            account_id = f"acct_{uuid4().hex[:12]}"
            account_label = (label or row["label"]).strip()
            conn.execute(
                """
                INSERT INTO base_layer_accounts (
                  id,
                  owner_wallet_address,
                  label,
                  eon_address,
                  encrypted_account_json,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    account_id,
                    owner_wallet_address,
                    account_label,
                    row["eon_address"],
                    row["encrypted_account_json"],
                ),
            )
            conn.execute(
                """
                UPDATE base_layer_account_pool
                SET
                  status = 'assigned',
                  assigned_base_layer_account_id = ?,
                  assigned_at = CURRENT_TIMESTAMP,
                  updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'available'
                """,
                (account_id, row["id"]),
            )

        return self.get_base_layer_account(account_id, include_secret=False)

    def _pool_row_to_dict(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "label": row["label"],
            "eon_address": row["eon_address"],
            "status": row["status"],
            "assigned_base_layer_account_id": row["assigned_base_layer_account_id"],
            "funding_tx_hash": row["funding_tx_hash"],
            "funded_amount": row["funded_amount"],
            "balance_last_checked": row["balance_last_checked"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "assigned_at": row["assigned_at"],
        }

    def list_base_layer_accounts(self) -> list:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  id,
                  owner_wallet_address,
                  label,
                  eon_address,
                  created_at,
                  updated_at
                FROM base_layer_accounts
                ORDER BY updated_at DESC, label, id
                """
            ).fetchall()
        return [
            {
                "id": row["id"],
                "owner_wallet_address": row["owner_wallet_address"],
                "label": row["label"],
                "eon_address": row["eon_address"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def base_layer_account_count(self) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM base_layer_accounts"
            ).fetchone()
        return int(row["count"])

    def get_base_layer_account(
        self,
        account_id: str,
        include_secret: bool = False,
    ) -> Optional[dict]:
        columns = """
          id,
          owner_wallet_address,
          label,
          eon_address,
          encrypted_account_json,
          created_at,
          updated_at
        """
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT {columns} FROM base_layer_accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
        if row is None:
            return None

        record = {
            "id": row["id"],
            "owner_wallet_address": row["owner_wallet_address"],
            "label": row["label"],
            "eon_address": row["eon_address"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_secret:
            record["encrypted_account_json"] = row["encrypted_account_json"]
        return record

    def upsert_semantic_layer(self, record: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO semantic_layers (
                  sl_id,
                  name,
                  version,
                  operator_wallet_address,
                  base_layer_account_id,
                  issuer_vk_ref,
                  operator_vk_ref,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(sl_id) DO UPDATE SET
                  name = excluded.name,
                  version = excluded.version,
                  operator_wallet_address = excluded.operator_wallet_address,
                  base_layer_account_id = excluded.base_layer_account_id,
                  issuer_vk_ref = excluded.issuer_vk_ref,
                  operator_vk_ref = excluded.operator_vk_ref,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    record["sl_id"],
                    record["name"],
                    record["version"],
                    record["operator_wallet_address"],
                    record.get("base_layer_account_id"),
                    record.get("issuer_vk_ref"),
                    record.get("operator_vk_ref"),
                ),
            )

    def get_semantic_layer(self, sl_id: str) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                  sl_id,
                  name,
                  version,
                  operator_wallet_address,
                  base_layer_account_id,
                  issuer_vk_ref,
                  operator_vk_ref,
                  created_at,
                  updated_at
                FROM semantic_layers
                WHERE sl_id = ?
                """,
                (sl_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "sl_id": row["sl_id"],
            "name": row["name"],
            "version": row["version"],
            "operator_wallet_address": row["operator_wallet_address"],
            "base_layer_account_id": row["base_layer_account_id"],
            "issuer_vk_ref": row["issuer_vk_ref"],
            "operator_vk_ref": row["operator_vk_ref"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_semantic_layers(self) -> list:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  sl_id,
                  name,
                  version,
                  operator_wallet_address,
                  base_layer_account_id,
                  issuer_vk_ref,
                  operator_vk_ref,
                  created_at,
                  updated_at
                FROM semantic_layers
                ORDER BY updated_at DESC, name, sl_id
                """
            ).fetchall()
        return [
            {
                "sl_id": row["sl_id"],
                "name": row["name"],
                "version": row["version"],
                "operator_wallet_address": row["operator_wallet_address"],
                "base_layer_account_id": row["base_layer_account_id"],
                "issuer_vk_ref": row["issuer_vk_ref"],
                "operator_vk_ref": row["operator_vk_ref"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def list_batches(self) -> list:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT record_json FROM operator_batches ORDER BY sequence"
            ).fetchall()
        return [_loads(row["record_json"]) for row in rows]

    def latest_batch(self) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT record_json
                FROM operator_batches
                ORDER BY sequence DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return _loads(row["record_json"])

    def commit_operator_batch(
        self,
        new_state: core.State,
        record: dict,
        sequence: int,
    ) -> None:
        with self.connect() as conn:
            self._put_state(conn, "operator", new_state)
            conn.execute("DELETE FROM pending_actions")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'pending_actions'")
            self._put_json(conn, "operator_meta", {"next_sequence": sequence + 1})
            conn.execute(
                """
                INSERT INTO operator_batches (
                  sequence,
                  record_json,
                  payload_hex,
                  prev_state_hash,
                  new_state_hash
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    _dumps(record),
                    record["payload_hex"],
                    record["prev_state_hash"],
                    record["new_state_hash"],
                ),
            )

    def record_devnet_submission(self, sequence: int, submission: dict) -> dict:
        with self.connect() as conn:
            return self._record_devnet_submission(conn, sequence, submission)

    def load_verified_log(self) -> list:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT entry_json FROM verifier_log ORDER BY sequence"
            ).fetchall()
        return [_loads(row["entry_json"]) for row in rows]

    def commit_verified_envelope(
        self,
        envelope: dict,
        state: core.State,
    ) -> None:
        entry = {
            "sequence": int(envelope["sequence"]),
            "prev_state_hash": envelope["prev_state_hash"],
            "new_state_hash": envelope["new_state_hash"],
            "actions_applied": len(envelope["actions_applied"]),
            "payload_hex": envelope["payload_hex"],
        }
        with self.connect() as conn:
            self._put_state(conn, "verifier", state)
            conn.execute(
                """
                INSERT INTO verifier_log (
                  sequence,
                  entry_json,
                  payload_hex,
                  prev_state_hash,
                  new_state_hash
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    entry["sequence"],
                    _dumps(entry),
                    entry["payload_hex"],
                    entry["prev_state_hash"],
                    entry["new_state_hash"],
                ),
            )
