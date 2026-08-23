import os
import time
import math
import asyncio
import requests
import io
import re
import html
import json
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from datetime import timedelta, datetime, time as dt_time
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# =========================================================
# SETTINGS
# =========================================================

ALLOWED_USERS = {568945385, 325575727}

TOKEN = os.environ.get("BOT_TOKEN")
MARKETDATA_TOKEN = os.environ.get("MARKETDATA_TOKEN")
FINNHUB_TOKEN = os.environ.get("FINNHUB_TOKEN")

# Fast backup news layer.
# We do NOT rely on unofficial X/Nitter mirrors because they commonly return 403
# from cloud hosts such as Render. Finnhub remains the primary source, while
# Google News RSS is used as a second independent discovery layer.
# If an official X API bearer token is added later, it can be integrated safely
# without weakening the current news path.
FAST_NEWS_RSS_URL = "https://news.google.com/rss/search"
FAST_NEWS_TIMEOUT_SECONDS = 15
FAST_NEWS_AUTO_MIN_IMPORTANCE = 8
FAST_NEWS_BATCH_SIZE = 8

PORT = int(os.environ.get("PORT", 10000))

WEBHOOK_BASE_URL = "https://option-vision-bot.onrender.com"
WEBHOOK_PATH = "telegram"
WEBHOOK_URL = f"{WEBHOOK_BASE_URL}/{WEBHOOK_PATH}"

MAX_OPTION_ASK = 5.00
MAX_LIVE_OPTION_QUOTE_AGE_MINUTES = 5.0
MIN_VOLUME = 1000
MIN_TOP_SCORE = 80
MIN_TOP_UOA = 3
TOP_N_RESULTS = 10

WATCH_INTERVAL_SECONDS = 300
WATCH_LOOP_SECONDS = 60
CACHE_SECONDS = 900

NEWS_DAYS = 1
# V28: refresh automatic news discovery every minute.
# Keep cache below the loop interval so each pass performs a fresh news scan.
NEWS_CACHE_SECONDS = 45
NEWS_AUTO_INTERVAL_SECONDS = 60
NEWS_AUTO_MIN_IMPORTANCE = 8
# الإرسال التلقائي فقط: الخبر العادي خلال ساعتين، و10/10 خلال 4 ساعات كحد أقصى.
NEWS_AUTO_MAX_AGE_HOURS = 18
NEWS_AUTO_CRITICAL_MAX_AGE_HOURS = 18
NEWS_AUTO_CRITICAL_IMPORTANCE = 10
MAX_NEWS_RESULTS = 30

# Automatic best-opportunity push.
AUTO_TOP_OPPORTUNITIES_INTERVAL_SECONDS = 3600
AUTO_TOP_OPPORTUNITIES_START_DELAY_SECONDS = 90
AUTO_TOP_OPPORTUNITIES_MIN_SCORE = 65

EARNINGS_LOOKAHEAD_DAYS = 21
EARNINGS_LOOP_SECONDS = 21600

# Earnings reminder safety:
# - Reminders are sent only once in the 09:00–09:59 KSA window.
# - Sent keys are also written to a local state file so a process restart
#   does not resend the same reminder during the same deployment.
EARNINGS_REMINDER_HOUR_KSA = 9
EARNINGS_SENT_STATE_FILE = os.environ.get("STATE_DIR", "/var/data").rstrip("/") + "/option_bot_earnings_sent.json"

# Dedupe state survives ordinary process restarts while the same Render
# runtime filesystem remains available. A fresh/deleted runtime still needs
# durable external storage if dedupe must survive every redeploy indefinitely.
AUTO_SENT_STATE_FILE = os.environ.get("STATE_DIR", "/var/data").rstrip("/") + "/option_bot_auto_sent.json"
VERIFIED_DAILY_NEWS_FILE = os.environ.get("STATE_DIR", "/var/data").rstrip("/") + "/option_bot_verified_daily_news.json"

SCAN_SYMBOLS = [
    "SPY", "QQQ", "AMD", "TSLA", "NVDA", "MRVL", "ARM", "AVGO", "MU", "GS",
    "META", "IWM", "AAPL", "GOOGL", "MSFT", "AMZN", "SMCI", "SNOW", "SHOP",
    "BA", "CRM", "CAT", "PLTR", "ORCL", "OPEN", "IBIT", "MSTR", "COIN", "SPCX",
    "SKHY",
]

TOP10_CACHE = {"time": 0, "results": None}
NEWS_CACHE = {"time": 0, "results": None}
PENDING_WATCHES = {}


def _load_earnings_sent_state():
    try:
        path = EARNINGS_SENT_STATE_FILE
        if not os.path.exists(path):
            return set()

        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)

        if not isinstance(data, list):
            return set()

        return {
            str(item)
            for item in data
            if item
        }

    except Exception as exc:
        print(
            "EARNINGS STATE LOAD ERROR:",
            repr(exc),
            flush=True,
        )
        return set()


def _save_earnings_sent_state(sent_keys):
    try:
        path = EARNINGS_SENT_STATE_FILE
        temp_path = path + ".tmp"

        with open(
            temp_path,
            "w",
            encoding="utf-8"
        ) as handle:
            json.dump(
                sorted(sent_keys),
                handle,
                ensure_ascii=False,
            )

        os.replace(
            temp_path,
            path,
        )

    except Exception as exc:
        print(
            "EARNINGS STATE SAVE ERROR:",
            repr(exc),
            flush=True,
        )



def _normalize_event_reminder_key(item):
    """Return a stable string key and migrate older JSON list/tuple keys."""
    if isinstance(item, str):
        return item

    if isinstance(item, (list, tuple)) and len(item) == 3:
        name, event_dt, stage = item
        return f"{name}|{event_dt}|{stage}"

    return None


def _load_auto_sent_state():
    try:
        if not os.path.exists(AUTO_SENT_STATE_FILE):
            return {
                "event_keys": set(),
                "news_ids": set(),
                "news_events": [],
                "top_signature": None,
            }

        with open(AUTO_SENT_STATE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)

        raw_event_keys = data.get("event_keys") or []
        event_keys = {
            normalized
            for item in raw_event_keys
            if (normalized := _normalize_event_reminder_key(item))
        }

        return {
            "event_keys": event_keys,
            "news_ids": set(data.get("news_ids") or []),
            "news_events": list(data.get("news_events") or []),
            "top_signature": data.get("top_signature"),
        }
    except Exception as exc:
        print("AUTO STATE LOAD ERROR:", repr(exc), flush=True)
        return {
            "event_keys": set(),
            "news_ids": set(),
            "news_events": [],
            "top_signature": None,
        }


def _save_auto_sent_state():
    try:
        payload = {
            "event_keys": sorted(SENT_EVENT_REMINDERS),
            "news_ids": sorted(SENT_NEWS_IDS),
            "news_events": SENT_NEWS_EVENTS[-300:],
            "top_signature": LAST_AUTO_TOP_SIGNATURE,
        }

        temp_path = AUTO_SENT_STATE_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)

        os.replace(temp_path, AUTO_SENT_STATE_FILE)
    except Exception as exc:
        print("AUTO STATE SAVE ERROR:", repr(exc), flush=True)


EARNINGS_SENT = _load_earnings_sent_state()
AUTO_SENT_STATE = _load_auto_sent_state()
LAST_AUTO_TOP_SIGNATURE = AUTO_SENT_STATE.get("top_signature")
SENT_NEWS_IDS = set(AUTO_SENT_STATE.get("news_ids") or [])
SENT_NEWS_EVENTS = list(AUTO_SENT_STATE.get("news_events") or [])



