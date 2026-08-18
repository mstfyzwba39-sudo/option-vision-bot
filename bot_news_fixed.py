import os
import time
import math
import asyncio
import requests
import io

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

PORT = int(os.environ.get("PORT", 10000))

WEBHOOK_BASE_URL = "https://option-vision-bot.onrender.com"
WEBHOOK_PATH = "telegram"
WEBHOOK_URL = f"{WEBHOOK_BASE_URL}/{WEBHOOK_PATH}"

MAX_OPTION_ASK = 5.00
MIN_VOLUME = 1000
MIN_TOP_SCORE = 80
MIN_TOP_UOA = 3
TOP_N_RESULTS = 10

WATCH_INTERVAL_SECONDS = 300
WATCH_LOOP_SECONDS = 60
CACHE_SECONDS = 900

NEWS_DAYS = 2
NEWS_CACHE_SECONDS = 600
NEWS_AUTO_INTERVAL_SECONDS = 900
NEWS_AUTO_MIN_IMPORTANCE = 8
MAX_NEWS_RESULTS = 10

EARNINGS_LOOKAHEAD_DAYS = 21
EARNINGS_LOOP_SECONDS = 21600

SCAN_SYMBOLS = [
    "SPY", "QQQ", "AMD", "TSLA", "NVDA", "MRVL", "ARM", "AVGO", "MU", "GS",
    "META", "IWM", "AAPL", "GOOGL", "MSFT", "AMZN", "SMCI", "SNOW", "SHOP",
    "BA", "CRM", "CAT", "PLTR", "ORCL", "OPEN", "IBIT", "MSTR", "COIN", "SPCX",
    "SKHY",
]

TOP10_CACHE = {"time": 0, "results": None}
NEWS_CACHE = {"time": 0, "results": None}
PENDING_WATCHES = {}
SENT_NEWS_IDS = set()
EARNINGS_SENT = set()


# =========================================================
# ACCESS
# =========================================================

def _allowed(update):
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USERS


