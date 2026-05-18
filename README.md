# EON Payment Token — Multi-Actor SL Demo

A Python CLI demo of a Payment Token semantic layer (SL) running on EON
Protocol. It models a centralized-issuer stablecoin (USDC model) with four
distinct actors and a devnet-oriented data-availability boundary.

    State Machine:  S_{i+1} = F(S_i, Input_i)
    Prf3 Strategy:  Path (a) — post raw inputs + state hashes, verifiers re-execute
    Base Layer:     EON devnet UTXO Data payload

The intended internal demo uses EON devnet as the data availability layer. The
operator prepares a canonical batch payload, then a devnet adapter should encode
that payload into EON scalars and submit it with `eoncli` / the SDK.

## Actors

| Actor | Script | Role |
| --- | --- | --- |
| Issuer | `issuer.py` | Authority VK. Queues mint / burn / freeze / unfreeze actions. |
| Wallet | `wallet.py` | End user. Creates local labels, queues transfers, reads balances from verifier-indexed state. |
| Operator | `sl_operator.py` | Runs F(), batches pending actions, prepares sequence-numbered devnet payloads. |
| Devnet adapter | `devnet_adapter.py` | Frames payload bytes into EON scalars and turns decoded UTXO data into verifier envelopes. |
| Verifier | `verifier.py` | Re-executes decoded payload envelopes, accepts valid state, serves wallet reads. |
| EON devnet | `eoncli` / SDK | Orders transactions and stores retrievable UTXO `Data`. |

## Layout

```text
payment_sl/
├── api.py            # FastAPI playground API around the shared demo world
├── storage.py        # SQLite persistence for the hosted API
├── core.py           # state machine (F), actions, payload serialization
├── issuer.py         # issuer CLI
├── wallet.py         # wallet CLI
├── sl_operator.py    # operator CLI
├── devnet_adapter.py # payload <-> scalar framing and verifier envelopes
├── verifier.py       # verifier CLI for decoded devnet payload envelopes
├── wallets/          # generated local wallet labels
├── operator_state/   # generated local state + pending queue
├── verifier_state/   # generated verifier-indexed accepted state
├── API.md            # playground API contract
├── railway.json      # Railway start command + health check
└── README.md
```

## Design Invariants

1. **The payment SL owns validity.** EON orders and stores posted `Data`; it does not execute payment-token rules.
2. **The operator posts canonical batches.** Issuer and wallets only queue actions.
3. **Nonces are automatic.** Each CLI computes the next nonce from current state plus pending queue length.
4. **Batches are sequence-numbered.** Verifiers accept payloads in monotonic order.
5. **Wallets read verified state.** Wallet balances come from verifier-indexed state, not the operator's local state.
6. **Wallet names are local labels.** The identity used by the SL is the address `Hash(VK)`.
7. **EON `Data` is scalar-oriented.** The devnet adapter frames/chunks this demo's payload bytes into EON scalars.

## Architecture

```mermaid
flowchart LR
  issuer["Issuer CLI / issuer UI<br/>mint, burn, freeze"]
  wallet["Wallet CLI / browser wallet<br/>create address, transfer"]
  walletRead["Wallet read path<br/>balance / history"]
  pending["Pending action queue<br/>operator_state/pending.json"]
  operator["SL operator<br/>runs F(S, Input), batches actions"]
  adapter["devnet_adapter.py<br/>payload bytes <-> scalar Data"]
  eoncli["eoncli / eon-sdk<br/>authorize + submit transaction"]
  devnet["EON devnet<br/>https://eon.zk524.com<br/>UTXO Data availability"]
  verifier["Verifier / indexer<br/>pulls UTXO Data, replays F, stores accepted state"]
  verifiedState["Verifier state log<br/>verifier_state/current_state.json"]

  issuer --> pending
  wallet --> pending
  wallet <--> walletRead
  pending --> operator
  operator --> adapter
  adapter --> eoncli
  eoncli --> devnet
  verifier --> devnet
  devnet --> verifier
  verifier --> verifiedState
  walletRead <--> verifier
  verifiedState --> verifier
```

The operator prepares this canonical payload:

```text
[SL_ID][version][sequence][prev_state_hash][new_state_hash][batch_count][actions...]
```

The devnet adapter should:

1. Take `BatchResult.data_field_payload()`.
2. Frame/chunk the bytes into EON scalar words using this SL's length-prefixed framing.
3. Build a self-owned data-bearing output whose amount covers `price * data_len`.
4. Authorize and submit the transaction with `eoncli` / `eon-sdk`.
5. Let verifiers fetch the resulting UTXO, decode scalar `Data` back into payload bytes, replay the SL, and update their accepted state log.
6. Let wallets read balances and history from verifier-indexed state.

Useful `eoncli` commands around the devnet integration:

```bash
export EON_API_HTTP_URL=https://eon.zk524.com

eoncli create-normal-account operator.pk
eoncli get-address operator.pk
eoncli get-balance <operator-address>
eoncli list-utxo <operator-address>
eoncli get-vk operator.pk
```

The hosted API can submit the latest batch to live devnet when a submitter
command is configured:

```bash
export EON_DEVNET_API_URL=https://eon.zk524.com
export EON_DEVNET_SUBMIT_CMD="/path/to/eon-devnet-submit"
export EON_KEY_ENCRYPTION_SECRET="long random deployment secret"
```

