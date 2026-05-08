"""
wallet.py — End-user Wallet CLI

A wallet represents an individual user. Each wallet has a local VK and address
stored at wallets/<name>.json. Wallets can create identities, inspect balances,
and queue payment transfers for the operator to include in the next batch.

Wallets read balances from verifier-indexed state by default. They do not trust
the operator's local state as the source of truth for balances.
"""

import argparse
import secrets
import sys

from core import (
    ActionType,
    WALLETS_DIR,
    append_pending,
    hash_vk,
    load_current_state,
    load_verified_state,
    load_wallet,
    next_nonce,
    resolve_address,
    save_wallet,
)


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------

def cmd_create(args) -> None:
    path = WALLETS_DIR / f"{args.name}.json"
    if path.exists():
        sys.exit(f"error: wallet '{args.name}' already exists at {path}")

    vk = f"{args.name}_vk_{secrets.token_hex(4)}"
    address = hash_vk(vk)
    save_wallet(args.name, {
        "name": args.name,
        "vk": vk,
        "address": address,
    })

    print(f"Wallet created: {args.name}")
    print(f"  Address: {address}")
    print(f"  VK:      {vk}")
    print(f"  File:    {path}")


# ---------------------------------------------------------------------------
# address
# ---------------------------------------------------------------------------

def cmd_address(args) -> None:
    print(load_wallet(args.name)["address"])


# ---------------------------------------------------------------------------
# balance
# ---------------------------------------------------------------------------

def cmd_balance(args) -> None:
    wallet = load_wallet(args.name)
    if args.source == "operator":
        state = load_current_state()
        source = "operator"
    else:
        state = load_verified_state()
        source = "verifier"
    bal = state.get_balance(wallet["address"])
    frozen_note = "  (FROZEN)" if wallet["address"] in state.frozen else ""
    print(f"{args.name}: {bal:,} tokens{frozen_note}")
    print(f"  source: {source}")
    print(f"  state_hash: {state.state_hash()}")


# ---------------------------------------------------------------------------
# transfer
# ---------------------------------------------------------------------------

def cmd_transfer(args) -> None:
    if args.amount <= 0:
        sys.exit("error: amount must be positive")

    sender = load_wallet(args.name)
    recipient_addr = resolve_address(args.to)

    action = {
        "type": ActionType.TRANSFER.value,
        "sender_vk": sender["vk"],
        "nonce": next_nonce(),
        "from_addr": sender["address"],
        "to": recipient_addr,
        "amount": args.amount,
    }
    append_pending(action)
    print(
        f"Transfer queued: {args.name} -> {args.to}, "
        f"{args.amount:,} tokens (nonce {action['nonce']})"
    )


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="wallet.py",
        description="End-user wallet CLI: create identity, check balance, queue transfers.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="Create a new wallet identity.")
    p_create.add_argument("--name", required=True)
    p_create.set_defaults(func=cmd_create)

    p_address = sub.add_parser("address", help="Show wallet address.")
    p_address.add_argument("--name", required=True)
    p_address.set_defaults(func=cmd_address)

    p_balance = sub.add_parser("balance", help="Check wallet balance from verified state.")
    p_balance.add_argument("--name", required=True)
    p_balance.add_argument(
        "--source",
        choices=("verifier", "operator"),
        default="verifier",
        help="Read from verifier-indexed state by default; operator is for local debugging.",
    )
    p_balance.set_defaults(func=cmd_balance)

    p_transfer = sub.add_parser("transfer", help="Queue a transfer action.")
    p_transfer.add_argument("--name", required=True, help="Sender wallet name.")
    p_transfer.add_argument("--to", required=True, help="Recipient wallet name.")
    p_transfer.add_argument("--amount", required=True, type=int)
    p_transfer.set_defaults(func=cmd_transfer)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
