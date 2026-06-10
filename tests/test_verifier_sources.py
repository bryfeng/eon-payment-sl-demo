from verifier_engine.sources import BaseLayerAPIEventSource


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
