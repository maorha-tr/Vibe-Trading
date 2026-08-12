"""Sign in with eToro — OAuth 2.0 authorization-code + PKCE client.

eToro's identity provider (discovered via
``https://www.etoro.com/.well-known/openid-configuration``):

* authorize: ``https://www.etoro.com/sso`` (``response_type=code``, PKCE S256)
* token:     ``https://www.etoro.com/api/sso/v1/token``
* revoke:    ``https://www.etoro.com/api/sso/v1/token/revoke``
* userinfo:  ``https://www.etoro.com/api/sso/v1/userinfo``

A Public API request authenticates with EITHER the ``x-api-key``/``x-user-key``
pair OR ``Authorization: Bearer <access token>`` — never both (422). This
module owns the bearer side: the OAuth client settings (an application
registered via ``POST /api/v1/sso/applications``), the on-disk token store,
refresh, and revocation. ``sdk.py`` and the market-data loader fall back to
:func:`bearer_token` whenever the key pair is absent.

Files (owner-only, under ``~/.vibe-trading/``):

* ``etoro-sso.json``        — client_id / client_secret / redirect_uri / scopes
* ``etoro-sso-tokens.json`` — access/refresh tokens + userinfo snapshot
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode

import requests

from src.config.paths import get_runtime_root

AUTHORIZE_URL = "https://www.etoro.com/sso"
TOKEN_URL = "https://www.etoro.com/api/sso/v1/token"
REVOKE_URL = "https://www.etoro.com/api/sso/v1/token/revoke"
USERINFO_URL = "https://www.etoro.com/api/sso/v1/userinfo"

SETTINGS_FILENAME = "etoro-sso.json"
TOKENS_FILENAME = "etoro-sso-tokens.json"

#: Demo-first default consent, mirroring the connector's demo-only trading
#: guard: identity + demo trading; no real-account write scopes in v1.
DEFAULT_SCOPES = (
    "etoro-public:user-info:read",
    "etoro-public:demo:read",
    "etoro-public:demo:write",
    "etoro-public:trade.demo:read",
    "etoro-public:trade.demo:write",
)

_REFRESH_SKEW_S = 120.0
_HTTP_TIMEOUT_S = 15.0


class EtoroSsoError(RuntimeError):
    """Raised when the SSO client is misconfigured or an OAuth call fails."""


# ---------------------------------------------------------------------------
# Client settings (registered OAuth application)
# ---------------------------------------------------------------------------


def settings_path() -> Path:
    return get_runtime_root() / SETTINGS_FILENAME


def tokens_path() -> Path:
    return get_runtime_root() / TOKENS_FILENAME


def _env(name: str, field: str) -> str:
    """Read an env override via the typed config, falling back to os.environ."""
    try:
        from src.config.accessor import get_env_config

        value = str(getattr(get_env_config().data, field, "") or "").strip()
    except Exception:  # pragma: no cover — env schema unavailable in odd setups
        value = ""
    return value or os.environ.get(name, "").strip()


def load_settings() -> dict[str, Any]:
    """Resolve OAuth client settings: env overrides ← ``etoro-sso.json``."""
    payload: dict[str, Any] = {}
    path = settings_path()
    if path.exists():
        try:
            payload = dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise EtoroSsoError(f"invalid eToro SSO settings at {path}: {exc}") from exc

    client_id = _env("ETORO_SSO_CLIENT_ID", "etoro_sso_client_id") or str(payload.get("client_id") or "").strip()
    client_secret = _env("ETORO_SSO_CLIENT_SECRET", "etoro_sso_client_secret") or str(
        payload.get("client_secret") or ""
    ).strip()
    redirect_uri = _env("ETORO_SSO_REDIRECT_URI", "etoro_sso_redirect_uri") or str(
        payload.get("redirect_uri") or ""
    ).strip()
    raw_scopes = _env("ETORO_SSO_SCOPES", "etoro_sso_scopes")
    scopes = (
        [token for token in raw_scopes.replace(",", " ").split() if token]
        if raw_scopes
        else [str(s) for s in payload.get("scopes") or [] if str(s).strip()]
    )
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri or "http://localhost:8000/auth/etoro/callback",
        "scopes": scopes or list(DEFAULT_SCOPES),
    }


def save_settings(settings: Mapping[str, Any]) -> Path:
    """Persist OAuth client settings with owner-only permissions."""
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(settings), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def client_configured() -> bool:
    """True when a registered OAuth client id is available."""
    try:
        return bool(load_settings()["client_id"])
    except EtoroSsoError:
        return False


# ---------------------------------------------------------------------------
# PKCE + authorize URL
# ---------------------------------------------------------------------------


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_state() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(
    *,
    state: str,
    code_challenge: str,
    settings: Mapping[str, Any] | None = None,
) -> str:
    """Build the browser redirect URL that starts the consent flow."""
    cfg = dict(settings or load_settings())
    if not cfg.get("client_id"):
        raise EtoroSsoError(
            "eToro SSO is not configured: register an OAuth application "
            "(scripts/etoro_sso_setup.py) or set ETORO_SSO_CLIENT_ID."
        )
    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "scope": " ".join(cfg["scopes"]),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Token store
# ---------------------------------------------------------------------------


def load_tokens() -> dict[str, Any] | None:
    path = tokens_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) and payload.get("access_token") else None


def _save_tokens(payload: Mapping[str, Any]) -> Path:
    path = tokens_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def clear_tokens() -> None:
    try:
        tokens_path().unlink(missing_ok=True)
    except OSError:
        pass


def _token_record(token_response: Mapping[str, Any], *, userinfo: Mapping[str, Any] | None = None) -> dict[str, Any]:
    now = time.time()
    expires_in = float(token_response.get("expires_in") or 0.0)
    record = {
        "access_token": str(token_response.get("access_token") or ""),
        "refresh_token": str(token_response.get("refresh_token") or ""),
        "token_type": str(token_response.get("token_type") or "Bearer"),
        "scope": str(token_response.get("scope") or ""),
        "obtained_at": now,
        "expires_at": now + expires_in if expires_in else None,
    }
    if userinfo is not None:
        record["userinfo"] = dict(userinfo)
    return record


# ---------------------------------------------------------------------------
# OAuth calls
# ---------------------------------------------------------------------------


def _token_request(form: dict[str, str], settings: Mapping[str, Any]) -> dict[str, Any]:
    """POST the token endpoint with client_secret_post auth when a secret exists."""
    body = dict(form)
    body["client_id"] = str(settings["client_id"])
    if settings.get("client_secret"):
        body["client_secret"] = str(settings["client_secret"])
    try:
        response = requests.post(
            TOKEN_URL,
            data=body,
            headers={"Accept": "application/json"},
            timeout=_HTTP_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise EtoroSsoError(f"eToro token request failed: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400 or not payload.get("access_token"):
        error = payload.get("error") or f"HTTP {response.status_code}"
        description = payload.get("error_description") or ""
        raise EtoroSsoError(f"eToro token endpoint rejected the request: {error} {description}".strip())
    return payload


def exchange_code(code: str, code_verifier: str, *, settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Exchange an authorization code for tokens and persist the session."""
    cfg = dict(settings or load_settings())
    payload = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": str(cfg["redirect_uri"]),
            "code_verifier": code_verifier,
        },
        cfg,
    )
    userinfo: dict[str, Any] | None = None
    try:
        userinfo = fetch_userinfo(str(payload["access_token"]))
    except EtoroSsoError:
        userinfo = None  # identity display is best-effort; the session still works
    record = _token_record(payload, userinfo=userinfo)
    _save_tokens(record)
    return record


