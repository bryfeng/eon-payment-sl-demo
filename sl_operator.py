"""
sl_operator.py — SL Operator CLI

The operator runs the Payment Token state machine. It:
  - initializes the SL (genesis state + directories)
  - receives actions from issuer.py and wallet.py via operator_state/pending.json
  - batches and applies them via F() from core.py
  - prepares the canonical payload that should be posted to EON devnet
  - maintains operator_state/current_state.json

Commands:
  init --issuer-vk <vk>    Initialize the SL with a genesis state
  pending                  Show actions queued for the next batch
  status                   Show current SL state (hash, supply, balances, frozen)
  batch                    Process the pending queue and print the devnet payload
  reset                    Delete local operator state and wallets
"""

import argparse
import shutil
import sys

from core import (
    Action,
    SL_CONFIG_FILE,
    SL_ID,
    STATE_DIR,
    State,
    VERSION,
    WALLETS_DIR,
    _read_json,
    _write_json,
    load_current_state,
    load_pending,
    load_sl_config,
    process_batch,
    save_current_state,
    save_pending,
    short,
)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def cmd_init(args) -> None:
    if SL_CONFIG_FILE.exists():
        sys.exit(
            "error: SL already initialized. "
            "Run 'python sl_operator.py reset' first to wipe state."
        )

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    WALLETS_DIR.mkdir(parents=True, exist_ok=True)

    config = {
        "issuer_vk": args.issuer_vk,
        "sl_id": SL_ID.hex(),
        "version": VERSION.hex(),
    }
    _write_json(SL_CONFIG_FILE, config)

    genesis = State(issuer_vk=args.issuer_vk)
    save_current_state(genesis)
    save_pending([])

    print("SL initialized.")
    print(f"  Issuer VK:   {args.issuer_vk}")
    print(f"  SL ID:       0x{SL_ID.hex()}")
    print(f"  Version:     0x{VERSION.hex()}")
    print(f"  Genesis state hash: {genesis.state_hash()}")


# ---------------------------------------------------------------------------
# pending
# ---------------------------------------------------------------------------

def cmd_pending(args) -> None:
    pending = load_pending()
    if not pending:
        print("No pending actions.")
        return

    print(f"Pending actions ({len(pending)}):")
    for i, a in enumerate(pending):
        print(f"  [{i}] {_describe_action(a)}")


def _describe_action(a: dict) -> str:
    t = a["type"]
    nonce = a["nonce"]
    sender = short(a["sender_vk"])  # display only; the real VK can be long
    if t == "mint":
        return f"nonce={nonce:<3} MINT       {a.get('amount'):>8} -> {short(a.get('to'))}  (by {sender})"
    if t == "burn":
        return f"nonce={nonce:<3} BURN       {a.get('amount'):>8} <- {short(a.get('from_addr'))}  (by {sender})"
    if t == "transfer":
        return (
            f"nonce={nonce:<3} TRANSFER   {a.get('amount'):>8} "
            f"{short(a.get('from_addr'))} -> {short(a.get('to'))}  (by {sender})"
        )
    if t == "freeze":
        return f"nonce={nonce:<3} FREEZE     {short(a.get('target'))}  (by {sender})"
    if t == "unfreeze":
        return f"nonce={nonce:<3} UNFREEZE   {short(a.get('target'))}  (by {sender})"
    return f"nonce={nonce:<3} {t.upper()}  (by {sender})"


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args) -> None:
    state = load_current_state()

    print("SL Status")
    print(f"  State hash:   {state.state_hash()}")
    print(f"  Total supply: {state.total_supply:,}")
    print(f"  Nonce:        {state.nonce}")

    if state.balances:
        print(f"  Balances ({len(state.balances)}):")
        name_by_addr = _wallet_addr_index()
        for addr in sorted(state.balances):
            label = name_by_addr.get(addr, "")
            label_str = f"  ({label})" if label else ""
            print(f"    {short(addr)}...  {state.balances[addr]:>10,}{label_str}")
    else:
        print("  Balances:     (none)")

    if state.frozen:
        print(f"  Frozen ({len(state.frozen)}):")
        name_by_addr = _wallet_addr_index()
        for addr in sorted(state.frozen):
            label = name_by_addr.get(addr, "")
            label_str = f"  ({label})" if label else ""
            print(f"    {short(addr)}...{label_str}")
    else:
        print("  Frozen:       (none)")


def _wallet_addr_index() -> dict:
    """address -> wallet name, for friendlier status output."""
    index = {}
    if not WALLETS_DIR.exists():
        return index
    for path in sorted(WALLETS_DIR.glob("*.json")):
        try:
            data = _read_json(path)
            index[data["address"]] = path.stem
        except Exception:
            continue
    return index


# ---------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------

def cmd_batch(args) -> None:
    _ = load_sl_config()  # asserts SL is initialized
    state = load_current_state()
    pending = load_pending()

    if not pending:
        print("No pending actions. Nothing to batch.")
        return

    actions = [Action.from_dict(d) for d in pending]
    new_state, result = process_batch(state, actions)
    rejected_index = {idx: err for idx, err in result.rejected}
    payload = result.data_field_payload()

    save_current_state(new_state)
    save_pending([])

    print("Batch prepared for EON devnet submission")
    print(f"  Submitted: {result.action_count}")
    print(f"  Applied:   {result.applied}")
    print(f"  Rejected:  {len(result.rejected)}")
    for i, action_dict in enumerate(pending):
        if i in rejected_index:
            print(f"    [{i}] REJECTED  {_describe_action(action_dict)}")
            print(f"          reason: {rejected_index[i]}")
        else:
            print(f"    [{i}] applied   {_describe_action(action_dict)}")
    print(f"  Prev state hash: {result.prev_state_hash}")
    print(f"  New state hash:  {result.new_state_hash}")
    print(f"  Payload size:    {len(payload)} bytes")
    print(f"  Payload hex:     {payload.hex()}")
    print("  Next step: submit this payload through the EON devnet adapter.")


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

def cmd_reset(args) -> None:
    removed = []
    for d in (STATE_DIR, WALLETS_DIR):
        if d.exists():
            shutil.rmtree(d)
            removed.append(str(d.relative_to(d.parent.parent)))
    if removed:
        print("Removed: " + ", ".join(removed))
    else:
        print("Nothing to remove. (no state present)")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sl_operator.py",
        description="SL Operator — runs the state machine and prepares devnet payloads.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize the SL with a genesis state.")
    p_init.add_argument("--issuer-vk", required=True, help="Issuer's verification key.")
    p_init.set_defaults(func=cmd_init)

    p_pending = sub.add_parser("pending", help="Show actions queued for the next batch.")
    p_pending.set_defaults(func=cmd_pending)

    p_status = sub.add_parser("status", help="Show current SL state.")
    p_status.set_defaults(func=cmd_status)

    p_batch = sub.add_parser("batch", help="Process the pending queue and print the devnet payload.")
    p_batch.set_defaults(func=cmd_batch)

    p_reset = sub.add_parser("reset", help="Delete local operator state and wallets.")
    p_reset.set_defaults(func=cmd_reset)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
