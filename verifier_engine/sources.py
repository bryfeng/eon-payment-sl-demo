"""Event source interfaces for verifier ingestion."""

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, Protocol


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
        del cursor
        for index, utxo in enumerate(self._utxos()):
            data_scalars = utxo.get("data") or utxo.get("data_scalars") or []
            if not data_scalars:
                continue

            utxo_id = str(utxo.get("id") or utxo.get("utxo_id") or "")
            if not utxo_id:
                continue

            owner = utxo.get("owner")
            tx_hash = str(utxo.get("tx_hash") or utxo.get("transaction_hash") or utxo_id)
            output_index = int(utxo.get("output_index", index))
            yield {
                "cursor": f"{self.network_id}:utxo:{utxo_id}:{output_index}",
                "event_key": f"{self.network_id}:utxo:{utxo_id}:{output_index}",
                "network_id": self.network_id,
                "height": int(utxo.get("height", 0)),
                "block_hash": utxo.get("block_hash"),
                "tx_hash": tx_hash,
                "tx_index": int(utxo.get("tx_index", 0)),
                "output_index": output_index,
                "utxo_id": utxo_id,
                "owner": owner,
                "amount": str(utxo.get("amount", "0")),
                "data_scalars": data_scalars,
            }

    def _utxos(self) -> list[dict]:
        path = "/utxos"
        if self.owner:
            path = f"{path}?owner={self.owner}"
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
