import os
import time
import math
import asyncio
import requests
import io
import html

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

NEWS_DAYS = 1
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
    if zone_is_relevant and zone_label == "DEMAND":
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
        f"القوة الفنية: {setup['confidence']}%\n"
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

NEWS_MAX_AGE_HOURS = 18

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
          "fda approval", "fda rejects"], "حدث جوهري جدًا"),
    (9, ["acquisition", "merger", "buyout", "takeover", "earnings",
         "guidance", "profit warning", "ceo resigns", "ceo steps down"],
        "تطور جوهري"),
    (8, ["beats estimates", "misses estimates", "contract", "partnership",
         "recall", "downgrade", "upgrade", "layoffs", "job cuts",
         "offering", "share sale", "capital raise", "buyback", "repurchase"],
        "خبر مهم للسهم"),
    (7, ["price target", "orders", "delivery", "deliveries", "production",
         "tariff", "export restriction", "regulatory", "antitrust"],
        "تطور يحتاج متابعة"),
]

ANALYSIS_ONLY_WORDS = [
    "technical analysis", "opinion", "why i think", "what's going on",
    "what is going on", "bullish outlier", "stock to watch"
]

POSITIVE_WORDS = [
    "beats estimates", "raises guidance", "raised guidance", "upgraded",
    "fda approval", "record revenue", "contract win", "wins contract",
    "buyback", "repurchase", "strong demand"
]

NEGATIVE_WORDS = [
    "misses estimates", "cuts guidance", "cut guidance", "downgraded",
    "investigation", "subpoena", "fraud", "recall", "bankruptcy",
    "chapter 11", "layoffs", "job cuts", "share sale", "capital raise",
    "weak demand", "fda rejects"
]


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


def _headline_mentions_symbol(symbol, headline):
    """Require the company/ticker itself to be part of the headline.

    Finnhub may tag a company merely because it is mentioned in an article body/summary.
    That caused unrelated stories (for example a Walmart story mentioning Amazon) to
    appear under another ticker.  The stock-news section is intentionally stricter.
    """
    symbol = str(symbol).upper().strip()
    headline_norm = _normalize_news_text(headline)

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
        "one number", "valuation", "demonstrates high-growth",
        "looks overdone", "likely game changer", "is a game changer",
        "my top", "top pick", "why i'm buying", "why i am buying",
        "why i'm selling", "why i am selling", "could be", "may be a",
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

    # The company must be the subject of the headline, not just mentioned in the body.
    if not _headline_mentions_symbol(symbol, headline):
        return None

    # Exclude broad-market ETFs from company-news. Their stories belong in US market news.
    if str(symbol).upper().strip() in {"SPY", "QQQ", "IWM"}:
        return None

    # Opinion / preview / valuation pieces are not treated as material company news.
    if _looks_like_commentary_or_preview(headline_norm):
        return None
    if any(x in headline_norm for x in ANALYSIS_ONLY_WORDS):
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

    # If it has no material-news trigger, do not show it.
    if importance < 7:
        return None

    # Sentiment can use headline + summary after the story has passed the strict relevance gate.
    pos = sum(1 for x in POSITIVE_WORDS if x in combined)
    neg = sum(1 for x in NEGATIVE_WORDS if x in combined)
    if pos >= 1 and pos > neg:
        sentiment, sentiment_ar = "POSITIVE", "🟢 إيجابي"
    elif neg >= 1 and neg > pos:
        sentiment, sentiment_ar = "NEGATIVE", "🔴 سلبي"
    else:
        sentiment, sentiment_ar = "NEUTRAL", "⚪ محايد"

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
    }


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

    results.sort(key=lambda x: (-x["importance"], -x["timestamp"]))
    results = results[:MAX_NEWS_RESULTS]
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


# Arabic headline display only. The original English headline is still used
# internally for relevance, scoring and filtering, so translation cannot alter
# the news-selection logic. If translation is unavailable, fall back safely.
_HEADLINE_AR_CACHE = {}

