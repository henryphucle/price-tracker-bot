from __future__ import annotations

import logging
from typing import Optional

import httpx

from config import TrackedItem

logger = logging.getLogger(__name__)

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"


class FetchError(Exception):
    pass


class PriceFetcher:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def fetch(self, item: TrackedItem) -> dict:
        try:
            if item.type == "coingecko":
                return await self._fetch_coingecko(item.id)
            elif item.type == "geckoterminal":
                return await self._fetch_geckoterminal(item.network, item.address, item.label)
            else:
                raise FetchError(f"Unknown type: {item.type}")
        except FetchError:
            raise
        except Exception as e:
            raise FetchError(f"Unexpected error fetching {item.display_label}: {e}") from e

    async def resolve_address(self, query: str) -> dict:
        """Search GeckoTerminal for a pool/token by address. Returns {network, address, name}."""
        try:
            url = f"{GECKOTERMINAL_BASE}/search/pools"
            resp = await self._client.get(url, params={"query": query})
            if resp.status_code != 200:
                raise FetchError(f"GeckoTerminal search returned {resp.status_code}")
            pools = resp.json().get("data", [])
            if not pools:
                raise FetchError(f"No pools found for address '{query}'")
            pool = pools[0]
            attrs = pool["attributes"]
            # Pool ID format is "{network}_{address}" — most reliable way to get network
            pool_id = pool.get("id", "")
            network = pool_id.split("_")[0] if "_" in pool_id else attrs.get("network", {}).get("identifier", "")
            if not network:
                raise FetchError("Could not determine network from search result")
            return {
                "network": network,
                "address": attrs["address"],
                "name": attrs["name"],
            }
        except FetchError:
            raise
        except Exception as e:
            raise FetchError(f"GeckoTerminal search failed: {e}") from e

    async def _fetch_coingecko(self, coin_id: str) -> dict:
        url = f"{COINGECKO_BASE}/coins/{coin_id}"
        params = {
            "localization": "false",
            "tickers": "false",
            "community_data": "false",
            "developer_data": "false",
        }
        resp = await self._client.get(url, params=params)
        if resp.status_code != 200:
            raise FetchError(f"CoinGecko returned {resp.status_code} for '{coin_id}'")

        data = resp.json()
        md = data.get("market_data", {})

        return {
            "name": data.get("name", coin_id),
            "symbol": data.get("symbol", "").upper(),
            "price_usd": _safe_float(md.get("current_price", {}).get("usd")),
            "change_24h_pct": _safe_float(md.get("price_change_percentage_24h")),
            "market_cap_usd": _safe_float(md.get("market_cap", {}).get("usd")),
            "volume_24h_usd": _safe_float(md.get("total_volume", {}).get("usd")),
            "source": "coingecko",
        }

    async def _fetch_geckoterminal(self, network: str, address: str, label: Optional[str]) -> dict:
        url = f"{GECKOTERMINAL_BASE}/networks/{network}/pools/{address}"
        resp = await self._client.get(url)
        if resp.status_code == 404:
            # Try as token address — fetch top pool
            url = f"{GECKOTERMINAL_BASE}/networks/{network}/tokens/{address}/pools"
            resp = await self._client.get(url)
            if resp.status_code != 200:
                raise FetchError(f"GeckoTerminal returned {resp.status_code} for {network}:{address}")
            pools = resp.json().get("data", [])
            if not pools:
                raise FetchError(f"No pools found for token {network}:{address}")
            pool_address = pools[0]["attributes"]["address"]
            url = f"{GECKOTERMINAL_BASE}/networks/{network}/pools/{pool_address}"
            resp = await self._client.get(url)

        if resp.status_code != 200:
            raise FetchError(f"GeckoTerminal returned {resp.status_code} for {network}:{address}")

        attrs = resp.json()["data"]["attributes"]
        name = label or attrs.get("name", f"{network}:{address}")

        change_raw = attrs.get("price_change_percentage", {}).get("h24")
        change_24h = _safe_float(change_raw)

        return {
            "name": name,
            "symbol": None,
            "price_usd": _safe_float(attrs.get("base_token_price_usd")),
            "change_24h_pct": change_24h,
            "market_cap_usd": None,
            "volume_24h_usd": _safe_float(attrs.get("volume_usd", {}).get("h24")),
            "source": "geckoterminal",
        }


def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