def _load_verified_daily_news():
    try:
        if not os.path.exists(VERIFIED_DAILY_NEWS_FILE):
            return []
        with open(VERIFIED_DAILY_NEWS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except Exception as exc:
        print("VERIFIED DAILY NEWS LOAD ERROR:", repr(exc), flush=True)
        return []


def _save_verified_daily_news(items):
    try:
        folder = os.path.dirname(VERIFIED_DAILY_NEWS_FILE)
        if folder:
            os.makedirs(folder, exist_ok=True)
        temp_path = VERIFIED_DAILY_NEWS_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(items[-200:], handle, ensure_ascii=False)
        os.replace(temp_path, VERIFIED_DAILY_NEWS_FILE)
    except Exception as exc:
        print("VERIFIED DAILY NEWS SAVE ERROR:", repr(exc), flush=True)


def _remember_verified_daily_news(items):
    """Persist only stories that already passed score_news_item() + dedupe."""
    today_ksa = datetime.now(ZoneInfo("Asia/Riyadh")).date()
    current = []
    for old in _load_verified_daily_news():
        try:
            ts = float(old.get("timestamp") or 0)
            if datetime.fromtimestamp(ts, ZoneInfo("Asia/Riyadh")).date() == today_ksa:
                current.append(old)
        except Exception:
            continue

    by_key = {}
    for item in current + list(items or []):
        try:
            ts = float(item.get("timestamp") or 0)
            if datetime.fromtimestamp(ts, ZoneInfo("Asia/Riyadh")).date() != today_ksa:
                continue
            key = (
                str(item.get("symbol") or "").upper(),
                str(item.get("url") or item.get("headline") or "").strip().lower(),
            )
            if not key[0] or not key[1]:
                continue
            by_key[key] = {
                "id": str(item.get("id") or ""),
                "symbol": str(item.get("symbol") or "").upper(),
                "headline": str(item.get("headline") or ""),
                "summary": str(item.get("summary") or ""),
                "source": str(item.get("source") or ""),
                "url": str(item.get("url") or ""),
                "timestamp": ts,
                "importance": int(item.get("importance") or 0),
                "reason_ar": str(item.get("reason_ar") or item.get("category_ar") or ""),
                "category_ar": str(item.get("category_ar") or item.get("reason_ar") or ""),
                "sentiment": str(item.get("sentiment") or ""),
                "sentiment_ar": str(item.get("sentiment_ar") or ""),
                "trusted_fast_news": bool(item.get("trusted_fast_news")),
            }
        except Exception:
            continue

    merged = list(by_key.values())
    merged.sort(key=lambda x: (-int(x.get("importance") or 0), -float(x.get("timestamp") or 0)))
    _save_verified_daily_news(merged)
    return merged


# =========================================================
# ACCESS
# =========================================================

def _allowed(update):
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USERS


async def deny_access(update):
    if update.callback_query:
        await update.callback_query.answer(
            "⛔ غير مصرح لك باستخدام هذا البوت.",
            show_alert=True
        )
        return

    if update.effective_message:
        await update.effective_message.reply_text(
            "⛔ غير مصرح لك باستخدام هذا البوت."
        )


# =========================================================
# HELPERS
# =========================================================

def format_expiry_timestamp(expiration):
    expiry = datetime.fromtimestamp(
        int(expiration),
        ZoneInfo("America/New_York")
    )
    return f"{expiry.day} {expiry.strftime('%b %Y')}"


def is_us_market_open():
    ny_now = datetime.now(ZoneInfo("America/New_York"))

    if ny_now.weekday() >= 5:
        return False

    return dt_time(9, 30) <= ny_now.time() < dt_time(16, 0)


def get_headers():
    return {"Authorization": f"Bearer {MARKETDATA_TOKEN}"}


def get_finnhub_headers():
    return {"X-Finnhub-Token": FINNHUB_TOKEN}


# =========================================================
# MARKET DATA
# =========================================================

def get_option_chain(symbol):
    url = f"https://api.marketdata.app/v1/options/chain/{symbol}/"

    today = datetime.now(
        ZoneInfo("America/New_York")
    ).date()

    params = {
        "from": (today + timedelta(days=5)).isoformat(),
        "to": (today + timedelta(days=30)).isoformat(),
        "maxAsk": MAX_OPTION_ASK,
        "minVolume": MIN_VOLUME,
        "minOpenInterest": 100,
    }

    response = requests.get(
        url,
        headers=get_headers(),
        params=params,
        timeout=20
    )
    response.raise_for_status()
    data = response.json()

    if data.get("s") != "ok":
        raise ValueError(
            data.get("errmsg", "لم يتم الحصول على بيانات الخيارات.")
        )

    return data


# =========================================================
# STOCK TREND
# =========================================================

def get_stock_trend(symbol):
    url = f"https://api.marketdata.app/v1/stocks/candles/D/{symbol}/"

    response = requests.get(
        url,
        headers=get_headers(),
        params={"countback": 30},
        timeout=20
    )
    response.raise_for_status()
    data = response.json()

    if data.get("s") != "ok":
        raise ValueError("تعذر الحصول على حركة السهم.")

    closes = data.get("c", [])
    volumes = data.get("v", [])

    if len(closes) < 21:
        raise ValueError("لا توجد شموع كافية لتحليل الاتجاه.")

    closes = [float(x) for x in closes]
    last_close = closes[-1]

    sma5 = sum(closes[-5:]) / 5
    sma10 = sum(closes[-10:]) / 10
    sma20 = sum(closes[-20:]) / 20

    change_3 = (
        (last_close - closes[-4]) / closes[-4] * 100
        if closes[-4] != 0 else 0
    )
    change_5 = (
        (last_close - closes[-6]) / closes[-6] * 100
        if closes[-6] != 0 else 0
    )
    change_10 = (
        (last_close - closes[-11]) / closes[-11] * 100
        if closes[-11] != 0 else 0
    )

    if volumes and len(volumes) >= 20:
        volume_values = [float(x) for x in volumes]
        recent_volume = volume_values[-1]
        avg_volume20 = sum(volume_values[-20:]) / 20
        volume_ratio = (
            recent_volume / avg_volume20 if avg_volume20 > 0 else 1
        )
    else:
        volume_ratio = 1

    bullish_points = 0
    bearish_points = 0

    if last_close > sma5:
        bullish_points += 1
    else:
        bearish_points += 1

    if sma5 > sma10:
        bullish_points += 1
    else:
        bearish_points += 1

    if sma10 > sma20:
        bullish_points += 1
    else:
        bearish_points += 1

    if change_5 > 1:
        bullish_points += 1
    elif change_5 < -1:
        bearish_points += 1

    if change_10 > 2:
        bullish_points += 1
    elif change_10 < -2:
        bearish_points += 1

    if bullish_points >= 3 and bullish_points > bearish_points:
        bias = "CALL"
        label = "🟢 صاعد"
        strength = bullish_points
    elif bearish_points >= 3 and bearish_points > bullish_points:
        bias = "PUT"
        label = "🔴 هابط"
        strength = bearish_points
    else:
        bias = "NEUTRAL"
        label = "🟡 محايد"
        strength = max(bullish_points, bearish_points)

    momentum_score = 0

    if bias == "CALL":
        if change_5 >= 5:
            momentum_score += 4
        elif change_5 >= 3:
            momentum_score += 3
        elif change_5 >= 1:
            momentum_score += 2

        if change_10 >= 8:
            momentum_score += 4
        elif change_10 >= 5:
            momentum_score += 3
        elif change_10 >= 2:
            momentum_score += 2

    elif bias == "PUT":
        if change_5 <= -5:
            momentum_score += 4
        elif change_5 <= -3:
            momentum_score += 3
        elif change_5 <= -1:
            momentum_score += 2

        if change_10 <= -8:
            momentum_score += 4
        elif change_10 <= -5:
            momentum_score += 3
        elif change_10 <= -2:
            momentum_score += 2

    if volume_ratio >= 1.5:
        momentum_score += 2
    elif volume_ratio >= 1.1:
        momentum_score += 1

    momentum_score = min(momentum_score, 10)

    if momentum_score >= 8:
        momentum_label = "🔥 قوي جدًا"
    elif momentum_score >= 6:
        momentum_label = "🟢 قوي"
    elif momentum_score >= 4:
        momentum_label = "🟡 متوسط"
    else:
        momentum_label = "⚪ ضعيف"

    continuation_score = 0

    if bias == "CALL":
        if change_3 > 1:
            continuation_score += 2
        elif change_3 < -1:
            continuation_score -= 2

        if last_close > sma5:
            continuation_score += 1
        else:
            continuation_score -= 1

        if sma5 > sma10 > sma20:
            continuation_score += 2

    elif bias == "PUT":
        if change_3 < -1:
            continuation_score += 2
        elif change_3 > 1:
            continuation_score -= 2

        if last_close < sma5:
            continuation_score += 1
        else:
            continuation_score -= 1

        if sma5 < sma10 < sma20:
            continuation_score += 2

    if continuation_score >= 4:
        continuation_label = "🔥 مستمر بقوة"
    elif continuation_score >= 2:
        continuation_label = "🟢 مستمر"
    elif continuation_score >= 0:
        continuation_label = "🟡 متماسك"
    else:
        continuation_label = "⚠️ بدأ يضعف"

    return {
        "bias": bias,
        "label": label,
        "strength": strength,
        "last_close": last_close,
        "sma5": sma5,
        "sma10": sma10,
        "sma20": sma20,
        "change_3": change_3,
        "change_5": change_5,
        "change_10": change_10,
        "volume_ratio": volume_ratio,
        "momentum_score": momentum_score,
        "momentum_label": momentum_label,
        "continuation_score": continuation_score,
        "continuation_label": continuation_label,
    }


# =========================================================
# 15 MINUTE DATA
# =========================================================

def get_intraday_15m(symbol):
    url = f"https://api.marketdata.app/v1/stocks/candles/15/{symbol}/"

    response = requests.get(
        url,
        headers=get_headers(),
        params={"countback": 30},
        timeout=20
    )
    response.raise_for_status()
    data = response.json()

    if data.get("s") != "ok":
        raise ValueError("تعذر الحصول على شموع 15 دقيقة.")

    closes = data.get("c", [])

    if len(closes) < 6:
        raise ValueError("بيانات 15 دقيقة غير كافية.")

    closes = [float(x) for x in closes]

    last_price = closes[-1]
    sma5 = sum(closes[-5:]) / 5

    change_3bars = (
        (last_price - closes[-4]) / closes[-4] * 100
        if closes[-4] != 0 else 0
    )

    up_bars = 0
    down_bars = 0

    recent = closes[-5:]

    for i in range(1, len(recent)):
        if recent[i] > recent[i - 1]:
            up_bars += 1
        elif recent[i] < recent[i - 1]:
            down_bars += 1

    return {
        "last_price": last_price,
        "sma5": sma5,
        "change_3bars": change_3bars,
        "up_bars": up_bars,
        "down_bars": down_bars,
    }


# =========================================================
# 1 HOUR DATA — TOP OPPORTUNITIES CONFIRMATION
# =========================================================

def get_intraday_1h(symbol):
    url = f"https://api.marketdata.app/v1/stocks/candles/60/{symbol}/"

    response = requests.get(
        url,
        headers=get_headers(),
        params={"countback": 30},
        timeout=20
    )
    response.raise_for_status()
    data = response.json()

    if data.get("s") != "ok":
        raise ValueError("تعذر الحصول على شموع الساعة.")

    closes = data.get("c", [])

    if len(closes) < 6:
        raise ValueError("بيانات الساعة غير كافية.")

    closes = [float(x) for x in closes]

    last_price = closes[-1]
    sma5 = sum(closes[-5:]) / 5

    change_3bars = (
        (last_price - closes[-4]) / closes[-4] * 100
        if closes[-4] != 0 else 0
    )

    up_bars = 0
    down_bars = 0

    for i in range(-5, 0):
        if closes[i] > closes[i - 1]:
            up_bars += 1
        elif closes[i] < closes[i - 1]:
            down_bars += 1

    return {
        "last_price": last_price,
        "sma5": sma5,
        "change_3bars": change_3bars,
        "up_bars": up_bars,
        "down_bars": down_bars,
    }


# =========================================================
# CHART DATA
# =========================================================

def get_raw_chart_data(symbol, resolution, countback):
    url = (
        f"https://api.marketdata.app/"
        f"v1/stocks/candles/{resolution}/{symbol}/"
    )

    response = requests.get(
        url,
        headers=get_headers(),
        params={"countback": countback},
        timeout=20
    )
    response.raise_for_status()
    data = response.json()

    if data.get("s") != "ok":
        raise ValueError(
            data.get("errmsg", "تعذر الحصول على بيانات الشارت.")
        )

    opens = data.get("o", [])
    highs = data.get("h", [])
    lows = data.get("l", [])
    closes = data.get("c", [])
    timestamps = data.get("t", [])

    count = min(
        len(opens), len(highs), len(lows), len(closes), len(timestamps)
    )

    if count < 20:
        raise ValueError("بيانات الشارت غير كافية.")

    return {
        "opens": [float(x) for x in opens[:count]],
        "highs": [float(x) for x in highs[:count]],
        "lows": [float(x) for x in lows[:count]],
        "closes": [float(x) for x in closes[:count]],
        "timestamps": [int(x) for x in timestamps[:count]],
    }


def aggregate_4hour_data(hourly_data):
    opens = hourly_data["opens"]
    highs = hourly_data["highs"]
    lows = hourly_data["lows"]
    closes = hourly_data["closes"]
    timestamps = hourly_data["timestamps"]

    grouped = {}
    ny_tz = ZoneInfo("America/New_York")

    for i in range(len(closes)):
        dt = datetime.fromtimestamp(timestamps[i], ny_tz)

        market_start_minutes = 9 * 60 + 30
        current_minutes = dt.hour * 60 + dt.minute
        minutes_from_open = current_minutes - market_start_minutes

        if minutes_from_open < 0:
            continue

        bucket = minutes_from_open // 240
        key = (dt.date(), bucket)

        if key not in grouped:
            grouped[key] = {
                "open": opens[i],
                "high": highs[i],
                "low": lows[i],
                "close": closes[i],
                "timestamp": timestamps[i],
            }
        else:
            grouped[key]["high"] = max(grouped[key]["high"], highs[i])
            grouped[key]["low"] = min(grouped[key]["low"], lows[i])
            grouped[key]["close"] = closes[i]

    values = list(grouped.values())
    values.sort(key=lambda x: x["timestamp"])

    if len(values) < 20:
        raise ValueError("بيانات 4 ساعات غير كافية.")

    return {
        "opens": [item["open"] for item in values],
        "highs": [item["high"] for item in values],
        "lows": [item["low"] for item in values],
        "closes": [item["close"] for item in values],
        "timestamps": [item["timestamp"] for item in values],
    }


def get_chart_data(symbol, resolution):
    if resolution == "15":
        return get_raw_chart_data(symbol, "15", 90)

    if resolution == "60":
        return get_raw_chart_data(symbol, "60", 90)

    if resolution == "240":
        hourly_data = get_raw_chart_data(symbol, "60", 260)
        return aggregate_4hour_data(hourly_data)

    return get_raw_chart_data(symbol, "D", 100)


# =========================================================
# INDICATORS + FORECAST
# =========================================================

def moving_average(values, period):
    result = []

    for i in range(len(values)):
        start = max(0, i - period + 1)
        window = values[start:i + 1]
        result.append(sum(window) / len(window))

    return result


def calculate_recent_volatility(closes):
    returns = []
    recent = closes[-21:]

    for i in range(1, len(recent)):
        previous = recent[i - 1]
        current = recent[i]

        if previous <= 0:
            continue

        returns.append(math.log(current / previous))

    if len(returns) < 2:
        return 0.01

    mean_return = sum(returns) / len(returns)

    variance = (
        sum((r - mean_return) ** 2 for r in returns)
        / (len(returns) - 1)
    )

    return max(math.sqrt(variance), 0.001)


def linear_trend_pct(closes, lookback=20):
    values = closes[-lookback:]
    n = len(values)

    if n < 5:
        return 0

    x_mean = (n - 1) / 2

    y_values = [
        math.log(max(value, 0.0001))
        for value in values
    ]

    y_mean = sum(y_values) / n

    numerator = 0
    denominator = 0

    for i, y in enumerate(y_values):
        numerator += (i - x_mean) * (y - y_mean)
        denominator += (i - x_mean) ** 2

    if denominator == 0:
        return 0

    return numerator / denominator


def calculate_atr(highs, lows, closes, period=14):
    true_ranges = []
    start = max(1, len(closes) - period)

    for i in range(start, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        true_ranges.append(tr)

    if not true_ranges:
        return max(closes[-1] * 0.01, 0.01)

    return sum(true_ranges) / len(true_ranges)


def interpolate_path(pivot_points, future_bars):
    path = [None] * (future_bars + 1)

    for (
        start_index,
        start_price
    ), (
        end_index,
        end_price
    ) in zip(
        pivot_points[:-1],
        pivot_points[1:]
    ):

        distance = end_index - start_index

        if distance <= 0:
            continue

        for step in range(distance + 1):
            ratio = step / distance

            value = (
                start_price
                + (end_price - start_price) * ratio
            )

            path[start_index + step] = value

    previous = pivot_points[0][1]

    for i in range(len(path)):
        if path[i] is None:
            path[i] = previous

        previous = path[i]

    return path


def build_forecast(closes, highs, lows, resolution):
    last_price = closes[-1]

    sma10 = moving_average(closes, 10)
    sma20 = moving_average(closes, 20)

    volatility = calculate_recent_volatility(closes)
    atr = calculate_atr(highs, lows, closes, 14)
    trend_slope = linear_trend_pct(closes, 20)

    recent_change_5 = (
        (closes[-1] - closes[-6]) / closes[-6]
        if len(closes) >= 6 and closes[-6] != 0
        else 0
    )

    ma_bias = (
        (sma10[-1] - sma20[-1]) / last_price
        if last_price > 0
        else 0
    )

    momentum_component = recent_change_5 / 5

    drift = (
        trend_slope * 0.50
        + momentum_component * 0.25
        + ma_bias * 0.25
    )

    future_bars = 10 if resolution == "D" else 12

    resistance = max(highs[-20:])
    support = min(lows[-20:])

    zone_width = max(
        atr * 0.30,
        last_price * 0.0025
    )

    resistance_low = resistance - zone_width
    resistance_high = resistance + zone_width
    support_low = support - zone_width
    support_high = support + zone_width

    direction_score = 0

    direction_score += 1 if last_price > sma10[-1] else -1
    direction_score += 1 if sma10[-1] > sma20[-1] else -1

    if trend_slope > 0:
        direction_score += 1
    elif trend_slope < 0:
        direction_score -= 1

    if recent_change_5 > 0:
        direction_score += 1
    elif recent_change_5 < 0:
        direction_score -= 1

    if drift > 0:
        direction_score += 1
    elif drift < 0:
        direction_score -= 1

    price_range = max(resistance - support, atr * 3)

    if direction_score >= 2:
        scenario = "BULLISH"
        scenario_ar = "🟢 صاعد"

        pullback = max(
            sma10[-1],
            last_price - atr * 0.70
        )

        first_push = max(
            resistance,
            last_price + atr * 1.10
        )

        retest = max(
            last_price,
            first_push - atr * 0.65
        )

        target2 = max(
            first_push + atr * 1.25,
            resistance + atr
        )

        pivot_points = [
            (0, last_price),
            (max(1, round(future_bars * 0.22)), pullback),
            (max(2, round(future_bars * 0.48)), first_push),
            (max(3, round(future_bars * 0.68)), retest),
            (future_bars, target2),
        ]

        target1 = first_push
        invalidation = support_low

    elif direction_score <= -2:
        scenario = "BEARISH"
        scenario_ar = "🔴 هابط"

        bounce = min(
            sma10[-1],
            last_price + atr * 0.70
        )

        first_drop = min(
            support,
            last_price - atr * 1.10
        )

        retest = min(
            last_price,
            first_drop + atr * 0.65
        )

        target2 = min(
            first_drop - atr * 1.25,
            support - atr
        )

        pivot_points = [
            (0, last_price),
            (max(1, round(future_bars * 0.22)), bounce),
            (max(2, round(future_bars * 0.48)), first_drop),
            (max(3, round(future_bars * 0.68)), retest),
            (future_bars, target2),
        ]

        target1 = first_drop
        invalidation = resistance_high

    else:
        scenario = "SIDEWAYS"
        scenario_ar = "🟡 عرضي"

        upper_test = min(
            resistance,
            last_price + price_range * 0.30
        )

        lower_test = max(
            support,
            last_price - price_range * 0.30
        )

        final_price = (upper_test + lower_test) / 2

        pivot_points = [
            (0, last_price),
            (max(1, round(future_bars * 0.30)), upper_test),
            (max(2, round(future_bars * 0.60)), lower_test),
            (future_bars, final_price),
        ]

        target1 = upper_test
        target2 = lower_test
        invalidation = support_low

    expected = interpolate_path(
        pivot_points,
        future_bars
    )

    upper = []
    lower = []

    for step, projected in enumerate(expected):
        if step == 0:
            uncertainty = 0
        else:
            uncertainty = (
                volatility * math.sqrt(step) * 0.55
            )

        upper.append(
            projected * math.exp(uncertainty)
        )

        lower.append(
            projected * math.exp(-uncertainty)
        )

    expected_end = expected[-1]

    change_pct = (
        (expected_end - last_price)
        / last_price
        * 100
        if last_price > 0
        else 0
    )

    confidence_raw = (
        abs(direction_score)
        + (
            abs(drift)
            / max(volatility, 0.001)
        )
    )

    confidence = min(
        88,
        max(
            48,
            round(
                50 + confidence_raw * 5
            )
        )
    )

    return {
        "expected": expected,
        "upper": upper,
        "lower": lower,
        "future_bars": future_bars,
        "pivot_points": pivot_points,
        "scenario": scenario,
        "scenario_ar": scenario_ar,
        "target1": target1,
        "target2": target2,
        "invalidation": invalidation,
        "support": support,
        "resistance": resistance,
        "support_low": support_low,
        "support_high": support_high,
        "resistance_low": resistance_low,
        "resistance_high": resistance_high,
        "change_pct": change_pct,
        "confidence": confidence,
        "volatility": volatility,
        "atr": atr,
    }


# =========================================================
# FORECAST CHART
# =========================================================

def _linear_fit(values):
    n = len(values)
    if n < 2:
        return 0.0, values[-1] if values else 0.0

    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    denominator = sum((i - x_mean) ** 2 for i in range(n))

    if denominator == 0:
        return 0.0, y_mean

    slope = sum(
        (i - x_mean) * (value - y_mean)
        for i, value in enumerate(values)
    ) / denominator
    intercept = y_mean - slope * x_mean
    return slope, intercept


def _line_value(slope, intercept, x):
    return slope * x + intercept


def _detect_clean_setup(highs, lows, closes):
    """Detect a clean price-action setup for the chart.

    Preference is given to a converging triangle. If no valid triangle is
    present, the function falls back to a simple trend / range structure
    without inventing a chart pattern.
    """
    last_price = closes[-1]
    atr = calculate_atr(highs, lows, closes, 14)
    n = len(closes)

    best_triangle = None

    for lookback in (18, 22, 26, 30, 35):
        if n < lookback:
            continue

        h = highs[-lookback:]
        l = lows[-lookback:]
        upper_slope, upper_intercept = _linear_fit(h)
        lower_slope, lower_intercept = _linear_fit(l)

        start_gap = (
            _line_value(upper_slope, upper_intercept, 0)
            - _line_value(lower_slope, lower_intercept, 0)
        )
        end_gap = (
            _line_value(upper_slope, upper_intercept, lookback - 1)
            - _line_value(lower_slope, lower_intercept, lookback - 1)
        )

        if start_gap <= 0 or end_gap <= 0:
            continue

        convergence = 1 - (end_gap / start_gap)
        upper_ok = upper_slope < 0
        lower_ok = lower_slope > 0
        price_scale = max(last_price, 0.01)
        slope_strength = (
            abs(upper_slope) + abs(lower_slope)
        ) / price_scale

        if upper_ok and lower_ok and convergence >= 0.28:
            score = convergence * 100 + slope_strength * 10000
            if best_triangle is None or score > best_triangle["score"]:
                best_triangle = {
                    "score": score,
                    "lookback": lookback,
                    "upper_slope": upper_slope,
                    "upper_intercept": upper_intercept,
                    "lower_slope": lower_slope,
                    "lower_intercept": lower_intercept,
                    "start_gap": start_gap,
                    "end_gap": end_gap,
                }

    recent_high = max(highs[-20:])
    recent_low = min(lows[-20:])
    trend_pct = linear_trend_pct(closes, 20)
    sma10 = moving_average(closes, 10)[-1]
    sma20 = moving_average(closes, 20)[-1]

    if best_triangle:
        lookback = best_triangle["lookback"]
        x_now = lookback - 1
        upper_now = _line_value(
            best_triangle["upper_slope"],
            best_triangle["upper_intercept"],
            x_now,
        )
        lower_now = _line_value(
            best_triangle["lower_slope"],
            best_triangle["lower_intercept"],
            x_now,
        )

        midpoint = (upper_now + lower_now) / 2
        bullish_bias = (
            last_price >= midpoint
            and sma10 >= sma20
            and trend_pct >= -0.002
        )
        bearish_bias = (
            last_price < midpoint
            and sma10 < sma20
            and trend_pct <= 0.002
        )

        if bullish_bias:
            direction = "BULLISH"
            trigger = upper_now + max(atr * 0.10, last_price * 0.001)
            invalidation = lower_now - max(atr * 0.20, last_price * 0.0015)
            measured = max(best_triangle["start_gap"], atr * 3)
            targets = [
                trigger + measured * 0.35,
                trigger + measured * 0.55,
                trigger + measured * 0.75,
                trigger + measured,
            ]
        elif bearish_bias:
            direction = "BEARISH"
            trigger = lower_now - max(atr * 0.10, last_price * 0.001)
            invalidation = upper_now + max(atr * 0.20, last_price * 0.0015)
            measured = max(best_triangle["start_gap"], atr * 3)
            targets = [
                trigger - measured * 0.35,
                trigger - measured * 0.55,
                trigger - measured * 0.75,
                trigger - measured,
            ]
        else:
            direction = "NEUTRAL"
            trigger = upper_now
            invalidation = lower_now
            measured = max(best_triangle["start_gap"], atr * 3)
            targets = [
                upper_now + measured * 0.35,
                upper_now + measured * 0.55,
                upper_now + measured * 0.75,
                upper_now + measured,
            ]

        confidence = int(min(92, max(60, 58 + best_triangle["score"] * 0.35)))

        return {
            "type": "triangle",
            "name_ar": "مثلث متماثل",
            "direction": direction,
            "lookback": lookback,
            "upper_slope": best_triangle["upper_slope"],
            "upper_intercept": best_triangle["upper_intercept"],
            "lower_slope": best_triangle["lower_slope"],
            "lower_intercept": best_triangle["lower_intercept"],
            "trigger": trigger,
            "invalidation": invalidation,
            "targets": targets,
            "confidence": confidence,
        }

    # No reliable pattern: show only the real structure, no invented model.
    bullish = last_price > sma10 > sma20 and trend_pct > 0
    bearish = last_price < sma10 < sma20 and trend_pct < 0

    if bullish:
        direction = "BULLISH"
        trigger = recent_high + max(atr * 0.10, last_price * 0.001)
        invalidation = max(recent_low, last_price - atr * 2.0)
        targets = [trigger + atr * x for x in (1.0, 1.8, 2.6, 3.4)]
        name_ar = "اتجاه صاعد"
    elif bearish:
        direction = "BEARISH"
        trigger = recent_low - max(atr * 0.10, last_price * 0.001)
        invalidation = min(recent_high, last_price + atr * 2.0)
        targets = [trigger - atr * x for x in (1.0, 1.8, 2.6, 3.4)]
        name_ar = "اتجاه هابط"
    else:
        direction = "NEUTRAL"
        trigger = recent_high
        invalidation = recent_low
        targets = [recent_high + atr * x for x in (1.0, 1.8, 2.6, 3.4)]
        name_ar = "نطاق سعري"

    return {
        "type": "structure",
        "name_ar": name_ar,
        "direction": direction,
        "trigger": trigger,
        "invalidation": invalidation,
        "targets": targets,
        "support": recent_low,
        "resistance": recent_high,
        "confidence": 55 if direction == "NEUTRAL" else 65,
    }


def make_chart(symbol, resolution):
    data = get_chart_data(symbol, resolution)

    opens = data["opens"]
    highs = data["highs"]
    lows = data["lows"]
    closes = data["closes"]

    # Clean reset: enough candles to read price action, without crushing the chart.
    visible_candles = {
        "15": 48,
        "60": 46,
        "240": 44,
        "D": 42,
    }.get(resolution, 44)

    candle_count = min(visible_candles, len(closes))
    opens = opens[-candle_count:]
    highs = highs[-candle_count:]
    lows = lows[-candle_count:]
    closes = closes[-candle_count:]

    if not closes:
        raise ValueError("No chart data available")

    last_price = closes[-1]
    first_price = closes[0]
    visible_change = ((last_price / first_price) - 1) * 100 if first_price else 0.0
    setup = _detect_clean_setup(highs, lows, closes)

    if resolution == "15":
        timeframe = "15 MIN"
        timeframe_ar = "15 دقيقة"
    elif resolution == "60":
        timeframe = "1 HOUR"
        timeframe_ar = "ساعة"
    elif resolution == "240":
        timeframe = "4 HOURS"
        timeframe_ar = "4 ساعات"
    else:
        timeframe = "DAILY"
        timeframe_ar = "يومي"

    # CLEAN BASE + one clean zone + conservative post-candle scenario path.
    fig, ax = plt.subplots(figsize=(11.8, 6.4))
    fig.patch.set_facecolor("#FFFFFF")
    ax.set_facecolor("#FFFFFF")

    candle_width = 0.62
    bull = "#2F8F5B"
    bear = "#C65345"
    wick = "#242424"
    gold = "#B48A34"
    charcoal = "#252525"
    muted = "#7E786F"
    invalid = "#B95B4F"

    for i, (o, h, l, c) in enumerate(zip(opens, highs, lows, closes)):
        is_up = c >= o
        ax.vlines(i, l, h, color=wick, linewidth=0.9, zorder=3)
        body_bottom = min(o, c)
        body_height = abs(c - o) or max(last_price * 0.00025, 0.01)
        ax.add_patch(Rectangle(
            (i - candle_width / 2, body_bottom),
            candle_width,
            body_height,
            facecolor=bull if is_up else bear,
            edgecolor=charcoal,
            linewidth=0.75,
            zorder=4,
        ))

    x_last = len(closes) - 1
    future_bars = 9
    future_end = x_last + future_bars

    # Only draw a real triangle when the existing detector actually finds one.
    if setup.get("type") == "triangle":
        lookback = setup["lookback"]
        x_start = len(closes) - lookback
        local_x = list(range(lookback + 3))
        chart_x = [x_start + x for x in local_x]
        upper_y = [_line_value(setup["upper_slope"], setup["upper_intercept"], x) for x in local_x]
        lower_y = [_line_value(setup["lower_slope"], setup["lower_intercept"], x) for x in local_x]
        ax.plot(chart_x, upper_y, color=charcoal, linewidth=1.35, zorder=5)
        ax.plot(chart_x, lower_y, color=charcoal, linewidth=1.35, zorder=5)

    direction = setup["direction"]
    level_color = gold if direction != "BEARISH" else charcoal

    # One clean supply/demand zone based only on recent price structure.
    # Keep it compact and behind candles; do not invent multiple zones.
    recent_n = min(18, len(closes))
    recent_highs = highs[-recent_n:]
    recent_lows = lows[-recent_n:]
    recent_closes = closes[-recent_n:]
    recent_ranges = [h - l for h, l in zip(recent_highs, recent_lows)]
    avg_range = (sum(recent_ranges) / len(recent_ranges)) if recent_ranges else max(last_price * 0.005, 0.01)
    zone_height = max(avg_range * 0.55, last_price * 0.0025)

    if direction == "BEARISH":
        anchor = max(recent_highs)
        zone_low = anchor - zone_height
        zone_high = anchor
        zone_label = "SUPPLY"
    else:
        anchor = min(recent_lows)
        zone_low = anchor
        zone_high = anchor + zone_height
        zone_label = "DEMAND"

    # Only show the zone when it remains relevant to current price.
    distance = min(abs(last_price - zone_low), abs(last_price - zone_high))
    zone_is_relevant = distance <= max(avg_range * 5.0, last_price * 0.05)

    # V29 chart logic guard:
    # A zone beyond the setup invalidation cannot be described as a retest
    # inside the SAME active scenario. Example: bullish invalidation 8.62
    # with demand at 6.70-7.00. Reaching that demand requires invalidating
    # the bullish setup first, so hide it from the current-scenario chart/text.
    invalidation_level = float(setup["invalidation"])
    zone_beyond_invalidation = False
    if direction == "BULLISH" and zone_label == "DEMAND":
        zone_beyond_invalidation = zone_high < invalidation_level
    elif direction == "BEARISH" and zone_label == "SUPPLY":
        zone_beyond_invalidation = zone_low > invalidation_level
    elif direction == "NEUTRAL":
        # Neutral setups still have an oriented structure from trigger vs invalidation.
        if float(setup["trigger"]) > invalidation_level and zone_label == "DEMAND":
            zone_beyond_invalidation = zone_high < invalidation_level
        elif float(setup["trigger"]) < invalidation_level and zone_label == "SUPPLY":
            zone_beyond_invalidation = zone_low > invalidation_level

    if zone_beyond_invalidation:
        zone_is_relevant = False

    if zone_is_relevant:
        zone_x0 = max(0, x_last - recent_n + 1)
        zone_x1 = x_last + 1.4
        ax.add_patch(Rectangle(
            (zone_x0, zone_low), zone_x1 - zone_x0, zone_high - zone_low,
            facecolor="#E9DFC5", edgecolor="none", alpha=0.62, zorder=0
        ))
        # Keep zone label inside the band and away from candle bodies.
        zone_mid = (zone_low + zone_high) / 2
        ax.text(zone_x0 + 0.35, zone_mid,
                f"{zone_label}  {zone_low:.2f}-{zone_high:.2f}",
                color=charcoal, fontsize=10.8, fontweight="bold",
                va="center", ha="left", zorder=5,
                bbox={"boxstyle": "round,pad=0.12", "facecolor": "#F4EEDC", "edgecolor": "none", "alpha": 0.86})

    # Confirmation.
    ax.hlines(setup["trigger"], x_last + 0.7, future_end,
              color=level_color, linewidth=1.35, linestyle="--", alpha=0.95, zorder=2)
    ax.text(future_end + 0.45, setup["trigger"], f"CONFIRM  {setup['trigger']:.2f}",
            color=level_color, fontsize=13.2, va="center", fontweight="bold")

    # Invalidation.
    ax.hlines(setup["invalidation"], x_last + 0.7, future_end,
              color=invalid, linewidth=1.15, linestyle="--", alpha=0.9, zorder=2)
    ax.text(future_end + 0.45, setup["invalidation"], f"INVALID  {setup['invalidation']:.2f}",
            color=invalid, fontsize=12.6, va="center", fontweight="bold")

    # Four targets, clean and separate.
    targets = setup["targets"][:4]
    for idx, target in enumerate(targets, 1):
        ax.hlines(target, x_last + 3.2, future_end,
                  color=gold, linewidth=0.95, linestyle=":", alpha=0.75, zorder=1)
        ax.text(future_end + 0.45, target, f"T{idx}  {target:.2f}",
                color=gold, fontsize=12.2, va="center", fontweight="bold")

    # Expected-path drawing intentionally removed.
    # Keep confirmation, invalidation, targets, zones and the Telegram text summary only.

    # Current price marker.
    ax.hlines(last_price, max(0, x_last - 7), x_last + 1.6,
              color="#C9C5BD", linewidth=0.7, linestyle=(0, (3, 4)), alpha=0.7, zorder=1)
    ax.text(x_last + 0.5, last_price, f"{last_price:.2f}",
            color=charcoal, fontsize=11.8, va="center",
            bbox={"boxstyle": "round,pad=0.22", "facecolor": "#FFFFFF", "edgecolor": "#C6A863", "linewidth": 0.8},
            zorder=6)

    title_direction = {"BULLISH": "BULLISH", "BEARISH": "BEARISH", "NEUTRAL": "NEUTRAL"}[direction]
    sign = "+" if visible_change >= 0 else ""
    ax.set_title(
        f"{symbol}   |   {timeframe}   |   ${last_price:.2f}   |   {title_direction}   |   {sign}{visible_change:.1f}%",
        fontsize=15.0, fontweight="bold", color=charcoal, pad=12, loc="left"
    )

    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")
    ax.tick_params(axis="y", colors="#69645E", labelsize=10.5, length=0, pad=4)
    ax.tick_params(axis="x", labelbottom=False, length=0)
    ax.grid(False)
    for side in ("top", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["right"].set_color("#DDD8CF")
    ax.spines["right"].set_linewidth(0.8)

    all_y = highs + lows + [setup["trigger"], setup["invalidation"]] + targets
    y_min, y_max = min(all_y), max(all_y)
    padding = max((y_max - y_min) * 0.08, last_price * 0.006)
    ax.set_xlim(-1, future_end + 5.2)
    ax.set_ylim(y_min - padding, y_max + padding)

    plt.tight_layout()
    image = io.BytesIO()
    plt.savefig(image, format="png", dpi=190, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    image.seek(0)

    scenario_text = {
        "BULLISH": "إيجابي",
        "BEARISH": "سلبي",
        "NEUTRAL": "محايد / انتظار تأكيد",
    }[direction]

    # V29: if price has already crossed the invalidation level, the setup is no
    # longer active. Do not keep showing "waiting for confirmation" or a bullish/
    # bearish scenario as if nothing happened.
    trigger_level = float(setup["trigger"])
    invalidation_level = float(setup["invalidation"])

    # V34: invalidation must work for NEUTRAL / waiting-for-confirmation setups too.
    # Some triangle/range setups are labelled NEUTRAL even though they still have
    # a clear confirmation above and invalidation below (or vice versa).
    # Example: AMD price 473.25, trigger 491.00, invalidation 478.63.
    # Price is already below invalidation, so the setup is no longer valid.
    bullish_structure = trigger_level > invalidation_level
    bearish_structure = trigger_level < invalidation_level

    scenario_invalidated = (
        (direction == "BULLISH" and last_price <= invalidation_level)
        or
        (direction == "BEARISH" and last_price >= invalidation_level)
        or
        (
            direction == "NEUTRAL"
            and bullish_structure
            and last_price <= invalidation_level
        )
        or
        (
            direction == "NEUTRAL"
            and bearish_structure
            and last_price >= invalidation_level
        )
    )
    if scenario_invalidated:
        scenario_text = "مُلغي — يحتاج إعادة تقييم"

    # Confidence-aware wording: avoid sounding more certain than the score.
    confidence_value = float(setup.get("confidence", 0) or 0)
    if confidence_value >= 75:
        scenario_prefix = "السيناريو المرجح حاليًا:"
    elif confidence_value >= 60:
        scenario_prefix = "السيناريو المحتمل حاليًا:"
    else:
        scenario_prefix = "احتمال قائم:"

    # Scenario: estimate whether price is likely to retest the detected zone first.
    zone_mid = None
    retest_text = ""

    if scenario_invalidated:
        if direction == "BULLISH":
            retest_text = (
                f"السيناريو الصاعد السابق أُلغي بعد كسر مستوى الإبطال "
                f"${setup['invalidation']:.2f}. يلزم إعادة تقييم الشارت وبناء سيناريو جديد "
                "قبل اعتماد أي منطقة طلب أو أهداف صاعدة."
            )
        elif direction == "BEARISH":
            retest_text = (
                f"السيناريو الهابط السابق أُلغي بعد اختراق مستوى الإبطال "
                f"${setup['invalidation']:.2f}. يلزم إعادة تقييم الشارت وبناء سيناريو جديد "
                "قبل اعتماد أي منطقة عرض أو أهداف هابطة."
            )
        else:
            if bullish_structure:
                retest_text = (
                    f"السيناريو السابق أُلغي لأن السعر كسر مستوى الإبطال "
                    f"${setup['invalidation']:.2f}. يلزم إعادة تقييم الشارت قبل "
                    "اعتماد أي منطقة طلب أو أهداف صاعدة."
                )
            elif bearish_structure:
                retest_text = (
                    f"السيناريو السابق أُلغي لأن السعر اخترق مستوى الإبطال "
                    f"${setup['invalidation']:.2f}. يلزم إعادة تقييم الشارت قبل "
                    "اعتماد أي منطقة عرض أو أهداف هابطة."
                )
            else:
                retest_text = (
                    "السيناريو السابق لم يعد صالحًا ويحتاج إلى إعادة تقييم فني جديد."
                )

    elif zone_is_relevant and zone_label == "DEMAND":
        zone_mid = (zone_low + zone_high) / 2
        distance_to_zone_pct = ((last_price - zone_high) / last_price) * 100 if last_price else 0
        recent_pullback = closes[-1] < closes[-3] if len(closes) >= 3 else False
        stretched_from_zone = distance_to_zone_pct > 0.60

        if recent_pullback or stretched_from_zone:
            retest_text = (
                f"{scenario_prefix} احتمال إعادة اختبار منطقة الطلب "
                f"${zone_low:.2f}–${zone_high:.2f} قبل محاولة الصعود. "
                f"الثبات فوقها يدعم الارتداد، وكسر ${setup['invalidation']:.2f} يلغي السيناريو."
            )
        else:
            retest_text = (
                f"{scenario_prefix} استمرار الحركة دون اشتراط العودة لمنطقة الطلب "
                f"${zone_low:.2f}–${zone_high:.2f}. "
                f"أي هبوط إليها يبقى إعادة اختبار ما دام السعر فوق ${setup['invalidation']:.2f}."
            )

    elif zone_is_relevant and zone_label == "SUPPLY":
        zone_mid = (zone_low + zone_high) / 2
        distance_to_zone_pct = ((zone_low - last_price) / last_price) * 100 if last_price else 0
        recent_bounce = closes[-1] > closes[-3] if len(closes) >= 3 else False
        stretched_from_zone = distance_to_zone_pct > 0.60

        if recent_bounce or stretched_from_zone:
            retest_text = (
                f"{scenario_prefix} احتمال إعادة اختبار منطقة العرض "
                f"${zone_low:.2f}–${zone_high:.2f} قبل محاولة الهبوط. "
                f"الرفض منها يدعم التراجع، واختراق ${setup['invalidation']:.2f} يلغي السيناريو."
            )
        else:
            retest_text = (
                f"{scenario_prefix} استمرار الضعف دون اشتراط العودة لمنطقة العرض "
                f"${zone_low:.2f}–${zone_high:.2f}. "
                f"أي صعود إليها يبقى إعادة اختبار ما دام السعر دون ${setup['invalidation']:.2f}."
            )

    elif zone_beyond_invalidation:
        if direction == "BULLISH":
            retest_text = (
                f"{scenario_prefix} منطقة الطلب المكتشفة تقع أسفل مستوى الإبطال "
                f"${setup['invalidation']:.2f}، لذلك لا تُعد إعادة اختبار ضمن السيناريو الصاعد الحالي."
            )
        elif direction == "BEARISH":
            retest_text = (
                f"{scenario_prefix} منطقة العرض المكتشفة تقع أعلى مستوى الإبطال "
                f"${setup['invalidation']:.2f}، لذلك لا تُعد إعادة اختبار ضمن السيناريو الهابط الحالي."
            )
        else:
            retest_text = (
                f"{scenario_prefix} المنطقة المكتشفة خارج حدود السيناريو الحالي."
            )
    else:
        retest_text = (
            f"{scenario_prefix} لا توجد حاليًا منطقة طلب/عرض موثوقة بما يكفي "
            "لبناء سيناريو إعادة اختبار واضح."
        )

    targets_text = " → ".join(f"${value:.2f}" for value in targets)
    caption = (
        f"📊 {symbol} — {timeframe_ar}\n"
        f"السعر: ${last_price:.2f}\n"
        f"النموذج: {setup['name_ar']}\n"
        f"السيناريو: {scenario_text}\n"
        f"التأكيد: ${setup['trigger']:.2f}\n"
        f"الإبطال: ${setup['invalidation']:.2f}\n"
        f"الأهداف: {targets_text}\n"
        f"القوة الفنية: {'— (السيناريو مُبطل)' if scenario_invalidated else str(setup['confidence']) + '%'}\n"
        f"📍 {retest_text}\n\n"
        "⚠️ تحليل فني احتمالي، وليس توصية شراء أو بيع."
    )
    return image, caption

def chart_timeframe_menu():
    keyboard = [
        [
            InlineKeyboardButton("15 دقيقة", callback_data="chart_15"),
            InlineKeyboardButton("ساعة", callback_data="chart_60"),
        ],
        [
            InlineKeyboardButton("4 ساعات", callback_data="chart_240"),
            InlineKeyboardButton("يومي", callback_data="chart_D"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

# =========================================================
# NEWS - FINNHUB
# =========================================================

NEWS_MAX_AGE_HOURS = 24

NEWS_ALIASES = {
    "SPY": ["spy", "s&p 500", "s&p500", "sp 500"],
    "QQQ": ["qqq", "nasdaq 100", "nasdaq-100"],
    "IWM": ["iwm", "russell 2000"],
    "IBIT": ["ibit", "ishares bitcoin trust"],
    "TSLA": ["tsla", "tesla"],
    "NVDA": ["nvda", "nvidia"],
    "AMD": ["amd", "advanced micro devices"],
    "MRVL": ["mrvl", "marvell technology", "marvell"],
    "ARM": ["arm holdings"],
    "AVGO": ["avgo", "broadcom"],
    "MU": ["mu", "micron"],
    "GS": ["goldman sachs"],
    "META": ["meta platforms", "facebook"],
    "AAPL": ["aapl", "apple"],
    "GOOGL": ["googl", "google", "alphabet"],
    "MSFT": ["msft", "microsoft"],
    "AMZN": ["amzn", "amazon"],
    "SMCI": ["smci", "super micro computer", "supermicro"],
    "SNOW": ["snowflake"],
    "SHOP": ["shopify"],
    "BA": ["boeing"],
    "CRM": ["salesforce"],
    "CAT": ["caterpillar"],
    "PLTR": ["palantir"],
    "ORCL": ["oracle"],
    "OPEN": ["opendoor"],
    "MSTR": ["microstrategy"],
    "COIN": ["coinbase"],
    "SPCX": ["spacex"],
}

IMPORTANT_NEWS_RULES = [
    (10, ["bankruptcy", "chapter 11", "fraud", "sec charges", "trading halt",
          "fda approval", "fda rejects", "mass recall", "recalls nearly 3 million",
          "recalls 3 million", "recalls more than 2 million"], "حدث جوهري جدًا"),
    (9, ["acquisition", "merger", "buyout", "takeover", "earnings",
         "guidance", "profit warning", "ceo resigns", "ceo steps down"],
        "تطور جوهري"),
    (8, ["beats estimates", "misses estimates", "contract", "partnership",
         "recall", "downgrade", "upgrade", "layoffs", "job cuts",
         "price increase", "price increases", "price hike", "price hikes",
         "cost increase", "cost increases", "raises prices", "higher prices",
         "offering", "share sale", "capital raise", "buyback", "repurchase",
         "launch", "expands", "expansion", "approval", "invests", "investment",
         "investigation", "probe", "safety issue", "safety defect", "regulator",
         "regulatory action", "vehicle recall"],
        "خبر مهم للسهم"),
    (7, ["price target", "orders", "delivery", "deliveries", "production",
         "tariff", "export restriction", "regulatory", "antitrust"],
        "تطور يحتاج متابعة"),
]

ANALYSIS_ONLY_WORDS = [
    "technical analysis", "opinion", "why i think", "what's going on",
    "what is going on", "bullish outlier", "stock to watch",
    "rating upgrade", "rating downgrade", "rating up",
    "inventory does not lie", "too far too fast",
    "valuation", "fair value", "my price target",
    "seeking alpha"
]

POSITIVE_WORDS = [
    "beats estimates", "beat estimates", "beats expectations", "beat expectations",
    "raises guidance", "raised guidance", "raises outlook", "raised outlook",
    "upgraded", "upgrade", "fda approval", "approved", "approval",
    "record revenue", "record profit", "contract win", "wins contract",
    "awarded contract", "secures contract", "wins order", "secures order",
    "buyback", "repurchase", "strong demand", "expands partnership",
    "expands ties", "strategic partnership", "partners with", "gains after",
    "shares rise", "shares jump", "stock rises", "stock jumps",
    "permit approval", "license approval", "receives approval"
]

NEGATIVE_WORDS = [
    "misses estimates", "missed estimates", "misses expectations", "missed expectations",
    "cuts guidance", "cut guidance", "cuts outlook", "cut outlook", "downgraded",
    "downgrade", "investigation", "subpoena", "fraud", "recall", "recalling",
    "bankruptcy", "chapter 11", "layoffs", "job cuts", "share sale", "capital raise",
    "weak demand", "fda rejects", "rejects contract", "rejected contract",
    "authorizes strike", "authorized strike", "strike authorization", "may strike",
    "plans strike", "walkout", "scales back", "scale back", "cuts plans",
    "delays", "delayed", "suspends", "suspended", "halts", "halted",
    "safety defect", "safety issue", "regulator orders", "ordered to close",
    "price hike", "price hikes", "price increase", "price increases",
    "cost increase", "cost increases", "higher prices",
    "ordered shutdown", "lawsuit", "sues", "probe"
]


def _strip_html_text(value):
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return html.unescape(" ".join(text.split()))


def _parse_feed_datetime(value):
    value = str(value or "").strip()
    if not value:
        return 0
    try:
        return int(parsedate_to_datetime(value).timestamp())
    except Exception:
        pass
    try:
        cleaned = value.replace("Z", "+00:00")
        return int(datetime.fromisoformat(cleaned).timestamp())
    except Exception:
        return 0


def _rss_local_name(tag):
    return str(tag or "").split("}")[-1].lower()


def _child_text(node, names):
    wanted = {str(x).lower() for x in names}
    for child in list(node):
        if _rss_local_name(child.tag) in wanted:
            if child.text:
                return child.text.strip()
    return ""


def _child_link(node):
    # RSS <link>text</link> and Atom <link href="..."/> are both supported.
    for child in list(node):
        if _rss_local_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href") or "").strip()
        if href:
            return href
        if child.text:
            return child.text.strip()
    return ""


def _news_query_terms_for_symbol(symbol):
    symbol = str(symbol).upper().strip()
    terms = [symbol]
    for alias in NEWS_ALIASES.get(symbol, []):
        alias = str(alias or "").strip()
        if alias and alias.lower() not in {x.lower() for x in terms}:
            terms.append(alias)
    # Keep queries compact; one strong alias is enough alongside the ticker.
    return terms[:2]


def _build_google_news_query(symbols):
    terms = []
    for symbol in symbols:
        for term in _news_query_terms_for_symbol(symbol):
            # Quote company names with spaces; tickers remain bare.
            if " " in term:
                term = f'"{term}"'
            terms.append(term)
    return "(" + " OR ".join(terms) + ") when:1d"


def get_fast_backup_news():
    """Independent backup discovery layer via Google News RSS.

    Finnhub stays primary. This layer is intentionally source-agnostic and only
    discovers candidate headlines; the same strict company attribution and
    importance scoring are applied afterwards before anything can be shown or sent.
    """
    items = []
    headers = {
        "User-Agent": "Mozilla/5.0 (OptionBot News Monitor/2.0)",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }

    symbols = [s for s in SCAN_SYMBOLS if s not in {"SPY", "QQQ", "IWM"}]
    for i in range(0, len(symbols), FAST_NEWS_BATCH_SIZE):
        batch = symbols[i:i + FAST_NEWS_BATCH_SIZE]
        query = _build_google_news_query(batch)
        try:
            response = requests.get(
                FAST_NEWS_RSS_URL,
                headers=headers,
                params={
                    "q": query,
                    "hl": "en-US",
                    "gl": "US",
                    "ceid": "US:en",
                },
                timeout=FAST_NEWS_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)

            entries = [
                node for node in root.iter()
                if _rss_local_name(node.tag) in {"item", "entry"}
            ]

            for entry in entries[:60]:
                title = _strip_html_text(_child_text(entry, {"title"}))
                description = _strip_html_text(
                    _child_text(entry, {"description", "summary", "content"})
                )
                link = _child_link(entry)
                published = _child_text(entry, {"pubdate", "published", "updated", "date"})
                timestamp = _parse_feed_datetime(published)
                if not title or not timestamp:
                    continue

                source_name = _child_text(entry, {"source"}) or "Google News"
                # Google News titles often append " - Source". Keep the original
                # headline because our attribution filter works on the company name.
                items.append({
                    "id": f"gnews:{link or timestamp}:{title}",
                    "headline": title,
                    "summary": description,
                    "datetime": timestamp,
                    "source": source_name,
                    "url": link,
                    "trusted_fast_news": True,
                })
        except Exception as exc:
            print(
                f"FAST NEWS RSS ERROR batch={','.join(batch)}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    return items


def _material_query_for_symbol(symbol):
    """Focused discovery for high-impact company events missed by broad feeds."""
    aliases = _news_query_terms_for_symbol(symbol)
    company = aliases[1] if len(aliases) > 1 else aliases[0]
    if " " in company:
        company = f'"{company}"'
    material = (
        'recall OR recalled OR recalling OR investigation OR probe OR regulator OR '
        'lawsuit OR acquisition OR merger OR guidance OR "profit warning" OR '
        '"CEO resigns" OR "CEO steps down" OR "FDA approval" OR "FDA rejects" OR '
        '"price increase" OR "price increases" OR "price hike" OR "price hikes" OR '
        '"cost increase" OR "cost increases" OR "raises prices" OR "higher prices" OR '
        'strike OR layoffs OR "job cuts" OR contract OR partnership OR '
        '"launch event" OR "launch date" OR permit OR approval OR production OR deliveries'
    )
    return f'({symbol} OR {company}) ({material}) when:1d'


def get_material_backup_news():
    """Per-company high-impact Google News search; complements, not replaces, Finnhub."""
    items = []
    headers = {
        "User-Agent": "Mozilla/5.0 (OptionBot Material News Monitor/4.0)",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    symbols = [s for s in SCAN_SYMBOLS if s not in {"SPY", "QQQ", "IWM"}]
    for symbol in symbols:
        try:
            response = requests.get(
                FAST_NEWS_RSS_URL,
                headers=headers,
                params={"q": _material_query_for_symbol(symbol), "hl":"en-US", "gl":"US", "ceid":"US:en"},
                timeout=FAST_NEWS_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
            entries = [n for n in root.iter() if _rss_local_name(n.tag) in {"item","entry"}]
            for entry in entries[:20]:
                title = _strip_html_text(_child_text(entry, {"title"}))
                description = _strip_html_text(_child_text(entry, {"description","summary","content"}))
                link = _child_link(entry)
                published = _child_text(entry, {"pubdate","published","updated","date"})
                timestamp = _parse_feed_datetime(published)
                if not title or not timestamp:
                    continue
                items.append({
                    "id": f"material:{symbol}:{link or timestamp}:{title}",
                    "headline": title,
                    "summary": description,
                    "datetime": timestamp,
                    "source": _child_text(entry, {"source"}) or "Google News",
                    "url": link,
                    "trusted_fast_news": True,
                    "material_search_symbol": symbol,
                })
        except Exception as exc:
            print(f"MATERIAL NEWS RSS ERROR {symbol}: {type(exc).__name__}: {exc}", flush=True)
    return items


def get_priority_event_backup_news():
    """Additive focused search for a few high-impact events.

    IMPORTANT: this does not replace or modify Finnhub, fast backup, or the
    existing material-news search. It only adds extra candidates so the stable
    V25 discovery behavior remains intact.
    """
    items = []
    headers = {
        "User-Agent": "Mozilla/5.0 (OptionBot Priority Event Monitor/1.0)",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    searches = [
        (
            "TSLA",
            '(Tesla OR TSLA) (Cybercab OR robotaxi OR Optimus) '
            '("launch event" OR "launch date" OR "event date" OR invites OR '
            '"sending invitations" OR "confirms date" OR "official date" OR "scheduled for") when:1d'
        ),
        (
            "BA",
            '(Boeing OR BA) ("union rejects contract" OR "union rejects offer" OR '
            '"authorizes strike" OR "strike authorization" OR "vote to strike" OR '
            '"workers reject contract" OR "engineers reject contract") when:1d'
        ),
    ]

    for symbol, query in searches:
        try:
            response = requests.get(
                FAST_NEWS_RSS_URL,
                headers=headers,
                params={"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                timeout=FAST_NEWS_TIMEOUT_SECONDS,
                allow_redirects=True,
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
            entries = [
                n for n in root.iter()
                if _rss_local_name(n.tag) in {"item", "entry"}
            ]
            for entry in entries[:20]:
                title = _strip_html_text(_child_text(entry, {"title"}))
                description = _strip_html_text(
                    _child_text(entry, {"description", "summary", "content"})
                )
                link = _child_link(entry)
                published = _child_text(
                    entry, {"pubdate", "published", "updated", "date"}
                )
                timestamp = _parse_feed_datetime(published)
                if not title or not timestamp:
                    continue
                items.append({
                    "id": f"priority:{symbol}:{link or timestamp}:{title}",
                    "headline": title,
                    "summary": description,
                    "datetime": timestamp,
                    "source": _child_text(entry, {"source"}) or "Google News",
                    "url": link,
                    "trusted_fast_news": True,
                    "priority_search_symbol": symbol,
                })
        except Exception as exc:
            print(
                f"PRIORITY NEWS RSS ERROR {symbol}: {type(exc).__name__}: {exc}",
                flush=True,
            )
    return items

def _symbols_mentioned_in_news(headline, summary=""):
    text = _normalize_news_text(f"{headline} {summary}")
    found = []
    risky_short_tickers = {"CAT", "ARM", "BA", "MU", "OPEN", "GS", "CRM"}

    for symbol in SCAN_SYMBOLS:
        symbol = str(symbol).upper().strip()
        if symbol in {"SPY", "QQQ", "IWM"}:
            continue

        # First prefer a strong headline-level company attribution.
        if _strong_company_attribution(symbol, headline, summary):
            found.append(symbol)
            continue

        # For risky short/common tickers, NEVER resolve from summary-only bare ticker text.
        if symbol in risky_short_tickers:
            continue

        # Longer/less ambiguous symbols may still be resolved from curated post bodies.
        ticker_pattern = rf"(?<![a-z0-9]){re.escape(symbol.lower())}(?![a-z0-9])"
        if re.search(ticker_pattern, text):
            found.append(symbol)
            continue

        for alias in NEWS_ALIASES.get(symbol, []):
            alias_norm = _normalize_news_text(alias)
            if not alias_norm:
                continue
            if len(alias_norm) <= 4 and " " not in alias_norm:
                pattern = rf"(?<![a-z0-9]){re.escape(alias_norm)}(?![a-z0-9])"
                if re.search(pattern, text):
                    found.append(symbol)
                    break
            elif alias_norm in text:
                found.append(symbol)
                break
    return found


def get_company_news(symbol, days=NEWS_DAYS):
    if not FINNHUB_TOKEN:
        raise RuntimeError("FINNHUB_TOKEN is missing")

    today = datetime.now(ZoneInfo("America/New_York")).date()
    response = requests.get(
        "https://finnhub.io/api/v1/company-news",
        headers=get_finnhub_headers(),
        params={
            "symbol": symbol,
            "from": (today - timedelta(days=days)).isoformat(),
            "to": today.isoformat(),
        },
        timeout=20
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("استجابة الأخبار غير متوقعة.")
    return data


def _normalize_news_text(value):
    return " ".join(str(value or "").lower().replace("’", "'").split())


def _direct_match(symbol, item, combined):
    symbol = str(symbol).upper().strip()
    related = str(item.get("related") or item.get("symbol") or "").upper()
    related_symbols = {x for x in re.split(r"[,;\s]+", related) if x}
    if symbol in related_symbols:
        return True
    return any(alias in combined for alias in NEWS_ALIASES.get(symbol, []))


def _strong_company_attribution(symbol, headline, summary=""):
    """Return True only when the story is clearly about the tracked company.

    This is stricter than raw ticker-token matching and is used before automatic
    company-news alerts. Short/common tickers such as CAT, ARM, BA, MU and OPEN
    can appear as ordinary English words or abbreviations, so they require a
    strong company alias or explicit market/ticker context.
    """
    symbol = str(symbol).upper().strip()
    headline_raw = str(headline or "")
    summary_raw = str(summary or "")
    headline_norm = _normalize_news_text(headline_raw)
    combined = _normalize_news_text(f"{headline_raw} {summary_raw}")

    # A full company alias in the headline is always strong attribution.
    aliases = NEWS_ALIASES.get(symbol, [])
    for alias in aliases:
        alias_norm = _normalize_news_text(alias)
        if not alias_norm:
            continue
        if len(alias_norm) <= 4 and " " not in alias_norm:
            pattern = rf"(?<![a-z0-9]){re.escape(alias_norm)}(?![a-z0-9])"
            if re.search(pattern, headline_norm):
                return True
        elif alias_norm in headline_norm:
            return True

    # Short/common tickers are too risky as bare words.
    # V32: for these symbols, require the REAL company name/alias in the headline.
    risky_short_tickers = {"CAT", "ARM", "BA", "MU", "OPEN", "GS", "CRM"}
    ticker_pattern = rf"(?<![a-z0-9]){re.escape(symbol.lower())}(?![a-z0-9])"
    ticker_in_headline = bool(re.search(ticker_pattern, headline_norm))

    if symbol in risky_short_tickers:
        return False

    # Less ambiguous tickers can still be accepted as standalone headline tokens.
    if ticker_in_headline:
        return True

    return False


def _headline_mentions_symbol(symbol, headline):
    """Require the company/ticker itself to be part of the headline.

    Finnhub may tag a company merely because it is mentioned in an article body/summary.
    That caused unrelated stories (for example a Walmart story mentioning Amazon) to
    appear under another ticker.  The stock-news section is intentionally stricter.
    """
    symbol = str(symbol).upper().strip()
    headline_norm = _normalize_news_text(headline)

    # Reject headlines where our ticker/company is only a catalyst or secondary mention
    # for another named subject, e.g. "Sandisk's Nvidia Catalyst ..." under NVDA.
    # In possessive headlines, the owner before 's is the primary subject.
    possessive = re.match(r"^(.+?)['’]s\s+", str(headline or "").strip(), re.I)
    if possessive:
        owner = _normalize_news_text(possessive.group(1))
        target_names = [_normalize_news_text(symbol)] + [
            _normalize_news_text(x) for x in NEWS_ALIASES.get(symbol, [])
        ]
        if not any(name and (owner == name or name in owner) for name in target_names):
            return False

    aliases = NEWS_ALIASES.get(symbol, [])
    # Match ticker as a token as well as the configured company aliases.
    ticker_pattern = rf"(?<![a-z0-9]){re.escape(symbol.lower())}(?![a-z0-9])"
    if re.search(ticker_pattern, headline_norm):
        return True

    for alias in aliases:
        alias_norm = _normalize_news_text(alias)
        if not alias_norm:
            continue
        # Short aliases need token boundaries; longer company names can use phrase match.
        if len(alias_norm) <= 4 and " " not in alias_norm:
            pattern = rf"(?<![a-z0-9]){re.escape(alias_norm)}(?![a-z0-9])"
            if re.search(pattern, headline_norm):
                return True
        elif alias_norm in headline_norm:
            return True
    return False

def _looks_like_commentary_or_preview(headline_norm):
    """Reject opinion, previews, valuation pieces and speculative headlines."""
    markers = [
        "technical analysis", "opinion", "why i think", "what's going on",
        "what is going on", "stock to watch", "strong buy", "strong sell",
        "buy rating", "sell rating", "earnings preview", "preview:",
        "ahead of earnings", "ahead of its earnings", "before earnings",
        "reports earnings on", "reports earnings this", "earnings on wednesday",
        "earnings on thursday", "earnings on friday", "expected move",
        "how much the stock could move", "how much stock could move",
        "what options imply", "one number", "valuation", "demonstrates high-growth",
        "looks overdone", "likely game changer", "is a game changer",
        "my top", "top pick", "why i'm buying", "why i am buying",
        "why i'm selling", "why i am selling", "could be", "may be a",
    ]
    return any(x in headline_norm for x in markers)

def _looks_like_rehash_or_non_event(headline_norm):
    """Reject fresh articles that merely repackage an older/anticipated story."""
    markers = [
        "ready to launch", "set to launch", "prepares to launch",
        "preparing to launch", "gears up to launch", "poised to launch",
        "what we know", "everything we know", "all you need to know",
        "what investors should know", "what investors need to know",
        "here's what to know", "here is what to know",
        "a look at", "deep dive", "explained:", "explainer:",
        "why this matters", "what it means for investors",
    ]
    return any(x in headline_norm for x in markers)

def score_news_item(symbol, item):
    headline = str(item.get("headline") or "").strip()
    summary = str(item.get("summary") or "").strip()
    timestamp = int(item.get("datetime") or 0)

    if not headline or not timestamp:
        return None

    age = time.time() - timestamp
    if age < -300 or age > NEWS_MAX_AGE_HOURS * 3600:
        return None

    headline_norm = _normalize_news_text(headline)
    combined = _normalize_news_text(f"{headline} {summary}")

    # V32 PRIMARY SUBJECT GUARD:
    # If another tracked company's real name appears earlier in the headline,
    # do not relabel that story under a later secondary company mention.
    headline_subject_positions = []
    for other_symbol, other_aliases in NEWS_ALIASES.items():
        if other_symbol in {"SPY", "QQQ", "IWM"}:
            continue
        candidates = []
        for alias in other_aliases:
            alias_norm = _normalize_news_text(alias)
            if not alias_norm or alias_norm == other_symbol.lower():
                continue
            pos = headline_norm.find(alias_norm)
            if pos >= 0:
                candidates.append(pos)
        if candidates:
            headline_subject_positions.append((min(candidates), other_symbol))

    if headline_subject_positions:
        headline_subject_positions.sort(key=lambda x: x[0])
        first_subject_symbol = headline_subject_positions[0][1]
        if first_subject_symbol != str(symbol).upper().strip():
            print(
                f"NEWS DROP SECONDARY COMPANY {symbol}: primary={first_subject_symbol} "
                f"{headline[:140]}",
                flush=True,
            )
            return None

    # V30 strict company verification gate:
    # automatic company news must first be clearly attributable to the tracked company.
    # This blocks false matches such as CAT <- "cats", ARM <- "arm", OPEN <- "open", etc.
    if not _strong_company_attribution(symbol, headline, summary):
        print(
            f"NEWS DROP WEAK COMPANY ATTRIBUTION {symbol}: {headline[:140]}",
            flush=True,
        )
        return None

    # Prefer the company/ticker in the headline. If Finnhub explicitly tags the
    # symbol, allow it only when the headline itself contains a material trigger.
    headline_subject_match = _headline_mentions_symbol(symbol, headline)

    # STRICT attribution:
    # Finnhub's related-symbol tag alone is not enough because an article can merely
    # mention a company in the body. The company/ticker must be a headline subject.
    # This prevents an SK Hynix story from being pushed as an NVDA announcement.
    if not headline_subject_match:
        if item.get("trusted_fast_news") and symbol in _symbols_mentioned_in_news(headline, summary):
            headline_subject_match = True
        else:
            return None

    # Exclude broad-market ETFs from company-news. Their stories belong in US market news.
    if str(symbol).upper().strip() in {"SPY", "QQQ", "IWM"}:
        return None

    # V21 CONTEXT GUARD:
    # Do not turn a CEO/founder macro story into company-specific stock news merely
    # because the headline identifies the person as "Tesla CEO", "SpaceX CEO", etc.
    # Example: Elon Musk warning about U.S. debt / a "$40 trillion bankruptcy
    # nightmare" is a macro/Bitcoin story, not a Tesla or SpaceX bankruptcy event.
    macro_context_markers = [
        "u.s. debt", "us debt", "national debt", "government debt",
        "federal debt", "sovereign debt", "debt ceiling", "fiscal deficit",
        "budget deficit", "40 trillion", "$40 trillion", "trillion debt",
        "dollar collapse", "dollar crisis", "bitcoin price", "bitcoin soars",
        "bitcoin surge", "crypto market",
    ]
    executive_context_markers = [
        "elon musk", "ceo", "chief executive", "founder", "co-founder",
        "chairman", "billionaire",
    ]
    direct_company_event_markers = [
        "tesla files", "tesla filed", "tesla filing", "tesla seeks",
        "tesla bankruptcy", "tesla chapter 11",
        "spacex files", "spacex filed", "spacex filing", "spacex seeks",
        "spacex bankruptcy", "spacex chapter 11",
        "company files", "company filed", "company filing for bankruptcy",
    ]

    if (
        any(x in combined for x in macro_context_markers)
        and any(x in combined for x in executive_context_markers)
        and not any(x in combined for x in direct_company_event_markers)
    ):
        print(
            f"NEWS DROP CEO/MACRO NOT COMPANY EVENT {symbol}: {headline[:140]}",
            flush=True,
        )
        return None

    # Opinion / preview / valuation pieces are not treated as material company news.
    if _looks_like_commentary_or_preview(headline_norm):
        return None
    if _looks_like_rehash_or_non_event(headline_norm):
        return None
    if "?" in headline or any(x in headline_norm for x in ["is it true", "what to know", "here's what", "here is what"]):
        return None
    if any(x in headline_norm for x in ANALYSIS_ONLY_WORDS):
        return None

    # Analyst-note hygiene:
    # A repeated/maintained rating or unchanged target is NOT treated as new material news.
    analyst_repeat_words = [
        "reiterates", "reiterated", "maintains", "maintained",
        "keeps", "kept", "repeats", "repeated"
    ]
    analyst_real_change_words = [
        "upgrades", "upgraded", "downgrades", "downgraded",
        "raises price target", "raised price target",
        "price target raised", "lifts price target", "lifted price target",
        "lowers price target", "lowered price target",
        "price target lowered", "cuts price target", "cut price target"
    ]

    if (
        any(x in headline_norm for x in analyst_repeat_words)
        and not any(x in headline_norm for x in analyst_real_change_words)
    ):
        return None

    # Speculative M&A wording is not an actual acquisition/merger event.
    speculative_words = [
        "potential", "possible", "could", "might", "may ", "rumor", "rumour",
        "reportedly considering", "explores", "exploring", "interest in"
    ]
    ma_words = ["acquisition", "merger", "buyout", "takeover", "acquire", "acquires", "acquired"]
    if any(x in headline_norm for x in ma_words) and any(x in headline_norm for x in speculative_words):
        return None

    # Importance is driven by the headline itself. The summary is only contextual.
    importance, reason = 0, ""
    for score, words, label in IMPORTANT_NEWS_RULES:
        if any(word in headline_norm for word in words):
            importance, reason = score, label
            break

    # Context guard for alarming words such as "bankruptcy".
    # A headline can mention Tesla/its CEO while the bankruptcy subject is actually
    # the U.S. government, sovereign debt, another company, or a quoted macro warning.
    # Only treat bankruptcy as a 10/10 company event when the filing/distress is
    # explicitly attributed to the tracked company/ticker itself.
    if "bankruptcy" in headline_norm or "chapter 11" in headline_norm:
        company_terms = [str(symbol).lower()] + [
            _normalize_news_text(alias) for alias in NEWS_ALIASES.get(symbol, [])
        ]
        direct_bankruptcy = False
        for company_term in company_terms:
            if not company_term:
                continue
            patterns = [
                rf"\b{re.escape(company_term)}\b[^\n]{{0,55}}\b(?:files|filed|filing|seeks|seeking|enters|entered|declares|declared)\b[^\n]{{0,30}}\b(?:bankruptcy|chapter 11)\b",
                rf"\b(?:bankruptcy|chapter 11)\b[^\n]{{0,55}}\b{re.escape(company_term)}\b",
            ]
            if any(re.search(p, combined, re.I) for p in patterns):
                direct_bankruptcy = True
                break

        if not direct_bankruptcy:
            # Remove the false 10/10 trigger and let any OTHER genuine company
            # event in the headline determine importance. If none exists, drop it.
            importance, reason = 0, ""
            for score, words, label in IMPORTANT_NEWS_RULES:
                safe_words = [w for w in words if w not in {"bankruptcy", "chapter 11"}]
                if any(word in headline_norm for word in safe_words):
                    importance, reason = score, label
                    break
            print(f"NEWS CONTEXT GUARD bankruptcy not direct to {symbol}: {headline[:140]}", flush=True)

    # More precise triggers for actual reported financial results.
    result_markers = [
        "reports q", "reported q", "quarterly results", "reports earnings",
        "reported earnings", "earnings results", "beats estimates", "misses estimates",
        "beats expectations", "misses expectations", "raises guidance", "cuts guidance",
        "reaffirms guidance", "lowers guidance", "record revenue"
    ]
    if any(x in headline_norm for x in result_markers):
        importance, reason = max(importance, 9), "نتائج أو توقعات مالية"

    # Plain "earnings" in a headline can still be a preview/commentary. It is not enough alone.
    if "earnings" in headline_norm and not any(x in headline_norm for x in result_markers):
        if importance == 9 and reason == "تطور جوهري":
            importance = 0
            reason = ""

    # Actual commercial orders/contracts/deployments are material when the
    # headline describes a completed announcement rather than a preview.
    commercial_event_markers = [
        "orders ", "ordered ", "order for ", "secures order", "wins order",
        "awarded contract", "wins contract", "signs contract", "signs deal",
        "strikes deal", "partners with", "partnership with", "to deploy ",
        "will deploy ", "agrees to buy", "agreed to buy", "to acquire ",
    ]
    if any(x in headline_norm for x in commercial_event_markers):
        importance, reason = max(importance, 9), "صفقة أو عقد تجاري مؤثر"

    # V25: Major company launch/event announcements are material.
    # Keep this narrow: only high-impact products/platforms plus explicit
    # launch/event/invitation/date-confirmation language.
    major_launch_products = [
        "cybercab", "robotaxi", "optimus", "starship",
    ]
    major_launch_event_markers = [
        "launch event", "launch date", "event date",
        "sends out invites", "sending invitations", "begun sending invitations",
        "event invitations", "invitation-only event",
        "confirms date", "official date", "scheduled for",
        "will hold", "to hold", "unveiling event",
    ]
    if (
        any(x in combined for x in major_launch_products)
        and any(x in combined for x in major_launch_event_markers)
    ):
        importance, reason = max(importance, 9), "إعلان موعد إطلاق أو حدث جوهري"

    # V24: Boeing labor/strike events are material company news.
    if str(symbol).upper().strip() == "BA":
        boeing_labor_markers = [
            "authorizes strike", "authorize strike", "strike authorization",
            "union rejects contract", "union rejects offer",
            "workers reject contract", "engineers reject contract",
            "technical workers reject", "vote to strike",
        ]
        if any(x in combined for x in boeing_labor_markers):
            importance, reason = max(importance, 8), "نزاع عمالي أو إضراب مؤثر"

    # Large-scale recalls are always material even when fast-news sources use
    # wording such as "recalling around 3 million" instead of "recalls 3 million".
    recall_scale_patterns = [
        r"\brecall(?:s|ed|ing)?\b[^\n]{0,100}\b(?:[1-9](?:\.\d+)?\s*million|[1-9]\d{0,2}(?:,\d{3}){2})\b",
        r"\b(?:[1-9](?:\.\d+)?\s*million|[1-9]\d{0,2}(?:,\d{3}){2})\b[^\n]{0,100}\bvehicle(?:s)?\b[^\n]{0,80}\brecall",
    ]
    if any(re.search(pattern, combined, re.I) for pattern in recall_scale_patterns):
        importance, reason = max(importance, 10), "استدعاء واسع النطاق"

    # If it has no material-news trigger, do not show it.
    if importance < 7:
        return None

    # Keep the original sentiment decision exactly as-is.
    # Important: do NOT force a neutral story into positive/negative just to hide neutrals.
    # We first classify naturally, then discard the story only if it is truly neutral/unclear.
    pos = sum(1 for x in POSITIVE_WORDS if x in combined)
    neg = sum(1 for x in NEGATIVE_WORDS if x in combined)
    if pos >= 1 and pos > neg:
        sentiment, sentiment_ar = "POSITIVE", "🟢 إيجابي"
    elif neg >= 1 and neg > pos:
        sentiment, sentiment_ar = "NEGATIVE", "🔴 سلبي"
    else:
        print(f"NEWS DROP TRUE NEUTRAL {symbol} {headline[:140]}", flush=True)
        return None

    news_id = str(item.get("id") or item.get("url") or f"{symbol}:{timestamp}:{headline}")
    return {
        "id": news_id,
        "symbol": symbol,
        "headline": headline,
        "summary": summary,
        "source": str(item.get("source") or "").strip(),
        "url": str(item.get("url") or "").strip(),
        "timestamp": timestamp,
        "importance": importance,
        "reason_ar": reason,
        "category_ar": reason,
        "sentiment": sentiment,
        "sentiment_ar": sentiment_ar,
        "trusted_fast_news": bool(item.get("trusted_fast_news")),
    }



_NEWS_DEDUP_STOPWORDS = {
    "the","a","an","and","or","of","to","for","in","on","at","with","from","by",
    "as","is","are","was","were","be","been","being","this","that","these","those",
    "its","their","his","her","after","before","amid","over","under","about","around",
    "says","said","report","reports","reported","new","latest","update","updates",
    "stock","shares","company","market","today","now","just","breaking",
}

def _news_event_tokens(item):
    """Compact semantic token set for cross-source duplicate detection."""
    symbol = str(item.get("symbol") or "").upper().strip()
    text = _normalize_news_text(
        f"{item.get('headline','')} {item.get('reason_ar','')} {item.get('category_ar','')}"
    )
    # Drop ticker/company aliases so similarity focuses on the event itself.
    text = re.sub(rf"(?<![a-z0-9]){re.escape(symbol.lower())}(?![a-z0-9])", " ", text)
    for alias in NEWS_ALIASES.get(symbol, []):
        alias_norm = _normalize_news_text(alias)
        if alias_norm:
            text = text.replace(alias_norm, " ")
    raw = re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text)
    tokens = {
        t for t in raw
        if len(t) >= 3 and t not in _NEWS_DEDUP_STOPWORDS
    }
    return tokens

def _news_event_is_duplicate(item, now_ts=None):
    """True when the same material event was already pushed from another source.

    We compare only the same symbol/category and only against the last 96 hours.
    A materially different development should have a low token overlap and pass.
    """
    now_ts = float(now_ts or time.time())
    symbol = str(item.get("symbol") or "").upper().strip()
    category = str(item.get("category_ar") or item.get("reason_ar") or "").strip()
    current_tokens = _news_event_tokens(item)
    current_sentiment = str(item.get("sentiment") or "").upper()
    if not symbol or len(current_tokens) < 3:
        return False

    kept = []
    duplicate = False
    for old in SENT_NEWS_EVENTS:
        try:
            ts = float(old.get("timestamp") or 0)
            if now_ts - ts > 96 * 3600:
                continue
            kept.append(old)
            if str(old.get("symbol") or "").upper() != symbol:
                continue
            old_category = str(old.get("category") or "")
            old_sentiment = str(old.get("sentiment") or "").upper()
            if current_sentiment and old_sentiment and current_sentiment != old_sentiment:
                continue
            if category and old_category and category != old_category:
                continue
            old_tokens = set(old.get("tokens") or [])
            if len(old_tokens) < 3:
                continue
            inter = len(current_tokens & old_tokens)
            union = len(current_tokens | old_tokens)
            jaccard = inter / union if union else 0.0
            containment = inter / min(len(current_tokens), len(old_tokens))
            if inter >= 3 and (jaccard >= 0.55 or containment >= 0.80):
                duplicate = True
                print(
                    "NEWS DUPLICATE EVENT:",
                    symbol,
                    f"jaccard={jaccard:.2f}",
                    f"containment={containment:.2f}",
                    item.get("headline","")[:120],
                    flush=True,
                )
                break
        except Exception:
            continue

    # Opportunistic pruning keeps persistent state compact.
    SENT_NEWS_EVENTS[:] = kept[-300:]
    return duplicate

def _remember_news_event(item, sent_ts=None):
    sent_ts = float(sent_ts or time.time())
    tokens = sorted(_news_event_tokens(item))
    if not tokens:
        return
    SENT_NEWS_EVENTS.append({
        "timestamp": sent_ts,
        "news_timestamp": float(item.get("timestamp") or sent_ts),
        "symbol": str(item.get("symbol") or "").upper().strip(),
        "category": str(item.get("category_ar") or item.get("reason_ar") or "").strip(),
        "reason_ar": str(item.get("reason_ar") or item.get("category_ar") or "").strip(),
        "sentiment": str(item.get("sentiment") or "").upper(),
        "sentiment_ar": str(item.get("sentiment_ar") or "").strip(),
        "importance": int(item.get("importance") or 0),
        "tokens": tokens,
        "headline": str(item.get("headline") or "")[:220],
        "summary": str(item.get("summary") or "")[:500],
        "source": str(item.get("source") or "").strip(),
        "url": str(item.get("url") or "").strip(),
    })
    # Keep only recent records and cap size.
    cutoff = sent_ts - 96 * 3600
    SENT_NEWS_EVENTS[:] = [
        x for x in SENT_NEWS_EVENTS
        if float(x.get("timestamp") or 0) >= cutoff
    ][-300:]




_NEWS_SOURCE_PRIORITY = {
    "reuters": 100,
    "associated press": 98, "ap news": 98,
    "bloomberg": 96,
    "cnbc": 94,
    "wall street journal": 93, "wsj": 93,
    "new york times": 92, "the new york times": 92,
    "financial times": 91,
    "marketwatch": 88,
    "techcrunch": 86,
    "yahoo finance": 82,
    "wired": 80,
}

def _news_source_rank(item):
    source = _normalize_news_text(item.get("source") or "")
    for key, rank in _NEWS_SOURCE_PRIORITY.items():
        if key in source:
            return rank
    return 50

def _news_event_signature(item):
    """Broad event fingerprint for same-company stories across different headlines."""
    text = _normalize_news_text(f"{item.get('headline','')} {item.get('summary','')}")
    groups = [
        ("recall", ["recall", "recalls", "recalled", "recalling", "safety defect", "safety risk"]),
        ("labor_contract_strike", ["strike", "union rejects", "union rejected", "rejects contract", "rejected contract", "contract offer", "authorizes strike", "authorize strike"]),
        ("robotaxi_approval", ["robotaxi", "robot taxi", "permit", "approval", "approved", "license", "las vegas", "nevada"]),
        ("partnership_deal", ["partnership", "partners with", "strategic partnership", "collaboration", "deal with"]),
        ("launch_delay_cut", ["launch plans", "launch plan", "scales back", "cuts launch", "delays launch", "launch delay"]),
        ("layoffs", ["layoff", "layoffs", "job cuts", "cuts jobs", "workforce reduction"]),
    ]
    for name, words in groups:
        hits = sum(1 for w in words if w in text)
        if hits >= 1:
            return name
    return ""

def _same_news_event(a, b):
    """Conservative cross-source event duplicate check."""
    if str(a.get("symbol") or "").upper() != str(b.get("symbol") or "").upper():
        return False
    sa = str(a.get("sentiment") or "").upper()
    sb = str(b.get("sentiment") or "").upper()
    if sa and sb and sa != sb:
        return False
    ta, tb = float(a.get("timestamp") or 0), float(b.get("timestamp") or 0)
    if abs(ta - tb) > 24 * 3600:
        return False
    sig_a, sig_b = _news_event_signature(a), _news_event_signature(b)
    if sig_a and sig_b and sig_a == sig_b:
        return True
    toks_a, toks_b = _news_event_tokens(a), _news_event_tokens(b)
    if len(toks_a) < 3 or len(toks_b) < 3:
        return False
    inter = len(toks_a & toks_b)
    union = len(toks_a | toks_b)
    jaccard = inter / union if union else 0.0
    containment = inter / min(len(toks_a), len(toks_b))
    return inter >= 3 and (jaccard >= 0.52 or containment >= 0.78)


def _dedupe_news_results(items):
    """Keep one headline per real event, preferring the strongest source."""
    chosen = []
    for item in sorted(items, key=lambda x: (-x.get("importance",0), -x.get("timestamp",0))):
        dup_idx = None
        for i, old in enumerate(chosen):
            if _same_news_event(item, old):
                dup_idx = i
                break
        if dup_idx is None:
            chosen.append(item)
            continue
        old = chosen[dup_idx]
        new_key = (_news_source_rank(item), item.get("importance",0), item.get("timestamp",0))
        old_key = (_news_source_rank(old), old.get("importance",0), old.get("timestamp",0))
        if new_key > old_key:
            print("NEWS EVENT REPLACE:", old.get("symbol"), old.get("source"), "->", item.get("source"), item.get("headline","")[:110], flush=True)
            chosen[dup_idx] = item
        else:
            print("NEWS EVENT DEDUPE:", item.get("symbol"), item.get("source"), item.get("headline","")[:110], flush=True)
    return chosen

def scan_important_news():
    now = time.time()
    if NEWS_CACHE["results"] is not None and now - NEWS_CACHE["time"] < NEWS_CACHE_SECONDS:
        return NEWS_CACHE["results"]

    results, seen = [], set()
    for symbol in SCAN_SYMBOLS:
        try:
            for item in get_company_news(symbol, NEWS_DAYS):
                scored = score_news_item(symbol, item)
                if not scored:
                    continue
                key = (scored.get("url") or scored["headline"]).lower().strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                results.append(scored)
        except Exception as exc:
            print(f"NEWS ERROR {symbol}: {exc}")

    # Independent backup layer. This catches material headlines that Finnhub may
    # miss, but reuses the exact same strict relevance/importance rules.
    try:
        for item in get_fast_backup_news():
            for symbol in _symbols_mentioned_in_news(
                item.get("headline"),
                item.get("summary"),
            ):
                scored = score_news_item(symbol, item)
                if not scored:
                    continue
                key = (scored.get("url") or scored["headline"]).lower().strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                results.append(scored)
    except Exception as exc:
        print(f"FAST NEWS SCAN ERROR: {type(exc).__name__}: {exc}", flush=True)

    # Focused second pass for genuinely material events (recalls/regulatory/M&A/etc.).
    # This is what prevents a TSLA-scale recall from being buried by generic 8/10 stories.
    try:
        for item in get_material_backup_news():
            symbol = str(item.get("material_search_symbol") or "").upper()
            if not symbol:
                continue
            scored = score_news_item(symbol, item)
            if not scored:
                continue
            key = (scored.get("url") or scored["headline"]).lower().strip()
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(scored)
    except Exception as exc:
        print(f"MATERIAL NEWS SCAN ERROR: {type(exc).__name__}: {exc}", flush=True)

    # V27 additive priority layer: keeps V25 feeds unchanged and only adds
    # high-impact Cybercab/Robotaxi/Optimus and Boeing labor candidates.
    try:
        for item in get_priority_event_backup_news():
            symbol = str(item.get("priority_search_symbol") or "").upper()
            if not symbol:
                continue
            scored = score_news_item(symbol, item)
            if not scored:
                continue
            key = (scored.get("url") or scored["headline"]).lower().strip()
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(scored)
    except Exception as exc:
        print(
            f"PRIORITY NEWS SCAN ERROR: {type(exc).__name__}: {exc}",
            flush=True,
        )

    # One real-world event = one headline, even when many outlets publish it.
    # Prefer Reuters/AP/CNBC/WSJ/etc. over weaker duplicate sources.
    results = _dedupe_news_results(results)
    results.sort(key=lambda x: (-x["importance"], -x["timestamp"]))
    # Do NOT truncate here. The manual button applies its limit only after
    # today's-date filtering; the auto monitor needs the full deduped set.
    # V39: persist only fully verified/deduped stories so they remain visible
    # for the rest of the Saudi day if a live feed stops returning them later.
    results = _remember_verified_daily_news(results)

    NEWS_CACHE["time"] = now
    NEWS_CACHE["results"] = results
    return results


def news_time_text(timestamp):
    try:
        return datetime.fromtimestamp(
            timestamp, ZoneInfo("Asia/Riyadh")
        ).strftime("%d/%m %I:%M %p")
    except Exception:
        return "غير متوفر"


_HEADLINE_AR_CACHE = {}

def _translate_headline_ar_google(headline):
    text = str(headline or "").strip()
    if not text:
        return text
    if text in _HEADLINE_AR_CACHE:
        return _HEADLINE_AR_CACHE[text]

    # Curated newsroom wording for recurring finance headline patterns.
    # This only changes display text; scoring/filtering still uses the original English headline.
    exact_newsroom = {
        "Sandisk's Nvidia Catalyst Just Emerged (Rating Upgrade)":
            "سانديسك تحصل على ترقية للتصنيف بدعم من محفز مرتبط بـNvidia",
        "Microsoft: Price Has Run Up Too Fast (Rating Downgrade)":
            "خفض تصنيف مايكروسوفت بعد الارتفاع السريع في السهم",

        # V22: guaranteed Arabic display for current material headlines.
        # These are display-only mappings; scoring/filtering still uses original English.
        "Nvidia partners with data center developer Cloverleaf - TechCrunch":
            "إنفيديا تعقد شراكة مع مطور مراكز البيانات Cloverleaf - TechCrunch",
        "Apple Reportedly Shifts Focus To AI And Smart Glasses With Job Cuts In Siri And Vision Pro Teams":
            "آبل تعيد توجيه تركيزها نحو الذكاء الاصطناعي والنظارات الذكية مع خفض وظائف في فريقي Siri وVision Pro",
        "Boeing union rejects contract, authorizes strike - FOX 13 Seattle":
            "نقابة بوينغ ترفض العقد وتجيز الإضراب - FOX 13 Seattle",
        "Tesla’s Door Handles Lead to Its Biggest Recall Yet - WIRED":
            "مقابض أبواب تسلا تقود إلى أكبر حملة استدعاء للشركة حتى الآن - WIRED",
        "Tesla's Door Handles Lead to Its Biggest Recall Yet - WIRED":
            "مقابض أبواب تسلا تقود إلى أكبر حملة استدعاء للشركة حتى الآن - WIRED",
        "SpaceX scales back plans for next Starship launch - SpaceNews":
            "سبيس إكس تقلّص خطط إطلاق Starship القادم - SpaceNews",
        "Tesla Recalls 3M Vehicles in China Over Door Handles - Briefs Finance":
            "تسلا تستدعي نحو 3 ملايين سيارة في الصين بسبب مقابض الأبواب - Briefs Finance",
        "Tesla China Recall Reaches 3 Million Vehicles Over Flush Door Handles and Driver Alertness - Briefs Finance":
            "استدعاء تسلا في الصين يصل إلى 3 ملايين مركبة بسبب مقابض الأبواب ومشكلة تنبيه السائق - Briefs Finance",
        "Hunt Valley Microsoft gaming employees outraged by mass layoffs - WMAR 2 News Baltimore":
            "موظفو ألعاب Microsoft في Hunt Valley غاضبون من عمليات التسريح الجماعي - WMAR 2 News Baltimore",
        "Tesla Confirms Date for Cybercab Event; Sends Out Invites":
            "تسلا تؤكد موعد حدث إطلاق Cybercab وترسل الدعوات",
        "Tesla Cybercab launch gets official date in Austin with dedicated event":
            "تسلا تحدد رسميًا موعد حدث إطلاق Cybercab في أوستن",
        "Tesla schedules Cybercab event for September 3":
            "تسلا تحدد 3 سبتمبر موعدًا لحدث إطلاق Cybercab",
    }
    if text in exact_newsroom:
        polished = exact_newsroom[text]
        _HEADLINE_AR_CACHE[text] = polished
        return polished

    # Normalize once before any pattern checks.
    # This must be defined before the Marvell/Microsoft rules below.
    _headline_l = text.lower()

    # Analyst target-change wording: show the actual NEW action,
    # not the repeated old rating.
    if (
        "marvell" in _headline_l
        and (
            "raises price target" in _headline_l
            or "price target raised" in _headline_l
            or "lifts price target" in _headline_l
        )
    ):
        target_match = re.search(
            r"(?:to|at)\s+\$?([0-9]+(?:\.[0-9]+)?)",
            text,
            re.I,
        )
        if target_match:
            polished = (
                "رفع السعر المستهدف لسهم Marvell إلى "
                f"${target_match.group(1)}"
            )
        else:
            polished = "رفع السعر المستهدف لسهم Marvell"
        _HEADLINE_AR_CACHE[text] = polished
        return polished

    # Robust newsroom wording for Microsoft downgrade headlines.
    # Handles minor punctuation/wording differences from the source title.
    if (
        "microsoft" in _headline_l
        and "rating downgrade" in _headline_l
        and (
            "run up too fast" in _headline_l
            or "ran up too fast" in _headline_l
            or "price has run up too fast" in _headline_l
        )
    ):
        polished = "خفض تصنيف مايكروسوفت بعد الارتفاع السريع في السهم"
        _HEADLINE_AR_CACHE[text] = polished
        return polished
    try:
        r = None
        last_translate_exc = None
        for _attempt in range(2):
            try:
                r = requests.get(
                    "https://translate.googleapis.com/translate_a/single",
                    params={
                        "client": "gtx",
                        "sl": "en",
                        "tl": "ar",
                        "dt": "t",
                        "q": text,
                    },
                    timeout=8,
                )
                r.raise_for_status()
                break
            except Exception as exc:
                last_translate_exc = exc
                if _attempt == 0:
                    time.sleep(0.8)
        if r is None or not r.ok:
            raise last_translate_exc or RuntimeError("headline translation failed")
        data = r.json()
        translated = "".join(
            part[0] for part in (data[0] or [])
            if isinstance(part, list) and part and part[0]
        ).strip()
        if translated:
            # Light Arabic newsroom-style cleanup only. Keep the working
            # translation path unchanged and avoid touching news filters/scoring.
            polish_rules = [
                ("ثلاثة أشياء يمكن أن تدفعه إلى أعلى", "وسط عوامل قد تدفعه لمزيد من الارتفاع"),
                ("يمكن أن تدفعه إلى أعلى", "قد تدفعه لمزيد من الارتفاع"),
                ("وصل عائد سندات الخزانة لأجل 30 عامًا إلى أعلى مستوى له منذ 19 عامًا.",
                 "عائد سندات الخزانة الأمريكية لأجل 30 عامًا يسجل أعلى مستوى منذ 19 عامًا،"),
                ("ما الذي يحدث مع سهم", "مستجدات سهم"),
                ("ما الذي يحدث مع أسهم", "مستجدات أسهم"),
                ("مايكروسوفت: ارتفع السعر بسرعة كبيرة جدًا (خفض التصنيف)",
                 "خفض تصنيف مايكروسوفت بعد الارتفاع السريع في السهم"),
            ]
            for awkward, natural in polish_rules:
                translated = translated.replace(awkward, natural)
            translated = " ".join(translated.split()).strip()
            translated = translated.replace("، وسط", "، وسط")
            _HEADLINE_AR_CACHE[text] = translated
            return translated
    except Exception as exc:
        print(f"HEADLINE TRANSLATION ERROR: {exc}")
    return text


# V37 translation protection:
# 1) persistent cache survives normal Render restarts,
# 2) throttle prevents burst 429s,
# 3) MyMemory is a full-headline fallback if Google returns 429/fails.
HEADLINE_TRANSLATION_CACHE_FILE = (
    os.environ.get("STATE_DIR", "/var/data").rstrip("/")
    + "/option_bot_headline_ar_cache.json"
)
_TRANSLATION_LAST_REQUEST_TS = 0.0
_TRANSLATION_MIN_GAP_SECONDS = 1.35


def _load_headline_translation_cache():
    try:
        if not os.path.exists(HEADLINE_TRANSLATION_CACHE_FILE):
            return {}
        with open(HEADLINE_TRANSLATION_CACHE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print("TRANSLATION CACHE LOAD ERROR:", repr(exc), flush=True)
        return {}


def _save_headline_translation_cache():
    try:
        folder = os.path.dirname(HEADLINE_TRANSLATION_CACHE_FILE)
        if folder:
            os.makedirs(folder, exist_ok=True)
        temp_path = HEADLINE_TRANSLATION_CACHE_FILE + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(_HEADLINE_AR_CACHE, handle, ensure_ascii=False)
        os.replace(temp_path, HEADLINE_TRANSLATION_CACHE_FILE)
    except Exception as exc:
        print("TRANSLATION CACHE SAVE ERROR:", repr(exc), flush=True)


def _looks_arabic_translation(value):
    value = str(value or "").strip()
    if not value:
        return False
    ar = len(re.findall(r"[\u0600-\u06FF]", value))
    latin = len(re.findall(r"[A-Za-z]", value))
    return ar >= 3 and ar >= max(3, latin // 3)


def _translate_headline_ar_mymemory(text):
    try:
        r = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text, "langpair": "en|ar"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        translated = str(
            (data.get("responseData") or {}).get("translatedText") or ""
        ).strip()
        translated = html.unescape(translated)
        translated = " ".join(translated.split()).strip()
        if _looks_arabic_translation(translated):
            return translated
    except Exception as exc:
        print("MYMEMORY TRANSLATION ERROR:", repr(exc), flush=True)
    return ""


# Load saved translations once at startup.
_HEADLINE_AR_CACHE.update(_load_headline_translation_cache())


def translate_headline_ar(headline):
    global _TRANSLATION_LAST_REQUEST_TS

    text = str(headline or "").strip()
    if not text:
        return text

    # Fast path: exact/newsroom/persistent cache.
    cached = _HEADLINE_AR_CACHE.get(text)
    if cached:
        return cached

    # Avoid hammering Google when several headlines are formatted in one button press.
    elapsed = time.time() - _TRANSLATION_LAST_REQUEST_TS
    if elapsed < _TRANSLATION_MIN_GAP_SECONDS:
        time.sleep(_TRANSLATION_MIN_GAP_SECONDS - elapsed)

    _TRANSLATION_LAST_REQUEST_TS = time.time()
    translated = _translate_headline_ar_google(text)

    if _looks_arabic_translation(translated):
        _HEADLINE_AR_CACHE[text] = translated
        _save_headline_translation_cache()
        return translated

    # Google 429 / transient failure: translate the SAME full headline with backup.
    backup = _translate_headline_ar_mymemory(text)
    if backup:
        _HEADLINE_AR_CACHE[text] = backup
        _save_headline_translation_cache()
        print("HEADLINE TRANSLATION FALLBACK OK:", text[:120], flush=True)
        return backup

    # V36 display policy remains: formatter will drop the item rather than invent text.
    return text

def _arabic_news_fallback(item, original_headline):
    symbol=str(item.get("symbol") or "").upper()
    text=_normalize_news_text(original_headline)
    company={"TSLA":"تسلا","MSFT":"مايكروسوفت","NVDA":"إنفيديا","AAPL":"آبل",
    "SPCX":"سبيس إكس","BA":"بوينغ","CAT":"كاتربيلر","AMD":"AMD","META":"ميتا",
    "AMZN":"أمازون","GOOGL":"ألفابت","ORCL":"أوراكل","CRM":"سيلزفورس",
    "AVGO":"برودكوم","MU":"مايكرون","PLTR":"بالانتير","COIN":"كوينبيس",
    "MSTR":"مايكروستراتيجي"}.get(symbol,symbol or "الشركة")
    if any(x in text for x in ["price hike", "price hikes", "price increase", "price increases",
                                  "raises prices", "higher prices", "cost increase", "cost increases"]):
        pct = re.search(r"(?:above|over|more than|up to)?\\s*([0-9]+(?:\\.[0-9]+)?)\\s*%", original_headline, re.I)
        if pct:
            return f"تطور في الأسعار أو التكاليف لدى {company} بنسبة تتجاوز/تصل إلى {pct.group(1)}%"
        return f"تطور مهم في أسعار أو تكاليف منتجات {company}"

    if "recall" in text or "recalled" in text or "recalling" in text:
        if "china" in text and ("3 million" in text or "3m" in text):
            return f"استدعاء {company} في الصين يصل إلى نحو 3 ملايين مركبة"
        return f"{company} تواجه استدعاءً للمنتجات أو المركبات"
    if any(x in text for x in ["authorizes strike","strike authorization","vote to strike",
        "union rejects contract","union rejects offer","workers reject contract","engineers reject contract"]):
        return f"موظفو {company} يرفضون عرض العقد ويمنحون النقابة تفويضًا للإضراب"
    if "layoff" in text or "job cuts" in text:
        return f"تسريحات وظيفية لدى {company} تثير ردود فعل بين الموظفين"
    if "cybercab" in text and any(x in text for x in ["invite","date","event","launch"]):
        return "تسلا تؤكد تفاصيل حدث Cybercab وترسل الدعوات"
    if "partnership" in text or "partners with" in text:
        return f"{company} تعلن شراكة جديدة ذات صلة بأعمالها"
    if "investigation" in text or "probe" in text:
        return f"تحقيق جديد يتعلق بـ{company}"
    if "downgrade" in text:
        return f"خفض التقييم أو النظرة الاستثمارية لسهم {company}"
    if "upgrade" in text:
        return f"رفع التقييم أو النظرة الاستثمارية لسهم {company}"
    reason=str(item.get("reason_ar") or item.get("category_ar") or "تطور مهم").strip()
    if reason.startswith("خبر "): reason=reason[4:].strip()
    return f"{reason} يتعلق بـ{company}"



def _clean_news_source_suffix(text, source):
    text = str(text or "").strip()
    source = str(source or "").strip()
    if source:
        text = re.sub(r"\s*[-–—]\s*" + re.escape(source) + r"\s*$", "", text, flags=re.I)
    return text.strip()


def build_arabic_news_summary(item):
    """Translate the real headline + RSS summary into a useful Arabic digest.
    No invented facts: only text supplied by the discovered news item is used.
    """
    source = str(item.get("source") or "").strip()
    headline_en = _clean_news_source_suffix(item.get("headline"), source)
    summary_en = _strip_html_text(item.get("summary") or "")
    summary_en = _clean_news_source_suffix(summary_en, source)

    # Some RSS summaries merely repeat the headline. Prefer detail only when it adds information.
    h_norm = _normalize_news_text(headline_en)
    s_norm = _normalize_news_text(summary_en)
    if not summary_en or s_norm == h_norm or (h_norm and s_norm.startswith(h_norm) and len(summary_en) < len(headline_en) + 40):
        summary_en = ""

    # Keep enough factual detail for numbers, products, dates and reasons, without creating huge Telegram cards.
    if len(summary_en) > 700:
        summary_en = summary_en[:697].rsplit(" ", 1)[0] + "..."

    headline_ar = translate_headline_ar(headline_en)
    if not _looks_arabic_translation(headline_ar):
        return None

    if summary_en:
        summary_ar = translate_headline_ar(summary_en)
        if _looks_arabic_translation(summary_ar):
            # Avoid repeating the same sentence after translation.
            if _normalize_news_text(summary_ar) != _normalize_news_text(headline_ar):
                text = f"{headline_ar}. {summary_ar}"
            else:
                text = headline_ar
        else:
            text = headline_ar
    else:
        text = headline_ar

    text = html.unescape(" ".join(str(text).split())).strip()
    # Source is rendered separately and must never be translated into phrases such as "كذبة موتلي".
    text = _clean_news_source_suffix(text, source)
    if len(text) > 900:
        text = text[:897].rsplit(" ", 1)[0] + "..."
    return text


def format_news_item(item):
    original_headline = str(item.get("headline") or "").strip()
    headline = build_arabic_news_summary(item)
    if not headline:
        print(
            f"NEWS DROP TRANSLATION FAILED {item.get('symbol')}: "
            f"{original_headline[:160]}",
            flush=True,
        )
        return None

    source = str(item.get("source") or "غير متوفر").strip()
    url = str(item.get("url") or "").strip()
    link_line = (
        f'🔗 <a href="{html.escape(url, quote=True)}">فتح الخبر من {html.escape(source)}</a>'
        if url else "🔗 رابط الخبر: غير متوفر"
    )
    return (
        f"📰 {html.escape(str(item['symbol']))}\n"
        f"{html.escape(str(item['sentiment_ar']))} | 🔥 الأهمية: {item['importance']}/10\n"
        f"📝 النوع: {html.escape(str(item['category_ar']))}\n"
        f"📌 {html.escape(headline)}\n"
        f"🕐 {news_time_text(item['timestamp'])}\n"
        f"🏷️ المصدر: {html.escape(source)}\n"
        f"{link_line}"
    )


def format_news_results(results):
    if not results:
        return (
            "📰 أهم الأخبار\n\n"
            f"✅ لا توجد أخبار جوهرية حديثة ومرتبطة مباشرة بأسهمنا خلال آخر {NEWS_MAX_AGE_HOURS} ساعة."
        )

    return (
        "📰 أهم أخبار أسهمنا\n"
        "✅ حديثة + مرتبطة مباشرة بالسهم + جوهرية فقط\n"
        "━━━━━━━━━━━━━━\n\n"
        + "\n\n━━━━━━━━━━━━━━\n\n".join(format_news_item(x) for x in results)
    )



# =========================================================
# US MARKET / MACRO NEWS - MANUAL ONLY
# =========================================================
MACRO_MAX_AGE_HOURS = 12
MACRO_RULES = [
    (10, ["federal reserve","fomc","interest rate decision","rate cut","rate hike","jerome powell"], "الفيدرالي والفائدة"),
    (10, ["consumer price index","core cpi"," cpi ","pce price index","pce inflation","producer price index"," ppi "], "التضخم"),
    (10, ["nonfarm payroll","non-farm payroll","jobs report","unemployment rate","jobless claims"], "الوظائف والبطالة"),
    (9, ["gross domestic product"," gdp "], "الناتج المحلي"),
    (8, ["retail sales"], "مبيعات التجزئة"),
    (8, ["treasury yield","treasury yields","10-year yield","2-year yield","bond yields"], "عوائد السندات"),
]
def get_us_market_news():
    if not FINNHUB_TOKEN: raise RuntimeError("FINNHUB_TOKEN is missing")
    r=requests.get("https://finnhub.io/api/v1/news",headers=get_finnhub_headers(),params={"category":"general"},timeout=20)
    r.raise_for_status()
    d=r.json()
    return d if isinstance(d,list) else []

def score_macro_news(item):
    h = str(item.get("headline") or "").strip()
    s = str(item.get("summary") or "").strip()
    ts = int(item.get("datetime") or 0)
    if not h or not ts:
        return None

    # Strict freshness: timestamp must truly be within the last 12 hours.
    now_ts = time.time()
    age_seconds = now_ts - ts
    if age_seconds < -300 or age_seconds > MACRO_MAX_AGE_HOURS * 3600:
        return None

    c = _normalize_news_text(f" {h} {s} ")

    # Reject clearly non-US macro stories.
    foreign_markers = [
        "china", "chinese", "beijing", "euro zone", "eurozone",
        "european central bank", " ecb ", "united kingdom", " u.k.",
        "britain", "british", "bank of england", " boe ",
        "japan", "bank of japan", " boj ", "india", "canada",
        "australia", "germany", "france"
    ]
    if any(x in c for x in foreign_markers):
        return None

    # A macro headline must explicitly establish US context.
    us_markers = [
        "u.s.", "u.s. ", "united states", "us economy", "american economy",
        "federal reserve", "fomc", "jerome powell", "fed chair",
        "u.s. treasury", "treasury yields", "wall street",
        "u.s. consumer", "u.s. retail", "u.s. jobs", "u.s. unemployment"
    ]
    if not any(x in c for x in us_markers):
        return None

    importance = 0
    category = ""
    for score, words, label in MACRO_RULES:
        if any(w in c for w in words):
            importance, category = score, label
            break

    if not importance:
        return None

    if any(x in c for x in ANALYSIS_ONLY_WORDS):
        return None

    return {
        "headline": h,
        "source": str(item.get("source") or "").strip(),
        "url": str(item.get("url") or "").strip(),
        "timestamp": ts,
        "importance": importance,
        "category_ar": category,
    }

def scan_us_market_news():
    found=[]; seen=set()
    try: items=get_us_market_news()
    except Exception as exc:
        print(f"US MARKET NEWS ERROR: {exc}"); return []
    for item in items:
        x=score_macro_news(item)
        if not x: continue
        k=(x["url"] or x["headline"]).lower().strip()
        if k in seen: continue
        seen.add(k); found.append(x)
    found.sort(key=lambda x:(-x["importance"],-x["timestamp"]))
    return found[:5]

def format_macro_item(x):
    headline_ar = translate_headline_ar(x['headline'])
    if len(headline_ar) > 220:
        headline_ar = headline_ar[:217] + "..."
    return (f"🇺🇸 {x['category_ar']}\n🔥 الأهمية: {x['importance']}/10\n"
            f"📌 {headline_ar}\n🕐 {news_time_text(x['timestamp'])}\n"
            f"🏷️ المصدر: {x['source'] or 'غير متوفر'}\n"
            f"🔗 رابط الخبر: {x.get('url') or 'غير متوفر'}")

def format_combined_news():
    upcoming_text = format_upcoming_us_events()
    macro_items=scan_us_market_news(); stock_items=scan_important_news()

    # Manual "أهم الأخبار" button: show today's Saudi-date stock headlines only.
    # Automatic alerts keep their independent 2h/4h freshness windows.
    today_ksa = datetime.now(ZoneInfo("Asia/Riyadh")).date()
    stock_items = [
        x for x in stock_items
        if datetime.fromtimestamp(float(x.get("timestamp") or 0), ZoneInfo("Asia/Riyadh")).date() == today_ksa
    ]

    # V32 manual-only news: show only the CURRENT verified scan.
    # Do not merge old auto-sent state, so previously bad CAT/SPCX items cannot reappear.

    stock_items.sort(key=lambda x: (-x.get("importance", 0), -x.get("timestamp", 0)))
    stock_items = stock_items[:MAX_NEWS_RESULTS]

    a=("🇺🇸 أهم أخبار السوق الأمريكي\n━━━━━━━━━━━━━━\n\n"+"\n\n━━━━━━━━━━━━━━\n\n".join(format_macro_item(x) for x in macro_items)
       if macro_items else f"🇺🇸 أخبار السوق الأمريكي\n\n✅ لا توجد أخبار اقتصادية أمريكية جوهرية مؤكدة وحديثة خلال آخر {MACRO_MAX_AGE_HOURS} ساعة.")
    formatted_stock_items = [y for x in stock_items if (y := format_news_item(x))]
    b=("🏢 أهم أخبار أسهمنا\n━━━━━━━━━━━━━━\n\n"+"\n\n━━━━━━━━━━━━━━\n\n".join(formatted_stock_items)
       if formatted_stock_items else f"🏢 أخبار أسهمنا\n\n✅ لا توجد أخبار جوهرية حديثة ومرتبطة مباشرة بأسهمنا خلال آخر {NEWS_MAX_AGE_HOURS} ساعة.")
    return upcoming_text+"\n\n━━━━━━━━━━━━━━\n\n"+a+"\n\n━━━━━━━━━━━━━━\n\n"+b


def split_telegram_text(text, limit=3800):
    """Split long Telegram text safely, preferring section/line boundaries."""
    text = str(text or "").strip()
    if not text:
        return []
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


# =========================================================
# UPCOMING US ECONOMIC EVENTS — LIVE OFFICIAL CALENDARS
# =========================================================

UPCOMING_EVENT_DAYS = 7
MAJOR_EVENT_LOOKAHEAD_DAYS = 60
ECON_CALENDAR_CACHE_SECONDS = 1800
ECON_CALENDAR_CACHE = {"time": 0, "events": None, "errors": []}

# Automatic reminders for major U.S. market events.
# Send at ~7 days, ~3 days, and again on the same calendar day (KSA).
# Reminder delivery is restricted to the noon hour in Saudi Arabia so it
# does not interrupt the main U.S. market session.
EVENT_REMINDER_HOUR_KSA = 12
EVENT_REMINDER_LOOP_SECONDS = 900
EVENT_REMINDER_STAGES = (
    ("WEEK", 7 * 24 * 3600),
    ("THREE_DAYS", 3 * 24 * 3600),
)
SENT_EVENT_REMINDERS = set(AUTO_SENT_STATE.get("event_keys") or [])

MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

BLS_RELEASES = [
    (
        "https://www.bls.gov/schedule/news_release/cpi.htm",
        "CPI — مؤشر أسعار المستهلك",
        "🔥🔥🔥",
    ),
    (
        "https://www.bls.gov/schedule/news_release/ppi.htm",
        "PPI — مؤشر أسعار المنتجين",
        "🔥🔥🔥",
    ),
    (
        "https://www.bls.gov/schedule/news_release/empsit.htm",
        "الوظائف والبطالة الأمريكية",
        "🔥🔥🔥",
    ),
    (
        "https://www.bls.gov/schedule/news_release/jolts.htm",
        "JOLTS — فرص العمل",
        "🔥🔥",
    ),
]

MAJOR_NAMES = {
    "CPI — مؤشر أسعار المستهلك",
    "PPI — مؤشر أسعار المنتجين",
    "الوظائف والبطالة الأمريكية",
    "PCE — التضخم والإنفاق الشخصي",
    "GDP — الناتج المحلي",
    "قرار الفيدرالي FOMC",
    "FOMC Minutes — محضر الفيدرالي",
}


def _clean_official_html(raw_html):
    cleaned = re.sub(
        r"(?is)<script.*?</script>|<style.*?</style>",
        " ",
        raw_html or "",
    )
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    return " ".join(cleaned.split())


def _parse_month_date(date_text, time_text, default_year=None):
    date_text = " ".join(
        str(date_text or "")
        .replace(",", " ")
        .replace(".", "")
        .split()
    )
    parts = date_text.split()
    if len(parts) < 2:
        return None

    month_key = parts[0].lower()
    month = MONTHS.get(month_key)
    if not month:
        return None

    try:
        day = int(parts[1])
        year = int(parts[2]) if len(parts) >= 3 else int(default_year)
    except Exception:
        return None

    tm = (
        str(time_text or "")
        .upper()
        .replace(".", "")
        .strip()
    )
    try:
        parsed_time = datetime.strptime(tm, "%I:%M %p")
    except ValueError:
        return None

    eastern = datetime(
        year,
        month,
        day,
        parsed_time.hour,
        parsed_time.minute,
        tzinfo=ZoneInfo("America/New_York"),
    )
    return eastern.astimezone(
        ZoneInfo("Asia/Riyadh")
    )


def _future_event(event_dt, horizon_days=MAJOR_EVENT_LOOKAHEAD_DAYS):
    now = datetime.now(ZoneInfo("Asia/Riyadh"))
    return now <= event_dt <= now + timedelta(days=horizon_days)


def _fetch_bls_release(url, name, heat):
    response = requests.get(
        url,
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    page = _clean_official_html(response.text)

    month_pattern = (
        r"January|February|March|April|May|June|July|August|"
        r"September|October|November|December"
    )
    abbrev_pattern = (
        r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
    )

    # BLS rows:
    # Reference Month | Release Date | Release Time
    pattern = re.compile(
        rf"(?:{month_pattern})\s+\d{{4}}\s+"
        rf"((?:{month_pattern}|{abbrev_pattern})\.?\s+\d{{1,2}},\s+\d{{4}})\s+"
        rf"(\d{{1,2}}:\d{{2}}\s+[AP]M)",
        re.I,
    )

    events = []
    for match in pattern.finditer(page):
        dt = _parse_month_date(
            match.group(1),
            match.group(2),
        )
        if dt and _future_event(dt):
            events.append({
                "name": name,
                "heat": heat,
                "dt": dt,
                "source": "BLS",
                "source_url": url,
                "major": name in MAJOR_NAMES,
            })

    return events


def _fetch_bea_events():
    url = "https://www.bea.gov/news/schedule"
    response = requests.get(
        url,
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.raise_for_status()
    page = _clean_official_html(response.text)

    current_year = datetime.now(
        ZoneInfo("Asia/Riyadh")
    ).year

    month_pattern = (
        r"January|February|March|April|May|June|July|August|"
        r"September|October|November|December"
    )

    # BEA rows are displayed as: Month DD | HH:MM AM | News | Title
    pattern = re.compile(
        rf"({month_pattern})\s+(\d{{1,2}})\s+"
        rf"(\d{{1,2}}:\d{{2}}\s+[AP]M)\s+"
        rf"(?:News\s+)?(.{{1,220}}?)"
        rf"(?=(?:{month_pattern})\s+\d{{1,2}}\s+\d{{1,2}}:\d{{2}}\s+[AP]M|\Z)",
        re.I,
    )

    events = []
    for match in pattern.finditer(page):
        title = " ".join(match.group(4).split())
        title_lower = title.lower()

        if "personal income and outlays" in title_lower:
            name = "PCE — التضخم والإنفاق الشخصي"
            heat = "🔥🔥🔥"
        elif re.search(r"\bgdp\b", title, re.I):
            name = "GDP — الناتج المحلي"
            heat = "🔥🔥🔥"
        else:
            continue

        dt = _parse_month_date(
            f"{match.group(1)} {match.group(2)} {current_year}",
            match.group(3),
        )
        if dt and _future_event(dt):
            events.append({
                "name": name,
                "heat": heat,
                "dt": dt,
                "source": "BEA",
                "source_url": url,
                "major": True,
            })

    return events


def _fed_month_urls():
    now = datetime.now(ZoneInfo("Asia/Riyadh"))
    urls = []
    cursor_year = now.year
    cursor_month = now.month

    for offset in range(0, 4):
        month_index = cursor_month - 1 + offset
        year = cursor_year + month_index // 12
        month = month_index % 12 + 1
        month_name = datetime(
            year,
            month,
            1,
        ).strftime("%B").lower()

        urls.append((
            year,
            month,
            f"https://www.federalreserve.gov/newsevents/{year}-{month_name}.htm",
        ))

    return urls


def _parse_fed_clock(clock_text):
    tm = (
        str(clock_text or "")
        .upper()
        .replace(".", "")
        .strip()
    )
    try:
        parsed = datetime.strptime(tm, "%I:%M %p")
        return parsed.hour, parsed.minute
    except ValueError:
        return None


def _fetch_fed_events():
    events = []

    for year, month, url in _fed_month_urls():
        response = requests.get(
            url,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        page = _clean_official_html(response.text)

        event_pattern = re.compile(
            r"(\d{1,2}:\d{2}\s+[ap]\.m\.)\s+"
            r"(FOMC Minutes|FOMC Meeting|Beige Book|Speech|Discussion|Testimony)\s+"
            r"(.*?)"
            r"(?=\s+\d{1,2}:\d{2}\s+[ap]\.m\.|\s+Statistical Releases|\s+Speeches|\Z)",
            re.I,
        )

        for match in event_pattern.finditer(page):
            clock = _parse_fed_clock(match.group(1))
            if not clock:
                continue

            event_title = match.group(2).strip()
            trailing = match.group(3)

            # The official monthly calendar places the release date(s)
            # at the end of the event block. Meeting-date ranges can also
            # appear in the description, so use the final standalone day.
            day_candidates = re.findall(
                r"(?<!\d)([0-3]?\d)(?!\d)",
                trailing,
            )
            day_candidates = [
                int(x)
                for x in day_candidates
                if 1 <= int(x) <= 31
            ]
            if not day_candidates:
                continue

            day = day_candidates[-1]
            hour, minute = clock

            try:
                eastern = datetime(
                    year,
                    month,
                    day,
                    hour,
                    minute,
                    tzinfo=ZoneInfo("America/New_York"),
                )
            except ValueError:
                continue

            dt = eastern.astimezone(
                ZoneInfo("Asia/Riyadh")
            )
            if not _future_event(dt):
                continue

            title_lower = event_title.lower()
            if "minutes" in title_lower:
                name = "FOMC Minutes — محضر الفيدرالي"
                heat = "🔥🔥🔥"
                major = True
            elif "meeting" in title_lower:
                name = "قرار الفيدرالي FOMC"
                heat = "🔥🔥🔥"
                major = True
            elif "beige book" in title_lower:
                name = "Beige Book — الكتاب البيج"
                heat = "🔥🔥"
                major = False
            else:
                # Fed speeches/discussions/testimony can move the market.
                # Keep only blocks that clearly contain a Fed policymaker name/title.
                detail = " ".join(trailing.split())
                relevant = re.search(
                    r"(Chair|Vice Chair|Governor|Federal Reserve|monetary policy|economic outlook|inflation|labor market)",
                    detail,
                    re.I,
                )
                if not relevant:
                    continue
                short_detail = detail[:120].strip(" -–—")
                kind_ar = {
                    "speech": "خطاب مسؤول في الفيدرالي",
                    "discussion": "حديث مسؤول في الفيدرالي",
                    "testimony": "شهادة مسؤول في الفيدرالي",
                }.get(title_lower, "حديث مسؤول في الفيدرالي")
                name = kind_ar + (f" — {short_detail}" if short_detail else "")
                heat = "🔥🔥"
                major = True

            events.append({
                "name": name,
                "heat": heat,
                "dt": dt,
                "source": "Federal Reserve",
                "source_url": url,
                "major": major,
            })

    return events


def _riyadh_event_status(event_dt):
    now = datetime.now(ZoneInfo("Asia/Riyadh"))
    seconds = (event_dt - now).total_seconds()

    if seconds < 0:
        return None

    if event_dt.date() == now.date():
        return "🚨 اليوم"

    if seconds <= 48 * 3600:
        return "⏳ غدًا"

    return "📅 قادم"


def _load_official_calendar():
    now_ts = time.time()

    if (
        ECON_CALENDAR_CACHE["events"] is not None
        and now_ts - ECON_CALENDAR_CACHE["time"]
        < ECON_CALENDAR_CACHE_SECONDS
    ):
        return (
            ECON_CALENDAR_CACHE["events"],
            ECON_CALENDAR_CACHE["errors"],
        )

    events = []
    errors = []

    # BLS commonly blocks cloud-hosted requests (including Render) with HTTP 403.
    # When that happens, do NOT flood logs with red errors and do NOT remove the
    # verified BLS dates. get_upcoming_us_events() will merge the verified fallback
    # calendar below for CPI/PPI/Jobs.
    bls_blocked = False
    for url, name, heat in BLS_RELEASES:
        if bls_blocked:
            break
        try:
            events.extend(
                _fetch_bls_release(
                    url,
                    name,
                    heat,
                )
            )
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 403:
                bls_blocked = True
                print(
                    "ECON CALENDAR BLS 403 — using verified fallback dates instead.",
                    flush=True,
                )
                continue
            errors.append(f"BLS {name}: {exc}")
            print(
                f"ECON CALENDAR ERROR BLS {name}:",
                exc,
                flush=True,
            )
        except Exception as exc:
            errors.append(f"BLS {name}: {exc}")
            print(
                f"ECON CALENDAR ERROR BLS {name}:",
                exc,
                flush=True,
            )

    try:
        events.extend(_fetch_bea_events())
    except Exception as exc:
        errors.append(f"BEA: {exc}")
        print("ECON CALENDAR ERROR BEA:", exc)

    try:
        events.extend(_fetch_fed_events())
    except Exception as exc:
        errors.append(f"Federal Reserve: {exc}")
        print("ECON CALENDAR ERROR FED:", exc)

    # Deduplicate identical events from overlapping official pages.
    unique = {}
    for event in events:
        key = (
            event["name"],
            event["dt"].strftime("%Y-%m-%d %H:%M"),
        )
        unique[key] = event

    clean_events = sorted(
        unique.values(),
        key=lambda item: item["dt"],
    )

    ECON_CALENDAR_CACHE["time"] = now_ts
    ECON_CALENDAR_CACHE["events"] = clean_events
    ECON_CALENDAR_CACHE["errors"] = errors

    return clean_events, errors



# Verified fallback for major market-moving releases.
# Used ONLY when a live official calendar source is unavailable.
# Dates below were cross-checked against BLS/Federal Reserve official schedules.
VERIFIED_MAJOR_FALLBACK_2026 = [
    ("2026-08-19", "14:00", "FOMC Minutes — محضر الفيدرالي", "🔥🔥🔥", "Federal Reserve"),
    # BEA official schedule: both releases are Aug 26, 2026 at 8:30 AM ET.
    ("2026-08-26", "08:30", "PCE — التضخم والإنفاق الشخصي", "🔥🔥🔥", "BEA"),
    ("2026-08-26", "08:30", "GDP — الناتج المحلي", "🔥🔥🔥", "BEA"),
    ("2026-09-04", "08:30", "الوظائف والبطالة الأمريكية", "🔥🔥🔥", "BLS"),
    ("2026-09-10", "08:30", "PPI — مؤشر أسعار المنتجين", "🔥🔥🔥", "BLS"),
    ("2026-09-11", "08:30", "CPI — مؤشر أسعار المستهلك", "🔥🔥🔥", "BLS"),
    ("2026-09-16", "14:00", "قرار الفيدرالي FOMC", "🔥🔥🔥", "Federal Reserve"),
    ("2026-10-02", "08:30", "الوظائف والبطالة الأمريكية", "🔥🔥🔥", "BLS"),
    ("2026-10-14", "08:30", "CPI — مؤشر أسعار المستهلك", "🔥🔥🔥", "BLS"),
    ("2026-10-15", "08:30", "PPI — مؤشر أسعار المنتجين", "🔥🔥🔥", "BLS"),
    ("2026-10-28", "14:00", "قرار الفيدرالي FOMC", "🔥🔥🔥", "Federal Reserve"),
    ("2026-11-06", "08:30", "الوظائف والبطالة الأمريكية", "🔥🔥🔥", "BLS"),
    ("2026-11-10", "08:30", "CPI — مؤشر أسعار المستهلك", "🔥🔥🔥", "BLS"),
    ("2026-11-13", "08:30", "PPI — مؤشر أسعار المنتجين", "🔥🔥🔥", "BLS"),
    ("2026-12-04", "08:30", "الوظائف والبطالة الأمريكية", "🔥🔥🔥", "BLS"),
    ("2026-12-09", "14:00", "قرار الفيدرالي FOMC", "🔥🔥🔥", "Federal Reserve"),
    ("2026-12-10", "08:30", "CPI — مؤشر أسعار المستهلك", "🔥🔥🔥", "BLS"),
    ("2026-12-15", "08:30", "PPI — مؤشر أسعار المنتجين", "🔥🔥🔥", "BLS"),
]


def _verified_major_fallback_events():
    now = datetime.now(ZoneInfo("Asia/Riyadh"))
    horizon = now + timedelta(days=MAJOR_EVENT_LOOKAHEAD_DAYS)
    events = []

    for ds, clock, name, heat, source in VERIFIED_MAJOR_FALLBACK_2026:
        day = datetime.strptime(ds, "%Y-%m-%d")
        hour, minute = map(int, clock.split(":"))
        eastern = datetime(
            day.year, day.month, day.day,
            hour, minute,
            tzinfo=ZoneInfo("America/New_York"),
        )
        dt = eastern.astimezone(ZoneInfo("Asia/Riyadh"))

        if now <= dt <= horizon:
            events.append({
                "name": name,
                "heat": heat,
                "dt": dt,
                "source": source + " ✓",
                "major": True,
                "fallback": True,
            })

    return events


def get_upcoming_us_events():
    all_events, errors = _load_official_calendar()
    now = datetime.now(ZoneInfo("Asia/Riyadh"))
    ordinary_end = now + timedelta(days=UPCOMING_EVENT_DAYS)

    # Merge live official results with verified fallback.
    # Live official data wins if both contain the same event.
    fallback_events = _verified_major_fallback_events()
    merged = {}

    for event in fallback_events:
        key = (event["name"], event["dt"].strftime("%Y-%m-%d %H:%M"))
        merged[key] = event

    for event in all_events:
        key = (event["name"], event["dt"].strftime("%Y-%m-%d %H:%M"))
        merged[key] = event

    all_verified = sorted(merged.values(), key=lambda item: item["dt"])

    selected = [
        event for event in all_verified
        if now <= event["dt"] <= ordinary_end
    ]

    # Always include the next major event of each type.
    for major_name in MAJOR_NAMES:
        candidates = [
            event for event in all_verified
            if event["name"] == major_name and event["dt"] >= now
        ]
        if candidates:
            selected.append(candidates[0])

    unique = {}
    for event in selected:
        key = (event["name"], event["dt"].strftime("%Y-%m-%d %H:%M"))
        unique[key] = event

    return sorted(unique.values(), key=lambda item: item["dt"]), errors


def format_upcoming_us_events():
    events, errors = get_upcoming_us_events()

    if not events:
        message = (
            "📅 الأحداث الأمريكية القادمة\n\n"
            "✅ لا توجد أحداث رئيسية مجدولة ضمن النطاق الحالي."
        )
        if errors:
            message += (
                "\n⚠️ تعذر تحديث بعض الجداول الرسمية؛ "
                "لا تعتبر النتيجة نهائية."
            )
        return message

    arabic_days = {
        0: "الاثنين",
        1: "الثلاثاء",
        2: "الأربعاء",
        3: "الخميس",
        4: "الجمعة",
        5: "السبت",
        6: "الأحد",
    }

    blocks = []
    for event in events:
        status = (
            _riyadh_event_status(event["dt"])
            or "📅 قادم"
        )
        day_name = arabic_days[
            event["dt"].weekday()
        ]

        blocks.append(
            f"{status} | {event['heat']}\n"
            f"🇺🇸 {event['name']}\n"
            f"🗓️ {day_name} "
            f"{event['dt'].strftime('%d/%m/%Y')}\n"
            f"⏰ {event['dt'].strftime('%I:%M %p')} "
            f"بتوقيت السعودية\n"
            f"🏛️ المصدر: {event['source']}"
        )

    result = (
        "📅 الأحداث الأمريكية القادمة\n"
        "━━━━━━━━━━━━━━\n\n"
        + "\n\n━━━━━━━━━━━━━━\n\n".join(
            blocks
        )
    )

    if errors:
        print(
            "US EVENTS SOURCE WARNING:",
            "; ".join(str(error) for error in errors)
        )

    return result


# =========================================================
# EARNINGS - FINNHUB
# =========================================================

# Officially confirmed earnings dates cross-checked with company IR.
# Any Finnhub date not listed here is shown conservatively as "expected".
OFFICIAL_EARNINGS_CONFIRMATIONS = {
    ("NVDA", "2026-08-26"): {
        "status": "confirmed",
        "source": "NVIDIA Investor Relations",
    },
    ("MRVL", "2026-08-27"): {
        "status": "confirmed",
        "source": "Marvell Investor Relations",
    },
}


def get_earnings_calendar(
    days=EARNINGS_LOOKAHEAD_DAYS
):
    if not FINNHUB_TOKEN:
        raise RuntimeError(
            "FINNHUB_TOKEN is missing"
        )

    today = datetime.now(
        ZoneInfo("America/New_York")
    ).date()

    to_date = (
        today + timedelta(days=days)
    )

    response = requests.get(
        "https://finnhub.io/api/v1/calendar/earnings",
        headers=get_finnhub_headers(),
        params={
            "from": today.isoformat(),
            "to": to_date.isoformat(),
        },
        timeout=20
    )

    response.raise_for_status()
    data = response.json()

    if not isinstance(data, dict):
        raise ValueError(
            "استجابة إعلانات الشركات غير متوقعة."
        )

    entries = data.get(
        "earningsCalendar",
        []
    )

    if not isinstance(entries, list):
        return []

    wanted = set(SCAN_SYMBOLS)
    filtered = []

    for item in entries:
        symbol = str(
            item.get("symbol") or ""
        ).upper()

        if symbol not in wanted:
            continue

        date_text = str(
            item.get("date") or ""
        )

        try:
            event_date = datetime.strptime(
                date_text,
                "%Y-%m-%d"
            ).date()
        except ValueError:
            continue

        filtered.append(
            {
                "symbol": symbol,
                "date": event_date,
                "date_text": date_text,
                "hour": str(
                    item.get("hour") or ""
                ).lower(),
                "eps_estimate": item.get(
                    "epsEstimate"
                ),
                "revenue_estimate": item.get(
                    "revenueEstimate"
                ),
                "quarter": item.get(
                    "quarter"
                ),
                "year": item.get(
                    "year"
                ),
                "verification": (
                    OFFICIAL_EARNINGS_CONFIRMATIONS.get(
                        (symbol, date_text),
                        {
                            "status": "expected",
                            "source": "Finnhub calendar",
                        }
                    )
                ),
            }
        )

    filtered.sort(
        key=lambda x: (
            x["date"],
            x["symbol"]
        )
    )

    return filtered


def earnings_hour_ar(hour):
    value = (hour or "").lower()

    if value in {"bmo", "before market open"}:
        return "🌅 قبل افتتاح السوق"

    if value in {"amc", "after market close"}:
        return "🌙 بعد إغلاق السوق"

    if value in {"dmh", "during market hours"}:
        return "🕐 أثناء السوق"

    return "🕐 التوقيت غير محدد"


def format_large_number(value):
    try:
        value = float(value)
    except (
        TypeError,
        ValueError
    ):
        return "غير متوفر"

    abs_value = abs(value)

    if abs_value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"

    if abs_value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"

    return f"${value:,.0f}"


def format_earnings_item(item):
    eps = item["eps_estimate"]

    try:
        eps_text = f"${float(eps):.2f}"
    except (
        TypeError,
        ValueError
    ):
        eps_text = "غير متوفر"

    revenue_text = format_large_number(
        item["revenue_estimate"]
    )

    verification = item.get(
        "verification",
        {
            "status": "expected",
            "source": "Finnhub calendar",
        }
    )

    if verification.get("status") == "confirmed":
        status_text = "✅ الموعد مؤكد رسميًا"
    else:
        status_text = "⚠️ موعد متوقع — غير مؤكد رسميًا"

    return (
        f"📅 {item['symbol']} — "
        f"{item['date'].strftime('%d/%m/%Y')}\n"
        f"{status_text}\n"
        f"{earnings_hour_ar(item['hour'])}\n"
        f"📊 EPS المتوقع: {eps_text}\n"
        f"💰 الإيرادات المتوقعة: {revenue_text}"
    )


def format_earnings_results(results):
    if not results:
        return (
            "📅 إعلانات الشركات\n\n"
            "✅ لا توجد إعلانات أرباح قادمة "
            "لأسهم قائمتنا خلال الفترة الحالية."
        )

    blocks = [
        format_earnings_item(item)
        for item in results
    ]

    return (
        "📅 إعلانات أسهمنا القادمة\n"
        "⏰ التنبيه تلقائي قبل 7 أيام، قبل 3 أيام، وفي نفس اليوم\n"
        "━━━━━━━━━━━━━━\n\n"
        + "\n\n━━━━━━━━━━━━━━\n\n".join(blocks)
        + "\n\nℹ️ توقعات EPS والإيرادات تتحدث حسب المصدر وقد تتغير قبل الإعلان."
    )


async def send_earnings_reminders(
    application
):
    while True:
        try:
            riyadh_now = datetime.now(
                ZoneInfo("Asia/Riyadh")
            )

            # Do not send immediately on every deploy/restart.
            # There is one controlled reminder window each day.
            if riyadh_now.hour != EARNINGS_REMINDER_HOUR_KSA:
                await asyncio.sleep(
                    EARNINGS_LOOP_SECONDS
                )
                continue

            # Reload the local state before each send window.
            # This protects against duplicate sends after a process restart.
            persisted = _load_earnings_sent_state()
            if persisted:
                EARNINGS_SENT.update(
                    persisted
                )

            events = (
                await asyncio.to_thread(
                    get_earnings_calendar,
                    14
                )
            )

            today = riyadh_now.date()

            for event in events:
                days_until = (
                    event["date"] - today
                ).days

                if days_until not in {
                    7,
                    3,
                    0
                }:
                    continue

                reminder_type = (
                    "week"
                    if days_until == 7
                    else "three_days"
                    if days_until == 3
                    else "today"
                )

                key = (
                    f"{event['symbol']}:"
                    f"{event['date_text']}:"
                    f"{reminder_type}"
                )

                if key in EARNINGS_SENT:
                    continue

                if days_until == 7:
                    heading = (
                        "📅 تنبيه إعلان بعد أسبوع"
                    )
                elif days_until == 3:
                    heading = (
                        "📅 تذكير: إعلان الشركة بعد 3 أيام"
                    )
                else:
                    heading = (
                        "🔔 تذكير: إعلان الشركة اليوم"
                    )

                text = (
                    f"{heading}\n\n"
                    f"{format_earnings_item(event)}"
                )

                sent_to_any_user = False

                for user_id in ALLOWED_USERS:
                    try:
                        await application.bot.send_message(
                            chat_id=user_id,
                            text=text
                        )
                        sent_to_any_user = True

                    except Exception as send_error:
                        print(
                            "EARNINGS SEND ERROR:",
                            user_id,
                            repr(send_error),
                            flush=True,
                        )

                # Mark as sent only when at least one delivery succeeded.
                if sent_to_any_user:
                    EARNINGS_SENT.add(
                        key
                    )
                    _save_earnings_sent_state(
                        EARNINGS_SENT
                    )

        except Exception as e:
            print(
                "EARNINGS MONITOR ERROR:",
                type(e).__name__,
                repr(e),
                flush=True,
            )

        await asyncio.sleep(
            EARNINGS_LOOP_SECONDS
        )


async def monitor_important_news(
    application
):
    await asyncio.sleep(30)
    baseline_ready = False

    while True:
        try:
            NEWS_CACHE["time"] = 0
            NEWS_CACHE["results"] = None

            results = (
                await asyncio.to_thread(
                    scan_important_news
                )
            )

            # عند تشغيل/إعادة تشغيل Render نأخذ لقطة مرجعية للأخبار الحالية
            # ولا نرسلها كتنبيهات جديدة. هذا يمنع رجوع أخبار الساعات السابقة
            # دفعة واحدة بعد كل Deploy، مع إبقاء نافذة 18 ساعة للزر اليدوي.
            if not baseline_ready:
                baselined = 0
                for baseline_item in results:
                    baseline_min_importance = (
                        FAST_NEWS_AUTO_MIN_IMPORTANCE
                        if baseline_item.get("trusted_fast_news")
                        else NEWS_AUTO_MIN_IMPORTANCE
                    )
                    if baseline_item.get("importance", 0) < baseline_min_importance:
                        continue
                    if baseline_item.get("sentiment") == "NEUTRAL":
                        continue
                    baseline_news_id = baseline_item.get("id")
                    if baseline_news_id:
                        SENT_NEWS_IDS.add(baseline_news_id)
                    _remember_news_event(baseline_item)
                    baselined += 1

                _save_auto_sent_state()
                baseline_ready = True
                print(
                    f"NEWS MONITOR V20 READY — BASELINED {baselined} CURRENT ITEMS; "
                    "AUTO SENDS ONLY NEWS DISCOVERED AFTER STARTUP",
                    flush=True,
                )
                await asyncio.sleep(NEWS_AUTO_INTERVAL_SECONDS)
                continue

            for item in results:
                min_importance = (
                    FAST_NEWS_AUTO_MIN_IMPORTANCE
                    if item.get("trusted_fast_news")
                    else NEWS_AUTO_MIN_IMPORTANCE
                )
                if item["importance"] < min_importance:
                    continue

                news_id = item["id"]

                # حماية إضافية: الأخبار المحايدة لا تصل لهذه المرحلة أصلًا، وإن وصلت تُتخطى.
                if item.get("sentiment") == "NEUTRAL":
                    print(
                        f"NEWS SKIP NEUTRAL {item.get('symbol')} "
                        f"{item.get('headline','')[:120]}",
                        flush=True,
                    )
                    SENT_NEWS_IDS.add(news_id)
                    _save_auto_sent_state()
                    continue
                if news_id in SENT_NEWS_IDS:
                    continue

                # Same event from Reuters/Yahoo/Barron's/etc. is one alert, not many.
                # This is semantic/event-level dedupe, not just URL-level dedupe.
                if _news_event_is_duplicate(item):
                    SENT_NEWS_IDS.add(news_id)
                    _save_auto_sent_state()
                    continue

                # Only push genuinely fresh stories. 10/10 gets a slightly wider window.
                age_seconds = time.time() - int(item.get("timestamp") or 0)
                max_age_hours = (
                    NEWS_AUTO_CRITICAL_MAX_AGE_HOURS
                    if item.get("importance", 0) >= NEWS_AUTO_CRITICAL_IMPORTANCE
                    else NEWS_AUTO_MAX_AGE_HOURS
                )
                if age_seconds < -300 or age_seconds > (max_age_hours * 3600):
                    print(
                        f"NEWS SKIP STALE {item.get('symbol')} age_h={age_seconds/3600:.1f} "
                        f"limit_h={max_age_hours} {item.get('headline','')[:120]}",
                        flush=True,
                    )
                    # Do not mark stale items as sent: a future policy/window change
                    # must not permanently suppress an otherwise unsent material event.
                    continue

                alert_header = (
                    "⚡ خبر مهم التقطه المصدر الاحتياطي"
                    if item.get("trusted_fast_news")
                    else "🚨 خبر مهم جديد"
                )
                text = (
                    f"{alert_header}\n\n"
                    f"{format_news_item(item)}"
                )

                sent = False
                for user_id in ALLOWED_USERS:
                    try:
                        await application.bot.send_message(
                            chat_id=user_id,
                            text=text,
                            parse_mode="HTML",
                            disable_web_page_preview=True
                        )
                        sent = True
                    except Exception as send_error:
                        print("NEWS SEND ERROR:", user_id, send_error)

                if sent:
                    SENT_NEWS_IDS.add(news_id)
                    _remember_news_event(item)
                    _save_auto_sent_state()

        except Exception as e:
            print("NEWS MONITOR ERROR:", e)

        await asyncio.sleep(NEWS_AUTO_INTERVAL_SECONDS)



async def monitor_major_us_events(application):
    """Remind about major U.S. events at noon KSA: one week, 3 days, and same day."""
    await asyncio.sleep(45)

    while True:
        try:
            events, _errors = await asyncio.to_thread(get_upcoming_us_events)
            now = datetime.now(ZoneInfo("Asia/Riyadh"))

            # Send scheduled event reminders only around noon KSA.
            # The loop runs every 15 minutes, so the first pass between
            # 12:00 and 12:59 sends any reminder due for that day/stage.
            if now.hour != EVENT_REMINDER_HOUR_KSA:
                await asyncio.sleep(EVENT_REMINDER_LOOP_SECONDS)
                continue

            for event in events:
                if not event.get("major"):
                    continue

                seconds = (event["dt"] - now).total_seconds()
                if seconds < 0:
                    continue

                stages_due = []

                event_day = event["dt"].date()
                today = now.date()
                days_until = (event_day - today).days

                # Exclusive reminder stages so a restart does not fire multiple
                # historical reminder labels at the same time.
                if 4 <= days_until <= 7:
                    stages_due.append(("WEEK", "قبل أسبوع"))
                elif 1 <= days_until <= 3:
                    stages_due.append(("THREE_DAYS", "قبل 3 أيام"))
                elif days_until == 0:
                    stages_due.append(("TODAY", "اليوم"))

                for stage, stage_ar in stages_due:
                    key = (
                        f"{event['name']}|"
                        f"{event['dt'].strftime('%Y-%m-%d %H:%M')}|"
                        f"{stage}"
                    )
                    if key in SENT_EVENT_REMINDERS:
                        continue

                    text_msg = (
                        f"🚨 تذكير {stage_ar} بحدث أمريكي مهم\n\n"
                        f"🇺🇸 {event['name']}\n"
                        f"📅 {event['dt'].strftime('%d/%m/%Y')}\n"
                        f"⏰ {event['dt'].strftime('%I:%M %p')} بتوقيت السعودية\n"
                        f"🔥 التأثير المتوقع: {event['heat']}\n"
                        f"🏛️ المصدر: {event['source']}"
                    )

                    sent = False
                    for user_id in ALLOWED_USERS:
                        try:
                            await application.bot.send_message(
                                chat_id=user_id,
                                text=text_msg,
                                disable_web_page_preview=True,
                            )
                            sent = True
                        except Exception as send_error:
                            print("EVENT REMINDER SEND ERROR:", user_id, send_error)

                    if sent:
                        SENT_EVENT_REMINDERS.add(key)
                        _save_auto_sent_state()

        except Exception as exc:
            print("EVENT REMINDER ERROR:", type(exc).__name__, repr(exc), flush=True)

        await asyncio.sleep(EVENT_REMINDER_LOOP_SECONDS)


# =========================================================
# CONTRACT SCORE
# =========================================================

def contract_score(
    delta,
    volume,
    oi,
    spread_pct,
    dte
):
    score = 0
    delta_abs = abs(delta)

    if 0.42 <= delta_abs <= 0.58:
        score += 25
    elif 0.35 <= delta_abs <= 0.65:
        score += 20
    elif 0.30 <= delta_abs <= 0.70:
        score += 12
    elif 0.20 <= delta_abs <= 0.80:
        score += 7
    else:
        score += 3

    if volume >= 10000:
        score += 20
    elif volume >= 5000:
        score += 18
    elif volume >= 2000:
        score += 15
    elif volume >= 1000:
        score += 12
    else:
        score += 2

    if oi >= 10000:
        score += 15
    elif oi >= 5000:
        score += 13
    elif oi >= 2000:
        score += 10
    elif oi >= 1000:
        score += 7
    elif oi >= 500:
        score += 4
    elif oi >= 100:
        score += 2
    else:
        score += 1

    if spread_pct <= 2:
        score += 20
    elif spread_pct <= 3:
        score += 17
    elif spread_pct <= 5:
        score += 13
    elif spread_pct <= 8:
        score += 8
    elif spread_pct <= 12:
        score += 4
    elif spread_pct <= 15:
        score += 2

    if 7 <= dte <= 14:
        score += 10
    elif 15 <= dte <= 21:
        score += 8
    elif 5 <= dte <= 6:
        score += 7
    elif 22 <= dte <= 30:
        score += 6

    return min(score, 90)


def normalize_contract_score(raw_score):
    return round(
        (raw_score / 90) * 100
    )


def unusual_activity_score(volume, oi):
    if oi <= 0:
        return (
            0,
            0,
            "⚪ غير متاح"
        )

    ratio = volume / oi

    if ratio >= 5:
        ratio_points = 5
    elif ratio >= 3:
        ratio_points = 4
    elif ratio >= 2:
        ratio_points = 3
    elif ratio >= 1:
        ratio_points = 2
    elif ratio >= 0.5:
        ratio_points = 1
    else:
        ratio_points = 0

    if volume >= 15000:
        volume_points = 3
    elif volume >= 7000:
        volume_points = 2
    elif volume >= 2500:
        volume_points = 1
    else:
        volume_points = 0

    if oi >= 5000:
        oi_points = 2
    elif oi >= 1500:
        oi_points = 1
    else:
        oi_points = 0

    score = min(
        ratio_points
        + volume_points
        + oi_points,
        10
    )

    if score >= 9:
        label = "🔥 استثنائي جدًا"
    elif score >= 7:
        label = "🔥 مرتفع جدًا"
    elif score >= 5:
        label = "🟢 مرتفع"
    elif score >= 3:
        label = "🟡 ملحوظ"
    else:
        label = "⚪ طبيعي"

    return score, ratio, label


def apply_market_score(side, trend):
    bias = trend["bias"]
    momentum = trend["momentum_score"]
    continuation = trend["continuation_score"]
    adjustment = 0

    if bias == "NEUTRAL":
        return 0

    if side == bias:
        if momentum >= 8:
            adjustment += 5
        elif momentum >= 6:
            adjustment += 4
        elif momentum >= 4:
            adjustment += 2
        else:
            adjustment += 1

        if continuation >= 4:
            adjustment += 2
        elif continuation >= 2:
            adjustment += 1
        elif continuation < 0:
            adjustment -= 3

    else:
        if momentum >= 8:
            adjustment -= 12
        elif momentum >= 6:
            adjustment -= 10
        elif momentum >= 4:
            adjustment -= 7
        else:
            adjustment -= 4

        if continuation >= 4:
            adjustment -= 3
        elif continuation >= 2:
            adjustment -= 2

    return adjustment


# =========================================================
# DECISION
# =========================================================

def decision_status(contract, trend):
    bias = trend["bias"]
    momentum = trend["momentum_score"]
    continuation = trend["continuation_score"]

    if bias == "NEUTRAL":
        return {
            "label": "🟡 انتظار تأكيد",
            "reason": "الاتجاه غير محسوم",
            "rank": 1,
        }

    if continuation < 0 and contract["side"] == bias:
        return {
            "label": "🔴 استبعاد",
            "reason": "استمرار الحركة بدأ يضعف",
            "rank": 0,
        }

    if contract["score"] < MIN_TOP_SCORE:
        return {
            "label": "🟡 غير مؤهل للمراقبة",
            "reason": (
                f"التقييم {contract['score']}/100 "
                f"أقل من {MIN_TOP_SCORE}/100"
            ),
            "rank": 1,
        }

    if contract["uoa_score"] < MIN_TOP_UOA:
        return {
            "label": "🟡 غير مؤهل للمراقبة",
            "reason": (
                f"النشاط {contract['uoa_score']}/10 "
                f"أقل من {MIN_TOP_UOA}/10"
            ),
            "rank": 1,
        }

    if (
        momentum >= 6
        and continuation >= 2
        and contract["base_score"] >= 78
    ):
        return {
            "label": "🟢 تأكيد دخول",
            "reason": "الاتجاه والزخم والاستمرار متوافقون",
            "rank": 2,
        }

    if momentum < 6:
        reason = (
            f"الزخم {momentum}/10 "
            f"ويحتاج تأكيد حركة قصيرة"
        )
    elif continuation < 2:
        reason = "ننتظر تأكيد استمرار الحركة"
    elif contract["base_score"] < 78:
        reason = (
            f"جودة العقد {contract['base_score']}/100 "
            f"وتحتاج تحسنًا"
        )
    else:
        reason = (
            "الفرصة جيدة لكنها تحتاج تأكيدًا إضافيًا"
        )

    return {
        "label": "🟡 انتظار تأكيد",
        "reason": reason,
        "rank": 1,
    }


def effective_decision(contract, trend):
    decision = decision_status(
        contract,
        trend
    )

    if (
        decision["rank"] == 2
        and not is_us_market_open()
    ):
        return {
            "label": "🟢 مرشح قوي — انتظار افتتاح السوق",
            "reason": "الشروط قوية لكن السوق الأمريكي مغلق",
            "rank": 1,
        }

    return decision


# =========================================================
# TOP CONTRACTS
# =========================================================

def get_top_contracts(data, trend, soft_direction=False):
    contracts = []

    fields = [
        "optionSymbol",
        "expiration",
        "side",
        "strike",
        "dte",
        "bid",
        "ask",
        "mid",
        "volume",
        "openInterest",
        "delta",
    ]

    for field in fields:
        if field not in data:
            raise ValueError(
                f"بيانات {field} غير موجودة."
            )

    count = len(
        data["optionSymbol"]
    )

    for i in range(count):
        try:
            option_symbol = data["optionSymbol"][i]
            expiration = data["expiration"][i]
            expiry_date = format_expiry_timestamp(
                expiration
            )

            side = str(
                data["side"][i]
            ).upper()

            strike = float(
                data["strike"][i]
            )

            dte = int(
                data["dte"][i]
            )

            bid = data["bid"][i]
            ask = data["ask"][i]
            mid = data["mid"][i]
            volume = data["volume"][i]
            oi = data["openInterest"][i]
            delta = data["delta"][i]

            if any(
                x is None
                for x in [
                    expiration,
                    bid,
                    ask,
                    mid,
                    volume,
                    oi,
                    delta,
                ]
            ):
                continue

            bid = float(bid)
            ask = float(ask)
            mid = float(mid)
            volume = int(volume)
            oi = int(oi)
            delta = float(delta)

            if dte < 5 or dte > 30:
                continue

            if ask > MAX_OPTION_ASK:
                continue

            if ask <= 0 or mid <= 0 or bid < 0:
                continue

            spread_pct = (
                (ask - bid)
                / mid
                * 100
            )

            if volume < MIN_VOLUME:
                continue

            if oi < 100:
                continue

            if (
                abs(delta) < 0.20
                or abs(delta) > 0.80
            ):
                continue

            if spread_pct > 15:
                continue

            raw_score = contract_score(
                delta,
                volume,
                oi,
                spread_pct,
                dte
            )

            base_score = normalize_contract_score(
                raw_score
            )

            (
                uoa_score,
                volume_oi_ratio,
                uoa_label
            ) = unusual_activity_score(
                volume,
                oi
            )

            market_adjustment = apply_market_score(
                side,
                trend
            )

            # In TOP OPPORTUNITIES, direction is only a soft factor, never a hard gate.
            # Keep the original strict behavior everywhere else (e.g. Best Contracts).
            if soft_direction and side != str(trend.get("bias", "NEUTRAL")).upper():
                market_adjustment = max(market_adjustment, -3)

            uoa_adjustment = round(
                uoa_score * 0.7
            )

            internal_score = (
                base_score
                + market_adjustment
                + uoa_adjustment
            )

            display_score = max(
                0,
                min(
                    round(internal_score),
                    98
                )
            )

            contract = {
                "option_symbol": option_symbol,
                "expiration": expiration,
                "expiry_date": expiry_date,
                "side": side,
                "strike": strike,
                "dte": dte,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "volume": volume,
                "oi": oi,
                "delta": delta,
                "spread_pct": spread_pct,
                "volume_oi_ratio": volume_oi_ratio,
                "base_score": base_score,
                "uoa_score": uoa_score,
                "uoa_label": uoa_label,
                "uoa_adjustment": uoa_adjustment,
                "market_adjustment": market_adjustment,
                "internal_score": internal_score,
                "score": display_score,
            }

            contract["decision"] = decision_status(
                contract,
                trend
            )

            contracts.append(contract)

        except (
            TypeError,
            ValueError,
            IndexError
        ):
            continue

    contracts.sort(
        key=lambda x: (
            -x["internal_score"],
            -x["uoa_score"],
            x["spread_pct"],
            -x["volume"],
            -x["oi"]
        )
    )

    return contracts[:TOP_N_RESULTS]


def analyze_symbol(symbol):
    trend = get_stock_trend(symbol)
    data = get_option_chain(symbol)
    contracts = get_top_contracts(
        data,
        trend
    )

    if not contracts:
        return None

    best = contracts[0]

    return {
        "symbol": symbol,
        "trend": trend,
        "contract": best,
        "contracts": contracts,
        "internal_score": best["internal_score"],
    }


# =========================================================
# WATCH
# =========================================================

def watch_key(chat_id, symbol):
    return f"{chat_id}:{symbol}"


def add_pending_watch(
    chat_id,
    symbol,
    contract,
    trend
):
    # Monitoring is based on the selected contract side + 1H confirmation.
    # Stock trend/bias is NOT a hard gate here; this keeps TOP opportunities
    # and Best Contracts follow-up consistent after a contract is selected.
    if contract["score"] < MIN_TOP_SCORE:
        return False

    if contract["uoa_score"] < MIN_TOP_UOA:
        return False

    if contract["ask"] > MAX_OPTION_ASK:
        return False

    if contract["volume"] < MIN_VOLUME:
        return False

    key = watch_key(
        chat_id,
        symbol
    )

    now = time.time()

    PENDING_WATCHES[key] = {
        "chat_id": chat_id,
        "symbol": symbol,
        "side": contract["side"],
        "strike": contract["strike"],
        "dte": contract["dte"],
        "expiration": contract["expiration"],
        "expiry_date": contract["expiry_date"],
        "option_symbol": contract["option_symbol"],
        "original_ask": contract["ask"],
        "created_at": now,
        "last_checked_at": now,
    }

    return True


def get_matching_contract(
    symbol,
    option_symbol
):
    # The OCC option symbol uniquely identifies the exact contract.
    # Use the single-contract quotes endpoint instead of re-reading a chain.
    try:
        return get_option_quote_by_symbol(option_symbol)
    except Exception as exc:
        print(
            "MATCHING CONTRACT QUOTE ERROR:",
            symbol, option_symbol, repr(exc), flush=True
        )
        return None


def check_intraday_confirmation(
    side,
    intraday
):
    last_price = intraday["last_price"]
    sma5 = intraday["sma5"]
    move = intraday["change_3bars"]
    up_bars = intraday["up_bars"]
    down_bars = intraday["down_bars"]

    if side == "CALL":
        confirmed = (
            last_price > sma5
            and move >= 0.20
            and up_bars >= 3
        )

        invalidated = (
            last_price < sma5
            and move <= -0.60
            and down_bars >= 3
        )

    else:
        confirmed = (
            last_price < sma5
            and move <= -0.20
            and down_bars >= 3
        )

        invalidated = (
            last_price > sma5
            and move >= 0.60
            and up_bars >= 3
        )

    if confirmed:
        return (
            "CONFIRMED",
            "حركة إطار التأكيد أصبحت موافقة للاتجاه"
        )

    if invalidated:
        return (
            "INVALID",
            "حركة إطار التأكيد انعكست ضد الفرصة"
        )

    return (
        "WAIT",
        "التأكيد لم يكتمل"
    )


async def monitor_pending(application):
    await asyncio.sleep(
        WATCH_LOOP_SECONDS
    )

    while True:
        if not is_us_market_open():
            await asyncio.sleep(
                WATCH_LOOP_SECONDS
            )
            continue

        now = time.time()

        for key, watch in list(
            PENDING_WATCHES.items()
        ):
            try:
                elapsed = (
                    now
                    - watch["last_checked_at"]
                )

                if elapsed < WATCH_INTERVAL_SECONDS:
                    continue

                watch["last_checked_at"] = now

                symbol = watch["symbol"]
                side = watch["side"]
                strike = watch["strike"]
                expiry_date = watch["expiry_date"]
                chat_id = watch["chat_id"]

                # Do not cancel a watch because the broader stock bias changed.
                # The selected CALL/PUT is judged directly on 1H confirmation.
                intraday = (
                    await asyncio.to_thread(
                        get_intraday_1h,
                        symbol
                    )
                )

                status, reason = (
                    check_intraday_confirmation(
                        side,
                        intraday
                    )
                )

                if status == "WAIT":
                    continue

                if status == "INVALID":
                    await application.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "🔴 تم استبعاد الفرصة\n\n"
                            f"{symbol} "
                            f"{side} "
                            f"{strike:g} | "
                            f"{expiry_date}\n"
                            f"💡 {reason}"
                        )
                    )

                    PENDING_WATCHES.pop(
                        key,
                        None
                    )
                    continue

                option_now = (
                    await asyncio.to_thread(
                        get_matching_contract,
                        symbol,
                        watch["option_symbol"]
                    )
                )

                if not option_now:
                    continue

                ask_now = option_now["ask"]

                if ask_now > MAX_OPTION_ASK:
                    await application.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "⚠️ تحقق التأكيد الفني "
                            "لكن السعر تجاوز $5\n\n"
                            f"{symbol} "
                            f"{side} "
                            f"{strike:g} | "
                            f"{expiry_date}\n"
                            f"💵 Ask ${ask_now:.2f}"
                        )
                    )

                    PENDING_WATCHES.pop(
                        key,
                        None
                    )
                    continue

                await application.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "🟢 تحقق تأكيد البوت\n\n"
                        f"{symbol} "
                        f"{side} "
                        f"{strike:g} | "
                        f"{expiry_date}\n"
                        f"💵 Ask ${ask_now:.2f}\n"
                        f"📊 Volume {option_now['volume']:,}\n"
                        f"💡 {reason}\n\n"
                        "📊 اعتمادًا على آخر "
                        "بيانات الساعة المتاحة."
                    )
                )

                PENDING_WATCHES.pop(
                    key,
                    None
                )

            except Exception as e:
                print(
                    "WATCH ERROR:",
                    key,
                    e
                )

        await asyncio.sleep(
            WATCH_LOOP_SECONDS
        )



# =========================================================
# TOP OPPORTUNITIES — SHARED OPPORTUNITY SCORE + ENTRY ZONE
# =========================================================

def get_chart_scenario_factor(symbol, selected_side):
    """Soft chart-scenario adjustment for Best Opportunities.

    The chart never hard-rejects a CALL/PUT. A matching scenario adds up to
    +8 points, an opposing scenario subtracts up to -8 points, and SIDEWAYS
    is neutral. The adjustment scales with forecast confidence.
    """
    side = str(selected_side or "").upper()
    if side not in ("CALL", "PUT"):
        return {
            "adjustment": 0,
            "scenario": "UNAVAILABLE",
            "confidence": 0,
            "aligned": None,
        }

    try:
        data = get_chart_data(symbol, "D")
        forecast = build_forecast(
            [float(x) for x in data["closes"]],
            [float(x) for x in data["highs"]],
            [float(x) for x in data["lows"]],
            "D",
        )

        scenario = str(forecast.get("scenario", "SIDEWAYS")).upper()
        confidence = float(forecast.get("confidence", 0) or 0)

        if scenario == "SIDEWAYS":
            adjustment = 0
            aligned = None
        else:
            expected_side = "CALL" if scenario == "BULLISH" else "PUT"
            aligned = side == expected_side

            # Forecast confidence currently lives roughly in the 48–88 range.
            # Convert it softly to a 4–8 point influence.
            strength = round(4 + max(0.0, min(confidence, 88.0) - 48.0) / 10.0)
            strength = int(max(4, min(strength, 8)))
            adjustment = strength if aligned else -strength

        return {
            "adjustment": adjustment,
            "scenario": scenario,
            "confidence": int(round(confidence)),
            "aligned": aligned,
        }

    except Exception as exc:
        print("TOP OPPORTUNITY CHART FACTOR ERROR:", symbol, exc, flush=True)
        return {
            "adjustment": 0,
            "scenario": "UNAVAILABLE",
            "confidence": 0,
            "aligned": None,
        }


def opportunity_metrics(symbol, trend=None, contracts=None, selected_side=None):
    """Shared opportunity score used by both 🎯 and 🏆."""
    if trend is None:
        trend = get_stock_trend(symbol)

    price = float(trend.get("last_close", trend.get("price", 0)) or 0)
    bias = str(trend.get("bias", "NEUTRAL")).upper()
    momentum_score = float(trend.get("momentum_score", 0) or 0)

    if price <= 0:
        raise ValueError("تعذر الحصول على سعر السهم بشكل صحيح.")

    if bias not in ("CALL", "PUT", "NEUTRAL"):
        bias = "NEUTRAL"

    trend_points = 38 if bias in ("CALL", "PUT") else 18
    momentum_points = min(max(momentum_score, 0) / 10.0, 1.0) * 22

    participation_points = 0
    if contracts is None:
        try:
            chain = get_option_chain(symbol)
            contracts = get_top_contracts(chain, trend)
        except Exception:
            contracts = []

    score_side = str(selected_side or bias).upper()
    side_contracts = [
        c for c in (contracts or [])
        if str(c.get("side", "")).upper() == score_side
    ]
    if side_contracts:
        best_score = max(float(c.get("score", 0) or 0) for c in side_contracts)
        participation_points = min(best_score / 100.0, 1.0) * 18

    # Direction is a soft bonus/penalty only. No CALL/PUT is rejected here.
    if score_side in ("CALL", "PUT") and bias in ("CALL", "PUT"):
        trend_points = 38 if score_side == bias else 24
    elif score_side in ("CALL", "PUT"):
        trend_points = 28

    market_open = is_us_market_open()
    live_points = 0
    live_status = "CLOSED"

    confirm_side = str(selected_side or bias).upper()
    if market_open and confirm_side in ("CALL", "PUT"):
        try:
            # Best Opportunities uses 1-hour candles for live confirmation.
            intraday = get_intraday_1h(symbol)
            status, _ = check_intraday_confirmation(confirm_side, intraday)
            live_status = status
            if status == "CONFIRMED":
                live_points = 12
            elif status == "INVALID":
                live_points = -15
        except Exception as exc:
            print("TOP OPPORTUNITY LIVE CONFIRM ERROR:", symbol, exc, flush=True)
            live_status = "UNAVAILABLE"

    chart_factor = get_chart_scenario_factor(symbol, score_side)
    chart_points = int(chart_factor.get("adjustment", 0) or 0)

    score = int(round(max(0, min(
        trend_points
        + momentum_points
        + participation_points
        + live_points
        + chart_points,
        100
    ))))

    if not market_open:
        score = min(score, 74)
    if market_open and live_points < 0:
        score = min(score, 49)

    return {
        "score": score,
        "market_open": market_open,
        "live_status": live_status,
        "bias": bias,
        "momentum_score": momentum_score,
        "participation": participation_points > 0,
        "chart_scenario": chart_factor.get("scenario", "UNAVAILABLE"),
        "chart_confidence": chart_factor.get("confidence", 0),
        "chart_adjustment": chart_points,
        "chart_aligned": chart_factor.get("aligned"),
    }


def get_entry_zone_status(symbol, bias):
    """Entry zone based on the underlying stock; 1h primary, daily fallback."""
    last_exc = None

    for resolution in ("60", "D"):
        try:
            data = get_chart_data(symbol, resolution)

            highs = [float(x) for x in data["highs"]]
            lows = [float(x) for x in data["lows"]]
            closes = [float(x) for x in data["closes"]]

            if len(closes) < 18:
                continue

            last_price = closes[-1]
            recent_n = min(18, len(closes))
            recent_highs = highs[-recent_n:]
            recent_lows = lows[-recent_n:]

            recent_ranges = [h - l for h, l in zip(recent_highs, recent_lows)]
            avg_range = (
                sum(recent_ranges) / len(recent_ranges)
                if recent_ranges
                else max(last_price * 0.005, 0.01)
            )

            zone_height = max(avg_range * 0.55, last_price * 0.0025)

            if bias == "CALL":
                anchor_price = min(recent_lows)
                zone_low = anchor_price
                zone_high = anchor_price + zone_height

                if zone_low <= last_price <= zone_high:
                    status = "INSIDE"
                elif last_price > zone_high:
                    distance_pct = ((last_price - zone_high) / last_price) * 100
                    status = "NEAR" if distance_pct <= 1.25 else "WAIT"
                else:
                    status = "INVALID"

            elif bias == "PUT":
                anchor_price = max(recent_highs)
                zone_low = anchor_price - zone_height
                zone_high = anchor_price

                if zone_low <= last_price <= zone_high:
                    status = "INSIDE"
                elif last_price < zone_low:
                    distance_pct = ((zone_low - last_price) / last_price) * 100
                    status = "NEAR" if distance_pct <= 1.25 else "WAIT"
                else:
                    status = "INVALID"
            else:
                return {"status": "UNKNOWN", "zone_text": "—", "source_resolution": resolution}

            zone_text = "\u2066" + f"${zone_low:.2f}–${zone_high:.2f}" + "\u2069"
            return {
                "status": status,
                "zone_low": zone_low,
                "zone_high": zone_high,
                "last_price": last_price,
                "zone_text": zone_text,
                "source_resolution": resolution,
            }

        except Exception as exc:
            last_exc = exc
            print("ENTRY ZONE DATA ERROR:", symbol, "resolution=", resolution, repr(exc), flush=True)

    print("ENTRY ZONE UNAVAILABLE:", symbol, repr(last_exc), flush=True)
    return {"status": "UNKNOWN", "zone_text": "—", "source_resolution": None}


# =========================================================
# SCAN TOP 10
# =========================================================

def scan_top10():
    now = time.time()
    cached = TOP10_CACHE["results"]

    if (
        cached is not None
        and (
            now - TOP10_CACHE["time"]
        ) < CACHE_SECONDS
    ):
        return cached

    results = []

    for symbol in SCAN_SYMBOLS:
        try:
            # TOP OPPORTUNITIES must evaluate CALL and PUT fairly.
            # Stock direction is a score factor only, not a qualification gate.
            trend = get_stock_trend(symbol)
            data = get_option_chain(symbol)
            contracts = get_top_contracts(
                data,
                trend,
                soft_direction=True,
            )

            if not contracts:
                continue

            matching = [
                contract
                for contract in contracts
                if (
                    contract["score"] >= MIN_TOP_SCORE
                    and contract["uoa_score"] >= MIN_TOP_UOA
                    and contract["volume"] >= MIN_VOLUME
                )
            ]

            if not matching:
                continue

            # Pick the strongest live contract regardless of pre-set stock direction.
            matching.sort(
                key=lambda x: (
                    -x["internal_score"],
                    -x["uoa_score"],
                    x["spread_pct"],
                    -x["volume"],
                )
            )
            contract = matching[0]
            selected_side = str(contract.get("side", "")).upper()

            # Direction alignment can improve the opportunity score, but disagreement
            # is never an automatic rejection.
            metrics = opportunity_metrics(
                symbol,
                trend=trend,
                contracts=contracts,
                selected_side=selected_side,
            )
            entry_zone = get_entry_zone_status(symbol, selected_side)

            # Opportunity-specific decision: contract/liquidity quality qualifies it.
            # Trend disagreement only lowers rank/score softly; it does not remove it.
            aligned = selected_side == str(trend.get("bias", "NEUTRAL")).upper()
            if contract["score"] >= 88 and contract["uoa_score"] >= 5:
                decision_rank = 2
                decision_label = "🟢 تأكيد دخول" if is_us_market_open() else "🟢 مرشح قوي — انتظار افتتاح السوق"
            else:
                decision_rank = 1
                decision_label = "🟡 انتظار تأكيد"

            decision = {
                "label": decision_label,
                "reason": (
                    "اتجاه السهم عامل ترجيح فقط"
                    if not aligned
                    else "العقد متوافق مع الاتجاه الحالي"
                ),
                "rank": decision_rank,
            }

            result = {
                "symbol": symbol,
                "trend": trend,
                "contract": contract,
                "contracts": contracts,
                "internal_score": contract["internal_score"],
                "decision": decision,
                "opportunity_score": metrics["score"],
                "market_open": metrics["market_open"],
                "entry_zone": entry_zone,
            }

            results.append(result)

        except Exception as e:
            print(
                f"SCAN ERROR {symbol}: {e}"
            )

    results.sort(
        key=lambda x: (
            -x["decision"]["rank"],
            -x["opportunity_score"],
            -x["internal_score"],
            -x["contract"]["uoa_score"],
            x["contract"]["spread_pct"]
        )
    )

    top10 = results[:TOP_N_RESULTS]

    TOP10_CACHE["time"] = now
    TOP10_CACHE["results"] = top10

    return top10


# =========================================================
# CONTRACT INPUT — SHORT FORMAT + AUTO MARKET DATA
# =========================================================

CONTRACT_INPUT_RE = re.compile(
    r"^\s*([A-Za-z]{1,6})\s+"
    r"(CALL|PUT|C|P)\s+"
    r"(\d+(?:\.\d+)?)\s*"
    r"(?:\|\s*)?"
    r"(\d{1,2})[\s/\-]+"
    r"([A-Za-z]{3,9}|\d{1,2})[\s/\-]+"
    r"(\d{2,4})\s*$",
    re.I,
)

MONTH_ALIASES = {
    "JAN": 1, "JANUARY": 1,
    "FEB": 2, "FEBRUARY": 2,
    "MAR": 3, "MARCH": 3,
    "APR": 4, "APRIL": 4,
    "MAY": 5,
    "JUN": 6, "JUNE": 6,
    "JUL": 7, "JULY": 7,
    "AUG": 8, "AUGUST": 8,
    "SEP": 9, "SEPT": 9, "SEPTEMBER": 9,
    "OCT": 10, "OCTOBER": 10,
    "NOV": 11, "NOVEMBER": 11,
    "DEC": 12, "DECEMBER": 12,
}


def parse_contract_short_input(value):
    match = CONTRACT_INPUT_RE.match(str(value or "").strip())
    if not match:
        raise ValueError(
            "صيغة العقد غير صحيحة.\n"
            "مثال: NVDA CALL 240 | 28 Aug 2026"
        )

    symbol = match.group(1).upper()
    side_raw = match.group(2).upper()
    side = "CALL" if side_raw in ("CALL", "C") else "PUT"
    strike = float(match.group(3))

    day = int(match.group(4))
    month_raw = match.group(5).upper()
    year = int(match.group(6))
    if year < 100:
        year += 2000

    if month_raw.isdigit():
        month = int(month_raw)
    else:
        month = MONTH_ALIASES.get(month_raw)

    if not month or not 1 <= month <= 12:
        raise ValueError("الشهر في تاريخ الانتهاء غير صحيح.")

    try:
        expiry = datetime(year, month, day).date()
    except ValueError:
        raise ValueError("تاريخ انتهاء العقد غير صحيح.")

    today = datetime.now(ZoneInfo("America/New_York")).date()
    if expiry < today:
        raise ValueError("تاريخ انتهاء العقد مضى.")

    return {
        "symbol": symbol,
        "side": side,
        "strike": strike,
        "expiry": expiry,
        "expiry_iso": expiry.isoformat(),
        "expiry_text": f"{expiry.day} {expiry.strftime('%b %Y')}",
    }



def get_option_quote_by_symbol(option_symbol):
    """Fetch the most current quote for one exact OCC option symbol."""
    option_symbol = str(option_symbol or "").strip()
    if not option_symbol:
        return None

    url = f"https://api.marketdata.app/v1/options/quotes/{option_symbol}/"
    response = requests.get(
        url,
        headers=get_headers(),
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("s") != "ok":
        return None

    def first(name, default=0):
        values = data.get(name, [])
        if not values:
            return default
        value = values[0]
        return default if value is None else value

    bid = float(first("bid", 0) or 0)
    ask = float(first("ask", 0) or 0)
    mid = float(first("mid", 0) or 0)
    last = float(first("last", 0) or 0)
    volume = int(float(first("volume", 0) or 0))
    oi = int(float(first("openInterest", 0) or 0))
    delta = float(first("delta", 0) or 0)
    updated = int(float(first("updated", 0) or 0))
    # Some APIs may expose epoch milliseconds; normalize to seconds.
    if updated > 10_000_000_000:
        updated = int(updated / 1000)

    if mid <= 0 and bid > 0 and ask > 0:
        mid = (bid + ask) / 2

    spread_pct = (
        ((ask - bid) / mid) * 100
        if bid >= 0 and ask > 0 and mid > 0
        else 999.0
    )

    age_minutes = None
    if updated > 0:
        try:
            age_minutes = max(0.0, (time.time() - updated) / 60.0)
        except Exception:
            age_minutes = None

    return {
        "option_symbol": str(first("optionSymbol", option_symbol)),
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last": last,
        "volume": volume,
        "oi": oi,
        "delta": delta,
        "updated": updated,
        "quote_age_minutes": age_minutes,
        "spread_pct": spread_pct,
    }


def option_quote_freshness_text(contract):
    """Arabic label for the age/time of the exact option quote."""
    updated = int(float(contract.get("updated", 0) or 0)) if contract else 0
    age = contract.get("quote_age_minutes") if contract else None

    if updated > 10_000_000_000:
        updated = int(updated / 1000)

    if age is None and updated > 0:
        try:
            age = max(0.0, (time.time() - updated) / 60.0)
        except Exception:
            age = None

    if updated > 0:
        try:
            time_text = datetime.fromtimestamp(
                updated, ZoneInfo("Asia/Riyadh")
            ).strftime("%I:%M %p").lstrip("0")
        except Exception:
            time_text = "—"
    else:
        time_text = "—"

    if age is None:
        status = "⚪ حداثة غير معروفة"
        age_text = ""
    elif age <= 2.0:
        status = "🟢 مباشر تقريبًا"
        age_text = ""
    elif age <= 20.0:
        status = "🟡 متأخر"
        age_text = ""
    else:
        status = "🔴 قديم"
        age_text = ""

    return f"🕐 السعر: {status}{age_text} | آخر تحديث {time_text}"


def option_quote_is_usable_now(contract):
    """Require a timestamped recent exact quote while the U.S. market is open."""
    if not contract:
        return False
    try:
        bid = float(contract.get("bid", 0) or 0)
        ask = float(contract.get("ask", 0) or 0)
        updated = int(float(contract.get("updated", 0) or 0))
        age = contract.get("quote_age_minutes")
        if updated > 10_000_000_000:
            updated = int(updated / 1000)
        if bid < 0 or ask <= 0 or ask < bid:
            return False
        if is_us_market_open():
            if updated <= 0 or age is None:
                return False
            if float(age) > MAX_LIVE_OPTION_QUOTE_AGE_MINUTES:
                return False
        return True
    except Exception:
        return False


def merge_fresh_option_quote(contract):
    """Update one selected contract with its exact option quote."""
    if not contract:
        return contract

    try:
        contract["exact_quote_ok"] = False
        fresh = get_option_quote_by_symbol(contract.get("option_symbol"))
        if not fresh:
            return contract

        # Never replace a usable quote with zero/invalid prices.
        if fresh.get("ask", 0) <= 0 or fresh.get("bid", 0) < 0:
            return contract

        for field in (
            "bid", "ask", "mid", "last", "volume", "oi", "delta",
            "updated", "quote_age_minutes", "spread_pct",
        ):
            contract[field] = fresh.get(field, contract.get(field))

        contract["exact_quote_ok"] = option_quote_is_usable_now(contract)

        # Recalculate quality/activity after the fresh quote.
        dte = int(contract.get("dte", 0) or 0)
        raw_score = contract_score(
            float(contract.get("delta", 0) or 0),
            int(contract.get("volume", 0) or 0),
            int(contract.get("oi", 0) or 0),
            float(contract.get("spread_pct", 999) or 999),
            dte,
        )
        base_score = normalize_contract_score(raw_score)
        contract["base_score"] = base_score

        uoa_score, volume_oi_ratio, uoa_label = unusual_activity_score(
            int(contract.get("volume", 0) or 0),
            int(contract.get("oi", 0) or 0),
        )
        contract["uoa_score"] = uoa_score
        contract["volume_oi_ratio"] = volume_oi_ratio
        contract["uoa_label"] = uoa_label
        contract["uoa_adjustment"] = round(uoa_score * 0.7)

        if "market_adjustment" in contract:
            contract["internal_score"] = (
                base_score
                + float(contract.get("market_adjustment", 0) or 0)
                + contract["uoa_adjustment"]
            )
            contract["score"] = max(
                0, min(round(contract["internal_score"]), 98)
            )
        else:
            contract["score"] = base_score

        print(
            "EXACT OPTION QUOTE:",
            contract.get("option_symbol"),
            "bid=", contract.get("bid"),
            "ask=", contract.get("ask"),
            "last=", contract.get("last"),
            "age_min=", contract.get("quote_age_minutes"),
            flush=True,
        )
    except Exception as exc:
        print(
            "EXACT OPTION QUOTE ERROR:",
            contract.get("option_symbol"), repr(exc), flush=True
        )

    return contract


def refresh_contracts_exact_quotes(contracts):
    """Refresh the already-ranked small contract list using exact symbols."""
    if not contracts:
        return contracts

    import copy
    refreshed = copy.deepcopy(contracts)
    for contract in refreshed:
        merge_fresh_option_quote(contract)

    if is_us_market_open():
        refreshed = [c for c in refreshed if option_quote_is_usable_now(c)]

    refreshed.sort(
        key=lambda x: (
            -float(x.get("internal_score", x.get("score", 0)) or 0),
            -int(x.get("uoa_score", 0) or 0),
            float(x.get("spread_pct", 999) or 999),
            -int(x.get("volume", 0) or 0),
        )
    )
    return refreshed


def get_exact_option_contract(symbol, side, strike, expiry):
    """Fetch only the requested expiry, then match side+strike exactly."""
    url = f"https://api.marketdata.app/v1/options/chain/{symbol}/"

    response = requests.get(
        url,
        headers=get_headers(),
        params={
            "from": expiry.isoformat(),
            "to": expiry.isoformat(),
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("s") != "ok":
        raise ValueError(
            data.get("errmsg", "تعذر الحصول على بيانات العقد.")
        )

    option_symbols = data.get("optionSymbol", [])
    count = len(option_symbols)

    for i in range(count):
        try:
            item_side = str(data.get("side", [])[i]).upper()
            item_strike = float(data.get("strike", [])[i])
            item_expiration = int(data.get("expiration", [])[i])

            item_expiry = datetime.fromtimestamp(
                item_expiration,
                ZoneInfo("America/New_York"),
            ).date()

            if item_side != side:
                continue
            if abs(item_strike - strike) > 0.001:
                continue
            if item_expiry != expiry:
                continue

            def field(name, default=0):
                values = data.get(name, [])
                if i >= len(values):
                    return default
                value = values[i]
                return default if value is None else value

            bid = float(field("bid", 0) or 0)
            ask = float(field("ask", 0) or 0)
            mid = float(field("mid", 0) or 0)
            volume = int(float(field("volume", 0) or 0))
            oi = int(float(field("openInterest", 0) or 0))
            delta = float(field("delta", 0) or 0)

            if mid <= 0 and bid > 0 and ask > 0:
                mid = (bid + ask) / 2

            if bid > 0 and ask > 0 and mid > 0:
                spread_pct = ((ask - bid) / mid) * 100
            else:
                spread_pct = 999.0

            today = datetime.now(
                ZoneInfo("America/New_York")
            ).date()
            dte = max((expiry - today).days, 0)

            raw_score = contract_score(
                delta,
                volume,
                oi,
                spread_pct,
                dte,
            )
            score = normalize_contract_score(raw_score)

            contract = {
                "option_symbol": str(option_symbols[i]),
                "symbol": symbol,
                "side": side,
                "strike": strike,
                "expiry": expiry,
                "expiry_text": f"{expiry.day} {expiry.strftime('%b %Y')}",
                "dte": dte,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "last": float(field("last", 0) or 0),
                "volume": volume,
                "oi": oi,
                "delta": delta,
                "updated": int(float(field("updated", 0) or 0)),
                "spread_pct": spread_pct,
                "score": score,
            }
            return merge_fresh_option_quote(contract)

        except (ValueError, TypeError, IndexError):
            continue

    return None


def format_exact_contract_evaluation(contract):
    spread = float(contract.get("spread_pct", 999) or 999)
    delta = abs(float(contract.get("delta", 0) or 0))
    volume = int(contract.get("volume", 0) or 0)
    oi = int(contract.get("oi", 0) or 0)
    dte = int(contract.get("dte", 0) or 0)
    score = int(contract.get("score", 0) or 0)

    spread_text = (
        f"{spread:.1f}%"
        if spread < 900
        else "غير متوفر"
    )

    strengths = []
    weaknesses = []

    if 0.35 <= delta <= 0.65:
        strengths.append("دلتا مناسبة للحركة")
    elif delta < 0.25:
        weaknesses.append("الدلتا منخفضة")
    elif delta > 0.75:
        weaknesses.append("الدلتا مرتفعة")

    if volume >= 2000:
        strengths.append("فوليوم جيد")
    elif volume < 500:
        weaknesses.append("الفوليوم منخفض")

    if oi >= 1000:
        strengths.append("Open Interest جيد")
    elif oi < 500:
        weaknesses.append("Open Interest منخفض")

    if spread <= 3:
        strengths.append("سبريد ممتاز")
    elif spread > 8 and spread < 900:
        weaknesses.append("السبريد واسع")
    elif spread >= 900:
        weaknesses.append("بيانات السبريد غير مكتملة")

    if 7 <= dte <= 21:
        strengths.append("مدة العقد مناسبة")
    elif dte < 5:
        weaknesses.append("الوقت المتبقي قصير")
    elif dte > 30:
        weaknesses.append("مدة العقد طويلة")

    if score >= 85:
        verdict = "🟢 عقد قوي"
    elif score >= 70:
        verdict = "🟡 عقد مقبول ويحتاج متابعة"
    else:
        verdict = "🔴 جودة العقد ضعيفة حاليًا"

    strength_text = (
        "\n".join(f"• {x}" for x in strengths)
        if strengths else "• لا توجد نقطة قوة بارزة"
    )
    weakness_text = (
        "\n".join(f"• {x}" for x in weaknesses)
        if weaknesses else "• لا توجد ملاحظة سلبية بارزة"
    )

    return (
        "📊 تقييم عقد أوبشن\n\n"
        f"📌 {contract['symbol']} {contract['side']} "
        f"{contract['strike']:g} | {contract['expiry_text']}\n\n"
        f"💵 Bid: ${contract['bid']:.2f}\n"
        f"💵 Ask: ${contract['ask']:.2f}\n"
        f"⚖️ Mid: ${contract['mid']:.2f}\n"
        f"🧾 Last: ${float(contract.get('last', 0) or 0):.2f}\n"
        f"{option_quote_freshness_text(contract)}\n"
        f"📐 Delta: {contract['delta']:.3f}\n"
        f"📊 Volume: {volume:,}\n"
        f"🏦 OI: {oi:,}\n"
        f"↔️ Spread: {spread_text}\n"
        f"⏳ DTE: {dte}\n\n"
        "━━━━━━━━━━━━━━\n"
        f"⭐ جودة العقد: {score}/100\n"
        f"{verdict}\n"
        "━━━━━━━━━━━━━━\n\n"
        "✅ نقاط القوة:\n"
        f"{strength_text}\n\n"
        "⚠️ الملاحظات:\n"
        f"{weakness_text}\n\n"
        "ℹ️ هذا تقييم لجودة العقد نفسه؛ "
        "قرار الدخول يعتمد أيضًا على تقييم فرصة السهم."
    )


# =========================================================
# MENUS
# =========================================================

def main_menu():
    keyboard = [
        [
            InlineKeyboardButton(
                "🏆 أفضل فرص اليوم",
                callback_data="top10"
            )
        ],
        [
            InlineKeyboardButton(
                "🔎 ابحث عن أفضل العقود",
                callback_data="scan_options"
            )
        ],
        [
            InlineKeyboardButton(
                "📊 تقييم عقد أوبشن",
                callback_data="contract"
            )
        ],
        [
            InlineKeyboardButton(
                "🎯 تقييم فرصة",
                callback_data="opportunity"
            )
        ],
        [
            InlineKeyboardButton(
                "🔮 الشارت المتوقع",
                callback_data="chart"
            )
        ],
        [
            InlineKeyboardButton(
                "📰 أهم الأخبار",
                callback_data="news"
            )
        ],
        [
            InlineKeyboardButton(
                "📅 إعلانات الشركات",
                callback_data="earnings"
            )
        ],
        [
            InlineKeyboardButton(
                "ℹ️ طريقة الاستخدام",
                callback_data="help"
            )
        ],
    ]

    return InlineKeyboardMarkup(
        keyboard
    )


# =========================================================
# FORMATTERS
# =========================================================

def refresh_top10_contract_prices(results):
    """Refresh every displayed winner from its exact OCC option quote."""
    if not results:
        return results

    import copy
    refreshed_results = copy.deepcopy(results)

    usable_results = []
    for item in refreshed_results:
        try:
            contract = item.get("contract", {})
            if not contract:
                continue
            merge_fresh_option_quote(contract)
            if not option_quote_is_usable_now(contract):
                print(
                    "PRICE EXCLUDED STALE/UNKNOWN:",
                    item.get("symbol"), contract.get("option_symbol"),
                    "updated=", contract.get("updated"),
                    "age_min=", contract.get("quote_age_minutes"),
                    flush=True,
                )
                continue
            item["internal_score"] = contract.get(
                "internal_score", item.get("internal_score", 0)
            )
            usable_results.append(item)
        except Exception as exc:
            print("PRICE REFRESH ERROR:", item.get("symbol"), repr(exc), flush=True)

    return usable_results


def format_top10(results):
    if not results:
        return "❌ لا توجد فرص مؤهلة لشروطنا حاليًا."

    market_open = any(item.get("market_open") for item in results)

    lines = [
        f"🏆 أفضل {len(results)} فرص لليوم",
        "🕒 السوق مفتوح — تأكيد ساعة لكل فرصة" if market_open else "🕒 السوق مغلق — تأكيد ساعة بعد الافتتاح",
        "📍 🟢 مناسب | 🟡 قريب | ⏳ بعيد",
        "━━━━━━━━━━━━",
        "",
    ]

    for idx, item in enumerate(results, 1):
        contract = item["contract"]
        zone = item.get("entry_zone", {})
        status = zone.get("status", "UNKNOWN")
        zone_text = zone.get("zone_text", "—")

        icon = {"INSIDE": "🟢", "NEAR": "🟡", "WAIT": "⏳", "INVALID": "🔴"}.get(status, "⚪")
        entry_text = f"{icon} {zone_text}" if zone_text != "—" else f"{icon} —"

        lines.extend([
            f"{idx}️⃣ {item['symbol']} {contract['side']} {contract['strike']:g} | {format_expiry_timestamp(contract['expiration'])}",
            f"🎯 قوة فرصة السهم {item['opportunity_score']}%",
            f"⭐ جودة العقد {contract['score']}%",
            f"🔥 النشاط {contract['uoa_score']}/10",
            entry_text,
            f"💵 ${contract['ask']:.2f} | 📊 {contract['volume']:,}",
            option_quote_freshness_text(contract),
            "",
            "━━━━━━━━━━━━",
            "",
        ])

    return "\n".join(lines).strip()

def format_top_contracts(
    symbol,
    contracts,
    trend
):
    qualified = [
        contract
        for contract in contracts
        if (
            contract["score"] >= MIN_TOP_SCORE
            and contract["uoa_score"] >= MIN_TOP_UOA
            and contract["volume"] >= MIN_VOLUME
            and contract["decision"]["rank"] > 0
        )
    ]

    qualified = qualified[:TOP_N_RESULTS]

    if not qualified:
        return (
            f"🔎 أفضل العقود لـ {symbol}\n"
            f"📊 {trend['label']} | "
            f"زخم {trend['momentum_score']}/10 | "
            f"{trend['continuation_label']}\n"
            f"💵 ${trend['last_close']:.2f}\n"
            f"━━━━━━━━━━━━━━\n\n"
            "❌ لا توجد عقود مؤهلة "
            "لشروطنا حاليًا."
        )

    message = (
        f"🔎 أفضل العقود لـ {symbol}\n"
        f"📊 {trend['label']} | "
        f"زخم {trend['momentum_score']}/10 | "
        f"{trend['continuation_label']}\n"
        f"💵 ${trend['last_close']:.2f}\n"
        f"━━━━━━━━━━━━━━\n\n"
    )

    for index, contract in enumerate(
        qualified,
        start=1
    ):
        decision = effective_decision(
            contract,
            trend
        )

        message += (
            f"{index}️⃣ "
            f"{symbol} "
            f"{contract['side']} "
            f"{contract['strike']:g} | "
            f"{contract['expiry_date']}\n"
            f"{decision['label']}\n"
            f"⭐ تقييم "
            f"{contract['score']}/100 | "
            f"🔥 نشاط "
            f"{contract['uoa_score']}/10\n"
            f"💵 Ask "
            f"${contract['ask']:.2f} | "
            f"📊 Volume "
            f"{contract['volume']:,}\n"
            f"{option_quote_freshness_text(contract)}\n\n"
            f"━━━━━━━━━━━━━━\n\n"
        )

    return message


def format_watch_added(
    symbol,
    contract,
    trend
):
    if not is_us_market_open():
        watch_message = (
            "👀 تمت إضافة العقد "
            "للمراقبة بعد افتتاح السوق"
        )
    else:
        watch_message = (
            "👀 تمت إضافة العقد للمراقبة على الساعة"
        )

    return (
        f"{watch_message}\n\n"
        f"{symbol} "
        f"{contract['side']} "
        f"{contract['strike']:g} | "
        f"{contract['expiry_date']}\n"
        f"⭐ تقييم "
        f"{contract['score']}/100 | "
        f"🔥 نشاط "
        f"{contract['uoa_score']}/10\n"
        f"💵 Ask "
        f"${contract['ask']:.2f} | "
        f"📊 Volume "
        f"{contract['volume']:,}\n"
        f"{option_quote_freshness_text(contract)}\n\n"
        "⏱️ التحقق كل 5 دقائق اعتمادًا على بيانات الساعة"
    )


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not _allowed(update):
        await deny_access(update)
        return

    await update.message.reply_text(
        "🤖 بوت تحليل واختيار عقود الأوبشن\n\n"
        "اختر الخدمة المطلوبة:",
        reply_markup=main_menu()
    )


# =========================================================
# BUTTONS
# =========================================================

async def buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not _allowed(update):
        await deny_access(update)
        return

    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id

    if query.data == "top10":
        await query.message.reply_text(
            "🏆 جاري فحص 30 رمزًا مختارًا...\n"
            "⏳ تقدر تستخدم البوت أثناء الفحص."
        )

        try:
            results = (
                await asyncio.to_thread(
                    scan_top10
                )
            )

            # Keep the expensive analysis cache, but refresh the displayed
            # winning contracts with current market Bid/Ask/Volume.
            results = await asyncio.to_thread(
                refresh_top10_contract_prices,
                results,
            )

            await query.message.reply_text(
                format_top10(results),
                reply_markup=main_menu()
            )

            watches_added = []

            for item in results:
                contract = item["contract"]
                trend = item["trend"]

                added = add_pending_watch(
                    chat_id,
                    item["symbol"],
                    contract,
                    trend
                )

                if added:
                    watches_added.append(
                        (
                            item["symbol"],
                            contract,
                            trend
                        )
                    )

            if watches_added:
                lines = []

                for (
                    symbol,
                    contract,
                    trend
                ) in watches_added:
                    decision = (
                        effective_decision(
                            contract,
                            trend
                        )
                    )

                    lines.append(
                        f"• {symbol} "
                        f"{contract['side']} "
                        f"{contract['strike']:g} | "
                        f"{contract['expiry_date']}"
                    )

                watch_text = (
                    "👀 فرص تحت المراقبة:\n\n"
                    + "\n\n".join(lines)
                    + "\n\n"
                    "⏱️ التحقق كل 5 دقائق أثناء السوق"
                )

                await query.message.reply_text(
                    watch_text
                )

        except Exception as e:
            print(
                "TOP10 ERROR:",
                e
            )

            await query.message.reply_text(
                "⚠️ تعذر إكمال الفحص حاليًا.",
                reply_markup=main_menu()
            )

    elif query.data == "scan_options":
        context.user_data["mode"] = "scan"

        await query.message.reply_text(
            "🔎 اكتب رمز الشركة:\n\n"
            "مثال:\nNVDA"
        )

    elif query.data == "contract":
        context.user_data["mode"] = "contract"

        await query.message.reply_text(
            "📊 أرسل العقد بهذه الصيغة:\n\n"
            "NVDA CALL 240 | 28 Aug 2026\n\n"
            "أو:\n"
            "TSLA PUT 330 | 04 Sep 2026\n\n"
            "وسأسحب بيانات العقد تلقائيًا."
        )

    elif query.data == "opportunity":
        context.user_data["mode"] = "opportunity"

        await query.message.reply_text(
            "🎯 اكتب رمز السهم فقط:\n\n"
            "مثال: TSLA\n\n"
            "وسأحلل الفرصة تلقائيًا."
        )

    elif query.data == "chart":
        context.user_data["mode"] = "chart"

        await query.message.reply_text(
            "🔮 اكتب رمز السهم:\n\n"
            "مثال:\nTSLA"
        )

    elif query.data.startswith("chart_"):
        symbol = context.user_data.get(
            "chart_symbol"
        )

        if not symbol:
            await query.message.reply_text(
                "⚠️ اختر الشارت المتوقع "
                "واكتب الرمز أولًا.",
                reply_markup=main_menu()
            )
            return

        resolution = query.data.split(
            "_",
            1
        )[1]

        await query.message.reply_text(
            f"🔮 جاري بناء المسار المتوقع لـ "
            f"{symbol}..."
        )

        try:
            (
                chart_image,
                caption
            ) = (
                await asyncio.to_thread(
                    make_chart,
                    symbol,
                    resolution
                )
            )

            await query.message.reply_photo(
                photo=chart_image,
                caption=caption,
                reply_markup=main_menu()
            )

        except Exception as e:
            print(
                "CHART ERROR:",
                e
            )

            await query.message.reply_text(
                "⚠️ تعذر إنشاء الشارت المتوقع حاليًا.",
                reply_markup=main_menu()
            )

    elif query.data == "news":
        await query.message.reply_text(
            "📰 جاري فحص الأخبار المهمة "
            "لأسهمنا..."
        )

        try:
            # Manual news button must run a fresh scan, not reuse the monitor/cache.
            # Otherwise a cached partial result can hide valid TSLA events.
            NEWS_CACHE["time"] = 0
            NEWS_CACHE["results"] = None

            news_text = await asyncio.to_thread(
                format_combined_news
            )
            news_chunks = split_telegram_text(news_text)
            for i, chunk in enumerate(news_chunks):
                await query.message.reply_text(
                    chunk,
                    reply_markup=main_menu() if i == len(news_chunks) - 1 else None,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )

        except Exception as e:
            print(
                "NEWS BUTTON ERROR:",
                e
            )

            await query.message.reply_text(
                "⚠️ تعذر جلب الأخبار مؤقتًا. تم تسجيل سبب الخطأ في Render Logs.",
                reply_markup=main_menu()
            )

    elif query.data == "earnings":
        await query.message.reply_text(
            "📅 جاري فحص إعلانات "
            "أسهمنا القادمة..."
        )

        try:
            results = (
                await asyncio.to_thread(
                    get_earnings_calendar,
                    EARNINGS_LOOKAHEAD_DAYS
                )
            )

            await query.message.reply_text(
                format_earnings_results(
                    results
                ),
                reply_markup=main_menu()
            )

        except Exception as e:
            print(
                "EARNINGS BUTTON ERROR:",
                e
            )

            await query.message.reply_text(
                "⚠️ تعذر جلب إعلانات الشركات حاليًا. "
                "تأكدي أن FINNHUB_TOKEN موجود في Render.",
                reply_markup=main_menu()
            )

    elif query.data == "help":
        await query.message.reply_text(
            "ℹ️ طريقة الاستخدام\n\n"
            "🏆 أفضل فرص اليوم: يفحص 30 رمزًا مختارًا.\n\n"
            "🔎 البحث اليدوي: اكتب رمز أي سهم.\n\n"
            "🔮 الشارت المتوقع: يعرض الشموع ومناطق "
            "الدعم والمقاومة ومسار الحركة المرجح.\n\n"
            "📰 أهم الأخبار: يعرض الأخبار المهمة "
            "فقط لأسهم قائمتنا، مع تقييم الأهمية "
            "والتأثير.\n\n"
            "📅 إعلانات الشركات: يعرض الإعلانات "
            "القادمة لأسهمنا، مع تنبيه تلقائي قبل "
            "7 أيام وتذكير في نفس يوم الإعلان.\n\n"
            "اليومي: 10 جلسات قادمة.\n"
            "4 ساعات: 12 شمعة.\n"
            "الساعة: 12 شمعة.\n"
            "15 دقيقة: 12 شمعة.\n\n"
            "🟢 تأكيد الدخول يظهر أثناء السوق فقط.\n"
            "🟢 مرشح قوي: انتظار افتتاح السوق.\n"
            "🟡 انتظار تأكيد: يدخل المراقبة تلقائيًا.\n"
            "⏱️ التحقق كل 5 دقائق أثناء السوق.\n\n"
            "⭐ تقييم 80+\n"
            "🔥 نشاط 3+\n"
            "📊 Volume 1,000+\n"
            "💰 Ask ≤ $5\n"
            "🧭 مع اتجاه السهم",
            reply_markup=main_menu()
        )



def build_opportunity_evaluation(symbol):
    trend = get_stock_trend(symbol)

    price = float(
        trend.get("last_close", trend.get("price", 0))
        or 0
    )
    bias = str(
        trend.get("bias", "NEUTRAL")
    ).upper()
    momentum_score = float(
        trend.get("momentum_score", 0)
        or 0
    )

    if price <= 0:
        raise ValueError(
            "تعذر الحصول على سعر السهم بشكل صحيح، لذلك لم يتم تقييم الفرصة."
        )

    if bias not in ("CALL", "PUT", "NEUTRAL"):
        bias = "NEUTRAL"

    market_open = is_us_market_open()

    if bias == "CALL":
        direction_text = "🟢 صاعد / CALL"
        trend_points = 38
    elif bias == "PUT":
        direction_text = "🔴 هابط / PUT"
        trend_points = 38
    else:
        direction_text = "🟡 محايد"
        trend_points = 18

    momentum_points = min(
        max(momentum_score, 0) / 10.0,
        1.0
    ) * 22

    if momentum_score >= 7:
        momentum_text = "قوي"
    elif momentum_score >= 4:
        momentum_text = "متوسط"
    else:
        momentum_text = "هادئ"

    participation_points = 0
    participation_text = "غير مكتمل"

    try:
        chain = get_option_chain(symbol)
        contracts = get_top_contracts(
            chain,
            trend
        )

        same_side = [
            contract
            for contract in contracts
            if str(
                contract.get("side", "")
            ).upper() == bias
        ]

        if same_side:
            best_score = max(
                float(
                    contract.get("score", 0)
                    or 0
                )
                for contract in same_side
            )

            participation_points = min(
                best_score / 100.0,
                1.0
            ) * 18

            participation_text = "متوفر"

    except Exception as exc:
        print(
            "OPPORTUNITY CONFIRMATION ERROR:",
            exc
        )

    live_points = 0
    live_status = "غير متاح والسوق مغلق"
    live_reason = "ننتظر افتتاح السوق"

    if market_open and bias in ("CALL", "PUT"):
        try:
            intraday = get_intraday_15m(symbol)
            status, reason = check_intraday_confirmation(
                bias,
                intraday
            )

            if status == "CONFIRMED":
                live_points = 12
                live_status = "🟢 مؤكد على 15 دقيقة"
                live_reason = reason

            elif status == "INVALID":
                live_points = -15
                live_status = "🔴 ضد الفرصة"
                live_reason = reason

            else:
                live_points = 0
                live_status = "🟡 لم يتأكد بعد"
                live_reason = reason

        except Exception as exc:
            print(
                "OPPORTUNITY LIVE CONFIRM ERROR:",
                exc
            )
            live_status = "⚪ تعذر التحقق حيًا"
            live_reason = "بيانات 15 دقيقة غير مكتملة"

    base_score = (
        trend_points
        + momentum_points
        + participation_points
    )

    score = int(
        round(
            max(
                0,
                min(
                    base_score + live_points,
                    100
                )
            )
        )
    )

    # Closed market cannot be labelled strong.
    if not market_open:
        score = min(score, 74)

    # If live action invalidates the setup, cap the score sharply.
    if market_open and live_points < 0:
        score = min(score, 49)

    missing = []

    if not market_open:
        missing.append(
            "تأكيد الحركة بعد افتتاح السوق"
        )
    elif live_points == 0:
        missing.append(
            "تأكيد حي على إطار 15 دقيقة"
        )

    if participation_points <= 0:
        missing.append(
            "تأكيد مشاركة العقود"
        )

    if momentum_score < 7:
        missing.append(
            "زخم أقوى"
        )

    if (
        market_open
        and live_points == 12
        and score >= 82
    ):
        verdict = "🟢 فرصة قوية — الحركة الحية أكدت الاتجاه"

    elif (
        market_open
        and live_points < 0
    ):
        verdict = "🔴 الفرصة مرفوضة حاليًا — الحركة الحية ضدها"

    elif score >= 68:
        verdict = "🟡 السيناريو المرجح حاليًا — يحتاج تأكيد"

    elif score >= 52:
        verdict = "🟠 احتمال قائم — الأفضل الانتظار"

    else:
        verdict = "🔴 الفرصة ضعيفة حاليًا"

    reasons = [
        (
            "اتجاه فني واضح"
            if bias in ("CALL", "PUT")
            else "الاتجاه غير محسوم"
        ),
        f"زخم {momentum_text}",
        (
            "مشاركة العقود داعمة"
            if participation_points > 0
            else "مشاركة العقود غير مؤكدة"
        ),
    ]

    if market_open:
        reasons.append(
            f"الحركة الحية: {live_status}"
        )

    missing_text = (
        " + ".join(missing)
        if missing
        else "لا يوجد تأكيد رئيسي ناقص"
    )

    return (
        "🎯 تقييم فرصة\n\n"
        f"📌 السهم: {symbol}\n"
        f"💵 السعر: ${price:.2f}\n"
        f"🧭 الاتجاه: {direction_text}\n"
        f"⚡ الزخم: {momentum_text} "
        f"({momentum_score:.1f}/10)\n"
        f"📊 تأكيد المشاركة: {participation_text}\n"
        f"🕒 السوق: {'مفتوح' if market_open else 'مغلق'}\n"
        f"📍 تأكيد 15 دقيقة: {live_status}\n\n"
        "━━━━━━━━━━━━━━\n"
        f"⭐ قوة الفرصة: {score}/100\n"
        f"{verdict}\n"
        "━━━━━━━━━━━━━━\n\n"
        "🔎 سبب التقييم:\n"
        + "\n".join(
            f"• {reason}"
            for reason in reasons
        )
        + "\n\n"
        f"⏳ ينقصها: {missing_text}\n\n"
        "ℹ️ تقييم الفرصة للسهم فقط، وليس لعقد أوبشن محدد."
    )


# =========================================================
# TEXT ANALYSIS
# =========================================================

async def analyze_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not _allowed(update):
        await deny_access(update)
        return

    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    mode = context.user_data.get("mode")

    try:
        if mode == "scan":
            symbol = text.upper().strip()

            if (
                not symbol.isalpha()
                or len(symbol) > 6
            ):
                await update.message.reply_text(
                    "⚠️ اكتب رمز سهم صحيح.\n\n"
                    "مثال: NVDA"
                )
                return

            await update.message.reply_text(
                f"🔎 جاري تحليل {symbol}...\n"
                "⏳ لحظة..."
            )

            trend = (
                await asyncio.to_thread(
                    get_stock_trend,
                    symbol
                )
            )

            data = (
                await asyncio.to_thread(
                    get_option_chain,
                    symbol
                )
            )

            contracts = get_top_contracts(
                data,
                trend
            )

            # Final displayed prices come from the exact OCC contract quote,
            # not from the broader option-chain snapshot.
            contracts = await asyncio.to_thread(
                refresh_contracts_exact_quotes,
                contracts
            )

            qualified = [
                contract
                for contract in contracts
                if (
                    contract["score"] >= MIN_TOP_SCORE
                    and contract["uoa_score"] >= MIN_TOP_UOA
                    and contract["volume"] >= MIN_VOLUME
                    and contract["decision"]["rank"] > 0
                )
            ]

            watched_contract = None

            for contract in qualified:
                decision = effective_decision(
                    contract,
                    trend
                )

                if (
                    decision["label"]
                    in [
                        "🟡 انتظار تأكيد",
                        "🟢 مرشح قوي — انتظار افتتاح السوق",
                    ]
                ):
                    watched_contract = contract
                    break

            await update.message.reply_text(
                format_top_contracts(
                    symbol,
                    contracts,
                    trend
                ),
                reply_markup=main_menu()
            )

            if watched_contract:
                added = add_pending_watch(
                    chat_id,
                    symbol,
                    watched_contract,
                    trend
                )

                if added:
                    await update.message.reply_text(
                        format_watch_added(
                            symbol,
                            watched_contract,
                            trend
                        )
                    )

            context.user_data["mode"] = None

        elif mode == "contract":
            parsed = parse_contract_short_input(text)

            await update.message.reply_text(
                f"📊 جاري جلب بيانات العقد:\n"
                f"{parsed['symbol']} {parsed['side']} "
                f"{parsed['strike']:g} | {parsed['expiry_text']}..."
            )

            contract = await asyncio.to_thread(
                get_exact_option_contract,
                parsed["symbol"],
                parsed["side"],
                parsed["strike"],
                parsed["expiry"],
            )

            if not contract:
                await update.message.reply_text(
                    "⚠️ لم أجد عقدًا مطابقًا تمامًا.\n\n"
                    "تأكدي من الرمز وCALL/PUT والسترايك "
                    "وتاريخ الانتهاء.",
                    reply_markup=main_menu()
                )
                context.user_data["mode"] = None
                return

            if is_us_market_open() and not option_quote_is_usable_now(contract):
                await update.message.reply_text(
                    "⚠️ تم العثور على العقد، لكن سعره اللحظي غير مؤكد أو قديم.\n"
                    "لن أعرض سعرًا غير موثوق. جرّبي مرة أخرى بعد قليل.",
                    reply_markup=main_menu()
                )
                context.user_data["mode"] = None
                return

            await update.message.reply_text(
                format_exact_contract_evaluation(contract),
                reply_markup=main_menu()
            )

            context.user_data["mode"] = None

        elif mode == "opportunity":
            symbol = text.upper().strip()

            if not symbol.isalpha() or len(symbol) > 6:
                await update.message.reply_text(
                    "⚠️ اكتب رمز السهم فقط.\n\nمثال: TSLA",
                    reply_markup=main_menu()
                )
                return

            await update.message.reply_text(
                f"🎯 جاري تقييم فرصة {symbol}..."
            )

            result = await asyncio.to_thread(
                build_opportunity_evaluation,
                symbol
            )

            await update.message.reply_text(
                result,
                reply_markup=main_menu()
            )
            context.user_data["mode"] = None

        elif mode == "chart":
            symbol = text.upper().strip()

            if (
                not symbol.isalpha()
                or len(symbol) > 6
            ):
                await update.message.reply_text(
                    "⚠️ اكتب رمز سهم صحيح.\n\n"
                    "مثال: TSLA"
                )
                return

            context.user_data["chart_symbol"] = symbol
            context.user_data["mode"] = None

            await update.message.reply_text(
                f"🔮 {symbol}\n\n"
                "اختر إطار التوقع:",
                reply_markup=chart_timeframe_menu()
            )

        else:
            await update.message.reply_text(
                "اضغط /start واختر الخدمة.",
                reply_markup=main_menu()
            )

    except ValueError as e:
        print("VALUE ERROR:", e)

        if mode == "contract":
            await update.message.reply_text(
                f"⚠️ {str(e) if str(e) else 'صيغة العقد غير صحيحة.'}\n\n"
                "الصيغة المطلوبة:\n"
                "NVDA CALL 240 | 28 Aug 2026",
                reply_markup=main_menu()
            )
        else:
            await update.message.reply_text(
                "⚠️ البيانات المدخلة غير صحيحة.",
                reply_markup=main_menu()
            )

    except requests.exceptions.RequestException as e:
        print(
            "API ERROR:",
            e
        )

        await update.message.reply_text(
            "⚠️ تعذر الاتصال ببيانات السوق حاليًا.",
            reply_markup=main_menu()
        )

    except Exception as e:
        print(
            "ERROR:",
            e
        )

        await update.message.reply_text(
            "⚠️ حصل خطأ أثناء تحليل البيانات.",
            reply_markup=main_menu()
        )



async def monitor_auto_top_opportunities(application):
    """Push the strongest current opportunities automatically while the U.S. market is open."""
    global LAST_AUTO_TOP_SIGNATURE

    await asyncio.sleep(AUTO_TOP_OPPORTUNITIES_START_DELAY_SECONDS)

    while True:
        try:
            if not is_us_market_open():
                await asyncio.sleep(300)
                continue

            # Force a fresh scan instead of reusing a stale 15-minute cache.
            TOP10_CACHE["time"] = 0
            TOP10_CACHE["results"] = None

            results = await asyncio.to_thread(scan_top10)
            results = await asyncio.to_thread(refresh_top10_contract_prices, results)
            qualified = [
                item for item in results
                if int(item.get("opportunity_score") or 0) >= AUTO_TOP_OPPORTUNITIES_MIN_SCORE
            ]

            if qualified:
                signature = tuple(
                    (
                        item["symbol"],
                        item["contract"]["side"],
                        float(item["contract"]["strike"]),
                        int(item.get("opportunity_score") or 0),
                        item.get("entry_zone", {}).get("status"),
                    )
                    for item in qualified
                )

                # Do not spam an identical list every hour.
                if signature != LAST_AUTO_TOP_SIGNATURE:
                    msg = "🚨 تحديث تلقائي — أهم الفرص الآن\n\n" + format_top10(qualified)

                    for user_id in ALLOWED_USERS:
                        try:
                            await application.bot.send_message(
                                chat_id=user_id,
                                text=msg,
                                disable_web_page_preview=True,
                            )
                        except Exception as send_error:
                            print("AUTO TOP SEND ERROR:", user_id, send_error)

                    LAST_AUTO_TOP_SIGNATURE = signature
                    _save_auto_sent_state()

        except Exception as exc:
            print("AUTO TOP OPPORTUNITIES ERROR:", type(exc).__name__, repr(exc), flush=True)

        await asyncio.sleep(AUTO_TOP_OPPORTUNITIES_INTERVAL_SECONDS)


# =========================================================
# START BACKGROUND TASKS
# =========================================================

async def post_init(application):
    # Keep individual opportunity confirmation monitoring active.
    asyncio.create_task(monitor_pending(application))

    # Automatically push the best current opportunities while the market is open.
    asyncio.create_task(monitor_auto_top_opportunities(application))
    print("AUTO TOP OPPORTUNITIES STARTED")

    # Major U.S. scheduled events: automatic reminders one week, 3 days, and same day.
    asyncio.create_task(monitor_major_us_events(application))
    print("MAJOR US EVENT REMINDERS STARTED")

    if FINNHUB_TOKEN:
        # V31: company/news alerts are MANUAL ONLY.
        # Do not start monitor_important_news(). The "📰 أهم الأخبار" button
        # still fetches and displays news on demand.
        asyncio.create_task(send_earnings_reminders(application))
        print("MANUAL NEWS ONLY — AUTO IMPORTANT NEWS DISABLED")
        print("EARNINGS REMINDERS STARTED")
    else:
        print("WARNING: FINNHUB_TOKEN is missing")

    print("AUTO WATCHER STARTED")


# =========================================================
# MAIN
# =========================================================

def main():
    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing"
        )

    if not MARKETDATA_TOKEN:
        raise RuntimeError(
            "MARKETDATA_TOKEN is missing"
        )

    if not FINNHUB_TOKEN:
        print(
            "WARNING: FINNHUB_TOKEN is missing - "
            "news and earnings will not work."
        )

    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(8)
        .post_init(post_init)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            buttons
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            analyze_message
        )
    )

    print(
        "STARTING WEBHOOK"
    )

    print(
        "WEBHOOK:",
        WEBHOOK_URL
    )

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=WEBHOOK_URL,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
