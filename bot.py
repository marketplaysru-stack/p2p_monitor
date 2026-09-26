#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram-бот для сравнения курсов P2P USDT/RUB.
Библиотека: python-telegram-bot 20.x
Токены: BOT_TOKEN, P2P_ARMY_KEY (переменные окружения)

Курсы:
- Bybit, MEXC — живые через p2p.army.
- OKX — заглушка (p2p.army не отдаёт USDT/RUB для OKX на нашем тарифе).
"""

import os
import time
import json
import logging
import urllib.request
import urllib.error
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest


# ================== РЕФЕРАЛЬНЫЕ ССЫЛКИ ==================
AFFILIATES = {
    "bybit": {
        "name": "Bybit",
        "ref_link": "https://www.bybit.com/invite?ref=W3RDA7J&medium=referral&utm_campaign=evergreen&share_to=link",
    },
    "mexc": {
        "name": "MEXC",
        "ref_link": "https://s.mexc.com/referral/YBh4C3W1lA",
    },
    "okx": {
        "name": "OKX",
        "ref_link": "https://okx.com/join/90472026",
    },
}

# Заглушки (последний резерв + фиксированные для OKX)
FALLBACK_RATES = {
    "bybit": 89.50,
    "mexc": 89.70,
    "okx": 89.80,
}

# ================== КЛЮЧ p2p.army ==================
P2P_ARMY_KEY = os.getenv("P2P_ARMY_KEY", "").strip()

# ================== КЭШ ==================
_rates_cache = {
    "data": None,
    "timestamp": None,
    "source": None,
    "live_keys": set(),
}
CACHE_TTL_SECONDS = 300  # 5 минут

# Какие биржи тянем через p2p.army (OKX исключён — у них его нет на free-тарифе)
P2P_ARMY_MARKETS = ["bybit", "mexc"]


# ================== ЛОГИРОВАНИЕ ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

logger = logging.getLogger("P2P-Bot")


# ================== p2p.army ==================
def _post_json(url: str, payload: dict, headers: dict):
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")[:300]
        except Exception:
            err_body = ""
        logger.warning(f"p2p.army HTTP {e.code}: {err_body}")
    except urllib.error.URLError as e:
        logger.warning(f"p2p.army URL: {e.reason}")
    except Exception as e:
        logger.warning(f"p2p.army: {type(e).__name__}: {e}")
    return None


def _extract_price(block: dict):
    avg = block.get("avg_price_BUY")
    if avg is not None:
        try:
            return float(avg)
        except (TypeError, ValueError):
            pass
    buy_prices = block.get("prices_BUY", [])
    if buy_prices:
        try:
            return float(buy_prices[0])
        except (TypeError, ValueError):
            pass
    return None


def fetch_p2p_army_rates():
    """Возвращает {"bybit": float, "mexc": float} — только те, что p2p.army отдаёт."""
    if not P2P_ARMY_KEY:
        logger.info("p2p.army: ключ не задан, пропускаю")
        return None

    url = "https://p2p.army/v1/api/get_p2p_prices"
    headers = {
        "X-APIKEY": P2P_ARMY_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    rates = {}
    for market in P2P_ARMY_MARKETS:
        payload = {"market": market, "fiat": "RUB", "asset": "USDT", "limit": 1}
        data = _post_json(url, payload, headers)
        if not data or data.get("status") != 1:
            err = data.get("errText", "no data") if data else "no response"
            logger.warning(f"p2p.army ({market}): {err}")
            continue
        prices = data.get("prices", [])
        if not prices:
            logger.warning(f"p2p.army ({market}): пустой prices")
            continue
        price = _extract_price(prices[0])
        if price:
            rates[market] = price
            logger.info(f"p2p.army ({market}): {price:.2f} RUB")

    return rates if rates else None


# ================== Bybit (резерв) ==================
def fetch_bybit_p2p_rate():
    url = "https://api2.bybit.com/fiat/otc/item/online"
    payload = {
        "userId": "",
        "tokenId": "USDT",
        "currencyId": "RUB",
        "payment": [],
        "side": "1",
        "size": "1",
        "page": "1",
        "amount": "",
        "authMaker": False,
        "canTrade": False,
    }
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; P2PBot/1.0)",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                items = data.get("result", {}).get("items", [])
                if items:
                    return float(items[0]["price"])
    except Exception as e:
        logger.debug(f"Bybit P2P: {type(e).__name__}: {e}")
    return None


# ================== Общий сборщик ==================
def get_rates(force_refresh=False):
    now = datetime.now()
    if not force_refresh and _rates_cache["data"] and _rates_cache["timestamp"]:
        age = (now - _rates_cache["timestamp"]).total_seconds()
        if age < CACHE_TTL_SECONDS:
            return _rates_cache["data"], _rates_cache["source"]

    rates = dict(FALLBACK_RATES)
    live_keys = set()
    source = "fallback"

    army = fetch_p2p_army_rates()
    if army:
        for k, v in army.items():
            rates[k] = v
            live_keys.add(k)
        source = "p2p.army"
    else:
        bybit_rate = fetch_bybit_p2p_rate()
        if bybit_rate:
            rates["bybit"] = bybit_rate
            live_keys.add("bybit")
            source = "bybit"

    _rates_cache["data"] = rates
    _rates_cache["timestamp"] = now
    _rates_cache["source"] = source
    _rates_cache["live_keys"] = live_keys
    return rates, source


# ================== ВСПОМОГАТЕЛЬНОЕ ==================
def log_action(user_id: int, username: str, action: str) -> None:
    logger.info(f"user_id={user_id} | username=@{username or 'без_ника'} | action={action}")


def build_rates_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=f"Зарегистрироваться на {AFFILIATES['bybit']['name']}",
            url=AFFILIATES["bybit"]["ref_link"],
        )],
        [InlineKeyboardButton(
            text=f"Зарегистрироваться на {AFFILIATES['mexc']['name']}",
            url=AFFILIATES["mexc"]["ref_link"],
        )],
        [InlineKeyboardButton(
            text=f"Зарегистрироваться на {AFFILIATES['okx']['name']}",
            url=AFFILIATES["okx"]["ref_link"],
        )],
        [InlineKeyboardButton(text="🔄 Обновить курсы", callback_data="refresh")],
    ]
    return InlineKeyboardMarkup(buttons)


def format_rates_message(rates: dict, source: str) -> str:
    live = _rates_cache.get("live_keys", set())

    def line(key: str, medal: str) -> str:
        name = AFFILIATES[key]["name"]
        price = rates[key]
        suffix = "" if key in live else " <i>(уточните на бирже)</i>"
        return f"{medal} <b>{name}</b>: ~{price:.2f} RUB{suffix}"

    src_tag = {
        "p2p.army": "источник: p2p.army",
        "bybit": "источник: bybit (только Bybit)",
        "fallback": "источник: заглушка (сервер не видит API)",
    }.get(source, f"источник: {source}")

    return (
        "💱 <b>Лучшие курсы P2P USDT за рубли</b>\n"
        f"<i>{src_tag}</i>\n\n"
        f"{line('bybit', '🥇')}\n"
        f"{line('mexc', '🥈')}\n"
        f"{line('okx', '🥉')}\n\n"
        "Выберите площадку и зарегистрируйтесь по ссылке ниже 👇"
    )


# ================== ХЕНДЛЕРЫ ==================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    log_action(user.id, user.username, "/start")
    text = (
        f"Привет, {user.first_name}!\n\n"
        "Я помогаю найти лучший курс P2P для покупки USDT за рубли.\n\n"
        "Доступные команды:\n"
        "/buy — посмотреть курсы"
    )
    await update.message.reply_text(text)


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    log_action(user.id, user.username, "/buy")
    rates, source = get_rates()
    await update.message.reply_text(
        format_rates_message(rates, source),
        parse_mode="HTML",
        reply_markup=build_rates_keyboard(),
        disable_web_page_preview=True,
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    await query.answer()

    if query.data == "refresh":
        log_action(user.id, user.username, "кнопка: Обновить курсы")
        rates, source = get_rates(force_refresh=True)
        try:
            await query.edit_message_text(
                format_rates_message(rates, source),
                parse_mode="HTML",
                reply_markup=build_rates_keyboard(),
                disable_web_page_preview=True,
            )
            await query.answer("Курсы обновлены ✅")
        except Exception as e:
            logger.warning(f"edit_message_text: {e}")
            await query.answer("Курсы актуальны ✅", show_alert=False)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Ошибка: {context.error}", exc_info=context.error)


# ================== ЗАПУСК ==================
def build_app(token: str) -> Application:
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=60.0,
    )
    app = (
        Application.builder()
        .token(token)
        .request(request)
        .get_updates_request(request)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("❌ Не задана переменная окружения BOT_TOKEN")

    logger.info("🚀 Бот запускается...")
    logger.info(f"p2p.army ключ: {'✅ есть' if P2P_ARMY_KEY else '❌ нет'}")

    for attempt in range(1, 6):
        try:
            logger.info(f"🔄 Попытка запуска #{attempt}...")
            app = build_app(token)
            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            break
        except Exception as e:
            logger.error(f"❌ Попытка #{attempt} упала: {type(e).__name__}: {e}")
            if attempt < 5:
                wait = 15 * attempt
                logger.info(f"⏳ Ждём {wait} сек и пробуем снова...")
                time.sleep(wait)
            else:
                logger.error("🚫 Все попытки исчерпаны.")
                raise


if __name__ == "__main__":
    main()