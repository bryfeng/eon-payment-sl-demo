# Payment SL Playground API

This API exposes one shared demo world around the Payment SL. It intentionally
does not use sessions. Everyone points at the same operator queue, operator
state, verifier state, and wallet/address registry.

The current API still uses JSON-file runtime state. SQLite is the next storage
step, but the endpoint shape below should survive that migration.

This API has no authentication or rate limiting. Treat it as an internal
playground surface until an auth layer and production storage are added.

## Running Locally

```bash
pip install -r requirements.txt
uvicorn api:app --reload --host 0.0.0.0 --port 8000
```

Open the generated OpenAPI UI at:

```text
http://localhost:8000/docs
```

## Model

The browser should generate or import a VK and derive:

```text
address = Hash(VK)
```

For this sandbox, transfer requests submit the raw VK so the server can check:

```text
Hash(vk) == from_address
```

That is intentionally not production custody. It is the temporary auth boundary
that later becomes a signature/proof without changing the payment flow.

## Core Endpoints

### Health And Config

```http
GET /health
GET /config
POST /reset
```

`POST /reset` clears the shared demo world: operator state, API wallet registry,
CLI wallet directory, and verifier state.

### Operator Init

```http
POST /operator/init
```

Request:

```json
{
  "issuer_vk": "circle_inc_verification_key",
  "reset_existing": false
}
```

This creates the genesis operator state and initializes batch sequence `1`.

### Wallet Registry

```http
POST /wallets
GET /wallets
GET /wallets/{address}
GET /balances/{address}
GET /balances/{address}?source=operator
```

Register by VK:

```json
{
  "label": "Alice",
  "vk": "alice_supplied_vk"
}
```

Register by address only:

```json
{
  "label": "Cold wallet",
  "address": "40_hex_chars"
}
```

The API stores label and address only. Browser clients should keep VK material
locally.

### Actions

```http
POST /actions/mint
POST /actions/burn
POST /actions/freeze
POST /actions/unfreeze
POST /actions/transfer
GET /pending
```

Mint:

```json
{
  "to_address": "40_hex_chars",
  "amount": 1000
}
```

Transfer:

```json
{
  "from_address": "40_hex_chars",
  "to_address": "40_hex_chars",
  "amount": 250,
  "vk": "sender_raw_vk_for_sandbox_auth"
}
```

Issuer actions use the issuer VK configured at `POST /operator/init`. Transfers
use the supplied raw VK and reject if it does not hash to `from_address`.

### Operator Batches

```http
GET /operator/state
POST /operator/batch
GET /operator/batches
GET /operator/latest-payload
```

`POST /operator/batch` consumes pending actions, runs `F(S, Input)`, advances
operator state, and returns:

```json
{
  "batched": true,
  "batch": {
    "sequence": 1,
    "prev_state_hash": "hex",
    "new_state_hash": "hex",
    "actions_applied": [],
    "payload_hex": "hex",
    "data_scalars": ["0x..."]
  }
}
```

The returned `data_scalars` use the same length-prefixed framing as
`devnet_adapter.py`.

### Verifier

```http
POST /verifier/accept-latest-batch
POST /verifier/accept-envelope
POST /verifier/envelope-from-payload
GET /verifier/state
GET /verifier/log
```

`POST /verifier/accept-latest-batch` is the fastest sandbox path. It takes the
latest operator batch, builds the decoded envelope, replays Payment SL rules,
checks sequence continuity, and writes verifier-indexed state.

`POST /verifier/accept-envelope` accepts the explicit decoded envelope shape:

```json
{
  "prev_state": {},
  "sequence": 1,
  "prev_state_hash": "hex",
  "new_state_hash": "hex",
  "actions_applied": [],
  "payload_hex": "hex"
}
```

### Devnet Boundary

```http
POST /devnet/encode-payload
```

Request:

```json
{
  "payload_hex": "hex"
}
```

This returns the scalar words that would be carried in EON UTXO `Data`. Live
submission and UTXO sync are intentionally left behind the devnet boundary for
the next integration pass.

## Suggested Demo Flow

1. `POST /operator/init`
2. `POST /wallets` for Alice and Bob
3. `POST /actions/mint` to Alice
4. `POST /operator/batch`
5. `POST /verifier/accept-latest-batch`
6. `GET /balances/{alice}`
7. `POST /actions/transfer` from Alice to Bob
8. `POST /operator/batch`
9. `POST /verifier/accept-latest-batch`
10. `GET /balances/{alice}` and `GET /balances/{bob}`

This gives a team member the full hands-on process: intent, operator batch,
devnet-ready payload, verifier acceptance, and verified wallet state.
