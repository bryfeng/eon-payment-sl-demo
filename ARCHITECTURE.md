# EON Payment Token SL — Architecture

## Data flow: who touches what

```
                        ┌────────────────────────────────────────────┐
                        │              core.py (library)             │
                        │  F(S, Input) -> S'  +  payload format      │
                        │  State, Action, apply_action, verify_batch │
                        └────────────────────────────────────────────┘
                                  ▲ import       ▲ import
                                  │              │
    ┌─────────────┐   queue   ┌───┴───┐     ┌───┴───┐              ┌──────────┐
    │  issuer.py  │──────────▶│       │     │       │──────▶ reads │wallet.py │
    │ mint/burn/  │           │pending│     │current│              │ balance  │
    │freeze/unfrz │           │.json  │     │_state │              └──────────┘
    └─────────────┘           │       │     │.json  │
                              └───┬───┘     └───▲───┘
    ┌─────────────┐   queue      │              │ write
    │  wallet.py  │──────────────┘              │
    │  transfer   │                             │
    └─────────────┘                    ┌────────┴─────────┐
           │                           │   sl_operator.py    │
           │ reads (resolve            │  reads pending,  │
           │  name → address)          │  runs F(),       │
           ▼                           │  writes block,   │
     ┌──────────┐                      │  advances state  │
     │ wallets/ │◀── reads ────────────┤                  │
     │  *.json  │                      └────────┬─────────┘
     └──────────┘                               │ append block
                                                ▼
                                       ┌────────────────┐
                                       │  base_layer/   │     ┌──────────────┐
                                       │ block_001.json │◀────│ verifier.py  │
                                       │ block_002.json │read │ re-executes  │
                                       │      ...       │     │ from genesis │
                                       └────────────────┘     └──────────────┘
```

**Who writes where:**

| Directory                           | Writer(s)                                         | Readers                          |
| ----------------------------------- | ------------------------------------------------- | -------------------------------- |
| `wallets/`                          | `wallet.py create`                                | all CLIs (name → address lookup) |
| `operator_state/sl_config.json`     | `sl_operator.py init`                                | `issuer.py`, `verifier.py`       |
| `operator_state/current_state.json` | `sl_operator.py`                                     | `wallet.py balance`, nonce calc  |
| `operator_state/pending.json`       | `issuer.py`, `wallet.py`, `sl_operator.py` (clears)  | `sl_operator.py batch`              |
| `base_layer/block_*.json`           | `sl_operator.py batch` (only)                        | `verifier.py`                    |

## Lifecycle of a single action

```
 issuer.py mint --to alice --amount 10000
        │
        │ 1. load_sl_config() → read issuer_vk
        │ 2. resolve_address("alice") → read wallets/alice.json
        │ 3. next_nonce() → state.nonce + len(pending) + 1
        │ 4. append_pending({type:"mint", sender_vk:..., nonce:N, to:addr, amount:10000})
        ▼
  operator_state/pending.json           ← action sits here until batched
        │
        │ sl_operator.py batch
        │ 5. Action.from_dict(d) for each pending entry
        │ 6. process_batch(state, actions)
        │      → for each action: apply_action(state, action)  (F)
        │      → returns (new_state, BatchResult)
        │ 7. BatchResult.data_field_payload() → SL_ID|ver|prev|new|count|actions
        │ 8. write base_layer/block_NNN.json
        │ 9. save_current_state(new_state)
        │10. save_pending([])
        ▼
  base_layer/block_NNN.json             ← the public record
        │
        │ verifier.py check --block base_layer/block_NNN.json
        │11. _replay_up_to(N)  → re-execute blocks 1..N-1 from genesis
        │12. verify_batch(prev_state, actions, claimed_new_hash)
        │      → re-run F() and compare state_hash
        ▼
  VERIFIED  or  FAILED
```

## Function map per file

### core.py — the only module with business logic

