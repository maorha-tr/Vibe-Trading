"""Unit tests for Sign in with eToro — OAuth client, bearer auth, routes (no network)."""

from __future__ import annotations

import base64
import hashlib
import json
import stat
import time

import pytest

from src.trading.connectors.etoro import sdk, sso

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path))
    for var in ("ETORO_SSO_CLIENT_ID", "ETORO_SSO_CLIENT_SECRET", "ETORO_SSO_REDIRECT_URI", "ETORO_SSO_SCOPES"):
        monkeypatch.delenv(var, raising=False)
    sdk._INSTRUMENT_CACHE.clear()
    yield
    sdk._INSTRUMENT_CACHE.clear()


def _write_settings(**overrides):
    payload = {
        "client_id": "client-123",
        "client_secret": "secret-456",
        "redirect_uri": "http://localhost:8000/auth/etoro/callback",
        "scopes": ["etoro-public:user-info:read", "etoro-public:demo:read"],
    }
    payload.update(overrides)
    sso.save_settings(payload)
    return payload


def _write_tokens(**overrides):
    payload = {
        "access_token": "access-abc",
        "refresh_token": "refresh-def",
        "token_type": "Bearer",
        "scope": "etoro-public:demo:read",
        "obtained_at": time.time(),
        "expires_at": time.time() + 3600,
        "userinfo": {"username": "trader1", "sub": "sub-1"},
    }
    payload.update(overrides)
    sso.tokens_path().parent.mkdir(parents=True, exist_ok=True)
    sso.tokens_path().write_text(json.dumps(payload), encoding="utf-8")
    return payload


