from pathlib import Path


def test_no_bybit_order_execution_surface_exists():
    banned_fragments = [
        "/v5/order/create",
        "/v5/order/amend",
        "/v5/order/cancel",
        "/v5/position/set-leverage",
        "/v5/position/trading-stop",
        "/v5/asset/transfer",
        "place_order",
        "create_order",
        "cancel_order",
        "amend_order",
        "set_leverage",
        "submit order",
    ]
    source = "\n".join(path.read_text() for path in Path("app").rglob("*.py"))
    lowered = source.lower()
    for fragment in banned_fragments:
        assert fragment not in lowered
