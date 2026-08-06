"""eToro connector via the public REST API (``public-api.etoro.com``).

Auth is the ``x-api-key``/``x-user-key`` header pair plus a fresh UUID
``x-request-id`` on every call; for order creation the request id doubles as
the idempotency key and is echoed back as ``referenceId``.

Demo-vs-real safety boundary is structural on two independent axes:

* **Path** — demo trading routes insert a ``demo`` segment (e.g.
  ``/api/v2/trading/execution/demo/orders``); paper profiles only ever build
  demo paths.
* **Credential** — an eToro user key is generated for exactly one environment
  (Demo or Real) and is rejected by the other environment's routes.

Order placement is only offered on paper (demo) profiles. ``side="buy"``
opens an unleveraged long (``settlementType="real"``, leverage 1);
``side="sell"`` exits existing positions in the symbol via eToro's
market-close endpoint (eToro's order endpoint only supports opens — shorting
is intentionally not exposed here). Real-account profiles are read-only and
refuse order calls before any REST client is touched.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import requests

from src.config.paths import get_runtime_root

CONFIG_FILENAME = "etoro.json"
DEFAULT_BASE_URL = "https://public-api.etoro.com"

PROFILE_ENVIRONMENTS = {
    "paper": "paper",
    "live-readonly": "live",
    "live": "live",
}

#: Generic ``period`` vocabulary → eToro candle interval enum. Case matters:
#: ``1m`` is minutes; ``1M`` (month) has no eToro interval and is rejected.
_PERIOD_TO_INTERVAL = {
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

_MAX_CANDLES = 1000


class EtoroConfigError(RuntimeError):
    """Raised when the eToro connector configuration is missing/invalid."""


class EtoroAPIError(RuntimeError):
    """Raised when eToro returns an auth, HTTP, network, or JSON error."""


@dataclass(frozen=True)
class EtoroConfig:
    """eToro connector connection settings.

    Args:
        api_key: Application key sent as ``x-api-key``.
        user_key: User key sent as ``x-user-key``. Generated per environment in
            eToro Settings → Trading → API Key Management; a Demo key cannot
            reach real routes and vice versa.
        profile: ``paper`` (demo routes), ``live-readonly`` or ``live`` (real
            routes; order placement refused either way in this layer).
        base_url: Public API base URL.
        timeout: Network timeout in seconds.
        readonly: Refuse order placement/cancellation when true.
    """

    api_key: str = ""
    user_key: str = ""
    profile: str = "paper"
    base_url: str = DEFAULT_BASE_URL
    timeout: float = 15.0
    readonly: bool = True

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None = None) -> "EtoroConfig":
        """Build a config from a JSON-like mapping, normalizing profile/URL."""
        payload = dict(data or {})
        profile = str(payload.get("profile") or "paper").strip().lower()
        if profile not in PROFILE_ENVIRONMENTS:
            raise EtoroConfigError("profile must be 'paper', 'live-readonly' or 'live'")
        base_url = str(payload.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise EtoroConfigError("base_url must start with http:// or https://")
        return cls(
            api_key=str(payload.get("api_key") or "").strip(),
            user_key=str(payload.get("user_key") or "").strip(),
            profile=profile,
            base_url=base_url,
            timeout=float(payload.get("timeout") or 15.0),
            readonly=bool(payload.get("readonly", True)),
        )

    def with_overrides(
        self,
        *,
        api_key: str | None = None,
        user_key: str | None = None,
        profile: str | None = None,
        base_url: str | None = None,
    ) -> "EtoroConfig":
        """Return a copy with CLI/tool overrides applied."""
        payload = asdict(self)
        if api_key is not None:
            payload["api_key"] = api_key
        if user_key is not None:
            payload["user_key"] = user_key
        if profile is not None:
            payload["profile"] = profile
        if base_url is not None:
            payload["base_url"] = base_url
        return EtoroConfig.from_mapping(payload)

    @property
    def environment(self) -> str:
        """Return ``paper`` or ``live`` for the configured profile."""
        return PROFILE_ENVIRONMENTS.get(self.profile, "live")

    @property
    def is_demo(self) -> bool:
        """True when this config targets eToro's demo (virtual) routes."""
        return self.environment == "paper"


