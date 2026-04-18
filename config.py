from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TrackedItem:
    type: str  # "coingecko" or "geckoterminal"
    interval_seconds: int
    # coingecko
    id: Optional[str] = None
    # geckoterminal
    network: Optional[str] = None
    address: Optional[str] = None
    label: Optional[str] = None

    @property
    def display_label(self) -> str:
        if self.label:
            return self.label
        if self.id:
            return self.id
        return f"{self.network}:{self.address}"


@dataclass
class BotConfig:
    telegram_bot_token: str
    chat_id: str
    default_interval_minutes: int
    tracked: list[TrackedItem] = field(default_factory=list)


def _item_to_dict(item: TrackedItem) -> dict:
    d: dict = {"type": item.type, "interval_minutes": item.interval_seconds // 60}
    if item.id:
        d["id"] = item.id
    if item.network:
        d["network"] = item.network
    if item.address:
        d["address"] = item.address
    if item.label:
        d["label"] = item.label
    return d


def save_config(config: BotConfig, path: str = "config.json") -> None:
    data = {
        "telegram_bot_token": config.telegram_bot_token,
        "chat_id": config.chat_id,
        "default_interval_minutes": config.default_interval_minutes,
        "tracked": [_item_to_dict(item) for item in config.tracked],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_config(path: str = "config.json") -> BotConfig:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    else:
        # No config file — bootstrap from environment variables (e.g. Railway)
        raw = {
            "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            "chat_id": os.environ.get("CHAT_ID", ""),
            "default_interval_minutes": int(os.environ.get("DEFAULT_INTERVAL_MINUTES", "60")),
            "tracked": json.loads(os.environ.get("TRACKED", "[]")),
        }

    token = raw.get("telegram_bot_token", "")
    if not token or token == "YOUR_BOT_TOKEN":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise ValueError("Set TELEGRAM_BOT_TOKEN env var or telegram_bot_token in config.json")

    chat_id = raw.get("chat_id", "")
    if not chat_id or chat_id == "YOUR_CHAT_ID":
        chat_id = os.environ.get("CHAT_ID", "")
    if not chat_id:
        raise ValueError("Set CHAT_ID env var or chat_id in config.json")

    default_interval = int(raw.get("default_interval_minutes", 60))

    tracked = []
    for i, item in enumerate(raw.get("tracked", [])):
        item_type = item.get("type", "")
        if item_type not in ("coingecko", "geckoterminal"):
            raise ValueError(f"tracked[{i}].type must be 'coingecko' or 'geckoterminal', got '{item_type}'")

        if item_type == "coingecko" and not item.get("id"):
            raise ValueError(f"tracked[{i}] (coingecko) requires 'id'")
        if item_type == "geckoterminal" and (not item.get("network") or not item.get("address")):
            raise ValueError(f"tracked[{i}] (geckoterminal) requires 'network' and 'address'")

        interval_minutes = int(item.get("interval_minutes", default_interval))
        tracked.append(TrackedItem(
            type=item_type,
            interval_seconds=interval_minutes * 60,
            id=item.get("id"),
            network=item.get("network"),
            address=item.get("address"),
            label=item.get("label"),
        ))

    return BotConfig(
        telegram_bot_token=token,
        chat_id=str(chat_id),
        default_interval_minutes=default_interval,
        tracked=tracked,
    )
