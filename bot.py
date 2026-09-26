#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram-бот для сравнения курсов P2P USDT/RUB.
Библиотека: python-telegram-bot 20.x
Токен: переменная окружения BOT_TOKEN
"""

import os
import time
import logging
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
# ЗАМЕНИ "ЗАМЕНИ" на свои реферальные коды/ссылки
AFFILIATES = {
    "bybit": {
        "name": "Bybit",
        "ref_link": "https://www.bybit.com/invite?ref=ЗАМЕНИ",
    },
    "bitget": {
        "name": "Bitget",
        "ref_link": "https://share.bitget.com/u/ЗАМЕНИ",
    },
    "okx": {
        "name": "OKX",
        "ref_link": "https://okx.com/join/ЗАМЕНИ",
    },
}

# Заглушки курсов (потом можно заменить на реальные с API бирж)
RATES = {
    "bybit": 89.50,
    "bitget": 89.65,
    "okx": 89.80,
}


# ================== ЛОГИРОВАНИЕ ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

# Отключаем подробные логи библиотек — в них светится токен
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)

logger = logging.getLogger("P2P-Bot")


# ================== ВСПОМОГАТЕЛЬНОЕ ==================
def log_action(user_id: int, username: str, action: str) -> None:
    """Пишет действие пользователя в bot.log."""
    logger.info(
        f"user_id={user_id} | username=@{username or 'без_ника'} | action={action}"
    )


def build_rates_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура с реферальными ссылками + кнопка обновления."""
    buttons = [
        [InlineKeyboardButton(
            text=f"Зарегистрироваться на {AFFILIATES['bybit']['name']}",
            url=AFFILIATES["bybit"]["ref_link"],
        )],
        [InlineKeyboardButton(
            text=f"Зарегистрироваться на {AFFILIATES['bitget']['name']}",
            url=AFFILIATES["bitget"]["ref_link"],
        )],
        [InlineKeyboardButton(
            text=f"Зарегистрироваться на {AFFILIATES['okx']['name']}",
            url=AFFILIATES["okx"]["ref_link"],
        )],
        [InlineKeyboardButton(text="🔄 Обновить курсы", callback_data="refresh")],
    ]
    return InlineKeyboardMarkup(buttons)


def format_rates_message() -> str:
    """Собирает сообщение с тремя курсами."""
    return (
        "💱 <b>Лучшие курсы P2P USDT за рубли</b>\n\n"
        f"🥇 <b>{AFFILIATES['bybit']['name']}</b>:  ~{RATES['bybit']:.2f} RUB\n"
        f"🥈 <b>{AFFILIATES['bitget']['name']}</b>: ~{RATES['bitget']:.2f} RUB\n"
        f"🥉 <b>{AFFILIATES['okx']['name']}</b>:    ~{RATES['okx']:.2f} RUB\n\n"
        "Выберите площадку и зарегистрируйтесь по ссылке ниже 👇"
    )


# ================== ХЕНДЛЕРЫ ==================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — приветствие."""
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
    """/buy — показывает курсы и кнопки."""
    user = update.effective_user
    log_action(user.id, user.username, "/buy")

    await update.message.reply_text(
        format_rates_message(),
        parse_mode="HTML",
        reply_markup=build_rates_keyboard(),
        disable_web_page_preview=True,
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка нажатий на inline-кнопки."""
    query = update.callback_query
    user = update.effective_user
    await query.answer()

    if query.data == "refresh":
        log_action(user.id, user.username, "кнопка: Обновить курсы")

        try:
            await query.edit_message_text(
                format_rates_message(),
                parse_mode="HTML",
                reply_markup=build_rates_keyboard(),
                disable_web_page_preview=True,
            )
            await query.answer("Курсы обновлены ✅")
        except Exception as e:
            logger.warning(f"edit_message_text: {e}")
            await query.answer("Курсы актуальны ✅", show_alert=False)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик ошибок."""
    logger.error(f"Ошибка: {context.error}", exc_info=context.error)


# ================== ЗАПУСК С RETRY ==================
def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("❌ Не задана переменная окружения BOT_TOKEN")

    logger.info("🚀 Бот запускается...")

    # Большие таймауты для нестабильных сетей (РФ и т.п.)
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

    # Retry на старте: если Telegram не отвечает — пробуем снова
    for attempt in range(1, 6):
        try:
            logger.info(f"🔄 Попытка запуска #{attempt}...")
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
                logger.error("🚫 Все попытки исчерпаны. Проверьте сеть/прокси.")
                raise


if __name__ == "__main__":
    main()