`POST /devnet/submit-latest-batch` sends the latest `payload_hex` and
`data_scalars` to that command over stdin. The command signs and submits the
data-bearing transaction, then returns JSON with at least `tx_hash`. Operator
EON account JSON is stored encrypted in SQLite and bound to semantic-layer
registry records, so each operator can post with its own base-layer account.
Without `EON_DEVNET_SUBMIT_CMD` and a bound base-layer account, the API reports
devnet submission as unconfigured instead of pretending that local scalar
encoding wrote to the base layer.

`POST /base-layer/accounts/generate` provisions a base-layer posting account for
an SL operator and stores the signing material encrypted for later submission.

For local workspace testing with the sibling `eon-sdk` checkout, the command can
point at the generic submitter example:

```bash
export EON_OPERATOR_WALLET_FILE=/path/to/operator_key.json
export EON_DEVNET_SUBMIT_CMD="cargo run --quiet --manifest-path /path/to/eon-sdk/Cargo.toml --example post_payment_sl_payload"
```

`EON_OPERATOR_WALLET_FILE` is still supported as a local fallback. The hosted
workbench path should use `POST /base-layer/accounts/generate` and
`POST /semantic-layers` instead, letting the API decrypt the selected account to
a temporary file only for the submitter subprocess.

## Playground API

The repo also includes a shared-world FastAPI wrapper for a hosted sandbox:

```bash
pip install -r requirements.txt
uvicorn api:app --reload --host 0.0.0.0 --port 8000
```

Then open:

```text
http://localhost:8000/docs
```

The root URL returns a short liveness message. Use `/health` for the
machine-readable health check and `/docs` for the OpenAPI UI.

The API keeps one shared demo world in SQLite rather than per-user sessions.
Browser clients can create or import a VK, derive `address = Hash(VK)`,
register the label/address with the API, and submit sandbox transfers with the
raw VK. The raw VK check is temporary demo auth and should later be replaced
with signatures or proofs.

Runtime state defaults to `./data/payment_sl.sqlite`. For Railway, attach a
volume at `/app/data`; the app will store the database there. Keep the Railway
service at one replica while using SQLite.

See [`API.md`](API.md) for endpoint details and the suggested demo flow.

## CLI Walkthrough

Run everything from inside the `payment_sl/` directory.

### Setup

```bash
python sl_operator.py init --issuer-vk "circle_inc_verification_key"
python wallet.py create --name alice
python wallet.py create --name bob
python wallet.py create --name charlie
```

Addresses are deterministic from each generated VK. Wallet names are only local
labels used by the demo CLIs.

### Issuance

```bash
python issuer.py mint --to alice --amount 10000
python issuer.py mint --to bob --amount 5000
python sl_operator.py pending
python sl_operator.py batch
python sl_operator.py status
```

`sl_operator.py batch` applies valid actions, advances local SL state, clears
the pending queue, advances `operator_state/operator_meta.json`, and prints the
canonical payload hex that should be posted to EON devnet by the adapter.

### Payments

```bash
python wallet.py transfer --name alice --to bob --amount 3000
python issuer.py mint --to charlie --amount 2000
python sl_operator.py batch
python wallet.py balance --name alice --source operator
python wallet.py balance --name bob --source operator
```

Actions from different actors are naturally multiplexed by the operator into
one batch.

### Compliance

```bash
python issuer.py freeze --target charlie
python wallet.py transfer --name charlie --to alice --amount 1000
python sl_operator.py batch
python sl_operator.py status
```

The freeze applies. The transfer is rejected by F() because Charlie is frozen.
Rejected actions do not advance the SL nonce.

### Redemption

```bash
python issuer.py burn --from bob --amount 2000
python sl_operator.py batch
python sl_operator.py status
```

Only the issuer VK registered at `sl_operator.py init` can mint, burn, freeze,
or unfreeze.

## Verification

`verifier.py` verifies decoded devnet payload envelopes and can persist accepted
state into `verifier_state/`. A devnet adapter should fetch the EON UTXO, decode
its scalar `Data` back into the canonical payload, and provide the previous
state plus decoded actions:

```json
{
  "prev_state": { "...": "State.to_dict() output" },
  "sequence": 1,
  "prev_state_hash": "hex",
  "new_state_hash": "hex",
  "actions_applied": [],
  "payload_hex": "hex"
}
```

The adapter can also frame payloads for submission and rebuild envelopes after a
verifier has decoded UTXO `Data` scalars:

```bash
python devnet_adapter.py encode-payload --payload-hex PAYLOAD_HEX
python devnet_adapter.py envelope-from-scalars \
  --scalar SCALAR_WORD_0 \
  --scalar SCALAR_WORD_1 \
  --prev-state-file previous-state.json \
  --out payload-envelope.json
```

Then run:

```bash
python verifier.py check-envelope --file payload-envelope.json
python verifier.py accept-envelope --file payload-envelope.json
python verifier.py status
```

The verifier checks that `payload_hex` matches the decoded envelope fields,
replays `F(S, Input)`, and compares the computed state hash to
`new_state_hash`. `accept-envelope` writes the latest accepted state to
`verifier_state/current_state.json` and appends to
`verifier_state/verified_log.json`.

Wallets read balances from verifier-indexed state by default:

```bash
python wallet.py balance --name alice
```

For local operator debugging only, a wallet can still read the operator's
unverified state:

```bash
python wallet.py balance --name alice --source operator
```

## Starting Over

```bash
python sl_operator.py reset
python verifier.py reset
```

Deletes generated operator/wallet state and verifier-indexed state.

## What This Demo Shows

EON gives the Payment SL canonical ordering and retrievable opaque data. The
base layer does not know token balances, issuer authority, freezes, burns, or
transfer validity. Those rules live in `core.py` and are enforced by operators
and verifiers.

The architectural point is:

> Canonicity is shared. Validity is sovereign.
