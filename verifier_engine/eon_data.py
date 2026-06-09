"""Generic EON Data scalar framing helpers.

The live devnet stores UTXO Data as scalar words. These helpers are deliberately
semantic-layer agnostic: they only frame bytes into scalars and recover bytes
from scalars.
"""

from dataclasses import dataclass
import json
import struct
from typing import Any


SCALAR_BYTES = 4
FIELD_SAFE_BITS = 30
FIELD_SAFE_LENGTH_MARKER = 0x40000000
FIELD_SAFE_MAX_PAYLOAD_LENGTH = 0x3F000000
FIELD_SAFE_CHUNK_MASK = (1 << FIELD_SAFE_BITS) - 1
BUNDLE_SL_ID = b"\x00\x01\xff\x01"
BUNDLE_VERSION = b"\x00\x01"


class ScalarFramingError(Exception):
    """Raised when Data scalar words cannot be reassembled."""


class PayloadDecodeError(Exception):
    """Raised when a canonical semantic-layer payload cannot be decoded."""


@dataclass(frozen=True)
class TransitionPayload:
    sl_id: bytes
    version: bytes
    sequence: int
    prev_state_hash: str
    new_state_hash: str
    actions: list[dict[str, Any]]
    payload_hex: str


@dataclass(frozen=True)
class BundlePayload:
    bundle_id: str
    version: bytes
    children: list[bytes]
    payload_hex: str


def normalize_scalar_hex(value: str) -> str:
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


def payload_hex_to_bytes(value: str) -> bytes:
    payload_hex = value.strip().lower()
    if payload_hex.startswith("0x"):
        payload_hex = payload_hex[2:]
    return bytes.fromhex(payload_hex)


def payload_bytes_to_scalar_hex(payload: bytes) -> list[str]:
    """
    Frame canonical payload bytes into EON scalar hex words.

    The first scalar word is a field-safe length marker. Remaining words pack the
    payload into 30-bit chunks so every scalar remains below the EON field
    modulus and survives devnet round-trips without normalization.
    """
    if len(payload) > FIELD_SAFE_MAX_PAYLOAD_LENGTH:
        raise ScalarFramingError("payload is too large for field-safe length prefix")

    scalars = [FIELD_SAFE_LENGTH_MARKER + len(payload)]
    bit_buffer = 0
    bit_count = 0

    for payload_byte in payload:
        bit_buffer = (bit_buffer << 8) | payload_byte
        bit_count += 8

        while bit_count >= FIELD_SAFE_BITS:
            shift = bit_count - FIELD_SAFE_BITS
            scalars.append((bit_buffer >> shift) & FIELD_SAFE_CHUNK_MASK)
            bit_buffer &= (1 << shift) - 1
            bit_count = shift

    if bit_count:
        scalars.append((bit_buffer << (FIELD_SAFE_BITS - bit_count)) & FIELD_SAFE_CHUNK_MASK)

    return [f"0x{scalar:0{SCALAR_BYTES * 2}x}" for scalar in scalars]


def scalar_hex_to_payload_bytes(scalars: list[str]) -> bytes:
    """Reverse payload_bytes_to_scalar_hex."""
    if not scalars:
        raise ScalarFramingError("no scalars supplied")

    scalar_values = [int(normalize_scalar_hex(scalar), 16) for scalar in scalars]
    length_word = scalar_values[0]
    if length_word >= FIELD_SAFE_LENGTH_MARKER:
        return _field_safe_scalars_to_payload_bytes(scalar_values)

    raw = b"".join(bytes.fromhex(normalize_scalar_hex(s)) for s in scalars)
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


def _field_safe_scalars_to_payload_bytes(scalar_values: list[int]) -> bytes:
    payload_len = scalar_values[0] - FIELD_SAFE_LENGTH_MARKER
    if payload_len > FIELD_SAFE_MAX_PAYLOAD_LENGTH:
        raise ScalarFramingError("field-safe payload length exceeds maximum")

    expected_chunks = (payload_len * 8 + FIELD_SAFE_BITS - 1) // FIELD_SAFE_BITS
    if len(scalar_values) != expected_chunks + 1:
        raise ScalarFramingError(
            f"field-safe scalar count mismatch: expected {expected_chunks + 1}, got {len(scalar_values)}"
        )

    payload = bytearray()
    bit_buffer = 0
    bit_count = 0

    for scalar in scalar_values[1:]:
        if scalar > FIELD_SAFE_CHUNK_MASK:
            raise ScalarFramingError("field-safe scalar exceeds 30-bit chunk size")

        bit_buffer = (bit_buffer << FIELD_SAFE_BITS) | scalar
        bit_count += FIELD_SAFE_BITS

        while bit_count >= 8 and len(payload) < payload_len:
            shift = bit_count - 8
            payload.append((bit_buffer >> shift) & 0xFF)
            bit_buffer &= (1 << shift) - 1
            bit_count = shift

    if len(payload) != payload_len:
        raise ScalarFramingError("field-safe scalars ended before declared payload length")
    if bit_buffer:
        raise ScalarFramingError("non-zero field-safe padding bits")

    return bytes(payload)


