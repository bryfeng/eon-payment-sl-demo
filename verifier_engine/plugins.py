"""Semantic-layer plugin contracts and registry."""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class VerificationResult:
    valid: bool
    message: str
    sl_id: bytes
    version: bytes
    sequence: int | None = None
    prev_state_hash: str | None = None
    new_state_hash: str | None = None
    next_state: Any = None
    transition: Any = None
    payload_hex: str | None = None

    def to_log_entry(self) -> dict:
        return {
            "valid": self.valid,
            "message": self.message,
            "sl_id": self.sl_id.hex(),
            "version": self.version.hex(),
            "sequence": self.sequence,
            "prev_state_hash": self.prev_state_hash,
            "new_state_hash": self.new_state_hash,
            "payload_hex": self.payload_hex,
        }


class SemanticLayerPlugin(Protocol):
    sl_id: bytes
    supported_versions: set[bytes]

    def genesis_state(self, config: dict) -> Any:
        ...

    def state_hash(self, state: Any) -> str:
        ...

    def state_to_dict(self, state: Any) -> dict:
        ...

    def state_from_dict(self, data: dict) -> Any:
        ...

    def parse_payload(self, payload: bytes) -> Any:
        ...

    def verify_transition(self, prev_state: Any, transition: Any) -> VerificationResult:
        ...


class PluginRegistry:
    def __init__(self, plugins: list[SemanticLayerPlugin] | None = None):
        self._plugins: dict[tuple[str, str], SemanticLayerPlugin] = {}
        for plugin in plugins or []:
            self.register(plugin)

    def register(self, plugin: SemanticLayerPlugin) -> None:
        for version in plugin.supported_versions:
            self._plugins[(plugin.sl_id.hex(), version.hex())] = plugin

    def get(self, sl_id: bytes, version: bytes) -> SemanticLayerPlugin | None:
        return self._plugins.get((sl_id.hex(), version.hex()))

    def require(self, sl_id: bytes, version: bytes) -> SemanticLayerPlugin:
        plugin = self.get(sl_id, version)
        if plugin is None:
            raise KeyError(f"no plugin registered for sl_id={sl_id.hex()} version={version.hex()}")
        return plugin
