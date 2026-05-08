"""
EON Payment Token SL — Shared State Machine Module
===================================================

State machine spec for a Tier 1 financial primitive:
a centralized-issuer payment token (USDC model) running
as an EON semantic layer.

    State Machine:  S_{i+1} = F(S_i, Input_i)
    Prf3 Strategy:  Path (a) — post raw inputs + state hashes, verifiers re-execute
    Base Layer:     Data payload in a UTXO output

This module is imported by issuer.py, wallet.py, sl_operator.py, and verifier.py.
It owns:
  - identity primitives (hash_vk)
  - ActionType, Action, State, TransitionError
  - apply_action (F), process_batch, verify_batch
  - BatchResult + canonical demo payload serialization
  - JSON round-trip helpers for on-disk persistence
  - SL_ID / VERSION constants
  - self-test suite (run with: python core.py --test)
"""

import hashlib
import json
import struct
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Persistence paths (shared by all CLI scripts)
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent
STATE_DIR = ROOT / "operator_state"
WALLETS_DIR = ROOT / "wallets"

SL_CONFIG_FILE = STATE_DIR / "sl_config.json"
CURRENT_STATE_FILE = STATE_DIR / "current_state.json"
PENDING_FILE = STATE_DIR / "pending.json"


# ---------------------------------------------------------------------------
# Identity primitives (mirrors EON's VK/PreImg model)
# ---------------------------------------------------------------------------

def hash_vk(vk: str) -> str:
    """addr = Hash(VK) — deterministic address derivation."""
    return hashlib.sha256(vk.encode()).hexdigest()[:40]


# ---------------------------------------------------------------------------
# Action types (Inputs to the state machine)
# ---------------------------------------------------------------------------

class ActionType(Enum):
    MINT = "mint"
    BURN = "burn"
    TRANSFER = "transfer"
    FREEZE = "freeze"
    UNFREEZE = "unfreeze"


