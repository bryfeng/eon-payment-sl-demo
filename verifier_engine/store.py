"""SQLite persistence for verifier/indexer state."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _loads(value: str) -> Any:
    return json.loads(value)


class VerifierStore:
    def __init__(self, db_path: Path):
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

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS base_events (
              event_key TEXT PRIMARY KEY,
              cursor TEXT NOT NULL,
              network_id TEXT NOT NULL,
              height INTEGER NOT NULL,
              block_hash TEXT,
              tx_hash TEXT NOT NULL,
              tx_index INTEGER NOT NULL,
              output_index INTEGER NOT NULL,
              utxo_id TEXT,
              owner TEXT,
              amount TEXT,
              data_scalars_json TEXT NOT NULL,
              event_json TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS verification_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sl_id TEXT NOT NULL,
              version TEXT NOT NULL,
              sequence INTEGER,
              event_key TEXT,
              verdict TEXT NOT NULL,
              message TEXT NOT NULL,
              prev_state_hash TEXT,
              new_state_hash TEXT,
              payload_hex TEXT,
              entry_json TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE UNIQUE INDEX IF NOT EXISTS verification_log_accepted_sequence_idx
              ON verification_log(sl_id, sequence)
              WHERE verdict = 'accepted' AND sequence IS NOT NULL;

            CREATE TABLE IF NOT EXISTS state_checkpoints (
              sl_id TEXT NOT NULL,
              version TEXT NOT NULL,
              sequence INTEGER NOT NULL,
              state_json TEXT NOT NULL,
              state_hash TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              PRIMARY KEY(sl_id, version)
            );

            CREATE TABLE IF NOT EXISTS sync_cursors (
              source TEXT PRIMARY KEY,
              cursor TEXT NOT NULL,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

    def reset(self) -> None:
        with self.connect() as conn:
            for table in (
                "sync_cursors",
                "state_checkpoints",
                "verification_log",
                "base_events",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'verification_log'")

    def reset_layer(self, sl_id: bytes, version: bytes) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                DELETE FROM state_checkpoints
                WHERE sl_id = ? AND version = ?
                """,
                (sl_id.hex(), version.hex()),
            )
            conn.execute(
                """
                DELETE FROM verification_log
                WHERE sl_id = ? AND version = ?
                """,
                (sl_id.hex(), version.hex()),
            )

    def _event_key(self, event: dict) -> str:
        network_id = str(event.get("network_id", "devnet"))
        height = int(event.get("height", 0))
        tx_hash = str(event.get("tx_hash", ""))
        output_index = int(event.get("output_index", 0))
        return f"{network_id}:{height}:{tx_hash}:{output_index}"

    def append_base_event(self, event: dict) -> tuple[str, bool]:
        event_key = str(event.get("event_key") or self._event_key(event))
        event = {**event, "event_key": event_key}
        with self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO base_events (
                      event_key,
                      cursor,
                      network_id,
                      height,
                      block_hash,
                      tx_hash,
                      tx_index,
                      output_index,
                      utxo_id,
                      owner,
                      amount,
                      data_scalars_json,
                      event_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_key,
                        str(event["cursor"]),
                        str(event.get("network_id", "devnet")),
                        int(event.get("height", 0)),
                        event.get("block_hash"),
                        str(event.get("tx_hash", "")),
                        int(event.get("tx_index", 0)),
                        int(event.get("output_index", 0)),
                        event.get("utxo_id"),
                        event.get("owner"),
                        str(event.get("amount", "0")),
                        _dumps(event.get("data_scalars", [])),
                        _dumps(event),
                    ),
                )
                return event_key, True
            except sqlite3.IntegrityError:
                return event_key, False

    def has_base_event(self, event: dict) -> tuple[str, bool]:
        event_key = str(event.get("event_key") or self._event_key(event))
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM base_events WHERE event_key = ?",
                (event_key,),
            ).fetchone()
        return event_key, row is not None

    def list_base_events(
        self,
        after: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self.connect() as conn:
            if after:
                rows = conn.execute(
                    """
                    SELECT event_json FROM base_events
                    WHERE cursor > ?
                    ORDER BY height, tx_index, output_index
                    LIMIT ?
                    """,
                    (after, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT event_json FROM base_events
                    ORDER BY height, tx_index, output_index
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [_loads(row["event_json"]) for row in rows]

    def load_checkpoint(self, sl_id: bytes, version: bytes) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT sequence, state_json, state_hash
                FROM state_checkpoints
                WHERE sl_id = ? AND version = ?
                """,
                (sl_id.hex(), version.hex()),
            ).fetchone()
        if row is None:
            return None
        return {
            "sequence": int(row["sequence"]),
            "state": _loads(row["state_json"]),
            "state_hash": row["state_hash"],
        }

    def list_verification_log(
        self,
        sl_id: bytes | None = None,
        version: bytes | None = None,
    ) -> list[dict]:
        with self.connect() as conn:
            if sl_id is None:
                rows = conn.execute(
                    """
                    SELECT
                      verification_log.entry_json,
                      verification_log.created_at,
                      base_events.tx_hash AS event_tx_hash,
                      base_events.output_index AS event_output_index,
                      base_events.utxo_id AS event_utxo_id,
                      base_events.amount AS event_amount
                    FROM verification_log
                    LEFT JOIN base_events
                      ON base_events.event_key = verification_log.event_key
                    ORDER BY verification_log.id
                    """
                ).fetchall()
            elif version is None:
                rows = conn.execute(
                    """
                    SELECT
                      verification_log.entry_json,
                      verification_log.created_at,
                      base_events.tx_hash AS event_tx_hash,
                      base_events.output_index AS event_output_index,
                      base_events.utxo_id AS event_utxo_id,
                      base_events.amount AS event_amount
                    FROM verification_log
                    LEFT JOIN base_events
                      ON base_events.event_key = verification_log.event_key
                    WHERE verification_log.sl_id = ?
                    ORDER BY verification_log.sequence
                    """,
                    (sl_id.hex(),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT
                      verification_log.entry_json,
                      verification_log.created_at,
                      base_events.tx_hash AS event_tx_hash,
                      base_events.output_index AS event_output_index,
                      base_events.utxo_id AS event_utxo_id,
                      base_events.amount AS event_amount
                    FROM verification_log
                    LEFT JOIN base_events
                      ON base_events.event_key = verification_log.event_key
                    WHERE verification_log.sl_id = ? AND verification_log.version = ?
                    ORDER BY verification_log.sequence
                    """,
                    (sl_id.hex(), version.hex()),
                ).fetchall()
        entries = []
        for row in rows:
            entry = _loads(row["entry_json"])
            entry.setdefault("created_at", row["created_at"])
            if row["event_tx_hash"] is not None:
                entry.setdefault("tx_hash", row["event_tx_hash"])
            if row["event_output_index"] is not None:
                entry.setdefault("output_index", row["event_output_index"])
            if row["event_utxo_id"] is not None:
                entry.setdefault("utxo_id", row["event_utxo_id"])
            if row["event_amount"] is not None:
                entry.setdefault("amount", row["event_amount"])
            entries.append(entry)
        return entries

    def commit_verification(
        self,
        event_key: str | None,
        result,
        state_json: dict | None,
        state_hash: str | None,
    ) -> None:
        entry = {
            **result.to_log_entry(),
            "event_key": event_key,
            "verdict": "accepted" if result.valid else "rejected",
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO verification_log (
                  sl_id,
                  version,
                  sequence,
                  event_key,
                  verdict,
                  message,
                  prev_state_hash,
                  new_state_hash,
                  payload_hex,
                  entry_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.sl_id.hex(),
                    result.version.hex(),
                    result.sequence,
                    event_key,
                    entry["verdict"],
                    result.message,
                    result.prev_state_hash,
                    result.new_state_hash,
                    result.payload_hex,
                    _dumps(entry),
                ),
            )
            if result.valid and state_json is not None and state_hash is not None:
                conn.execute(
                    """
                    INSERT INTO state_checkpoints (
                      sl_id,
                      version,
                      sequence,
                      state_json,
                      state_hash,
                      updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(sl_id, version) DO UPDATE SET
                      sequence = excluded.sequence,
                      state_json = excluded.state_json,
                      state_hash = excluded.state_hash,
                      updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        result.sl_id.hex(),
                        result.version.hex(),
                        int(result.sequence or 0),
                        _dumps(state_json),
                        state_hash,
                    ),
                )

    def save_cursor(self, source: str, cursor: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_cursors (source, cursor, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(source) DO UPDATE SET
                  cursor = excluded.cursor,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (source, cursor),
            )

    def load_cursor(self, source: str) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT cursor FROM sync_cursors WHERE source = ?",
                (source,),
            ).fetchone()
        if row is None:
            return None
        return row["cursor"]
