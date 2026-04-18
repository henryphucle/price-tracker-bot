from __future__ import annotations

import re
from typing import Optional


_SPECIAL = re.compile(r'([_*\[\]()~`>#+\-=|{}.!])')


def _escape(text: str) -> str:
    return _SPECIAL.sub(r'\\\1', str(text))


def _fmt_price(value: float) -> str:
    if value >= 1.0:
        return f"${value:,.4f}"
    # For very small prices, show up to 8 significant digits
    return f"${value:.8g}"


def _fmt_large(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    return f"${value:,.0f}"


def format_price_message(data: dict) -> str:
    name = data.get("name", "Unknown")
    symbol = data.get("symbol")
    price = data.get("price_usd")
    change = data.get("change_24h_pct")
    market_cap = data.get("market_cap_usd")
    volume = data.get("volume_24h_usd")

    header = _escape(name)
    if symbol:
        header = f"*{header}* \\({_escape(symbol)}\\)"
    else:
        header = f"*{header}*"

    lines = [header]

    if price is not None:
        lines.append(f"Price: `{_escape(_fmt_price(price))}`")
    else:
        lines.append("Price: _unavailable_")

    if change is not None:
        sign = "+" if change >= 0 else ""
        change_str = f"{sign}{change:.2f}%"
        lines.append(f"24h: `{_escape(change_str)}`")

    if market_cap is not None:
        lines.append(f"Market Cap: `{_escape(_fmt_large(market_cap))}`")

    if volume is not None:
        lines.append(f"Volume: `{_escape(_fmt_large(volume))}`")

    return "\n".join(lines)