```
Identity
  hash_vk(vk)                       → addr = SHA256(vk)[:40]

Types
  ActionType                        enum: MINT|BURN|TRANSFER|FREEZE|UNFREEZE
  Action(type, sender_vk, nonce, to?, from_addr?, amount?, target?)
    .serialize()                    → canonical demo payload bytes
    .to_dict()/.from_dict()         → JSON round-trip for pending & blocks
  State(issuer_vk, balances, total_supply, nonce, frozen)
    .state_hash()                   → SHA256 of canonical serialization
    .clone()                        → defensive copy (F is pure)
    .get_balance(addr)
    .to_dict()/.from_dict()         → JSON round-trip for current_state.json
  TransitionError                   raised by F on rejection

Transition
  apply_action(state, action) → State            the F() itself
  process_batch(state, [actions]) → (State, BatchResult)
  verify_batch(prev_state, actions, claimed_hash) → (bool, msg)
  BatchResult
    .data_field_payload()           SL_ID|ver|prev|new|count|actions
    .payload_size(), .summary()
  SL_ID = 0x00010001, VERSION = 0x0001

Persistence paths  (ROOT = __file__.parent)
  STATE_DIR, WALLETS_DIR, BASE_LAYER_DIR
  SL_CONFIG_FILE, CURRENT_STATE_FILE, PENDING_FILE

Persistence helpers (shared by all CLIs)
  require_sl_initialized()          abort if init not run
  load_sl_config() / load_current_state() / save_current_state()
  load_pending() / save_pending() / append_pending()
  next_nonce()                      state.nonce + len(pending) + 1
  load_wallet(name) / save_wallet(name, data) / resolve_address(name)
  short(addr)                       first 8 hex chars

Tests
  run_tests()                       17 tests covering F, batch, verify, round-trips
  __main__: python core.py --test
```

### sl_operator.py — runs F, posts blocks

```
cmd_init      create STATE_DIR, WALLETS_DIR, BASE_LAYER_DIR;
              write sl_config.json; persist genesis state; empty pending
cmd_pending   load_pending() → list queued actions with _describe_action()
cmd_status    load_current_state() → print hash/supply/nonce + labeled
              balances and frozen addresses (uses _wallet_addr_index())
cmd_batch     load pending → Action.from_dict → process_batch
              → write block_NNN.json with payload_hex, timestamps, reject reasons
              → save_current_state(new_state); save_pending([])
cmd_reset     shutil.rmtree STATE_DIR, BASE_LAYER_DIR, WALLETS_DIR

_describe_action(d)    one-line formatter used by pending + batch output
_wallet_addr_index()   reverse map addr → name for status readability
```

### wallet.py — end-user actions

```
cmd_create    generate vk = f"{name}_vk_{random_hex(8)}";
              address = hash_vk(vk);
              save_wallet(name, {name, vk, address})
cmd_address   print load_wallet(name).address
cmd_balance   load_current_state().get_balance(wallet.address)
              + note if address is in state.frozen
cmd_transfer  load_wallet(sender); resolve_address(recipient);
              append_pending({type:TRANSFER, sender_vk:wallet.vk,
                              nonce:next_nonce(), from_addr, to, amount})
```

### issuer.py — authority actions

```
_issuer_vk()     load_sl_config()["issuer_vk"]
cmd_mint         append_pending MINT    (sender_vk = issuer_vk)
cmd_burn         append_pending BURN    (flag --from stored as from_name
                                         to avoid Python keyword clash)
cmd_freeze       append_pending FREEZE
cmd_unfreeze     append_pending UNFREEZE

Every issuer command uses next_nonce() — user never specifies nonces.
```

### verifier.py — trust-minimized re-execution

```
_load_block(path)              read JSON, fail loudly if missing
_genesis_state()               State(issuer_vk=sl_config.issuer_vk)
_replay_up_to(N)               walk blocks 1..N-1, verifying each, return
                               the state that should precede block N
_apply_block(state, block, label)
                               sanity-check prev_hash chain,
                               verify payload_hex,
                               verify_batch, then re-apply actions to advance

cmd_check          replay to block_N; verify_batch against claimed new_hash
cmd_check_all      replay entire chain from genesis, one block at a time;
                   stop at first failure, exit 1 on any mismatch
```

## Trust model

```
Trusts:                          Does NOT trust:
─────────────────────────        ─────────────────────────────
core.py's F()                    operator's "new_state_hash" claims
                                   (re-executes and re-hashes them)
SL_CONFIG_FILE.issuer_vk         the contents of pending.json
  (the registered authority)       (F() re-validates sender_vk)
The block chain as posted        the operator's block ordering
  (prev_hash must chain)           (caught by prev_hash mismatch)
```

The operator is **ecosystem-trusted**, not protocol-trusted. It could lie about `new_state_hash` once, but the next block's `prev_state_hash` won't chain, and `verifier.py check-all` surfaces the break immediately.
