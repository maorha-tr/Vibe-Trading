"""Sign in with eToro — browser OAuth routes.

Mounted by ``agent/api_server.py`` via ``register_etoro_auth_routes(app)``.

Flow: ``GET /auth/etoro/login`` stores a single-use PKCE state and 302s the
browser to eToro's consent screen; eToro redirects back to
``GET /auth/etoro/callback`` which exchanges the code and persists the session
(``src.trading.connectors.etoro.sso``). ``/status`` and ``/logout`` serve the
frontend account card.

Auth model: ``login`` and ``callback`` are top-level browser navigations, which
cannot carry an ``Authorization`` header — they are therefore open to loopback
clients even when ``API_AUTH_KEY`` is set (non-local callers still need the
key). Neither exposes secrets: login emits a redirect built from the public
client_id, and callback only consumes an eToro-issued code bound to our own
single-use ``state``. ``status``/``logout`` use the standard dependencies.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials

AuthDep = Callable[..., Awaitable[Any] | Any]

#: state -> (code_verifier, return_to, created_at). Single-process, single-use.
_PENDING_LOGINS: dict[str, tuple[str, str, float]] = {}
_PENDING_TTL_S = 600.0
_PENDING_MAX = 50


def _prune_pending(now: float | None = None) -> None:
    ts = now if now is not None else time.time()
    expired = [state for state, (_, _, created) in _PENDING_LOGINS.items() if ts - created > _PENDING_TTL_S]
    for state in expired:
        _PENDING_LOGINS.pop(state, None)
    while len(_PENDING_LOGINS) > _PENDING_MAX:
        _PENDING_LOGINS.pop(next(iter(_PENDING_LOGINS)), None)


def _safe_return_to(raw: str | None) -> str:
    """Only same-origin absolute paths — no scheme/host, no protocol-relative."""
    value = str(raw or "").strip()
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/settings"


def register_etoro_auth_routes(
    app: FastAPI,
    require_local_or_auth: AuthDep | None = None,
) -> None:
    """Mount the eToro SSO routes onto ``app``.

    Args:
        app: The host FastAPI app.
        require_local_or_auth: Dependency guarding ``status``/``logout``. When
            omitted it is resolved from ``src.api.security`` directly.
    """
    if require_local_or_auth is None:
        from src.api.security import require_local_or_auth as _default_dep

        require_local_or_auth = _default_dep

    from src.api.security import _is_local_client, _security, _validate_api_auth

    async def _require_browser_nav_auth(
        request: Request,
        cred: Optional[HTTPAuthorizationCredentials] = Security(_security),
    ) -> None:
        """Loopback navigations pass; anything remote needs the API key."""
        if _is_local_client(request):
            return
        _validate_api_auth(request=request, cred=cred)

    @app.get("/auth/etoro/login", dependencies=[Depends(_require_browser_nav_auth)])
    async def etoro_login(return_to: str | None = None) -> RedirectResponse:
        """Start the Sign-in-with-eToro consent flow (browser navigation)."""
        from src.trading.connectors.etoro import sso

        try:
            settings = sso.load_settings()
            verifier, challenge = sso.generate_pkce_pair()
            state = sso.generate_state()
            url = sso.build_authorize_url(state=state, code_challenge=challenge, settings=settings)
        except sso.EtoroSsoError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _prune_pending()
        _PENDING_LOGINS[state] = (verifier, _safe_return_to(return_to), time.time())
        return RedirectResponse(url, status_code=302)

    @app.get("/auth/etoro/callback", dependencies=[Depends(_require_browser_nav_auth)])
    async def etoro_callback(
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
    ) -> RedirectResponse:
        """Finish the consent flow: validate state, swap the code for tokens."""
        from src.trading.connectors.etoro import sso

        _prune_pending()
        pending = _PENDING_LOGINS.pop(str(state or ""), None)
        return_to = pending[1] if pending else "/settings"

        def _fail(reason: str) -> RedirectResponse:
            sep = "&" if "?" in return_to else "?"
            return RedirectResponse(
                f"{return_to}{sep}{urlencode({'etoro': 'error', 'reason': reason[:200]})}",
                status_code=302,
            )

        if error:
            return _fail(f"{error}: {error_description or ''}".strip(" :"))
        if pending is None:
            return _fail("login expired or state mismatch — try again")
        if not code:
            return _fail("eToro returned no authorization code")
        try:
            sso.exchange_code(code, pending[0])
        except sso.EtoroSsoError as exc:
            return _fail(str(exc))
        sep = "&" if "?" in return_to else "?"
        return RedirectResponse(f"{return_to}{sep}etoro=connected", status_code=302)

    @app.get("/auth/etoro/status", dependencies=[Depends(require_local_or_auth)])
    async def etoro_status() -> dict[str, Any]:
        """SSO session + credential mode snapshot for the account card."""
        from src.trading.connectors.etoro import sdk, sso

        report = dict(sso.status())
        try:
            report["auth_mode"] = sdk.auth_mode()
        except sdk.EtoroConfigError:
            report["auth_mode"] = None
        report["login_url"] = "/auth/etoro/login?return_to=" + quote("/settings", safe="")
        return report

    @app.post("/auth/etoro/logout", dependencies=[Depends(require_local_or_auth)])
    async def etoro_logout() -> dict[str, str]:
        """Revoke (best-effort) and forget the local SSO session."""
        from src.trading.connectors.etoro import sso

        sso.logout()
        return {"status": "ok"}
