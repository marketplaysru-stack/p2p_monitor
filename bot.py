#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram-бот для сравнения курсов P2P USDT/RUB.
Библиотека: python-telegram-bot 20.x
Токен: переменная окружения BOT_TOKEN

Bybit: реальный P2P-курс через публичный эндпоинт.
OKX и MEXC: заглушки (публичных API нет).
"""

import os
import time
import logging
from datetime import datetime, timedelta

import requests
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

# Заглушки для бирж без публичного API
FALLBACK_RATES = {
    "bybit": 89.50,  # будет заменён реальным, если запрос успешен
    "mexc": 89.70,
    "okx": 89.80,
}

# ================== КЭШ КУРСОВ ==================
_rates_cache = {
    "data": None,
    "timestamp": None,
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


# ================== ПОЛУЧЕНИЕ КУРСОВ ==================
def fetch_bybit_p2p_rate() -> float | None:
    """Получает лучший P2P-курс USDT/RUB (покупка) на Bybit."""
    url = "https://api2.bybit.com/fiat/otc/item/online"
    payload = {
        "userId": "",
        "tokenId": "USDT",
        "currencyId": "RUB",
        "payment": [],
        "side": "1",       # 1 = покупка USDT за RUB
        "size": "1",
        "page": "1",
        "amount": "",
        "authMaker": False,
        "canTrade": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            data = r.json()
            items = data.get("result", {}).get("items", [])
            if items:
                price = float(items[0]["price"])
                logger.info(f"Bybit P2P: {price:.2f} RUB")
                return price
        logger.warning(f"Bybit P2P: статус {r.status_code}")
    except Exception as e:
        logger.warning(f"Bybit P2P: {e}")
    return None


def get_rates(force_refresh: bool = False) -> dict:
    """Возвращает курсы с учётом кэша."""
    now = datetime.now()
    if not force_refresh and _rates_cache["data"] and _rates_cache["timestamp"]:
        age = (now - _rates_cache["timestamp"]).total_seconds()
        if age < CACHE_TTL_SECONDS:
            return _rates_cache["data"]

    rates = dict(FALLBACK_RATES)
    bybit_rate = fetch_bybit_p2p_rate()
    if bybit_rate:
        rates["bybit"] = bybit_rate

    _rates_cache["data"] = rates
    _rates_cache["timestamp"] = now
    return rates


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


def format_rates_message(rates: dict) -> str:
    return (
        "💱 <b>Лучшие курсы P2P USDT за рубли</b>\n\n"
        f"🥇 <b>{AFFILIATES['bybit']['name']}</b>:  ~{rates['bybit']:.2f} RUB\n"
        f"🥈 <b>{AFFILIATES['mexc']['name']}</b>:  ~{rates['mexc']:.2f} RUB <i>(уточните на бирже)</i>\n"
        f"🥉 <b>{AFFILIATES['okx']['name']}</b>:    ~{rates['okx']:.2f} RUB <i>(уточните на бирже)</i>\n\n"
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
    rates = get_rates()
    await update.message.reply_text(
        format_rates_message(rates),
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
        rates = get_rates(force_refresh=True)
        try:
            await query.edit_message_text(
                format_rates_message(rates),
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