"""
api.py - Shared playground API for the Payment SL demo.

This wraps the existing state machine, operator, devnet adapter, and verifier
logic with SQLite-backed runtime storage, so this is one shared demo world that
can be hosted on a persistent Railway volume.
"""

from threading import RLock
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from account_vault import (
    AccountVaultError,
    decrypt_account_json,
    encrypt_account_json,
    vault_configured,
)
import core
from devnet_adapter import envelope_from_payload_hex
from devnet_submitter import (
    DevnetSubmitError,
    devnet_status as command_devnet_status,
    submit_batch_to_devnet,
)
from payment_plugin import PAYMENT_PLUGIN
from storage import DEFAULT_DB_PATH, SQLiteStorage
from verifier_engine import PluginRegistry, VerifierEngine, VerifierStore
from verifier_engine.eon_data import ScalarFramingError, payload_bytes_to_scalar_hex


STATE_LOCK = RLock()
STORE = SQLiteStorage(DEFAULT_DB_PATH)


app = FastAPI(
    title="EON Payment SL Playground API",
    version="0.1.0",
    description="Shared-world API for experimenting with the Payment SL demo.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class InitRequest(BaseModel):
    issuer_vk: str = Field(default="circle_inc_verification_key", min_length=1)
    reset_existing: bool = False


class WalletRequest(BaseModel):
    label: Optional[str] = None
    vk: Optional[str] = None
    address: Optional[str] = None
    kind: Literal["user", "sl_operator", "coordinator", "verifier"] = "user"


class SemanticLayerRequest(BaseModel):
    name: str = Field(min_length=1)
    sl_id: str = Field(default=core.SL_ID.hex(), min_length=1)
    version: str = Field(default=core.VERSION.hex(), min_length=1)
    operator_wallet_address: str
    base_layer_account_id: Optional[str] = None
    issuer_vk_ref: Optional[str] = None
    operator_vk_ref: Optional[str] = None


class BaseLayerAccountRequest(BaseModel):
    label: str = Field(min_length=1)
    owner_wallet_address: str
    eon_address: Optional[str] = None
    account_json: dict[str, Any]


class AmountToRequest(BaseModel):
    to_address: str
    amount: int = Field(gt=0)


class AmountFromRequest(BaseModel):
    from_address: str
    amount: int = Field(gt=0)


class TargetRequest(BaseModel):
    target_address: str


class TransferRequest(BaseModel):
    from_address: str
    to_address: str
    amount: int = Field(gt=0)
    vk: str = Field(min_length=1)


class PayloadRequest(BaseModel):
    payload_hex: str


class DevnetSubmitRequest(BaseModel):
    force: bool = False


class BaseEventRequest(BaseModel):
    cursor: str
    network_id: str = "devnet"
    height: int = Field(ge=0)
    block_hash: Optional[str] = None
    tx_hash: str
    tx_index: int = Field(ge=0)
    output_index: int = Field(ge=0)
    utxo_id: Optional[str] = None
    owner: Optional[str] = None
    amount: str = "0"
    data_scalars: list[str]
    event_key: Optional[str] = None


def configure_storage(root: Optional[Path] = None, db_path: Optional[Path] = None) -> None:
    """Point the hosted API at a different SQLite database."""
    if db_path is None:
        db_path = DEFAULT_DB_PATH if root is None else Path(root) / "payment_sl.sqlite"
    STORE.configure(Path(db_path))


def _require_initialized() -> None:
    if not _initialized():
        raise HTTPException(
            status_code=409,
            detail="SL is not initialized. Call POST /operator/init first.",
        )


def _initialized() -> bool:
    return STORE.is_initialized()


def _validate_address(address: str) -> str:
    addr = address.strip().lower()
    if len(addr) != 40:
        raise HTTPException(status_code=400, detail="address must be 40 hex chars")
    try:
        int(addr, 16)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="address must be hex") from e
    return addr


def _validate_eon_address(address: str) -> str:
    value = address.strip().lower()
    raw = value[2:] if value.startswith("0x") else value
    if not raw:
        raise HTTPException(status_code=400, detail="eon_address is required")
    try:
        int(raw, 16)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="eon_address must be hex") from e
    return f"0x{raw}"


