"""eToro loader: key-gated multi-asset OHLCV via the eToro public API.

Fetches candles from ``public-api.etoro.com`` using the ``x-api-key``/
``x-user-key`` header pair (plus a per-request UUID ``x-request-id``).
Credentials come from ``ETORO_API_KEY``/``ETORO_USER_KEY`` env vars, falling
back to the trading connector's ``~/.vibe-trading/etoro.json``. Market-data
endpoints are environment-agnostic, so either a Demo or Real user key works.

Candle endpoint shape (all selectors are path segments, not query params):
  /api/v1/market-data/instruments/{id}/history/candles/desc/{interval}/{count}
with at most 1000 candles per call and no from/to date filtering — the window
is cut client-side, so ranges reaching further than 1000 bars back from today
are truncated (logged as a warning).

Symbol convention (Vibe-Trading -> eToro):
  * ``AAPL.US`` -> ``AAPL`` (the ``.US`` suffix is dropped; eToro uses bare
    symbols). Ids are resolved via the search endpoint and cached — eToro
    instrument ids are immutable.

The shared market-data quota is 120 requests/60s per user key, so every call
routes through :mod:`backtest.loaders._http` under the ``"etoro"`` bucket.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

import pandas as pd

from backtest.loaders._http import resolve_min_interval, throttled_get
from backtest.loaders.base import cached_loader_fetch, validate_date_range
from backtest.loaders.registry import register

logger = logging.getLogger(__name__)

_BASE_URL = "https://public-api.etoro.com"
HOST_KEY = "etoro"

_MIN_INTERVAL_ENV = "VIBE_TRADING_ETORO_MIN_INTERVAL"
_DEFAULT_MIN_INTERVAL_S = 0.6

_MAX_CANDLES = 1000

#: Loader interval vocabulary → eToro candle interval enum. ``1M`` (month) is
#: rejected before this lowercase lookup so it can never collide with minutes.
_INTERVAL_MAP = {
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

_OUTPUT_COLUMNS = ["open", "high", "low", "close", "volume"]

#: Symbol → instrumentId cache; eToro instrument ids are immutable.
_INSTRUMENT_CACHE: dict[str, int] = {}


def _min_interval() -> float:
    """Resolve the per-call minimum spacing, honoring the env override."""
    return resolve_min_interval(_MIN_INTERVAL_ENV, _DEFAULT_MIN_INTERVAL_S)


def map_symbol(symbol: str) -> str:
    """Translate a Vibe-Trading symbol into eToro's bare-symbol convention."""
    token = str(symbol or "").strip().upper()
    if token.endswith(".US"):
        return token[: -len(".US")]
    return token


def _credentials() -> Optional[tuple[str, str]]:
    """Resolve (api_key, user_key) from env config, else the connector file."""
    from src.config.accessor import get_env_config

    data = get_env_config().data
    api_key = str(getattr(data, "etoro_api_key", "") or "").strip()
    user_key = str(getattr(data, "etoro_user_key", "") or "").strip()
    if api_key and user_key:
        return api_key, user_key

    from src.config.paths import get_runtime_root

    path = get_runtime_root() / "etoro.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    api_key = api_key or str(payload.get("api_key") or "").strip()
    user_key = user_key or str(payload.get("user_key") or "").strip()
    if api_key and user_key:
        return api_key, user_key
    return None


def _get_json(path: str, *, params: Optional[dict[str, Any]] = None) -> Any:
    """Authenticated throttled GET against the eToro public API."""
    creds = _credentials()
    if creds is None:
        raise RuntimeError("eToro credentials missing: set ETORO_API_KEY and ETORO_USER_KEY.")
    api_key, user_key = creds
    response = throttled_get(
        f"{_BASE_URL}{path}",
        host_key=HOST_KEY,
        min_interval=_min_interval(),
        params=params,
        headers={
            "Accept": "application/json",
            "x-api-key": api_key,
            "x-user-key": user_key,
            "x-request-id": str(uuid.uuid4()),
        },
    )
    response.raise_for_status()
    return response.json()


def _resolve_instrument_id(symbol: str) -> Optional[int]:
    """Resolve a symbol to eToro's immutable instrument id (cached)."""
    token = map_symbol(symbol)
    if not token:
        return None
    cached = _INSTRUMENT_CACHE.get(token)
    if cached is not None:
        return cached
    payload = _get_json(
        "/api/v1/market-data/search",
        params={
            "internalSymbolFull": token,
            "fields": "instrumentId,internalSymbolFull",
            "pageSize": 10,
        },
    )
    for item in (payload or {}).get("items") or []:
        if str(item.get("internalSymbolFull") or "").upper() == token and item.get("instrumentId") is not None:
            instrument_id = int(item["instrumentId"])
            _INSTRUMENT_CACHE[token] = instrument_id
            return instrument_id
    return None


