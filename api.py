"""
api.py - Shared playground API for the Payment SL demo.

This wraps the existing state machine, operator, devnet adapter, and verifier
logic with SQLite-backed runtime storage, so this is one shared demo world that
can be hosted on a persistent Railway volume.
"""

import hashlib
import json
import os
import sqlite3
import re
import time
from types import SimpleNamespace
from threading import Event, RLock, Thread
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
from payment_plugin import PAYMENT_PLUGIN, PaymentSLPlugin, payment_plugin_for
from storage import DEFAULT_DB_PATH, SQLiteStorage
from verifier_engine import PluginRegistry, VerifierEngine, VerifierStore
from verifier_engine.eon_data import (
    BUNDLE_SL_ID,
    ScalarFramingError,
    decode_bundle_payload,
    decode_transition_payload,
    payload_header,
    scalar_hex_to_payload_bytes,
    payload_bytes_to_scalar_hex,
)
from verifier_engine.sources import BaseLayerAPIEventSource


STATE_LOCK = RLock()
STORE = SQLiteStorage(DEFAULT_DB_PATH)
VERIFIER_POLL_STOP = Event()
VERIFIER_POLL_THREAD: Optional[Thread] = None


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
    sl_id: str = Field(default=core.SL_ID.hex(), min_length=1)
    version: str = Field(default=core.VERSION.hex(), min_length=1)
    operator_wallet_address: Optional[str] = None
    base_layer_account_id: Optional[str] = None
    reset_existing: bool = False


class LayerRequest(BaseModel):
    sl_id: str = Field(default=core.SL_ID.hex(), min_length=1)
    version: str = Field(default=core.VERSION.hex(), min_length=1)
    asset_id: Optional[str] = None


class WalletRequest(BaseModel):
    label: Optional[str] = None
    vk: Optional[str] = None
    address: Optional[str] = None
    kind: Literal["user", "sl_operator", "coordinator", "verifier"] = "user"


class SemanticLayerAssetRequest(BaseModel):
    asset_id: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=24)
    name: str = Field(min_length=1)
    decimals: int = Field(default=0, ge=0, le=18)
    asset_type: str = Field(default="fungible", min_length=1, max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SemanticLayerRequest(BaseModel):
    name: str = Field(min_length=1)
    sl_id: str = Field(default=core.SL_ID.hex(), min_length=1)
    version: str = Field(default=core.VERSION.hex(), min_length=1)
    operator_wallet_address: str
    base_layer_account_id: Optional[str] = None
    issuer_vk_ref: Optional[str] = None
    operator_vk_ref: Optional[str] = None
    assets: list[SemanticLayerAssetRequest] = Field(default_factory=list)


class BaseLayerAccountRequest(BaseModel):
    label: str = Field(min_length=1)
    owner_wallet_address: str
    purpose: Optional[
        Literal["user_wallet", "sl_operator", "coordinator", "verifier"]
    ] = None
    eon_address: Optional[str] = None
    account_json: dict[str, Any]


class BaseLayerAccountPoolRequest(BaseModel):
    label: str = Field(min_length=1)
    eon_address: Optional[str] = None
    account_json: dict[str, Any]
    funding_tx_hash: Optional[str] = None
    funded_amount: Optional[str] = None
    balance_last_checked: Optional[str] = None


class BaseLayerAccountAllocateRequest(BaseModel):
    label: Optional[str] = None
    owner_wallet_address: str
    purpose: Optional[
        Literal["user_wallet", "sl_operator", "coordinator", "verifier"]
    ] = None


class BaseLayerAccountGenerateRequest(BaseLayerAccountAllocateRequest):
    pass


class AmountToRequest(LayerRequest):
    to_address: str
    amount: int = Field(gt=0)


class AmountFromRequest(LayerRequest):
    from_address: str
    amount: int = Field(gt=0)


class TargetRequest(LayerRequest):
    target_address: str


class TransferRequest(LayerRequest):
    from_address: str
    to_address: str
    amount: int = Field(gt=0)
    vk: str = Field(min_length=1)


class PayloadRequest(BaseModel):
    payload_hex: str


class DevnetSubmitRequest(BaseModel):
    force: bool = False
    sequence: Optional[int] = Field(default=None, ge=1)
    sl_id: str = Field(default=core.SL_ID.hex(), min_length=1)
    version: str = Field(default=core.VERSION.hex(), min_length=1)
    wait_for_verifier: bool = True
    verifier_timeout_seconds: int = Field(default=120, ge=0, le=300)
    verifier_poll_interval_seconds: float = Field(default=5, gt=0, le=30)


class VerifierSyncRequest(BaseModel):
    sl_id: str = Field(default=core.SL_ID.hex(), min_length=1)
    version: str = Field(default=core.VERSION.hex(), min_length=1)
    posting_owner: Optional[str] = None
    expected_sequence: Optional[int] = Field(default=None, ge=1)
    expected_state_hash: Optional[str] = None
    timeout_seconds: int = Field(default=0, ge=0, le=300)
    poll_interval_seconds: float = Field(default=5, gt=0, le=30)


class IntentAssetRefRequest(BaseModel):
    sl_id: str = Field(min_length=1)
    version: str = Field(default=core.VERSION.hex(), min_length=1)
    asset_id: str = Field(min_length=1)


class SignedIntentRequest(BaseModel):
    proposal_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    signer: str = Field(min_length=1)
    signer_vk: str = Field(min_length=1)
    nonce: int = Field(ge=0)
    asset_ref: Optional[IntentAssetRefRequest] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    expires_at_height: Optional[int] = Field(default=None, ge=0)
    max_fee_bps: Optional[int] = Field(default=None, ge=0, lt=10_000)
    signature: str = Field(min_length=1)


class OperatorExecutionRequest(BaseModel):
    proposal_id: str = Field(min_length=1)
    proposal: dict[str, Any] = Field(default_factory=dict)
    signed_intents: list[SignedIntentRequest] = Field(default_factory=list)
    submit_to_base: bool = True
    wait_for_verifier: bool = True
    verifier_timeout_seconds: int = Field(default=120, ge=0, le=300)
    verifier_poll_interval_seconds: float = Field(default=5, gt=0, le=30)


class VerifierNotifyRequest(BaseModel):
    proposal_id: Optional[str] = None
    source_event: Optional[Dict[str, Any]] = None
    source_events: list[Dict[str, Any]] = Field(default_factory=list)
    signed_intent_count: Optional[int] = None
    mode: Optional[str] = None


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


def _require_initialized(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> None:
    if not _initialized(sl_id, version):
        raise HTTPException(
            status_code=409,
            detail="SL is not initialized. Call POST /operator/init first.",
        )


def _initialized(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> bool:
    if sl_id is None and version is None:
        return bool(STORE.list_runtime_configs())
    layer_sl_id, layer_version = _layer_hex(sl_id, version)
    return STORE.is_initialized(layer_sl_id, layer_version)


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


def _validate_asset_id(value: str) -> str:
    asset_id = value.strip().upper()
    if not asset_id:
        raise HTTPException(status_code=400, detail="asset_id is required")
    if len(asset_id) > 64:
        raise HTTPException(status_code=400, detail="asset_id must be 64 chars or fewer")
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")
    if any(char not in allowed for char in asset_id):
        raise HTTPException(
            status_code=400,
            detail="asset_id may contain letters, numbers, underscore, dash, dot, or colon",
        )
    return asset_id


def _asset_to_record(asset: SemanticLayerAssetRequest | dict) -> dict:
    data = _model_to_dict(asset) if isinstance(asset, BaseModel) else dict(asset)
    asset_id = _validate_asset_id(str(data.get("asset_id", "")))
    symbol = str(data.get("symbol") or asset_id).strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="asset symbol is required")
    if len(symbol) > 24:
        raise HTTPException(status_code=400, detail="asset symbol must be 24 chars or fewer")
    name = str(data.get("name") or symbol).strip()
    if not name:
        raise HTTPException(status_code=400, detail="asset name is required")
    decimals = int(data.get("decimals", 0))
    if decimals < 0 or decimals > 18:
        raise HTTPException(status_code=400, detail="asset decimals must be between 0 and 18")
    asset_type = str(data.get("asset_type") or "fungible").strip().lower()
    if not asset_type:
        raise HTTPException(status_code=400, detail="asset_type is required")
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="asset metadata must be an object")
    return {
        "asset_id": asset_id,
        "symbol": symbol,
        "name": name,
        "decimals": decimals,
        "asset_type": asset_type,
        "metadata": metadata,
    }


def _default_asset_for_layer(record: Optional[dict]) -> dict:
    name = str((record or {}).get("name") or "Payment SL").strip()
    symbol_source = re.sub(r"\s+SL$", "", name, flags=re.IGNORECASE)
    symbol = re.sub(r"[^A-Z0-9_.:-]", "_", symbol_source.upper()).strip("_")[:12] or "ASSET"
    return {
        "asset_id": core.DEFAULT_ASSET_ID,
        "symbol": symbol,
        "name": f"{name} asset" if name else "Payment token",
        "decimals": 0,
        "asset_type": "fungible",
        "metadata": {},
    }


def _effective_assets(record: Optional[dict], state: Optional[core.State] = None) -> list[dict]:
    assets = list((record or {}).get("assets") or [])
    if assets:
        return assets
    if state and state.assets:
        return [state.assets[asset_id] for asset_id in sorted(state.assets)]
    return [_default_asset_for_layer(record)]


def _semantic_layer_assets(sl_id: str, version: str) -> list[dict]:
    record = STORE.get_semantic_layer(sl_id)
    if not record or record.get("version") != version:
        return []
    return list(record.get("assets") or [])


def _resolve_asset_id(
    sl_id: str,
    version: str,
    requested_asset_id: Optional[str] = None,
) -> str:
    assets = _semantic_layer_assets(sl_id, version)
    if requested_asset_id:
        asset_id = _validate_asset_id(requested_asset_id)
    elif assets:
        asset_id = str(assets[0]["asset_id"])
    else:
        asset_id = core.DEFAULT_ASSET_ID

    if assets and asset_id not in {asset["asset_id"] for asset in assets}:
        raise HTTPException(
            status_code=400,
            detail=f"asset_id is not registered on semantic layer: {asset_id}",
        )
    return asset_id


def _asset_action_fields(asset: dict) -> dict:
    return {
        "asset_id": asset["asset_id"],
        "symbol": asset["symbol"],
        "asset_name": asset["name"],
        "decimals": asset["decimals"],
        "asset_type": asset["asset_type"],
        "metadata": asset.get("metadata", {}),
    }


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
        "purpose": record.get("purpose", "sl_operator"),
        "eon_address": record["eon_address"],
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    }


def _public_base_layer_pool_account(record: dict) -> dict:
    return {
        "id": record["id"],
        "label": record["label"],
        "eon_address": record["eon_address"],
        "status": record["status"],
        "assigned_base_layer_account_id": record.get("assigned_base_layer_account_id"),
        "funding_tx_hash": record.get("funding_tx_hash"),
        "funded_amount": record.get("funded_amount"),
        "balance_last_checked": record.get("balance_last_checked"),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "assigned_at": record.get("assigned_at"),
    }


def _store_base_layer_account(
    label: str,
    owner_wallet_address: str,
    account_json: dict[str, Any],
    eon_address: Optional[str] = None,
    purpose: Optional[str] = None,
) -> dict:
    owner_address = _validate_address(owner_wallet_address)
    owner_wallet = STORE.get_wallet(owner_address)
    if not owner_wallet:
        raise HTTPException(status_code=400, detail="owner wallet is not registered")
    account_purpose = _resolve_base_layer_account_purpose(owner_wallet, purpose)

    json_address = _account_json_address(account_json)
    requested_address = _validate_eon_address(eon_address) if eon_address else json_address
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
        encrypted_account_json = encrypt_account_json(account_json)
    except AccountVaultError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    record = {
        "id": f"acct_{uuid4().hex[:12]}",
        "owner_wallet_address": owner_address,
        "label": label.strip(),
        "purpose": account_purpose,
        "eon_address": requested_address,
        "encrypted_account_json": encrypted_account_json,
    }
    created = STORE.create_base_layer_account(record)
    return _public_base_layer_account(created)