def _account_json_address(account_json: dict[str, Any]) -> Optional[str]:
    address = account_json.get("address")
    if not isinstance(address, str) or not address.strip():
        return None
    return _validate_eon_address(address)


def _public_base_layer_account(record: dict) -> dict:
    return {
        "id": record["id"],
        "owner_wallet_address": record["owner_wallet_address"],
        "label": record["label"],
        "eon_address": record["eon_address"],
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


def _state_to_response(state: core.State) -> dict:
    return {
        "issuer_vk": state.issuer_vk,
        "balances": dict(sorted(state.balances.items())),
        "total_supply": state.total_supply,
        "nonce": state.nonce,
        "frozen": sorted(state.frozen),
        "state_hash": state.state_hash(),
    }


def _operator_state() -> core.State:
    _require_initialized()
    state = STORE.load_operator_state()
    if state is None:
        raise HTTPException(status_code=409, detail="operator state is missing")
    return state


def _verified_state() -> core.State:
    checkpoint = _verifier_store().load_checkpoint(
        PAYMENT_PLUGIN.sl_id,
        core.VERSION,
    )
    if checkpoint is not None:
        return PAYMENT_PLUGIN.state_from_dict(checkpoint["state"])

    legacy_state = STORE.load_verified_state()
    if legacy_state is not None:
        return legacy_state

    raise HTTPException(status_code=404, detail="no verifier state found")


def _verifier_store() -> VerifierStore:
    return VerifierStore(STORE.db_path)


def _verifier_engine() -> VerifierEngine:
    config = {}
    if _initialized():
        config[PAYMENT_PLUGIN.sl_id.hex()] = {"issuer_vk": _issuer_vk()}
    return VerifierEngine(
        store=_verifier_store(),
        registry=PluginRegistry([PAYMENT_PLUGIN]),
        plugin_config=config,
    )


def _sl_id_bytes(sl_id: str) -> bytes:
    value = sl_id.strip().lower()
    if value.startswith("0x"):
        value = value[2:]
    try:
        raw = bytes.fromhex(value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="sl_id must be hex") from e
    if len(raw) != 4:
        raise HTTPException(status_code=400, detail="sl_id must be 4 bytes")
    return raw


def _version_hex(version: str) -> str:
    value = version.strip().lower()
    if value.startswith("0x"):
        value = value[2:]
    try:
        raw = bytes.fromhex(value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="version must be hex") from e
    if len(raw) != 2:
        raise HTTPException(status_code=400, detail="version must be 2 bytes")
    return raw.hex()


def _model_to_dict(model: BaseModel) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)


def _queue_action(action: dict) -> dict:
    STORE.append_pending(action)
    return {
        "queued": True,
        "action": action,
        "pending_count": STORE.pending_count(),
    }


def _issuer_vk() -> str:
    _require_initialized()
    return STORE.load_sl_config()["issuer_vk"]


def _next_nonce() -> int:
    _require_initialized()
    return STORE.next_nonce()


def _latest_batch() -> dict:
    batch = STORE.latest_batch()
    if batch is None:
        raise HTTPException(status_code=404, detail="no operator batch found")
    return batch


def _active_semantic_layer_record() -> Optional[dict]:
    sl_id = core.SL_ID.hex()
    config = STORE.load_sl_config()
    if config and config.get("sl_id"):
        sl_id = str(config["sl_id"])
    return STORE.get_semantic_layer(sl_id)


def _devnet_runtime_status() -> dict:
    status = command_devnet_status()
    active_record = _active_semantic_layer_record()
    active_account_id = active_record.get("base_layer_account_id") if active_record else None
    vault_ready = vault_configured()
    account_ready = bool(status.get("wallet_file_configured") or (active_account_id and vault_ready))
    submitter_ready = bool(status.get("submitter_configured", status.get("enabled")))

    status.update(
        {
            "account_vault_configured": vault_ready,
            "base_layer_account_count": STORE.base_layer_account_count(),
            "active_semantic_layer_id": active_record.get("sl_id") if active_record else None,
            "active_base_layer_account_id": active_account_id,
            "account_configured": account_ready,
            "ready": submitter_ready and account_ready,
            "enabled": submitter_ready and account_ready,
        }
    )
    return status