_OVERRIDE_KEYS = ("api_key", "user_key", "profile", "base_url")


def build_config(
    profile_config: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> EtoroConfig:
    """Resolve config: saved file ← profile defaults ← CLI overrides."""
    base = asdict(load_config())
    for key, value in dict(profile_config or {}).items():
        if value is not None:
            base[key] = value
    cfg = EtoroConfig.from_mapping(base)
    clean = {k: v for k, v in dict(overrides or {}).items() if k in _OVERRIDE_KEYS and v not in (None, "")}
    return cfg.with_overrides(**clean) if clean else cfg


def config_path() -> Path:
    """Return the user-level eToro config path."""
    return get_runtime_root() / CONFIG_FILENAME


def load_config() -> EtoroConfig:
    """Load eToro settings from ``~/.vibe-trading/etoro.json``."""
    path = config_path()
    if not path.exists():
        return EtoroConfig()
    try:
        return EtoroConfig.from_mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise EtoroConfigError(f"invalid eToro config at {path}: {exc}") from exc


def save_config(config: EtoroConfig) -> Path:
    """Persist eToro settings with owner-only permissions."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def etoro_available() -> bool:
    """The connector is pure REST on ``requests`` — always importable."""
    return True


# ---------------------------------------------------------------------------
# Demo/real path construction
# ---------------------------------------------------------------------------


def _p_portfolio(cfg: EtoroConfig) -> str:
    return "/api/v1/trading/info/demo/portfolio" if cfg.is_demo else "/api/v1/trading/info/portfolio"


def _p_create_order(cfg: EtoroConfig) -> str:
    return "/api/v2/trading/execution/demo/orders" if cfg.is_demo else "/api/v2/trading/execution/orders"


def _p_cancel_order(cfg: EtoroConfig, order_id: str) -> str:
    stem = "/api/v2/trading/execution/demo/orders" if cfg.is_demo else "/api/v2/trading/execution/orders"
    return f"{stem}/{order_id}"


def _p_close_position(cfg: EtoroConfig, position_id: int) -> str:
    stem = (
        "/api/v1/trading/execution/demo/market-close-orders/positions"
        if cfg.is_demo
        else "/api/v1/trading/execution/market-close-orders/positions"
    )
    return f"{stem}/{position_id}"


def _p_lookup_order(cfg: EtoroConfig) -> str:
    return "/api/v2/trading/info/demo/orders:lookup" if cfg.is_demo else "/api/v2/trading/info/orders:lookup"


# ---------------------------------------------------------------------------
# HTTP core
# ---------------------------------------------------------------------------


def _missing_fields(config: EtoroConfig) -> list[str]:
    missing = []
    if not config.api_key:
        missing.append("api_key")
    if not config.user_key:
        missing.append("user_key")
    return missing


def _request(
    config: EtoroConfig,
    method: str,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
    json_body: Any = None,
    request_id: str | None = None,
) -> Any:
    """Run an HTTP request and normalize eToro failure modes."""
    missing = _missing_fields(config)
    if missing:
        raise EtoroConfigError(f"eToro connector not configured: missing {', '.join(missing)}.")

    url = f"{config.base_url.rstrip('/')}/{path.lstrip('/')}"
    headers = {
        "Accept": "application/json",
        "x-api-key": config.api_key,
        "x-user-key": config.user_key,
        "x-request-id": request_id or str(uuid.uuid4()),
    }
    try:
        response = requests.request(
            method.upper(),
            url,
            headers=headers,
            params=dict(params or {}),
            json=json_body,
            timeout=config.timeout,
        )
    except requests.RequestException as exc:
        raise EtoroAPIError(f"eToro request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise EtoroAPIError(
            "eToro API authentication failed: check api_key/user_key and that the "
            f"user key was generated for the {'Demo' if config.is_demo else 'Real'} environment."
        )
    if response.status_code == 429:
        raise EtoroAPIError("eToro API rate limit hit (429): per-user-key rolling 60s window; retry with backoff.")
    if response.status_code >= 400:
        raise EtoroAPIError(f"eToro API returned HTTP {response.status_code}: {_error_message(response)}")
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise EtoroAPIError("eToro API returned invalid JSON.") from exc


def _error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip() or response.reason or "request failed"
    if isinstance(payload, Mapping):
        for key in ("message", "error", "errorMessage", "detail", "title"):
            value = payload.get(key)
            if value:
                return str(value)
    return str(payload)


def _public_config(config: EtoroConfig) -> dict[str, Any]:
    """Config snapshot with credentials redacted."""
    data = asdict(config)
    if data.get("api_key"):
        data["api_key"] = data["api_key"][:4] + "***"
    if data.get("user_key"):
        data["user_key"] = "***redacted***"
    return data


# ---------------------------------------------------------------------------
# Instrument resolution
# ---------------------------------------------------------------------------

#: Symbol → instrumentId cache. eToro instrument ids are immutable.
_INSTRUMENT_CACHE: dict[str, int] = {}


def map_symbol(symbol: str) -> str:
    """Translate a project-side symbol into eToro's symbol convention.

    eToro uses bare symbols (``AAPL``, ``BTC``); the project's ``.US`` market
    suffix is stripped, anything else passes through upper-cased.
    """
    token = str(symbol or "").strip().upper()
    if token.endswith(".US"):
        return token[: -len(".US")]
    return token


def _resolve_instrument_id(config: EtoroConfig, symbol: str) -> int:
    """Resolve a symbol to eToro's immutable instrument id (cached)."""
    token = map_symbol(symbol)
    if not token:
        raise EtoroAPIError("empty symbol")
    cached = _INSTRUMENT_CACHE.get(token)
    if cached is not None:
        return cached
    payload = _request(
        config,
        "GET",
        "/api/v1/market-data/search",
        params={
            "internalSymbolFull": token,
            "fields": "instrumentId,internalSymbolFull,displayname",
            "pageSize": 10,
        },
    )
    items = payload.get("items") if isinstance(payload, Mapping) else None
    for item in items or []:
        if str(item.get("internalSymbolFull") or "").upper() == token and item.get("instrumentId") is not None:
            instrument_id = int(item["instrumentId"])
            _INSTRUMENT_CACHE[token] = instrument_id
            return instrument_id
    raise EtoroAPIError(f"eToro instrument not found for symbol '{symbol}'.")


def _symbols_for_ids(config: EtoroConfig, instrument_ids: list[int]) -> dict[int, str]:
    """Best-effort reverse map instrumentId → symbolFull for display."""
    if not instrument_ids:
        return {}
    try:
        payload = _request(
            config,
            "GET",
            "/api/v1/market-data/instruments",
            params={"instrumentIds": ",".join(str(i) for i in sorted(set(instrument_ids)))},
        )
        rows = payload.get("instrumentDisplayDatas") if isinstance(payload, Mapping) else None
        return {
            int(row["instrumentID"]): str(row.get("symbolFull") or "")
            for row in rows or []
            if row.get("instrumentID") is not None
        }
    except (EtoroAPIError, EtoroConfigError, ValueError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def check_status(config: EtoroConfig | None = None) -> dict[str, Any]:
    """Check REST readiness and config completeness without mutating broker state."""
    cfg = config or load_config()
    report: dict[str, Any] = {
        "status": "ok",
        "config": _public_config(cfg),
        "sdk": {"package": "requests", "installed": True},
        "base_url": cfg.base_url,
    }

    missing = _missing_fields(cfg)
    if missing:
        report["status"] = "error"
        report["error"] = f"eToro connector not configured: missing {', '.join(missing)}."
        return report

    try:
        me = _request(cfg, "GET", "/api/v1/me")
        portfolio = _request(cfg, "GET", _p_portfolio(cfg))
    except (EtoroConfigError, EtoroAPIError) as exc:
        report["status"] = "error"
        report["error"] = str(exc)
        return report
    except Exception as exc:  # noqa: BLE001 - health endpoint reports cleanly
        report["status"] = "error"
        report["error"] = f"eToro connector check failed: {exc}"
        return report

    client = portfolio.get("clientPortfolio") if isinstance(portfolio, Mapping) else None
    report["account"] = {
        "profile": cfg.profile,
        "username": (me or {}).get("username"),
        "gcid": (me or {}).get("gcid"),
        "credit_usd": (client or {}).get("credit"),
    }
    return report


def get_account_snapshot(config: EtoroConfig | None = None) -> dict[str, Any]:
    """Fetch demo/real account cash and position/order counts."""
    cfg = config or load_config()
    payload = _request(cfg, "GET", _p_portfolio(cfg))
    client = payload.get("clientPortfolio") if isinstance(payload, Mapping) else {}
    client = client or {}
    return {
        "status": "ok",
        "profile": cfg.profile,
        "environment": cfg.environment,
        "cash": {"credit_usd": client.get("credit"), "bonus_credit_usd": client.get("bonusCredit")},
        "positions_count": len(client.get("positions") or []),
        "open_orders_count": len(client.get("orders") or []),
        "mirrors_count": len(client.get("mirrors") or []),
    }


def get_positions(config: EtoroConfig | None = None) -> dict[str, Any]:
    """Fetch current portfolio positions."""
    cfg = config or load_config()
    payload = _request(cfg, "GET", _p_portfolio(cfg))
    client = payload.get("clientPortfolio") if isinstance(payload, Mapping) else {}
    rows = (client or {}).get("positions") or []
    symbols = _symbols_for_ids(cfg, [int(r["instrumentID"]) for r in rows if r.get("instrumentID") is not None])
    return {
        "status": "ok",
        "profile": cfg.profile,
        "environment": cfg.environment,
        "positions": [_position_to_dict(row, symbols) for row in rows],
    }


def get_open_orders(config: EtoroConfig | None = None, *, include_executions: bool = False) -> dict[str, Any]:
    """Fetch pending open orders (and optionally recent closed-trade history)."""
    cfg = config or load_config()
    payload = _request(cfg, "GET", _p_portfolio(cfg))
    client = payload.get("clientPortfolio") if isinstance(payload, Mapping) else {}
    rows = (client or {}).get("orders") or []
    symbols = _symbols_for_ids(cfg, [int(r["instrumentID"]) for r in rows if r.get("instrumentID") is not None])
    result: dict[str, Any] = {
        "status": "ok",
        "profile": cfg.profile,
        "environment": cfg.environment,
        "open_orders": [_order_to_dict(row, symbols) for row in rows],
    }
    if include_executions:
        from datetime import datetime, timedelta, timezone

        min_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        path = "/api/v1/trading/info/trade/demo/history" if cfg.is_demo else "/api/v1/trading/info/trade/history"
        try:
            history = _request(cfg, "GET", path, params={"minDate": min_date, "pageSize": 100})
            result["executions"] = list(history or [])
        except (EtoroAPIError, EtoroConfigError) as exc:
            result["executions"] = []
            result["executions_error"] = str(exc)
    return result


def get_quote(symbol: str, *, config: EtoroConfig | None = None, **_: Any) -> dict[str, Any]:
    """Fetch the current bid/ask/last rate for one symbol."""
    cfg = config or load_config()
    try:
        instrument_id = _resolve_instrument_id(cfg, symbol)
        payload = _request(
            cfg,
            "GET",
            "/api/v1/market-data/instruments/rates",
            params={"instrumentIds": str(instrument_id)},
        )
    except (EtoroAPIError, EtoroConfigError) as exc:
        return {"status": "error", "profile": cfg.profile, "symbol": map_symbol(symbol), "error": str(exc)}
    rates = payload.get("rates") if isinstance(payload, Mapping) else None
    if not rates:
        return {
            "status": "error",
            "profile": cfg.profile,
            "symbol": map_symbol(symbol),
            "error": f"eToro returned no rate for '{symbol}'.",
        }
    rate = rates[0]
    return {
        "status": "ok",
        "profile": cfg.profile,
        "symbol": map_symbol(symbol),
        "instrument_id": rate.get("instrumentID"),
        "bid": rate.get("bid"),
        "ask": rate.get("ask"),
        "last": rate.get("lastExecution"),
        "timestamp": rate.get("date"),
    }


def get_historical_bars(
    symbol: str,
    *,
    config: EtoroConfig | None = None,
    period: str = "1d",
    limit: int = 90,
    **_: Any,
) -> dict[str, Any]:
    """Fetch up to 1000 recent OHLCV bars, oldest first.

    ``period`` uses the generic vocabulary (``1m``/``5m``/``15m``/``30m``/
    ``1h``/``4h``/``1d``/``1w``). ``1M`` (month) has no eToro interval and is
    rejected rather than silently served as minutes.
    """
    cfg = config or load_config()
    token = str(period or "").strip()
    if token == "1M":
        return {
            "status": "error",
            "profile": cfg.profile,
            "symbol": map_symbol(symbol),
            "error": "eToro has no monthly candle interval; use 1w or 1d.",
        }
    interval = _PERIOD_TO_INTERVAL.get(token.lower())
    if interval is None:
        return {
            "status": "error",
            "profile": cfg.profile,
            "symbol": map_symbol(symbol),
            "error": f"unsupported period '{period}' (supported: {', '.join(sorted(_PERIOD_TO_INTERVAL))}).",
        }
    count = max(1, min(int(limit), _MAX_CANDLES))
    try:
        instrument_id = _resolve_instrument_id(cfg, symbol)
        payload = _request(
            cfg,
            "GET",
            f"/api/v1/market-data/instruments/{instrument_id}/history/candles/desc/{interval}/{count}",
        )
    except (EtoroAPIError, EtoroConfigError) as exc:
        return {"status": "error", "profile": cfg.profile, "symbol": map_symbol(symbol), "error": str(exc)}
    bars = _parse_candles(payload)
    bars.reverse()  # desc from the API → oldest-first for callers
    return {
        "status": "ok",
        "profile": cfg.profile,
        "symbol": map_symbol(symbol),
        "instrument_id": instrument_id,
        "period": period,
        "bars": bars,
    }


def _parse_candles(payload: Any) -> list[dict[str, Any]]:
    """Flatten eToro's doubly-nested candle payload into bar dicts."""
    groups = payload.get("candles") if isinstance(payload, Mapping) else None
    rows = (groups or [{}])[0].get("candles") if groups else None
    bars = []
    for row in rows or []:
        bars.append(
            {
                "timestamp": row.get("fromDate"),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "volume": row.get("volume"),
            }
        )
    return bars


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

_LIVE_ORDER_ERROR = (
    "eToro order placement is not supported for real-account profiles in this "
    "connector layer; use the demo profiles."
)

_READONLY_ORDER_ERROR = "eToro profile is read-only: order placement is disabled."


def place_order(
    config: EtoroConfig | None = None,
    *,
    symbol: str,
    side: str,
    quantity: float | None = None,
    notional: float | None = None,
    order_type: str = "market",
    limit_price: float | None = None,
    time_in_force: str = "day",
) -> dict[str, Any]:
    """Place a demo order: ``buy`` opens an unleveraged long, ``sell`` exits.

    Buys go through eToro's v2 order endpoint (``action=open``, leverage 1,
    ``settlementType="real"``; CFD-only instruments are rejected by eToro with
    a clear error rather than silently opened as CFDs). ``quantity`` maps to
    ``units``, ``notional`` to USD ``amount`` — exactly one is required.
    Sells close existing positions for the symbol oldest-first (eToro opens
    are the only supported order action): with ``quantity`` the given units
    are deducted across positions, without it every position is closed fully.
    Limit buys use eToro's ``limitIOC`` type (≤10% deviation); limit sells are
    not supported.
    """
    cfg = config or load_config()
    clean_side = str(side or "").strip().lower()
    if clean_side not in ("buy", "sell"):
        return _order_refused(cfg, f"unsupported side '{side}' (use 'buy' or 'sell').", symbol=symbol, side=side)
    if quantity is not None and notional is not None:
        return _order_refused(cfg, "provide exactly one of quantity or notional, not both.", symbol=symbol, side=side)
    if clean_side == "buy" and quantity is None and notional is None:
        return _order_refused(cfg, "a buy requires quantity (units) or notional (USD amount).", symbol=symbol, side=side)
    clean_type = str(order_type or "market").strip().lower()
    if clean_type not in ("market", "limit"):
        return _order_refused(cfg, f"unsupported order_type '{order_type}' (use market or limit).", symbol=symbol, side=side)
    if clean_type == "limit" and limit_price is None:
        return _order_refused(cfg, "limit orders require limit_price.", symbol=symbol, side=side)

    # ---- HARD GUARD: demo-only trading in this layer (must run first) ----
    if cfg.environment != "paper":
        return _order_refused(cfg, _LIVE_ORDER_ERROR, symbol=symbol, side=side)
    if cfg.readonly:
        return _order_refused(cfg, _READONLY_ORDER_ERROR, symbol=symbol, side=side)

    if clean_side == "sell":
        if clean_type == "limit":
            return _order_refused(cfg, "limit sells are not supported; sells are market closes.", symbol=symbol, side=side)
        return _close_symbol_positions(cfg, symbol=symbol, units=quantity, notional=notional)

    try:
        instrument_id = _resolve_instrument_id(cfg, symbol)
        request_id = str(uuid.uuid4())
        body: dict[str, Any] = {
            "action": "open",
            "transaction": "buy",
            "instrumentId": instrument_id,
            "settlementType": "real",
            "orderType": "mkt" if clean_type == "market" else "limitIOC",
            "leverage": 1,
            "orderCurrency": "usd",
        }
        if clean_type == "limit":
            body["limitRate"] = float(limit_price)
        if quantity is not None:
            body["units"] = float(quantity)
        else:
            body["amount"] = float(notional)
        payload = _request(cfg, "POST", _p_create_order(cfg), json_body=body, request_id=request_id)
    except (EtoroAPIError, EtoroConfigError) as exc:
        return _order_refused(cfg, str(exc), symbol=symbol, side=side)
    return {
        "status": "ok",
        "profile": cfg.profile,
        "environment": cfg.environment,
        "symbol": map_symbol(symbol),
        "side": clean_side,
        "order_id": str((payload or {}).get("orderId") or ""),
        "reference_id": (payload or {}).get("referenceId") or request_id,
        "token": (payload or {}).get("token"),
        "note": "eToro orders execute asynchronously; poll lookup_order with order_id or reference_id.",
    }


def _close_symbol_positions(
    cfg: EtoroConfig,
    *,
    symbol: str,
    units: float | None,
    notional: float | None,
) -> dict[str, Any]:
    """Close positions for ``symbol`` oldest-first via market-close orders."""
    if notional is not None:
        return _order_refused(cfg, "sells take quantity (units), not notional.", symbol=symbol, side="sell")
    try:
        instrument_id = _resolve_instrument_id(cfg, symbol)
        portfolio = _request(cfg, "GET", _p_portfolio(cfg))
    except (EtoroAPIError, EtoroConfigError) as exc:
        return _order_refused(cfg, str(exc), symbol=symbol, side="sell")
    client = portfolio.get("clientPortfolio") if isinstance(portfolio, Mapping) else {}
    positions = [
        row
        for row in (client or {}).get("positions") or []
        if row.get("instrumentID") == instrument_id and row.get("mirrorID") in (0, None)
    ]
    positions.sort(key=lambda row: str(row.get("openDateTime") or ""))
    if not positions:
        return _order_refused(cfg, f"no open non-copy positions for '{symbol}'.", symbol=symbol, side="sell")

    remaining = float(units) if units is not None else None
    closes: list[dict[str, Any]] = []
    # Demo and real specs disagree on the casing of the instrument-id field.
    id_field = "InstrumentID" if cfg.is_demo else "InstrumentId"
    try:
        for row in positions:
            if remaining is not None and remaining <= 0:
                break
            position_id = int(row["positionID"])
            position_units = float(row.get("units") or 0.0)
            body: dict[str, Any] = {id_field: instrument_id}
            if remaining is not None and remaining < position_units:
                body["UnitsToDeduct"] = remaining
                deducted = remaining
            else:
                body["UnitsToDeduct"] = None  # null → close the full position
                deducted = position_units
            payload = _request(cfg, "POST", _p_close_position(cfg, position_id), json_body=body)
            order = (payload or {}).get("orderForClose") or {}
            closes.append(
                {
                    "position_id": position_id,
                    "units": deducted,
                    "order_id": str(order.get("orderID") or ""),
                    "token": (payload or {}).get("token"),
                }
            )
            if remaining is not None:
                remaining -= deducted
    except (EtoroAPIError, EtoroConfigError) as exc:
        return {
            "status": "error",
            "error": str(exc),
            "profile": cfg.profile,
            "symbol": map_symbol(symbol),
            "side": "sell",
            "closes": closes,
        }
    result: dict[str, Any] = {
        "status": "ok",
        "profile": cfg.profile,
        "environment": cfg.environment,
        "symbol": map_symbol(symbol),
        "side": "sell",
        "order_id": closes[0]["order_id"] if closes else "",
        "closes": closes,
    }
    if remaining is not None and remaining > 0:
        result["unfilled_units"] = remaining
    return result


def cancel_order(
    config: EtoroConfig | None = None,
    order_id: str = "",
    *,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Cancel a pending open order (waiting for market open or MIT trigger)."""
    cfg = config or load_config()
    # ---- HARD GUARD: demo-only trading in this layer (must run first) ----
    if cfg.environment != "paper":
        return _order_refused(cfg, _LIVE_ORDER_ERROR, order_id=order_id, symbol=symbol)
    if cfg.readonly:
        return _order_refused(cfg, _READONLY_ORDER_ERROR, order_id=order_id, symbol=symbol)
    clean_id = str(order_id or "").strip()
    if not clean_id:
        return _order_refused(cfg, "cancel_order requires order_id.", symbol=symbol)
    try:
        payload = _request(cfg, "DELETE", _p_cancel_order(cfg, clean_id))
    except (EtoroAPIError, EtoroConfigError) as exc:
        return _order_refused(cfg, str(exc), order_id=clean_id, symbol=symbol)
    return {
        "status": "ok",
        "profile": cfg.profile,
        "environment": cfg.environment,
        "order_id": clean_id,
        "token": (payload or {}).get("token"),
    }


def lookup_order(
    config: EtoroConfig | None = None,
    *,
    order_id: str | None = None,
    reference_id: str | None = None,
) -> dict[str, Any]:
    """Look up an order's status by order id or the submission's request id."""
    cfg = config or load_config()
    if bool(order_id) == bool(reference_id):
        return {"status": "error", "profile": cfg.profile, "error": "pass exactly one of order_id or reference_id."}
    params = {"orderId": order_id} if order_id else {"referenceId": reference_id}
    try:
        payload = _request(cfg, "GET", _p_lookup_order(cfg), params=params)
    except (EtoroAPIError, EtoroConfigError) as exc:
        return {"status": "error", "profile": cfg.profile, "error": str(exc)}
    status = (payload or {}).get("status") or {}
    return {
        "status": "ok",
        "profile": cfg.profile,
        "order_id": str((payload or {}).get("orderId") or ""),
        "order_status": status.get("name"),
        "order_status_id": status.get("id"),
        "error_message": status.get("errorMessage"),
        "raw": payload,
    }


def _order_refused(config: EtoroConfig, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "error",
        "error": message,
        "profile": config.profile,
        "environment": config.environment,
    }
    payload.update({key: value for key, value in extra.items() if value is not None})
    return payload


# ---------------------------------------------------------------------------
# Row normalization
# ---------------------------------------------------------------------------


def _position_to_dict(row: Mapping[str, Any], symbols: Mapping[int, str]) -> dict[str, Any]:
    instrument_id = row.get("instrumentID")
    return {
        "position_id": row.get("positionID"),
        "symbol": symbols.get(instrument_id) or None,
        "instrument_id": instrument_id,
        "side": "long" if row.get("isBuy") else "short",
        "units": row.get("units"),
        "open_rate": row.get("openRate"),
        "amount_usd": row.get("amount"),
        "leverage": row.get("leverage"),
        "stop_loss_rate": row.get("stopLossRate"),
        "take_profit_rate": row.get("takeProfitRate"),
        "opened_at": row.get("openDateTime"),
        "mirror_id": row.get("mirrorID"),
    }


def _order_to_dict(row: Mapping[str, Any], symbols: Mapping[int, str]) -> dict[str, Any]:
    instrument_id = row.get("instrumentID")
    return {
        "order_id": str(row.get("orderID") or ""),
        "symbol": symbols.get(instrument_id) or None,
        "instrument_id": instrument_id,
        "side": "buy" if row.get("isBuy") else "sell",
        "rate": row.get("rate"),
        "amount_usd": row.get("amount"),
        "units": row.get("units"),
        "leverage": row.get("leverage"),
        "created_at": row.get("openDateTime"),
    }
