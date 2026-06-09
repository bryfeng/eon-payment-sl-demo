"""Semantic-layer verifier/indexer engine."""

from types import SimpleNamespace

from .eon_data import (
    BUNDLE_SL_ID,
    BUNDLE_VERSION,
    decode_bundle_payload,
    payload_header,
    payload_hex_to_bytes,
    scalar_hex_to_payload_bytes,
)
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

    def _checkpoint_state_hash(self, plugin, checkpoint: dict) -> str:
        try:
            return plugin.state_hash(plugin.state_from_dict(checkpoint["state"]))
        except Exception:
            return checkpoint["state_hash"]

    def _plugin_config(self, sl_id: bytes, version: bytes) -> dict:
        return (
            self.plugin_config.get(f"{sl_id.hex()}:{version.hex()}")
            or self.plugin_config.get(sl_id.hex())
            or {}
        )

    def _event_payload(self, event: dict) -> bytes:
        if event.get("payload_hex"):
            return payload_hex_to_bytes(str(event["payload_hex"]))
        return scalar_hex_to_payload_bytes(event.get("data_scalars", []))

    def _existing_event_is_terminal(self, event_key: str, retry_rejected: bool) -> bool:
        accepted = self.store.has_accepted_verification(event_key)
        rejected = self.store.has_rejected_verification(event_key)
        if accepted:
            return True
        if rejected and not retry_rejected:
            return True
        return False

    def _duplicate_result(self, event_key: str) -> dict:
        return {
            "stored": True,
            "duplicate": True,
            "event_key": event_key,
            "accepted": False,
            "ignored": True,
            "message": "base event already ingested",
        }

    def ingest_event(self, event: dict, *, retry_rejected: bool = False) -> dict:
        event_key, exists = self.store.has_base_event(event)

        try:
            payload = self._event_payload(event)
            sl_id, version = payload_header(payload)
        except Exception as e:
            if not exists:
                event_key, _inserted = self.store.append_base_event(event)
            return {
                "stored": True,
                "duplicate": False,
                "event_key": event_key,
                "accepted": False,
                "ignored": True,
                "message": f"could not decode payload header: {e}",
            }

        if sl_id == BUNDLE_SL_ID:
            return self._ingest_bundle(
                event_key,
                event,
                payload,
                exists=exists,
                retry_rejected=retry_rejected,
            )

        if exists and self._existing_event_is_terminal(event_key, retry_rejected):
            return self._duplicate_result(event_key)

        plugin = self.registry.get(sl_id, version)
        if plugin is None:
            if not exists:
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
            if not exists:
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

        if not exists:
            event_key, inserted = self.store.append_base_event(event)
        else:
            inserted = False
        if not inserted and not exists and not retry_rejected:
            return {
                "stored": True,
                "duplicate": True,
                "event_key": event_key,
                "accepted": False,
                "ignored": True,
                "message": "base event already ingested",
            }

        if checkpoint is None:
            prev_state = plugin.genesis_state(self._plugin_config(sl_id, version))
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

    def _parse_registered_bundle_children(self, children: list[bytes]) -> list[dict]:
        parsed = []
        for child in children:
            sl_id, version = payload_header(child)
            plugin = self.registry.get(sl_id, version)
            if plugin is None:
                continue
            transition = plugin.parse_payload(child)
            parsed.append({
                "sl_id": sl_id,
                "version": version,
                "plugin": plugin,
                "transition": transition,
            })
        return parsed

    def _bundle_rejection_result(
        self,
        *,
        message: str,
        sl_id: bytes = BUNDLE_SL_ID,
        version: bytes = BUNDLE_VERSION,
        transition: dict | None = None,
        payload_hex: str | None = None,
    ) -> VerificationResult:
        return VerificationResult(
            valid=False,
            message=message,
            sl_id=sl_id,
            version=version,
            sequence=(
                int(transition["sequence"])
                if transition is not None and transition.get("sequence") is not None
                else None
            ),
            prev_state_hash=transition.get("prev_state_hash") if transition else None,
            new_state_hash=transition.get("new_state_hash") if transition else None,
            transition=transition,
            payload_hex=payload_hex or (transition.get("payload_hex") if transition else None),
        )

    def _context_transition(self, transition: dict) -> dict:
        if "actions" in transition or "actions_applied" not in transition:
            return transition
        return {
            **transition,
            "actions": transition["actions_applied"],
        }

    def _ingest_bundle(
        self,
        event_key: str,
        event: dict,
        payload: bytes,
        *,
        exists: bool,
        retry_rejected: bool,
    ) -> dict:
        if (
            exists
            and self.store.has_rejected_verification(event_key)
            and not self.store.has_accepted_verification(event_key)
            and not retry_rejected
        ):
            return self._duplicate_result(event_key)

        try:
            bundle = decode_bundle_payload(payload)
            parsed = self._parse_registered_bundle_children(bundle.children)
        except Exception as e:
            if not exists:
                event_key, _inserted = self.store.append_base_event(event)
            result = self._bundle_rejection_result(
                message=f"bundle rejected: {e}",
                payload_hex=payload.hex(),
            )
            self.store.commit_verification(event_key, result, None, None)
            return {
                "stored": True,
                "duplicate": False,
                "ignored": False,
                "accepted": False,
                "event_key": event_key,
                "message": result.message,
            }

        if not parsed:
            if not exists:
                event_key, _inserted = self.store.append_base_event(event)
            return {
                "stored": True,
                "duplicate": False,
                "ignored": True,
                "accepted": False,
                "event_key": event_key,
                "bundle_id": bundle.bundle_id,
                "message": "bundle has no registered semantic-layer children",
            }

        child_transitions = [self._context_transition(child["transition"]) for child in parsed]
        context = SimpleNamespace(
            bundle_id=bundle.bundle_id,
            child_transitions=child_transitions,
            height=event.get("height"),
            event=event,
        )
        staged = []
        skipped = []

        for child in parsed:
            plugin = child["plugin"]
            checkpoint = self.store.load_checkpoint(child["sl_id"], child["version"])
            expected_sequence = 1 if checkpoint is None else int(checkpoint["sequence"]) + 1
            transition = child["transition"]
            sequence = int(transition["sequence"])

            if sequence > expected_sequence:
                return {
                    "stored": False,
                    "duplicate": False,
                    "event_key": event_key,
                    "accepted": False,
                    "ignored": True,
                    "deferred": True,
                    "bundle_id": bundle.bundle_id,
                    "sl_id": child["sl_id"].hex(),
                    "version": child["version"].hex(),
                    "sequence": sequence,
                    "expected_sequence": expected_sequence,
                    "message": f"waiting for sequence {expected_sequence}",
                }
            if sequence < expected_sequence:
                skipped.append({
                    "sl_id": child["sl_id"].hex(),
                    "version": child["version"].hex(),
                    "sequence": sequence,
                    "expected_sequence": expected_sequence,
                })
                continue

            if checkpoint is None:
                prev_state = plugin.genesis_state(self._plugin_config(child["sl_id"], child["version"]))
            else:
                prev_state = plugin.state_from_dict(checkpoint["state"])

            result = plugin.verify_transition(prev_state, transition, context)
            if not result.valid:
                if not exists:
                    event_key, _inserted = self.store.append_base_event(event)
                    exists = True
                self.store.commit_verification(event_key, result, None, None)
                return {
                    "stored": True,
                    "duplicate": False,
                    "ignored": False,
                    "accepted": False,
                    "event_key": event_key,
                    "bundle_id": bundle.bundle_id,
                    "message": result.message,
                    "failed_sl_id": result.sl_id.hex(),
                }
            staged.append((
                result,
                plugin.state_to_dict(result.next_state),
                plugin.state_hash(result.next_state),
            ))

        if not staged:
            return {
                "stored": exists,
                "duplicate": bool(skipped),
                "ignored": True,
                "accepted": False,
                "event_key": event_key,
                "bundle_id": bundle.bundle_id,
                "skipped_children": skipped,
                "message": "registered bundle children are already behind local checkpoints",
            }

        if not exists:
            event_key, _inserted = self.store.append_base_event(event)
            exists = True

        for result, state_json, state_hash in staged:
            self.store.commit_verification(event_key, result, state_json, state_hash)

        return {
            "stored": True,
            "duplicate": False,
            "ignored": False,
            "accepted": True,
            "event_key": event_key,
            "bundle_id": bundle.bundle_id,
            "children": [
                {
                    "sl_id": result.sl_id.hex(),
                    "version": result.version.hex(),
                    "sequence": result.sequence,
                    "state_hash": state_hash,
                }
                for result, _state_json, state_hash in staged
            ],
            "skipped_children": skipped,
            "message": "bundle child payloads verified",
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
        if (
            checkpoint is not None
            and self._checkpoint_state_hash(plugin, checkpoint) != result.prev_state_hash
        ):
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

    def sync_from_source(
        self,
        source,
        source_name: str = "default",
        *,
        retry_rejected: bool = False,
    ) -> dict:
        cursor = self.store.load_cursor(source_name)
        ingested = []
        latest_cursor = cursor
        for event in source.events_after(cursor):
            ingested.append(self.ingest_event(event, retry_rejected=retry_rejected))
            latest_cursor = str(event.get("cursor", latest_cursor or ""))
        if latest_cursor is not None:
            self.store.save_cursor(source_name, latest_cursor)
        return {
            "source": source_name,
            "previous_cursor": cursor,
            "latest_cursor": latest_cursor,
            "events": ingested,
        }
