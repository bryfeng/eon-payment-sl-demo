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
import subprocess
import tempfile
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

    @property
    def enabled(self) -> bool:
        return bool(self.command)

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
        )


def devnet_status() -> dict:
    config = DevnetSubmitConfig.from_env()
    return {
        "network_id": "devnet",
        "api_url": config.api_url,
        "submitter": "command" if config.enabled else None,
        "enabled": config.enabled,
        "submitter_configured": config.enabled,
        "wallet_file_configured": bool(os.environ.get("EON_OPERATOR_WALLET_FILE")),
        "timeout_seconds": config.timeout_seconds,
    }


def submit_batch_to_devnet(
    batch: dict,
    account_json: Optional[dict[str, Any]] = None,
) -> dict:
    config = DevnetSubmitConfig.from_env()
    if not config.enabled:
        raise DevnetSubmitError(
            "EON devnet submission is not configured. Set EON_DEVNET_SUBMIT_CMD "
            "to a command that signs and submits the payload transaction."
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