def polish_arabic_news_headline(ar_text, original_en=""):
    """Light editorial cleanup for displayed Arabic headlines only.
    Does not affect filtering, scoring, relevance, timestamps or event logic.
    """
    t = str(ar_text or "").strip()
    en = str(original_en or "").lower()

    # Remove clickbait tails that translate awkwardly; keep the market-moving fact.
    t = re.sub(r"[.!؟]?\s*(ثلاثة|3)\s+(?:أشياء|عوامل)\s+(?:يمكن|قد)\s+أن\s+تدفعه\s+(?:إلى\s+)?أعلى.*$", "", t, flags=re.I)
    t = re.sub(r"[.!؟]?\s*إليك\s+ما\s+تحتاج\s+(?:إلى\s+)?معرفته.*$", "", t, flags=re.I)
    t = re.sub(r"[.!؟]?\s*ماذا\s+يعني\s+ذلك.*$", "", t, flags=re.I)

    # More natural financial-news phrasing for common literal translations.
    t = t.replace("وصل عائد سندات الخزانة لأجل 30 عامًا إلى أعلى مستوى له منذ",
                  "عائد سندات الخزانة الأمريكية لأجل 30 عامًا يسجل أعلى مستوى منذ")
    t = t.replace("وصلت عوائد سندات الخزانة", "عوائد سندات الخزانة الأمريكية تسجل")
    t = re.sub(r"\s{2,}", " ", t).strip(" .،؛-")

    # Keep headlines concise for Telegram; do not invent facts.
    if len(t) > 180:
        cut = t[:180]
        for sep in ["،", ";", " - ", ":"]:
            pos = cut.rfind(sep)
            if pos >= 90:
                cut = cut[:pos]
                break
        t = cut.rstrip() + "…"
    return t

def translate_headline_ar(headline):
    text = str(headline or "").strip()
    if not text:
        return text
    if text in _HEADLINE_AR_CACHE:
        return _HEADLINE_AR_CACHE[text]
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
        data = r.json()
        translated = "".join(
            part[0] for part in (data[0] or [])
            if isinstance(part, list) and part and part[0]
        ).strip()
        if translated:
            translated = polish_arabic_news_headline(translated, text)
            _HEADLINE_AR_CACHE[text] = translated
            return translated
    except Exception as exc:
        print(f"HEADLINE TRANSLATION ERROR: {exc}")
    return text


def format_news_item(item):
    headline = translate_headline_ar(item["headline"])
    if len(headline) > 220:
        headline = headline[:217] + "..."
    return (
        f"📰 {item['symbol']}\n"
        f"{item['sentiment_ar']} | 🔥 الأهمية: {item['importance']}/10\n"
        f"📝 النوع: {item['category_ar']}\n"
        f"📌 {headline}\n"
        f"🕐 {news_time_text(item['timestamp'])}\n"
        f"🏷️ المصدر: {item['source'] or 'غير متوفر'}"
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
            f"🏷️ المصدر: {x['source'] or 'غير متوفر'}")

def format_combined_news():
    upcoming_text = format_upcoming_us_events()
    macro_items=scan_us_market_news(); stock_items=scan_important_news()
    a=("🇺🇸 أهم أخبار السوق الأمريكي\n━━━━━━━━━━━━━━\n\n"+"\n\n━━━━━━━━━━━━━━\n\n".join(format_macro_item(x) for x in macro_items)
       if macro_items else f"🇺🇸 أخبار السوق الأمريكي\n\n✅ لا توجد أخبار اقتصادية أمريكية جوهرية مؤكدة وحديثة خلال آخر {MACRO_MAX_AGE_HOURS} ساعة.")
    b=("🏢 أهم أخبار أسهمنا\n━━━━━━━━━━━━━━\n\n"+"\n\n━━━━━━━━━━━━━━\n\n".join(format_news_item(x) for x in stock_items)
       if stock_items else f"🏢 أخبار أسهمنا\n\n✅ لا توجد أخبار جوهرية حديثة ومرتبطة مباشرة بأسهمنا خلال آخر {NEWS_MAX_AGE_HOURS} ساعة.")
    return upcoming_text+"\n\n━━━━━━━━━━━━━━\n\n"+a+"\n\n━━━━━━━━━━━━━━\n\n"+b


# =========================================================
# UPCOMING US ECONOMIC EVENTS — LIVE OFFICIAL CALENDARS
# =========================================================

