"""Curated read/write classification for eToro public-API operations.

Keys are the connector's own operation names. Order-mutating calls are pinned
WRITE so the live gate never treats them as plain reads; anything unlisted and
not a known read is treated as WRITE (fail-closed) by the gate.
"""

from __future__ import annotations

from src.live.classification import ToolClass

#: eToro public-API operation read/write catalog.
ETORO_TOOL_CLASS: dict[str, ToolClass] = {
    # READ
    "get_me": ToolClass.READ,
    "get_portfolio": ToolClass.READ,
    "get_pnl": ToolClass.READ,
    "get_aggregate_portfolio": ToolClass.READ,
    "get_rates": ToolClass.READ,
    "get_candles": ToolClass.READ,
    "search_instruments": ToolClass.READ,
    "get_instruments": ToolClass.READ,
    "lookup_order": ToolClass.READ,
    "get_trade_history": ToolClass.READ,
    # WRITE
    "create_order": ToolClass.WRITE,
    "close_position": ToolClass.WRITE,
    "cancel_order": ToolClass.WRITE,
    "cancel_close_order": ToolClass.WRITE,
    "update_position": ToolClass.WRITE,
}
