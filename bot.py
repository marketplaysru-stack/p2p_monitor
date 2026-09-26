#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram-бот для сравнения курсов P2P USDT/RUB.
Библиотека: python-telegram-bot 20.x
Токены: BOT_TOKEN, P2P_ARMY_KEY (переменные окружения)

Источник курсов:
1. p2p.army — основной (реальные курсы Bybit, MEXC, OKX).
2. Bybit — публичный эндпоинт (резерв).
3. Жёсткие заглушки (последний резерв).
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

# Жёсткие заглушки (последний резерв)
FALLBACK_RATES = {
    "bybit": 89.50,
    "mexc": 89.70,
    "okx": 89.80,
}

# ================== КЛЮЧ p2p.army ==================
P2P_ARMY_KEY = os.getenv("P2P_ARMY_KEY", "").strip()

# ================== КЭШ КУРСОВ ==================
_rates_cache = {
    "data": None,
    "timestamp": None,
    "source": None,
}
CACHE_TTL_SECONDS = 300  # 5 минут


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
def _post_json(url: str, payload: dict, headers: dict) -> dict | None:
    """Универсальный POST-запрос с JSON-телом и JSON-ответом."""
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
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


def _fetch_rate_for_market(market: str) -> float | None:
    """
    Получает курс покупки USDT за RUB на конкретной бирже через p2p.army.
    Использует эндпоинт /get_p2p_prices.
    Возвращает avg_price_BUY или None.
    """
    url = "https://p2p.army/v1/api/get_p2p_prices"
    headers = {
        "X-APIKEY": P2P_ARMY_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "market": market,
        "fiat": "RUB",
        "asset": "USDT",
        "limit": 1,
    }

    data = _post_json(url, payload, headers)
    if not data or data.get("status") != 1:
        logger.warning(f"p2p.army ({market}): ответ без статуса 1: {str(data)[:200]}")
        return None

    prices = data.get("prices", [])
    if not prices:
        logger.warning(f"p2p.army ({market}): пустой массив prices")
        return None

    # Берём первую запись (обычно одна, т.к. limit=1)
    block = prices[0]
    avg = block.get("avg_price_BUY")
    if avg is not None:
        try:
            return float(avg)
        except (TypeError, ValueError):
            pass

    # Если avg нет — берём первую цену из prices_BUY
    buy_prices = block.get("prices_BUY", [])
    if buy_prices:
        try:
            return float(buy_prices[0])
        except (TypeError, ValueError):
            pass

    logger.warning(f"p2p.army ({market}): не удалось извлечь цену из {block}")
    return None


def fetch_p2p_army_rates() -> dict | None:
    """
    Получает курсы для Bybit, MEXC, OKX через p2p.army.
    Возвращает {"bybit": float, "mexc": float, "okx": float} или None.
    """
    if not P2P_ARMY_KEY:
        logger.info("p2p.army: ключ не задан, пропускаю")
        return None

    rates = {}
    for market in ("bybit", "mexc", "okx"):
        rate = _fetch_rate_for_market(market)
        if rate:
            rates[market] = rate
            logger.info(f"p2p.army ({market}): {rate:.2f} RUB")
        else:
            logger.warning(f"p2p.army ({market}): курс не получен")

    if rates:
        return rates
    return None


# ================== Bybit (резерв) ==================
def fetch_bybit_p2p_rate() -> float | None:
    """Публичный эндпоинт Bybit. Таймаут 5 сек."""
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
                    price = float(items[0]["price"])
                    logger.info(f"Bybit P2P: {price:.2f} RUB")
                    return price
    except Exception as e:
        logger.debug(f"Bybit P2P: {type(e).__name__}: {e}")
    return None


# ================== Общий сборщик ==================
def get_rates(force_refresh=False):
    """Возвращает (rates, source_name)."""
    now = datetime.now()
    if not force_refresh and _rates_cache["data"] and _rates_cache["timestamp"]:
        age = (now - _rates_cache["timestamp"]).total_seconds()
        if age < CACHE_TTL_SECONDS:
            return _rates_cache["data"], _rates_cache["source"]

    rates = dict(FALLBACK_RATES)
    source = "fallback"

    # 1. p2p.army — приоритет
    army = fetch_p2p_army_rates()
    if army:
        for k in ("bybit", "mexc", "okx"):
            if k in army:
                rates[k] = army[k]
        source = "p2p.army"
    else:
        # 2. Bybit напрямую
        bybit_rate = fetch_bybit_p2p_rate()
        if bybit_rate:
            rates["bybit"] = bybit_rate
            source = "bybit"

    _rates_cache["data"] = rates
    _rates_cache["timestamp"] = now
    _rates_cache["source"] = source
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
    src_tag = {
        "p2p.army": "источник: p2p.army",
        "bybit": "источник: bybit (только Bybit)",
        "fallback": "источник: заглушка (сервер не видит API)",
    }.get(source, f"источник: {source}")

    return (
        "💱 <b>Лучшие курсы P2P USDT за рубли</b>\n"
        f"<i>{src_tag}</i>\n\n"
        f"🥇 <b>{AFFILIATES['bybit']['name']}</b>:  ~{rates['bybit']:.2f} RUB\n"
        f"🥈 <b>{AFFILIATES['mexc']['name']}</b>:  ~{rates['mexc']:.2f} RUB\n"
        f"🥉 <b>{AFFILIATES['okx']['name']}</b>:    ~{rates['okx']:.2f} RUB\n\n"
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