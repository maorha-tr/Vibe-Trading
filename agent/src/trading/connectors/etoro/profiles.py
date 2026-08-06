"""Built-in eToro connector profiles.

The paper profiles target eToro's demo (virtual portfolio) trading routes; the
live profile is read-only. eToro user keys are generated per environment
(Demo or Real) in Settings → Trading → API Key Management, so a demo key
structurally cannot reach real trading routes and vice versa. A real-money
trade profile is intentionally not shipped in this layer yet.
"""

from __future__ import annotations

from src.trading.types import READ_CAPABILITIES, TradingProfile

ETORO_PROFILES: tuple[TradingProfile, ...] = (
    TradingProfile(
        id="etoro-demo-sdk",
        connector="etoro",
        label="eToro Demo · Public API",
        environment="paper",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES,
        readonly=True,
        config={"profile": "paper"},
        notes=(
            "Reads an eToro demo (virtual) portfolio via the public API "
            "(public-api.etoro.com). Requires a user key generated for the "
            "Demo environment; demo keys cannot reach real trading routes."
        ),
    ),
    TradingProfile(
        id="etoro-demo-trade",
        connector="etoro",
        label="eToro Demo · Public API Trade",
        environment="paper",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES + ("orders.place",),
        readonly=False,
        config={"profile": "paper"},
        notes=(
            "Reads and places orders on an eToro demo (virtual) portfolio via "
            "the public API. Demo routes carry a 'demo' path segment and demo "
            "user keys are environment-bound, so no real funds are ever at risk."
        ),
    ),
    TradingProfile(
        id="etoro-live-sdk-readonly",
        connector="etoro",
        label="eToro Real · Public API Read-Only",
        environment="live",
        transport="broker_sdk",
        capabilities=READ_CAPABILITIES,
        readonly=True,
        config={"profile": "live-readonly"},
        notes=(
            "Reads a real eToro account only (requires a Real-environment user "
            "key). Order placement is not exposed for real accounts in this "
            "connector layer."
        ),
    ),
)