def _submission_account_json() -> Optional[dict[str, Any]]:
    active_record = _active_semantic_layer_record()
    if not active_record:
        return None

    account_id = active_record.get("base_layer_account_id")
    if not account_id:
        return None

    account_record = STORE.get_base_layer_account(account_id, include_secret=True)
    if not account_record:
        raise HTTPException(
            status_code=409,
            detail="active semantic layer references a missing base-layer account",
        )

    try:
        return decrypt_account_json(account_record["encrypted_account_json"])
    except AccountVaultError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


def _accept_envelope(envelope: dict) -> tuple[bool, str]:
    result = _verifier_engine().accept_envelope(PAYMENT_PLUGIN, envelope)
    if result["accepted"]:
        return True, "accepted"
    return False, result["message"]


@app.get("/health")
def health() -> dict:
    return {"ok": True, "initialized": _initialized()}


@app.get("/")
def root() -> dict:
    return {
        "message": "EON Payment SL Playground API is live.",
        "health": "/health",
        "docs": "/docs",
    }


@app.get("/config")
def config() -> dict:
    response = {
        "initialized": _initialized(),
        "sl_id": core.SL_ID.hex(),
        "version": core.VERSION.hex(),
        "storage": {
            "type": "sqlite",
            "db_path": str(STORE.db_path),
        },
    }
    if _initialized():
        response["issuer_vk"] = STORE.load_sl_config()["issuer_vk"]
        response["operator_state_hash"] = _operator_state().state_hash()
        response["next_batch_sequence"] = STORE.next_batch_sequence()
    return response


@app.post("/reset")
def reset() -> dict:
    with STATE_LOCK:
        STORE.reset()
        _verifier_store().reset()
        return {
            "reset": True,
            "storage": {
                "type": "sqlite",
                "db_path": str(STORE.db_path),
            },
        }


@app.post("/operator/init")
def operator_init(request: InitRequest) -> dict:
    with STATE_LOCK:
        if _initialized():
            if not request.reset_existing:
                raise HTTPException(
                    status_code=409,
                    detail="SL is already initialized. Use reset_existing=true or POST /reset.",
                )
            reset()

        config_obj = {
            "issuer_vk": request.issuer_vk,
            "sl_id": core.SL_ID.hex(),
            "version": core.VERSION.hex(),
        }
        genesis = STORE.initialize(request.issuer_vk)
        _verifier_store().reset()

        return {
            "initialized": True,
            "config": config_obj,
            "operator_state": _state_to_response(genesis),
        }


@app.get("/operator/state")
def operator_state() -> dict:
    state = _operator_state()
    return {
        "state": _state_to_response(state),
        "pending_count": STORE.pending_count(),
        "next_batch_sequence": STORE.next_batch_sequence(),
    }


@app.post("/wallets")
def register_wallet(request: WalletRequest) -> dict:
    with STATE_LOCK:
        vk = request.vk.strip() if request.vk else None
        address = request.address.strip().lower() if request.address else None

        if not vk and not address:
            raise HTTPException(status_code=400, detail="provide vk or address")

        derived_address = core.hash_vk(vk) if vk else None
        if address:
            address = _validate_address(address)
        else:
            address = derived_address

        if derived_address and address != derived_address:
            raise HTTPException(status_code=400, detail="vk does not match address")

        label = (request.label or address[:8]).strip()
        STORE.upsert_wallet(label, address, request.kind)

        return {
            "label": label,
            "address": address,
            "kind": request.kind,
            "derived_from_vk": bool(vk),
        }


@app.get("/wallets")
def list_wallets() -> dict:
    return {"wallets": STORE.list_wallets()}


@app.get("/wallets/{address}")
def get_wallet(address: str) -> dict:
    addr = _validate_address(address)
    wallet = STORE.get_wallet(addr)
    if not wallet:
        raise HTTPException(status_code=404, detail="wallet is not registered")
    return wallet