def _store_base_layer_pool_account(request: BaseLayerAccountPoolRequest) -> dict:
    json_address = _account_json_address(request.account_json)
    requested_address = (
        _validate_eon_address(request.eon_address) if request.eon_address else json_address
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
        "id": f"pool_{uuid4().hex[:12]}",
        "label": request.label.strip(),
        "eon_address": requested_address,
        "encrypted_account_json": encrypted_account_json,
        "funding_tx_hash": request.funding_tx_hash,
        "funded_amount": request.funded_amount,
        "balance_last_checked": request.balance_last_checked,
    }
    try:
        created = STORE.import_base_layer_pool_account(record)
    except sqlite3.IntegrityError as e:
        raise HTTPException(
            status_code=409,
            detail="base-layer account already exists in the pool",
        ) from e
    return _public_base_layer_pool_account(created)


def _default_account_purpose_for_wallet_kind(kind: str) -> str:
    if kind == "user":
        return "user_wallet"
    if kind in {"sl_operator", "coordinator", "verifier"}:
        return kind
    raise HTTPException(status_code=400, detail="unsupported wallet kind")


def _resolve_base_layer_account_purpose(
    wallet: dict,
    requested_purpose: Optional[str],
) -> str:
    expected = _default_account_purpose_for_wallet_kind(wallet.get("kind", "user"))
    if requested_purpose and requested_purpose != expected:
        raise HTTPException(
            status_code=400,
            detail=f"purpose must be {expected} for wallet kind={wallet.get('kind', 'user')}",
        )
    return requested_purpose or expected


def _state_to_response(state: core.State) -> dict:
    balances_by_asset = {
        core.DEFAULT_ASSET_ID: dict(sorted(state.balances.items())),
        **{
            asset_id: dict(sorted(balances.items()))
            for asset_id, balances in sorted(state.balances_by_asset.items())
        },
    }
    total_supply_by_asset = {
        core.DEFAULT_ASSET_ID: state.total_supply,
        **dict(sorted(state.total_supply_by_asset.items())),
    }
    frozen_by_asset = {
        core.DEFAULT_ASSET_ID: sorted(state.frozen),
        **{
            asset_id: sorted(addresses)
            for asset_id, addresses in sorted(state.frozen_by_asset.items())
        },
    }
    pool_escrow = {
        pool_id: dict(sorted(balances.items()))
        for pool_id, balances in sorted(state.pool_escrow.items())
    }
    return {
        "issuer_vk": state.issuer_vk,
        "balances": dict(sorted(state.balances.items())),
        "total_supply": state.total_supply,
        "nonce": state.nonce,
        "frozen": sorted(state.frozen),
        "assets": [
            state.assets[asset_id]
            for asset_id in sorted(state.assets)
        ],
        "balances_by_asset": balances_by_asset,
        "total_supply_by_asset": total_supply_by_asset,
        "frozen_by_asset": frozen_by_asset,
        "pool_escrow": pool_escrow,
        "state_hash": state.state_hash(),
    }


