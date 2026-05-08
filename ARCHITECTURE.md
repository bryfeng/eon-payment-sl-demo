# EON Payment Token SL — Architecture

## Master Data Flow

```mermaid
flowchart LR
  issuer["issuer.py<br/>mint / burn / freeze"]
  wallet["wallet.py<br/>create / transfer"]
  walletRead["wallet.py balance<br/>read verified state"]
  wallets["wallets/*.json<br/>local labels + demo VKs"]
  pending["operator_state/pending.json<br/>queued SL inputs"]
  meta["operator_state/operator_meta.json<br/>next batch sequence"]
  state["operator_state/current_state.json<br/>operator's current SL state"]
  core["core.py<br/>F(S, Input), State, Action, BatchResult, payload parser"]
  operator["sl_operator.py<br/>runs F, advances state, emits payload_hex"]
  adapter["devnet_adapter.py<br/>payload bytes <-> EON scalars"]
  eoncli["eoncli / eon-sdk<br/>authorize + submit"]
  devnet["EON devnet<br/>UTXO Data availability"]
  verifier["verifier.py<br/>verify decoded payload envelope"]
  verified["verifier_state/current_state.json<br/>accepted state index"]
  verifiedLog["verifier_state/verified_log.json<br/>accepted sequence log"]

  wallet --> wallets
  issuer --> pending
  wallet --> pending
  wallet --> walletRead
  pending --> operator
  state --> operator
  meta --> operator
  core --> operator
  operator --> state
  operator --> meta
  operator --> adapter
  adapter --> eoncli
  eoncli --> devnet
  verifier --> devnet
  devnet --> adapter
  adapter --> verifier
  core --> verifier
  verifier --> verified
  verifier --> verifiedLog
  verified --> verifier
  verifiedLog --> verifier
  walletRead <--> verifier
```

The project no longer has a local base-layer substitute. The canonical target
for posted data is EON devnet.

## Isolated Flow Diagrams

These diagrams are intentionally narrower than the master map. Use them as
standalone references when explaining one part of the system at a time.

### 1. Action Intake

```mermaid
flowchart LR
  issuer["Issuer CLI<br/>mint / burn / freeze / unfreeze"]
  wallet["Wallet CLI<br/>transfer"]
  config["operator_state/sl_config.json<br/>issuer authority"]
  labels["wallets/*.json<br/>local VK + address labels"]
  nonce["next_nonce()<br/>current state + pending length"]
  pending["operator_state/pending.json<br/>queued SL inputs"]

  issuer --> config
  issuer --> nonce
  issuer --> pending
  wallet --> labels
  wallet --> nonce
  wallet --> pending
```

What this isolates: issuer and wallet CLIs do not mutate balances. They only
form signed-intent-like `Action` objects and append them to the operator queue.

### 2. Operator Batch Execution

```mermaid
flowchart LR
  pending["pending.json<br/>queued actions"]
  stateIn["current_state.json<br/>previous operator state"]
  metaIn["operator_meta.json<br/>next sequence"]
  core["core.py<br/>F(S, Input)"]
  operator["sl_operator.py batch"]
  result["BatchResult<br/>sequence + hashes + applied actions"]
  stateOut["current_state.json<br/>new operator state"]
  metaOut["operator_meta.json<br/>sequence + 1"]
  payload["payload_hex<br/>canonical devnet bytes"]

  pending --> operator
  stateIn --> operator
  metaIn --> operator
  core --> operator
  operator --> result
  result --> stateOut
  result --> metaOut
  result --> payload
```

What this isolates: the operator proposes state by running the same transition
function that verifiers will later replay. The sequence number makes each batch
position explicit before it reaches EON.

### 3. Devnet Posting

```mermaid
flowchart LR
  payload["payload_hex<br/>canonical Payment SL bytes"]
  adapter["devnet_adapter.py<br/>length prefix + scalar framing"]
  scalars["EON Data scalars<br/>ordered scalar words"]
  submitter["eoncli / eon-sdk<br/>authorize transaction"]
  output["Data-bearing UTXO<br/>self-owned output"]
  devnet["EON devnet<br/>ordering + data availability"]

  payload --> adapter
  adapter --> scalars
  scalars --> submitter
  submitter --> output
  output --> devnet
```

