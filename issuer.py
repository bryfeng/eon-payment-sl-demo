"""
issuer.py — Issuer CLI

The issuer is the centralized authority. It holds the issuer VK that was
registered at SL init. Only the issuer can mint, burn, freeze, and unfreeze.

Like wallets, the issuer does not post directly to the base layer. It
submits actions into operator_state/pending.json, and the operator
includes them in the next batch.

Commands:
  mint     --to <wallet> --amount <n>
  burn     --from <wallet> --amount <n>
  freeze   --target <wallet>
  unfreeze --target <wallet>
"""

import argparse
import sys

from core import (
    ActionType,
    append_pending,
    load_sl_config,
    next_nonce,
    resolve_address,
)


def _issuer_vk() -> str:
    return load_sl_config()["issuer_vk"]


# ---------------------------------------------------------------------------
# mint
# ---------------------------------------------------------------------------

def cmd_mint(args) -> None:
    if args.amount <= 0:
        sys.exit("error: amount must be positive")

    to_addr = resolve_address(args.to)
    action = {
        "type": ActionType.MINT.value,
        "sender_vk": _issuer_vk(),
        "nonce": next_nonce(),
        "to": to_addr,
        "amount": args.amount,
    }
    append_pending(action)
    print(f"Mint queued: {args.amount:,} tokens -> {args.to} (nonce {action['nonce']})")


# ---------------------------------------------------------------------------
# burn
# ---------------------------------------------------------------------------

def cmd_burn(args) -> None:
    if args.amount <= 0:
        sys.exit("error: amount must be positive")

    from_addr = resolve_address(args.from_name)
    action = {
        "type": ActionType.BURN.value,
        "sender_vk": _issuer_vk(),
        "nonce": next_nonce(),
        "from_addr": from_addr,
        "amount": args.amount,
    }
    append_pending(action)
    print(f"Burn queued: {args.amount:,} tokens <- {args.from_name} (nonce {action['nonce']})")


# ---------------------------------------------------------------------------
# freeze / unfreeze
# ---------------------------------------------------------------------------

def cmd_freeze(args) -> None:
    target_addr = resolve_address(args.target)
    action = {
        "type": ActionType.FREEZE.value,
        "sender_vk": _issuer_vk(),
        "nonce": next_nonce(),
        "target": target_addr,
    }
    append_pending(action)
    print(f"Freeze queued: {args.target} (nonce {action['nonce']})")


def cmd_unfreeze(args) -> None:
    target_addr = resolve_address(args.target)
    action = {
        "type": ActionType.UNFREEZE.value,
        "sender_vk": _issuer_vk(),
        "nonce": next_nonce(),
        "target": target_addr,
    }
    append_pending(action)
    print(f"Unfreeze queued: {args.target} (nonce {action['nonce']})")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="issuer.py",
        description="Issuer CLI: queue mint/burn/freeze/unfreeze actions.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_mint = sub.add_parser("mint", help="Queue a mint action.")
    p_mint.add_argument("--to", required=True, help="Recipient wallet name.")
    p_mint.add_argument("--amount", required=True, type=int)
    p_mint.set_defaults(func=cmd_mint)

    p_burn = sub.add_parser("burn", help="Queue a burn action.")
    # --from is a Python keyword; expose as --from but store on from_name.
    p_burn.add_argument("--from", required=True, dest="from_name",
                        help="Wallet name to burn from.")
    p_burn.add_argument("--amount", required=True, type=int)
    p_burn.set_defaults(func=cmd_burn)

    p_freeze = sub.add_parser("freeze", help="Queue a freeze action.")
    p_freeze.add_argument("--target", required=True, help="Wallet name to freeze.")
    p_freeze.set_defaults(func=cmd_freeze)

    p_unfreeze = sub.add_parser("unfreeze", help="Queue an unfreeze action.")
    p_unfreeze.add_argument("--target", required=True, help="Wallet name to unfreeze.")
    p_unfreeze.set_defaults(func=cmd_unfreeze)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
