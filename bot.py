from __future__ import annotations

import asyncio
import logging

import httpx
from telegram import Update
from telegram.ext import Application, CallbackContext, CommandHandler

from config import BotConfig, TrackedItem, load_config, save_config
from fetcher import FetchError, PriceFetcher
from formatter import format_price_message, _escape

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# --- Job helpers ---

def _make_job(item: TrackedItem, fetcher: PriceFetcher, chat_id: str):
    async def job(context: CallbackContext) -> None:
        try:
            data = await fetcher.fetch(item)
            msg = format_price_message(data)
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="MarkdownV2")
            logger.info("Sent price update for %s", item.display_label)
        except FetchError as e:
            logger.error("Fetch failed for %s: %s", item.display_label, e)
        except Exception as e:
            logger.error("Unexpected error for %s: %s", item.display_label, e)
    return job


def _schedule_item(app: Application, item: TrackedItem) -> None:
    config: BotConfig = app.bot_data["config"]
    fetcher: PriceFetcher = app.bot_data["fetcher"]
    app.job_queue.run_repeating(
        _make_job(item, fetcher, config.chat_id),
        interval=item.interval_seconds,
        first=10,
        name=item.display_label,
    )


def _cancel_jobs(app: Application, name: str) -> int:
    jobs = app.job_queue.get_jobs_by_name(name)
    for job in jobs:
        job.schedule_removal()
    return len(jobs)


def _find_item(config: BotConfig, key: str) -> TrackedItem | None:
    key_lower = key.lower()
    for item in config.tracked:
        if (item.id and item.id.lower() == key_lower) or \
           (item.label and item.label.lower() == key_lower):
            return item
    return None


def _fmt_price(value: float) -> str:
    return f"${value:,.4f}" if value >= 1.0 else f"${value:.8g}"


def _looks_like_address(s: str) -> bool:
    # Ethereum: 0x + 40 hex chars
    if s.startswith("0x") and len(s) >= 10:
        return True
    # Solana / other base58 addresses: long alphanumeric, no special chars
    if len(s) >= 32 and s.isalnum():
        return True
    return False


# --- Auth guard ---

def _authorized(update: Update, config: BotConfig) -> bool:
    return str(update.effective_chat.id) == config.chat_id


# --- Command handlers ---

async def cmd_list(update: Update, context: CallbackContext) -> None:
    config: BotConfig = context.application.bot_data["config"]
    fetcher: PriceFetcher = context.application.bot_data["fetcher"]
    if not _authorized(update, config):
        return

    if not config.tracked:
        await update.message.reply_text("No tokens tracked yet. Use /add to add one.")
        return

    await update.message.reply_text("Fetching prices...")

    results = await asyncio.gather(
        *[fetcher.fetch(item) for item in config.tracked],
        return_exceptions=True,
    )

    from datetime import timezone as _tz
    now = __import__("datetime").datetime.now(_tz.utc)

    lines = ["*Tracked tokens:*\n"]
    for item, result in zip(config.tracked, results):
        interval_m = item.interval_seconds // 60
        unit = "min" if interval_m < 60 else ("hr" if interval_m < 1440 else "day")
        val = interval_m if interval_m < 60 else (interval_m // 60 if interval_m < 1440 else interval_m // 1440)

        # Next alert time
        jobs = context.job_queue.get_jobs_by_name(item.display_label)
        next_str = "unknown"
        if jobs and jobs[0].next_t:
            delta = jobs[0].next_t - now
            secs = int(delta.total_seconds())
            if secs <= 0:
                next_str = "now"
            elif secs < 60:
                next_str = f"{secs}s"
            elif secs < 3600:
                next_str = f"{secs // 60}m {secs % 60}s"
            else:
                next_str = f"{secs // 3600}h {(secs % 3600) // 60}m"

        if isinstance(result, Exception):
            lines.append(
                f"• *{item.display_label}* — every {val}{unit}\n"
                f"  _Price unavailable_ \\| next in {_escape(next_str)}"
            )
        else:
            price = result.get("price_usd")
            change = result.get("change_24h_pct")
            price_str = _fmt_price(price) if price is not None else "N/A"
            if change is not None:
                sign = "+" if change >= 0 else ""
                price_str += f" \\({sign}{change:.2f}%\\)"
            lines.append(
                f"• *{item.display_label}* — `{price_str}` every {val}{unit}\n"
                f"  next in {_escape(next_str)}"
            )

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


async def cmd_add(update: Update, context: CallbackContext) -> None:
    config: BotConfig = context.application.bot_data["config"]
    fetcher: PriceFetcher = context.application.bot_data["fetcher"]
    if not _authorized(update, config):
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /add <address\\_or\\_coin\\_id> \\[interval\\_minutes\\]\n\n"
            "Examples:\n"
            "`/add 0xabc123...` — DEX token by contract address\n"
            "`/add bitcoin 5` — CoinGecko coin by ID",
            parse_mode="MarkdownV2",
        )
        return

    # Last arg is interval if it's a number
    interval_m = config.default_interval_minutes
    if args[-1].isdigit():
        interval_m = int(args[-1])
        args = args[:-1]

    query = args[0]

    await update.message.reply_text(f"Looking up `{query}`...", parse_mode="Markdown")

    try:
        if _looks_like_address(query):
            resolved = await fetcher.resolve_address(query)
            item = TrackedItem(
                type="geckoterminal",
                interval_seconds=interval_m * 60,
                network=resolved["network"],
                address=resolved["address"],
                label=resolved["name"],
            )
        else:
            # Validate the CoinGecko ID by fetching it
            data = await fetcher.fetch(TrackedItem(type="coingecko", interval_seconds=0, id=query.lower()))
            item = TrackedItem(
                type="coingecko",
                interval_seconds=interval_m * 60,
                id=query.lower(),
                label=data["name"],
            )

        if _find_item(config, item.display_label):
            await update.message.reply_text(f"'{item.display_label}' is already tracked. Use /remove first.")
            return

        config.tracked.append(item)
        save_config(config)
        _schedule_item(context.application, item)

        await update.message.reply_text(
            f"Added *{item.display_label}* — updates every {interval_m} min.",
            parse_mode="Markdown",
        )
        logger.info("Added tracked item: %s", item.display_label)

    except FetchError as e:
        await update.message.reply_text(f"Could not find `{query}`: {e}", parse_mode="Markdown")
    except Exception as e:
        logger.exception("Unexpected error in /add for %s", query)
        await update.message.reply_text(f"Unexpected error: {e}")