@dataclass
class Action:
    action_type: ActionType
    sender_vk: str           # VK of whoever is submitting this action
    nonce: int
    to: Optional[str] = None          # address (Hash(VK))
    from_addr: Optional[str] = None
    amount: Optional[int] = None
    target: Optional[str] = None      # for freeze/unfreeze

    def serialize(self) -> bytes:
        """Compact serialization for the demo payload."""
        payload = {
            "type": self.action_type.value,
            "sender_vk": self.sender_vk,
            "nonce": self.nonce,
        }
        if self.to is not None:
            payload["to"] = self.to
        if self.from_addr is not None:
            payload["from"] = self.from_addr
        if self.amount is not None:
            payload["amount"] = self.amount
        if self.target is not None:
            payload["target"] = self.target
        return json.dumps(payload, separators=(",", ":")).encode()

    def to_dict(self) -> dict:
        """JSON-friendly dict for on-disk persistence and payload envelopes."""
        d = {
            "type": self.action_type.value,
            "sender_vk": self.sender_vk,
            "nonce": self.nonce,
        }
        if self.to is not None:
            d["to"] = self.to
        if self.from_addr is not None:
            d["from_addr"] = self.from_addr
        if self.amount is not None:
            d["amount"] = self.amount
        if self.target is not None:
            d["target"] = self.target
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Action":
        return cls(
            action_type=ActionType(d["type"]),
            sender_vk=d["sender_vk"],
            nonce=d["nonce"],
            to=d.get("to"),
            from_addr=d.get("from_addr"),
            amount=d.get("amount"),
            target=d.get("target"),
        )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class State:
    issuer_vk: str
    balances: dict = field(default_factory=dict)    # address -> u64
    total_supply: int = 0
    nonce: int = 0
    frozen: set = field(default_factory=set)        # set of addresses

    def state_hash(self) -> str:
        """Commitment to current state — H(canonical serialization)."""
        canonical = json.dumps({
            "issuer_vk": self.issuer_vk,
            "balances": dict(sorted(self.balances.items())),
            "total_supply": self.total_supply,
            "nonce": self.nonce,
            "frozen": sorted(list(self.frozen)),
        }, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def clone(self) -> "State":
        return State(
            issuer_vk=self.issuer_vk,
            balances=dict(self.balances),
            total_supply=self.total_supply,
            nonce=self.nonce,
            frozen=set(self.frozen),
        )

    def get_balance(self, addr: str) -> int:
        return self.balances.get(addr, 0)

    def to_dict(self) -> dict:
        return {
            "issuer_vk": self.issuer_vk,
            "balances": dict(sorted(self.balances.items())),
            "total_supply": self.total_supply,
            "nonce": self.nonce,
            "frozen": sorted(list(self.frozen)),
            "state_hash": self.state_hash(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "State":
        return cls(
            issuer_vk=d["issuer_vk"],
            balances=dict(d.get("balances", {})),
            total_supply=d.get("total_supply", 0),
            nonce=d.get("nonce", 0),
            frozen=set(d.get("frozen", [])),
        )


# ---------------------------------------------------------------------------
# Transition function F(S, Input)
# ---------------------------------------------------------------------------

class TransitionError(Exception):
    """Raised when F rejects an input."""
    pass


def apply_action(state: State, action: Action) -> State:
    """
    F(S, Input) -> S'

    Pure function — returns new state or raises TransitionError.
    Does NOT mutate the input state.
    """
    s = state.clone()
    sender_addr = hash_vk(action.sender_vk)

    # Nonce check (replay protection)
    if action.nonce != s.nonce + 1:
        raise TransitionError(
            f"Invalid nonce: expected {s.nonce + 1}, got {action.nonce}"
        )

    if action.action_type == ActionType.MINT:
        if action.sender_vk != s.issuer_vk:
            raise TransitionError("Only issuer can mint")
        if action.amount is None or action.amount <= 0:
            raise TransitionError("Mint amount must be positive")
        if action.to is None:
            raise TransitionError("Mint requires 'to' address")
        s.balances[action.to] = s.get_balance(action.to) + action.amount
        s.total_supply += action.amount
        s.nonce = action.nonce

    elif action.action_type == ActionType.BURN:
        if action.sender_vk != s.issuer_vk:
            raise TransitionError("Only issuer can burn")
        if action.amount is None or action.amount <= 0:
            raise TransitionError("Burn amount must be positive")
        if action.from_addr is None:
            raise TransitionError("Burn requires 'from_addr'")
        if s.get_balance(action.from_addr) < action.amount:
            raise TransitionError(
                f"Insufficient balance: {s.get_balance(action.from_addr)} < {action.amount}"
            )
        s.balances[action.from_addr] -= action.amount
        s.total_supply -= action.amount
        s.nonce = action.nonce

    elif action.action_type == ActionType.TRANSFER:
        if action.from_addr is None or action.to is None:
            raise TransitionError("Transfer requires 'from_addr' and 'to'")
        if action.amount is None or action.amount <= 0:
            raise TransitionError("Transfer amount must be positive")
        # Sender must authenticate as from_addr
        if sender_addr != action.from_addr:
            raise TransitionError(
                f"Sender {sender_addr} does not match from_addr {action.from_addr}"
            )
        if action.from_addr in s.frozen:
            raise TransitionError(f"Address {action.from_addr} is frozen")
        if action.to in s.frozen:
            raise TransitionError(f"Address {action.to} is frozen")
        if s.get_balance(action.from_addr) < action.amount:
            raise TransitionError(
                f"Insufficient balance: {s.get_balance(action.from_addr)} < {action.amount}"
            )
        s.balances[action.from_addr] -= action.amount
        s.balances[action.to] = s.get_balance(action.to) + action.amount
        s.nonce = action.nonce

    elif action.action_type == ActionType.FREEZE:
        if action.sender_vk != s.issuer_vk:
            raise TransitionError("Only issuer can freeze")
        if action.target is None:
            raise TransitionError("Freeze requires 'target' address")
        s.frozen.add(action.target)
        s.nonce = action.nonce

    elif action.action_type == ActionType.UNFREEZE:
        if action.sender_vk != s.issuer_vk:
            raise TransitionError("Only issuer can unfreeze")
        if action.target is None:
            raise TransitionError("Unfreeze requires 'target' address")
        s.frozen.discard(action.target)
        s.nonce = action.nonce

    else:
        raise TransitionError(f"Unknown action type: {action.action_type}")

    return s


# ---------------------------------------------------------------------------
# Batch processing & canonical payload generation
# ---------------------------------------------------------------------------

SL_ID = b"\x00\x01\x00\x01"   # 4 bytes — unique SL identifier
VERSION = b"\x00\x01"          # 2 bytes — state machine version 0.1


@dataclass
class BatchResult:
    sl_id: bytes
    version: bytes
    prev_state_hash: str
    new_state_hash: str
    actions: list        # successfully applied actions, in order
    action_count: int    # total submitted (applied + rejected)
    applied: int
    rejected: list       # list of (action_index, error_message)

    def data_field_payload(self) -> bytes:
        """
        Serialize to the demo Data payload format.

        The Rust base layer stores Data as scalars. This demo keeps the payload
        as bytes and writes it as hex so the SL encoding is easy to inspect.

        [SL_ID: 4B][version: 2B][prev_hash: 32B][new_hash: 32B]
        [batch_count: 2B][actions...]
        """
        prev_hash_bytes = bytes.fromhex(self.prev_state_hash)
        new_hash_bytes = bytes.fromhex(self.new_state_hash)
        batch_count = struct.pack(">H", self.applied)

        serialized_actions = b""
        for a in self.actions:
            action_bytes = a.serialize()
            # length-prefix each action (2 byte big-endian)
            serialized_actions += struct.pack(">H", len(action_bytes)) + action_bytes

        return (
            self.sl_id
            + self.version
            + prev_hash_bytes
            + new_hash_bytes
            + batch_count
            + serialized_actions
        )

    def payload_size(self) -> int:
        return len(self.data_field_payload())

    def summary(self) -> str:
        lines = [
            f"  Batch: {self.applied} applied, {len(self.rejected)} rejected",
            f"  Prev state: {self.prev_state_hash[:16]}...",
            f"  New state:  {self.new_state_hash[:16]}...",
            f"  Payload size: {self.payload_size()} bytes",
        ]
        if self.rejected:
            for idx, err in self.rejected:
                lines.append(f"  REJECTED action #{idx}: {err}")
        return "\n".join(lines)


def process_batch(state: State, actions: list) -> tuple:
    """
    Process a batch of actions sequentially.

    Applies valid actions, skips invalid ones (logged in rejected).
    Returns (final_state, batch_result).
    """
    prev_hash = state.state_hash()
    current = state.clone()
    applied_actions = []
    rejected = []

    for i, action in enumerate(actions):
        try:
            current = apply_action(current, action)
            applied_actions.append(action)
        except TransitionError as e:
            rejected.append((i, str(e)))

    result = BatchResult(
        sl_id=SL_ID,
        version=VERSION,
        prev_state_hash=prev_hash,
        new_state_hash=current.state_hash(),
        actions=applied_actions,
        action_count=len(actions),
        applied=len(applied_actions),
        rejected=rejected,
    )

    return current, result


# ---------------------------------------------------------------------------
# Verifier — re-execution verification (Path (a) Prf3 strategy)
# ---------------------------------------------------------------------------

def verify_batch(
    prev_state: State,
    actions: list,
    claimed_new_hash: str,
) -> tuple:
    """
    Verifier re-executes the batch against prev_state
    and checks if the resulting state hash matches the claimed hash.

    This is the Path (a) verification model — no ZK proof needed.
    """
    current = prev_state.clone()
    for action in actions:
        try:
            current = apply_action(current, action)
        except TransitionError as e:
            return False, f"Re-execution failed: {e}"

    computed_hash = current.state_hash()
    if computed_hash != claimed_new_hash:
        return False, (
            f"State hash mismatch: computed {computed_hash[:16]}... "
            f"vs claimed {claimed_new_hash[:16]}..."
        )

    return True, "Verification passed"


# ---------------------------------------------------------------------------
# Persistence helpers (shared by all CLI scripts)
# ---------------------------------------------------------------------------

def _read_json(path: Path):
    with open(path) as f:
        return json.load(f)


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")


def require_sl_initialized() -> None:
    """Abort with a clear message if sl_operator.py init hasn't been run."""
    if not SL_CONFIG_FILE.exists() or not CURRENT_STATE_FILE.exists():
        sys.exit(
            "error: SL is not initialized. Run: python sl_operator.py init --issuer-vk <vk>"
        )


def load_sl_config() -> dict:
    require_sl_initialized()
    return _read_json(SL_CONFIG_FILE)


def load_current_state() -> State:
    require_sl_initialized()
    return State.from_dict(_read_json(CURRENT_STATE_FILE))


def save_current_state(state: State) -> None:
    _write_json(CURRENT_STATE_FILE, state.to_dict())


def load_pending() -> list:
    if not PENDING_FILE.exists():
        return []
    return _read_json(PENDING_FILE)


def save_pending(pending: list) -> None:
    _write_json(PENDING_FILE, pending)


def append_pending(action_dict: dict) -> None:
    pending = load_pending()
    pending.append(action_dict)
    save_pending(pending)


def next_nonce() -> int:
    """
    Compute the next nonce to assign to a newly queued action.

    Nonces are strictly sequential. The next one is the current state nonce
    plus the count of actions already sitting in the pending queue, plus one.
    If some pending actions turn out to be rejected at batch time, subsequent
    submissions after the batch will resync from the post-batch state nonce.
    """
    state = load_current_state()
    pending = load_pending()
    return state.nonce + len(pending) + 1


def load_wallet(name: str) -> dict:
    path = WALLETS_DIR / f"{name}.json"
    if not path.exists():
        sys.exit(
            f"error: wallet '{name}' does not exist. "
            f"Create it with: python wallet.py create --name {name}"
        )
    return _read_json(path)


def save_wallet(name: str, data: dict) -> None:
    _write_json(WALLETS_DIR / f"{name}.json", data)


def resolve_address(name: str) -> str:
    """Convert a wallet name into its on-chain address."""
    return load_wallet(name)["address"]


def short(addr: str) -> str:
    """Truncate an address to 8 hex chars for display."""
    return addr[:8] if addr else "(none)"


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------

def run_tests():
    print("Running test vectors...\n")
    passed = 0
    failed = 0

    ISSUER = "test_issuer_vk"
    USER_A = "user_a_vk"
    USER_B = "user_b_vk"
    addr_a = hash_vk(USER_A)
    addr_b = hash_vk(USER_B)

    def test(name, fn):
        nonlocal passed, failed
        try:
            fn()
            print(f"  PASS: {name}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {name} - {e}")
            failed += 1

    def t_mint():
        s = State(issuer_vk=ISSUER)
        s2 = apply_action(s, Action(ActionType.MINT, ISSUER, 1, to=addr_a, amount=100))
        assert s2.get_balance(addr_a) == 100
        assert s2.total_supply == 100
        assert s2.nonce == 1
    test("Basic mint", t_mint)

    def t_unauth_mint():
        s = State(issuer_vk=ISSUER)
        try:
            apply_action(s, Action(ActionType.MINT, USER_A, 1, to=addr_a, amount=100))
            assert False, "Should have raised"
        except TransitionError:
            pass
    test("Unauthorized mint rejected", t_unauth_mint)

    def t_transfer():
        s = State(issuer_vk=ISSUER, balances={addr_a: 500}, total_supply=500, nonce=1)
        s2 = apply_action(s, Action(
            ActionType.TRANSFER, USER_A, 2, from_addr=addr_a, to=addr_b, amount=200
        ))
        assert s2.get_balance(addr_a) == 300
        assert s2.get_balance(addr_b) == 200
        assert s2.total_supply == 500  # unchanged
    test("Basic transfer", t_transfer)

    def t_insufficient():
        s = State(issuer_vk=ISSUER, balances={addr_a: 50}, nonce=1)
        try:
            apply_action(s, Action(
                ActionType.TRANSFER, USER_A, 2, from_addr=addr_a, to=addr_b, amount=100
            ))
            assert False, "Should have raised"
        except TransitionError:
            pass
    test("Insufficient balance rejected", t_insufficient)

    def t_frozen_sender():
        s = State(issuer_vk=ISSUER, balances={addr_a: 500}, nonce=1, frozen={addr_a})
        try:
            apply_action(s, Action(
                ActionType.TRANSFER, USER_A, 2, from_addr=addr_a, to=addr_b, amount=100
            ))
            assert False, "Should have raised"
        except TransitionError:
            pass
    test("Frozen sender rejected", t_frozen_sender)

    def t_frozen_recipient():
        s = State(issuer_vk=ISSUER, balances={addr_a: 500}, nonce=1, frozen={addr_b})
        try:
            apply_action(s, Action(
                ActionType.TRANSFER, USER_A, 2, from_addr=addr_a, to=addr_b, amount=100
            ))
            assert False, "Should have raised"
        except TransitionError:
            pass
    test("Frozen recipient rejected", t_frozen_recipient)

    def t_burn():
        s = State(issuer_vk=ISSUER, balances={addr_a: 500}, total_supply=500, nonce=1)
        s2 = apply_action(s, Action(
            ActionType.BURN, ISSUER, 2, from_addr=addr_a, amount=200
        ))
        assert s2.get_balance(addr_a) == 300
        assert s2.total_supply == 300
    test("Basic burn", t_burn)

    def t_nonce_replay():
        s = State(issuer_vk=ISSUER, nonce=5)
        try:
            apply_action(s, Action(ActionType.MINT, ISSUER, 5, to=addr_a, amount=100))
            assert False, "Should have raised"
        except TransitionError:
            pass
    test("Nonce replay rejected", t_nonce_replay)

    def t_freeze_unfreeze():
        s = State(issuer_vk=ISSUER, nonce=0)
        s2 = apply_action(s, Action(ActionType.FREEZE, ISSUER, 1, target=addr_a))
        assert addr_a in s2.frozen
        s3 = apply_action(s2, Action(ActionType.UNFREEZE, ISSUER, 2, target=addr_a))
        assert addr_a not in s3.frozen
    test("Freeze and unfreeze", t_freeze_unfreeze)

    def t_vk_mismatch():
        s = State(issuer_vk=ISSUER, balances={addr_a: 500}, nonce=1)
        try:
            apply_action(s, Action(
                ActionType.TRANSFER, USER_B, 2, from_addr=addr_a, to=addr_b, amount=100
            ))
            assert False, "Should have raised"
        except TransitionError:
            pass
    test("VK/address mismatch rejected", t_vk_mismatch)

    def t_batch_mixed():
        s = State(issuer_vk=ISSUER)
        actions = [
            Action(ActionType.MINT, ISSUER, 1, to=addr_a, amount=1000),
            Action(ActionType.MINT, USER_A, 2, to=addr_b, amount=500),  # should fail
            Action(ActionType.MINT, ISSUER, 2, to=addr_b, amount=500),  # should succeed
        ]
        s2, result = process_batch(s, actions)
        assert result.applied == 2
        assert len(result.rejected) == 1
        assert s2.get_balance(addr_a) == 1000
        assert s2.get_balance(addr_b) == 500
    test("Batch with mixed valid/invalid", t_batch_mixed)

    def t_verify():
        s = State(issuer_vk=ISSUER)
        actions = [
            Action(ActionType.MINT, ISSUER, 1, to=addr_a, amount=1000),
        ]
        s2, result = process_batch(s, actions)
        valid, msg = verify_batch(s, actions, result.new_state_hash)
        assert valid, msg
    test("Verification round-trip", t_verify)

    def t_verify_tamper():
        s = State(issuer_vk=ISSUER)
        actions = [
            Action(ActionType.MINT, ISSUER, 1, to=addr_a, amount=1000),
        ]
        s2, result = process_batch(s, actions)
        valid, msg = verify_batch(s, actions, "0" * 64)
        assert not valid
    test("Tampered verification rejected", t_verify_tamper)

    def t_immutable():
        s = State(issuer_vk=ISSUER, balances={addr_a: 100}, nonce=0)
        original_hash = s.state_hash()
        _ = apply_action(s, Action(ActionType.MINT, ISSUER, 1, to=addr_a, amount=50))
        assert s.state_hash() == original_hash, "Original state was mutated"
        assert s.get_balance(addr_a) == 100, "Original balance was mutated"
    test("State immutability", t_immutable)

    def t_payload():
        s = State(issuer_vk=ISSUER)
        actions = [
            Action(ActionType.MINT, ISSUER, 1, to=addr_a, amount=100),
        ]
        _, result = process_batch(s, actions)
        payload = result.data_field_payload()
        assert payload[:4] == SL_ID
        assert payload[4:6] == VERSION
        assert len(payload[6:38]) == 32   # prev hash
        assert len(payload[38:70]) == 32  # new hash
        count = struct.unpack(">H", payload[70:72])[0]
        assert count == 1
    test("Payload serialization structure", t_payload)

    # Extra tests for the refactored JSON round-trip helpers used by the CLIs.
    def t_state_roundtrip():
        s = State(
            issuer_vk=ISSUER,
            balances={addr_a: 100, addr_b: 50},
            total_supply=150,
            nonce=7,
            frozen={addr_b},
        )
        s2 = State.from_dict(s.to_dict())
        assert s.state_hash() == s2.state_hash()
        assert s2.balances == s.balances
        assert s2.frozen == s.frozen
    test("State JSON round-trip preserves hash", t_state_roundtrip)

    def t_action_roundtrip():
        for a in [
            Action(ActionType.MINT, ISSUER, 1, to=addr_a, amount=100),
            Action(ActionType.TRANSFER, USER_A, 2, from_addr=addr_a, to=addr_b, amount=50),
            Action(ActionType.FREEZE, ISSUER, 3, target=addr_b),
        ]:
            a2 = Action.from_dict(a.to_dict())
            assert a2.serialize() == a.serialize()
    test("Action JSON round-trip preserves wire format", t_action_roundtrip)

    print(f"\n{passed} passed, {failed} failed out of {passed + failed} tests")
    return failed == 0


if __name__ == "__main__":
    if "--test" in sys.argv:
        success = run_tests()
        sys.exit(0 if success else 1)
    else:
        print("core.py is a library module. Run with --test to execute test vectors.")
        print("Use the CLI scripts: sl_operator.py, issuer.py, wallet.py, verifier.py")
