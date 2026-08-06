"""Unit tests for the eToro SDK connector (no network)."""

from __future__ import annotations

import json

import pytest

from src.trading.connectors.etoro import sdk
from src.trading.profiles import BUILTIN_PROFILES

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    sdk._INSTRUMENT_CACHE.clear()
    yield
    sdk._INSTRUMENT_CACHE.clear()


class _Resp:
    def __init__(self, payload, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.content = json.dumps(payload).encode() if payload is not None else b""
        self.text = json.dumps(payload) if payload is not None else ""
        self.reason = "OK"

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _patch_api(monkeypatch, handler):
    """Route sdk HTTP calls through ``handler(method, url, kwargs) -> _Resp``."""
    calls: list[tuple[str, str, dict]] = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return handler(method, url, kwargs)

    monkeypatch.setattr(sdk.requests, "request", fake_request)
    return calls


def _demo_trade_cfg() -> sdk.EtoroConfig:
    return sdk.EtoroConfig(api_key="appkey", user_key="userkey", profile="paper", readonly=False)


_SEARCH_PAYLOAD = {"items": [{"instrumentId": 1001, "internalSymbolFull": "AAPL", "displayname": "Apple"}]}


# ---------------------------------------------------------------------------
# Profiles / config
# ---------------------------------------------------------------------------


def test_etoro_profiles_registered() -> None:
    ids = {p.id for p in BUILTIN_PROFILES if p.connector == "etoro"}
    assert ids == {"etoro-demo-sdk", "etoro-demo-trade", "etoro-live-sdk-readonly"}


def test_etoro_exposes_no_live_trade_profile() -> None:
    for profile in BUILTIN_PROFILES:
        if profile.connector != "etoro":
            continue
        if profile.environment == "live":
            assert profile.readonly
            assert not any(cap.startswith("orders.place") for cap in profile.capabilities)


def test_config_rejects_unknown_profile() -> None:
    with pytest.raises(sdk.EtoroConfigError):
        sdk.EtoroConfig.from_mapping({"profile": "demo"})


def test_config_rejects_bad_base_url() -> None:
    with pytest.raises(sdk.EtoroConfigError):
        sdk.EtoroConfig.from_mapping({"base_url": "ftp://x"})


def test_build_config_filters_overrides() -> None:
    cfg = sdk.build_config({"profile": "paper"}, {"api_key": "abc", "bogus": "zzz", "user_key": ""})
    assert cfg.api_key == "abc"
    assert cfg.user_key == ""
    assert cfg.profile == "paper"


def test_public_config_redacts_secrets() -> None:
    cfg = sdk.EtoroConfig(api_key="appkey1234567", user_key="secretuserkey")
    public = sdk._public_config(cfg)
    assert public["api_key"] == "appk***"
    assert public["user_key"] == "***redacted***"
    assert "secretuserkey" not in json.dumps(public)


def test_save_and_load_config_roundtrip(tmp_path) -> None:
    path = sdk.save_config(sdk.EtoroConfig(api_key="k", user_key="u", profile="paper"))
    assert path == sdk.config_path()
    cfg = sdk.load_config()
    assert (cfg.api_key, cfg.user_key, cfg.profile) == ("k", "u", "paper")


# ---------------------------------------------------------------------------
# Order guards (no network may be touched)
# ---------------------------------------------------------------------------


def test_place_order_rejects_bad_side() -> None:
    out = sdk.place_order(_demo_trade_cfg(), symbol="AAPL", side="hold", quantity=1)
    assert out["status"] == "error"


def test_place_order_rejects_both_qty_and_notional() -> None:
    out = sdk.place_order(_demo_trade_cfg(), symbol="AAPL", side="buy", quantity=1, notional=100)
    assert out["status"] == "error"


def test_place_order_buy_requires_some_size() -> None:
    out = sdk.place_order(_demo_trade_cfg(), symbol="AAPL", side="buy")
    assert out["status"] == "error"


def test_place_order_limit_requires_price() -> None:
    out = sdk.place_order(_demo_trade_cfg(), symbol="AAPL", side="buy", quantity=1, order_type="limit")
    assert out["status"] == "error"


def test_place_order_refused_on_live_profiles() -> None:
    for profile in ("live-readonly", "live"):
        cfg = sdk.EtoroConfig(api_key="k", user_key="u", profile=profile, readonly=False)
        out = sdk.place_order(cfg, symbol="AAPL", side="buy", quantity=1)
        assert out["status"] == "error" and "demo" in out["error"].lower()
        out2 = sdk.cancel_order(cfg, "42")
        assert out2["status"] == "error" and "demo" in out2["error"].lower()


def test_place_order_refused_when_readonly() -> None:
    cfg = sdk.EtoroConfig(api_key="k", user_key="u", profile="paper", readonly=True)
    out = sdk.place_order(cfg, symbol="AAPL", side="buy", quantity=1)
    assert out["status"] == "error" and "read-only" in out["error"]


# ---------------------------------------------------------------------------
# Demo order flows (mocked HTTP)
# ---------------------------------------------------------------------------


def test_demo_buy_by_units_hits_demo_route(monkeypatch) -> None:
    def handler(method, url, kwargs):
        if "/market-data/search" in url:
            return _Resp(_SEARCH_PAYLOAD)
        assert method == "POST" and url.endswith("/api/v2/trading/execution/demo/orders")
        body = kwargs["json"]
        assert body["instrumentId"] == 1001
        assert "symbol" not in body
        assert body["units"] == 2.0 and "amount" not in body
        assert body["settlementType"] == "real" and body["leverage"] == 1
        assert body["orderType"] == "mkt"
        return _Resp({"orderId": 555, "referenceId": kwargs["headers"]["x-request-id"], "token": "tok"})

    _patch_api(monkeypatch, handler)
    out = sdk.place_order(_demo_trade_cfg(), symbol="AAPL.US", side="buy", quantity=2)
    assert out["status"] == "ok"
    assert out["order_id"] == "555"
    assert out["reference_id"]


def test_demo_buy_by_notional_and_limit(monkeypatch) -> None:
    def handler(method, url, kwargs):
        if "/market-data/search" in url:
            return _Resp(_SEARCH_PAYLOAD)
        body = kwargs["json"]
        assert body["amount"] == 250.0 and "units" not in body
        assert body["orderType"] == "limitIOC" and body["limitRate"] == 190.5
        return _Resp({"orderId": 7, "referenceId": "r", "token": "t"})

    _patch_api(monkeypatch, handler)
    out = sdk.place_order(
        _demo_trade_cfg(), symbol="AAPL", side="buy", notional=250, order_type="limit", limit_price=190.5
    )
    assert out["status"] == "ok"


def test_demo_sell_closes_positions_with_demo_casing(monkeypatch) -> None:
    portfolio = {
        "clientPortfolio": {
            "positions": [
                {"positionID": 2, "instrumentID": 1001, "units": 5.0, "mirrorID": 0, "openDateTime": "2025-02-01T00:00:00Z"},
                {"positionID": 1, "instrumentID": 1001, "units": 3.0, "mirrorID": 0, "openDateTime": "2025-01-01T00:00:00Z"},
                {"positionID": 9, "instrumentID": 1001, "units": 8.0, "mirrorID": 77, "openDateTime": "2024-01-01T00:00:00Z"},
                {"positionID": 4, "instrumentID": 2222, "units": 1.0, "mirrorID": 0, "openDateTime": "2024-01-01T00:00:00Z"},
            ]
        }
    }
    close_calls: list[tuple[str, dict]] = []

    def handler(method, url, kwargs):
        if "/market-data/search" in url:
            return _Resp(_SEARCH_PAYLOAD)
        if url.endswith("/api/v1/trading/info/demo/portfolio"):
            return _Resp(portfolio)
        assert "/api/v1/trading/execution/demo/market-close-orders/positions/" in url
        close_calls.append((url, kwargs["json"]))
        return _Resp({"orderForClose": {"orderID": 100 + len(close_calls)}, "token": "t"})

    _patch_api(monkeypatch, handler)
    out = sdk.place_order(_demo_trade_cfg(), symbol="AAPL", side="sell", quantity=4)
    assert out["status"] == "ok"
    # Oldest position (id 1, 3 units) closes fully, then 1 unit deducted from id 2.
    assert [url.rsplit("/", 1)[1] for url, _ in close_calls] == ["1", "2"]
    assert close_calls[0][1] == {"InstrumentID": 1001, "UnitsToDeduct": None}
    assert close_calls[1][1] == {"InstrumentID": 1001, "UnitsToDeduct": 1.0}
    assert len(out["closes"]) == 2


def test_demo_sell_without_units_closes_everything(monkeypatch) -> None:
    portfolio = {
        "clientPortfolio": {
            "positions": [
                {"positionID": 1, "instrumentID": 1001, "units": 3.0, "mirrorID": 0, "openDateTime": "2025-01-01T00:00:00Z"},
                {"positionID": 2, "instrumentID": 1001, "units": 5.0, "mirrorID": 0, "openDateTime": "2025-02-01T00:00:00Z"},
            ]
        }
    }
    bodies: list[dict] = []

    def handler(method, url, kwargs):
        if "/market-data/search" in url:
            return _Resp(_SEARCH_PAYLOAD)
        if url.endswith("/demo/portfolio"):
            return _Resp(portfolio)
        bodies.append(kwargs["json"])
        return _Resp({"orderForClose": {"orderID": 1}, "token": "t"})

    _patch_api(monkeypatch, handler)
    out = sdk.place_order(_demo_trade_cfg(), symbol="AAPL", side="sell")
    assert out["status"] == "ok"
    assert bodies == [
        {"InstrumentID": 1001, "UnitsToDeduct": None},
        {"InstrumentID": 1001, "UnitsToDeduct": None},
    ]


def test_cancel_order_demo_route(monkeypatch) -> None:
    def handler(method, url, kwargs):
        assert method == "DELETE" and url.endswith("/api/v2/trading/execution/demo/orders/42")
        return _Resp({"token": "tok"})

    _patch_api(monkeypatch, handler)
    out = sdk.cancel_order(_demo_trade_cfg(), "42")
    assert out["status"] == "ok" and out["token"] == "tok"


# ---------------------------------------------------------------------------
# Market data / reads (mocked HTTP)
# ---------------------------------------------------------------------------


def test_get_quote(monkeypatch) -> None:
    def handler(method, url, kwargs):
        if "/market-data/search" in url:
            return _Resp(_SEARCH_PAYLOAD)
        assert url.endswith("/api/v1/market-data/instruments/rates")
        assert kwargs["params"] == {"instrumentIds": "1001"}
        return _Resp({"rates": [{"instrumentID": 1001, "bid": 189.9, "ask": 190.1, "lastExecution": 190.0, "date": "d"}]})

    _patch_api(monkeypatch, handler)
    out = sdk.get_quote("AAPL.US", config=_demo_trade_cfg())
    assert out["status"] == "ok"
    assert (out["bid"], out["ask"], out["last"]) == (189.9, 190.1, 190.0)


def test_get_historical_bars_ascending(monkeypatch) -> None:
    candles = {
        "interval": "OneDay",
        "candles": [
            {
                "instrumentId": 1001,
                "candles": [
                    {"instrumentID": 1001, "fromDate": "2025-01-03T00:00:00Z", "open": 2, "high": 3, "low": 1, "close": 2.5, "volume": 10},
                    {"instrumentID": 1001, "fromDate": "2025-01-02T00:00:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 5},
                ],
            }
        ],
    }

    def handler(method, url, kwargs):
        if "/market-data/search" in url:
            return _Resp(_SEARCH_PAYLOAD)
        assert "/history/candles/desc/OneDay/30" in url
        return _Resp(candles)

    _patch_api(monkeypatch, handler)
    out = sdk.get_historical_bars("AAPL", config=_demo_trade_cfg(), period="1d", limit=30)
    assert out["status"] == "ok"
    assert [bar["close"] for bar in out["bars"]] == [1.5, 2.5]


def test_get_historical_bars_rejects_month_and_unknown_periods() -> None:
    cfg = _demo_trade_cfg()
    out = sdk.get_historical_bars("AAPL", config=cfg, period="1M")
    assert out["status"] == "error" and "monthly" in out["error"]
    out2 = sdk.get_historical_bars("AAPL", config=cfg, period="3d")
    assert out2["status"] == "error"


def test_timeframe_map_covers_generic_vocabulary() -> None:
    assert sdk._PERIOD_TO_INTERVAL == {
        "1m": "OneMinute",
        "5m": "FiveMinutes",
        "10m": "TenMinutes",
        "15m": "FifteenMinutes",
        "30m": "ThirtyMinutes",
        "1h": "OneHour",
        "4h": "FourHours",
        "1d": "OneDay",
        "1w": "OneWeek",
    }


def test_check_status_reports_missing_config() -> None:
    report = sdk.check_status(sdk.EtoroConfig())
    assert report["status"] == "error"
    assert "api_key" in report["error"] and "user_key" in report["error"]


def test_auth_failure_message_mentions_environment(monkeypatch) -> None:
    _patch_api(monkeypatch, lambda method, url, kwargs: _Resp({"message": "no"}, status=401))
    out = sdk.get_quote("AAPL", config=_demo_trade_cfg())
    assert out["status"] == "error" and "Demo" in out["error"]