def payload_header(payload: bytes) -> tuple[bytes, bytes]:
    """Return the common [SL_ID:4][version:2] header from a payload."""
    if len(payload) < 6:
        raise ScalarFramingError("payload too short for semantic-layer header")
    return payload[:4], payload[4:6]


def decode_transition_payload(payload: bytes) -> TransitionPayload:
    """Decode the generic transition payload used by marketplace SL frameworks."""
    header_len = 4 + 2 + 8 + 32 + 32 + 2
    if len(payload) < header_len:
        raise PayloadDecodeError(
            f"payload too short: {len(payload)} bytes, expected at least {header_len}"
        )

    offset = 0
    sl_id = payload[offset:offset + 4]
    offset += 4
    version = payload[offset:offset + 2]
    offset += 2
    sequence = struct.unpack(">Q", payload[offset:offset + 8])[0]
    offset += 8
    if sequence <= 0:
        raise PayloadDecodeError("sequence must be positive")

    prev_state_hash = payload[offset:offset + 32].hex()
    offset += 32
    new_state_hash = payload[offset:offset + 32].hex()
    offset += 32
    action_count = struct.unpack(">H", payload[offset:offset + 2])[0]
    offset += 2

    actions: list[dict[str, Any]] = []
    for idx in range(action_count):
        if offset + 2 > len(payload):
            raise PayloadDecodeError(f"missing length prefix for action #{idx}")
        action_len = struct.unpack(">H", payload[offset:offset + 2])[0]
        offset += 2
        if offset + action_len > len(payload):
            raise PayloadDecodeError(f"truncated action #{idx}")
        try:
            action = json.loads(payload[offset:offset + action_len].decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise PayloadDecodeError(f"invalid action #{idx}: {e}") from e
        if not isinstance(action, dict):
            raise PayloadDecodeError(f"action #{idx} must decode to an object")
        actions.append(action)
        offset += action_len

    if offset != len(payload):
        raise PayloadDecodeError(f"trailing bytes after payload: {len(payload) - offset}")

    return TransitionPayload(
        sl_id=sl_id,
        version=version,
        sequence=sequence,
        prev_state_hash=prev_state_hash,
        new_state_hash=new_state_hash,
        actions=actions,
        payload_hex=payload.hex(),
    )


def _bundle_id_bytes(bundle_id: str | bytes) -> bytes:
    if isinstance(bundle_id, bytes):
        raw = bundle_id
    else:
        value = bundle_id.lower()
        if value.startswith("0x"):
            value = value[2:]
        raw = bytes.fromhex(value)
    if len(raw) != 32:
        raise PayloadDecodeError("bundle_id must be 32 bytes")
    return raw


def encode_bundle_payload(
    *,
    bundle_id: str | bytes,
    children: list[bytes],
    version: bytes = BUNDLE_VERSION,
) -> bytes:
    """Encode the canonical marketplace bundle wrapper payload."""
    if len(version) != 2:
        raise PayloadDecodeError("version must be 2 bytes")
    if not children:
        raise PayloadDecodeError("bundle must include at least one child payload")
    if len(children) > 0xFFFF:
        raise PayloadDecodeError("too many child payloads for u16 child_count")

    payload = BUNDLE_SL_ID + version + _bundle_id_bytes(bundle_id) + struct.pack(">H", len(children))
    for child in children:
        if len(child) > 0xFFFFFFFF:
            raise PayloadDecodeError("child payload is too large for u32 length prefix")
        payload += struct.pack(">I", len(child)) + child
    return payload


def decode_bundle_payload(payload: bytes) -> BundlePayload:
    """Decode the canonical marketplace bundle wrapper payload."""
    header_len = 4 + 2 + 32 + 2
    if len(payload) < header_len:
        raise PayloadDecodeError(
            f"bundle payload too short: {len(payload)} bytes, expected at least {header_len}"
        )
    sl_id = payload[:4]
    if sl_id != BUNDLE_SL_ID:
        raise PayloadDecodeError(f"unexpected bundle SL_ID: {sl_id.hex()}")
    version = payload[4:6]
    bundle_id = payload[6:38].hex()
    child_count = struct.unpack(">H", payload[38:40])[0]
    if child_count == 0:
        raise PayloadDecodeError("bundle must include at least one child payload")

    offset = 40
    children: list[bytes] = []
    for idx in range(child_count):
        if offset + 4 > len(payload):
            raise PayloadDecodeError(f"missing length prefix for child #{idx}")
        child_len = struct.unpack(">I", payload[offset:offset + 4])[0]
        offset += 4
        if child_len < 6:
            raise PayloadDecodeError(f"child #{idx} is too short for semantic-layer header")
        if offset + child_len > len(payload):
            raise PayloadDecodeError(f"truncated child #{idx}")
        children.append(payload[offset:offset + child_len])
        offset += child_len

    if offset != len(payload):
        raise PayloadDecodeError(f"trailing bytes after bundle: {len(payload) - offset}")

    return BundlePayload(
        bundle_id=bundle_id,
        version=version,
        children=children,
        payload_hex=payload.hex(),
    )