@app.post("/base-layer/accounts")
def register_base_layer_account(request: BaseLayerAccountRequest) -> dict:
    with STATE_LOCK:
        owner_address = _validate_address(request.owner_wallet_address)
        owner_wallet = STORE.get_wallet(owner_address)
        if not owner_wallet:
            raise HTTPException(status_code=400, detail="owner wallet is not registered")

        json_address = _account_json_address(request.account_json)
        requested_address = (
            _validate_eon_address(request.eon_address)
            if request.eon_address
            else json_address
        )
        if not requested_address:
            raise HTTPException(
                status_code=400,
                detail="provide eon_address or account_json.address",
            )
        if json_address and requested_address != json_address:
            raise HTTPException(
                status_code=400,
                detail="eon_address does not match account_json.address",
            )

        try:
            encrypted_account_json = encrypt_account_json(request.account_json)
        except AccountVaultError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

        record = {
            "id": f"acct_{uuid4().hex[:12]}",
            "owner_wallet_address": owner_address,
            "label": request.label.strip(),
            "eon_address": requested_address,
            "encrypted_account_json": encrypted_account_json,
        }
        created = STORE.create_base_layer_account(record)
        return _public_base_layer_account(created)


@app.get("/base-layer/accounts")
def list_base_layer_accounts() -> dict:
    return {
        "accounts": [
            _public_base_layer_account(record)
            for record in STORE.list_base_layer_accounts()
        ]
    }


@app.post("/semantic-layers")
def register_semantic_layer(request: SemanticLayerRequest) -> dict:
    with STATE_LOCK:
        sl_id = _sl_id_bytes(request.sl_id).hex()
        version = _version_hex(request.version)
        operator_address = _validate_address(request.operator_wallet_address)
        operator_wallet = STORE.get_wallet(operator_address)
        if not operator_wallet:
            raise HTTPException(
                status_code=400,
                detail="operator wallet is not registered",
            )
        if operator_wallet.get("kind") != "sl_operator":
            raise HTTPException(
                status_code=400,
                detail="operator wallet must use kind=sl_operator",
            )

        base_layer_account_id = (
            request.base_layer_account_id.strip()
            if request.base_layer_account_id
            else None
        )
        if base_layer_account_id:
            base_layer_account = STORE.get_base_layer_account(base_layer_account_id)
            if not base_layer_account:
                raise HTTPException(
                    status_code=400,
                    detail="base-layer account is not registered",
                )
            if base_layer_account["owner_wallet_address"] != operator_address:
                raise HTTPException(
                    status_code=400,
                    detail="base-layer account must belong to operator wallet",
                )

        record = {
            "name": request.name.strip(),
            "sl_id": sl_id,
            "version": version,
            "operator_wallet_address": operator_address,
            "base_layer_account_id": base_layer_account_id,
            "issuer_vk_ref": request.issuer_vk_ref,
            "operator_vk_ref": request.operator_vk_ref,
        }
        STORE.upsert_semantic_layer(record)
        return record


@app.get("/semantic-layers")
def list_semantic_layers() -> dict:
    return {"semantic_layers": STORE.list_semantic_layers()}


@app.get("/balances/{address}")
def get_balance(
    address: str,
    source: str = Query(default="verifier", pattern="^(verifier|operator)$"),
) -> dict:
    addr = _validate_address(address)
    state = _verified_state() if source == "verifier" else _operator_state()
    return {
        "address": addr,
        "balance": state.get_balance(addr),
        "frozen": addr in state.frozen,
        "source": source,
        "state_hash": state.state_hash(),
    }


@app.post("/actions/mint")
def mint(request: AmountToRequest) -> dict:
    with STATE_LOCK:
        to_address = _validate_address(request.to_address)
        action = {
            "type": core.ActionType.MINT.value,
            "sender_vk": _issuer_vk(),
            "nonce": _next_nonce(),
            "to": to_address,
            "amount": request.amount,
        }
        return _queue_action(action)


@app.post("/actions/burn")
def burn(request: AmountFromRequest) -> dict:
    with STATE_LOCK:
        from_address = _validate_address(request.from_address)
        action = {
            "type": core.ActionType.BURN.value,
            "sender_vk": _issuer_vk(),
            "nonce": _next_nonce(),
            "from_addr": from_address,
            "amount": request.amount,
        }
        return _queue_action(action)


@app.post("/actions/freeze")
def freeze(request: TargetRequest) -> dict:
    with STATE_LOCK:
        target_address = _validate_address(request.target_address)
        action = {
            "type": core.ActionType.FREEZE.value,
            "sender_vk": _issuer_vk(),
            "nonce": _next_nonce(),
            "target": target_address,
        }
        return _queue_action(action)


