"""Semantic-layer verifier/indexer engine."""

from .eon_data import payload_header, scalar_hex_to_payload_bytes
from .plugins import PluginRegistry, VerificationResult
from .store import VerifierStore


class VerifierEngine:
    def __init__(
        self,
        store: VerifierStore,
        registry: PluginRegistry,
        plugin_config: dict | None = None,
    ):
        self.store = store
        self.registry = registry
        self.plugin_config = plugin_config or {}

    def ingest_event(self, event: dict) -> dict:
        event_key, exists = self.store.has_base_event(event)
        if exists:
            return {
                "stored": True,
                "duplicate": True,
                "event_key": event_key,
                "accepted": False,
                "ignored": True,
                "message": "base event already ingested",
            }

        try:
            payload = scalar_hex_to_payload_bytes(event.get("data_scalars", []))
            sl_id, version = payload_header(payload)
        except Exception as e:
            event_key, _inserted = self.store.append_base_event(event)
            return {
                "stored": True,
                "duplicate": False,
                "event_key": event_key,
                "accepted": False,
                "ignored": True,
                "message": f"could not decode payload header: {e}",
            }

        plugin = self.registry.get(sl_id, version)
        if plugin is None:
            event_key, _inserted = self.store.append_base_event(event)
            return {
                "stored": True,
                "duplicate": False,
                "event_key": event_key,
                "accepted": False,
                "ignored": True,
                "sl_id": sl_id.hex(),
                "version": version.hex(),
                "message": "no semantic-layer plugin registered",
            }

        try:
            transition = plugin.parse_payload(payload)
        except Exception as e:
            event_key, _inserted = self.store.append_base_event(event)
            return {
                "stored": True,
                "duplicate": False,
                "event_key": event_key,
                "accepted": False,
                "ignored": True,
                "sl_id": sl_id.hex(),
                "version": version.hex(),
                "message": f"could not parse semantic-layer payload: {e}",
            }
        checkpoint = self.store.load_checkpoint(sl_id, version)
        expected_sequence = 1 if checkpoint is None else int(checkpoint["sequence"]) + 1
        sequence = int(transition["sequence"])
        if sequence > expected_sequence:
            return {
                "stored": False,
                "duplicate": False,
                "event_key": event_key,
                "accepted": False,
                "ignored": True,
                "deferred": True,
                "sl_id": sl_id.hex(),
                "version": version.hex(),
                "sequence": sequence,
                "expected_sequence": expected_sequence,
                "message": f"waiting for sequence {expected_sequence}",
            }
        if sequence < expected_sequence:
            return {
                "stored": False,
                "duplicate": False,
                "event_key": event_key,
                "accepted": False,
                "ignored": True,
                "sl_id": sl_id.hex(),
                "version": version.hex(),
                "sequence": sequence,
                "expected_sequence": expected_sequence,
                "message": f"sequence {sequence} is already behind checkpoint {checkpoint['sequence']}",
            }

        event_key, inserted = self.store.append_base_event(event)
        if not inserted:
            return {
                "stored": True,
                "duplicate": True,
                "event_key": event_key,
                "accepted": False,
                "ignored": True,
                "message": "base event already ingested",
            }

        if checkpoint is None:
            prev_state = plugin.genesis_state(self.plugin_config.get(sl_id.hex(), {}))
        else:
            prev_state = plugin.state_from_dict(checkpoint["state"])

        result = plugin.verify_transition(prev_state, transition)
        state_json = plugin.state_to_dict(result.next_state) if result.valid else None
        state_hash = plugin.state_hash(result.next_state) if result.valid else None
        self.store.commit_verification(event_key, result, state_json, state_hash)

        return {
            "stored": True,
            "duplicate": False,
            "ignored": False,
            "accepted": result.valid,
            "event_key": event_key,
            "sl_id": result.sl_id.hex(),
            "version": result.version.hex(),
            "sequence": result.sequence,
            "message": result.message,
            "state_hash": state_hash,
        }

    def accept_envelope(self, plugin, envelope: dict) -> dict:
        try:
            transition = plugin.transition_from_envelope(envelope)
            prev_state = plugin.state_from_dict(envelope["prev_state"])
        except Exception as e:
            return {
                "accepted": False,
                "message": str(e),
            }
        result = plugin.verify_transition(prev_state, transition)
        if not result.valid:
            return {
                "accepted": False,
                "message": result.message,
            }

        checkpoint = self.store.load_checkpoint(result.sl_id, result.version)
        expected_sequence = 1 if checkpoint is None else int(checkpoint["sequence"]) + 1
        if result.sequence != expected_sequence:
            return {
                "accepted": False,
                "message": f"sequence mismatch: expected {expected_sequence}, got {result.sequence}",
            }
        if checkpoint is not None and checkpoint["state_hash"] != result.prev_state_hash:
            return {
                "accepted": False,
                "message": "prev_state_hash does not match current verifier state",
            }

        state_json = plugin.state_to_dict(result.next_state)
        state_hash = plugin.state_hash(result.next_state)
        self.store.commit_verification(
            envelope.get("event_key"),
            result,
            state_json,
            state_hash,
        )
        return {
            "accepted": True,
            "message": result.message,
            "sequence": result.sequence,
            "state": state_json,
            "state_hash": state_hash,
        }

    def sync_from_source(self, source, source_name: str = "default") -> dict:
        cursor = self.store.load_cursor(source_name)
        ingested = []
        latest_cursor = cursor
        for event in source.events_after(cursor):
            ingested.append(self.ingest_event(event))
            latest_cursor = str(event.get("cursor", latest_cursor or ""))
        if latest_cursor is not None:
            self.store.save_cursor(source_name, latest_cursor)
        return {
            "source": source_name,
            "previous_cursor": cursor,
            "latest_cursor": latest_cursor,
            "events": ingested,
        }
