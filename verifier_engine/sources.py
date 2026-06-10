"""Event source interfaces for verifier ingestion."""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Protocol

from .eon_data import (
    BUNDLE_SL_ID,
    decode_bundle_payload,
    decode_transition_payload,
    payload_header,
    scalar_hex_to_payload_bytes,
)


class EventSource(Protocol):
    def events_after(self, cursor: str | None = None) -> Iterable[dict]:
        ...


class FixtureEventSource:
    def __init__(self, events: Iterable[dict]):
        self.events = list(events)

    def events_after(self, cursor: str | None = None) -> Iterable[dict]:
        for event in self.events:
            if cursor is None or str(event.get("cursor", "")) > cursor:
                yield event


class NDJSONEventSource:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def events_after(self, cursor: str | None = None) -> Iterable[dict]:
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if cursor is None or str(event.get("cursor", "")) > cursor:
                    yield event


class BaseLayerAPIEventSource:
    """Poll a base-layer API's UTXO endpoint and normalize data outputs."""

    def __init__(
        self,
        base_url: str,
        *,
        owner: str | None = None,
        network_id: str = "devnet",
        timeout_seconds: int = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.owner = owner
        self.network_id = network_id
        self.timeout_seconds = timeout_seconds

    def events_after(self, cursor: str | None = None) -> Iterable[dict]:
        cursor_key = self._cursor_sort_key(cursor)
        for index, utxo in enumerate(self._sorted_utxos()):
            data_scalars = utxo.get("data") or utxo.get("data_scalars") or []
            if not data_scalars:
                continue

            utxo_id = str(utxo.get("id") or utxo.get("utxo_id") or "")
            if not utxo_id:
                continue

            output_index = int(utxo.get("output_index", index))
            event_cursor = self._event_cursor(utxo, utxo_id, output_index)
            event_cursor_key = self._cursor_sort_key(event_cursor)
            if cursor_key is not None and event_cursor_key is not None and event_cursor_key <= cursor_key:
                continue
            yield self._event_from_utxo(utxo, utxo_id, output_index)

    def event_for_hint(self, hint: dict) -> dict | None:
        expected_data = hint.get("data") or hint.get("data_scalars") or []
        if not isinstance(expected_data, list) or not expected_data:
            return None
        expected_data = [str(item) for item in expected_data]
        expected_tx_hash = str(hint.get("tx_hash") or hint.get("transaction_hash") or "")

        for index, utxo in enumerate(self._sorted_utxos()):
            data_scalars = utxo.get("data") or utxo.get("data_scalars") or []
            if [str(item) for item in data_scalars] != expected_data:
                continue

            utxo_id = str(utxo.get("id") or utxo.get("utxo_id") or "")
            if not utxo_id:
                continue

            utxo_tx_hash = str(utxo.get("tx_hash") or utxo.get("transaction_hash") or "")
            if expected_tx_hash and utxo_tx_hash and utxo_tx_hash != expected_tx_hash:
                continue

            return self._event_from_utxo(utxo, utxo_id, int(utxo.get("output_index", index)))
        return None

    def _utxos(self) -> list[dict]:
        path = "/utxos"
        if self.owner:
            path = f"{path}?{urllib.parse.urlencode({'owner': self.owner, 'limit': 50})}"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"base-layer API GET /utxos failed with HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"base-layer API GET /utxos failed: {e}") from e
        parsed = json.loads(body or "[]")
        if not isinstance(parsed, list):
            raise RuntimeError("base-layer API GET /utxos returned non-list JSON")
        return [item for item in parsed if isinstance(item, dict)]

    def _sorted_utxos(self) -> list[dict]:
        return sorted(
            self._utxos(),
            key=lambda utxo: (
                int(utxo.get("height", 0)),
                int(utxo.get("tx_index", 0)),
                int(utxo.get("output_index", 0)),
                self._payload_sequence(utxo),
                str(utxo.get("id") or utxo.get("utxo_id") or ""),
            ),
        )

    def _event_cursor(self, utxo: dict, utxo_id: str, output_index: int) -> str:
        return ":".join([
            self.network_id,
            f"{int(utxo.get('height', 0)):020d}",
            f"{int(utxo.get('tx_index', 0)):010d}",
            f"{int(output_index):010d}",
            f"{self._payload_sequence(utxo):020d}",
            utxo_id,
        ])

    def _cursor_sort_key(self, cursor: str | None) -> tuple[int, int, int, int, str] | None:
        if not cursor:
            return None
        parts = str(cursor).split(":", 5)
        if len(parts) != 6:
            return None
        try:
            return (
                int(parts[1]),
                int(parts[2]),
                int(parts[3]),
                int(parts[4]),
                parts[5],
            )
        except ValueError:
            return None

    def _payload_sequence(self, utxo: dict) -> int:
        data_scalars = utxo.get("data") or utxo.get("data_scalars") or []
        try:
            payload = scalar_hex_to_payload_bytes(data_scalars)
        except Exception:
            return 2**63 - 1
        if len(payload) < 14:
            return 2**63 - 1
        sl_id, _version = payload_header(payload)
        if sl_id == BUNDLE_SL_ID:
            sequences = []
            try:
                bundle = decode_bundle_payload(payload)
            except Exception:
                return 2**63 - 1
            for child in bundle.children:
                try:
                    sequences.append(decode_transition_payload(child).sequence)
                except Exception:
                    continue
            return min(sequences) if sequences else 2**63 - 1
        return int.from_bytes(payload[6:14], "big")

    def _event_from_utxo(self, utxo: dict, utxo_id: str, output_index: int) -> dict:
        data_scalars = utxo.get("data") or utxo.get("data_scalars") or []
        tx_hash = str(utxo.get("tx_hash") or utxo.get("transaction_hash") or utxo_id)
        return {
            "cursor": self._event_cursor(utxo, utxo_id, output_index),
            "event_key": f"{self.network_id}:utxo:{utxo_id}:{output_index}",
            "network_id": self.network_id,
            "height": int(utxo.get("height", 0)),
            "block_hash": utxo.get("block_hash"),
            "tx_hash": tx_hash,
            "tx_index": int(utxo.get("tx_index", 0)),
            "output_index": output_index,
            "utxo_id": utxo_id,
            "owner": utxo.get("owner"),
            "amount": str(utxo.get("amount", "0")),
            "data_scalars": data_scalars,
        }
