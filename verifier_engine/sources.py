"""Event source interfaces for verifier ingestion."""

import json
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
