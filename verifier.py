"""
verifier.py — Trust-minimized Verifier CLI

The verifier downloads block files from the base layer and re-executes
each batch against the previous state. If the computed state hash matches
the block's claimed new_state_hash, the block is verified.

This is the Path (a) Prf3 strategy: the base layer does not verify the SL.
Anyone can be a verifier; they trust no one (not the issuer, not the
operator), only the transition function F() from core.py and the blocks
on the base layer.

Commands:
  check --block <path>     Verify a single block
  check-all                Verify every block in base_layer/ from genesis
"""

import argparse
import struct
import sys
from pathlib import Path

from core import (
    Action,
    BASE_LAYER_DIR,
    SL_ID,
    SL_CONFIG_FILE,
    State,
    VERSION,
    _read_json,
    apply_action,
    verify_batch,
)


def _load_block(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"error: block file not found: {path}")
    return _read_json(path)


def _genesis_state() -> State:
    if not SL_CONFIG_FILE.exists():
        sys.exit(
            "error: sl_config.json not found. Cannot verify without the SL config "
            "(issuer_vk is needed to reconstruct genesis)."
        )
    config = _read_json(SL_CONFIG_FILE)
    return State(issuer_vk=config["issuer_vk"])


def _replay_up_to(target_block_number: int) -> State:
    """
    Reconstruct the state as it was just before `target_block_number`.

    For block 1: returns genesis state.
    For block N: re-executes blocks 1..N-1 against genesis, re-verifying
    each on the way (so a corrupted earlier block will surface here).
    """
    state = _genesis_state()
    for i in range(1, target_block_number):
        path = BASE_LAYER_DIR / f"block_{i:03d}.json"
        block = _load_block(path)
        state = _apply_block(state, block, label=f"block_{i:03d}")
    return state


def _apply_block(state: State, block: dict, label: str) -> State:
    """Apply the block's already-applied actions and sanity-check the hash."""
    if state.state_hash() != block["prev_state_hash"]:
        sys.exit(
            f"error: {label} prev_state_hash does not match our reconstructed "
            f"state. Chain is broken at this block."
        )
    _verify_payload(block, label)
    actions = [Action.from_dict(d) for d in block["actions_applied"]]
    valid, msg = verify_batch(state, actions, block["new_state_hash"])
    if not valid:
        sys.exit(f"error: {label} failed verification during replay: {msg}")
    # Re-run to produce the next state (verify_batch recomputed it but didn't return it).
    for a in actions:
        state = apply_action(state, a)
    return state


def _verify_payload(block: dict, label: str) -> None:
    """Check that payload_hex is exactly the canonical encoding of the block."""
    actions = [Action.from_dict(d) for d in block["actions_applied"]]
    payload = (
        SL_ID
        + VERSION
        + bytes.fromhex(block["prev_state_hash"])
        + bytes.fromhex(block["new_state_hash"])
        + struct.pack(">H", len(actions))
    )
    for action in actions:
        action_bytes = action.serialize()
        payload += struct.pack(">H", len(action_bytes)) + action_bytes

    claimed = block.get("payload_hex")
    expected = payload.hex()
    if claimed != expected:
        sys.exit(f"error: {label} payload_hex does not match decoded block fields")


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def cmd_check(args) -> None:
    block_path = Path(args.block)
    block = _load_block(block_path)
    block_number = block["block_number"]
    label = f"block_{block_number:03d}"

    # Reconstruct the prior state.
    prev_state = _replay_up_to(block_number)

    # Sanity: prev hash matches.
    if prev_state.state_hash() != block["prev_state_hash"]:
        print(f"{label}: FAILED")
        print(
            f"  prev_state_hash mismatch: "
            f"reconstructed {prev_state.state_hash()[:16]}... "
            f"vs claimed {block['prev_state_hash'][:16]}..."
        )
        sys.exit(1)
    _verify_payload(block, label)

    # Re-execute this block.
    actions = [Action.from_dict(d) for d in block["actions_applied"]]
    valid, msg = verify_batch(prev_state, actions, block["new_state_hash"])

    if valid:
        print(f"{label}: VERIFIED")
        print(f"  prev_state_hash: {block['prev_state_hash']}")
        print(f"  new_state_hash:  {block['new_state_hash']}")
        print(f"  actions applied: {len(actions)}")
        print(f"  actions rejected at batch time: {len(block.get('actions_rejected', []))}")
    else:
        print(f"{label}: FAILED - {msg}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# check-all
# ---------------------------------------------------------------------------

def cmd_check_all(args) -> None:
    if not BASE_LAYER_DIR.exists():
        sys.exit("error: base_layer/ does not exist. Is the SL initialized?")

    block_paths = sorted(BASE_LAYER_DIR.glob("block_*.json"))
    if not block_paths:
        print("No blocks in base_layer/. Nothing to verify.")
        return

    state = _genesis_state()
    print(f"Verifying {len(block_paths)} block(s) from genesis...")
    print(f"  Genesis state hash: {state.state_hash()}")

    failures = 0
    for path in block_paths:
        block = _load_block(path)
        label = f"block_{block['block_number']:03d}"
        actions = [Action.from_dict(d) for d in block["actions_applied"]]

        if state.state_hash() != block["prev_state_hash"]:
            print(f"  {label}: FAILED - prev_state_hash does not chain")
            failures += 1
            # Stop the chain walk; state is no longer trustworthy.
            break
        try:
            _verify_payload(block, label)
        except SystemExit as e:
            print(f"  {label}: FAILED - payload_hex does not match decoded block fields")
            failures += 1
            break

        valid, msg = verify_batch(state, actions, block["new_state_hash"])
        if not valid:
            print(f"  {label}: FAILED - {msg}")
            failures += 1
            break

        for a in actions:
            state = apply_action(state, a)

        print(
            f"  {label}: VERIFIED  "
            f"({len(actions)} action(s), "
            f"new hash {block['new_state_hash'][:16]}...)"
        )

    total = len(block_paths)
    verified = total - failures if failures == 0 else total - failures - 1
    if failures == 0:
        print(f"\nAll {total}/{total} blocks verified.")
    else:
        print(f"\n{verified}/{total} blocks verified, {failures} failure(s) found.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="verifier.py",
        description="Trust-minimized verifier: re-execute blocks and check state hashes.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="Verify a single block.")
    p_check.add_argument("--block", required=True, help="Path to block JSON file.")
    p_check.set_defaults(func=cmd_check)

    p_check_all = sub.add_parser("check-all", help="Verify every block from genesis.")
    p_check_all.set_defaults(func=cmd_check_all)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