def _operator_state(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> core.State:
    layer_sl_id, layer_version = _layer_hex(sl_id, version)
    _require_initialized(layer_sl_id, layer_version)
    state = STORE.load_operator_state(layer_sl_id, layer_version)
    if state is None:
        raise HTTPException(status_code=409, detail="operator state is missing")
    return state


def _verified_state(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> core.State:
    state, _, _ = _verified_state_for_layer(sl_id, version)
    return state


def _verifier_store() -> VerifierStore:
    return VerifierStore(STORE.db_path)


def _verifier_engine() -> VerifierEngine:
    config = {}
    plugins_by_key: dict[tuple[str, str], Any] = {
        (PAYMENT_PLUGIN.sl_id.hex(), core.VERSION.hex()): PAYMENT_PLUGIN
    }
    for runtime in STORE.list_runtime_configs():
        sl_id_hex = str(runtime["sl_id"])
        version_hex = str(runtime["version"])
        plugin = _payment_plugin(sl_id_hex, version_hex)
        plugins_by_key[(sl_id_hex, version_hex)] = plugin
        record = STORE.get_semantic_layer(sl_id_hex)
        assets = (
            list(record.get("assets", []))
            if record and record.get("version") == version_hex
            else []
        )
        plugin_config = {
            "issuer_vk": runtime["issuer_vk"],
            "assets": assets,
        }
        config[f"{sl_id_hex}:{version_hex}"] = plugin_config
        config[sl_id_hex] = plugin_config
    return VerifierEngine(
        store=_verifier_store(),
        registry=PluginRegistry(list(plugins_by_key.values())),
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


def _layer_hex(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> tuple[str, str]:
    sl_id_hex = _sl_id_bytes(sl_id).hex() if sl_id else core.SL_ID.hex()
    version_value = _version_hex(version) if version else core.VERSION.hex()
    return sl_id_hex, version_value


def _layer_bytes(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> tuple[bytes, bytes]:
    sl_id_hex, version_hex = _layer_hex(sl_id, version)
    return bytes.fromhex(sl_id_hex), bytes.fromhex(version_hex)


def _payment_plugin(sl_id: str, version: str) -> PaymentSLPlugin:
    sl_id_bytes, version_bytes = _layer_bytes(sl_id, version)
    if sl_id_bytes == PAYMENT_PLUGIN.sl_id and version_bytes == core.VERSION:
        return PAYMENT_PLUGIN
    return payment_plugin_for(sl_id_bytes, version_bytes)


def _layer_coordinates(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> tuple[bytes, bytes]:
    return _layer_bytes(sl_id, version)


def _verified_state_for_layer(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> tuple[core.State, bytes, bytes]:
    sl_id_hex, version_hex = _layer_hex(sl_id, version)
    sl_id_bytes, version_bytes = _layer_bytes(sl_id_hex, version_hex)
    plugin = _payment_plugin(sl_id_hex, version_hex)
    checkpoint = _verifier_store().load_checkpoint(sl_id_bytes, version_bytes)
    if checkpoint is not None:
        return plugin.state_from_dict(checkpoint["state"]), sl_id_bytes, version_bytes

    legacy_state = STORE.load_verified_state(sl_id_hex, version_hex)
    if legacy_state is not None:
        return legacy_state, sl_id_bytes, version_bytes

    raise HTTPException(status_code=404, detail="no verifier state found for semantic layer")


def _operator_state_for_layer(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> tuple[core.State, bytes, bytes]:
    sl_id_hex, version_hex = _layer_hex(sl_id, version)
    state = STORE.load_operator_state(sl_id_hex, version_hex)
    if state is None:
        raise HTTPException(status_code=404, detail="no operator state found for semantic layer")

    sl_id_bytes, version_bytes = _layer_bytes(sl_id_hex, version_hex)
    return state, sl_id_bytes, version_bytes


def _operator_checkpoint_status(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> dict:
    layer_sl_id, layer_version = _layer_hex(sl_id, version)
    runtime_config = STORE.load_sl_config(layer_sl_id, layer_version)
    operator_state = STORE.load_operator_state(layer_sl_id, layer_version)
    operator_next_sequence = (
        STORE.next_batch_sequence(layer_sl_id, layer_version)
        if runtime_config is not None
        else None
    )
    checkpoint = _verifier_store().load_checkpoint(
        bytes.fromhex(layer_sl_id),
        bytes.fromhex(layer_version),
    )
    verifier_sequence = int(checkpoint["sequence"]) if checkpoint is not None else None
    verifier_state_hash = str(checkpoint["state_hash"]) if checkpoint is not None else None
    operator_state_hash = operator_state.state_hash() if operator_state is not None else None
    operator_behind = bool(
        checkpoint is not None
        and operator_next_sequence is not None
        and verifier_sequence is not None
        and verifier_sequence >= operator_next_sequence
    )
    state_hash_mismatch = bool(
        verifier_state_hash
        and operator_state_hash
        and verifier_state_hash != operator_state_hash
    )
    operator_last_sequence = (
        operator_next_sequence - 1
        if operator_next_sequence is not None
        else None
    )
    operator_conflict = bool(
        checkpoint is not None
        and operator_last_sequence is not None
        and verifier_sequence == operator_last_sequence
        and state_hash_mismatch
    )
    if operator_behind:
        message = (
            "operator state is behind verifier checkpoint; sync operator from verifier "
            "before accepting new semantic-layer actions"
        )
    elif operator_conflict:
        message = (
            "operator state conflicts with verifier checkpoint at the same sequence; "
            "sync operator from verifier before accepting new semantic-layer actions"
        )
    elif state_hash_mismatch:
        message = "operator has local state not yet matched by verifier checkpoint"
    elif checkpoint is not None:
        message = "operator checkpoint is current with verifier"
    else:
        message = "no verifier checkpoint exists yet"

    return {
        "current": not operator_behind and not operator_conflict,
        "operator_behind_verifier": operator_behind,
        "operator_conflicts_verifier": operator_conflict,
        "state_hash_mismatch": state_hash_mismatch,
        "operator_next_sequence": operator_next_sequence,
        "operator_last_sequence": operator_last_sequence,
        "verifier_sequence": verifier_sequence,
        "operator_state_hash": operator_state_hash,
        "verifier_state_hash": verifier_state_hash,
        "message": message,
        "sl_id": layer_sl_id,
        "version": layer_version,
    }


def _require_operator_checkpoint_current(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> dict:
    status = _operator_checkpoint_status(sl_id, version)
    if not status["current"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": status["message"],
                "operator_checkpoint": status,
            },
        )
    return status


def _model_to_dict(model: BaseModel) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _normalize_intent_ref(value: Optional[dict[str, Any]]) -> Optional[dict[str, str]]:
    if value is None:
        return None
    sl_id = str(value["sl_id"]).lower().removeprefix("0x")
    version = str(value.get("version", core.VERSION.hex())).lower().removeprefix("0x")
    try:
        if len(bytes.fromhex(sl_id)) != 4:
            raise ValueError
        if len(bytes.fromhex(version)) != 2:
            raise ValueError
    except ValueError as e:
        raise HTTPException(status_code=400, detail="signed intent asset_ref has invalid layer coordinates") from e
    return {
        "sl_id": sl_id,
        "version": version,
        "asset_id": str(value["asset_id"]).upper(),
    }


def _intent_signing_payload(intent: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "proposal_id": intent["proposal_id"],
        "action": intent["action"],
        "signer": str(intent["signer"]).lower(),
        "nonce": int(intent["nonce"]),
        "asset_ref": _normalize_intent_ref(intent.get("asset_ref")),
        "payload": intent.get("payload") or {},
        "expires_at_height": intent.get("expires_at_height"),
        "max_fee_bps": intent.get("max_fee_bps"),
    }
    return {key: value for key, value in payload.items() if value is not None}


def _demo_intent_signature(intent: dict[str, Any], signer_vk: str) -> str:
    payload = _canonical_json(_intent_signing_payload(intent))
    return hashlib.sha256(f"{payload}:{signer_vk}".encode()).hexdigest()


def _normalized_signed_intent(intent: SignedIntentRequest) -> dict[str, Any]:
    data = _model_to_dict(intent)
    data["signer"] = str(data["signer"]).lower()
    if data.get("asset_ref") is not None:
        data["asset_ref"] = _normalize_intent_ref(data["asset_ref"])
    return data


def _validate_execution_signed_intents(request: OperatorExecutionRequest) -> list[dict[str, Any]]:
    proposal = request.proposal or {}
    proposal_id = str(proposal.get("proposal_id") or request.proposal_id)
    if proposal_id != request.proposal_id:
        raise HTTPException(status_code=400, detail="execution request proposal_id does not match proposal")
    terms = proposal.get("terms") or {}
    height = int(terms.get("height", 0))
    required = [
        _intent_signing_payload(intent)
        for intent in proposal.get("required_intents", [])
    ]
    supplied = [_normalized_signed_intent(intent) for intent in request.signed_intents]

    if not required:
        raise HTTPException(status_code=400, detail="proposal has no semantic-layer intents for operator execution")
    if len(supplied) != len(required):
        raise HTTPException(
            status_code=400,
            detail=f"expected {len(required)} signed intents, got {len(supplied)}",
        )

    unmatched = list(required)
    for intent in supplied:
        if intent["proposal_id"] != proposal_id:
            raise HTTPException(status_code=400, detail="signed intent proposal_id does not match execution proposal")
        if core.hash_vk(intent["signer_vk"]) != intent["signer"]:
            raise HTTPException(status_code=400, detail="signed intent signer_vk does not match signer")
        if _demo_intent_signature(intent, intent["signer_vk"]) != intent["signature"]:
            raise HTTPException(status_code=400, detail="signed intent signature is invalid")
        expires_at_height = intent.get("expires_at_height")
        if expires_at_height is not None and height > int(expires_at_height):
            raise HTTPException(status_code=400, detail="signed intent is expired")

        signing_payload = _intent_signing_payload(intent)
        for index, candidate in enumerate(unmatched):
            if signing_payload == candidate:
                unmatched.pop(index)
                break
        else:
            raise HTTPException(status_code=400, detail="signed intent does not match proposal requirements")

    if unmatched:
        raise HTTPException(status_code=400, detail="missing signed intents for proposal")
    return supplied


def _amm_action_from_proposal(proposal: dict[str, Any]) -> dict[str, Any]:
    kind = str(proposal.get("kind") or "")
    terms = proposal.get("terms") or {}
    movements = terms.get("asset_movements") or []
    if kind == "add_liquidity":
        return {
            "type": "add_liquidity",
            "nonce": int(terms.get("amm_nonce") or 0),
            "pool_id": str(terms["pool_id"]),
            "provider": str(terms["provider"]),
            "amount_a": int(terms["amount_a"]),
            "amount_b": int(terms["amount_b"]),
            "min_lp_shares": int(terms.get("min_lp_shares", 1)),
            "asset_movements": movements,
        }
    if kind == "swap_exact_in":
        action = {
            "type": "swap_exact_in",
            "nonce": int(terms.get("amm_nonce") or 0),
            "pool_id": str(terms["pool_id"]),
            "trader": str(terms["trader"]),
            "input_asset": terms["input_asset"],
            "amount_in": int(terms["amount_in"]),
            "min_amount_out": int(terms.get("min_amount_out", 0)),
            "amount_out": int(terms["amount_out"]),
            "asset_movements": movements,
        }
        if terms.get("deadline_height") is not None:
            action["deadline_height"] = int(terms["deadline_height"])
        return action
    raise HTTPException(status_code=400, detail=f"unsupported operator execution proposal kind: {kind}")


def _execution_context_from_proposal(proposal: dict[str, Any]) -> SimpleNamespace:
    terms = proposal.get("terms") or {}
    bundle_id = str(terms.get("bundle_id") or "")
    if not bundle_id:
        raise HTTPException(status_code=400, detail="proposal terms missing bundle_id")
    return SimpleNamespace(
        bundle_id=bundle_id,
        child_transitions=[
            {
                "sl_id": str(terms.get("amm_sl_id") or "marketplace_amm"),
                "actions": [_amm_action_from_proposal(proposal)],
            }
        ],
        height=terms.get("height"),
    )


def _pool_action_from_intent(intent: dict[str, Any], proposal: dict[str, Any]) -> dict[str, Any]:
    asset_ref = intent.get("asset_ref")
    if not asset_ref:
        raise HTTPException(status_code=400, detail="signed pool intent is missing asset_ref")
    payload = intent.get("payload") or {}
    action_kind = str(intent["action"])
    if action_kind == "deposit":
        action_type = core.ActionType.POOL_DEPOSIT.value
        address_field = "owner"
    elif action_kind == "withdraw":
        action_type = core.ActionType.POOL_WITHDRAW.value
        address_field = "owner"
    elif action_kind == "swap_in":
        action_type = core.ActionType.POOL_SWAP_IN.value
        address_field = "trader"
    elif action_kind == "swap_out":
        action_type = core.ActionType.POOL_SWAP_OUT.value
        address_field = "trader"
    else:
        raise HTTPException(status_code=400, detail=f"unsupported signed intent action: {action_kind}")

    terms = proposal.get("terms") or {}
    return {
        "type": action_type,
        "sender_vk": intent["signer_vk"],
        "nonce": int(intent["nonce"]),
        "asset_id": str(asset_ref["asset_id"]),
        "bundle_id": str(terms["bundle_id"]),
        "pool_id": str(payload["pool_id"]),
        "leg_id": str(payload["leg_id"]),
        address_field: str(payload["address"]),
        "amount": int(payload["amount"]),
    }


def _execution_actions_by_layer(
    proposal: dict[str, Any],
    signed_intents: list[dict[str, Any]],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for intent in signed_intents:
        asset_ref = intent.get("asset_ref")
        if not asset_ref:
            raise HTTPException(status_code=400, detail="signed intent missing asset_ref")
        key = (str(asset_ref["sl_id"]), str(asset_ref["version"]))
        grouped.setdefault(key, []).append({
            "intent": intent,
            "action": _pool_action_from_intent(intent, proposal),
        })
    return grouped


def _queue_action(
    action: dict,
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> dict:
    layer_sl_id, layer_version = _layer_hex(sl_id, version)
    _require_operator_checkpoint_current(layer_sl_id, layer_version)
    STORE.append_pending(action, layer_sl_id, layer_version)
    return {
        "queued": True,
        "action": action,
        "pending_count": STORE.pending_count(layer_sl_id, layer_version),
        "sl_id": layer_sl_id,
        "version": layer_version,
    }


def _queue_asset_registration_if_runtime_exists(
    asset: dict,
    sl_id: str,
    version: str,
) -> Optional[dict]:
    if not _initialized(sl_id, version):
        return None

    state = STORE.load_operator_state(sl_id, version)
    if state is not None and state.asset_record(asset["asset_id"]):
        return None

    action = {
        "type": core.ActionType.REGISTER_ASSET.value,
        "sender_vk": _issuer_vk(sl_id, version),
        "nonce": _next_nonce(sl_id, version),
        **_asset_action_fields(asset),
    }
    return _queue_action(action, sl_id, version)


def _issuer_vk(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> str:
    layer_sl_id, layer_version = _layer_hex(sl_id, version)
    _require_initialized(layer_sl_id, layer_version)
    config = STORE.load_sl_config(layer_sl_id, layer_version)
    if not config:
        raise HTTPException(status_code=409, detail="SL config is missing")
    return config["issuer_vk"]


def _next_nonce(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> int:
    layer_sl_id, layer_version = _layer_hex(sl_id, version)
    _require_initialized(layer_sl_id, layer_version)
    _require_operator_checkpoint_current(layer_sl_id, layer_version)
    return STORE.next_nonce(layer_sl_id, layer_version)


def _latest_batch(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> dict:
    layer_sl_id, layer_version = _layer_hex(sl_id, version)
    batch = STORE.latest_batch(layer_sl_id, layer_version)
    if batch is None:
        raise HTTPException(status_code=404, detail="no operator batch found")
    return batch


def _batch_for_submission(
    sl_id: str,
    version: str,
    sequence: Optional[int] = None,
) -> dict:
    if sequence is None:
        return _latest_batch(sl_id, version)

    for batch in STORE.list_batches(sl_id, version):
        if int(batch["sequence"]) == sequence:
            return batch
    raise HTTPException(status_code=404, detail=f"operator batch {sequence} not found")


def _commit_operator_action_batch(
    *,
    actions: list[dict[str, Any]],
    context: Any,
    sl_id: str,
    version: str,
    source: str,
    proposal_id: Optional[str] = None,
) -> dict:
    checkpoint_status = _require_operator_checkpoint_current(sl_id, version)
    state = _operator_state(sl_id, version)
    sequence = STORE.next_batch_sequence(sl_id, version)
    core_actions = [core.Action.from_dict(action) for action in actions]
    new_state, result = core.process_batch(
        state,
        core_actions,
        sequence=sequence,
        sl_id=bytes.fromhex(sl_id),
        version=bytes.fromhex(version),
        context=context,
    )
    if result.rejected:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "operator rejected one or more execution-request actions",
                "rejected": [
                    {
                        "index": idx,
                        "error": err,
                        "action": actions[idx],
                    }
                    for idx, err in result.rejected
                ],
                "sl_id": sl_id,
                "version": version,
            },
        )

    payload = result.data_field_payload()
    payload_hex = payload.hex()
    data_scalars = payload_bytes_to_scalar_hex(payload)
    submitted = [
        {
            "index": idx,
            "status": "applied",
            "action": action,
            "error": None,
        }
        for idx, action in enumerate(actions)
    ]
    record = {
        "status": "batched",
        "source": source,
        "proposal_id": proposal_id,
        "sequence": result.sequence,
        "action_count": result.action_count,
        "applied": result.applied,
        "rejected": [],
        "submitted": submitted,
        "prev_state": state.to_dict(),
        "prev_state_hash": result.prev_state_hash,
        "new_state_hash": result.new_state_hash,
        "actions_applied": [action.to_dict() for action in result.actions],
        "payload_hex": payload_hex,
        "payload_size": len(payload),
        "data_scalars": data_scalars,
        "data_len": len(data_scalars),
        "sl_id": sl_id,
        "version": version,
    }
    STORE.commit_operator_batch(
        new_state,
        record,
        sequence,
        sl_id,
        version,
        clear_pending=False,
    )
    return {
        "batch": record,
        "operator_state": _state_to_response(new_state),
        "operator_checkpoint": checkpoint_status,
        "sl_id": sl_id,
        "version": version,
    }


def _preflight_operator_action_batch(
    *,
    actions: list[dict[str, Any]],
    context: Any,
    sl_id: str,
    version: str,
) -> None:
    _require_operator_checkpoint_current(sl_id, version)
    state = _operator_state(sl_id, version)
    sequence = STORE.next_batch_sequence(sl_id, version)
    core_actions = [core.Action.from_dict(action) for action in actions]
    _new_state, result = core.process_batch(
        state,
        core_actions,
        sequence=sequence,
        sl_id=bytes.fromhex(sl_id),
        version=bytes.fromhex(version),
        context=context,
    )
    if result.rejected:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "operator preflight rejected one or more execution-request actions",
                "rejected": [
                    {
                        "index": idx,
                        "error": err,
                        "action": actions[idx],
                    }
                    for idx, err in result.rejected
                ],
                "sl_id": sl_id,
                "version": version,
            },
        )


def _submit_operator_batch_to_devnet(
    *,
    batch: dict,
    sl_id: str,
    version: str,
    force: bool = False,
) -> tuple[dict, dict]:
    existing = batch.get("devnet_submission")
    if existing and existing.get("status") == "submitted" and not force:
        raise HTTPException(
            status_code=409,
            detail=f"batch {batch['sequence']} is already submitted to devnet; pass force=true to resubmit",
        )

    try:
        status = _devnet_runtime_status(sl_id, version)
        if not status["ready"]:
            submitter_error = status.get("submitter_error")
            if submitter_error:
                raise DevnetSubmitError(
                    f"EON devnet submitter is misconfigured: {submitter_error}"
                )
            if not status.get("submitter_configured"):
                raise DevnetSubmitError(
                    "EON devnet submission is not configured. Set BASE_LAYER_API_URL "
                    "to the iovi-api service or configure legacy EON_DEVNET_SUBMIT_CMD."
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

        submission = submit_batch_to_devnet(batch, _submission_account_json(sl_id, version))
        updated_batch = STORE.record_devnet_submission(
            batch["sequence"],
            submission,
            sl_id,
            version,
        )
    except DevnetSubmitError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return submission, updated_batch


def _verify_submitted_operator_batch(
    *,
    batch: dict,
    submission: dict,
    sl_id: str,
    version: str,
    wait_for_verifier: bool,
    timeout_seconds: int,
    poll_interval_seconds: float,
) -> tuple[Optional[dict], dict]:
    updated_batch = batch
    verification = None
    if wait_for_verifier and _base_layer_api_url():
        verification = _sync_verifier_from_base_layer_api(
            sl_id,
            version,
            posting_owner=submission.get("owner"),
            expected_sequence=int(updated_batch["sequence"]),
            expected_state_hash=str(updated_batch["new_state_hash"]),
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        try:
            updated_batch = STORE.record_batch_verification(
                int(updated_batch["sequence"]),
                verification,
                sl_id,
                version,
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
    elif wait_for_verifier:
        verification = {
            "status": "skipped",
            "verified": False,
            "message": "BASE_LAYER_API_URL is not configured for verifier sync",
        }
    return verification, updated_batch


def _receipt_for_action(
    *,
    proposal_id: str,
    signed_intent: dict[str, Any],
    action: dict[str, Any],
    batch: dict,
    verification: Optional[dict],
    submission: Optional[dict],
    evidence: dict,
) -> dict:
    asset_ref = signed_intent["asset_ref"]
    address = action.get("owner") or action.get("trader")
    accepted = bool(batch.get("verified") and verification and verification.get("verified"))
    return {
        "proposal_id": proposal_id,
        "status": "accepted" if accepted else "submitted",
        "accepted": accepted,
        "action": signed_intent["action"],
        "sl_id": asset_ref["sl_id"],
        "version": asset_ref["version"],
        "asset_id": asset_ref["asset_id"],
        "pool_id": action.get("pool_id"),
        "leg_id": action.get("leg_id"),
        "address": address,
        "amount": int(action.get("amount") or 0),
        "signer": signed_intent["signer"],
        "nonce": int(signed_intent["nonce"]),
        "bundle_id": action.get("bundle_id"),
        "sequence": batch.get("sequence"),
        "state_hash": batch.get("new_state_hash"),
        "payload_hex": batch.get("payload_hex"),
        "data_scalars": batch.get("data_scalars") or [],
        "base_event_key": evidence.get("event_key"),
        "utxo_id": evidence.get("utxo_id") or (submission or {}).get("utxo_id"),
        "tx_hash": evidence.get("verification_tx_hash") or (submission or {}).get("tx_hash"),
        "output_index": evidence.get("verification_output_index") or (submission or {}).get("output_index"),
        "devnet_backed": bool(evidence.get("devnet_backed")),
        "verification": verification,
    }


def _batch_envelope(batch: dict) -> dict:
    return {
        "prev_state": batch["prev_state"],
        "sequence": batch["sequence"],
        "prev_state_hash": batch["prev_state_hash"],
        "new_state_hash": batch["new_state_hash"],
        "actions_applied": batch["actions_applied"],
        "payload_hex": batch["payload_hex"],
    }


def _active_runtime_config(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> Optional[dict]:
    if sl_id is not None or version is not None:
        layer_sl_id, layer_version = _layer_hex(sl_id, version)
        return STORE.load_sl_config(layer_sl_id, layer_version)

    runtimes = STORE.list_runtime_configs()
    return runtimes[0] if runtimes else STORE.load_sl_config()


def _active_semantic_layer_record(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> Optional[dict]:
    config = _active_runtime_config(sl_id, version)
    if config and config.get("sl_id"):
        record = STORE.get_semantic_layer(str(config["sl_id"]))
        if record and record.get("version") == config.get("version"):
            return record
    layer_sl_id, layer_version = _layer_hex(sl_id, version)
    record = STORE.get_semantic_layer(layer_sl_id)
    if record and record.get("version") == layer_version:
        return record
    return None


def _workbench_layer_coordinates(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> tuple[str, str]:
    if sl_id is not None or version is not None:
        return _layer_hex(sl_id, version)

    active_config = _active_runtime_config()
    if active_config:
        return str(active_config["sl_id"]), str(active_config["version"])

    layers = STORE.list_semantic_layers()
    if layers:
        return str(layers[0]["sl_id"]), str(layers[0]["version"])

    return core.SL_ID.hex(), core.VERSION.hex()


def _resolved_base_layer_account(
    record: Optional[dict],
    runtime_config: Optional[dict],
) -> Optional[dict]:
    account_id = (
        (record or {}).get("base_layer_account_id")
        or (runtime_config or {}).get("base_layer_account_id")
    )
    if account_id:
        account = STORE.get_base_layer_account(str(account_id))
        if account:
            return _public_base_layer_account(account)

    operator_address = (
        (record or {}).get("operator_wallet_address")
        or (runtime_config or {}).get("operator_wallet_address")
    )
    if not operator_address:
        return None

    return next(
        (
            _public_base_layer_account(account)
            for account in STORE.list_base_layer_accounts()
            if account.get("owner_wallet_address") == operator_address
            and account.get("purpose") == "sl_operator"
        ),
        None,
    )


def _balance_projection(
    address: str,
    state: core.State,
    source: str,
    sl_id: str,
    version: str,
    asset_id: str,
) -> dict:
    return {
        "address": address,
        "asset_id": asset_id,
        "balance": state.get_balance(address, asset_id),
        "frozen": state.is_frozen(address, asset_id),
        "source": source,
        "state_hash": state.state_hash(),
        "sl_id": sl_id,
        "version": version,
    }


def _latest_payload_projection(batch: Optional[dict], sl_id: str, version: str) -> Optional[dict]:
    if not batch:
        return None
    return {
        "sequence": batch["sequence"],
        "payload_hex": batch["payload_hex"],
        "payload_size": batch["payload_size"],
        "data_scalars": batch["data_scalars"],
        "data_len": batch["data_len"],
        "sl_id": sl_id,
        "version": version,
    }


def _config_response() -> dict:
    active_config = _active_runtime_config()
    active_sl_id = str(active_config["sl_id"]) if active_config else core.SL_ID.hex()
    active_version = str(active_config["version"]) if active_config else core.VERSION.hex()
    response = {
        "initialized": bool(active_config),
        "sl_id": active_sl_id,
        "version": active_version,
        "runtimes": STORE.list_runtime_configs(),
        "storage": {
            "type": "sqlite",
            "db_path": str(STORE.db_path),
        },
    }
    if active_config:
        operator_state = STORE.load_operator_state(active_sl_id, active_version)
        response["issuer_vk"] = active_config["issuer_vk"]
        response["operator_state_hash"] = operator_state.state_hash() if operator_state else None
        response["next_batch_sequence"] = STORE.next_batch_sequence(active_sl_id, active_version)
    return response


def _devnet_runtime_status(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> dict:
    status = command_devnet_status()
    active_record = _active_semantic_layer_record(sl_id, version)
    active_config = _active_runtime_config(sl_id, version)
    active_account_id = (
        (active_record or {}).get("base_layer_account_id")
        or (active_config or {}).get("base_layer_account_id")
    )
    vault_ready = vault_configured()
    pool_counts = STORE.base_layer_account_pool_counts()
    account_generator_ready = bool(vault_ready and pool_counts["available"] > 0)
    base_layer_api_recipient_ready = bool(
        status.get("base_layer_transfer_recipient_configured")
        or status.get("base_layer_wallet_address")
    )
    account_ready = bool(
        status.get("wallet_file_configured")
        or base_layer_api_recipient_ready
        or (active_account_id and vault_ready)
    )
    submitter_ready = bool(status.get("submitter_configured", status.get("enabled")))

    status.update(
        {
            "account_vault_configured": vault_ready,
            "account_generator": "configured" if account_generator_ready else None,
            "account_generator_configured": account_generator_ready,
            "base_layer_account_count": STORE.base_layer_account_count(),
            "active_semantic_layer_id": (
                (active_record or active_config or {}).get("sl_id")
            ),
            "active_base_layer_account_id": active_account_id,
            "account_configured": account_ready,
            "ready": submitter_ready and account_ready,
            "enabled": submitter_ready and account_ready,
        }
    )
    return status


def _submission_account_json(
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    active_record = _active_semantic_layer_record(sl_id, version)
    active_config = _active_runtime_config(sl_id, version)
    if not active_record and not active_config:
        return None

    account_id = (
        (active_record or {}).get("base_layer_account_id")
        or (active_config or {}).get("base_layer_account_id")
    )
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


def _accept_envelope(
    envelope: dict,
    sl_id: Optional[str] = None,
    version: Optional[str] = None,
) -> tuple[bool, str]:
    layer_sl_id, layer_version = _layer_hex(sl_id, version)
    result = _verifier_engine().accept_envelope(
        _payment_plugin(layer_sl_id, layer_version),
        envelope,
    )
    if result["accepted"]:
        return True, "accepted"
    return False, result["message"]


def _base_layer_api_url() -> Optional[str]:
    for key in ("BASE_LAYER_API_URL", "EON_BASE_LAYER_API_URL"):
        value = os.environ.get(key)
        if value and value.strip():
            return value.strip()
    return None


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _checkpoint_response(sl_id: str, version: str) -> Optional[dict]:
    checkpoint = _verifier_store().load_checkpoint(
        bytes.fromhex(sl_id),
        bytes.fromhex(version),
    )
    if checkpoint is None:
        return None
    plugin = _payment_plugin(sl_id, version)
    state = plugin.state_from_dict(checkpoint["state"])
    return {
        "sequence": checkpoint["sequence"],
        "state_hash": plugin.state_hash(state),
        "state": checkpoint["state"],
    }


def _checkpoint_matches(
    checkpoint: Optional[dict],
    expected_sequence: Optional[int],
    expected_state_hash: Optional[str],
) -> bool:
    if checkpoint is None:
        return False
    if expected_sequence is not None and int(checkpoint["sequence"]) < expected_sequence:
        return False
    if expected_state_hash and checkpoint["state_hash"] != expected_state_hash:
        return False
    return True


def _event_evidence(entry: Optional[dict], batch: Optional[dict] = None) -> dict:
    entry = entry or {}
    submission = (batch or {}).get("devnet_submission") or {}
    event_key = str(entry.get("event_key") or entry.get("cursor") or "")
    parts = event_key.split(":")
    parsed_tx_hash = ""
    parsed_output_index = ""
    if len(parts) >= 4 and parts[0] == "devnet":
        if parts[1] == "utxo":
            parsed_tx_hash = parts[2]
            parsed_output_index = parts[3]
        else:
            parsed_tx_hash = parts[2]
            parsed_output_index = parts[3]

    tx_hash = str(entry.get("tx_hash") or parsed_tx_hash or "")
    output_index = entry.get("output_index", parsed_output_index)
    output_index_text = "" if output_index is None else str(output_index)
    utxo_id = str(entry.get("utxo_id") or parsed_tx_hash or "")
    submission_tx_hash = str(submission.get("tx_hash") or "")
    source = "devnet_utxo" if event_key or tx_hash or utxo_id else "local_replay"

    return {
        "verification_source": source,
        "verification_label": "Devnet UTXO" if source == "devnet_utxo" else "Local replay",
        "event_key": event_key or None,
        "utxo_id": utxo_id or None,
        "verification_tx_hash": tx_hash or None,
        "verification_output_index": output_index_text or None,
        "submission_tx_hash": submission_tx_hash or None,
        "submission_output_index": submission.get("output_index"),
        "submission_amount": submission.get("amount"),
        "devnet_backed": source == "devnet_utxo",
        "verified_at": entry.get("created_at"),
    }


def _accepted_log_by_sequence(sl_id: str, version: str) -> dict[int, dict]:
    entries = _verifier_store().list_verification_log(
        bytes.fromhex(sl_id),
        bytes.fromhex(version),
    )
    accepted: dict[int, dict] = {}
    for entry in entries:
        sequence = entry.get("sequence")
        if entry.get("verdict") == "accepted" and sequence is not None:
            accepted[int(sequence)] = entry
    return accepted


def _verification_log_entry_for_batch(sl_id: str, version: str, batch: dict) -> Optional[dict]:
    entries = _verifier_store().list_verification_log(
        bytes.fromhex(sl_id),
        bytes.fromhex(version),
    )
    sequence = int(batch["sequence"])
    prev_hash = str(batch["prev_state_hash"])
    new_hash = str(batch["new_state_hash"])
    matches = [
        entry
        for entry in entries
        if entry.get("sequence") is not None
        and int(entry["sequence"]) == sequence
        and entry.get("prev_state_hash") == prev_hash
        and entry.get("new_state_hash") == new_hash
    ]
    return matches[-1] if matches else None


def _sync_operator_to_verifier_checkpoint(sl_id: str, version: str) -> Optional[dict]:
    checkpoint = _verifier_store().load_checkpoint(
        bytes.fromhex(sl_id),
        bytes.fromhex(version),
    )
    if checkpoint is None:
        return None

    plugin = _payment_plugin(sl_id, version)
    verifier_state = plugin.state_from_dict(checkpoint["state"])
    next_sequence = int(checkpoint["sequence"]) + 1
    STORE.sync_operator_state_from_verifier(
        verifier_state,
        next_sequence,
        sl_id,
        version,
    )
    STORE.delete_operator_batches_after(int(checkpoint["sequence"]), sl_id, version)
    return {
        "synced": True,
        "message": "operator state synced from verifier checkpoint",
        "operator_state": _state_to_response(verifier_state),
        "operator_checkpoint": _operator_checkpoint_status(sl_id, version),
        "checkpoint_sequence": int(checkpoint["sequence"]),
        "checkpoint_state_hash": checkpoint["state_hash"],
    }


def _rollback_operator_batch_after_rejection(sl_id: str, version: str, batch: dict) -> Optional[dict]:
    entry = _verification_log_entry_for_batch(sl_id, version, batch)
    if not entry or entry.get("verdict") != "rejected":
        return None

    STORE.delete_operator_batch(int(batch["sequence"]), sl_id, version)
    sync = _sync_operator_to_verifier_checkpoint(sl_id, version)
    return {
        "rolled_back": True,
        "reason": entry.get("message") or "verifier rejected operator batch",
        "rejected_event_key": entry.get("event_key"),
        "sequence": int(batch["sequence"]),
        "sync": sync,
    }


def _batch_with_evidence(batch: dict, accepted_log: Optional[dict[int, dict]] = None) -> dict:
    record = {**batch}
    sequence = int(record["sequence"])
    entry = (accepted_log or {}).get(sequence)
    verified = bool(
        entry
        and entry.get("new_state_hash") == record.get("new_state_hash")
        and entry.get("prev_state_hash") == record.get("prev_state_hash")
    )
    record["verified"] = verified
    record["effective_status"] = "verified" if verified else record.get("status", "batched")
    if verified:
        record.update(_event_evidence(entry, record))
    else:
        record.update(
            {
                "verification_source": None,
                "verification_label": "Not verified",
                "event_key": None,
                "utxo_id": (record.get("devnet_submission") or {}).get("utxo_id"),
                "verification_tx_hash": None,
                "verification_output_index": None,
                "submission_tx_hash": (record.get("devnet_submission") or {}).get("tx_hash"),
                "submission_output_index": (record.get("devnet_submission") or {}).get("output_index"),
                "submission_amount": (record.get("devnet_submission") or {}).get("amount"),
                "devnet_backed": False,
                "verified_at": None,
            }
        )
    return record


def _batches_with_evidence(batches: list[dict], sl_id: str, version: str) -> list[dict]:
    accepted_log = _accepted_log_by_sequence(sl_id, version)
    return [_batch_with_evidence(batch, accepted_log) for batch in batches]


def _sync_verifier_from_base_layer_api(
    sl_id: str,
    version: str,
    *,
    posting_owner: Optional[str] = None,
    expected_sequence: Optional[int] = None,
    expected_state_hash: Optional[str] = None,
    timeout_seconds: int = 0,
    poll_interval_seconds: float = 5,
) -> dict:
    base_url = _base_layer_api_url()
    if not base_url:
        raise DevnetSubmitError("BASE_LAYER_API_URL is required for verifier sync")

    started = time.monotonic()
    deadline = started + max(0, timeout_seconds)
    attempts: list[dict] = []
    layer_source = f"base-layer-api:v2:{sl_id}:{version}"
    checkpoint = _checkpoint_response(sl_id, version)

    while True:
        try:
            source = BaseLayerAPIEventSource(
                base_url,
                owner=posting_owner,
                network_id="devnet",
            )
            sync_result = _verifier_engine().sync_from_source(
                source,
                layer_source,
                retry_rejected=bool(expected_sequence or expected_state_hash),
            )
        except Exception as e:
            sync_result = {"error": str(e), "events": []}

        checkpoint = _checkpoint_response(sl_id, version)
        advanced = _checkpoint_matches(
            checkpoint,
            expected_sequence,
            expected_state_hash,
        )
        accepted_events = [
            event for event in sync_result.get("events", [])
            if event.get("accepted")
        ]
        attempts.append({
            "accepted": len(accepted_events),
            "event_count": len(sync_result.get("events", [])),
            "error": sync_result.get("error"),
            "checkpoint_sequence": (
                checkpoint["sequence"] if checkpoint is not None else None
            ),
            "checkpoint_state_hash": (
                checkpoint["state_hash"] if checkpoint is not None else None
            ),
        })

        if advanced:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(min(poll_interval_seconds, max(0, deadline - time.monotonic())))

    elapsed_ms = int((time.monotonic() - started) * 1000)
    status = "verified" if _checkpoint_matches(
        checkpoint,
        expected_sequence,
        expected_state_hash,
    ) else "timeout"
    return {
        "status": status,
        "verified": status == "verified",
        "source": layer_source,
        "posting_owner": posting_owner,
        "expected_sequence": expected_sequence,
        "expected_state_hash": expected_state_hash,
        "checkpoint": checkpoint,
        "attempts": attempts,
        "elapsed_ms": elapsed_ms,
        "timeout_seconds": timeout_seconds,
        "poll_interval_seconds": poll_interval_seconds,
    }


def _sync_all_verifier_layers_once() -> dict:
    if not _base_layer_api_url():
        return {
            "status": "skipped",
            "message": "BASE_LAYER_API_URL is not configured",
            "layers": [],
        }

    layers = STORE.list_runtime_configs()
    results = []
    for runtime in layers:
        sl_id = str(runtime["sl_id"])
        version = str(runtime["version"])
        try:
            result = _sync_verifier_from_base_layer_api(sl_id, version, timeout_seconds=0)
            updated_batch = _record_verification_for_checkpoint_batch(sl_id, version, result)
            if updated_batch is not None:
                result["batch"] = _batch_with_evidence(
                    updated_batch,
                    _accepted_log_by_sequence(sl_id, version),
                )
        except Exception as e:
            result = {
                "status": "error",
                "verified": False,
                "sl_id": sl_id,
                "version": version,
                "message": str(e),
            }
        results.append({"sl_id": sl_id, "version": version, "result": result})

    accepted = sum(
        attempt.get("accepted", 0)
        for entry in results
        for attempt in entry.get("result", {}).get("attempts", [])
        if isinstance(attempt, dict)
    )
    return {
        "status": "indexed",
        "layer_count": len(layers),
        "accepted": accepted,
        "layers": results,
    }


def _notify_payload_sequence(event: Dict[str, Any]) -> int:
    data_scalars = event.get("data") or event.get("data_scalars") or []
    try:
        payload = scalar_hex_to_payload_bytes(data_scalars)
    except Exception:
        return 2**63 - 1
    if len(payload) < 14:
        return 2**63 - 1
    sl_id, _version = payload_header(payload)
    if sl_id == BUNDLE_SL_ID:
        sequences: list[int] = []
        try:
            bundle = decode_bundle_payload(payload)
        except Exception:
            return 2**63 - 1
        for child in bundle.children:
            try:
                sequences.append(decode_transition_payload(child).sequence)
            except Exception:
                continue
        return min(sequences) if sequences else 2**63 - 1
    return int.from_bytes(payload[6:14], "big")


def _notify_event_sort_key(event: Dict[str, Any]) -> tuple[int, int, int, int, str]:
    return (
        int(event.get("height", 0)),
        int(event.get("tx_index", 0)),
        _notify_payload_sequence(event),
        int(event.get("output_index", 0)),
        str(event.get("event_key") or event.get("utxo_id") or event.get("tx_hash") or ""),
    )


def _event_matches_hint(event: Dict[str, Any], hint: Optional[Dict[str, Any]]) -> bool:
    if not hint:
        return False

    event_key = str(event.get("event_key") or "")
    hint_event_key = str(hint.get("event_key") or "")
    if event_key and hint_event_key and event_key == hint_event_key:
        return True

    event_data = event.get("data") or event.get("data_scalars") or []
    hint_data = hint.get("data") or hint.get("data_scalars") or []
    if not isinstance(event_data, list) or not isinstance(hint_data, list):
        return False
    if [str(item) for item in event_data] != [str(item) for item in hint_data]:
        return False

    event_tx = str(event.get("tx_hash") or event.get("transaction_hash") or "")
    hint_tx = str(hint.get("tx_hash") or hint.get("transaction_hash") or "")
    event_utxo = str(event.get("utxo_id") or "")
    hint_utxo = str(hint.get("utxo_id") or hint.get("id") or "")
    return not hint_tx or event_tx == hint_tx or event_utxo == hint_tx or event_utxo == hint_utxo


def _sync_supplied_notify_events(
    source_events: list[Dict[str, Any]],
    source_event: Optional[Dict[str, Any]],
) -> tuple[dict, Optional[Dict[str, Any]]]:
    if not source_events:
        return {"events": []}, None

    deduped: dict[str, Dict[str, Any]] = {}
    target_event: Optional[Dict[str, Any]] = None
    for event in source_events:
        if not isinstance(event, dict):
            continue
        key = str(event.get("event_key") or event.get("cursor") or event.get("utxo_id") or event.get("tx_hash") or len(deduped))
        deduped[key] = event
        if target_event is None and _event_matches_hint(event, source_event):
            target_event = event

    results = []
    for event in sorted(deduped.values(), key=_notify_event_sort_key):
        results.append(_verifier_engine().ingest_event(event, retry_rejected=True))

    return {
        "source": "supplied-source-events",
        "previous_cursor": None,
        "latest_cursor": None,
        "events": results,
    }, target_event


def _notify_verifier_from_source_event(
    source_event: Optional[Dict[str, Any]],
    source_events: Optional[list[Dict[str, Any]]] = None,
) -> dict:
    if not source_event and not source_events:
        return _sync_all_verifier_layers_once()

    base_url = _base_layer_api_url()
    owner = source_event.get("owner") if source_event else None
    target_event: Dict[str, Any] | None = None
    sync_result: dict = {"events": []}

    if source_events:
        sync_result, target_event = _sync_supplied_notify_events(source_events, source_event)

    if not source_events and base_url and owner:
        source = BaseLayerAPIEventSource(
            base_url,
            owner=str(owner),
            network_id=str(source_event.get("network_id") or "devnet"),
        )
        target_event = source.event_for_hint(source_event)
        target_hint = (
            (target_event or {}).get("event_key")
            or source_event.get("event_key")
            or source_event.get("tx_hash")
            or "unknown"
        )
        sync_result = _verifier_engine().sync_from_source(
            source,
            f"base-layer-api:notify:v2:{owner}:{target_hint}",
            retry_rejected=True,
        )

    if target_event is None and source_event is not None:
        target_event = dict(source_event)

    target_key = str(target_event.get("event_key") or "")
    matching_results = [
        event for event in sync_result.get("events", [])
        if target_key and event.get("event_key") == target_key
    ]
    if matching_results:
        target_result = matching_results[-1]
    elif target_event is not None:
        target_result = _verifier_engine().ingest_event(target_event, retry_rejected=True)
    else:
        target_result = {"accepted": False, "ignored": True, "message": "no target event supplied"}

    target_accepted = bool(target_result.get("accepted"))
    if target_key and not target_accepted:
        target_accepted = _verifier_store().has_accepted_verification(target_key)

    accepted_count = sum(
        1 for event in sync_result.get("events", [])
        if isinstance(event, dict) and event.get("accepted")
    )
    if target_accepted and not any(
        target_key and event.get("event_key") == target_key and event.get("accepted")
        for event in sync_result.get("events", [])
        if isinstance(event, dict)
    ):
        accepted_count += 1

    return {
        "status": "notified",
        "accepted": target_accepted,
        "accepted_count": accepted_count,
        "target_event": target_event,
        "target_result": target_result,
        "sync_result": sync_result,
        "source_event_count": len(source_events or []),
    }


def _verifier_poll_loop() -> None:
    interval = _env_float("EON_VERIFIER_POLL_INTERVAL_SECONDS", 5.0)
    while not VERIFIER_POLL_STOP.wait(interval):
        try:
            with STATE_LOCK:
                _sync_all_verifier_layers_once()
        except Exception:
            continue


@app.on_event("startup")
def start_verifier_polling() -> None:
    global VERIFIER_POLL_THREAD
    if not _env_bool("EON_VERIFIER_POLL_ENABLED", False):
        return
    if not _base_layer_api_url():
        return
    if VERIFIER_POLL_THREAD and VERIFIER_POLL_THREAD.is_alive():
        return
    VERIFIER_POLL_STOP.clear()
    VERIFIER_POLL_THREAD = Thread(
        target=_verifier_poll_loop,
        name="payment-sl-verifier-poller",
        daemon=True,
    )
    VERIFIER_POLL_THREAD.start()


@app.on_event("shutdown")
def stop_verifier_polling() -> None:
    VERIFIER_POLL_STOP.set()
    if VERIFIER_POLL_THREAD and VERIFIER_POLL_THREAD.is_alive():
        VERIFIER_POLL_THREAD.join(timeout=2)


def _record_verification_for_checkpoint_batch(
    sl_id: str,
    version: str,
    verification: dict,
) -> Optional[dict]:
    checkpoint = verification.get("checkpoint") or {}
    if not verification.get("verified") or not checkpoint:
        return None

    checkpoint_sequence = int(checkpoint["sequence"])
    checkpoint_hash = str(checkpoint["state_hash"])
    matching_batch = None
    for batch in STORE.list_batches(sl_id, version):
        if int(batch["sequence"]) == checkpoint_sequence:
            matching_batch = batch
            break
    if matching_batch is None or matching_batch.get("new_state_hash") != checkpoint_hash:
        return None

    return STORE.record_batch_verification(
        checkpoint_sequence,
        verification,
        sl_id,
        version,
    )


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
    return _config_response()


@app.post("/operator/init")
def operator_init(request: InitRequest) -> dict:
    with STATE_LOCK:
        sl_id = _sl_id_bytes(request.sl_id).hex()
        version = _version_hex(request.version)
        operator_wallet_address = (
            _validate_address(request.operator_wallet_address)
            if request.operator_wallet_address
            else None
        )
        base_layer_account_id = (
            request.base_layer_account_id.strip()
            if request.base_layer_account_id
            else None
        )
        record = STORE.get_semantic_layer(sl_id)
        if record and record.get("version") == version:
            operator_wallet_address = operator_wallet_address or record.get("operator_wallet_address")
            base_layer_account_id = base_layer_account_id or record.get("base_layer_account_id")

        if operator_wallet_address:
            operator_wallet = STORE.get_wallet(operator_wallet_address)
            if not operator_wallet:
                raise HTTPException(status_code=400, detail="operator wallet is not registered")
            if operator_wallet.get("kind") != "sl_operator":
                raise HTTPException(status_code=400, detail="operator wallet must use kind=sl_operator")

        if base_layer_account_id:
            base_layer_account = STORE.get_base_layer_account(base_layer_account_id)
            if not base_layer_account:
                raise HTTPException(status_code=400, detail="base-layer account is not registered")
            if (
                operator_wallet_address
                and base_layer_account["owner_wallet_address"] != operator_wallet_address
            ):
                raise HTTPException(
                    status_code=400,
                    detail="base-layer account must belong to operator wallet",
                )

        if _initialized(sl_id, version):
            if not request.reset_existing:
                raise HTTPException(
                    status_code=409,
                    detail="SL runtime is already initialized. Use reset_existing=true only for a scoped runtime rebuild.",
                )
            _verifier_store().reset_layer(bytes.fromhex(sl_id), bytes.fromhex(version))

        config_obj = {
            "issuer_vk": request.issuer_vk,
            "sl_id": sl_id,
            "version": version,
            "operator_wallet_address": operator_wallet_address,
            "base_layer_account_id": base_layer_account_id,
        }
        genesis = STORE.initialize(
            request.issuer_vk,
            sl_id=sl_id,
            version=version,
            operator_wallet_address=operator_wallet_address,
            base_layer_account_id=base_layer_account_id,
            assets=(record or {}).get("assets", []),
            reset_existing=request.reset_existing,
        )

        return {
            "initialized": True,
            "config": config_obj,
            "operator_state": _state_to_response(genesis),
        }


@app.get("/operator/state")
def operator_state(
    sl_id: Optional[str] = Query(default=None),
    version: Optional[str] = Query(default=None),
) -> dict:
    layer_sl_id, layer_version = _layer_hex(sl_id, version)
    state = _operator_state(layer_sl_id, layer_version)
    return {
        "state": _state_to_response(state),
        "pending_count": STORE.pending_count(layer_sl_id, layer_version),
        "next_batch_sequence": STORE.next_batch_sequence(layer_sl_id, layer_version),
        "sl_id": layer_sl_id,
        "version": layer_version,
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
        return _store_base_layer_account(
            request.label,
            request.owner_wallet_address,
            request.account_json,
            request.eon_address,
            request.purpose,
        )


@app.get("/base-layer/accounts")
def list_base_layer_accounts() -> dict:
    return {
        "accounts": [
            _public_base_layer_account(record)
            for record in STORE.list_base_layer_accounts()
        ]
    }


@app.post("/base-layer/account-pool", include_in_schema=False)
def import_base_layer_pool_account(request: BaseLayerAccountPoolRequest) -> dict:
    with STATE_LOCK:
        return _store_base_layer_pool_account(request)


@app.get("/base-layer/account-pool", include_in_schema=False)
def list_base_layer_pool_accounts() -> dict:
    return {
        "accounts": [
            _public_base_layer_pool_account(record)
            for record in STORE.list_base_layer_pool_accounts()
        ],
        "counts": STORE.base_layer_account_pool_counts(),
    }


def _allocate_base_layer_account_for_wallet(
    request: BaseLayerAccountAllocateRequest,
) -> dict:
    with STATE_LOCK:
        owner_address = _validate_address(request.owner_wallet_address)
        owner_wallet = STORE.get_wallet(owner_address)
        if not owner_wallet:
            raise HTTPException(status_code=400, detail="owner wallet is not registered")
        account_purpose = _resolve_base_layer_account_purpose(
            owner_wallet,
            request.purpose,
        )

        account = STORE.allocate_base_layer_account(
            owner_address,
            account_purpose,
            request.label.strip() if request.label else None,
        )
        if not account:
            raise HTTPException(
                status_code=409,
                detail="base-layer account generation is temporarily unavailable",
            )
        return _public_base_layer_account(account)


@app.post("/base-layer/accounts/generate")
def generate_base_layer_account_for_wallet(
    request: BaseLayerAccountGenerateRequest,
) -> dict:
    return _allocate_base_layer_account_for_wallet(request)


@app.post("/base-layer/accounts/allocate", include_in_schema=False)
def allocate_base_layer_account(request: BaseLayerAccountAllocateRequest) -> dict:
    return _allocate_base_layer_account_for_wallet(request)


@app.post("/semantic-layers")
def register_semantic_layer(request: SemanticLayerRequest) -> dict:
    with STATE_LOCK:
        sl_id = _sl_id_bytes(request.sl_id).hex()
        version = _version_hex(request.version)
        previous_record = STORE.get_semantic_layer(sl_id)
        previous_asset_ids = {
            asset["asset_id"]
            for asset in (previous_record or {}).get("assets", [])
            if isinstance(asset, dict) and asset.get("asset_id")
        }
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
            if base_layer_account.get("purpose") != "sl_operator":
                raise HTTPException(
                    status_code=400,
                    detail="semantic-layer base account must use purpose=sl_operator",
                )

        fields_set = (
            request.model_fields_set
            if hasattr(request, "model_fields_set")
            else getattr(request, "__fields_set__", set())
        )
        assets = (
            [_asset_to_record(asset) for asset in request.assets]
            if "assets" in fields_set
            else list((previous_record or {}).get("assets", []))
        )
        record = {
            "name": request.name.strip(),
            "sl_id": sl_id,
            "version": version,
            "operator_wallet_address": operator_address,
            "base_layer_account_id": base_layer_account_id,
            "issuer_vk_ref": request.issuer_vk_ref,
            "operator_vk_ref": request.operator_vk_ref,
            "assets": assets,
        }
        STORE.upsert_semantic_layer(record)
        queued = []
        for asset in record["assets"]:
            if asset["asset_id"] in previous_asset_ids:
                continue
            queued_registration = _queue_asset_registration_if_runtime_exists(asset, sl_id, version)
            if queued_registration:
                queued.append(queued_registration["action"])
        if queued:
            record["queued_asset_registrations"] = queued
        return record


@app.post("/semantic-layers/{sl_id}/assets")
def register_semantic_layer_asset(
    sl_id: str,
    request: SemanticLayerAssetRequest,
    version: str = Query(default=core.VERSION.hex()),
) -> dict:
    with STATE_LOCK:
        layer_sl_id = _sl_id_bytes(sl_id).hex()
        layer_version = _version_hex(version)
        asset = _asset_to_record(request)
        try:
            record = STORE.append_semantic_layer_asset(layer_sl_id, layer_version, asset)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

        queued_registration = _queue_asset_registration_if_runtime_exists(
            asset,
            layer_sl_id,
            layer_version,
        )
        return {
            "asset": asset,
            "semantic_layer": record,
            "queued_registration": queued_registration["action"] if queued_registration else None,
            "sl_id": layer_sl_id,
            "version": layer_version,
        }


@app.get("/semantic-layers")
def list_semantic_layers() -> dict:
    return {"semantic_layers": STORE.list_semantic_layers()}


@app.get("/semantic-layers/workbench-state")
def semantic_layer_workbench_state(
    sl_id: Optional[str] = Query(default=None),
    version: Optional[str] = Query(default=None),
    wallet_address: list[str] = Query(default_factory=list),
) -> dict:
    layer_sl_id, layer_version = _workbench_layer_coordinates(sl_id, version)
    semantic_layers = STORE.list_semantic_layers()
    base_layer_accounts = [
        _public_base_layer_account(record)
        for record in STORE.list_base_layer_accounts()
    ]
    server_wallets = STORE.list_wallets()
    runtime_config = STORE.load_sl_config(layer_sl_id, layer_version)
    record = next(
        (
            layer
            for layer in semantic_layers
            if layer["sl_id"] == layer_sl_id and layer["version"] == layer_version
        ),
        None,
    )
    operator_state = STORE.load_operator_state(layer_sl_id, layer_version)
    verifier_state = None
    verifier_sl_id_bytes = bytes.fromhex(layer_sl_id)
    verifier_version_bytes = bytes.fromhex(layer_version)

    try:
        verifier_state, verifier_sl_id_bytes, verifier_version_bytes = _verified_state_for_layer(
            layer_sl_id,
            layer_version,
        )
    except HTTPException:
        verifier_state = None

    pending_actions = STORE.load_pending(layer_sl_id, layer_version) if runtime_config else []
    raw_batches = STORE.list_batches(layer_sl_id, layer_version) if runtime_config else []
    batches = _batches_with_evidence(raw_batches, layer_sl_id, layer_version) if runtime_config else []
    latest_batch = batches[-1] if batches else None
    operator_checkpoint = _operator_checkpoint_status(layer_sl_id, layer_version) if runtime_config else None
    verifier_log = _verifier_store().list_verification_log(
        verifier_sl_id_bytes,
        verifier_version_bytes,
    )
    base_layer_account = _resolved_base_layer_account(record, runtime_config)
    operator_wallet_address = (
        (record or {}).get("operator_wallet_address")
        or (runtime_config or {}).get("operator_wallet_address")
    )
    operator_wallet = STORE.get_wallet(operator_wallet_address) if operator_wallet_address else None
    effective_record = {
        "name": (record or {}).get("name") or f"SL {layer_sl_id}",
        "sl_id": layer_sl_id,
        "version": layer_version,
        "operator_wallet_address": operator_wallet_address,
        "base_layer_account_id": (
            (record or {}).get("base_layer_account_id")
            or (runtime_config or {}).get("base_layer_account_id")
            or (base_layer_account or {}).get("id")
        ),
        "issuer_vk_ref": (record or {}).get("issuer_vk_ref"),
        "operator_vk_ref": (record or {}).get("operator_vk_ref"),
        "assets": _effective_assets(record, operator_state),
        "created_at": (record or {}).get("created_at"),
        "updated_at": (record or {}).get("updated_at"),
    }
    asset_id = _resolve_asset_id(layer_sl_id, layer_version)
    balance_addresses = {wallet["address"] for wallet in server_wallets}
    balance_addresses.update(_validate_address(address) for address in wallet_address)
    balances = {}
    for address in sorted(balance_addresses):
        pair = {}
        if operator_state is not None:
            pair["operator"] = _balance_projection(
                address,
                operator_state,
                "operator",
                layer_sl_id,
                layer_version,
                asset_id,
            )
        if verifier_state is not None:
            pair["verifier"] = _balance_projection(
                address,
                verifier_state,
                "verifier",
                layer_sl_id,
                layer_version,
                asset_id,
            )
        balances[address] = pair

    return {
        "health": health(),
        "config": _config_response(),
        "server_wallets": server_wallets,
        "semantic_layers": semantic_layers,
        "base_layer_accounts": base_layer_accounts,
        "devnet_status": _devnet_runtime_status(layer_sl_id, layer_version),
        "selected_layer": {
            "sl_id": layer_sl_id,
            "version": layer_version,
            "record": record,
            "effective_record": effective_record,
            "operator_wallet": operator_wallet,
            "base_layer_account": base_layer_account,
            "assets": effective_record["assets"],
            "runtime_config": runtime_config,
            "runtime_initialized": bool(runtime_config and operator_state),
            "signer_status": (
                "bound"
                if (record or {}).get("base_layer_account_id")
                else "ready" if base_layer_account else "missing"
            ),
        },
        "runtime": {
            "operator_state": (
                {
                    "state": _state_to_response(operator_state),
                    "pending_count": len(pending_actions),
                    "next_batch_sequence": STORE.next_batch_sequence(layer_sl_id, layer_version),
                    "checkpoint": operator_checkpoint,
                    "sl_id": layer_sl_id,
                    "version": layer_version,
                }
                if operator_state is not None
                else None
            ),
            "verifier_state": (
                {
                    "sl_id": verifier_sl_id_bytes.hex(),
                    "version": verifier_version_bytes.hex(),
                    "state": _state_to_response(verifier_state),
                    "accepted_payloads": len(
                        [entry for entry in verifier_log if entry.get("verdict") == "accepted"]
                    ),
                }
                if verifier_state is not None
                else None
            ),
            "pending_actions": pending_actions,
            "batches": batches,
            "latest_payload": _latest_payload_projection(latest_batch, layer_sl_id, layer_version),
            "verifier_log": verifier_log,
            "operator_checkpoint": operator_checkpoint,
            "balances": balances,
        },
    }


@app.get("/balances/{address}")
def get_balance(
    address: str,
    source: str = Query(default="verifier", pattern="^(verifier|operator)$"),
    sl_id: Optional[str] = Query(default=None),
    version: Optional[str] = Query(default=None),
    asset_id: Optional[str] = Query(default=None),
) -> dict:
    addr = _validate_address(address)
    if source == "verifier":
        state, resolved_sl_id, resolved_version = _verified_state_for_layer(sl_id, version)
    else:
        state, resolved_sl_id, resolved_version = _operator_state_for_layer(sl_id, version)
    resolved_asset_id = _resolve_asset_id(
        resolved_sl_id.hex(),
        resolved_version.hex(),
        asset_id,
    )
    return {
        "address": addr,
        "asset_id": resolved_asset_id,
        "balance": state.get_balance(addr, resolved_asset_id),
        "frozen": state.is_frozen(addr, resolved_asset_id),
        "source": source,
        "state_hash": state.state_hash(),
        "sl_id": resolved_sl_id.hex(),
        "version": resolved_version.hex(),
    }


@app.post("/actions/mint")
def mint(request: AmountToRequest) -> dict:
    with STATE_LOCK:
        sl_id, version = _layer_hex(request.sl_id, request.version)
        to_address = _validate_address(request.to_address)
        asset_id = _resolve_asset_id(sl_id, version, request.asset_id)
        action = {
            "type": core.ActionType.MINT.value,
            "sender_vk": _issuer_vk(sl_id, version),
            "nonce": _next_nonce(sl_id, version),
            "asset_id": asset_id,
            "to": to_address,
            "amount": request.amount,
        }
        return _queue_action(action, sl_id, version)


@app.post("/actions/burn")
def burn(request: AmountFromRequest) -> dict:
    with STATE_LOCK:
        sl_id, version = _layer_hex(request.sl_id, request.version)
        from_address = _validate_address(request.from_address)
        asset_id = _resolve_asset_id(sl_id, version, request.asset_id)
        action = {
            "type": core.ActionType.BURN.value,
            "sender_vk": _issuer_vk(sl_id, version),
            "nonce": _next_nonce(sl_id, version),
            "asset_id": asset_id,
            "from_addr": from_address,
            "amount": request.amount,
        }
        return _queue_action(action, sl_id, version)


@app.post("/actions/freeze")
def freeze(request: TargetRequest) -> dict:
    with STATE_LOCK:
        sl_id, version = _layer_hex(request.sl_id, request.version)
        target_address = _validate_address(request.target_address)
        asset_id = _resolve_asset_id(sl_id, version, request.asset_id)
        action = {
            "type": core.ActionType.FREEZE.value,
            "sender_vk": _issuer_vk(sl_id, version),
            "nonce": _next_nonce(sl_id, version),
            "asset_id": asset_id,
            "target": target_address,
        }
        return _queue_action(action, sl_id, version)


@app.post("/actions/unfreeze")
def unfreeze(request: TargetRequest) -> dict:
    with STATE_LOCK:
        sl_id, version = _layer_hex(request.sl_id, request.version)
        target_address = _validate_address(request.target_address)
        asset_id = _resolve_asset_id(sl_id, version, request.asset_id)
        action = {
            "type": core.ActionType.UNFREEZE.value,
            "sender_vk": _issuer_vk(sl_id, version),
            "nonce": _next_nonce(sl_id, version),
            "asset_id": asset_id,
            "target": target_address,
        }
        return _queue_action(action, sl_id, version)


@app.post("/actions/transfer")
def transfer(request: TransferRequest) -> dict:
    with STATE_LOCK:
        sl_id, version = _layer_hex(request.sl_id, request.version)
        from_address = _validate_address(request.from_address)
        to_address = _validate_address(request.to_address)
        asset_id = _resolve_asset_id(sl_id, version, request.asset_id)
        if core.hash_vk(request.vk) != from_address:
            raise HTTPException(status_code=400, detail="vk does not match from_address")

        action = {
            "type": core.ActionType.TRANSFER.value,
            "sender_vk": request.vk,
            "nonce": _next_nonce(sl_id, version),
            "asset_id": asset_id,
            "from_addr": from_address,
            "to": to_address,
            "amount": request.amount,
        }
        return _queue_action(action, sl_id, version)


@app.get("/pending")
def pending(
    sl_id: Optional[str] = Query(default=None),
    version: Optional[str] = Query(default=None),
) -> dict:
    layer_sl_id, layer_version = _layer_hex(sl_id, version)
    _require_initialized(layer_sl_id, layer_version)
    return {
        "pending": STORE.load_pending(layer_sl_id, layer_version),
        "sl_id": layer_sl_id,
        "version": layer_version,
    }


@app.get("/pending/all")
def pending_all() -> dict:
    pending_actions = STORE.load_all_pending()
    return {
        "pending": pending_actions,
        "count": len(pending_actions),
    }


@app.post("/operator/batch")
def operator_batch(
    sl_id: Optional[str] = Query(default=None),
    version: Optional[str] = Query(default=None),
) -> dict:
    with STATE_LOCK:
        layer_sl_id, layer_version = _layer_hex(sl_id, version)
        checkpoint_status = _require_operator_checkpoint_current(layer_sl_id, layer_version)
        state = _operator_state(layer_sl_id, layer_version)
        pending_actions = STORE.load_pending(layer_sl_id, layer_version)
        if not pending_actions:
            return {
                "batched": False,
                "message": "No pending actions. Nothing to batch.",
                "sl_id": layer_sl_id,
                "version": layer_version,
            }

        sequence = STORE.next_batch_sequence(layer_sl_id, layer_version)
        actions = [core.Action.from_dict(d) for d in pending_actions]
        new_state, result = core.process_batch(
            state,
            actions,
            sequence=sequence,
            sl_id=bytes.fromhex(layer_sl_id),
            version=bytes.fromhex(layer_version),
        )
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
            "status": "batched",
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
            "sl_id": layer_sl_id,
            "version": layer_version,
        }

        STORE.commit_operator_batch(new_state, record, sequence, layer_sl_id, layer_version)

        return {
            "batched": True,
            "batch": record,
            "operator_state": _state_to_response(new_state),
            "operator_checkpoint": checkpoint_status,
            "sl_id": layer_sl_id,
            "version": layer_version,
        }


@app.post("/operator/execution-request")
def operator_execution_request(request: OperatorExecutionRequest) -> dict:
    proposal = request.proposal or {}
    if not proposal:
        raise HTTPException(status_code=400, detail="execution request requires proposal")
    signed_intents = _validate_execution_signed_intents(request)
    context = _execution_context_from_proposal(proposal)
    grouped = _execution_actions_by_layer(proposal, signed_intents)
    submitted_groups = []

    with STATE_LOCK:
        for (sl_id, version), pairs in grouped.items():
            _preflight_operator_action_batch(
                actions=[pair["action"] for pair in pairs],
                context=context,
                sl_id=sl_id,
                version=version,
            )
        for (sl_id, version), pairs in grouped.items():
            actions = [pair["action"] for pair in pairs]
            committed = _commit_operator_action_batch(
                actions=actions,
                context=context,
                sl_id=sl_id,
                version=version,
                source="operator_execution_request",
                proposal_id=request.proposal_id,
            )
            batch = committed["batch"]
            submission = None
            if request.submit_to_base:
                submission, batch = _submit_operator_batch_to_devnet(
                    batch=batch,
                    sl_id=sl_id,
                    version=version,
                )
            submitted_groups.append({
                **committed,
                "batch": batch,
                "submission": submission,
                "pairs": pairs,
            })

    receipts = []
    groups = []
    for group in submitted_groups:
        sl_id = group["sl_id"]
        version = group["version"]
        submission = group.get("submission")
        batch = group["batch"]
        verification = None
        if submission is not None:
            verification, batch = _verify_submitted_operator_batch(
                batch=batch,
                submission=submission,
                sl_id=sl_id,
                version=version,
                wait_for_verifier=request.wait_for_verifier,
                timeout_seconds=request.verifier_timeout_seconds,
                poll_interval_seconds=request.verifier_poll_interval_seconds,
            )
            rollback = (
                _rollback_operator_batch_after_rejection(sl_id, version, batch)
                if verification and not verification.get("verified")
                else None
            )
        elif request.wait_for_verifier:
            verification = {
                "status": "skipped",
                "verified": False,
                "message": "submit_to_base=false; verifier sync was not attempted",
            }
            rollback = None
        else:
            rollback = None

        evidence_batch = _batch_with_evidence(
            batch,
            _accepted_log_by_sequence(sl_id, version),
        )
        group_receipts = [
            _receipt_for_action(
                proposal_id=request.proposal_id,
                signed_intent=pair["intent"],
                action=pair["action"],
                batch=evidence_batch,
                verification=verification,
                submission=submission,
                evidence=evidence_batch,
            )
            for pair in group["pairs"]
        ]
        receipts.extend(group_receipts)
        groups.append({
            "sl_id": sl_id,
            "version": version,
            "batch": evidence_batch,
            "devnet_submission": submission,
            "verification": verification,
            "rollback": rollback,
            "receipts": group_receipts,
        })

    all_accepted = bool(receipts) and all(receipt.get("accepted") for receipt in receipts)
    return {
        "status": "verified" if all_accepted else "submitted",
        "proposal_id": request.proposal_id,
        "receipt_count": len(receipts),
        "receipts": receipts,
        "groups": groups,
    }


@app.post("/operator/sync-from-verifier")
def operator_sync_from_verifier(request: LayerRequest) -> dict:
    with STATE_LOCK:
        layer_sl_id, layer_version = _layer_hex(request.sl_id, request.version)
        _require_initialized(layer_sl_id, layer_version)
        synced = _sync_operator_to_verifier_checkpoint(layer_sl_id, layer_version)
        if synced is None:
            raise HTTPException(
                status_code=404,
                detail="no verifier checkpoint found for semantic layer",
            )

        return {
            **synced,
            "synced": True,
            "sl_id": layer_sl_id,
            "version": layer_version,
        }


@app.get("/operator/batches")
def operator_batches(
    sl_id: Optional[str] = Query(default=None),
    version: Optional[str] = Query(default=None),
) -> dict:
    layer_sl_id, layer_version = _layer_hex(sl_id, version)
    _require_initialized(layer_sl_id, layer_version)
    return {
        "batches": _batches_with_evidence(STORE.list_batches(layer_sl_id, layer_version), layer_sl_id, layer_version),
        "sl_id": layer_sl_id,
        "version": layer_version,
    }


@app.get("/operator/latest-payload")
def latest_payload(
    sl_id: Optional[str] = Query(default=None),
    version: Optional[str] = Query(default=None),
) -> dict:
    layer_sl_id, layer_version = _layer_hex(sl_id, version)
    batch = _latest_batch(layer_sl_id, layer_version)
    return {
        "sequence": batch["sequence"],
        "payload_hex": batch["payload_hex"],
        "payload_size": batch["payload_size"],
        "data_scalars": batch["data_scalars"],
        "data_len": batch["data_len"],
        "sl_id": layer_sl_id,
        "version": layer_version,
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
def get_devnet_status(
    sl_id: Optional[str] = Query(default=None),
    version: Optional[str] = Query(default=None),
) -> dict:
    return _devnet_runtime_status(sl_id, version)


@app.post("/devnet/submit-latest-batch")
def submit_latest_batch_to_devnet(request: DevnetSubmitRequest) -> dict:
    with STATE_LOCK:
        sl_id, version = _layer_hex(request.sl_id, request.version)
        batch = _batch_for_submission(sl_id, version, request.sequence)
        submission, updated_batch = _submit_operator_batch_to_devnet(
            batch=batch,
            sl_id=sl_id,
            version=version,
            force=request.force,
        )

    verification, updated_batch = _verify_submitted_operator_batch(
        batch=updated_batch,
        submission=submission,
        sl_id=sl_id,
        version=version,
        wait_for_verifier=request.wait_for_verifier,
        timeout_seconds=request.verifier_timeout_seconds,
        poll_interval_seconds=request.verifier_poll_interval_seconds,
    )

    return {
        "submitted": True,
        "sequence": submission["sequence"],
        "devnet_submission": submission,
        "verification": verification,
        "batch": _batch_with_evidence(updated_batch, _accepted_log_by_sequence(sl_id, version)),
        "sl_id": sl_id,
        "version": version,
    }


@app.get("/verifier/state")
def verifier_state(
    sl_id: str = Query(default=core.SL_ID.hex()),
    version: str = Query(default=core.VERSION.hex()),
) -> dict:
    state, sl_id_bytes, version_bytes = _verified_state_for_layer(sl_id, version)
    log = _verifier_store().list_verification_log(sl_id_bytes, version_bytes)
    return {
        "sl_id": sl_id_bytes.hex(),
        "version": version_bytes.hex(),
        "state": _state_to_response(state),
        "accepted_payloads": len([entry for entry in log if entry.get("verdict") == "accepted"]),
    }


@app.get("/verifier/log")
def verifier_log(
    sl_id: str = Query(default=core.SL_ID.hex()),
    version: Optional[str] = Query(default=None),
) -> dict:
    sl_id_bytes = _sl_id_bytes(sl_id)
    version_bytes = bytes.fromhex(_version_hex(version)) if version else None
    return {
        "sl_id": sl_id_bytes.hex(),
        "version": version_bytes.hex() if version_bytes else None,
        "log": _verifier_store().list_verification_log(sl_id_bytes, version_bytes),
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


@app.post("/verifier/sync")
def verifier_sync(request: VerifierSyncRequest) -> dict:
    sl_id, version = _layer_hex(request.sl_id, request.version)
    try:
        result = _sync_verifier_from_base_layer_api(
            sl_id,
            version,
            posting_owner=request.posting_owner,
            expected_sequence=request.expected_sequence,
            expected_state_hash=request.expected_state_hash,
            timeout_seconds=request.timeout_seconds,
            poll_interval_seconds=request.poll_interval_seconds,
        )
        updated_batch = _record_verification_for_checkpoint_batch(sl_id, version, result)
        if updated_batch is not None:
            result["batch"] = _batch_with_evidence(
                updated_batch,
                _accepted_log_by_sequence(sl_id, version),
            )
        return result
    except DevnetSubmitError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@app.post("/verifier/notify")
def verifier_notify(request: VerifierNotifyRequest) -> dict:
    with STATE_LOCK:
        return _notify_verifier_from_source_event(request.source_event, request.source_events)


@app.post("/verifier/index")
def verifier_index() -> dict:
    with STATE_LOCK:
        return _sync_all_verifier_layers_once()


@app.post("/verifier/accept-latest-batch")
def verifier_accept_latest_batch(
    sl_id: Optional[str] = Query(default=None),
    version: Optional[str] = Query(default=None),
) -> dict:
    with STATE_LOCK:
        layer_sl_id, layer_version = _layer_hex(sl_id, version)
        batches = STORE.list_batches(layer_sl_id, layer_version)
        if not batches:
            raise HTTPException(status_code=404, detail="no operator batch found")

        plugin = _payment_plugin(layer_sl_id, layer_version)
        checkpoint = _verifier_store().load_checkpoint(plugin.sl_id, plugin.version)
        expected_sequence = 1 if checkpoint is None else int(checkpoint["sequence"]) + 1
        latest_sequence = int(batches[-1]["sequence"])

        if expected_sequence > latest_sequence:
            return {
                "accepted": True,
                "message": "latest batch already accepted",
                "sequence": latest_sequence,
                "accepted_sequences": [],
                "verifier_state": _state_to_response(_verified_state(layer_sl_id, layer_version)),
                "sl_id": layer_sl_id,
                "version": layer_version,
            }

        batch_by_sequence = {int(batch["sequence"]): batch for batch in batches}
        accepted_sequences: list[int] = []
        last_sequence = expected_sequence - 1

        for sequence in range(expected_sequence, latest_sequence + 1):
            batch = batch_by_sequence.get(sequence)
            if batch is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"missing operator batch for expected sequence {sequence}",
                )

            valid, msg = _accept_envelope(
                _batch_envelope(batch),
                layer_sl_id,
                layer_version,
            )
            if not valid:
                raise HTTPException(status_code=400, detail=msg)
            accepted_sequences.append(sequence)
            last_sequence = sequence

        return {
            "accepted": True,
            "message": (
                "accepted"
                if len(accepted_sequences) == 1
                else f"accepted {len(accepted_sequences)} batches"
            ),
            "sequence": last_sequence,
            "accepted_sequences": accepted_sequences,
            "verifier_state": _state_to_response(_verified_state(layer_sl_id, layer_version)),
            "sl_id": layer_sl_id,
            "version": layer_version,
        }


@app.post("/verifier/accept-envelope")
def verifier_accept_envelope(
    envelope: Dict[str, Any],
    sl_id: Optional[str] = Query(default=None),
    version: Optional[str] = Query(default=None),
) -> dict:
    with STATE_LOCK:
        layer_sl_id, layer_version = _layer_hex(sl_id, version)
        valid, msg = _accept_envelope(envelope, layer_sl_id, layer_version)
        if not valid:
            raise HTTPException(status_code=400, detail=msg)

        return {
            "accepted": True,
            "message": msg,
            "sequence": envelope["sequence"],
            "verifier_state": _state_to_response(_verified_state(layer_sl_id, layer_version)),
            "sl_id": layer_sl_id,
            "version": layer_version,
        }


@app.post("/verifier/envelope-from-payload")
def verifier_envelope_from_payload(
    request: PayloadRequest,
    sl_id: Optional[str] = Query(default=None),
    version: Optional[str] = Query(default=None),
) -> dict:
    layer_sl_id, layer_version = _layer_hex(sl_id, version)
    state = _verified_state(layer_sl_id, layer_version)
    try:
        return envelope_from_payload_hex(
            request.payload_hex,
            state,
            expected_sl_id=bytes.fromhex(layer_sl_id),
            expected_version=bytes.fromhex(layer_version),
        )
    except core.PayloadDecodeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