@app.post("/actions/unfreeze")
def unfreeze(request: TargetRequest) -> dict:
    with STATE_LOCK:
        target_address = _validate_address(request.target_address)
        action = {
            "type": core.ActionType.UNFREEZE.value,
            "sender_vk": _issuer_vk(),
            "nonce": _next_nonce(),
            "target": target_address,
        }
        return _queue_action(action)


@app.post("/actions/transfer")
def transfer(request: TransferRequest) -> dict:
    with STATE_LOCK:
        from_address = _validate_address(request.from_address)
        to_address = _validate_address(request.to_address)
        if core.hash_vk(request.vk) != from_address:
            raise HTTPException(status_code=400, detail="vk does not match from_address")

        action = {
            "type": core.ActionType.TRANSFER.value,
            "sender_vk": request.vk,
            "nonce": _next_nonce(),
            "from_addr": from_address,
            "to": to_address,
            "amount": request.amount,
        }
        return _queue_action(action)


@app.get("/pending")
def pending() -> dict:
    _require_initialized()
    return {"pending": STORE.load_pending()}


@app.post("/operator/batch")
def operator_batch() -> dict:
    with STATE_LOCK:
        state = _operator_state()
        pending_actions = STORE.load_pending()
        if not pending_actions:
            return {"batched": False, "message": "No pending actions. Nothing to batch."}

        sequence = STORE.next_batch_sequence()
        actions = [core.Action.from_dict(d) for d in pending_actions]
        new_state, result = core.process_batch(state, actions, sequence=sequence)
        payload = result.data_field_payload()
        payload_hex = payload.hex()
        data_scalars = payload_bytes_to_scalar_hex(payload)

        rejected_index = {idx: err for idx, err in result.rejected}
        rejected = [
            {
                "index": idx,
                "error": err,
                "action": pending_actions[idx],
            }
            for idx, err in result.rejected
        ]
        submitted = [
            {
                "index": idx,
                "status": "rejected" if idx in rejected_index else "applied",
                "action": action,
                "error": rejected_index.get(idx),
            }
            for idx, action in enumerate(pending_actions)
        ]
        record = {
            "sequence": result.sequence,
            "action_count": result.action_count,
            "applied": result.applied,
            "rejected": rejected,
            "submitted": submitted,
            "prev_state": state.to_dict(),
            "prev_state_hash": result.prev_state_hash,
            "new_state_hash": result.new_state_hash,
            "actions_applied": [action.to_dict() for action in result.actions],
            "payload_hex": payload_hex,
            "payload_size": len(payload),
            "data_scalars": data_scalars,
            "data_len": len(data_scalars),
        }

        STORE.commit_operator_batch(new_state, record, sequence)

        return {
            "batched": True,
            "batch": record,
            "operator_state": _state_to_response(new_state),
        }


@app.get("/operator/batches")
def operator_batches() -> dict:
    _require_initialized()
    return {"batches": STORE.list_batches()}


@app.get("/operator/latest-payload")
def latest_payload() -> dict:
    batch = _latest_batch()
    return {
        "sequence": batch["sequence"],
        "payload_hex": batch["payload_hex"],
        "payload_size": batch["payload_size"],
        "data_scalars": batch["data_scalars"],
        "data_len": batch["data_len"],
    }


@app.post("/devnet/encode-payload")
def encode_payload(request: PayloadRequest) -> dict:
    try:
        payload_hex = request.payload_hex.strip().lower()
        if payload_hex.startswith("0x"):
            payload_hex = payload_hex[2:]
        payload = bytes.fromhex(payload_hex)
        scalars = payload_bytes_to_scalar_hex(payload)
    except (ValueError, ScalarFramingError) as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {
        "payload_hex": payload.hex(),
        "scalar_bytes": 4,
        "data_scalars": scalars,
        "data_len": len(scalars),
    }


@app.get("/devnet/status")
def get_devnet_status() -> dict:
    return _devnet_runtime_status()


