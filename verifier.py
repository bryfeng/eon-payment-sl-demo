"""
verifier.py — Trust-minimized Verifier CLI

The verifier checks a decoded EON devnet payload envelope by re-executing the
Payment SL transition function against the previous state. If the computed
state hash matches the claimed new_state_hash, the transition is verified.

This is the Path (a) Prf3 strategy: EON orders and stores the payload, but it
does not verify the Payment SL's business rules.

Commands:
  check-envelope --file <path>    Verify one decoded payload envelope
"""

import argparse
import struct
import sys
from pathlib import Path

from core import (
    Action,
    SL_ID,
    VERSION,
    _read_json,
    State,
    verify_batch,
)


def _load_envelope(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"error: envelope file not found: {path}")
    return _read_json(path)


def _canonical_payload_hex(envelope: dict) -> str:
    actions = [Action.from_dict(d) for d in envelope["actions_applied"]]
    payload = (
        SL_ID
        + VERSION
        + bytes.fromhex(envelope["prev_state_hash"])
        + bytes.fromhex(envelope["new_state_hash"])
        + struct.pack(">H", len(actions))
    )
    for action in actions:
        action_bytes = action.serialize()
        payload += struct.pack(">H", len(action_bytes)) + action_bytes
    return payload.hex()


def verify_envelope(envelope: dict) -> tuple[bool, str]:
    """
    Verify one decoded devnet payload envelope.

    Expected shape:
      {
        "prev_state": {... State.to_dict() ...},
        "prev_state_hash": "...",
        "new_state_hash": "...",
        "actions_applied": [... Action.to_dict() ...],
        "payload_hex": "..."
      }

    The envelope can be produced by a devnet adapter after fetching and decoding
    a data-bearing EON UTXO. It is not a local base-layer block.
    """
    required = [
        "prev_state",
        "prev_state_hash",
        "new_state_hash",
        "actions_applied",
        "payload_hex",
    ]
    missing = [key for key in required if key not in envelope]
    if missing:
        return False, f"missing required field(s): {', '.join(missing)}"

    prev_state = State.from_dict(envelope["prev_state"])
    if prev_state.state_hash() != envelope["prev_state_hash"]:
        return False, (
            "prev_state_hash mismatch: "
            f"computed {prev_state.state_hash()[:16]}... "
            f"vs claimed {envelope['prev_state_hash'][:16]}..."
        )

    expected_payload = _canonical_payload_hex(envelope)
    if envelope["payload_hex"] != expected_payload:
        return False, "payload_hex does not match decoded envelope fields"

    actions = [Action.from_dict(d) for d in envelope["actions_applied"]]
    return verify_batch(prev_state, actions, envelope["new_state_hash"])


def cmd_check_envelope(args) -> None:
    envelope = _load_envelope(Path(args.file))
    valid, msg = verify_envelope(envelope)
    if not valid:
        print(f"FAILED - {msg}")
        sys.exit(1)

    print("VERIFIED")
    print(f"  prev_state_hash: {envelope['prev_state_hash']}")
    print(f"  new_state_hash:  {envelope['new_state_hash']}")
    print(f"  actions applied: {len(envelope['actions_applied'])}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="verifier.py",
        description="Trust-minimized verifier: re-execute decoded EON payloads.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check-envelope", help="Verify one decoded payload envelope.")
    p_check.add_argument("--file", required=True, help="Path to decoded payload envelope JSON.")
    p_check.set_defaults(func=cmd_check_envelope)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
