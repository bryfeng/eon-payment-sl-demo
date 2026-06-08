"""Payment SL plugin for the generic verifier engine."""

from core import (
    Action,
    BatchResult,
    PayloadDecodeError,
    SL_ID,
    VERSION,
    State,
    apply_action,
    parse_data_field_payload,
    verify_batch,
)
from verifier_engine.plugins import VerificationResult


class PaymentSLPlugin:
    def __init__(self, sl_id: bytes = SL_ID, version: bytes = VERSION):
        self.sl_id = sl_id
        self.version = version
        self.supported_versions = {version}

    def genesis_state(self, config: dict) -> State:
        issuer_vk = config.get("issuer_vk")
        if not issuer_vk:
            raise ValueError("Payment SL plugin requires issuer_vk for genesis state")
        state = State(issuer_vk=issuer_vk)
        for asset in config.get("assets", []):
            state.register_asset(asset)
        return state

    def state_hash(self, state: State) -> str:
        return state.state_hash()

    def state_to_dict(self, state: State) -> dict:
        return state.to_dict()

    def state_from_dict(self, data: dict) -> State:
        return State.from_dict(data)

    def parse_payload(self, payload: bytes) -> dict:
        decoded = parse_data_field_payload(payload, self.sl_id, self.version)
        return {
            **decoded,
            "sl_id": self.sl_id.hex(),
            "version": self.version.hex(),
        }

    def transition_from_envelope(self, envelope: dict) -> dict:
        required = [
            "sequence",
            "prev_state_hash",
            "new_state_hash",
            "actions_applied",
            "payload_hex",
        ]
        missing = [key for key in required if key not in envelope]
        if missing:
            raise PayloadDecodeError(f"missing required field(s): {', '.join(missing)}")

        try:
            payload = bytes.fromhex(envelope["payload_hex"])
        except ValueError as e:
            raise PayloadDecodeError(f"payload_hex is not valid hex: {e}") from e
        decoded = self.parse_payload(payload)

        expected_payload = self.canonical_payload_hex(envelope)
        if envelope["payload_hex"].lower() != expected_payload:
            raise PayloadDecodeError("payload_hex does not match decoded envelope fields")

        decoded_fields = {
            "sequence": int(envelope["sequence"]),
            "prev_state_hash": envelope["prev_state_hash"],
            "new_state_hash": envelope["new_state_hash"],
            "actions_applied": envelope["actions_applied"],
            "payload_hex": envelope["payload_hex"].lower(),
            "sl_id": self.sl_id.hex(),
            "version": self.version.hex(),
        }
        if decoded != decoded_fields:
            raise PayloadDecodeError("payload_hex does not decode to the envelope fields")

        return decoded

    def canonical_payload_hex(self, envelope: dict) -> str:
        actions = [Action.from_dict(d) for d in envelope["actions_applied"]]
        result = BatchResult(
            sl_id=self.sl_id,
            version=self.version,
            sequence=int(envelope["sequence"]),
            prev_state_hash=envelope["prev_state_hash"],
            new_state_hash=envelope["new_state_hash"],
            actions=actions,
            action_count=len(actions),
            applied=len(actions),
            rejected=[],
        )
        return result.data_field_payload().hex()

    def verify_transition(self, prev_state: State, transition: dict) -> VerificationResult:
        sequence = int(transition["sequence"])
        actions = [Action.from_dict(d) for d in transition["actions_applied"]]

        if prev_state.state_hash() != transition["prev_state_hash"]:
            return VerificationResult(
                valid=False,
                message=(
                    "prev_state_hash mismatch: "
                    f"computed {prev_state.state_hash()[:16]}... "
                    f"vs claimed {transition['prev_state_hash'][:16]}..."
                ),
                sl_id=self.sl_id,
                version=self.version,
                sequence=sequence,
                prev_state_hash=transition["prev_state_hash"],
                new_state_hash=transition["new_state_hash"],
                transition=transition,
                payload_hex=transition["payload_hex"],
            )

        valid, msg = verify_batch(prev_state, actions, transition["new_state_hash"])
        next_state = None
        if valid:
            next_state = prev_state
            for action in actions:
                next_state = apply_action(next_state, action)

        return VerificationResult(
            valid=valid,
            message=msg,
            sl_id=self.sl_id,
            version=self.version,
            sequence=sequence,
            prev_state_hash=transition["prev_state_hash"],
            new_state_hash=transition["new_state_hash"],
            next_state=next_state,
            transition=transition,
            payload_hex=transition["payload_hex"],
        )


PAYMENT_PLUGIN = PaymentSLPlugin()


def payment_plugin_for(sl_id: bytes, version: bytes) -> PaymentSLPlugin:
    return PaymentSLPlugin(sl_id=sl_id, version=version)