@app.post("/devnet/submit-latest-batch")
def submit_latest_batch_to_devnet(request: DevnetSubmitRequest) -> dict:
    with STATE_LOCK:
        batch = _latest_batch()
        existing = batch.get("devnet_submission")
        if existing and existing.get("status") == "submitted" and not request.force:
            raise HTTPException(
                status_code=409,
                detail="latest batch is already submitted to devnet; pass force=true to resubmit",
            )

        try:
            status = _devnet_runtime_status()
            if not status["ready"]:
                if not status.get("submitter_configured"):
                    raise DevnetSubmitError(
                        "EON devnet submission is not configured. Set EON_DEVNET_SUBMIT_CMD "
                        "to a command that signs and submits the payload transaction."
                    )
                if (
                    status.get("active_base_layer_account_id")
                    and not status.get("account_vault_configured")
                    and not status.get("wallet_file_configured")
                ):
                    raise DevnetSubmitError(
                        "EON_KEY_ENCRYPTION_SECRET is required to decrypt the bound "
                        "base-layer account"
                    )
                raise DevnetSubmitError(
                    "active semantic layer has no bound base-layer account; register one "
                    "or configure legacy EON_OPERATOR_WALLET_FILE"
                )

            submission = submit_batch_to_devnet(batch, _submission_account_json())
            updated_batch = STORE.record_devnet_submission(batch["sequence"], submission)
        except DevnetSubmitError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    return {
        "submitted": True,
        "sequence": submission["sequence"],
        "devnet_submission": submission,
        "batch": updated_batch,
    }


@app.get("/verifier/state")
def verifier_state(sl_id: str = Query(default=core.SL_ID.hex())) -> dict:
    sl_id_bytes = _sl_id_bytes(sl_id)
    if sl_id_bytes != PAYMENT_PLUGIN.sl_id:
        raise HTTPException(status_code=404, detail="no plugin registered for sl_id")
    state = _verified_state()
    log = _verifier_store().list_verification_log(sl_id_bytes)
    return {
        "sl_id": sl_id_bytes.hex(),
        "state": _state_to_response(state),
        "accepted_payloads": len([entry for entry in log if entry.get("verdict") == "accepted"]),
    }


@app.get("/verifier/log")
def verifier_log(sl_id: str = Query(default=core.SL_ID.hex())) -> dict:
    sl_id_bytes = _sl_id_bytes(sl_id)
    return {
        "sl_id": sl_id_bytes.hex(),
        "log": _verifier_store().list_verification_log(sl_id_bytes),
    }


@app.get("/verifier/events")
def verifier_events(
    after: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return {
        "events": _verifier_store().list_base_events(after=after, limit=limit),
    }


@app.post("/verifier/ingest-event")
def verifier_ingest_event(event: BaseEventRequest) -> dict:
    with STATE_LOCK:
        result = _verifier_engine().ingest_event(_model_to_dict(event))
        if not result.get("accepted") and not result.get("ignored"):
            raise HTTPException(status_code=400, detail=result["message"])
        return result


@app.post("/verifier/accept-latest-batch")
def verifier_accept_latest_batch() -> dict:
    with STATE_LOCK:
        batch = _latest_batch()
        envelope = {
            "prev_state": batch["prev_state"],
            "sequence": batch["sequence"],
            "prev_state_hash": batch["prev_state_hash"],
            "new_state_hash": batch["new_state_hash"],
            "actions_applied": batch["actions_applied"],
            "payload_hex": batch["payload_hex"],
        }
        valid, msg = _accept_envelope(envelope)
        if not valid:
            raise HTTPException(status_code=400, detail=msg)

        return {
            "accepted": True,
            "message": msg,
            "sequence": envelope["sequence"],
            "verifier_state": _state_to_response(_verified_state()),
        }


@app.post("/verifier/accept-envelope")
def verifier_accept_envelope(envelope: Dict[str, Any]) -> dict:
    with STATE_LOCK:
        valid, msg = _accept_envelope(envelope)
        if not valid:
            raise HTTPException(status_code=400, detail=msg)

        return {
            "accepted": True,
            "message": msg,
            "sequence": envelope["sequence"],
            "verifier_state": _state_to_response(_verified_state()),
        }


@app.post("/verifier/envelope-from-payload")
def verifier_envelope_from_payload(request: PayloadRequest) -> dict:
    state = _verified_state()
    try:
        return envelope_from_payload_hex(request.payload_hex, state)
    except core.PayloadDecodeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
