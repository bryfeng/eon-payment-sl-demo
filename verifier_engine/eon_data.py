"""Generic EON Data scalar framing helpers.

The live devnet stores UTXO Data as scalar words. These helpers are deliberately
semantic-layer agnostic: they only frame bytes into scalars and recover bytes
from scalars.
"""

SCALAR_BYTES = 4
FIELD_SAFE_BITS = 30
FIELD_SAFE_LENGTH_MARKER = 0x40000000
FIELD_SAFE_MAX_PAYLOAD_LENGTH = 0x3F000000
FIELD_SAFE_CHUNK_MASK = (1 << FIELD_SAFE_BITS) - 1


class ScalarFramingError(Exception):
    """Raised when Data scalar words cannot be reassembled."""


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