class _Resp:
    def __init__(self, payload, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


# ---------------------------------------------------------------------------
# PKCE + authorize URL
# ---------------------------------------------------------------------------


def test_pkce_pair_is_s256_consistent() -> None:
    verifier, challenge = sso.generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_authorize_url_carries_oauth_params() -> None:
    _write_settings()
    url = sso.build_authorize_url(state="st4te", code_challenge="ch4llenge")
    assert url.startswith(f"{sso.AUTHORIZE_URL}?")
    for fragment in (
        "response_type=code",
        "client_id=client-123",
        "state=st4te",
        "code_challenge=ch4llenge",
        "code_challenge_method=S256",
        "redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fauth%2Fetoro%2Fcallback",
    ):
        assert fragment in url


def test_authorize_url_requires_client_id() -> None:
    with pytest.raises(sso.EtoroSsoError, match="not configured"):
        sso.build_authorize_url(state="s", code_challenge="c")


def test_settings_env_overrides_file(monkeypatch) -> None:
    _write_settings()
    monkeypatch.setenv("ETORO_SSO_CLIENT_ID", "env-client")
    monkeypatch.setenv("ETORO_SSO_SCOPES", "a:read b:write")
    cfg = sso.load_settings()
    assert cfg["client_id"] == "env-client"
    assert cfg["scopes"] == ["a:read", "b:write"]


# ---------------------------------------------------------------------------
# Code exchange + token store
# ---------------------------------------------------------------------------


def test_exchange_code_posts_pkce_form_and_persists(monkeypatch) -> None:
    _write_settings()
    calls: list[dict] = []

    def fake_post(url, data=None, headers=None, timeout=None):
        calls.append({"url": url, "data": data})
        return _Resp({"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 600, "scope": "s1 s2"})

    monkeypatch.setattr(sso.requests, "post", fake_post)
    monkeypatch.setattr(sso, "fetch_userinfo", lambda token: {"username": "trader1"})

    record = sso.exchange_code("auth-code", "verifier-xyz")
    assert calls[0]["url"] == sso.TOKEN_URL
    form = calls[0]["data"]
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "auth-code"
    assert form["code_verifier"] == "verifier-xyz"
    assert form["client_id"] == "client-123"
    assert form["client_secret"] == "secret-456"
    assert record["access_token"] == "at-1"
    assert record["userinfo"] == {"username": "trader1"}

    mode = stat.S_IMODE(sso.tokens_path().stat().st_mode)
    assert mode == 0o600
    stored = sso.load_tokens()
    assert stored is not None and stored["refresh_token"] == "rt-1"
    assert stored["expires_at"] > time.time()


def test_exchange_code_survives_userinfo_failure(monkeypatch) -> None:
    _write_settings()
    monkeypatch.setattr(
        sso.requests, "post", lambda *a, **k: _Resp({"access_token": "at-1", "expires_in": 600})
    )

    def boom(token):
        raise sso.EtoroSsoError("userinfo down")

    monkeypatch.setattr(sso, "fetch_userinfo", boom)
    record = sso.exchange_code("code", "verifier")
    assert record["access_token"] == "at-1"
    assert "userinfo" not in record


def test_bearer_token_returns_unexpired_access_token() -> None:
    _write_tokens()
    assert sso.bearer_token() == "access-abc"


def test_bearer_token_refreshes_near_expiry(monkeypatch) -> None:
    _write_settings()
    _write_tokens(expires_at=time.time() + 10)

    def fake_post(url, data=None, headers=None, timeout=None):
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "refresh-def"
        return _Resp({"access_token": "at-new", "expires_in": 600})

    monkeypatch.setattr(sso.requests, "post", fake_post)
    assert sso.bearer_token() == "at-new"
    stored = sso.load_tokens()
    assert stored["access_token"] == "at-new"
    assert stored["refresh_token"] == "refresh-def"  # rotation optional — kept
    assert stored["userinfo"]["username"] == "trader1"


def test_bearer_token_clears_session_on_invalid_grant(monkeypatch) -> None:
    _write_settings()
    _write_tokens(expires_at=time.time() - 10)
    monkeypatch.setattr(
        sso.requests, "post", lambda *a, **k: _Resp({"error": "invalid_grant"}, status=400)
    )
    assert sso.bearer_token() is None
    assert sso.load_tokens() is None


def test_logout_revokes_and_clears(monkeypatch) -> None:
    _write_settings()
    _write_tokens()
    revoked: list[dict] = []
    monkeypatch.setattr(
        sso.requests, "post", lambda url, data=None, timeout=None, **k: revoked.append({"url": url, "data": data}) or _Resp({})
    )
    sso.logout()
    assert sso.load_tokens() is None
    assert all(call["url"] == sso.REVOKE_URL for call in revoked)
    hints = {call["data"]["token_type_hint"] for call in revoked}
    assert hints == {"refresh_token", "access_token"}


def test_status_reports_session() -> None:
    assert sso.status() == {"connected": False, "client_configured": False}
    _write_settings()
    _write_tokens()
    report = sso.status()
    assert report["connected"] is True
    assert report["username"] == "trader1"
    assert report["scopes"] == ["etoro-public:demo:read"]


# ---------------------------------------------------------------------------
# SDK bearer mode (mutually exclusive with the key pair)
# ---------------------------------------------------------------------------


def test_sdk_prefers_key_pair() -> None:
    cfg = sdk.EtoroConfig(api_key="app", user_key="user")
    headers = sdk._auth_headers(cfg)
    assert headers == {"x-api-key": "app", "x-user-key": "user"}
    assert "Authorization" not in headers


def test_sdk_falls_back_to_bearer(monkeypatch) -> None:
    monkeypatch.setattr(sso, "bearer_token", lambda: "tok-123")
    headers = sdk._auth_headers(sdk.EtoroConfig())
    assert headers == {"Authorization": "Bearer tok-123"}
    assert "x-api-key" not in headers


def test_sdk_errors_without_keys_or_session(monkeypatch) -> None:
    monkeypatch.setattr(sso, "bearer_token", lambda: None)
    with pytest.raises(sdk.EtoroConfigError, match="sign in with eToro"):
        sdk._auth_headers(sdk.EtoroConfig())


def test_sdk_auth_mode_reporting(monkeypatch) -> None:
    assert sdk.auth_mode(sdk.EtoroConfig(api_key="a", user_key="u")) == "keys"
    assert sdk.auth_mode(sdk.EtoroConfig()) is None
    _write_tokens()
    assert sdk.auth_mode(sdk.EtoroConfig()) == "sso"


def test_sdk_request_sends_bearer(monkeypatch) -> None:
    _write_tokens()
    seen: dict = {}

    def fake_request(method, url, **kwargs):
        seen.update(kwargs["headers"])

        class R:
            status_code = 200
            content = b"{}"

            def json(self):
                return {}

        return R()

    monkeypatch.setattr(sdk.requests, "request", fake_request)
    sdk._request(sdk.EtoroConfig(), "GET", "/api/v1/me")
    assert seen["Authorization"] == "Bearer access-abc"
    assert "x-api-key" not in seen and "x-user-key" not in seen
    assert seen["x-request-id"]


def test_check_status_unconfigured_mentions_sso(monkeypatch) -> None:
    monkeypatch.setattr(sso, "load_tokens", lambda: None)
    report = sdk.check_status(sdk.EtoroConfig())
    assert report["status"] == "error"
    assert report["auth_mode"] is None
    assert "sign in with eToro" in report["error"]


# ---------------------------------------------------------------------------
# Market-data loader fallback
# ---------------------------------------------------------------------------


def test_loader_uses_bearer_when_no_keys(monkeypatch) -> None:
    from backtest.loaders import etoro_loader

    monkeypatch.setattr(etoro_loader, "_credentials", lambda: None)
    monkeypatch.setattr(sso, "bearer_token", lambda: "tok-999")
    assert etoro_loader._auth_headers() == {"Authorization": "Bearer tok-999"}

    monkeypatch.setattr(sso, "bearer_token", lambda: None)
    with pytest.raises(RuntimeError, match="sign in with eToro"):
        etoro_loader._auth_headers()


def test_loader_prefers_key_pair(monkeypatch) -> None:
    from backtest.loaders import etoro_loader

    monkeypatch.setattr(etoro_loader, "_credentials", lambda: ("app", "user"))
    assert etoro_loader._auth_headers() == {"x-api-key": "app", "x-user-key": "user"}


# ---------------------------------------------------------------------------
# HTTP routes (login redirect, callback, status, logout)
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    import api_server
    from src.api import etoro_auth_routes

    monkeypatch.delenv("API_AUTH_KEY", raising=False)
    monkeypatch.setattr(api_server, "_API_KEY", "", raising=False)
    etoro_auth_routes._PENDING_LOGINS.clear()
    yield TestClient(api_server.app, client=("127.0.0.1", 50000))
    etoro_auth_routes._PENDING_LOGINS.clear()


def test_login_redirects_to_etoro_consent(client) -> None:
    _write_settings()
    from src.api import etoro_auth_routes

    response = client.get("/auth/etoro/login?return_to=/agent", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(f"{sso.AUTHORIZE_URL}?")
    assert "code_challenge_method=S256" in location
    assert len(etoro_auth_routes._PENDING_LOGINS) == 1
    state, (verifier, return_to, _) = next(iter(etoro_auth_routes._PENDING_LOGINS.items()))
    assert f"state={state}" in location
    assert verifier
    assert return_to == "/agent"


def test_login_without_client_is_409(client) -> None:
    response = client.get("/auth/etoro/login", follow_redirects=False)
    assert response.status_code == 409
    assert "not configured" in response.json()["detail"]


def test_callback_rejects_unknown_state(client) -> None:
    _write_settings()
    response = client.get("/auth/etoro/callback?code=c&state=bogus", follow_redirects=False)
    assert response.status_code == 302
    assert "etoro=error" in response.headers["location"]
    assert "state+mismatch" in response.headers["location"] or "state%20mismatch" in response.headers["location"]


def test_callback_exchanges_code_and_redirects(client, monkeypatch) -> None:
    _write_settings()
    from src.api import etoro_auth_routes

    etoro_auth_routes._PENDING_LOGINS["st-1"] = ("ver-1", "/agent", time.time())
    seen: dict = {}

    def fake_exchange(code, verifier):
        seen["code"], seen["verifier"] = code, verifier
        return {"access_token": "at"}

    monkeypatch.setattr(sso, "exchange_code", fake_exchange)
    response = client.get("/auth/etoro/callback?code=c0de&state=st-1", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/agent?etoro=connected"
    assert seen == {"code": "c0de", "verifier": "ver-1"}
    assert "st-1" not in etoro_auth_routes._PENDING_LOGINS  # single-use


def test_callback_reports_provider_error(client) -> None:
    _write_settings()
    response = client.get(
        "/auth/etoro/callback?error=access_denied&error_description=user+cancelled",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "etoro=error" in response.headers["location"]
    assert "access_denied" in response.headers["location"]


def test_status_and_logout_roundtrip(client, monkeypatch) -> None:
    _write_settings()
    _write_tokens()
    response = client.get("/auth/etoro/status")
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["auth_mode"] == "sso"
    assert body["username"] == "trader1"

    monkeypatch.setattr(sso.requests, "post", lambda *a, **k: _Resp({}))
    response = client.post("/auth/etoro/logout")
    assert response.status_code == 200
    assert sso.load_tokens() is None
    assert client.get("/auth/etoro/status").json()["connected"] is False
