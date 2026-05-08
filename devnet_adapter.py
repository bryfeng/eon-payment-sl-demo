"""
devnet_adapter.py - EON devnet adapter boundary

The live devnet stores UTXO Data as ordered scalar values. This module owns the
Payment SL's application-level framing between canonical payload bytes and those
scalars, plus the conversion from decoded devnet data into verifier envelopes.

It intentionally keeps transaction fetching/submission outside the Python demo
until the EON SDK exposes a stable data-bearing-output path. The deterministic
parts are implemented and tested here:

  payload bytes -> scalar hex words
  scalar hex words -> payload bytes
  payload bytes + verifier previous state -> verifier envelope
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from core import (
    PayloadDecodeError,
    State,
    _read_json,
    parse_data_field_payload,
)


# Current devnet default scalar serialization is MockScalar/u32, exposed as
# 4-byte big-endian words by eoncli's scalars_to_0x helper.
SCALAR_BYTES = 4


class ScalarFramingError(Exception):
    """Raised when Data scalar words cannot be reassembled into payload bytes."""
    pass


def _normalize_scalar_hex(value: str) -> str:
    scalar = value.strip().lower()
    if scalar.startswith("0x"):
        scalar = scalar[2:]
    if not scalar:
        raise ScalarFramingError("empty scalar")
    if len(scalar) > SCALAR_BYTES * 2:
        raise ScalarFramingError(f"scalar is wider than {SCALAR_BYTES} bytes: {value}")
    try:
        int(scalar, 16)
    except ValueError as e:
        raise ScalarFramingError(f"invalid scalar hex: {value}") from e
    return scalar.rjust(SCALAR_BYTES * 2, "0")


def _payload_hex_to_bytes(value: str) -> bytes:
    payload_hex = value.strip().lower()
    if payload_hex.startswith("0x"):
        payload_hex = payload_hex[2:]
    return bytes.fromhex(payload_hex)


def payload_bytes_to_scalar_hex(payload: bytes) -> list:
    """
    Frame canonical payload bytes into EON scalar hex words.

    The first scalar word is the byte length of the payload. Remaining words are
    raw payload bytes, zero-padded only in the final word.
    """
    if len(payload) > 0xFFFFFFFF:
        raise ScalarFramingError("payload is too large for u32 length prefix")

    framed = len(payload).to_bytes(SCALAR_BYTES, "big") + payload
    pad = (-len(framed)) % SCALAR_BYTES
    if pad:
        framed += b"\x00" * pad

    return [
        "0x" + framed[i:i + SCALAR_BYTES].hex()
        for i in range(0, len(framed), SCALAR_BYTES)
    ]


def scalar_hex_to_payload_bytes(scalars: list) -> bytes:
    """Reverse payload_bytes_to_scalar_hex."""
    if not scalars:
        raise ScalarFramingError("no scalars supplied")

    raw = b"".join(bytes.fromhex(_normalize_scalar_hex(s)) for s in scalars)
    payload_len = int.from_bytes(raw[:SCALAR_BYTES], "big")
    payload_start = SCALAR_BYTES
    payload_end = payload_start + payload_len

    if payload_end > len(raw):
        raise ScalarFramingError(
            f"declared payload length {payload_len} exceeds scalar data length"
        )

    padding = raw[payload_end:]
    if any(padding):
        raise ScalarFramingError("non-zero bytes after declared payload length")

    return raw[payload_start:payload_end]


def envelope_from_payload_bytes(
    payload: bytes,
    prev_state: State,
    eon_metadata: Optional[dict] = None,
) -> dict:
    """
    Convert decoded devnet payload bytes into the verifier envelope shape.

    The payload only carries hashes and actions. Path (a) verification also
    needs the previous state, which a verifier gets from its own accepted state
    log before processing the next ordered UTXO.
    """
    decoded = parse_data_field_payload(payload)
    prev_hash = prev_state.state_hash()
    if prev_hash != decoded["prev_state_hash"]:
        raise PayloadDecodeError(
            "previous state does not match payload prev_state_hash: "
            f"{prev_hash[:16]}... vs {decoded['prev_state_hash'][:16]}..."
        )

    envelope = {
        "prev_state": prev_state.to_dict(),
        **decoded,
    }
    if eon_metadata:
        envelope["eon"] = eon_metadata
    return envelope


def envelope_from_payload_hex(
    payload_hex: str,
    prev_state: State,
    eon_metadata: Optional[dict] = None,
) -> dict:
    try:
        payload = _payload_hex_to_bytes(payload_hex)
    except ValueError as e:
        raise PayloadDecodeError(f"invalid payload hex: {e}") from e
    return envelope_from_payload_bytes(payload, prev_state, eon_metadata)


def envelope_from_scalars(
    scalars: list,
    prev_state: State,
    eon_metadata: Optional[dict] = None,
) -> dict:
    payload = scalar_hex_to_payload_bytes(scalars)
    return envelope_from_payload_bytes(payload, prev_state, eon_metadata)


def _load_state(path: str) -> State:
    state_path = Path(path)
    if not state_path.exists():
        sys.exit(f"error: previous-state file not found: {state_path}")
    return State.from_dict(_read_json(state_path))


def _metadata_from_args(args) -> dict:
    metadata = {}
    for key in ("utxo_id", "tx_hash", "output_index"):
        value = getattr(args, key, None)
        if value is not None:
            metadata[key] = value
    return metadata


def _write_json_result(obj: dict, out_path: Optional[str] = None) -> None:
    if out_path:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"Wrote {path}")
        return

    print(json.dumps(obj, indent=2, sort_keys=True))


def cmd_encode_payload(args) -> None:
    try:
        payload = _payload_hex_to_bytes(args.payload_hex)
        scalars = payload_bytes_to_scalar_hex(payload)
    except (ValueError, ScalarFramingError) as e:
        sys.exit(f"error: {e}")

    _write_json_result({
        "payload_hex": payload.hex(),
        "scalar_bytes": SCALAR_BYTES,
        "data_scalars": scalars,
        "data_len": len(scalars),
    }, args.out)


def cmd_envelope_from_payload(args) -> None:
    prev_state = _load_state(args.prev_state_file)
    try:
        envelope = envelope_from_payload_hex(
            args.payload_hex,
            prev_state,
            _metadata_from_args(args),
        )
    except PayloadDecodeError as e:
        sys.exit(f"error: {e}")
    _write_json_result(envelope, args.out)


def cmd_envelope_from_scalars(args) -> None:
    prev_state = _load_state(args.prev_state_file)
    try:
        envelope = envelope_from_scalars(
            args.scalar,
            prev_state,
            _metadata_from_args(args),
        )
    except (PayloadDecodeError, ScalarFramingError) as e:
        sys.exit(f"error: {e}")
    _write_json_result(envelope, args.out)


def _add_envelope_metadata_args(parser) -> None:
    parser.add_argument("--utxo-id", help="Optional EON UTXO id for audit metadata.")
    parser.add_argument("--tx-hash", help="Optional EON transaction hash for audit metadata.")
    parser.add_argument("--output-index", type=int, help="Optional EON output index.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="devnet_adapter.py",
        description="Frame Payment SL payloads for EON Data and build verifier envelopes.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_encode = sub.add_parser(
        "encode-payload",
        help="Frame canonical payload hex into EON scalar hex words.",
    )
    p_encode.add_argument("--payload-hex", required=True)
    p_encode.add_argument("--out", help="Write JSON result to this path.")
    p_encode.set_defaults(func=cmd_encode_payload)

    p_payload = sub.add_parser(
        "envelope-from-payload",
        help="Build a verifier envelope from canonical payload hex.",
    )
    p_payload.add_argument("--payload-hex", required=True)
    p_payload.add_argument("--prev-state-file", required=True)
    p_payload.add_argument("--out", help="Write envelope JSON to this path.")
    _add_envelope_metadata_args(p_payload)
    p_payload.set_defaults(func=cmd_envelope_from_payload)

    p_scalars = sub.add_parser(
        "envelope-from-scalars",
        help="Build a verifier envelope from decoded EON Data scalar words.",
    )
    p_scalars.add_argument("--scalar", required=True, action="append")
    p_scalars.add_argument("--prev-state-file", required=True)
    p_scalars.add_argument("--out", help="Write envelope JSON to this path.")
    _add_envelope_metadata_args(p_scalars)
    p_scalars.set_defaults(func=cmd_envelope_from_scalars)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
