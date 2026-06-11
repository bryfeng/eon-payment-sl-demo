from verifier_engine.eon_data import encode_bundle_payload, payload_bytes_to_scalar_hex
from verifier_engine.sources import BaseLayerAPIEventSource


def _transition_payload(sl_id_hex: str, sequence: int) -> bytes:
    return (
        bytes.fromhex(sl_id_hex)
        + b"\x00\x01"
        + int(sequence).to_bytes(8, "big")
        + (b"\x11" * 32)
        + (b"\x22" * 32)
        + (0).to_bytes(2, "big")
    )


def test_base_layer_event_source_filters_after_cursor(monkeypatch):
    utxos = [
        {
            "id": "0xold",
            "height": 1,
            "tx_index": 0,
            "output_index": 0,
            "data": ["0x1", "0x1"],
        },
        {
            "id": "0xnew",
            "height": 2,
            "tx_index": 0,
            "output_index": 0,
            "data": ["0x1", "0x2"],
        },
    ]
    source = BaseLayerAPIEventSource("https://base.example")
    monkeypatch.setattr(source, "_utxos", lambda: utxos)

    events = list(source.events_after(None))
    assert [event["event_key"] for event in events] == [
        "devnet:utxo:0xold:0",
        "devnet:utxo:0xnew:0",
    ]

    after_old = list(source.events_after(events[0]["cursor"]))
    assert [event["event_key"] for event in after_old] == ["devnet:utxo:0xnew:0"]


def test_base_layer_event_source_replays_for_legacy_cursor(monkeypatch):
    source = BaseLayerAPIEventSource("https://base.example")
    monkeypatch.setattr(
        source,
        "_utxos",
        lambda: [{
            "id": "0xnew",
            "height": 2,
            "tx_index": 0,
            "output_index": 0,
            "data": ["0x1", "0x2"],
        }],
    )

    events = list(source.events_after("devnet:utxo:0xold:0"))
    assert len(events) == 1
    assert events[0]["event_key"] == "devnet:utxo:0xnew:0"


def test_base_layer_event_source_resolves_event_hint(monkeypatch):
    source = BaseLayerAPIEventSource("https://base.example", owner="0xowner")
    monkeypatch.setattr(
        source,
        "_utxos",
        lambda: [
            {
                "id": "0xother",
                "tx_hash": "0xother-tx",
                "output_index": 0,
                "data": ["0x1", "0x1"],
            },
            {
                "id": "0xtarget",
                "tx_hash": "0xtarget-tx",
                "output_index": 2,
                "owner": "0xowner",
                "amount": "10",
                "data": ["0x1", "0x2"],
            },
        ],
    )

    event = source.event_for_hint({
        "tx_hash": "0xtarget-tx",
        "data_scalars": ["0x1", "0x2"],
    })

    assert event is not None
    assert event["event_key"] == "devnet:utxo:0xtarget:2"
    assert event["owner"] == "0xowner"
    assert event["data_scalars"] == ["0x1", "0x2"]


def test_base_layer_event_source_orders_bundle_by_child_sequence():
    source = BaseLayerAPIEventSource("https://base.example")
    bundle_payload = encode_bundle_payload(
        bundle_id="ff" * 32,
        children=[
            _transition_payload("00010120", 5),
            _transition_payload("00010121", 2),
        ],
    )
    utxo = {"data": payload_bytes_to_scalar_hex(bundle_payload)}

    assert source._payload_sequence(utxo) == 2


def test_base_layer_event_source_cursor_uses_payload_sequence_before_output(monkeypatch):
    source = BaseLayerAPIEventSource("https://base.example")
    utxos = [
        {
            "id": "0xseq4",
            "height": 0,
            "tx_index": 0,
            "output_index": 0,
            "data": payload_bytes_to_scalar_hex(_transition_payload("00010003", 4)),
        },
        {
            "id": "0xseq3",
            "height": 0,
            "tx_index": 0,
            "output_index": 12,
            "data": payload_bytes_to_scalar_hex(_transition_payload("00010003", 3)),
        },
    ]
    monkeypatch.setattr(source, "_utxos", lambda: utxos)

    events = list(source.events_after(None))
    assert [event["event_key"] for event in events] == [
        "devnet:utxo:0xseq3:12",
        "devnet:utxo:0xseq4:0",
    ]

    after_seq3 = list(source.events_after(events[0]["cursor"]))
    assert [event["event_key"] for event in after_seq3] == ["devnet:utxo:0xseq4:0"]
