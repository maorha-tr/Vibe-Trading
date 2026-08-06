"""Unit tests for the eToro market-data loader (no network)."""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.loaders import etoro_loader

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    etoro_loader._INSTRUMENT_CACHE.clear()
    yield
    etoro_loader._INSTRUMENT_CACHE.clear()


_SEARCH_PAYLOAD = {"items": [{"instrumentId": 1001, "internalSymbolFull": "AAPL"}]}

_CANDLES_PAYLOAD = {
    "interval": "OneDay",
    "candles": [
        {
            "instrumentId": 1001,
            "candles": [
                {"instrumentID": 1001, "fromDate": "2025-01-06T00:00:00Z", "open": 3, "high": 4, "low": 2, "close": 3.5, "volume": 30},
                {"instrumentID": 1001, "fromDate": "2025-01-03T00:00:00Z", "open": 2, "high": 3, "low": 1, "close": 2.5, "volume": 20},
                {"instrumentID": 1001, "fromDate": "2025-01-02T00:00:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
                {"instrumentID": 1001, "fromDate": "2024-12-30T00:00:00Z", "open": 9, "high": 9, "low": 9, "close": 9, "volume": 1},
            ],
        }
    ],
}


def _patch_api(monkeypatch):
    def fake_get_json(path, *, params=None):
        if "/market-data/search" in path:
            return _SEARCH_PAYLOAD
        assert "/history/candles/desc/OneDay/" in path
        return _CANDLES_PAYLOAD

    monkeypatch.setattr(etoro_loader, "_get_json", fake_get_json)


def test_registered_in_registry() -> None:
    from backtest.loaders.registry import FALLBACK_CHAINS, LOADER_REGISTRY, VALID_SOURCES, _ensure_registered

    assert "etoro" in VALID_SOURCES
    _ensure_registered()
    assert LOADER_REGISTRY["etoro"] is etoro_loader.DataLoader
    for market in ("us_equity", "crypto", "forex"):
        assert "etoro" in FALLBACK_CHAINS[market]


def test_unavailable_without_credentials(monkeypatch) -> None:
    monkeypatch.setattr(etoro_loader, "_credentials", lambda: None)
    assert not etoro_loader.DataLoader().is_available()


def test_available_with_credentials(monkeypatch) -> None:
    monkeypatch.setattr(etoro_loader, "_credentials", lambda: ("k", "u"))
    assert etoro_loader.DataLoader().is_available()


def test_fetch_windows_and_sorts(monkeypatch) -> None:
    _patch_api(monkeypatch)
    result = etoro_loader.DataLoader().fetch(["AAPL.US"], "2025-01-02", "2025-01-06", interval="1D")
    assert set(result) == {"AAPL.US"}
    frame = result["AAPL.US"]
    assert frame.index.name == "trade_date"
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    # The 2024-12-30 row is outside the requested window and must be cut.
    assert list(frame["close"]) == [1.5, 2.5, 3.5]
    assert frame.index.is_monotonic_increasing
    assert all(frame.dtypes == float)


def test_fetch_rejects_month_interval(monkeypatch) -> None:
    _patch_api(monkeypatch)
    assert etoro_loader.DataLoader().fetch(["AAPL.US"], "2025-01-01", "2025-01-31", interval="1M") == {}


def test_unknown_symbol_omitted_not_fatal(monkeypatch) -> None:
    def fake_get_json(path, *, params=None):
        if "/market-data/search" in path:
            return {"items": []}
        raise AssertionError("candles must not be fetched for unknown symbols")

    monkeypatch.setattr(etoro_loader, "_get_json", fake_get_json)
    assert etoro_loader.DataLoader().fetch(["ZZZQ.US"], "2025-01-02", "2025-01-06", interval="1D") == {}


def test_map_symbol_strips_us_suffix() -> None:
    assert etoro_loader.map_symbol("aapl.us") == "AAPL"
    assert etoro_loader.map_symbol("BTC") == "BTC"


def test_candles_count_daily_span() -> None:
    count = etoro_loader._candles_count("2025-01-01", "1d")
    assert 1 <= count <= 1000


def test_credentials_fall_back_to_connector_file(monkeypatch, tmp_path) -> None:
    import json

    (tmp_path / "etoro.json").write_text(json.dumps({"api_key": "fk", "user_key": "fu"}), encoding="utf-8")
    creds = etoro_loader._credentials()
    assert creds == ("fk", "fu")
