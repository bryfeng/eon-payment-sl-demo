# Payment SL Playground API

This API exposes one shared demo world around the Payment SL. It intentionally
does not use sessions. Everyone points at the same operator queue, operator
state, verifier state, and wallet/address registry.

The API stores runtime state in SQLite. Locally it defaults to
`./data/payment_sl.sqlite`. On Railway it will use
`RAILWAY_VOLUME_MOUNT_PATH/payment_sl.sqlite` when a volume is attached, or
`PAYMENT_SL_DB_PATH` if you set that variable explicitly.

This API has no authentication or rate limiting. Treat it as an internal
playground surface until an auth layer and production-grade storage are added.

## Running Locally

```bash
pip install -r requirements.txt
uvicorn api:app --reload --host 0.0.0.0 --port 8000
```

Open the generated OpenAPI UI at:

```text
http://localhost:8000/docs
```

The deployed internal sandbox is currently:

```text
https://eon-payment-sl-demo-production.up.railway.app
```

## Railway Hosting

The repo includes `railway.json` with a Uvicorn start command and `/health`
deployment health check.

For a persistent demo world:

1. Create one Railway web service from this repo.
2. Attach one Railway Volume to the service.
3. Mount the volume at `/app/data`.
4. Keep the service at one replica while using SQLite.

Optional explicit variable:

```text
PAYMENT_SL_DB_PATH=/app/data/payment_sl.sqlite
```

SQLite is a good fit for this internal sandbox as long as one API instance owns
the file. Move to Postgres, Turso/libSQL, or another networked database before
horizontal scaling.

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

The fenced endpoint inventory below is tested against FastAPI's generated
OpenAPI schema. If an endpoint is added, removed, or renamed, this block should
change in the same commit.

```api-endpoints
GET /
GET /health
GET /config
POST /reset
POST /operator/init
GET /operator/state
POST /wallets
GET /wallets
GET /wallets/{address}
GET /balances/{address}
POST /actions/mint
POST /actions/burn
POST /actions/freeze
POST /actions/unfreeze
POST /actions/transfer
GET /pending
POST /operator/batch
GET /operator/batches
GET /operator/latest-payload
POST /devnet/encode-payload
GET /verifier/state
GET /verifier/log
GET /verifier/events
POST /verifier/accept-latest-batch
POST /verifier/accept-envelope
POST /verifier/envelope-from-payload
POST /verifier/ingest-event
```

### Health And Config

```http
GET /
GET /health
GET /config
POST /reset
```

`GET /` returns a short liveness message for quick browser checks of the public
Railway URL. `GET /health` remains the machine-readable health check.

`POST /reset` clears the shared demo world in SQLite: operator state, pending
actions, operator batches, wallet registry, verifier state, and verifier log.

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
GET /verifier/events
POST /verifier/ingest-event
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

`POST /verifier/ingest-event` accepts a normalized EON data-output event. It is
the verifier/indexer boundary used before live block polling is wired in:

```json
{
  "cursor": "devnet:1:0:0",
  "network_id": "devnet",
  "height": 1,
  "tx_hash": "0x...",
  "tx_index": 0,
  "output_index": 0,
  "data_scalars": ["0x..."]
}
```

`GET /verifier/events` returns the stored normalized base events. `GET
/verifier/state?sl_id=00010001` and `GET /verifier/log?sl_id=00010001` expose
the plugin-indexed verified state and log.

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

The executable smoke flow below is also tested. Variables such as
`$alice.address` are resolved from earlier named responses.

```api-smoke-test
[
  {
    "name": "reset",
    "method": "POST",
    "path": "/reset"
  },
  {
    "name": "init",
    "method": "POST",
    "path": "/operator/init",
    "body": {
      "issuer_vk": "issuer_vk"
    }
  },
  {
    "name": "alice",
    "method": "POST",
    "path": "/wallets",
    "body": {
      "label": "Alice",
      "vk": "alice_vk"
    },
    "expect": {
      "derived_from_vk": true
    }
  },
  {
    "name": "bob",
    "method": "POST",
    "path": "/wallets",
    "body": {
      "label": "Bob",
      "vk": "bob_vk"
    },
    "expect": {
      "derived_from_vk": true
    }
  },
  {
    "name": "mint",
    "method": "POST",
    "path": "/actions/mint",
    "body": {
      "to_address": "$alice.address",
      "amount": 100
    },
    "expect": {
      "pending_count": 1
    }
  },
  {
    "name": "batch_1",
    "method": "POST",
    "path": "/operator/batch",
    "expect": {
      "batched": true
    }
  },
  {
    "name": "accept_1",
    "method": "POST",
    "path": "/verifier/accept-latest-batch",
    "expect": {
      "accepted": true,
      "sequence": 1
    }
  },
  {
    "name": "transfer",
    "method": "POST",
    "path": "/actions/transfer",
    "body": {
      "from_address": "$alice.address",
      "to_address": "$bob.address",
      "amount": 40,
      "vk": "alice_vk"
    },
    "expect": {
      "pending_count": 1
    }
  },
  {
    "name": "batch_2",
    "method": "POST",
    "path": "/operator/batch",
    "expect": {
      "batched": true
    }
  },
  {
    "name": "accept_2",
    "method": "POST",
    "path": "/verifier/accept-latest-batch",
    "expect": {
      "accepted": true,
      "sequence": 2
    }
  },
  {
    "name": "alice_balance",
    "method": "GET",
    "path": "/balances/$alice.address",
    "expect": {
      "balance": 60,
      "source": "verifier"
    }
  },
  {
    "name": "bob_balance",
    "method": "GET",
    "path": "/balances/$bob.address",
    "expect": {
      "balance": 40,
      "source": "verifier"
    }
  }
]
```
