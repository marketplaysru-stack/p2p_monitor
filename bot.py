#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram-бот для сравнения курсов P2P USDT/RUB.
Библиотека: python-telegram-bot 20.x
Токены: BOT_TOKEN, P2P_ARMY_KEY (переменные окружения)

Функции:
- /start — приветствие
- /buy — показать курсы Bybit, MEXC, OKX
- /subscribe — подписка на ежедневную рассылку лучшего курса в 10:00 МСК
- /unsubscribe — отписка
- /set <цена> — уведомить, когда Bybit опустится ниже порога
"""

import os
import json
import time
import asyncio
import logging
import threading
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

import schedule
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

FALLBACK_RATES = {
    "bybit": 89.50,
    "mexc": 89.70,
    "okx": 89.80,
}

# ================== КОНФИГ ==================
P2P_ARMY_KEY = os.getenv("P2P_ARMY_KEY", "").strip()
SUBSCRIBERS_FILE = "subscribers.json"
SUBSCRIBERS_LOCK = threading.Lock()
P2P_ARMY_MARKETS = ["bybit", "mexc"]  # OKX у них нет на free-тарифе

# МСК = UTC+3
MSK_TZ = timezone(timedelta(hours=3))

# ================== КЭШ КУРСОВ ==================
_rates_cache = {
    "data": None,
    "timestamp": None,
    "source": None,
    "live_keys": set(),
}
CACHE_TTL_SECONDS = 300


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


# ================== ХРАНИЛИЩЕ ПОДПИСЧИКОВ ==================
def load_subscribers():
    if not os.path.exists(SUBSCRIBERS_FILE):
        return []
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"subscribers load: {e}")
        return []


def save_subscribers(subs):
    try:
        with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
            json.dump(subs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"subscribers save: {e}")


def get_subscriber(subs, user_id):
    for s in subs:
        if s.get("user_id") == user_id:
            return s
    return None


def add_subscriber(user_id, username):
    with SUBSCRIBERS_LOCK:
        subs = load_subscribers()
        existing = get_subscriber(subs, user_id)
        if existing:
            existing["username"] = username or existing.get("username", "")
        else:
            subs.append({
                "user_id": user_id,
                "username": username or "",
                "alert_price": None,
            })
        save_subscribers(subs)


def remove_subscriber(user_id):
    with SUBSCRIBERS_LOCK:
        subs = load_subscribers()
        subs = [s for s in subs if s.get("user_id") != user_id]
        save_subscribers(subs)


def set_alert_price(user_id, username, price):
    """price = None — снять алерт. Иначе — установить."""
    with SUBSCRIBERS_LOCK:
        subs = load_subscribers()
        existing = get_subscriber(subs, user_id)
        if not existing:
            existing = {
                "user_id": user_id,
                "username": username or "",
                "alert_price": None,
            }
            subs.append(existing)
        existing["alert_price"] = price
        save_subscribers(subs)


# ================== p2p.army ==================
def _post_json(url, payload, headers):
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


def _extract_price(block):
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
    if not P2P_ARMY_KEY:
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
            continue
        price = _extract_price(prices[0])
        if price:
            rates[market] = price
            logger.info(f"p2p.army ({market}): {price:.2f} RUB")
    return rates if rates else None


def fetch_bybit_p2p_rate():
    url = "https://api2.bybit.com/fiat/otc/item/online"
    payload = {
        "userId": "", "tokenId": "USDT", "currencyId": "RUB",
        "payment": [], "side": "1", "size": "1", "page": "1",
        "amount": "", "authMaker": False, "canTrade": False,
    }
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
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


# ================== ФОРМАТИРОВАНИЕ ==================
def log_action(user_id, username, action):
    logger.info(f"user_id={user_id} | username=@{username or 'без_ника'} | action={action}")


def build_rates_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            text=f"Зарегистрироваться на {AFFILIATES['bybit']['name']}",
            url=AFFILIATES["bybit"]["ref_link"])],
        [InlineKeyboardButton(
            text=f"Зарегистрироваться на {AFFILIATES['mexc']['name']}",
            url=AFFILIATES["mexc"]["ref_link"])],
        [InlineKeyboardButton(
            text=f"Зарегистрироваться на {AFFILIATES['okx']['name']}",
            url=AFFILIATES["okx"]["ref_link"])],
        [InlineKeyboardButton(text="🔄 Обновить курсы", callback_data="refresh")],
    ])


def format_rates_message(rates, source):
    live = _rates_cache.get("live_keys", set())

    def line(key, medal):
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
        "Команды:\n"
        "/buy — посмотреть курсы\n"
        "/subscribe — подписка на рассылку в 10:00 МСК\n"
        "/set 85 — уведомить, когда Bybit упадёт ниже 85 RUB\n"
        "/unsubscribe — отписаться\n"
        "/help — все команды"
    )
    await update.message.reply_text(text)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    log_action(user.id, user.username, "/help")
    await update.message.reply_text(
        "📋 <b>Команды бота</b>\n\n"
        "/buy — текущие курсы P2P (Bybit, MEXC, OKX)\n\n"
        "/subscribe — подписаться на ежедневную рассылку\n"
        "лучшего курса в 10:00 МСК\n\n"
        "/unsubscribe — отписаться от рассылки\n\n"
        "/set 85 — уведомить, когда курс Bybit\n"
        "опустится ниже 85 RUB\n"
        "/set 0 — отключить уведомление по порогу\n\n"
        "/help — эта справка",
        parse_mode="HTML",
    )


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


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    log_action(user.id, user.username, "/subscribe")
    add_subscriber(user.id, user.username)
    await update.message.reply_text(
        "✅ Ты подписан на ежедневную рассылку лучшего курса.\n\n"
        "⏰ Приходит каждый день в 10:00 МСК.\n\n"
        "Отписаться: /unsubscribe"
    )


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    log_action(user.id, user.username, "/unsubscribe")
    remove_subscriber(user.id)
    await update.message.reply_text(
        "❌ Ты отписан от рассылки.\n\n"
        "Подписаться снова: /subscribe"
    )


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    args = context.args

    # Без аргументов — показать текущий порог
    if not args:
        subs = load_subscribers()
        s = get_subscriber(subs, user.id)
        current = s.get("alert_price") if s else None
        if current:
            text = (
                f"🔔 Текущий порог: <b>{current:.2f} RUB</b>\n\n"
                f"Уведомлю, когда Bybit опустится ниже.\n"
                f"Отключить: /set 0"
            )
        else:
            text = (
                "ℹ️ Порог не задан.\n\n"
                "Установить: <code>/set 85</code>\n"
                "— уведомлю, когда Bybit упадёт ниже 85 RUB."
            )
        await update.message.reply_text(text, parse_mode="HTML")
        return

    raw = args[0].replace(",", ".").strip()
    try:
        price = float(raw)
    except ValueError:
        await update.message.reply_text(
            "❌ Не могу разобрать число.\n\n"
            "Пример: <code>/set 85</code>",
            parse_mode="HTML",
        )
        return

    if price <= 0:
        log_action(user.id, user.username, "/set 0 (сброс)")
        set_alert_price(user.id, user.username, None)
        await update.message.reply_text("🔕 Уведомление по порогу отключено.")
        return

    log_action(user.id, user.username, f"/set {price}")
    set_alert_price(user.id, user.username, price)
    await update.message.reply_text(
        f"🔔 Готово!\n\n"
        f"Уведомлю, когда курс Bybit опустится ниже "
        f"<b>{price:.2f} RUB</b>.\n\n"
        f"Отключить: /set 0",
        parse_mode="HTML",
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


# ================== ПЛАНИРОВЩИК ==================
_last_broadcast = {"date": None}


def _run_daily_broadcast(app):
    """Проверяет, 10:00 ли сейчас по МСК. Если да — рассылает один раз в сутки."""
    now_msk = datetime.now(MSK_TZ)
    today = now_msk.date()

    if now_msk.hour != 10:
        return
    if _last_broadcast["date"] == today:
        return

    _last_broadcast["date"] = today
    logger.info(f"📢 Запуск рассылки ({now_msk.strftime('%Y-%m-%d %H:%M МСК')})")

    loop = app.bot_data.get("loop")
    if not loop:
        logger.warning("loop не найден — рассылка пропущена")
        return
    asyncio.run_coroutine_threadsafe(_do_broadcast(app), loop)


async def _do_broadcast(app):
    with SUBSCRIBERS_LOCK:
        subs = load_subscribers()
    if not subs:
        logger.info("📢 Подписчиков нет — рассылка пропущена")
        return

    rates, source = get_rates(force_refresh=True)
    text = format_rates_message(rates, source)
    kb = build_rates_keyboard()

    sent = 0
    for s in subs:
        uid = s.get("user_id")
        if not uid:
            continue
        try:
            await app.bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            sent += 1
            await asyncio.sleep(0.05)  # под лимит Telegram 30 msg/sec
        except Exception as e:
            logger.warning(f"broadcast → {uid}: {e}")

    logger.info(f"📢 Рассылка завершена: {sent}/{len(subs)}")


def _run_hourly_alerts(app):
    """Проверяет пороги. Работает каждый час."""
    loop = app.bot_data.get("loop")
    if not loop:
        return
    asyncio.run_coroutine_threadsafe(_do_alert_check(app), loop)


async def _do_alert_check(app):
    # Текущий курс Bybit
    rate = fetch_bybit_p2p_rate()
    if not rate:
        rates = fetch_p2p_army_rates()
        if rates and "bybit" in rates:
            rate = rates["bybit"]
    if not rate:
        logger.info("🔔 Алерт: курс Bybit недоступен")
        return

    # Кого надо уведомить
    triggered = []
    with SUBSCRIBERS_LOCK:
        subs = load_subscribers()
        for s in subs:
            alert = s.get("alert_price")
            if alert is not None:
                try:
                    if rate < float(alert):
                        triggered.append({
                            "user_id": s.get("user_id"),
                            "alert": float(alert),
                        })
                        s["alert_price"] = None  # сбрасываем, чтобы не спамить
                except (TypeError, ValueError):
                    pass
        if triggered:
            save_subscribers(subs)

    if not triggered:
        return

    logger.info(f"🔔 Алерт сработал для {len(triggered)} подписчиков (Bybit: {rate:.2f})")

    for t in triggered:
        uid = t["user_id"]
        alert = t["alert"]
        try:
            await app.bot.send_message(
                chat_id=uid,
                text=(
                    f"🔔 <b>Курс Bybit упал ниже {alert:.2f} RUB!</b>\n\n"
                    f"Сейчас: <b>{rate:.2f} RUB</b>\n\n"
                    f"Посмотреть все курсы: /buy"
                ),
                parse_mode="HTML",
            )
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.warning(f"alert → {uid}: {e}")


def run_scheduler(app):
    """Отдельный поток с планировщиком."""
    schedule.every(5).minutes.do(_run_daily_broadcast, app=app)
    schedule.every(1).hours.do(_run_hourly_alerts, app=app)

    logger.info("🗓 Планировщик запущен:")
    logger.info("  • рассылка — проверка каждые 5 мин, срабатывает в 10:00 МСК")
    logger.info("  • алерты — раз в час")

    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            logger.error(f"scheduler error: {e}")
        time.sleep(30)


# ================== ЗАПУСК ==================
async def post_init(app: Application) -> None:
    """Сохраняем loop и запускаем планировщик."""
    app.bot_data["loop"] = asyncio.get_running_loop()
    threading.Thread(target=run_scheduler, args=(app,), daemon=True).start()


def build_app(token):
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
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("❌ Не задана переменная окружения BOT_TOKEN")

    logger.info("🚀 Бот запускается...")
    logger.info(f"p2p.army ключ: {'✅ есть' if P2P_ARMY_KEY else '❌ нет'}")
    logger.info(f"Файл подписчиков: {SUBSCRIBERS_FILE} "
                f"({'есть' if os.path.exists(SUBSCRIBERS_FILE) else 'будет создан'})")

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