What this isolates: EON receives opaque scalar `Data`. It orders and stores the
payload; it does not know token semantics or verify payment validity.

### 4. Verifier Sync From Devnet

```mermaid
flowchart LR
  verifier["Verifier / indexer"]
  devnet["EON devnet<br/>UTXO set"]
  data["Decoded UTXO Data<br/>scalar words"]
  adapter["devnet_adapter.py<br/>scalars -> payload bytes"]
  parser["core.parse_data_field_payload()<br/>sequence + hashes + actions"]
  prev["verifier_state/current_state.json<br/>previous accepted state"]
  replay["core.verify_batch()<br/>replay F(S, Input)"]
  accepted["verifier_state/current_state.json<br/>accepted new state"]
  log["verifier_state/verified_log.json<br/>accepted sequence log"]

  verifier --> devnet
  devnet --> data
  data --> adapter
  adapter --> parser
  parser --> replay
  prev --> replay
  replay --> accepted
  replay --> log
```

What this isolates: the verifier pulls ordered payloads from devnet, reconstructs
the envelope from its own previous accepted state, and accepts only if replayed
state matches the posted hash at the expected sequence.

### 5. Wallet Read Path

```mermaid
flowchart LR
  wallet["Wallet / client<br/>balance or history request"]
  localLabel["wallets/*.json<br/>address lookup"]
  verifier["Verifier service / local verifier"]
  state["verifier_state/current_state.json<br/>accepted balances"]
  log["verifier_state/verified_log.json<br/>accepted history"]
  response["Wallet view<br/>balance + state hash"]

  wallet --> localLabel
  wallet --> verifier
  verifier --> state
  verifier --> log
  state --> response
  log --> response
  response --> wallet
```

What this isolates: wallets should not depend on the operator's local state for
truth. The ideal production path is wallet-to-verifier reads, with the verifier
serving state it accepted from devnet-backed payloads.

### 6. Trust Boundaries

```mermaid
flowchart LR
  operator["Operator<br/>proposes batches"]
  eon["EON devnet<br/>orders + stores Data"]
  verifier["Verifier<br/>checks Payment SL validity"]
  wallet["Wallet<br/>consumes verified state"]

  operator -->|"payload + claimed hash"| eon
  eon -->|"ordered opaque Data"| verifier
  verifier -->|"accepted state + log"| wallet
  wallet -->|"new transfer intent"| operator
```

What this isolates: EON supplies shared canonicity, the Payment SL verifier
supplies validity, and wallets consume verifier-accepted state.

## Who Writes Where

| Path | Writer(s) | Readers |
| --- | --- | --- |
| `wallets/` | `wallet.py create` | all CLIs for name -> address lookup |
| `operator_state/sl_config.json` | `sl_operator.py init` | issuer, operator, verifier tooling |
| `operator_state/current_state.json` | `sl_operator.py batch` | wallet balance, nonce calculation, operator |
| `operator_state/operator_meta.json` | `sl_operator.py init`, `sl_operator.py batch` | operator |
| `operator_state/pending.json` | `issuer.py`, `wallet.py`, `sl_operator.py` clears | `sl_operator.py batch` |
| `verifier_state/current_state.json` | `verifier.py accept-envelope` | wallets, verifier status, clients |
| `verifier_state/verified_log.json` | `verifier.py accept-envelope` | wallets, explorers, clients |
| EON devnet UTXO `Data` | devnet adapter via `eoncli` / SDK | verifier / explorer / clients |

## Lifecycle Of One Action

