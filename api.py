"""
api.py - Shared playground API for the Payment SL demo.

This wraps the existing state machine, operator, devnet adapter, and verifier
logic without introducing SQLite yet. State is still stored in the repo's
runtime JSON directories, so this is one shared demo world.
"""

import shutil
from threading import RLock
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import core
from devnet_adapter import (
    ScalarFramingError,
    envelope_from_payload_hex,
    payload_bytes_to_scalar_hex,
)
from verifier import accept_envelope


DEFAULT_ROOT = Path(__file__).parent
STATE_LOCK = RLock()


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


def configure_storage(root: Path) -> None:
    """Point the shared JSON runtime state at a different root."""
    root = Path(root)
    core.ROOT = root
    core.STATE_DIR = root / "operator_state"
    core.WALLETS_DIR = root / "wallets"
    core.VERIFIER_STATE_DIR = root / "verifier_state"
    core.SL_CONFIG_FILE = core.STATE_DIR / "sl_config.json"
    core.CURRENT_STATE_FILE = core.STATE_DIR / "current_state.json"
    core.PENDING_FILE = core.STATE_DIR / "pending.json"
    core.OPERATOR_META_FILE = core.STATE_DIR / "operator_meta.json"
    core.VERIFIED_STATE_FILE = core.VERIFIER_STATE_DIR / "current_state.json"
    core.VERIFIED_LOG_FILE = core.VERIFIER_STATE_DIR / "verified_log.json"


def _api_wallets_file() -> Path:
    return core.STATE_DIR / "api_wallets.json"


def _batches_file() -> Path:
    return core.STATE_DIR / "batches.json"


def _initialized() -> bool:
    return core.SL_CONFIG_FILE.exists() and core.CURRENT_STATE_FILE.exists()


def _require_initialized() -> None:
    if not _initialized():
        raise HTTPException(
            status_code=409,
            detail="SL is not initialized. Call POST /operator/init first.",
        )


def _read_json_or_default(path: Path, default):
    if not path.exists():
        return default
    return core._read_json(path)


def _load_api_wallets() -> Dict[str, dict]:
    return _read_json_or_default(_api_wallets_file(), {})


def _save_api_wallets(wallets: Dict[str, dict]) -> None:
    core._write_json(_api_wallets_file(), wallets)


def _load_batches() -> List[dict]:
    return _read_json_or_default(_batches_file(), [])


def _save_batches(batches: List[dict]) -> None:
    core._write_json(_batches_file(), batches)


def _validate_address(address: str) -> str:
    addr = address.strip().lower()
    if len(addr) != 40:
        raise HTTPException(status_code=400, detail="address must be 40 hex chars")
    try:
        int(addr, 16)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="address must be hex") from e
    return addr


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
    return core.State.from_dict(core._read_json(core.CURRENT_STATE_FILE))


def _verified_state() -> core.State:
    if not core.VERIFIED_STATE_FILE.exists():
        raise HTTPException(status_code=404, detail="no verifier state found")
    return core.State.from_dict(core._read_json(core.VERIFIED_STATE_FILE))


def _queue_action(action: dict) -> dict:
    core.append_pending(action)
    return {
        "queued": True,
        "action": action,
        "pending_count": len(core.load_pending()),
    }


def _issuer_vk() -> str:
    _require_initialized()
    return core._read_json(core.SL_CONFIG_FILE)["issuer_vk"]


def _next_nonce() -> int:
    _require_initialized()
    return core.next_nonce()


def _latest_batch() -> dict:
    batches = _load_batches()
    if not batches:
        raise HTTPException(status_code=404, detail="no operator batch found")
    return batches[-1]


@app.get("/health")
def health() -> dict:
    return {"ok": True, "initialized": _initialized()}


@app.get("/config")
def config() -> dict:
    response = {
        "initialized": _initialized(),
        "sl_id": core.SL_ID.hex(),
        "version": core.VERSION.hex(),
    }
    if _initialized():
        response["issuer_vk"] = core._read_json(core.SL_CONFIG_FILE)["issuer_vk"]
        response["operator_state_hash"] = _operator_state().state_hash()
        response["next_batch_sequence"] = core.next_batch_sequence()
    return response


@app.post("/reset")
def reset() -> dict:
    with STATE_LOCK:
        removed = []
        for path in (core.STATE_DIR, core.WALLETS_DIR, core.VERIFIER_STATE_DIR):
            if path.exists():
                shutil.rmtree(path)
                removed.append(path.name)
        return {"reset": True, "removed": removed}


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

        core.STATE_DIR.mkdir(parents=True, exist_ok=True)
        core.WALLETS_DIR.mkdir(parents=True, exist_ok=True)

        config_obj = {
            "issuer_vk": request.issuer_vk,
            "sl_id": core.SL_ID.hex(),
            "version": core.VERSION.hex(),
        }
        genesis = core.State(issuer_vk=request.issuer_vk)
        core._write_json(core.SL_CONFIG_FILE, config_obj)
        core.save_current_state(genesis)
        core.save_pending([])
        core.save_operator_meta({"next_sequence": 1})
        _save_batches([])
        _save_api_wallets({})

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
        "pending_count": len(core.load_pending()),
        "next_batch_sequence": core.next_batch_sequence(),
    }


@app.post("/wallets")
def register_wallet(request: WalletRequest) -> dict:
    with STATE_LOCK:
        _require_initialized()
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
        wallets = _load_api_wallets()
        wallets[address] = {
            "label": label,
            "address": address,
        }
        _save_api_wallets(wallets)

        return {
            "label": label,
            "address": address,
            "derived_from_vk": bool(vk),
        }


@app.get("/wallets")
def list_wallets() -> dict:
    _require_initialized()
    wallets = _load_api_wallets()
    return {"wallets": list(wallets.values())}


@app.get("/wallets/{address}")
def get_wallet(address: str) -> dict:
    _require_initialized()
    addr = _validate_address(address)
    wallet = _load_api_wallets().get(addr)
    if not wallet:
        raise HTTPException(status_code=404, detail="wallet is not registered")
    return wallet


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
    return {"pending": core.load_pending()}


@app.post("/operator/batch")
def operator_batch() -> dict:
    with STATE_LOCK:
        state = _operator_state()
        pending_actions = core.load_pending()
        if not pending_actions:
            return {"batched": False, "message": "No pending actions. Nothing to batch."}

        sequence = core.next_batch_sequence()
        actions = [core.Action.from_dict(d) for d in pending_actions]
        new_state, result = core.process_batch(state, actions, sequence=sequence)
        payload = result.data_field_payload()
        payload_hex = payload.hex()
        data_scalars = payload_bytes_to_scalar_hex(payload)

        core.save_current_state(new_state)
        core.save_pending([])
        core.advance_batch_sequence(sequence)

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

        batches = _load_batches()
        batches.append(record)
        _save_batches(batches)

        return {
            "batched": True,
            "batch": record,
            "operator_state": _state_to_response(new_state),
        }


@app.get("/operator/batches")
def operator_batches() -> dict:
    _require_initialized()
    return {"batches": _load_batches()}


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


@app.get("/verifier/state")
def verifier_state() -> dict:
    state = _verified_state()
    return {
        "state": _state_to_response(state),
        "accepted_payloads": len(core.load_verified_log()),
    }


@app.get("/verifier/log")
def verifier_log() -> dict:
    return {"log": core.load_verified_log()}


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
        valid, msg = accept_envelope(envelope)
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
        valid, msg = accept_envelope(envelope)
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
