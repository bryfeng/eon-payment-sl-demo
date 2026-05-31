"""Configurable EON devnet submission boundary.

The Python SL runtime owns deterministic payload construction. Actual EON
transactions require account key material and the EON Rust signing stack, so the
hosted API delegates the transaction write to a configured submitter command.
The command protocol is JSON over stdin/stdout and is intentionally explicit.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional


DEFAULT_DEVNET_API_URL = "https://eon.zk524.com"


class DevnetSubmitError(RuntimeError):
    """Raised when a live devnet submission cannot be completed."""


@dataclass(frozen=True)
class DevnetSubmitConfig:
    api_url: str
    command: Optional[str]
    timeout_seconds: int
    base_layer_api_url: Optional[str]
    base_layer_api_key: Optional[str]
    base_layer_transfer_recipient: Optional[str]
    base_layer_transfer_fee: int
    base_layer_transfer_amount: Optional[int]

    @property
    def enabled(self) -> bool:
        return bool(self.base_layer_api_url or self.command)

    @property
    def uses_base_layer_api(self) -> bool:
        return bool(self.base_layer_api_url)

    @property
    def validation_error(self) -> Optional[str]:
        if self.base_layer_api_url:
            return validate_base_layer_api_config(self)
        if not self.command:
            return None
        return validate_submitter_command(self.command)

    @classmethod
    def from_env(cls) -> "DevnetSubmitConfig":
        timeout = os.environ.get("EON_DEVNET_SUBMIT_TIMEOUT_SECONDS", "120")
        try:
            timeout_seconds = int(timeout)
        except ValueError:
            timeout_seconds = 120

        return cls(
            api_url=os.environ.get("EON_DEVNET_API_URL", DEFAULT_DEVNET_API_URL),
            command=os.environ.get("EON_DEVNET_SUBMIT_CMD"),
            timeout_seconds=timeout_seconds,
            base_layer_api_url=_env_first("BASE_LAYER_API_URL", "EON_BASE_LAYER_API_URL"),
            base_layer_api_key=_env_first("BASE_LAYER_API_KEY", "EON_BASE_LAYER_API_KEY"),
            base_layer_transfer_recipient=_env_first(
                "BASE_LAYER_TRANSFER_RECIPIENT",
                "EON_BASE_LAYER_TRANSFER_RECIPIENT",
            ),
            base_layer_transfer_fee=_int_env(
                "BASE_LAYER_TRANSFER_FEE",
                "EON_BASE_LAYER_TRANSFER_FEE",
                default=1,
            ),
            base_layer_transfer_amount=_optional_int_env(
                "BASE_LAYER_TRANSFER_AMOUNT",
                "EON_BASE_LAYER_TRANSFER_AMOUNT",
            ),
        )


def devnet_status() -> dict:
    config = DevnetSubmitConfig.from_env()
    submitter_error = config.validation_error
    base_layer_status = base_layer_api_status(config) if config.uses_base_layer_api else {}
    if not submitter_error:
        submitter_error = base_layer_status.get("base_layer_api_error")
    submitter_ready = config.enabled and not submitter_error
    if config.uses_base_layer_api:
        submitter_ready = bool(
            submitter_ready
            and base_layer_status.get("base_layer_api_reachable")
            and base_layer_status.get("base_layer_wallet_configured")
        )

    return {
        "network_id": "devnet",
        "api_url": config.api_url,
        "submitter": (
            "base_layer_api"
            if config.uses_base_layer_api
            else "command" if config.command else None
        ),
        "enabled": submitter_ready,
        "submitter_configured": submitter_ready,
        "submitter_command_configured": bool(config.command),
        "submitter_error": submitter_error,
        "wallet_file_configured": bool(os.environ.get("EON_OPERATOR_WALLET_FILE")),
        "timeout_seconds": config.timeout_seconds,
        **base_layer_status,
    }


def _env_first(*names: str) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _int_env(*names: str, default: int) -> int:
    value = _env_first(*names)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _optional_int_env(*names: str) -> Optional[int]:
    value = _env_first(*names)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def validate_base_layer_api_config(config: DevnetSubmitConfig) -> Optional[str]:
    api_url = (config.base_layer_api_url or "").strip()
    if not api_url:
        return None
    if not (api_url.startswith("http://") or api_url.startswith("https://")):
        return "BASE_LAYER_API_URL must start with http:// or https://"
    if not config.base_layer_api_key:
        return "BASE_LAYER_API_KEY is required for the base-layer API submitter"
    return None


def validate_submitter_command(command: str) -> Optional[str]:
    if "/path/to/" in command:
        return "EON_DEVNET_SUBMIT_CMD still contains a placeholder /path/to/... value"

    try:
        parts = shlex.split(command)
    except ValueError as e:
        return f"EON_DEVNET_SUBMIT_CMD cannot be parsed: {e}"

    if not parts:
        return "EON_DEVNET_SUBMIT_CMD is empty"

    executable = parts[0]
    if len(parts) == 1 and " " in executable:
        return "EON_DEVNET_SUBMIT_CMD appears quoted as one executable; remove the outer quotes"

    if os.path.sep in executable:
        if not os.path.exists(executable):
            return f"submitter executable does not exist: {executable}"
    elif shutil.which(executable) is None:
        return f"submitter executable is not installed or not on PATH: {executable}"

    if "--manifest-path" in parts:
        manifest_index = parts.index("--manifest-path") + 1
        if manifest_index >= len(parts):
            return "cargo submitter command is missing the --manifest-path value"
        manifest_path = parts[manifest_index]
        if not os.path.exists(manifest_path):
            return f"cargo manifest does not exist: {manifest_path}"

    return None


def base_layer_api_status(config: DevnetSubmitConfig) -> dict:
    status = {
        "base_layer_api_url": config.base_layer_api_url,
        "base_layer_api_reachable": False,
        "base_layer_wallet_configured": False,
        "base_layer_wallet_address": None,
        "base_layer_transfer_recipient_configured": bool(
            config.base_layer_transfer_recipient
        ),
        "base_layer_api_error": None,
    }
    if not config.base_layer_api_url:
        return status

    config_error = validate_base_layer_api_config(config)
    if config_error:
        status["base_layer_api_error"] = config_error
        return status

    try:
        health = _base_layer_api_json(config, "GET", "/health")
    except DevnetSubmitError as e:
        status["base_layer_api_error"] = str(e)
        return status

    status["base_layer_api_reachable"] = True
    status["base_layer_wallet_configured"] = bool(health.get("wallet_configured"))
    if not status["base_layer_wallet_configured"]:
        status["base_layer_api_error"] = "base-layer API wallet is not configured"
        return status

    try:
        wallet = _base_layer_api_json(config, "GET", "/wallet/address")
    except DevnetSubmitError as e:
        status["base_layer_api_error"] = str(e)
        return status

    wallet_address = str(wallet.get("address") or "").strip()
    if wallet_address:
        status["base_layer_wallet_address"] = wallet_address
        status["base_layer_transfer_recipient_configured"] = True
    return status


def submit_batch_to_devnet(
    batch: dict,
    account_json: Optional[dict[str, Any]] = None,
) -> dict:
    config = DevnetSubmitConfig.from_env()
    if config.uses_base_layer_api:
        submitter_error = config.validation_error
        if submitter_error:
            raise DevnetSubmitError(
                f"EON devnet submitter is misconfigured: {submitter_error}"
            )
        return submit_batch_via_base_layer_api(config, batch, account_json)

    if not config.enabled:
        raise DevnetSubmitError(
            "EON devnet submission is not configured. Set EON_DEVNET_SUBMIT_CMD "
            "to a command that signs and submits the payload transaction."
        )
    submitter_error = config.validation_error
    if submitter_error:
        raise DevnetSubmitError(
            f"EON devnet submitter is misconfigured: {submitter_error}"
        )

    request = {
        "network_id": "devnet",
        "api_url": config.api_url,
        "sequence": batch["sequence"],
        "payload_hex": batch["payload_hex"],
        "payload_size": batch["payload_size"],
        "data_scalars": batch["data_scalars"],
        "data_len": batch["data_len"],
    }

    env = os.environ.copy()
    temp_account_file: Optional[str] = None
    if account_json is not None:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            delete=False,
        ) as handle:
            json.dump(account_json, handle, separators=(",", ":"), sort_keys=True)
            temp_account_file = handle.name
        env["EON_OPERATOR_WALLET_FILE"] = temp_account_file

    try:
        completed = subprocess.run(
            shlex.split(config.command),
            input=json.dumps(request),
            text=True,
            capture_output=True,
            timeout=config.timeout_seconds,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DevnetSubmitError(str(e)) from e
    finally:
        if temp_account_file:
            try:
                os.unlink(temp_account_file)
            except OSError:
                pass

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DevnetSubmitError(detail or f"submitter exited with {completed.returncode}")

    try:
        response: dict[str, Any] = json.loads(completed.stdout)
    except json.JSONDecodeError as e:
        raise DevnetSubmitError("submitter did not return JSON") from e

    tx_hash = str(response.get("tx_hash", "")).strip()
    if not tx_hash:
        raise DevnetSubmitError("submitter response missing tx_hash")

    return {
        "status": "submitted",
        "network_id": "devnet",
        "api_url": config.api_url,
        "sequence": batch["sequence"],
        "tx_hash": tx_hash,
        "utxo_id": response.get("utxo_id"),
        "spent_utxo": response.get("spent_utxo"),
        "owner": response.get("owner"),
        "output_index": response.get("output_index", 0),
        "amount": str(response.get("amount", response.get("data_amount", batch["data_len"]))),
        "response": response.get("response", "ok"),
        "payload_hex": batch["payload_hex"],
        "data_len": batch["data_len"],
        "data_scalars": batch["data_scalars"],
    }


def submit_batch_via_base_layer_api(
    config: DevnetSubmitConfig,
    batch: dict,
    account_json: Optional[dict[str, Any]] = None,
) -> dict:
    recipient = resolve_base_layer_recipient(config, account_json)
    amount = config.base_layer_transfer_amount or 1
    fee = config.base_layer_transfer_fee
    request = {
        "recipient": recipient,
        "amount": amount,
        "fee": fee,
        "data": batch["data_scalars"],
    }
    response = _base_layer_api_json(
        config,
        "POST",
        "/transactions/transfer",
        request,
        include_api_key=True,
    )
    tx_hash = str(response.get("hash") or "").strip()
    if not tx_hash:
        raise DevnetSubmitError("base-layer API response missing transaction hash")

    return {
        "status": "submitted",
        "network_id": "devnet",
        "api_url": config.api_url,
        "submitter": "base_layer_api",
        "base_layer_api_url": config.base_layer_api_url,
        "sequence": batch["sequence"],
        "tx_hash": tx_hash,
        "utxo_id": response.get("utxo_id"),
        "spent_utxo": response.get("spent_utxo"),
        "owner": recipient,
        "output_index": response.get("output_index", 0),
        "amount": str(response.get("amount", amount)),
        "response": response.get("response", "ok" if response.get("submitted") else response),
        "payload_hex": batch["payload_hex"],
        "data_len": batch["data_len"],
        "data_scalars": batch["data_scalars"],
    }


def resolve_base_layer_recipient(
    config: DevnetSubmitConfig,
    account_json: Optional[dict[str, Any]] = None,
) -> str:
    account_address = str((account_json or {}).get("address") or "").strip()
    if account_address:
        return account_address
    if config.base_layer_transfer_recipient:
        return config.base_layer_transfer_recipient

    wallet = _base_layer_api_json(config, "GET", "/wallet/address")
    wallet_address = str(wallet.get("address") or "").strip()
    if not wallet_address:
        raise DevnetSubmitError("base-layer API wallet address is unavailable")
    return wallet_address


def _base_layer_api_json(
    config: DevnetSubmitConfig,
    method: str,
    path: str,
    body: Optional[dict[str, Any]] = None,
    include_api_key: bool = False,
) -> dict[str, Any]:
    if not config.base_layer_api_url:
        raise DevnetSubmitError("BASE_LAYER_API_URL is not configured")

    url = f"{config.base_layer_api_url.rstrip('/')}/{path.lstrip('/')}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if include_api_key and config.base_layer_api_key:
        headers["x-api-key"] = config.base_layer_api_key

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace").strip()
        raise DevnetSubmitError(
            f"base-layer API {method} {path} failed with HTTP {e.code}: {detail}"
        ) from e
    except urllib.error.URLError as e:
        raise DevnetSubmitError(f"base-layer API {method} {path} failed: {e}") from e
    except TimeoutError as e:
        raise DevnetSubmitError(f"base-layer API {method} {path} timed out") from e

    if not response_body:
        return {}
    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as e:
        raise DevnetSubmitError(
            f"base-layer API {method} {path} did not return JSON"
        ) from e
    if not isinstance(parsed, dict):
        raise DevnetSubmitError(
            f"base-layer API {method} {path} returned non-object JSON"
        )
    return parsed
