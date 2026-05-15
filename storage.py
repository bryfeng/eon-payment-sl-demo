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

            CREATE TABLE IF NOT EXISTS semantic_layers (
              sl_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              version TEXT NOT NULL,
              operator_wallet_address TEXT NOT NULL,
              issuer_vk_ref TEXT,
              operator_vk_ref TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY(operator_wallet_address) REFERENCES wallets(address)
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

    def upsert_semantic_layer(self, record: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO semantic_layers (
                  sl_id,
                  name,
                  version,
                  operator_wallet_address,
                  issuer_vk_ref,
                  operator_vk_ref,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(sl_id) DO UPDATE SET
                  name = excluded.name,
                  version = excluded.version,
                  operator_wallet_address = excluded.operator_wallet_address,
                  issuer_vk_ref = excluded.issuer_vk_ref,
                  operator_vk_ref = excluded.operator_vk_ref,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    record["sl_id"],
                    record["name"],
                    record["version"],
                    record["operator_wallet_address"],
                    record.get("issuer_vk_ref"),
                    record.get("operator_vk_ref"),
                ),
            )

    def list_semantic_layers(self) -> list:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  sl_id,
                  name,
                  version,
                  operator_wallet_address,
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