async def deny_access(update):
    if update.callback_query:
        await update.callback_query.answer(
            "â ØºÙØ± ÙØµØ±Ø­ ÙÙ Ø¨Ø§Ø³ØªØ®Ø¯Ø§Ù ÙØ°Ø§ Ø§ÙØ¨ÙØª.",
            show_alert=True
        )
        return

    if update.effective_message:
        await update.effective_message.reply_text(
            "â ØºÙØ± ÙØµØ±Ø­ ÙÙ Ø¨Ø§Ø³ØªØ®Ø¯Ø§Ù ÙØ°Ø§ Ø§ÙØ¨ÙØª."
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
            data.get("errmsg", "ÙÙ ÙØªÙ Ø§ÙØ­ØµÙÙ Ø¹ÙÙ Ø¨ÙØ§ÙØ§Øª Ø§ÙØ®ÙØ§Ø±Ø§Øª.")
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
        raise ValueError("ØªØ¹Ø°Ø± Ø§ÙØ­ØµÙÙ Ø¹ÙÙ Ø­Ø±ÙØ© Ø§ÙØ³ÙÙ.")

    closes = data.get("c", [])
    volumes = data.get("v", [])

    if len(closes) < 21:
        raise ValueError("ÙØ§ ØªÙØ¬Ø¯ Ø´ÙÙØ¹ ÙØ§ÙÙØ© ÙØªØ­ÙÙÙ Ø§ÙØ§ØªØ¬Ø§Ù.")

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
        label = "ð¢ ØµØ§Ø¹Ø¯"
        strength = bullish_points
    elif bearish_points >= 3 and bearish_points > bullish_points:
        bias = "PUT"
        label = "ð´ ÙØ§Ø¨Ø·"
        strength = bearish_points
    else:
        bias = "NEUTRAL"
        label = "ð¡ ÙØ­Ø§ÙØ¯"
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
        momentum_label = "ð¥ ÙÙÙ Ø¬Ø¯ÙØ§"
    elif momentum_score >= 6:
        momentum_label = "ð¢ ÙÙÙ"
    elif momentum_score >= 4:
        momentum_label = "ð¡ ÙØªÙØ³Ø·"
    else:
        momentum_label = "âª Ø¶Ø¹ÙÙ"

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
        continuation_label = "ð¥ ÙØ³ØªÙØ± Ø¨ÙÙØ©"
    elif continuation_score >= 2:
        continuation_label = "ð¢ ÙØ³ØªÙØ±"
    elif continuation_score >= 0:
        continuation_label = "ð¡ ÙØªÙØ§Ø³Ù"
    else:
        continuation_label = "â ï¸ Ø¨Ø¯Ø£ ÙØ¶Ø¹Ù"

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
        raise ValueError("ØªØ¹Ø°Ø± Ø§ÙØ­ØµÙÙ Ø¹ÙÙ Ø´ÙÙØ¹ 15 Ø¯ÙÙÙØ©.")

    closes = data.get("c", [])

    if len(closes) < 6:
        raise ValueError("Ø¨ÙØ§ÙØ§Øª 15 Ø¯ÙÙÙØ© ØºÙØ± ÙØ§ÙÙØ©.")

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
            data.get("errmsg", "ØªØ¹Ø°Ø± Ø§ÙØ­ØµÙÙ Ø¹ÙÙ Ø¨ÙØ§ÙØ§Øª Ø§ÙØ´Ø§Ø±Øª.")
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
        raise ValueError("Ø¨ÙØ§ÙØ§Øª Ø§ÙØ´Ø§Ø±Øª ØºÙØ± ÙØ§ÙÙØ©.")

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
        raise ValueError("Ø¨ÙØ§ÙØ§Øª 4 Ø³Ø§Ø¹Ø§Øª ØºÙØ± ÙØ§ÙÙØ©.")

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
        scenario_ar = "ð¢ ØµØ§Ø¹Ø¯"

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
        scenario_ar = "ð´ ÙØ§Ø¨Ø·"

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
        scenario_ar = "ð¡ Ø¹Ø±Ø¶Ù"

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

def make_chart(symbol, resolution):
    data = get_chart_data(symbol, resolution)

    opens = data["opens"]
    highs = data["highs"]
    lows = data["lows"]
    closes = data["closes"]

    candle_count = min(55, len(closes))

    opens = opens[-candle_count:]
    highs = highs[-candle_count:]
    lows = lows[-candle_count:]
    closes = closes[-candle_count:]

    sma10 = moving_average(closes, 10)
    sma20 = moving_average(closes, 20)

    forecast = build_forecast(
        closes,
        highs,
        lows,
        resolution
    )

    last_price = closes[-1]

    if resolution == "15":
        timeframe = "15 MIN"
        timeframe_ar = "15 Ø¯ÙÙÙØ©"
    elif resolution == "60":
        timeframe = "1 HOUR"
        timeframe_ar = "Ø³Ø§Ø¹Ø©"
    elif resolution == "240":
        timeframe = "4 HOURS"
        timeframe_ar = "4 Ø³Ø§Ø¹Ø§Øª"
    else:
        timeframe = "DAILY"
        timeframe_ar = "ÙÙÙÙ"

    fig, ax = plt.subplots(figsize=(12, 7))

    fig.patch.set_facecolor("#0B0F14")
    ax.set_facecolor("#0B0F14")

    candle_width = 0.62

    for i in range(len(closes)):
        open_price = opens[i]
        high_price = highs[i]
        low_price = lows[i]
        close_price = closes[i]

        candle_color = (
            "#22C55E"
            if close_price >= open_price
            else "#EF4444"
        )

        ax.vlines(
            i,
            low_price,
            high_price,
            color=candle_color,
            linewidth=1.0,
            zorder=3
        )

        body_bottom = min(open_price, close_price)
        body_height = abs(close_price - open_price)

        if body_height == 0:
            body_height = max(
                last_price * 0.0003,
                0.01
            )

        rectangle = Rectangle(
            (
                i - candle_width / 2,
                body_bottom
            ),
            candle_width,
            body_height,
            facecolor=candle_color,
            edgecolor=candle_color,
            linewidth=1,
            zorder=3
        )

        ax.add_patch(rectangle)

    historical_x = list(range(len(closes)))

    ax.plot(
        historical_x,
        sma10,
        linewidth=1.0,
        color="#FACC15",
        alpha=0.8,
        label="SMA 10"
    )

    ax.plot(
        historical_x,
        sma20,
        linewidth=1.0,
        color="#38BDF8",
        alpha=0.8,
        label="SMA 20"
    )

    future_start = len(closes) - 1
    future_end = (
        future_start + forecast["future_bars"]
    )

    zone_start = max(
        0,
        len(closes) - 18
    )

    ax.fill_between(
        [zone_start, future_end + 1],
        [
            forecast["resistance_low"],
            forecast["resistance_low"]
        ],
        [
            forecast["resistance_high"],
            forecast["resistance_high"]
        ],
        color="#EF4444",
        alpha=0.16,
        zorder=1
    )

    ax.fill_between(
        [zone_start, future_end + 1],
        [
            forecast["support_low"],
            forecast["support_low"]
        ],
        [
            forecast["support_high"],
            forecast["support_high"]
        ],
        color="#22C55E",
        alpha=0.16,
        zorder=1
    )

    ax.text(
        future_end + 0.4,
        forecast["resistance"],
        "RESISTANCE",
        color="#F87171",
        fontsize=9,
        va="center"
    )

    ax.text(
        future_end + 0.4,
        forecast["support"],
        "SUPPORT",
        color="#4ADE80",
        fontsize=9,
        va="center"
    )

    future_x = list(
        range(
            future_start,
            future_end + 1
        )
    )

    ax.fill_between(
        future_x,
        forecast["lower"],
        forecast["upper"],
        color="#A78BFA",
        alpha=0.08,
        zorder=1
    )

    ax.plot(
        future_x,
        forecast["expected"],
        linestyle="--",
        linewidth=2.2,
        color="#FFFFFF",
        alpha=0.95,
        zorder=5
    )

    pivot_points = forecast["pivot_points"]

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

        x1 = future_start + start_index
        x2 = future_start + end_index

        ax.annotate(
            "",
            xy=(x2, end_price),
            xytext=(x1, start_price),
            arrowprops={
                "arrowstyle": "-|>",
                "color": "#FFFFFF",
                "lw": 2.3,
                "mutation_scale": 15,
                "shrinkA": 0,
                "shrinkB": 0,
                "connectionstyle": "arc3,rad=0.08"
            },
            zorder=6
        )

    ax.axvline(
        len(closes) - 0.5,
        linestyle=":",
        linewidth=1.1,
        color="#94A3B8",
        alpha=0.8
    )

    ax.text(
        len(closes) + 0.3,
        max(forecast["upper"]),
        "FORECAST",
        color="#FFFFFF",
        fontsize=9,
        fontweight="bold"
    )

    target1_index = min(
        2,
        len(pivot_points) - 1
    )

    target1_x = (
        future_start
        + pivot_points[target1_index][0]
    )

    ax.scatter(
        [target1_x],
        [forecast["target1"]],
        s=55,
        color="#FACC15",
        zorder=7
    )

    ax.text(
        target1_x + 0.3,
        forecast["target1"],
        f"T1 ${forecast['target1']:.2f}",
        color="#FACC15",
        fontsize=9,
        va="center"
    )

    ax.scatter(
        [future_end],
        [forecast["target2"]],
        s=65,
        color="#F59E0B",
        zorder=7
    )

    ax.text(
        future_end + 0.3,
        forecast["target2"],
        f"T2 ${forecast['target2']:.2f}",
        color="#F59E0B",
        fontsize=9,
        va="center"
    )

    ax.axhline(
        forecast["invalidation"],
        linestyle=":",
        linewidth=1,
        color="#FB7185",
        alpha=0.9
    )

    ax.text(
        zone_start,
        forecast["invalidation"],
        (
            f" INVALIDATION "
            f"${forecast['invalidation']:.2f}"
        ),
        color="#FB7185",
        fontsize=8,
        va="bottom"
    )

    ax.set_title(
        (
            f"{symbol} | "
            f"{timeframe} | "
            f"${last_price:.2f}"
        ),
        fontsize=15,
        fontweight="bold",
        color="#FFFFFF"
    )

    ax.set_ylabel(
        "Price",
        color="#CBD5E1"
    )

    ax.tick_params(
        axis="y",
        colors="#CBD5E1"
    )

    ax.tick_params(
        axis="x",
        labelbottom=False,
        colors="#CBD5E1"
    )

    for spine in ax.spines.values():
        spine.set_color("#334155")

    ax.grid(
        True,
        alpha=0.10,
        color="#94A3B8"
    )

    ax.set_xlim(
        -1,
        future_end + 4
    )

    all_y = (
        highs
        + forecast["upper"]
        + forecast["lower"]
        + [forecast["invalidation"]]
    )

    y_min = min(all_y)
    y_max = max(all_y)

    padding = (
        (y_max - y_min)
        * 0.08
    )

    if padding <= 0:
        padding = max(
            last_price * 0.02,
            0.5
        )

    ax.set_ylim(
        y_min - padding,
        y_max + padding
    )

    ax.legend(
        loc="upper left",
        fontsize=8,
        facecolor="#111827",
        edgecolor="#334155",
        labelcolor="#E5E7EB"
    )

    plt.tight_layout()

    image = io.BytesIO()

    plt.savefig(
        image,
        format="png",
        dpi=170,
        bbox_inches="tight",
        facecolor=fig.get_facecolor()
    )

    plt.close(fig)
    image.seek(0)

    forecast_period_text = (
        "10 Ø¬ÙØ³Ø§Øª ÙØ§Ø¯ÙØ©"
        if resolution == "D"
        else f"{forecast['future_bars']} Ø´ÙØ¹Ø© ÙØ§Ø¯ÙØ©"
    )

    caption = (
        f"ð® {symbol} â {timeframe_ar}\n\n"
        f"ðµ Ø§ÙØ³Ø¹Ø± Ø§ÙØ­Ø§ÙÙ: ${last_price:.2f}\n"
        f"ð Ø§ÙØ³ÙÙØ§Ø±ÙÙ Ø§ÙÙØ±Ø¬Ø­: {forecast['scenario_ar']}\n"
        f"â³ Ø§ÙÙØ³Ø§Ø±: {forecast_period_text}\n\n"
        f"ð¯ Ø§ÙÙØ¯Ù 1: ${forecast['target1']:.2f}\n"
        f"ð¯ Ø§ÙÙØ¯Ù 2: ${forecast['target2']:.2f}\n"
        f"ð Ø¥ÙØºØ§Ø¡ Ø§ÙØ³ÙÙØ§Ø±ÙÙ: ${forecast['invalidation']:.2f}\n\n"
        f"ð¢ ÙÙØ·ÙØ© Ø§ÙØ¯Ø¹Ù: "
        f"${forecast['support_low']:.2f} - ${forecast['support_high']:.2f}\n"
        f"ð´ ÙÙØ·ÙØ© Ø§ÙÙÙØ§ÙÙØ©: "
        f"${forecast['resistance_low']:.2f} - ${forecast['resistance_high']:.2f}\n\n"
        f"ð ÙÙØ§ÙØ© Ø§ÙÙØ³Ø§Ø± Ø§ÙÙØªÙÙØ¹Ø©: {forecast['change_pct']:+.1f}%\n"
        f"ð Ø§ÙØ«ÙØ© Ø§ÙÙÙÙØ©: {forecast['confidence']}%\n\n"
        "â ï¸ Ø§ÙÙØ³Ø§Ø± Ø³ÙÙØ§Ø±ÙÙ ÙÙÙ Ø§Ø­ØªÙØ§ÙÙ ÙÙÙØ³ ØªÙÙØ¹ÙØ§ ÙØ¤ÙØ¯ÙØ§ ÙÙØ³Ø¹Ø±."
    )

    return image, caption


def chart_timeframe_menu():
    keyboard = [
        [
            InlineKeyboardButton(
                "15 Ø¯ÙÙÙØ©",
                callback_data="chart_15"
            ),
            InlineKeyboardButton(
                "Ø³Ø§Ø¹Ø©",
                callback_data="chart_60"
            ),
        ],
        [
            InlineKeyboardButton(
                "4 Ø³Ø§Ø¹Ø§Øª",
                callback_data="chart_240"
            ),
            InlineKeyboardButton(
                "ÙÙÙÙ",
                callback_data="chart_D"
            ),
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


# =========================================================
# NEWS - FINNHUB
# =========================================================

IMPORTANT_NEWS_RULES = [
    (
        10,
        [
            "bankruptcy", "chapter 11", "fraud", "sec charges", "criminal",
            "acquisition", "acquire", "merger", "buyout", "takeover",
            "fda approval", "fda rejects", "recall", "halted", "trading halt"
        ],
        "Ø­Ø¯Ø« Ø¬ÙÙØ±Ù Ø¬Ø¯ÙØ§"
    ),
    (
        9,
        [
            "earnings", "revenue", "guidance", "forecast", "outlook",
            "beats estimates", "misses estimates", "profit warning",
            "investigation", "subpoena", "lawsuit", "settlement",
            "ceo resigns", "ceo steps down"
        ],
        "ÙØªØ§Ø¦Ø¬ Ø£Ù ØªØ·ÙØ± Ø¬ÙÙØ±Ù"
    ),
    (
        8,
        [
            "contract", "deal", "partnership", "approval", "launch",
            "price target", "downgrade", "upgrade", "layoffs",
            "job cuts", "offering", "share sale", "capital raise",
            "dividend", "buyback", "repurchase", "split"
        ],
        "Ø®Ø¨Ø± ÙØ¯ ÙØ¤Ø«Ø± Ø¹ÙÙ Ø­Ø±ÙØ© Ø§ÙØ³ÙÙ"
    ),
    (
        7,
        [
            "analyst", "target", "orders", "delivery", "deliveries",
            "production", "sales", "tariff", "export", "restriction",
            "regulatory", "antitrust"
        ],
        "ØªØ·ÙØ± ÙÙÙ ÙØ­ØªØ§Ø¬ ÙØªØ§Ø¨Ø¹Ø©"
    ),
]

POSITIVE_WORDS = [
    "beats", "beat estimates", "raises guidance", "raised guidance",
    "upgrade", "upgraded", "approval", "approved", "record revenue",
    "record sales", "partnership", "contract win", "wins contract",
    "buyback", "repurchase", "strong demand", "surge", "growth",
    "acquisition", "acquire", "launch"
]

NEGATIVE_WORDS = [
    "misses", "missed estimates", "cuts guidance", "cut guidance",
    "downgrade", "downgraded", "investigation", "subpoena",
    "lawsuit", "fraud", "recall", "bankruptcy", "chapter 11",
    "layoffs", "job cuts", "offering", "share sale", "capital raise",
    "weak demand", "decline", "falls", "drops", "rejects", "rejected"
]


# News relevance aliases. A company-news item must be primarily about the
# requested symbol, not merely mention it somewhere in the summary.
NEWS_SYMBOL_ALIASES = {
    "SPY": ["spy", "spdr s&p 500", "spdr s&p 500 etf"],
    "QQQ": ["qqq", "invesco qqq"],
    "IWM": ["iwm", "ishares russell 2000"],
    "IBIT": ["ibit", "ishares bitcoin trust"],
    "TSLA": ["tsla", "tesla"],
    "NVDA": ["nvda", "nvidia"],
    "AMD": ["amd", "advanced micro devices"],
    "MRVL": ["mrvl", "marvell"],
    "ARM": ["arm holdings", "arm"],
    "AVGO": ["avgo", "broadcom"],
    "MU": ["mu", "micron"],
    "GS": ["gs", "goldman sachs"],
    "META": ["meta platforms", "meta", "facebook", "instagram"],
    "AAPL": ["aapl", "apple"],
    "GOOGL": ["googl", "google", "alphabet", "youtube"],
    "MSFT": ["msft", "microsoft"],
    "AMZN": ["amzn", "amazon", "aws"],
    "SMCI": ["smci", "super micro computer", "supermicro"],
    "SNOW": ["snowflake"],
    "SHOP": ["shopify"],
    "BA": ["boeing"],
    "CRM": ["salesforce"],
    "CAT": ["caterpillar"],
    "PLTR": ["palantir"],
    "ORCL": ["oracle"],
    "OPEN": ["opendoor"],
    "MSTR": ["mstr", "strategy", "microstrategy"],
    "COIN": ["coinbase"],
    "SPCX": ["spcx"],
    "SKHY": ["skhy"],
}

NEWS_EDITORIAL_PATTERNS = [
    "preview", "the real reason", "why ", "why i", "why we",
    "is a buy", "is it a buy", "should you buy", "could ",
    "may ", "what to expect", "outlook for", "analysis",
]

NEWS_HARD_EVENT_WORDS = [
    "reports earnings", "reported earnings", "earnings results",
    "beats estimates", "misses estimates", "raises guidance",
    "cuts guidance", "acquisition", "merger", "buyout", "takeover",
    "fda approval", "fda rejects", "recall", "trading halt",
    "bankruptcy", "chapter 11", "sec charges", "investigation",
    "subpoena", "lawsuit", "settlement", "ceo resigns",
    "ceo steps down", "layoffs", "job cuts", "offering",
    "share sale", "capital raise", "buyback", "repurchase",
]


def _news_aliases(symbol):
    return NEWS_SYMBOL_ALIASES.get(symbol.upper(), [symbol.lower()])


def is_news_relevant_to_symbol(symbol, item):
    """Return True only when the story is primarily about `symbol`.

    Finnhub company-news results can contain articles where the symbol is only
    mentioned in the body/summary. Requiring a symbol/company alias in the
    headline removes those false positives (e.g. Walmart articles under AMZN).
    """
    headline = str(item.get("headline") or "").strip().lower()
    if not headline:
        return False

    aliases = _news_aliases(symbol)
    return any(alias in headline for alias in aliases)


def is_editorial_news(item):
    headline = str(item.get("headline") or "").strip().lower()
    return any(pattern in headline for pattern in NEWS_EDITORIAL_PATTERNS)


def has_hard_news_event(item):
    headline = str(item.get("headline") or "").strip().lower()
    summary = str(item.get("summary") or "").strip().lower()
    combined = f"{headline} {summary}"
    return any(word in combined for word in NEWS_HARD_EVENT_WORDS)


def get_company_news(symbol, days=NEWS_DAYS):
    if not FINNHUB_TOKEN:
        raise RuntimeError("FINNHUB_TOKEN is missing")

    ny_today = datetime.now(
        ZoneInfo("America/New_York")
    ).date()

    from_date = (
        ny_today - timedelta(days=days)
    ).isoformat()

    to_date = ny_today.isoformat()

    response = requests.get(
        "https://finnhub.io/api/v1/company-news",
        headers=get_finnhub_headers(),
        params={
            "symbol": symbol,
            "from": from_date,
            "to": to_date,
        },
        timeout=20
    )

    response.raise_for_status()
    data = response.json()

    if not isinstance(data, list):
        raise ValueError("Ø§Ø³ØªØ¬Ø§Ø¨Ø© Ø§ÙØ£Ø®Ø¨Ø§Ø± ØºÙØ± ÙØªÙÙØ¹Ø©.")

    return data


def score_news_item(symbol, item):
    headline = str(
        item.get("headline") or ""
    ).strip()

    summary = str(
        item.get("summary") or ""
    ).strip()

    source = str(
        item.get("source") or ""
    ).strip()

    combined = (
        f"{headline} {summary}"
    ).lower()

    importance = 3
    reason = "Ø®Ø¨Ø± Ø¹Ø§Ù"

    for score, words, label in IMPORTANT_NEWS_RULES:
        if any(
            word in combined
            for word in words
        ):
            importance = max(
                importance,
                score
            )
            reason = label
            break

    # Relevance is handled before scoring. Only reward a direct headline hit,
    # never a passing mention in the summary/body.
    headline_lower = headline.lower()
    if any(alias in headline_lower for alias in _news_aliases(symbol)):
        importance = min(
            10,
            importance + 1
        )

    # Opinion/preview pieces are useful context, but they are not 9/10 breaking
    # company events unless the article also contains a concrete hard event.
    if is_editorial_news(item) and not has_hard_news_event(item):
        importance = min(importance, 6)
        reason = "تحليل أو رأي — ليس حدثًا جوهريًا"

    positive_hits = sum(
        1
        for word in POSITIVE_WORDS
        if word in combined
    )

    negative_hits = sum(
        1
        for word in NEGATIVE_WORDS
        if word in combined
    )

    if positive_hits > negative_hits:
        sentiment = "POSITIVE"
        sentiment_ar = "ð¢ Ø¥ÙØ¬Ø§Ø¨Ù"
    elif negative_hits > positive_hits:
        sentiment = "NEGATIVE"
        sentiment_ar = "ð´ Ø³ÙØ¨Ù"
    else:
        sentiment = "NEUTRAL"
        sentiment_ar = "âª ÙØ­Ø§ÙØ¯"

    category_ar = reason

    if "earnings" in combined or "revenue" in combined:
        category_ar = "ÙØªØ§Ø¦Ø¬ Ø£Ù ØªÙÙØ¹Ø§Øª ÙØ§ÙÙØ©"
    elif any(
        x in combined
        for x in [
            "upgrade", "downgrade", "price target", "analyst"
        ]
    ):
        category_ar = "ØªØºÙÙØ± ÙÙ Ø±Ø£Ù Ø£Ù ÙØ¯Ù ÙØ­ÙÙ"
    elif any(
        x in combined
        for x in [
            "contract", "deal", "partnership"
        ]
    ):
        category_ar = "ØµÙÙØ© Ø£Ù Ø´Ø±Ø§ÙØ© ÙÙÙØ©"
    elif any(
        x in combined
        for x in [
            "acquisition", "merger", "buyout", "takeover"
        ]
    ):
        category_ar = "Ø§Ø³ØªØ­ÙØ§Ø° Ø£Ù Ø§ÙØ¯ÙØ§Ø¬"
    elif any(
        x in combined
        for x in [
            "investigation", "lawsuit", "subpoena", "fraud"
        ]
    ):
        category_ar = "ØªØ·ÙØ± ÙØ§ÙÙÙÙ Ø£Ù ØªÙØ¸ÙÙÙ"
    elif any(
        x in combined
        for x in [
            "launch", "approval", "approved"
        ]
    ):
        category_ar = "ÙÙØªØ¬ Ø£Ù ÙÙØ§ÙÙØ© Ø¬Ø¯ÙØ¯Ø©"
    elif any(
        x in combined
        for x in [
            "layoffs", "job cuts"
        ]
    ):
        category_ar = "ØªØºÙÙØ±Ø§Øª ØªØ´ØºÙÙÙØ© ÙÙÙØ©"

    timestamp = int(
        item.get("datetime") or 0
    )

    news_id = str(
        item.get("id")
        or item.get("url")
        or f"{symbol}:{timestamp}:{headline}"
    )

    return {
        "id": news_id,
        "symbol": symbol,
        "headline": headline,
        "summary": summary,
        "source": source,
        "url": str(
            item.get("url") or ""
        ).strip(),
        "timestamp": timestamp,
        "importance": importance,
        "reason_ar": reason,
        "category_ar": category_ar,
        "sentiment": sentiment,
        "sentiment_ar": sentiment_ar,
    }


def scan_important_news():
    now = time.time()

    if (
        NEWS_CACHE["results"] is not None
        and (
            now - NEWS_CACHE["time"]
        ) < NEWS_CACHE_SECONDS
    ):
        return NEWS_CACHE["results"]

    results = []
    seen = set()

    for symbol in SCAN_SYMBOLS:
        try:
            raw_news = get_company_news(
                symbol,
                NEWS_DAYS
            )

            for item in raw_news:
                # Finnhub may return articles that only mention the company
                # incidentally. Reject them before scoring.
                if not is_news_relevant_to_symbol(symbol, item):
                    continue

                scored = score_news_item(
                    symbol,
                    item
                )

                if scored["importance"] < 7:
                    continue

                dedupe_key = (
                    scored["headline"]
                    .lower()
                    .strip()
                )

                if not dedupe_key:
                    continue

                if dedupe_key in seen:
                    continue

                seen.add(dedupe_key)
                results.append(scored)

        except Exception as e:
            print(
                f"NEWS ERROR {symbol}:",
                e
            )

    results.sort(
        key=lambda x: (
            -x["importance"],
            -x["timestamp"]
        )
    )

    results = results[
        :MAX_NEWS_RESULTS
    ]

    NEWS_CACHE["time"] = now
    NEWS_CACHE["results"] = results

    return results


def news_time_text(timestamp):
    if not timestamp:
        return "ØºÙØ± ÙØªÙÙØ±"

    try:
        dt = datetime.fromtimestamp(
            timestamp,
            ZoneInfo("Asia/Riyadh")
        )
        return dt.strftime(
            "%d/%m %I:%M %p"
        )
    except Exception:
        return "ØºÙØ± ÙØªÙÙØ±"


def format_news_item(item):
    headline = item["headline"]

    if len(headline) > 220:
        headline = (
            headline[:217]
            + "..."
        )

    return (
        f"ð° {item['symbol']}\n"
        f"{item['sentiment_ar']} | "
        f"ð¥ Ø§ÙØ£ÙÙÙØ©: {item['importance']}/10\n"
        f"ð Ø§ÙØ®ÙØ§ØµØ©: {item['category_ar']}\n"
        f"ð {headline}\n"
        f"ð {news_time_text(item['timestamp'])}\n"
        f"ð·ï¸ Ø§ÙÙØµØ¯Ø±: {item['source'] or 'ØºÙØ± ÙØªÙÙØ±'}"
    )


def format_news_results(results):
    if not results:
        return (
            "ð° Ø£ÙÙ Ø§ÙØ£Ø®Ø¨Ø§Ø±\n\n"
            "â ÙØ§ ØªÙØ¬Ø¯ Ø£Ø®Ø¨Ø§Ø± ÙÙÙØ© Ø¬Ø¯ÙØ¯Ø© "
            "Ø¹ÙÙ Ø£Ø³ÙÙÙØ§ Ø®ÙØ§Ù Ø§ÙÙØªØ±Ø© Ø§ÙØ­Ø§ÙÙØ©."
        )

    blocks = [
        format_news_item(item)
        for item in results
    ]

    return (
        "ð° Ø£ÙÙ Ø£Ø®Ø¨Ø§Ø± Ø£Ø³ÙÙÙØ§\n"
        "ð¥ ÙØ¹Ø±Ø¶ Ø§ÙØ£Ø®Ø¨Ø§Ø± Ø§ÙÙÙÙØ© ÙÙØ·\n"
        "ââââââââââââââ\n\n"
        + "\n\nââââââââââââââ\n\n".join(blocks)
    )


# =========================================================
# EARNINGS - FINNHUB
# =========================================================

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
            "Ø§Ø³ØªØ¬Ø§Ø¨Ø© Ø¥Ø¹ÙØ§ÙØ§Øª Ø§ÙØ´Ø±ÙØ§Øª ØºÙØ± ÙØªÙÙØ¹Ø©."
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
        return "ð ÙØ¨Ù Ø§ÙØªØªØ§Ø­ Ø§ÙØ³ÙÙ"

    if value in {"amc", "after market close"}:
        return "ð Ø¨Ø¹Ø¯ Ø¥ØºÙØ§Ù Ø§ÙØ³ÙÙ"

    if value in {"dmh", "during market hours"}:
        return "ð Ø£Ø«ÙØ§Ø¡ Ø§ÙØ³ÙÙ"

    return "ð Ø§ÙØªÙÙÙØª ØºÙØ± ÙØ­Ø¯Ø¯"


def format_large_number(value):
    try:
        value = float(value)
    except (
        TypeError,
        ValueError
    ):
        return "ØºÙØ± ÙØªÙÙØ±"

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
        eps_text = "ØºÙØ± ÙØªÙÙØ±"

    revenue_text = format_large_number(
        item["revenue_estimate"]
    )

    return (
        f"ð {item['symbol']} â "
        f"{item['date'].strftime('%d/%m/%Y')}\n"
        f"{earnings_hour_ar(item['hour'])}\n"
        f"ð EPS Ø§ÙÙØªÙÙØ¹: {eps_text}\n"
        f"ð° Ø§ÙØ¥ÙØ±Ø§Ø¯Ø§Øª Ø§ÙÙØªÙÙØ¹Ø©: {revenue_text}"
    )


def format_earnings_results(results):
    if not results:
        return (
            "ð Ø¥Ø¹ÙØ§ÙØ§Øª Ø§ÙØ´Ø±ÙØ§Øª\n\n"
            "â ÙØ§ ØªÙØ¬Ø¯ Ø¥Ø¹ÙØ§ÙØ§Øª Ø£Ø±Ø¨Ø§Ø­ ÙØ§Ø¯ÙØ© "
            "ÙØ£Ø³ÙÙ ÙØ§Ø¦ÙØªÙØ§ Ø®ÙØ§Ù Ø§ÙÙØªØ±Ø© Ø§ÙØ­Ø§ÙÙØ©."
        )

    blocks = [
        format_earnings_item(item)
        for item in results
    ]

    return (
        "ð Ø¥Ø¹ÙØ§ÙØ§Øª Ø£Ø³ÙÙÙØ§ Ø§ÙÙØ§Ø¯ÙØ©\n"
        "â° Ø§ÙØªÙØ¨ÙÙ ØªÙÙØ§Ø¦Ù ÙØ¨Ù 7 Ø£ÙØ§Ù "
        "ÙØªØ°ÙÙØ± ÙÙ ÙÙØ³ Ø§ÙÙÙÙ\n"
        "ââââââââââââââ\n\n"
        + "\n\nââââââââââââââ\n\n".join(blocks)
    )


async def send_earnings_reminders(
    application
):
    while True:
        try:
            events = (
                await asyncio.to_thread(
                    get_earnings_calendar,
                    14
                )
            )

            today = datetime.now(
                ZoneInfo("America/New_York")
            ).date()

            for event in events:
                days_until = (
                    event["date"] - today
                ).days

                if days_until not in {
                    7,
                    0
                }:
                    continue

                reminder_type = (
                    "week"
                    if days_until == 7
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
                        "ð ØªÙØ¨ÙÙ Ø¥Ø¹ÙØ§Ù Ø¨Ø¹Ø¯ Ø£Ø³Ø¨ÙØ¹"
                    )
                else:
                    heading = (
                        "ð ØªØ°ÙÙØ±: Ø¥Ø¹ÙØ§Ù Ø§ÙØ´Ø±ÙØ© Ø§ÙÙÙÙ"
                    )

                text = (
                    f"{heading}\n\n"
                    f"{format_earnings_item(event)}"
                )

                for user_id in ALLOWED_USERS:
                    try:
                        await application.bot.send_message(
                            chat_id=user_id,
                            text=text
                        )
                    except Exception as send_error:
                        print(
                            "EARNINGS SEND ERROR:",
                            user_id,
                            send_error
                        )

                EARNINGS_SENT.add(key)

        except Exception as e:
            print(
                "EARNINGS MONITOR ERROR:",
                e
            )

        await asyncio.sleep(
            EARNINGS_LOOP_SECONDS
        )


async def monitor_important_news(
    application
):
    await asyncio.sleep(30)

    while True:
        try:
            NEWS_CACHE["time"] = 0
            NEWS_CACHE["results"] = None

            results = (
                await asyncio.to_thread(
                    scan_important_news
                )
            )

            for item in results:
                if (
                    item["importance"]
                    < NEWS_AUTO_MIN_IMPORTANCE
                ):
                    continue

                news_id = item["id"]

                if news_id in SENT_NEWS_IDS:
                    continue

                text = (
                    "ð¨ Ø®Ø¨Ø± ÙÙÙ Ø¬Ø¯ÙØ¯\n\n"
                    f"{format_news_item(item)}"
                )

                for user_id in ALLOWED_USERS:
                    try:
                        await application.bot.send_message(
                            chat_id=user_id,
                            text=text,
                            disable_web_page_preview=True
                        )
                    except Exception as send_error:
                        print(
                            "NEWS SEND ERROR:",
                            user_id,
                            send_error
                        )

                SENT_NEWS_IDS.add(news_id)

        except Exception as e:
            print(
                "NEWS MONITOR ERROR:",
                e
            )

        await asyncio.sleep(
            NEWS_AUTO_INTERVAL_SECONDS
        )


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
            "âª ØºÙØ± ÙØªØ§Ø­"
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
        label = "ð¥ Ø§Ø³ØªØ«ÙØ§Ø¦Ù Ø¬Ø¯ÙØ§"
    elif score >= 7:
        label = "ð¥ ÙØ±ØªÙØ¹ Ø¬Ø¯ÙØ§"
    elif score >= 5:
        label = "ð¢ ÙØ±ØªÙØ¹"
    elif score >= 3:
        label = "ð¡ ÙÙØ­ÙØ¸"
    else:
        label = "âª Ø·Ø¨ÙØ¹Ù"

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
            "label": "ð¡ Ø§ÙØªØ¸Ø§Ø± ØªØ£ÙÙØ¯",
            "reason": "Ø§ÙØ§ØªØ¬Ø§Ù ØºÙØ± ÙØ­Ø³ÙÙ",
            "rank": 1,
        }

    if contract["side"] != bias:
        return {
            "label": "ð´ Ø§Ø³ØªØ¨Ø¹Ø§Ø¯",
            "reason": "Ø§ÙØ¹ÙØ¯ Ø¹ÙØ³ Ø§ØªØ¬Ø§Ù Ø§ÙØ³ÙÙ",
            "rank": 0,
        }

    if continuation < 0:
        return {
            "label": "ð´ Ø§Ø³ØªØ¨Ø¹Ø§Ø¯",
            "reason": "Ø§Ø³ØªÙØ±Ø§Ø± Ø§ÙØ­Ø±ÙØ© Ø¨Ø¯Ø£ ÙØ¶Ø¹Ù",
            "rank": 0,
        }

    if contract["score"] < MIN_TOP_SCORE:
        return {
            "label": "ð¡ ØºÙØ± ÙØ¤ÙÙ ÙÙÙØ±Ø§ÙØ¨Ø©",
            "reason": (
                f"Ø§ÙØªÙÙÙÙ {contract['score']}/100 "
                f"Ø£ÙÙ ÙÙ {MIN_TOP_SCORE}/100"
            ),
            "rank": 1,
        }

    if contract["uoa_score"] < MIN_TOP_UOA:
        return {
            "label": "ð¡ ØºÙØ± ÙØ¤ÙÙ ÙÙÙØ±Ø§ÙØ¨Ø©",
            "reason": (
                f"Ø§ÙÙØ´Ø§Ø· {contract['uoa_score']}/10 "
                f"Ø£ÙÙ ÙÙ {MIN_TOP_UOA}/10"
            ),
            "rank": 1,
        }

    if (
        momentum >= 6
        and continuation >= 2
        and contract["base_score"] >= 78
    ):
        return {
            "label": "ð¢ ØªØ£ÙÙØ¯ Ø¯Ø®ÙÙ",
            "reason": "Ø§ÙØ§ØªØ¬Ø§Ù ÙØ§ÙØ²Ø®Ù ÙØ§ÙØ§Ø³ØªÙØ±Ø§Ø± ÙØªÙØ§ÙÙÙÙ",
            "rank": 2,
        }

    if momentum < 6:
        reason = (
            f"Ø§ÙØ²Ø®Ù {momentum}/10 "
            f"ÙÙØ­ØªØ§Ø¬ ØªØ£ÙÙØ¯ Ø­Ø±ÙØ© ÙØµÙØ±Ø©"
        )
    elif continuation < 2:
        reason = "ÙÙØªØ¸Ø± ØªØ£ÙÙØ¯ Ø§Ø³ØªÙØ±Ø§Ø± Ø§ÙØ­Ø±ÙØ©"
    elif contract["base_score"] < 78:
        reason = (
            f"Ø¬ÙØ¯Ø© Ø§ÙØ¹ÙØ¯ {contract['base_score']}/100 "
            f"ÙØªØ­ØªØ§Ø¬ ØªØ­Ø³ÙÙØ§"
        )
    else:
        reason = (
            "Ø§ÙÙØ±ØµØ© Ø¬ÙØ¯Ø© ÙÙÙÙØ§ ØªØ­ØªØ§Ø¬ ØªØ£ÙÙØ¯ÙØ§ Ø¥Ø¶Ø§ÙÙÙØ§"
        )

    return {
        "label": "ð¡ Ø§ÙØªØ¸Ø§Ø± ØªØ£ÙÙØ¯",
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
            "label": "ð¢ ÙØ±Ø´Ø­ ÙÙÙ â Ø§ÙØªØ¸Ø§Ø± Ø§ÙØªØªØ§Ø­ Ø§ÙØ³ÙÙ",
            "reason": "Ø§ÙØ´Ø±ÙØ· ÙÙÙØ© ÙÙÙ Ø§ÙØ³ÙÙ Ø§ÙØ£ÙØ±ÙÙÙ ÙØºÙÙ",
            "rank": 1,
        }

    return decision


# =========================================================
# TOP CONTRACTS
# =========================================================

def get_top_contracts(data, trend):
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
                f"Ø¨ÙØ§ÙØ§Øª {field} ØºÙØ± ÙÙØ¬ÙØ¯Ø©."
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
    if trend["bias"] == "NEUTRAL":
        return False

    if contract["side"] != trend["bias"]:
        return False

    if contract["score"] < MIN_TOP_SCORE:
        return False

    if contract["uoa_score"] < MIN_TOP_UOA:
        return False

    if contract["ask"] > MAX_OPTION_ASK:
        return False

    if contract["volume"] < MIN_VOLUME:
        return False

    decision = effective_decision(
        contract,
        trend
    )

    if decision["rank"] <= 0:
        return False

    if (
        decision["label"]
        not in [
            "ð¡ Ø§ÙØªØ¸Ø§Ø± ØªØ£ÙÙØ¯",
            "ð¢ ÙØ±Ø´Ø­ ÙÙÙ â Ø§ÙØªØ¸Ø§Ø± Ø§ÙØªØªØ§Ø­ Ø§ÙØ³ÙÙ",
        ]
    ):
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
    data = get_option_chain(symbol)
    symbols = data.get("optionSymbol", [])

    for i, item in enumerate(symbols):
        if item != option_symbol:
            continue

        try:
            return {
                "ask": float(data["ask"][i]),
                "bid": float(data["bid"][i]),
                "mid": float(data["mid"][i]),
                "volume": int(data["volume"][i]),
                "oi": int(data["openInterest"][i]),
            }

        except (
            TypeError,
            ValueError,
            IndexError
        ):
            return None

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
            "Ø­Ø±ÙØ© 15 Ø¯ÙÙÙØ© Ø£ØµØ¨Ø­Øª ÙÙØ§ÙÙØ© ÙÙØ§ØªØ¬Ø§Ù"
        )

    if invalidated:
        return (
            "INVALID",
            "Ø­Ø±ÙØ© 15 Ø¯ÙÙÙØ© Ø§ÙØ¹ÙØ³Øª Ø¶Ø¯ Ø§ÙÙØ±ØµØ©"
        )

    return (
        "WAIT",
        "Ø§ÙØªØ£ÙÙØ¯ ÙÙ ÙÙØªÙÙ"
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

                trend = (
                    await asyncio.to_thread(
                        get_stock_trend,
                        symbol
                    )
                )

                if (
                    trend["bias"] != side
                    or trend["continuation_score"] < 0
                ):
                    await application.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "ð´ ØªÙ Ø§Ø³ØªØ¨Ø¹Ø§Ø¯ Ø§ÙÙØ±ØµØ©\n\n"
                            f"{symbol} "
                            f"{side} "
                            f"{strike:g} | "
                            f"{expiry_date}\n"
                            "Ø§ÙØ§ØªØ¬Ø§Ù ÙÙ ÙØ¹Ø¯ Ø¯Ø§Ø¹ÙÙØ§."
                        )
                    )

                    PENDING_WATCHES.pop(
                        key,
                        None
                    )
                    continue

                intraday = (
                    await asyncio.to_thread(
                        get_intraday_15m,
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
                            "ð´ ØªÙ Ø§Ø³ØªØ¨Ø¹Ø§Ø¯ Ø§ÙÙØ±ØµØ©\n\n"
                            f"{symbol} "
                            f"{side} "
                            f"{strike:g} | "
                            f"{expiry_date}\n"
                            f"ð¡ {reason}"
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
                            "â ï¸ ØªØ­ÙÙ Ø§ÙØªØ£ÙÙØ¯ Ø§ÙÙÙÙ "
                            "ÙÙÙ Ø§ÙØ³Ø¹Ø± ØªØ¬Ø§ÙØ² $5\n\n"
                            f"{symbol} "
                            f"{side} "
                            f"{strike:g} | "
                            f"{expiry_date}\n"
                            f"ðµ Ask ${ask_now:.2f}"
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
                        "ð¢ ØªØ­ÙÙ ØªØ£ÙÙØ¯ Ø§ÙØ¨ÙØª\n\n"
                        f"{symbol} "
                        f"{side} "
                        f"{strike:g} | "
                        f"{expiry_date}\n"
                        f"ðµ Ask ${ask_now:.2f}\n"
                        f"ð Volume {option_now['volume']:,}\n"
                        f"ð¡ {reason}\n\n"
                        "ð Ø§Ø¹ØªÙØ§Ø¯ÙØ§ Ø¹ÙÙ Ø¢Ø®Ø± "
                        "Ø¨ÙØ§ÙØ§Øª 15 Ø¯ÙÙÙØ© Ø§ÙÙØªØ§Ø­Ø©."
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
            result = analyze_symbol(
                symbol
            )

            if not result:
                continue

            trend = result["trend"]

            if trend["bias"] == "NEUTRAL":
                continue

            matching = [
                contract
                for contract
                in result["contracts"]
                if (
                    contract["side"]
                    == trend["bias"]
                    and contract["score"]
                    >= MIN_TOP_SCORE
                    and contract["uoa_score"]
                    >= MIN_TOP_UOA
                    and contract["volume"]
                    >= MIN_VOLUME
                )
            ]

            if not matching:
                continue

            matching.sort(
                key=lambda x: (
                    -x["internal_score"],
                    -x["uoa_score"],
                    x["spread_pct"]
                )
            )

            contract = matching[0]

            decision = effective_decision(
                contract,
                trend
            )

            if decision["rank"] == 0:
                continue

            result["contract"] = contract
            result["internal_score"] = (
                contract["internal_score"]
            )
            result["decision"] = decision

            results.append(result)

        except Exception as e:
            print(
                f"SCAN ERROR {symbol}: {e}"
            )

    results.sort(
        key=lambda x: (
            -x["decision"]["rank"],
            -x["internal_score"],
            -x["trend"]["momentum_score"],
            -x["contract"]["uoa_score"],
            x["contract"]["spread_pct"]
        )
    )

    top10 = results[:TOP_N_RESULTS]

    TOP10_CACHE["time"] = now
    TOP10_CACHE["results"] = top10

    return top10


# =========================================================
# MENUS
# =========================================================

def main_menu():
    keyboard = [
        [
            InlineKeyboardButton(
                "ð Ø£ÙØ¶Ù ÙØ±Øµ Ø§ÙÙÙÙ",
                callback_data="top10"
            )
        ],
        [
            InlineKeyboardButton(
                "ð Ø§Ø¨Ø­Ø« Ø¹Ù Ø£ÙØ¶Ù Ø§ÙØ¹ÙÙØ¯",
                callback_data="scan_options"
            )
        ],
        [
            InlineKeyboardButton(
                "ð ØªÙÙÙÙ Ø¹ÙØ¯ Ø£ÙØ¨Ø´Ù",
                callback_data="contract"
            )
        ],
        [
            InlineKeyboardButton(
                "ð¯ ØªÙÙÙÙ ÙØ±ØµØ©",
                callback_data="opportunity"
            )
        ],
        [
            InlineKeyboardButton(
                "ð® Ø§ÙØ´Ø§Ø±Øª Ø§ÙÙØªÙÙØ¹",
                callback_data="chart"
            )
        ],
        [
            InlineKeyboardButton(
                "ð° Ø£ÙÙ Ø§ÙØ£Ø®Ø¨Ø§Ø±",
                callback_data="news"
            )
        ],
        [
            InlineKeyboardButton(
                "ð Ø¥Ø¹ÙØ§ÙØ§Øª Ø§ÙØ´Ø±ÙØ§Øª",
                callback_data="earnings"
            )
        ],
        [
            InlineKeyboardButton(
                "â¹ï¸ Ø·Ø±ÙÙØ© Ø§ÙØ§Ø³ØªØ®Ø¯Ø§Ù",
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

def format_top10(results):
    if not results:
        return (
            "ð Ø£ÙØ¶Ù ÙØ±Øµ Ø§ÙÙÙÙ\n\n"
            "â ÙØ§ ØªÙØ¬Ø¯ ÙØ±Øµ ØªØ­ÙÙ "
            "Ø§ÙØ´Ø±ÙØ· Ø­Ø§ÙÙÙØ§."
        )

    message = (
        f"ð Ø£ÙØ¶Ù {len(results)} ÙØ±Øµ ÙÙÙÙÙ\n"
        f"ð° Ask â¤ $5 | "
        f"â­ ØªÙÙÙÙ 80+ | "
        f"ð¥ ÙØ´Ø§Ø· 3+ | "
        f"ð Volume â¥ 1,000\n"
        f"ââââââââââââââ\n\n"
    )

    for index, item in enumerate(
        results,
        start=1
    ):
        contract = item["contract"]
        decision = item["decision"]

        message += (
            f"{index}ï¸â£ "
            f"{item['symbol']} "
            f"{contract['side']} "
            f"{contract['strike']:g} | "
            f"{contract['expiry_date']}\n"
            f"{decision['label']}\n"
            f"â­ ØªÙÙÙÙ "
            f"{contract['score']}/100 | "
            f"ð¥ ÙØ´Ø§Ø· "
            f"{contract['uoa_score']}/10\n"
            f"ðµ Ask "
            f"${contract['ask']:.2f} | "
            f"ð Volume "
            f"{contract['volume']:,}\n\n"
            f"ââââââââââââââ\n\n"
        )

    return message


def format_top_contracts(
    symbol,
    contracts,
    trend
):
    qualified = [
        contract
        for contract in contracts
        if (
            contract["side"] == trend["bias"]
            and contract["score"] >= MIN_TOP_SCORE
            and contract["uoa_score"] >= MIN_TOP_UOA
            and contract["volume"] >= MIN_VOLUME
            and contract["decision"]["rank"] > 0
        )
    ]

    qualified = qualified[:TOP_N_RESULTS]

    if not qualified:
        return (
            f"ð Ø£ÙØ¶Ù Ø§ÙØ¹ÙÙØ¯ ÙÙ {symbol}\n"
            f"ð {trend['label']} | "
            f"Ø²Ø®Ù {trend['momentum_score']}/10 | "
            f"{trend['continuation_label']}\n"
            f"ðµ ${trend['last_close']:.2f}\n"
            f"ââââââââââââââ\n\n"
            "â ÙØ§ ØªÙØ¬Ø¯ Ø¹ÙÙØ¯ ÙØ¤ÙÙØ© "
            "ÙØ´Ø±ÙØ·ÙØ§ Ø­Ø§ÙÙÙØ§."
        )

    message = (
        f"ð Ø£ÙØ¶Ù Ø§ÙØ¹ÙÙØ¯ ÙÙ {symbol}\n"
        f"ð {trend['label']} | "
        f"Ø²Ø®Ù {trend['momentum_score']}/10 | "
        f"{trend['continuation_label']}\n"
        f"ðµ ${trend['last_close']:.2f}\n"
        f"ââââââââââââââ\n\n"
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
            f"{index}ï¸â£ "
            f"{symbol} "
            f"{contract['side']} "
            f"{contract['strike']:g} | "
            f"{contract['expiry_date']}\n"
            f"{decision['label']}\n"
            f"â­ ØªÙÙÙÙ "
            f"{contract['score']}/100 | "
            f"ð¥ ÙØ´Ø§Ø· "
            f"{contract['uoa_score']}/10\n"
            f"ðµ Ask "
            f"${contract['ask']:.2f} | "
            f"ð Volume "
            f"{contract['volume']:,}\n\n"
            f"ââââââââââââââ\n\n"
        )

    return message


def format_watch_added(
    symbol,
    contract,
    trend
):
    decision = effective_decision(
        contract,
        trend
    )

    if (
        decision["label"]
        ==
        "ð¢ ÙØ±Ø´Ø­ ÙÙÙ â Ø§ÙØªØ¸Ø§Ø± Ø§ÙØªØªØ§Ø­ Ø§ÙØ³ÙÙ"
    ):
        watch_message = (
            "ð¢ ØªÙØª Ø¥Ø¶Ø§ÙØ© Ø§ÙÙØ±ØµØ© "
            "ÙØ§ÙØªØ¸Ø§Ø± Ø§ÙØªØªØ§Ø­ Ø§ÙØ³ÙÙ"
        )
    else:
        watch_message = (
            "ð ØªÙØª Ø¥Ø¶Ø§ÙØ© Ø§ÙÙØ±ØµØ© ÙÙÙØ±Ø§ÙØ¨Ø©"
        )

    return (
        f"{watch_message}\n\n"
        f"{symbol} "
        f"{contract['side']} "
        f"{contract['strike']:g} | "
        f"{contract['expiry_date']}\n"
        f"â­ ØªÙÙÙÙ "
        f"{contract['score']}/100 | "
        f"ð¥ ÙØ´Ø§Ø· "
        f"{contract['uoa_score']}/10\n"
        f"ðµ Ask "
        f"${contract['ask']:.2f} | "
        f"ð Volume "
        f"{contract['volume']:,}\n\n"
        "â±ï¸ Ø§ÙØªØ­ÙÙ ÙÙ 5 Ø¯ÙØ§Ø¦Ù Ø£Ø«ÙØ§Ø¡ Ø§ÙØ³ÙÙ"
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
        "ð¤ Ø¨ÙØª ØªØ­ÙÙÙ ÙØ§Ø®ØªÙØ§Ø± Ø¹ÙÙØ¯ Ø§ÙØ£ÙØ¨Ø´Ù\n\n"
        "Ø§Ø®ØªØ± Ø§ÙØ®Ø¯ÙØ© Ø§ÙÙØ·ÙÙØ¨Ø©:",
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
            "ð Ø¬Ø§Ø±Ù ÙØ­Øµ 30 Ø±ÙØ²ÙØ§ ÙØ®ØªØ§Ø±ÙØ§...\n"
            "â³ ØªÙØ¯Ø± ØªØ³ØªØ®Ø¯Ù Ø§ÙØ¨ÙØª Ø£Ø«ÙØ§Ø¡ Ø§ÙÙØ­Øµ."
        )

        try:
            results = (
                await asyncio.to_thread(
                    scan_top10
                )
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
                        f"â¢ {symbol} "
                        f"{contract['side']} "
                        f"{contract['strike']:g} | "
                        f"{contract['expiry_date']}\n"
                        f"  {decision['label']}"
                    )

                watch_text = (
                    "ð ÙØ±Øµ ØªØ­Øª Ø§ÙÙØ±Ø§ÙØ¨Ø©:\n\n"
                    + "\n\n".join(lines)
                    + "\n\n"
                    "â±ï¸ Ø§ÙØªØ­ÙÙ ÙÙ 5 Ø¯ÙØ§Ø¦Ù Ø£Ø«ÙØ§Ø¡ Ø§ÙØ³ÙÙ"
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
                "â ï¸ ØªØ¹Ø°Ø± Ø¥ÙÙØ§Ù Ø§ÙÙØ­Øµ Ø­Ø§ÙÙÙØ§.",
                reply_markup=main_menu()
            )

    elif query.data == "scan_options":
        context.user_data["mode"] = "scan"

        await query.message.reply_text(
            "ð Ø§ÙØªØ¨ Ø±ÙØ² Ø§ÙØ´Ø±ÙØ©:\n\n"
            "ÙØ«Ø§Ù:\nNVDA"
        )

    elif query.data == "contract":
        context.user_data["mode"] = "contract"

        await query.message.reply_text(
            "ð Ø£Ø±Ø³Ù Ø¨ÙØ§ÙØ§Øª Ø§ÙØ¹ÙØ¯:\n\n"
            "SYMBOL CALL/PUT STRIKE "
            "DTE DELTA VOLUME OI SPREAD"
        )

    elif query.data == "opportunity":
        context.user_data["mode"] = "opportunity"

        await query.message.reply_text(
            "ð¯ Ø£Ø±Ø³Ù Ø¨ÙØ§ÙØ§Øª Ø§ÙÙØ±ØµØ©:\n\n"
            "SYMBOL DIRECTION MOMENTUM VOLUME TREND"
        )

    elif query.data == "chart":
        context.user_data["mode"] = "chart"

        await query.message.reply_text(
            "ð® Ø§ÙØªØ¨ Ø±ÙØ² Ø§ÙØ³ÙÙ:\n\n"
            "ÙØ«Ø§Ù:\nTSLA"
        )

    elif query.data.startswith("chart_"):
        symbol = context.user_data.get(
            "chart_symbol"
        )

        if not symbol:
            await query.message.reply_text(
                "â ï¸ Ø§Ø®ØªØ± Ø§ÙØ´Ø§Ø±Øª Ø§ÙÙØªÙÙØ¹ "
                "ÙØ§ÙØªØ¨ Ø§ÙØ±ÙØ² Ø£ÙÙÙØ§.",
                reply_markup=main_menu()
            )
            return

        resolution = query.data.split(
            "_",
            1
        )[1]

        await query.message.reply_text(
            f"ð® Ø¬Ø§Ø±Ù Ø¨ÙØ§Ø¡ Ø§ÙÙØ³Ø§Ø± Ø§ÙÙØªÙÙØ¹ ÙÙ "
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
                "â ï¸ ØªØ¹Ø°Ø± Ø¥ÙØ´Ø§Ø¡ Ø§ÙØ´Ø§Ø±Øª Ø§ÙÙØªÙÙØ¹ Ø­Ø§ÙÙÙØ§.",
                reply_markup=main_menu()
            )

    elif query.data == "news":
        await query.message.reply_text(
            "ð° Ø¬Ø§Ø±Ù ÙØ­Øµ Ø§ÙØ£Ø®Ø¨Ø§Ø± Ø§ÙÙÙÙØ© "
            "ÙØ£Ø³ÙÙÙØ§..."
        )

        try:
            results = (
                await asyncio.to_thread(
                    scan_important_news
                )
            )

            await query.message.reply_text(
                format_news_results(
                    results
                ),
                reply_markup=main_menu(),
                disable_web_page_preview=True
            )

        except Exception as e:
            print(
                "NEWS BUTTON ERROR:",
                e
            )

            await query.message.reply_text(
                "â ï¸ ØªØ¹Ø°Ø± Ø¬ÙØ¨ Ø§ÙØ£Ø®Ø¨Ø§Ø± Ø­Ø§ÙÙÙØ§. "
                "ØªØ£ÙØ¯Ù Ø£Ù FINNHUB_TOKEN ÙÙØ¬ÙØ¯ ÙÙ Render.",
                reply_markup=main_menu()
            )

    elif query.data == "earnings":
        await query.message.reply_text(
            "ð Ø¬Ø§Ø±Ù ÙØ­Øµ Ø¥Ø¹ÙØ§ÙØ§Øª "
            "Ø£Ø³ÙÙÙØ§ Ø§ÙÙØ§Ø¯ÙØ©..."
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
                "â ï¸ ØªØ¹Ø°Ø± Ø¬ÙØ¨ Ø¥Ø¹ÙØ§ÙØ§Øª Ø§ÙØ´Ø±ÙØ§Øª Ø­Ø§ÙÙÙØ§. "
                "ØªØ£ÙØ¯Ù Ø£Ù FINNHUB_TOKEN ÙÙØ¬ÙØ¯ ÙÙ Render.",
                reply_markup=main_menu()
            )

    elif query.data == "help":
        await query.message.reply_text(
            "â¹ï¸ Ø·Ø±ÙÙØ© Ø§ÙØ§Ø³ØªØ®Ø¯Ø§Ù\n\n"
            "ð Ø£ÙØ¶Ù ÙØ±Øµ Ø§ÙÙÙÙ: ÙÙØ­Øµ 30 Ø±ÙØ²ÙØ§ ÙØ®ØªØ§Ø±ÙØ§.\n\n"
            "ð Ø§ÙØ¨Ø­Ø« Ø§ÙÙØ¯ÙÙ: Ø§ÙØªØ¨ Ø±ÙØ² Ø£Ù Ø³ÙÙ.\n\n"
            "ð® Ø§ÙØ´Ø§Ø±Øª Ø§ÙÙØªÙÙØ¹: ÙØ¹Ø±Ø¶ Ø§ÙØ´ÙÙØ¹ ÙÙÙØ§Ø·Ù "
            "Ø§ÙØ¯Ø¹Ù ÙØ§ÙÙÙØ§ÙÙØ© ÙÙØ³Ø§Ø± Ø§ÙØ­Ø±ÙØ© Ø§ÙÙØ±Ø¬Ø­.\n\n"
            "ð° Ø£ÙÙ Ø§ÙØ£Ø®Ø¨Ø§Ø±: ÙØ¹Ø±Ø¶ Ø§ÙØ£Ø®Ø¨Ø§Ø± Ø§ÙÙÙÙØ© "
            "ÙÙØ· ÙØ£Ø³ÙÙ ÙØ§Ø¦ÙØªÙØ§Ø ÙØ¹ ØªÙÙÙÙ Ø§ÙØ£ÙÙÙØ© "
            "ÙØ§ÙØªØ£Ø«ÙØ±.\n\n"
            "ð Ø¥Ø¹ÙØ§ÙØ§Øª Ø§ÙØ´Ø±ÙØ§Øª: ÙØ¹Ø±Ø¶ Ø§ÙØ¥Ø¹ÙØ§ÙØ§Øª "
            "Ø§ÙÙØ§Ø¯ÙØ© ÙØ£Ø³ÙÙÙØ§Ø ÙØ¹ ØªÙØ¨ÙÙ ØªÙÙØ§Ø¦Ù ÙØ¨Ù "
            "7 Ø£ÙØ§Ù ÙØªØ°ÙÙØ± ÙÙ ÙÙØ³ ÙÙÙ Ø§ÙØ¥Ø¹ÙØ§Ù.\n\n"
            "Ø§ÙÙÙÙÙ: 10 Ø¬ÙØ³Ø§Øª ÙØ§Ø¯ÙØ©.\n"
            "4 Ø³Ø§Ø¹Ø§Øª: 12 Ø´ÙØ¹Ø©.\n"
            "Ø§ÙØ³Ø§Ø¹Ø©: 12 Ø´ÙØ¹Ø©.\n"
            "15 Ø¯ÙÙÙØ©: 12 Ø´ÙØ¹Ø©.\n\n"
            "ð¢ ØªØ£ÙÙØ¯ Ø§ÙØ¯Ø®ÙÙ ÙØ¸ÙØ± Ø£Ø«ÙØ§Ø¡ Ø§ÙØ³ÙÙ ÙÙØ·.\n"
            "ð¢ ÙØ±Ø´Ø­ ÙÙÙ: Ø§ÙØªØ¸Ø§Ø± Ø§ÙØªØªØ§Ø­ Ø§ÙØ³ÙÙ.\n"
            "ð¡ Ø§ÙØªØ¸Ø§Ø± ØªØ£ÙÙØ¯: ÙØ¯Ø®Ù Ø§ÙÙØ±Ø§ÙØ¨Ø© ØªÙÙØ§Ø¦ÙÙØ§.\n"
            "â±ï¸ Ø§ÙØªØ­ÙÙ ÙÙ 5 Ø¯ÙØ§Ø¦Ù Ø£Ø«ÙØ§Ø¡ Ø§ÙØ³ÙÙ.\n\n"
            "â­ ØªÙÙÙÙ 80+\n"
            "ð¥ ÙØ´Ø§Ø· 3+\n"
            "ð Volume 1,000+\n"
            "ð° Ask â¤ $5\n"
            "ð§­ ÙØ¹ Ø§ØªØ¬Ø§Ù Ø§ÙØ³ÙÙ",
            reply_markup=main_menu()
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
                    "â ï¸ Ø§ÙØªØ¨ Ø±ÙØ² Ø³ÙÙ ØµØ­ÙØ­.\n\n"
                    "ÙØ«Ø§Ù: NVDA"
                )
                return

            await update.message.reply_text(
                f"ð Ø¬Ø§Ø±Ù ØªØ­ÙÙÙ {symbol}...\n"
                "â³ ÙØ­Ø¸Ø©..."
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

            qualified = [
                contract
                for contract in contracts
                if (
                    contract["side"] == trend["bias"]
                    and contract["score"] >= MIN_TOP_SCORE
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
                        "ð¡ Ø§ÙØªØ¸Ø§Ø± ØªØ£ÙÙØ¯",
                        "ð¢ ÙØ±Ø´Ø­ ÙÙÙ â Ø§ÙØªØ¸Ø§Ø± Ø§ÙØªØªØ§Ø­ Ø§ÙØ³ÙÙ",
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
            parts = text.split()

            if len(parts) != 8:
                raise ValueError

            symbol = parts[0].upper()
            direction = parts[1].upper()
            strike = float(parts[2])
            dte = int(parts[3])
            delta = float(parts[4])
            volume = int(parts[5])
            oi = int(parts[6])
            spread = float(parts[7])

            raw_score = contract_score(
                delta,
                volume,
                oi,
                spread,
                dte
            )

            score = normalize_contract_score(
                raw_score
            )

            await update.message.reply_text(
                f"ð ØªØ­ÙÙÙ Ø§ÙØ¹ÙØ¯\n\n"
                f"Ø§ÙØ³ÙÙ: {symbol}\n"
                f"Ø§ÙØ§ØªØ¬Ø§Ù: {direction}\n"
                f"Strike: {strike}\n"
                f"DTE: {dte}\n"
                f"Delta: {delta}\n"
                f"Volume: {volume:,}\n"
                f"OI: {oi:,}\n"
                f"Spread: {spread}%\n\n"
                f"â­ Ø§ÙØ¬ÙØ¯Ø©: {score}/100",
                reply_markup=main_menu()
            )

            context.user_data["mode"] = None

        elif mode == "opportunity":
            parts = text.split()

            if len(parts) != 5:
                raise ValueError

            symbol = parts[0].upper()
            direction = parts[1].upper()
            momentum = float(parts[2])
            volume = float(parts[3])
            trend_score = float(parts[4])

            score = min(
                round(
                    (
                        momentum * 0.4
                        + volume * 0.3
                        + trend_score * 0.3
                    )
                    * 10
                ),
                100
            )

            await update.message.reply_text(
                f"ð¯ ØªÙÙÙÙ Ø§ÙÙØ±ØµØ©\n\n"
                f"Ø§ÙØ³ÙÙ: {symbol}\n"
                f"Ø§ÙØ§ØªØ¬Ø§Ù: {direction}\n"
                f"â­ Ø§ÙÙØªÙØ¬Ø©: {score}/100",
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
                    "â ï¸ Ø§ÙØªØ¨ Ø±ÙØ² Ø³ÙÙ ØµØ­ÙØ­.\n\n"
                    "ÙØ«Ø§Ù: TSLA"
                )
                return

            context.user_data["chart_symbol"] = symbol
            context.user_data["mode"] = None

            await update.message.reply_text(
                f"ð® {symbol}\n\n"
                "Ø§Ø®ØªØ± Ø¥Ø·Ø§Ø± Ø§ÙØªÙÙØ¹:",
                reply_markup=chart_timeframe_menu()
            )

        else:
            await update.message.reply_text(
                "Ø§Ø¶ØºØ· /start ÙØ§Ø®ØªØ± Ø§ÙØ®Ø¯ÙØ©.",
                reply_markup=main_menu()
            )

    except requests.exceptions.RequestException as e:
        print(
            "API ERROR:",
            e
        )

        await update.message.reply_text(
            "â ï¸ ØªØ¹Ø°Ø± Ø§ÙØ§ØªØµØ§Ù Ø¨Ø¨ÙØ§ÙØ§Øª Ø§ÙØ³ÙÙ Ø­Ø§ÙÙÙØ§.",
            reply_markup=main_menu()
        )

    except Exception as e:
        print(
            "ERROR:",
            e
        )

        await update.message.reply_text(
            "â ï¸ Ø­ØµÙ Ø®Ø·Ø£ Ø£Ø«ÙØ§Ø¡ ØªØ­ÙÙÙ Ø§ÙØ¨ÙØ§ÙØ§Øª.",
            reply_markup=main_menu()
        )


# =========================================================
# START BACKGROUND TASKS
# =========================================================

async def post_init(application):
    asyncio.create_task(
        monitor_pending(application)
    )

    if FINNHUB_TOKEN:
        asyncio.create_task(
            monitor_important_news(
                application
            )
        )

        asyncio.create_task(
            send_earnings_reminders(
                application
            )
        )

        print(
            "NEWS + EARNINGS MONITORS STARTED"
        )
    else:
        print(
            "WARNING: FINNHUB_TOKEN is missing"
        )

    print(
        "AUTO WATCHER STARTED"
    )


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
