"""Generic EON Data scalar framing helpers.

The live devnet stores UTXO Data as scalar words. These helpers are deliberately
semantic-layer agnostic: they only frame bytes into scalars and recover bytes
from scalars.
"""

SCALAR_BYTES = 4


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


def scalar_hex_to_payload_bytes(scalars: list[str]) -> bytes:
    """Reverse payload_bytes_to_scalar_hex."""
    if not scalars:
        raise ScalarFramingError("no scalars supplied")

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


def payload_header(payload: bytes) -> tuple[bytes, bytes]:
    """Return the common [SL_ID:4][version:2] header from a payload."""
    if len(payload) < 6:
        raise ScalarFramingError("payload too short for semantic-layer header")
    return payload[:4], payload[4:6]
