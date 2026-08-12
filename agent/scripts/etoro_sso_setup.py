"""One-time setup for "Sign in with eToro": register the OAuth application.

Registers a client via ``POST /api/v1/sso/applications`` using your eToro API
key pair (the pair is only needed for this one call — afterwards users sign in
via SSO). The returned ``clientSecret`` is revealed exactly once, so it is
written straight to ``~/.vibe-trading/etoro-sso.json`` (chmod 600).

Usage (from ``agent/``):

    python scripts/etoro_sso_setup.py \
        --name "Vibe Trading" \
        --redirect http://localhost:8000/auth/etoro/callback

Keys are taken from ``--api-key``/``--user-key``, else ``ETORO_API_KEY``/
``ETORO_USER_KEY``, else ``~/.vibe-trading/etoro.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.trading.connectors.etoro import sso  # noqa: E402
from src.trading.connectors.etoro.sdk import load_config  # noqa: E402


def _resolve_keys(args: argparse.Namespace) -> tuple[str, str]:
    api_key = args.api_key or os.environ.get("ETORO_API_KEY", "").strip()
    user_key = args.user_key or os.environ.get("ETORO_USER_KEY", "").strip()
    if not (api_key and user_key):
        cfg = load_config()
        api_key = api_key or cfg.api_key
        user_key = user_key or cfg.user_key
    if not (api_key and user_key):
        raise SystemExit(
            "eToro API keys required for the one-time registration call: pass "
            "--api-key/--user-key, set ETORO_API_KEY/ETORO_USER_KEY, or fill "
            "~/.vibe-trading/etoro.json (eToro Settings → Trading → API Key Management)."
        )
    return api_key, user_key


def main() -> None:
    parser = argparse.ArgumentParser(description="Register the Vibe-Trading OAuth app with eToro SSO.")
    parser.add_argument("--name", default="Vibe Trading")
    parser.add_argument("--icon-url", default="https://raw.githubusercontent.com/HKUDS/Vibe-Trading/main/assets/logo.png")
    parser.add_argument(
        "--redirect",
        action="append",
        default=None,
        help="Redirect URI (repeatable). Default: http://localhost:8000/auth/etoro/callback",
    )
    parser.add_argument(
        "--scopes",
        default=None,
        help=f"Comma/space-separated scope names. Default: {', '.join(sso.DEFAULT_SCOPES)}",
    )
    parser.add_argument("--api-key", default=None, help="eToro application API key (x-api-key)")
    parser.add_argument("--user-key", default=None, help="eToro user API key (x-user-key)")
    args = parser.parse_args()

    api_key, user_key = _resolve_keys(args)
    scope_names = (
        [token for token in args.scopes.replace(",", " ").split() if token] if args.scopes else None
    )
    try:
        result = sso.register_application(
            api_key=api_key,
            user_key=user_key,
            name=args.name,
            icon_url=args.icon_url,
            redirect_uris=args.redirect,
            scope_names=scope_names,
        )
    except sso.EtoroSsoError as exc:
        raise SystemExit(f"registration failed: {exc}") from exc

    application = result["application"]
    print(json.dumps({k: application.get(k) for k in ("clientId", "applicationState", "supportedFlows")}, indent=2))
    print(f"\nClient settings saved to {result['settings_path']} (client secret included — keep it private).")
    print("Start the app and open http://localhost:8000/settings to sign in with eToro.")


if __name__ == "__main__":
    main()