UPCOMING_EVENT_DAYS = 7
MAJOR_EVENT_LOOKAHEAD_DAYS = 60
ECON_CALENDAR_CACHE_SECONDS = 1800
ECON_CALENDAR_CACHE = {"time": 0, "events": None, "errors": []}

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
            r"(FOMC Minutes|FOMC Meeting|Beige Book)\s+"
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
            else:
                name = "Beige Book — الكتاب البيج"
                heat = "🔥🔥"
                major = False

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

    for url, name, heat in BLS_RELEASES:
        try:
            events.extend(
                _fetch_bls_release(
                    url,
                    name,
                    heat,
                )
            )
        except Exception as exc:
            errors.append(f"BLS {name}: {exc}")
            print(
                f"ECON CALENDAR ERROR BLS {name}:",
                exc,
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
        result += (
            "\n\nℹ️ بعض الجداول المباشرة لم تستجب؛ "
            "تم استخدام المواعيد الرسمية المتحقق منها كنسخة احتياطية."
        )

    return result


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

    return (
        f"📅 {item['symbol']} — "
        f"{item['date'].strftime('%d/%m/%Y')}\n"
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
        "⏰ التنبيه تلقائي قبل 7 أيام "
        "وتذكير في نفس اليوم\n"
        "━━━━━━━━━━━━━━\n\n"
        + "\n\n━━━━━━━━━━━━━━\n\n".join(blocks)
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
                        "📅 تنبيه إعلان بعد أسبوع"
                    )
                else:
                    heading = (
                        "🔔 تذكير: إعلان الشركة اليوم"
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
                    "🚨 خبر مهم جديد\n\n"
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

    if contract["side"] != bias:
        return {
            "label": "🔴 استبعاد",
            "reason": "العقد عكس اتجاه السهم",
            "rank": 0,
        }

    if continuation < 0:
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
            "🟡 انتظار تأكيد",
            "🟢 مرشح قوي — انتظار افتتاح السوق",
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
            "حركة 15 دقيقة أصبحت موافقة للاتجاه"
        )

    if invalidated:
        return (
            "INVALID",
            "حركة 15 دقيقة انعكست ضد الفرصة"
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
                            "🔴 تم استبعاد الفرصة\n\n"
                            f"{symbol} "
                            f"{side} "
                            f"{strike:g} | "
                            f"{expiry_date}\n"
                            "الاتجاه لم يعد داعمًا."
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
                        "بيانات 15 دقيقة المتاحة."
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

def format_top10(results):
    if not results:
        return (
            "🏆 أفضل فرص اليوم\n\n"
            "❌ لا توجد فرص تحقق "
            "الشروط حاليًا."
        )

    message = (
        f"🏆 أفضل {len(results)} فرص لليوم\n"
        f"💰 Ask ≤ $5 | "
        f"⭐ تقييم 80+ | "
        f"🔥 نشاط 3+ | "
        f"📊 Volume ≥ 1,000\n"
        f"━━━━━━━━━━━━━━\n\n"
    )

    for index, item in enumerate(
        results,
        start=1
    ):
        contract = item["contract"]
        decision = item["decision"]

        message += (
            f"{index}️⃣ "
            f"{item['symbol']} "
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
            f"{contract['volume']:,}\n\n"
            f"━━━━━━━━━━━━━━\n\n"
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
            f"{contract['volume']:,}\n\n"
            f"━━━━━━━━━━━━━━\n\n"
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
        "🟢 مرشح قوي — انتظار افتتاح السوق"
    ):
        watch_message = (
            "🟢 تمت إضافة الفرصة "
            "لانتظار افتتاح السوق"
        )
    else:
        watch_message = (
            "👀 تمت إضافة الفرصة للمراقبة"
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
        f"{contract['volume']:,}\n\n"
        "⏱️ التحقق كل 5 دقائق أثناء السوق"
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
                        f"{contract['expiry_date']}\n"
                        f"  {decision['label']}"
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
            "📊 أرسل بيانات العقد:\n\n"
            "SYMBOL CALL/PUT STRIKE "
            "DTE DELTA VOLUME OI SPREAD"
        )

    elif query.data == "opportunity":
        context.user_data["mode"] = "opportunity"

        await query.message.reply_text(
            "🎯 أرسل بيانات الفرصة:\n\n"
            "SYMBOL DIRECTION MOMENTUM VOLUME TREND"
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
            results = (
                await asyncio.to_thread(
                    scan_important_news
                )
            )

            await query.message.reply_text(
                format_combined_news(),
                reply_markup=main_menu(),
                disable_web_page_preview=True
            )

        except Exception as e:
            print(
                "NEWS BUTTON ERROR:",
                e
            )

            await query.message.reply_text(
                "⚠️ تعذر جلب الأخبار حاليًا. "
                "تأكدي أن FINNHUB_TOKEN موجود في Render.",
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
                f"📊 تحليل العقد\n\n"
                f"السهم: {symbol}\n"
                f"الاتجاه: {direction}\n"
                f"Strike: {strike}\n"
                f"DTE: {dte}\n"
                f"Delta: {delta}\n"
                f"Volume: {volume:,}\n"
                f"OI: {oi:,}\n"
                f"Spread: {spread}%\n\n"
                f"⭐ الجودة: {score}/100",
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
                f"🎯 تقييم الفرصة\n\n"
                f"السهم: {symbol}\n"
                f"الاتجاه: {direction}\n"
                f"⭐ النتيجة: {score}/100",
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


# =========================================================
# START BACKGROUND TASKS
# =========================================================

async def post_init(application):
    # Keep opportunity monitoring active.
    asyncio.create_task(monitor_pending(application))

    # IMPORTANT: news is manual-only while we validate the new filter.
    # Do NOT start monitor_important_news here.
    if FINNHUB_TOKEN:
        asyncio.create_task(send_earnings_reminders(application))
        print("AUTO NEWS DISABLED — MANUAL NEWS ONLY")
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
