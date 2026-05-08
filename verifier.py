"""
verifier.py — Trust-minimized Verifier CLI

The verifier checks a decoded EON devnet payload envelope by re-executing the
Payment SL transition function against the previous state. If the computed
state hash matches the claimed new_state_hash, the transition is verified.

This is the Path (a) Prf3 strategy: EON orders and stores the payload, but it
does not verify the Payment SL's business rules.

Commands:
  check-envelope --file <path>    Verify one decoded payload envelope
  accept-envelope --file <path>   Verify and persist latest verifier state
  status                          Show latest verifier-indexed state
  reset                           Delete verifier-indexed state
"""

import argparse
import shutil
import struct
import sys
from pathlib import Path

from core import (
    Action,
    SL_ID,
    VERSION,
    VERIFIER_STATE_DIR,
    _read_json,
    State,
    apply_action,
    load_verified_log,
    load_verified_state,
    save_verified_log,
    save_verified_state,
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


def _state_after_envelope(envelope: dict) -> State:
    state = State.from_dict(envelope["prev_state"])
    for action_dict in envelope["actions_applied"]:
        state = apply_action(state, Action.from_dict(action_dict))
    return state


def accept_envelope(envelope: dict) -> tuple[bool, str]:
    valid, msg = verify_envelope(envelope)
    if not valid:
        return False, msg

    state = _state_after_envelope(envelope)
    save_verified_state(state)

    log = load_verified_log()
    log.append({
        "sequence": envelope.get("sequence", len(log) + 1),
        "prev_state_hash": envelope["prev_state_hash"],
        "new_state_hash": envelope["new_state_hash"],
        "actions_applied": len(envelope["actions_applied"]),
        "payload_hex": envelope["payload_hex"],
    })
    save_verified_log(log)
    return True, "accepted"


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


def cmd_accept_envelope(args) -> None:
    envelope = _load_envelope(Path(args.file))
    valid, msg = accept_envelope(envelope)
    if not valid:
        print(f"REJECTED - {msg}")
        sys.exit(1)

    state = load_verified_state()
    print("ACCEPTED")
    print(f"  verified_state_hash: {state.state_hash()}")
    print(f"  total_supply:        {state.total_supply:,}")
    print(f"  nonce:               {state.nonce}")


def cmd_status(args) -> None:
    state = load_verified_state()
    log = load_verified_log()
    print("Verifier State")
    print(f"  State hash:   {state.state_hash()}")
    print(f"  Total supply: {state.total_supply:,}")
    print(f"  Nonce:        {state.nonce}")
    print(f"  Accepted:     {len(log)} payload(s)")


def cmd_reset(args) -> None:
    if VERIFIER_STATE_DIR.exists():
        shutil.rmtree(VERIFIER_STATE_DIR)
        print("Removed: verifier_state")
    else:
        print("Nothing to remove. (no verifier state present)")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="verifier.py",
        description="Trust-minimized verifier: re-execute decoded EON payloads.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check-envelope", help="Verify one decoded payload envelope.")
    p_check.add_argument("--file", required=True, help="Path to decoded payload envelope JSON.")
    p_check.set_defaults(func=cmd_check_envelope)

    p_accept = sub.add_parser(
        "accept-envelope",
        help="Verify one decoded payload envelope and update verifier-indexed state.",
    )
    p_accept.add_argument("--file", required=True, help="Path to decoded payload envelope JSON.")
    p_accept.set_defaults(func=cmd_accept_envelope)

    p_status = sub.add_parser("status", help="Show latest verifier-indexed state.")
    p_status.set_defaults(func=cmd_status)

    p_reset = sub.add_parser("reset", help="Delete verifier-indexed state.")
    p_reset.set_defaults(func=cmd_reset)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