async def cmd_remove(update: Update, context: CallbackContext) -> None:
    config: BotConfig = context.application.bot_data["config"]
    if not _authorized(update, config):
        return

    if not context.args:
        await update.message.reply_text("Usage: /remove <id_or_label>")
        return

    key = " ".join(context.args)
    item = _find_item(config, key)
    if not item:
        await update.message.reply_text(f"No tracked token found for '{key}'. Use /list to see all.")
        return

    cancelled = _cancel_jobs(context.application, item.display_label)
    config.tracked.remove(item)
    save_config(config)

    await update.message.reply_text(f"Removed *{item.display_label}*.", parse_mode="Markdown")
    logger.info("Removed %s (cancelled %d jobs)", item.display_label, cancelled)


async def cmd_setinterval(update: Update, context: CallbackContext) -> None:
    config: BotConfig = context.application.bot_data["config"]
    if not _authorized(update, config):
        return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("Usage: /setinterval <id_or_label> <minutes>")
        return

    key = " ".join(args[:-1])
    try:
        minutes = int(args[-1])
        if minutes < 1:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("Interval must be a positive integer (minutes).")
        return

    item = _find_item(config, key)
    if not item:
        await update.message.reply_text(f"No tracked token found for '{key}'. Use /list to see all.")
        return

    old_label = item.display_label
    _cancel_jobs(context.application, old_label)
    item.interval_seconds = minutes * 60
    save_config(config)
    _schedule_item(context.application, item)

    await update.message.reply_text(
        f"Updated *{old_label}* — now every {minutes} min.",
        parse_mode="Markdown",
    )
    logger.info("Updated interval for %s to %dm", old_label, minutes)


async def cmd_price(update: Update, context: CallbackContext) -> None:
    config: BotConfig = context.application.bot_data["config"]
    fetcher: PriceFetcher = context.application.bot_data["fetcher"]
    if not _authorized(update, config):
        return

    if not context.args:
        await update.message.reply_text("Usage: /price <id_or_label>")
        return

    key = " ".join(context.args)
    item = _find_item(config, key)
    if not item:
        await update.message.reply_text(f"No tracked token found for '{key}'. Use /list to see all.")
        return

    await update.message.reply_text(f"Fetching {item.display_label}...")
    try:
        data = await fetcher.fetch(item)
        msg = format_price_message(data)
        await update.message.reply_text(msg, parse_mode="MarkdownV2")
    except FetchError as e:
        await update.message.reply_text(f"Error: {e}")


async def cmd_help(update: Update, context: CallbackContext) -> None:
    config: BotConfig = context.application.bot_data["config"]
    if not _authorized(update, config):
        return

    text = (
        "*Price Tracker Bot*\n\n"
        "/list — show all tracked tokens\n"
        "/price <name> — get price now\n"
        "/add <address\\_or\\_coin\\_id> \\[interval\\_min\\]\n"
        "/remove <name>\n"
        "/setinterval <name> <minutes>\n\n"
        "Examples:\n"
        "`/add 0xabc123...` — any DEX token by contract address\n"
        "`/add bitcoin 5` — CoinGecko coin by ID\n"
        "`/setinterval Bitcoin 30`\n"
        "`/remove Ethereum`"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


# --- Lifecycle ---

async def post_init(application: Application) -> None:
    config: BotConfig = application.bot_data["config"]

    for item in config.tracked:
        _schedule_item(application, item)
        logger.info("Scheduled %s every %ds", item.display_label, item.interval_seconds)

    await application.bot.set_my_commands([
        ("list", "Show tracked tokens"),
        ("price", "Get price now"),
        ("add", "Add a token"),
        ("remove", "Remove a token"),
        ("setinterval", "Change update interval"),
        ("help", "Show help"),
    ])


async def post_shutdown(application: Application) -> None:
    client: httpx.AsyncClient = application.bot_data.get("http_client")
    if client:
        await client.aclose()


def main() -> None:
    config = load_config()
    client = httpx.AsyncClient(timeout=10.0)
    fetcher = PriceFetcher(client)

    app = (
        Application.builder()
        .token(config.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["config"] = config
    app.bot_data["fetcher"] = fetcher
    app.bot_data["http_client"] = client

    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("setinterval", cmd_setinterval))
    app.add_handler(CommandHandler("price", cmd_price))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))

    logger.info("Starting price tracker bot with %d tracked items", len(config.tracked))
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling()


if __name__ == "__main__":
    main()
