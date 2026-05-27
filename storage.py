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
              purpose TEXT NOT NULL DEFAULT 'sl_operator',
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
              assets_json TEXT NOT NULL DEFAULT '[]',
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

            CREATE TABLE IF NOT EXISTS sl_runtime_configs (
              sl_id TEXT NOT NULL,
              version TEXT NOT NULL,
              issuer_vk TEXT NOT NULL,
              operator_wallet_address TEXT,
              base_layer_account_id TEXT,
              next_sequence INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY(sl_id, version)
            );

            CREATE TABLE IF NOT EXISTS sl_states (
              sl_id TEXT NOT NULL,
              version TEXT NOT NULL,
              scope TEXT NOT NULL,
              state_json TEXT NOT NULL,
              state_hash TEXT NOT NULL,
              nonce INTEGER NOT NULL,
              total_supply INTEGER NOT NULL,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              CHECK (scope IN ('operator', 'verifier')),
              PRIMARY KEY(sl_id, version, scope)
            );

            CREATE TABLE IF NOT EXISTS sl_pending_actions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sl_id TEXT NOT NULL,
              version TEXT NOT NULL,
              action_json TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sl_operator_batches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sl_id TEXT NOT NULL,
              version TEXT NOT NULL,
              sequence INTEGER NOT NULL,
              record_json TEXT NOT NULL,
              payload_hex TEXT NOT NULL,
              prev_state_hash TEXT NOT NULL,
              new_state_hash TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(sl_id, version, sequence)
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
        if "assets_json" not in semantic_layer_columns:
            conn.execute(
                "ALTER TABLE semantic_layers ADD COLUMN assets_json TEXT NOT NULL DEFAULT '[]'"
            )
        base_layer_account_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(base_layer_accounts)").fetchall()
        }
        if "purpose" not in base_layer_account_columns:
            conn.execute(
                "ALTER TABLE base_layer_accounts "
                "ADD COLUMN purpose TEXT NOT NULL DEFAULT 'sl_operator'"
            )
        self._migrate_legacy_runtime(conn)
        self._hydrate_runtime_metadata_from_registry(conn)

    def _record_devnet_submission(
        self,
        conn: sqlite3.Connection,
        sequence: int,
        submission: dict,
        sl_id: str = core.SL_ID.hex(),
        version: str = core.VERSION.hex(),
    ) -> dict:
        row = conn.execute(
            """
            SELECT record_json FROM sl_operator_batches
            WHERE sl_id = ? AND version = ? AND sequence = ?
            """,
            (sl_id, version, sequence),
        ).fetchone()
        if row is None:
            raise KeyError(f"batch {sequence} not found")

        record = _loads(row["record_json"])
        record["devnet_submission"] = submission
        conn.execute(
            """
            UPDATE sl_operator_batches
            SET record_json = ?
            WHERE sl_id = ? AND version = ? AND sequence = ?
            """,
            (_dumps(record), sl_id, version, sequence),
        )
        if sl_id == core.SL_ID.hex() and version == core.VERSION.hex():
            legacy_row = conn.execute(
                "SELECT record_json FROM operator_batches WHERE sequence = ?",
                (sequence,),
            ).fetchone()
            if legacy_row is not None:
                legacy_record = _loads(legacy_row["record_json"])
                legacy_record["devnet_submission"] = submission
                conn.execute(
                    """
                    UPDATE operator_batches
                    SET record_json = ?
                    WHERE sequence = ?
                    """,
                    (_dumps(legacy_record), sequence),
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

    def _put_scoped_state(
        self,
        conn: sqlite3.Connection,
        sl_id: str,
        version: str,
        scope: str,
        state: core.State,
    ) -> None:
        conn.execute(
            """
            INSERT INTO sl_states (
              sl_id, version, scope, state_json, state_hash, nonce, total_supply, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(sl_id, version, scope) DO UPDATE SET
              state_json = excluded.state_json,
              state_hash = excluded.state_hash,
              nonce = excluded.nonce,
              total_supply = excluded.total_supply,
              updated_at = CURRENT_TIMESTAMP
            """,
            (
                sl_id,
                version,
                scope,
                _dumps(state.to_dict()),
                state.state_hash(),
                state.nonce,
                state.total_supply,
            ),
        )

    def _get_scoped_state(
        self,
        conn: sqlite3.Connection,
        sl_id: str,
        version: str,
        scope: str,
    ) -> Optional[core.State]:
        row = conn.execute(
            """
            SELECT state_json FROM sl_states
            WHERE sl_id = ? AND version = ? AND scope = ?
            """,
            (sl_id, version, scope),
        ).fetchone()
        if row is None:
            return None
        return core.State.from_dict(_loads(row["state_json"]))

    def _put_runtime_config(
        self,
        conn: sqlite3.Connection,
        sl_id: str,
        version: str,
        issuer_vk: str,
        operator_wallet_address: Optional[str] = None,
        base_layer_account_id: Optional[str] = None,
        next_sequence: int = 1,
    ) -> None:
        conn.execute(
            """
            INSERT INTO sl_runtime_configs (
              sl_id,
              version,
              issuer_vk,
              operator_wallet_address,
              base_layer_account_id,
              next_sequence,
              updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(sl_id, version) DO UPDATE SET
              issuer_vk = excluded.issuer_vk,
              operator_wallet_address = COALESCE(excluded.operator_wallet_address, sl_runtime_configs.operator_wallet_address),
              base_layer_account_id = COALESCE(excluded.base_layer_account_id, sl_runtime_configs.base_layer_account_id),
              next_sequence = excluded.next_sequence,
              updated_at = CURRENT_TIMESTAMP
            """,
            (
                sl_id,
                version,
                issuer_vk,
                operator_wallet_address,
                base_layer_account_id,
                next_sequence,
            ),
        )

    def _get_runtime_config(
        self,
        conn: sqlite3.Connection,
        sl_id: str,
        version: str,
    ) -> Optional[dict]:
        row = conn.execute(
            """
            SELECT
              sl_id,
              version,
              issuer_vk,
              operator_wallet_address,
              base_layer_account_id,
              next_sequence,
              created_at,
              updated_at
            FROM sl_runtime_configs
            WHERE sl_id = ? AND version = ?
            """,
            (sl_id, version),
        ).fetchone()
        if row is None:
            return None
        return {
            "sl_id": row["sl_id"],
            "version": row["version"],
            "issuer_vk": row["issuer_vk"],
            "operator_wallet_address": row["operator_wallet_address"],
            "base_layer_account_id": row["base_layer_account_id"],
            "next_sequence": int(row["next_sequence"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _hydrate_runtime_metadata_from_registry(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT
              c.sl_id,
              c.version,
              c.issuer_vk,
              c.next_sequence,
              s.operator_wallet_address,
              s.base_layer_account_id
            FROM sl_runtime_configs c
            JOIN semantic_layers s
              ON s.sl_id = c.sl_id AND s.version = c.version
            WHERE c.operator_wallet_address IS NULL
               OR c.base_layer_account_id IS NULL
            """
        ).fetchall()

        for row in rows:
            self._put_runtime_config(
                conn,
                row["sl_id"],
                row["version"],
                row["issuer_vk"],
                operator_wallet_address=row["operator_wallet_address"],
                base_layer_account_id=row["base_layer_account_id"],
                next_sequence=int(row["next_sequence"]),
            )

    def _migrate_legacy_runtime(self, conn: sqlite3.Connection) -> None:
        sl_id = core.SL_ID.hex()
        version = core.VERSION.hex()
        existing = self._get_runtime_config(conn, sl_id, version)
        if existing is not None:
            return

        legacy_config = self._get_json(conn, "sl_config")
        legacy_operator = self._get_state(conn, "operator")
        if legacy_config is None or legacy_operator is None:
            return

        meta = self._get_json(conn, "operator_meta", {"next_sequence": 1})
        legacy_sl_id = str(legacy_config.get("sl_id", sl_id))
        legacy_version = str(legacy_config.get("version", version))
        self._put_runtime_config(
            conn,
            legacy_sl_id,
            legacy_version,
            str(legacy_config["issuer_vk"]),
            next_sequence=int(meta.get("next_sequence", 1)),
        )
        self._put_scoped_state(conn, legacy_sl_id, legacy_version, "operator", legacy_operator)

        legacy_verifier = self._get_state(conn, "verifier")
        if legacy_verifier is not None:
            self._put_scoped_state(conn, legacy_sl_id, legacy_version, "verifier", legacy_verifier)

        pending_rows = conn.execute(
            "SELECT action_json, created_at FROM pending_actions ORDER BY id"
        ).fetchall()
        for row in pending_rows:
            conn.execute(
                """
                INSERT INTO sl_pending_actions (sl_id, version, action_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (legacy_sl_id, legacy_version, row["action_json"], row["created_at"]),
            )

        batch_rows = conn.execute(
            """
            SELECT sequence, record_json, payload_hex, prev_state_hash, new_state_hash, created_at
            FROM operator_batches ORDER BY sequence
            """
        ).fetchall()
        for row in batch_rows:
            conn.execute(
                """
                INSERT OR IGNORE INTO sl_operator_batches (
                  sl_id,
                  version,
                  sequence,
                  record_json,
                  payload_hex,
                  prev_state_hash,
                  new_state_hash,
                  created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    legacy_sl_id,
                    legacy_version,
                    int(row["sequence"]),
                    row["record_json"],
                    row["payload_hex"],
                    row["prev_state_hash"],
                    row["new_state_hash"],
                    row["created_at"],
                ),
            )

    def reset(self) -> None:
        with self.connect() as conn:
            for table in (
                "sl_operator_batches",
                "sl_pending_actions",
                "sl_states",
                "sl_runtime_configs",
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
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'sl_pending_actions'")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'sl_operator_batches'")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'pending_actions'")

    def initialize(
        self,
        issuer_vk: str,
        sl_id: str = core.SL_ID.hex(),
        version: str = core.VERSION.hex(),
        operator_wallet_address: Optional[str] = None,
        base_layer_account_id: Optional[str] = None,
        assets: Optional[list[dict]] = None,
        reset_existing: bool = False,
    ) -> core.State:
        genesis = core.State(issuer_vk=issuer_vk)
        for asset in assets or []:
            genesis.register_asset(asset)
        config = {
            "issuer_vk": issuer_vk,
            "sl_id": sl_id,
            "version": version,
            "operator_wallet_address": operator_wallet_address,
            "base_layer_account_id": base_layer_account_id,
        }

        with self.connect() as conn:
            if reset_existing:
                conn.execute(
                    "DELETE FROM sl_operator_batches WHERE sl_id = ? AND version = ?",
                    (sl_id, version),
                )
                conn.execute(
                    "DELETE FROM sl_pending_actions WHERE sl_id = ? AND version = ?",
                    (sl_id, version),
                )
                conn.execute(
                    "DELETE FROM sl_states WHERE sl_id = ? AND version = ?",
                    (sl_id, version),
                )
                conn.execute(
                    "DELETE FROM sl_runtime_configs WHERE sl_id = ? AND version = ?",
                    (sl_id, version),
                )

            self._put_runtime_config(
                conn,
                sl_id,
                version,
                issuer_vk,
                operator_wallet_address=operator_wallet_address,
                base_layer_account_id=base_layer_account_id,
                next_sequence=1,
            )
            self._put_scoped_state(conn, sl_id, version, "operator", genesis)

            if sl_id == core.SL_ID.hex() and version == core.VERSION.hex():
                self._put_json(conn, "sl_config", config)
                self._put_json(conn, "operator_meta", {"next_sequence": 1})
                self._put_state(conn, "operator", genesis)

        return genesis

    def is_initialized(
        self,
        sl_id: str = core.SL_ID.hex(),
        version: str = core.VERSION.hex(),
    ) -> bool:
        with self.connect() as conn:
            has_config = self._get_runtime_config(conn, sl_id, version) is not None
            has_state = self._get_scoped_state(conn, sl_id, version, "operator") is not None
        return bool(has_config and has_state)

    def load_sl_config(
        self,
        sl_id: str = core.SL_ID.hex(),
        version: str = core.VERSION.hex(),
    ) -> Optional[dict]:
        with self.connect() as conn:
            return self._get_runtime_config(conn, sl_id, version)

    def list_runtime_configs(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                  sl_id,
                  version,
                  issuer_vk,
                  operator_wallet_address,
                  base_layer_account_id,
                  next_sequence,
                  created_at,
                  updated_at
                FROM sl_runtime_configs
                ORDER BY updated_at DESC, sl_id, version
                """
            ).fetchall()
        return [
            {
                "sl_id": row["sl_id"],
                "version": row["version"],
                "issuer_vk": row["issuer_vk"],
                "operator_wallet_address": row["operator_wallet_address"],
                "base_layer_account_id": row["base_layer_account_id"],
                "next_sequence": int(row["next_sequence"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def load_operator_state(
        self,
        sl_id: str = core.SL_ID.hex(),
        version: str = core.VERSION.hex(),
    ) -> Optional[core.State]:
        with self.connect() as conn:
            return self._get_scoped_state(conn, sl_id, version, "operator")

    def load_verified_state(
        self,
        sl_id: str = core.SL_ID.hex(),
        version: str = core.VERSION.hex(),
    ) -> Optional[core.State]:
        with self.connect() as conn:
            return self._get_scoped_state(conn, sl_id, version, "verifier")

    def load_pending(
        self,
        sl_id: str = core.SL_ID.hex(),
        version: str = core.VERSION.hex(),
    ) -> list:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT action_json FROM sl_pending_actions
                WHERE sl_id = ? AND version = ?
                ORDER BY id
                """,
                (sl_id, version),
            ).fetchall()
        return [_loads(row["action_json"]) for row in rows]

    def pending_count(
        self,
        sl_id: str = core.SL_ID.hex(),
        version: str = core.VERSION.hex(),
    ) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count FROM sl_pending_actions
                WHERE sl_id = ? AND version = ?
                """,
                (sl_id, version),
            ).fetchone()
        return int(row["count"])

    def append_pending(
        self,
        action_dict: dict,
        sl_id: str = core.SL_ID.hex(),
        version: str = core.VERSION.hex(),
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sl_pending_actions (sl_id, version, action_json)
                VALUES (?, ?, ?)
                """,
                (sl_id, version, _dumps(action_dict)),
            )

    def next_batch_sequence(
        self,
        sl_id: str = core.SL_ID.hex(),
        version: str = core.VERSION.hex(),
    ) -> int:
        with self.connect() as conn:
            config = self._get_runtime_config(conn, sl_id, version)
            candidates = [int((config or {}).get("next_sequence", 1))]

            scoped_row = conn.execute(
                """
                SELECT MAX(sequence) AS max_sequence
                FROM sl_operator_batches
                WHERE sl_id = ? AND version = ?
                """,
                (sl_id, version),
            ).fetchone()
            if scoped_row and scoped_row["max_sequence"] is not None:
                candidates.append(int(scoped_row["max_sequence"]) + 1)

            if sl_id == core.SL_ID.hex() and version == core.VERSION.hex():
                legacy_row = conn.execute(
                    "SELECT MAX(sequence) AS max_sequence FROM operator_batches"
                ).fetchone()
                if legacy_row and legacy_row["max_sequence"] is not None:
                    candidates.append(int(legacy_row["max_sequence"]) + 1)

        return max(candidates)

    def next_nonce(
        self,
        sl_id: str = core.SL_ID.hex(),
        version: str = core.VERSION.hex(),
    ) -> int:
        state = self.load_operator_state(sl_id, version)
        if state is None:
            raise RuntimeError("operator state is not initialized")
        return state.nonce + self.pending_count(sl_id, version) + 1

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
                  purpose,
                  eon_address,
                  encrypted_account_json,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    record["id"],
                    record["owner_wallet_address"],
                    record["label"],
                    record.get("purpose", "sl_operator"),
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
        purpose: str,
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
                  purpose,
                  eon_address,
                  encrypted_account_json,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    account_id,
                    owner_wallet_address,
                    account_label,
                    purpose,
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
                  purpose,
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
                "purpose": row["purpose"],
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
          purpose,
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
            "purpose": row["purpose"],
            "eon_address": row["eon_address"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_secret:
            record["encrypted_account_json"] = row["encrypted_account_json"]
        return record

    def upsert_semantic_layer(self, record: dict) -> None:
        with self.connect() as conn:
            existing = self._get_semantic_layer(conn, record["sl_id"])
            assets = record.get("assets")
            if assets is None and existing is not None:
                assets = existing.get("assets", [])
            assets = assets or []
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
                  assets_json,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(sl_id) DO UPDATE SET
                  name = excluded.name,
                  version = excluded.version,
                  operator_wallet_address = excluded.operator_wallet_address,
                  base_layer_account_id = excluded.base_layer_account_id,
                  issuer_vk_ref = excluded.issuer_vk_ref,
                  operator_vk_ref = excluded.operator_vk_ref,
                  assets_json = excluded.assets_json,
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
                    _dumps(assets),
                ),
            )
            config = self._get_runtime_config(conn, record["sl_id"], record["version"])
            if config is not None:
                self._put_runtime_config(
                    conn,
                    record["sl_id"],
                    record["version"],
                    config["issuer_vk"],
                    operator_wallet_address=record["operator_wallet_address"],
                    base_layer_account_id=record.get("base_layer_account_id"),
                    next_sequence=int(config["next_sequence"]),
                )

    def _get_semantic_layer(self, conn: sqlite3.Connection, sl_id: str) -> Optional[dict]:
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
              assets_json,
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
            "assets": _loads(row["assets_json"] or "[]"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_semantic_layer(self, sl_id: str) -> Optional[dict]:
        with self.connect() as conn:
            return self._get_semantic_layer(conn, sl_id)

    def append_semantic_layer_asset(
        self,
        sl_id: str,
        version: str,
        asset: dict,
    ) -> dict:
        with self.connect() as conn:
            record = self._get_semantic_layer(conn, sl_id)
            if record is None or record["version"] != version:
                raise KeyError("semantic layer not found")

            assets = list(record.get("assets", []))
            if any(existing["asset_id"] == asset["asset_id"] for existing in assets):
                raise ValueError(f"asset already registered: {asset['asset_id']}")
            assets.append(asset)
            conn.execute(
                """
                UPDATE semantic_layers
                SET assets_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE sl_id = ? AND version = ?
                """,
                (_dumps(assets), sl_id, version),
            )
            record["assets"] = assets
            return record

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
                  assets_json,
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
                "assets": _loads(row["assets_json"] or "[]"),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def list_batches(
        self,
        sl_id: str = core.SL_ID.hex(),
        version: str = core.VERSION.hex(),
    ) -> list:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT record_json FROM sl_operator_batches
                WHERE sl_id = ? AND version = ?
                ORDER BY sequence
                """,
                (sl_id, version),
            ).fetchall()
        return [_loads(row["record_json"]) for row in rows]

    def latest_batch(
        self,
        sl_id: str = core.SL_ID.hex(),
        version: str = core.VERSION.hex(),
    ) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT record_json
                FROM sl_operator_batches
                WHERE sl_id = ? AND version = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (sl_id, version),
            ).fetchone()
        if row is None:
            return None
        return _loads(row["record_json"])

    def commit_operator_batch(
        self,
        new_state: core.State,
        record: dict,
        sequence: int,
        sl_id: str = core.SL_ID.hex(),
        version: str = core.VERSION.hex(),
    ) -> None:
        with self.connect() as conn:
            config = self._get_runtime_config(conn, sl_id, version)
            self._put_scoped_state(conn, sl_id, version, "operator", new_state)
            conn.execute(
                """
                DELETE FROM sl_pending_actions
                WHERE sl_id = ? AND version = ?
                """,
                (sl_id, version),
            )
            self._put_runtime_config(
                conn,
                sl_id,
                version,
                new_state.issuer_vk,
                operator_wallet_address=(config or {}).get("operator_wallet_address"),
                base_layer_account_id=(config or {}).get("base_layer_account_id"),
                next_sequence=sequence + 1,
            )
            conn.execute(
                """
                INSERT INTO sl_operator_batches (
                  sl_id,
                  version,
                  sequence,
                  record_json,
                  payload_hex,
                  prev_state_hash,
                  new_state_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sl_id,
                    version,
                    sequence,
                    _dumps(record),
                    record["payload_hex"],
                    record["prev_state_hash"],
                    record["new_state_hash"],
                ),
            )

            if sl_id == core.SL_ID.hex() and version == core.VERSION.hex():
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

    def record_devnet_submission(
        self,
        sequence: int,
        submission: dict,
        sl_id: str = core.SL_ID.hex(),
        version: str = core.VERSION.hex(),
    ) -> dict:
        with self.connect() as conn:
            return self._record_devnet_submission(conn, sequence, submission, sl_id, version)

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