def refresh_tokens(*, settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Refresh the stored session; clears it when the grant is invalid."""
    stored = load_tokens()
    if not stored or not stored.get("refresh_token"):
        raise EtoroSsoError("no eToro SSO session to refresh — sign in with eToro first.")
    cfg = dict(settings or load_settings())
    try:
        payload = _token_request(
            {"grant_type": "refresh_token", "refresh_token": str(stored["refresh_token"])},
            cfg,
        )
    except EtoroSsoError as exc:
        if "invalid_grant" in str(exc):
            clear_tokens()
        raise
    record = _token_record(payload)
    if not record.get("refresh_token"):
        record["refresh_token"] = str(stored["refresh_token"])  # rotation is optional
    if stored.get("userinfo") is not None:
        record["userinfo"] = stored["userinfo"]
    _save_tokens(record)
    return record


def bearer_token() -> str | None:
    """Return a valid access token, refreshing when close to expiry.

    Returns ``None`` when no SSO session exists or it cannot be refreshed —
    callers fall back to their key-pair error path.
    """
    stored = load_tokens()
    if not stored:
        return None
    expires_at = stored.get("expires_at")
    if expires_at is None or float(expires_at) - _REFRESH_SKEW_S > time.time():
        return str(stored["access_token"])
    try:
        return str(refresh_tokens()["access_token"])
    except EtoroSsoError:
        return None


def fetch_userinfo(access_token: str) -> dict[str, Any]:
    """Fetch the OIDC userinfo claims for a bearer token."""
    try:
        response = requests.get(
            USERINFO_URL,
            headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
            timeout=_HTTP_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise EtoroSsoError(f"eToro userinfo request failed: {exc}") from exc
    if response.status_code >= 400:
        raise EtoroSsoError(f"eToro userinfo returned HTTP {response.status_code}.")
    try:
        payload = response.json()
    except ValueError as exc:
        raise EtoroSsoError("eToro userinfo returned invalid JSON.") from exc
    return payload if isinstance(payload, dict) else {}


def logout() -> None:
    """Best-effort revoke of the stored session, then forget it locally."""
    stored = load_tokens()
    if stored:
        try:
            cfg = load_settings()
            for hint, key in (("refresh_token", "refresh_token"), ("access_token", "access_token")):
                token = str(stored.get(key) or "")
                if not token:
                    continue
                body = {"token": token, "token_type_hint": hint, "client_id": str(cfg["client_id"])}
                if cfg.get("client_secret"):
                    body["client_secret"] = str(cfg["client_secret"])
                requests.post(REVOKE_URL, data=body, timeout=_HTTP_TIMEOUT_S)
        except (EtoroSsoError, requests.RequestException):
            pass
    clear_tokens()


def status() -> dict[str, Any]:
    """Local (no-network) view of the SSO session for the UI."""
    stored = load_tokens()
    if not stored:
        return {"connected": False, "client_configured": client_configured()}
    userinfo = stored.get("userinfo") or {}
    return {
        "connected": True,
        "client_configured": True,
        "username": userinfo.get("username") or userinfo.get("given_name") or None,
        "subject": userinfo.get("sub"),
        "scopes": [s for s in str(stored.get("scope") or "").split() if s],
        "expires_at": stored.get("expires_at"),
    }


# ---------------------------------------------------------------------------
# One-time application registration (self-service via the Public API)
# ---------------------------------------------------------------------------


def register_application(
    *,
    api_key: str,
    user_key: str,
    name: str = "Vibe Trading",
    icon_url: str = "https://raw.githubusercontent.com/HKUDS/Vibe-Trading/main/assets/logo.png",
    redirect_uris: list[str] | None = None,
    scope_names: list[str] | None = None,
    base_url: str = "https://public-api.etoro.com",
) -> dict[str, Any]:
    """Register the OAuth application and persist its client settings.

    Uses the caller's key pair once; afterwards the app runs on SSO alone.
    The returned ``clientSecret`` is revealed only in this response, so it is
    written straight into ``etoro-sso.json``.
    """
    import uuid

    uris = redirect_uris or ["http://localhost:8000/auth/etoro/callback"]
    wanted = scope_names or list(DEFAULT_SCOPES)
    headers = {
        "Accept": "application/json",
        "x-api-key": api_key,
        "x-user-key": user_key,
    }

    def _call(method: str, path: str, json_body: Any = None) -> Any:
        try:
            response = requests.request(
                method,
                f"{base_url.rstrip('/')}{path}",
                headers={**headers, "x-request-id": str(uuid.uuid4())},
                json=json_body,
                timeout=_HTTP_TIMEOUT_S,
            )
        except requests.RequestException as exc:
            raise EtoroSsoError(f"eToro API request failed: {exc}") from exc
        if response.status_code >= 400:
            raise EtoroSsoError(f"eToro API returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise EtoroSsoError("eToro API returned invalid JSON.") from exc

    catalog = _call("GET", "/api/v1/sso/scopes")
    by_name = {
        str(row.get("scopeName")): int(row["scopeId"])
        for row in (catalog.get("scopes") if isinstance(catalog, Mapping) else None) or []
        if row.get("scopeId") is not None
    }
    unknown = [s for s in wanted if s not in by_name]
    if unknown:
        raise EtoroSsoError(f"unknown eToro OAuth scopes: {', '.join(unknown)} (catalog has {len(by_name)}).")

    created = _call(
        "POST",
        "/api/v1/sso/applications",
        json_body={
            "applicationName": name,
            "applicationIconUrl": icon_url,
            "scopes": [{"scopeId": by_name[s], "isMandatory": True} for s in wanted],
            "redirectUris": uris,
        },
    )
    application = (created or {}).get("application") or {}
    client_id = str(application.get("clientId") or "")
    client_secret = str((created or {}).get("clientSecret") or "")
    if not client_id:
        raise EtoroSsoError(f"eToro did not return a clientId: {json.dumps(created)[:300]}")
    save_settings(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": uris[0],
            "scopes": wanted,
            "application_id": application.get("applicationId"),
            "application_state": application.get("applicationState"),
        }
    )
    return {"client_id": client_id, "application": application, "settings_path": str(settings_path())}