```text
issuer.py mint --to alice --amount 10000
  -> load issuer_vk from operator_state/sl_config.json
  -> resolve alice through wallets/alice.json
  -> compute next nonce from current_state + pending queue length
  -> append action to operator_state/pending.json

sl_operator.py batch
  -> read pending actions
  -> read next batch sequence from operator_meta.json
  -> Action.from_dict(...)
  -> process_batch(state, actions, sequence)
  -> apply_action(state, action) for each input
  -> produce BatchResult
  -> save current_state.json
  -> clear pending.json
  -> increment operator_meta.json next_sequence
  -> print canonical sequence-numbered payload_hex

devnet adapter
  -> take BatchResult.data_field_payload()
  -> length-prefix and frame bytes into EON scalar Data
  -> construct a data-bearing self-owned output
  -> authorize and submit with eoncli / eon-sdk

verifier / indexer
  -> query EON UTXOs carrying Payment SL Data
  -> decode scalar Data back into payload bytes
  -> parse sequence, hashes, and actions from payload
  -> build an envelope using its current accepted previous state
  -> verify payload_hex matches decoded fields and expected sequence
  -> rerun F(S, Input)
  -> compare computed state hash to claimed new_state_hash
  -> persist accepted state and verified log

wallet.py balance
  -> ask/read the verifier-indexed state by default
  -> show balance at the latest verifier-accepted state
```

## Function Map

### `core.py`

```text
Identity
  hash_vk(vk)                       -> addr = SHA256(vk)[:40]

Types
  ActionType                        MINT | BURN | TRANSFER | FREEZE | UNFREEZE
  Action                            sender_vk, nonce, to?, from_addr?, amount?, target?
  State                             issuer_vk, balances, total_supply, nonce, frozen
  TransitionError                   raised by F on rejection

Transition
  apply_action(state, action)       F(S, Input) -> S'
  process_batch(state, actions, sequence)
  verify_batch(prev_state, actions, claimed_hash)
  BatchResult.data_field_payload()  SL_ID | version | sequence | prev | new | count | actions
  parse_data_field_payload(bytes)   decode devnet payload bytes into envelope fields

Persistence helpers
  load_sl_config()
  load_current_state() / save_current_state()
  load_operator_meta() / save_operator_meta()
  next_batch_sequence() / advance_batch_sequence()
  load_verified_state() / save_verified_state()
  load_verified_log() / save_verified_log()
  load_pending() / save_pending() / append_pending()
  next_nonce()
  load_wallet() / save_wallet() / resolve_address()
```

### `sl_operator.py`

```text
cmd_init      create local state directories; write config and genesis state
cmd_pending   show queued inputs
cmd_status    show current SL state
cmd_batch     run F over pending inputs; print canonical devnet payload_hex
cmd_reset     delete generated local state and wallet labels
```

### `devnet_adapter.py`

```text
payload_bytes_to_scalar_hex()
  length-prefix canonical payload bytes and split into current devnet scalar words
scalar_hex_to_payload_bytes()
  reassemble decoded EON Data scalar words into canonical payload bytes
envelope_from_scalars()
  parse payload bytes and combine them with verifier-held previous state
```

### `wallet.py`

```text
cmd_create    generate demo VK and local wallet label
cmd_address   print local wallet address
cmd_balance   read verifier-indexed balance by default
cmd_transfer  queue TRANSFER input
```

### `issuer.py`

```text
cmd_mint      queue MINT input
cmd_burn      queue BURN input
cmd_freeze    queue FREEZE input
cmd_unfreeze  queue UNFREEZE input
```

### `verifier.py`

```text
cmd_check_envelope
  read decoded payload envelope JSON
  check prev_state_hash against prev_state
  parse payload_hex and check canonical encoding
  verify_batch(prev_state, actions, new_state_hash)
cmd_accept_envelope
  verify decoded payload envelope
  require sequence == len(verified_log) + 1
  require prev_state_hash matches current verifier state after genesis
  write verifier_state/current_state.json
  append verifier_state/verified_log.json
cmd_status
  show latest verifier-indexed state
cmd_reset
  delete verifier-indexed state
```

## Trust Model

```text
Trusts:                         Does NOT trust:
-----------------------------   ------------------------------------
core.py's F()                   operator's new_state_hash claim
SL config issuer_vk             queued pending actions
EON devnet ordering + DA        EON devnet to verify payment logic
decoded devnet payload bytes    issuer/wallet labels as protocol identity
verifier-indexed state          operator API as wallet balance source
```

The operator is ecosystem-trusted, not protocol-trusted. It can propose a false
state hash, but verifiers reject the payload because re-executing `F` will not
produce the claimed `new_state_hash`.