@register
class DataLoader:
    """eToro multi-asset OHLCV loader (key-gated REST)."""

    name = "etoro"
    markets = {"us_equity", "crypto", "forex"}
    requires_auth = True

    def __init__(self) -> None:
        pass

    def is_available(self) -> bool:
        """Available when the api/user key pair is configured."""
        return _credentials() is not None

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[List[str]] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch OHLCV bars for ``codes`` over ``[start_date, end_date]``.

        Args:
            codes: Project-side symbols (e.g. ``["AAPL.US", "BTC"]``).
            start_date: Inclusive start date (``YYYY-MM-DD``).
            end_date: Inclusive end date (``YYYY-MM-DD``).
            interval: Bar size in the loader vocabulary; monthly (``1M``) is
                unsupported and returns an empty map so fallbacks can continue.
            fields: Unused — candles always carry the full OHLCV set.

        Returns:
            Mapping ``{symbol: DataFrame}`` for symbols that returned data. Each
            frame has a ``DatetimeIndex`` named ``trade_date`` and float columns
            ``open/high/low/close/volume`` in ascending date order. A symbol
            that errors or has no data is omitted, never aborting the batch.

        Raises:
            ValueError: If ``start_date > end_date`` or a date is unparseable.
        """
        validate_date_range(start_date, end_date)

        token = str(interval or "").strip()
        if token == "1M":
            logger.warning("etoro has no monthly candles; rejecting interval=%r", interval)
            return {}
        api_interval = _INTERVAL_MAP.get(token.lower())
        if api_interval is None:
            logger.warning("etoro does not support interval=%r", interval)
            return {}

        result: Dict[str, pd.DataFrame] = {}
        for code in codes:
            try:
                df = cached_loader_fetch(
                    source=self.name,
                    symbol=code,
                    timeframe=interval,
                    start_date=start_date,
                    end_date=end_date,
                    fields=None,
                    fetch=lambda code=code: self._fetch_one(code, start_date, end_date, api_interval, token.lower()),
                )
                if df is not None and not df.empty:
                    result[code] = df
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not abort the batch
                logger.warning("etoro failed for %s: %s", code, exc)
        return result

    def _fetch_one(
        self,
        code: str,
        start_date: str,
        end_date: str,
        api_interval: str,
        interval_token: str,
    ) -> Optional[pd.DataFrame]:
        """Fetch and window one symbol's candles; ``None`` when unknown/empty."""
        instrument_id = _resolve_instrument_id(code)
        if instrument_id is None:
            logger.warning("etoro instrument not found for %s", code)
            return None

        count = _candles_count(start_date, interval_token)
        payload = _get_json(
            f"/api/v1/market-data/instruments/{instrument_id}/history/candles/desc/{api_interval}/{count}"
        )
        frame = _candles_to_frame(payload)
        if frame is None:
            return None
        if count >= _MAX_CANDLES and not frame.empty and frame.index.min() > pd.Timestamp(start_date):
            logger.warning(
                "etoro candle window truncated for %s: %s bars max, earliest %s > requested %s",
                code,
                _MAX_CANDLES,
                frame.index.min().date(),
                start_date,
            )
        frame = frame.loc[(frame.index >= pd.Timestamp(start_date)) & (frame.index <= pd.Timestamp(end_date) + pd.Timedelta(days=1))]
        return frame if not frame.empty else None


def _candles_count(start_date: str, interval_token: str) -> int:
    """Bars needed to reach back from today to ``start_date``, capped at 1000.

    The endpoint has no date-range parameters and pages newest-first, so the
    count must span from *now* back to the window start. Intraday intervals
    just take the maximum — the window is cut client-side either way.
    """
    span_days = max(1, (pd.Timestamp.now(tz="UTC").tz_localize(None) - pd.Timestamp(start_date)).days + 3)
    if interval_token == "1d":
        needed = span_days
    elif interval_token == "1w":
        needed = span_days // 7 + 2
    else:
        needed = _MAX_CANDLES
    return max(1, min(needed, _MAX_CANDLES))


def _candles_to_frame(payload: Any) -> Optional[pd.DataFrame]:
    """Convert the doubly-nested candle payload into our OHLCV frame."""
    groups = (payload or {}).get("candles") if isinstance(payload, dict) else None
    rows = (groups or [{}])[0].get("candles") if groups else None
    if not rows:
        return None
    frame = pd.DataFrame(rows)
    if "fromDate" not in frame.columns:
        return None
    frame["trade_date"] = pd.to_datetime(frame["fromDate"], errors="coerce").dt.tz_localize(None)
    frame = frame.dropna(subset=["trade_date"]).set_index("trade_date").sort_index()
    frame.index.name = "trade_date"
    for col in _OUTPUT_COLUMNS:
        if col not in frame.columns:
            return None
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame[_OUTPUT_COLUMNS].dropna(subset=["open", "high", "low", "close"])
    if frame.empty:
        return None
    return frame.astype(float)
