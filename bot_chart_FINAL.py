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
    """Detect real price-action patterns without forcing a pattern.

    Supported patterns:
      - Symmetrical / ascending / descending triangles
      - Head & shoulders / inverse head & shoulders
      - Cup & handle
      - Double top / double bottom

    Detection is based on swing highs/lows and ATR tolerances. If no pattern passes
    the quality rules, the function explicitly returns "no clear pattern".
    """
    last_price = closes[-1]
    atr = max(calculate_atr(highs, lows, closes, 14), max(last_price * 0.002, 0.01))
    n = len(closes)

    def _pivot_points(values, mode="high", radius=2):
        pts = []
        for i in range(radius, len(values) - radius):
            window = values[i-radius:i+radius+1]
            v = values[i]
            if mode == "high" and v == max(window) and window.count(v) == 1:
                pts.append((i, float(v)))
            elif mode == "low" and v == min(window) and window.count(v) == 1:
                pts.append((i, float(v)))
        return pts

    def _fit_points(points):
        if len(points) < 2:
            return 0.0, points[-1][1] if points else 0.0, 0.0
        xs = [float(x) for x, _ in points]
        ys = [float(y) for _, y in points]
        m = len(xs)
        sx, sy = sum(xs), sum(ys)
        sxx = sum(x*x for x in xs)
        sxy = sum(x*y for x, y in zip(xs, ys))
        denom = m*sxx - sx*sx
        if abs(denom) < 1e-12:
            return 0.0, ys[-1], 0.0
        slope = (m*sxy - sx*sy) / denom
        intercept = (sy - slope*sx) / m
        ybar = sy/m
        ss_tot = sum((y-ybar)**2 for y in ys)
        ss_res = sum((y-(slope*x+intercept))**2 for x, y in zip(xs, ys))
        r2 = 1.0 - ss_res/ss_tot if ss_tot > 1e-12 else 1.0
        return slope, intercept, max(0.0, min(1.0, r2))

    def _between(points, left, right):
        return [(x, y) for x, y in points if left < x < right]

    def _detect_demand_zone():
        """Find a real demand area from either repeated defended lows or one strong recent origin.

        Priority 1: clustered swing lows that produced clear rebounds.
        Priority 2: a single recent swing low that launched an impulsive move of >= 2 ATR.
        The zone is never allowed above the current market price.
        """
        lookback = min(42, n)
        l = lows[-lookback:]
        c = closes[-lookback:]
        o = closes[-lookback:]  # bodies are approximated from closes; lows define the base.
        pivots = _pivot_points(l, "low", 2)
        if not pivots:
            return None

        tolerance = max(atr * 0.60, last_price * 0.004)
        defended = []
        strong_origins = []

        for i, p in pivots:
            after = c[i + 1:min(len(c), i + 10)]
            if not after:
                continue
            departure = max(after) - p
            bars_ago = (lookback - 1) - i
            if departure >= atr * 1.10:
                defended.append((i, p, departure, bars_ago))
            if departure >= atr * 2.0 and bars_ago <= 22:
                strong_origins.append((i, p, departure, bars_ago))

        best = None

        # Repeated defended lows: strongest evidence.
        for _, anchor, _, _ in defended:
            cluster = [(i, p, dep, ago) for i, p, dep, ago in defended if abs(p - anchor) <= tolerance]
            if len(cluster) < 2:
                continue
            prices = [p for _, p, _, _ in cluster]
            lower = min(prices) - atr * 0.10
            upper = max(prices) + atr * 0.38
            if upper >= last_price - atr * 0.05:
                continue
            recency = min(ago for *_, ago in cluster)
            score = len(cluster) * 30 + max(dep for *_, dep, _ in cluster) / atr * 8 - recency * 0.7
            candidate = {
                'lower': lower, 'upper': upper, 'touches': len(cluster),
                'score': score, 'kind': 'repeated', 'bars_ago': recency,
            }
            if best is None or candidate['score'] > best['score']:
                best = candidate

        # A single explosive origin is still a legitimate demand zone.
        for i, p, departure, bars_ago in strong_origins:
            lower = p - atr * 0.08
            # Keep the zone compact around the launch base instead of painting a huge band.
            local_closes = c[max(0, i - 1):min(len(c), i + 3)]
            body_ref = max(local_closes) if local_closes else p + atr * 0.45
            upper = min(p + atr * 0.70, max(p + atr * 0.32, body_ref))
            if upper >= last_price - atr * 0.05:
                continue
            score = 42 + min(28, (departure / atr) * 7) - bars_ago * 0.55
            candidate = {
                'lower': lower, 'upper': upper, 'touches': 1,
                'score': score, 'kind': 'origin', 'bars_ago': bars_ago,
            }
            if best is None or candidate['score'] > best['score']:
                best = candidate

        return best

    demand_zone = _detect_demand_zone()
    candidates = []

    # -----------------------------------------------------
    # TRIANGLES — allow symmetrical, ascending and descending.
    # -----------------------------------------------------
    for lookback in (20, 24, 28, 32, 36, 42, 48):
        if n < lookback:
            continue
        h = highs[-lookback:]
        l = lows[-lookback:]
        ph = _pivot_points(h, 'high', 2)[-6:]
        pl = _pivot_points(l, 'low', 2)[-6:]
        # A visible triangle needs several real touches, not just two fitted points.
        if len(ph) < 3 or len(pl) < 3:
            continue
        if (lookback - 1) - max(ph[-1][0], pl[-1][0]) > 8:
            continue
        us, ui, ur2 = _fit_points(ph)
        ls, li, lr2 = _fit_points(pl)
        end_x = float(lookback - 1)
        start_x = float(min(ph[0][0], pl[0][0]))
        u0, l0 = us*start_x+ui, ls*start_x+li
        u1, l1 = us*end_x+ui, ls*end_x+li
        gap0, gap1 = u0-l0, u1-l1
        if gap0 <= atr*0.9 or gap1 <= 0 or gap1 >= gap0*0.90:
            continue
        convergence = 1.0-gap1/gap0
        flat_tol = atr*0.025
        kind = None
        direction = 'NEUTRAL'
        name = None
        if us < -flat_tol and ls > flat_tol:
            kind, name = 'triangle_sym', 'مثلث متماثل'
        elif abs(us) <= flat_tol*1.8 and ls > flat_tol:
            kind, name, direction = 'triangle_asc', 'مثلث صاعد', 'BULLISH'
        elif us < -flat_tol and abs(ls) <= flat_tol*1.8:
            kind, name, direction = 'triangle_desc', 'مثلث هابط', 'BEARISH'
        if not kind or ur2 < 0.35 or lr2 < 0.35 or convergence < 0.12:
            continue
        tol = max(atr*0.55, last_price*0.004)
        if last_price > u1+tol or last_price < l1-tol:
            continue
        midpoint = (u1+l1)/2
        if direction == 'NEUTRAL':
            recent_slope = (closes[-1]-closes[max(0, len(closes)-8)]) / max(1, min(7, len(closes)-1))
            if last_price >= midpoint and recent_slope >= 0:
                direction='BULLISH'
            elif last_price < midpoint and recent_slope < 0:
                direction='BEARISH'
        measured = max(gap0, atr*2.8)
        if direction == 'BEARISH':
            trigger = l1 - atr*0.10
            invalid = u1 + atr*0.22
            targets = [trigger-measured*x for x in (0.30,0.50,0.72,1.0)]
        else:
            trigger = u1 + atr*0.10
            invalid = l1 - atr*0.22
            targets = [trigger+measured*x for x in (0.30,0.50,0.72,1.0)]
        candidates.append({
            'score': 62 + convergence*30 + (ur2+lr2)*8,
            'type': kind, 'name_ar': name, 'direction': direction,
            'lookback': lookback, 'trigger': trigger, 'invalidation': invalid,
            'targets': targets,
            'pattern_lines': [
                [(float(ph[0][0]), us*ph[0][0]+ui), (end_x+6, us*(end_x+6)+ui)],
                [(float(pl[0][0]), ls*pl[0][0]+li), (end_x+6, ls*(end_x+6)+li)],
            ],
            'label_xy': (max(ph[0][0], pl[0][0])+1, max(u1,l1)),
            'confidence': int(min(91, 62+convergence*22+(ur2+lr2)*6)),
        })

    # Use a recent window for classic reversal patterns.
    lookback = min(50, n)
    offset = n-lookback
    H = highs[-lookback:]
    L = lows[-lookback:]
    C = closes[-lookback:]
    ph = _pivot_points(H, 'high', 2)
    pl = _pivot_points(L, 'low', 2)

    # -----------------------------------------------------
    # HEAD & SHOULDERS / INVERSE H&S
    # -----------------------------------------------------
    for a in range(max(0, len(ph)-6), len(ph)-2):
        h1,h2,h3 = ph[a],ph[a+1],ph[a+2]
        if not (3 <= h2[0]-h1[0] <= 18 and 3 <= h3[0]-h2[0] <= 18):
            continue
        lows12 = _between(pl,h1[0],h2[0]); lows23 = _between(pl,h2[0],h3[0])
        if not lows12 or not lows23: continue
        n1=min(lows12,key=lambda p:p[1]); n2=min(lows23,key=lambda p:p[1])
        shoulders_tol=max(atr*0.85,last_price*0.018)
        if abs(h1[1]-h3[1]) > shoulders_tol: continue
        if (lookback - 1) - h3[0] > 9: continue
        if abs((h2[0]-h1[0]) - (h3[0]-h2[0])) > 7: continue
        if h2[1]-max(h1[1],h3[1]) < atr*0.80: continue
        if h1[1]-C[h1[0]] > atr*0.90 or h3[1]-C[h3[0]] > atr*0.90: continue
        neck=(n1[1]+n2[1])/2
        height=h2[1]-neck
        if height < atr*1.6: continue
        trigger=neck-atr*0.08; invalid=h2[1]+atr*0.18
        targets=[trigger-height*x for x in (0.30,0.50,0.72,1.0)]
        score=79 - abs(h1[1]-h3[1])/shoulders_tol*8
        candidates.append({
            'score':score,'type':'head_shoulders','name_ar':'رأس وكتفين','direction':'BEARISH',
            'lookback':lookback,'trigger':trigger,'invalidation':invalid,'targets':targets,
            'pattern_lines':[[(float(x),float(y)) for x,y in (h1,n1,h2,n2,h3)]],
            'label_xy':(h2[0],h2[1]+atr*0.35),'confidence':int(max(70,min(90,score))),
        })

    for a in range(max(0, len(pl)-6), len(pl)-2):
        l1,l2,l3 = pl[a],pl[a+1],pl[a+2]
        if not (3 <= l2[0]-l1[0] <= 18 and 3 <= l3[0]-l2[0] <= 18): continue
        highs12=_between(ph,l1[0],l2[0]); highs23=_between(ph,l2[0],l3[0])
        if not highs12 or not highs23: continue
        n1=max(highs12,key=lambda p:p[1]); n2=max(highs23,key=lambda p:p[1])
        shoulders_tol=max(atr*0.85,last_price*0.018)
        if abs(l1[1]-l3[1]) > shoulders_tol: continue
        if (lookback - 1) - l3[0] > 9: continue
        if abs((l2[0]-l1[0]) - (l3[0]-l2[0])) > 7: continue
        if min(l1[1],l3[1])-l2[1] < atr*0.80: continue
        if C[l1[0]]-l1[1] > atr*0.90 or C[l3[0]]-l3[1] > atr*0.90: continue
        neck=(n1[1]+n2[1])/2
        height=neck-l2[1]
        if height < atr*1.6: continue
        trigger=neck+atr*0.08; invalid=l2[1]-atr*0.18
        targets=[trigger+height*x for x in (0.30,0.50,0.72,1.0)]
        score=79-abs(l1[1]-l3[1])/shoulders_tol*8
        candidates.append({
            'score':score,'type':'inverse_head_shoulders','name_ar':'رأس وكتفين مقلوب','direction':'BULLISH',
            'lookback':lookback,'trigger':trigger,'invalidation':invalid,'targets':targets,
            'pattern_lines':[[(float(x),float(y)) for x,y in (l1,n1,l2,n2,l3)]],
            'label_xy':(l2[0],l2[1]-atr*0.35),'confidence':int(max(70,min(90,score))),
        })

    # -----------------------------------------------------
    # DOUBLE TOP / DOUBLE BOTTOM — strict visual validation.
    # -----------------------------------------------------
    # These rules intentionally reject wick-only or stale formations. A valid
    # double top/bottom must be recent, visually balanced, have a meaningful
    # neckline swing, and the second peak/trough must be confirmed by nearby closes.
    for i in range(max(0, len(ph)-6), len(ph)-1):
        p1, p2 = ph[i], ph[i+1]
        sep = p2[0] - p1[0]
        if not 6 <= sep <= 22:
            continue
        # The second top has to be part of the current structure, not an old wick.
        if (lookback - 1) - p2[0] > 9:
            continue
        tops_tol = max(atr * 0.50, last_price * 0.008)
        if abs(p1[1] - p2[1]) > tops_tol:
            continue
        mids = _between(pl, p1[0], p2[0])
        if not mids:
            continue
        trough = min(mids, key=lambda p: p[1])
        height = ((p1[1] + p2[1]) / 2.0) - trough[1]
        if not (atr * 1.10 <= height <= atr * 4.8):
            continue
        # Reject peaks that exist only because of a long isolated wick.
        c1 = C[p1[0]]
        c2 = C[p2[0]]
        if p1[1] - c1 > atr * 0.75 or p2[1] - c2 > atr * 0.75:
            continue
        # Both peaks should have some local body participation near the top.
        if max(C[max(0,p1[0]-1):min(lookback,p1[0]+2)]) < p1[1] - atr * 0.65:
            continue
        if max(C[max(0,p2[0]-1):min(lookback,p2[0]+2)]) < p2[1] - atr * 0.65:
            continue
        trigger = trough[1] - atr * 0.08
        invalid = max(p1[1], p2[1]) + atr * 0.14
        # Current price should be near the neckline / fresh breakdown area.
        if abs(last_price - trigger) > atr * 2.2:
            continue
        # No fresh higher high after the second top.
        if p2[0] < lookback - 2 and max(H[p2[0]+1:]) > invalid + atr * 0.05:
            continue
        targets = [trigger - height*x for x in (0.30, 0.50, 0.72, 1.0)]
        similarity = 1.0 - min(1.0, abs(p1[1]-p2[1]) / tops_tol)
        score = 76 + similarity * 8 + min(6, height/atr)
        candidates.append({
            'score': score, 'type': 'double_top', 'name_ar': 'قمة مزدوجة', 'direction': 'BEARISH',
            'lookback': lookback, 'trigger': trigger, 'invalidation': invalid, 'targets': targets,
            'pattern_lines': [[(float(x), float(y)) for x,y in (p1, trough, p2)]],
            'label_xy': ((p1[0]+p2[0])/2, max(p1[1],p2[1])+atr*0.30),
            'confidence': int(min(90, score)),
        })

    for i in range(max(0, len(pl)-6), len(pl)-1):
        p1, p2 = pl[i], pl[i+1]
        sep = p2[0] - p1[0]
        if not 6 <= sep <= 22:
            continue
        if (lookback - 1) - p2[0] > 9:
            continue
        bottoms_tol = max(atr * 0.50, last_price * 0.008)
        if abs(p1[1] - p2[1]) > bottoms_tol:
            continue
        mids = _between(ph, p1[0], p2[0])
        if not mids:
            continue
        peak = max(mids, key=lambda p: p[1])
        height = peak[1] - ((p1[1] + p2[1]) / 2.0)
        if not (atr * 1.10 <= height <= atr * 4.8):
            continue
        c1 = C[p1[0]]
        c2 = C[p2[0]]
        if c1 - p1[1] > atr * 0.75 or c2 - p2[1] > atr * 0.75:
            continue
        if min(C[max(0,p1[0]-1):min(lookback,p1[0]+2)]) > p1[1] + atr * 0.65:
            continue
        if min(C[max(0,p2[0]-1):min(lookback,p2[0]+2)]) > p2[1] + atr * 0.65:
            continue
        trigger = peak[1] + atr * 0.08
        invalid = min(p1[1], p2[1]) - atr * 0.14
        if abs(last_price - trigger) > atr * 2.2:
            continue
        if p2[0] < lookback - 2 and min(L[p2[0]+1:]) < invalid - atr * 0.05:
            continue
        targets = [trigger + height*x for x in (0.30, 0.50, 0.72, 1.0)]
        similarity = 1.0 - min(1.0, abs(p1[1]-p2[1]) / bottoms_tol)
        score = 76 + similarity * 8 + min(6, height/atr)
        candidates.append({
            'score': score, 'type': 'double_bottom', 'name_ar': 'قاع مزدوج', 'direction': 'BULLISH',
            'lookback': lookback, 'trigger': trigger, 'invalidation': invalid, 'targets': targets,
            'pattern_lines': [[(float(x), float(y)) for x,y in (p1, peak, p2)]],
            'label_xy': ((p1[0]+p2[0])/2, min(p1[1],p2[1])-atr*0.30),
            'confidence': int(min(90, score)),
        })

    # -----------------------------------------------------
    # CUP & HANDLE — conservative detection.
    # -----------------------------------------------------
    if lookback >= 28:
        # Left rim must be in first ~40%, right rim in last ~40% before a short handle.
        left_range=range(2,max(5,int(lookback*0.42)))
        right_start=max(12,int(lookback*0.55)); right_end=max(right_start+1,lookback-4)
        if left_range and right_start < right_end:
            li=max(left_range,key=lambda i:H[i])
            ri=max(range(right_start,right_end),key=lambda i:H[i])
            if ri-li >= 12:
                bottom_i=min(range(li+3,ri-2),key=lambda i:L[i]) if ri-li>6 else None
                if bottom_i is not None:
                    rim=(H[li]+H[ri])/2
                    rim_tol=max(atr*1.0,last_price*0.025)
                    depth=rim-L[bottom_i]
                    handle_slice=list(range(ri+1,lookback))
                    if abs(H[li]-H[ri]) <= rim_tol and depth >= atr*2.1 and handle_slice:
                        handle_i=min(handle_slice,key=lambda i:L[i])
                        pullback=rim-L[handle_i]
                        if atr*0.25 <= pullback <= depth*0.48 and L[handle_i] > L[bottom_i]+depth*0.38:
                            trigger=max(H[li],H[ri])+atr*0.08
                            invalid=L[handle_i]-atr*0.15
                            targets=[trigger+depth*x for x in (0.30,0.50,0.72,1.0)]
                            # Actual-price curve points make the cup visible on-chart.
                            mid1=(li+bottom_i)//2; mid2=(bottom_i+ri)//2
                            curve=[(li,H[li]),(mid1,C[mid1]),(bottom_i,L[bottom_i]),(mid2,C[mid2]),(ri,H[ri]),(handle_i,L[handle_i]),(lookback-1,C[-1])]
                            score=82 - abs(H[li]-H[ri])/rim_tol*8
                            candidates.append({'score':score,'type':'cup_handle','name_ar':'كوب وعروة','direction':'BULLISH',
                                'lookback':lookback,'trigger':trigger,'invalidation':invalid,'targets':targets,
                                'pattern_lines':[[(float(x),float(y)) for x,y in curve]],
                                'label_xy':(bottom_i,L[bottom_i]-atr*0.45),'confidence':int(max(72,min(90,score)))})

    if candidates:
        # Reject stale or already-played-out patterns. A model must still be actionable
        # around the current price, not merely exist somewhere on the left side of the chart.
        actionable = []
        for c in candidates:
            pts = [p for line in (c.get('pattern_lines') or []) for p in line]
            if pts:
                raw_end = max(float(p[0]) for p in pts)
                pattern_end = min(c.get('lookback', n) - 1, int(round(raw_end)))
                bars_since_end = (c.get('lookback', n) - 1) - pattern_end
            else:
                bars_since_end = 999

            # Classic reversal formations must have completed recently. Triangles are
            # allowed to extend into the current bar because their boundaries remain live.
            max_age = 7 if not str(c.get('type', '')).startswith('triangle') else 10
            if bars_since_end > max_age:
                continue

            trigger = c['trigger']
            targets = c['targets']
            direction = c['direction']

            # If price has already travelled through the second target, the setup is historical.
            if len(targets) > 1:
                if direction == 'BEARISH' and last_price <= targets[1] - atr * 0.15:
                    continue
                if direction == 'BULLISH' and last_price >= targets[1] + atr * 0.15:
                    continue

            # A confirmation level many ATRs away is not a useful current setup.
            if abs(last_price - trigger) > atr * 3.0:
                continue

            # Reject geometrically tiny or stretched patterns that look wrong on the chart.
            if pts:
                xs = [float(p[0]) for p in pts]
                ys = [float(p[1]) for p in pts]
                span_bars = max(xs) - min(xs)
                span_price = max(ys) - min(ys)
                if span_bars < 8 or span_price < atr * 1.0:
                    continue
                if span_price > atr * 7.0:
                    continue

            c['bars_since_pattern'] = bars_since_end
            c['score'] += max(0, 10 - bars_since_end) * 1.8
            actionable.append(c)

        if actionable:
            best = max(actionable, key=lambda d: d['score'])
            best['demand_zone'] = demand_zone
            return best

    # -----------------------------------------------------
    # NO PATTERN — structure only, no invented model.
    # -----------------------------------------------------
    recent_high=max(highs[-20:]); recent_low=min(lows[-20:])
    trend_pct=linear_trend_pct(closes,20)
    if trend_pct > 0.003:
        direction='BULLISH'; trigger=recent_high+atr*0.10; invalid=max(recent_low,last_price-atr*2.0)
        targets=[trigger+atr*x for x in (1.0,1.8,2.6,3.4)]
    elif trend_pct < -0.003:
        direction='BEARISH'; trigger=recent_low-atr*0.10; invalid=min(recent_high,last_price+atr*2.0)
        targets=[trigger-atr*x for x in (1.0,1.8,2.6,3.4)]
    else:
        direction='NEUTRAL'; trigger=recent_high; invalid=recent_low
        targets=[trigger+atr*x for x in (1.0,1.8,2.6,3.4)]
    return {'type':'structure','name_ar':'لا يوجد نموذج واضح','direction':direction,
        'trigger':trigger,'invalidation':invalid,'targets':targets,'confidence':55 if direction=='NEUTRAL' else 64,
        'demand_zone':demand_zone,'pattern_lines':[]}

def make_chart(symbol, resolution):
    data = get_chart_data(symbol, resolution)

    opens = data["opens"]
    highs = data["highs"]
    lows = data["lows"]
    closes = data["closes"]

    # Keep enough history to read the structure without making the chart busy.
    visible_candles = {
        "15": 48,
        "60": 46,
        "240": 44,
        "D": 40,
    }.get(resolution, 56)

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

    # Darker analyst-style chart: stronger candles, black structure, restrained gold levels.
    fig, ax = plt.subplots(figsize=(15.2, 8.6))
    fig.patch.set_facecolor("#FFFFFF")
    ax.set_facecolor("#FFFFFF")

    candle_width = 0.74
    bullish_candle = "#2E9B61"
    bearish_candle = "#C94F45"
    wick_color = "#202020"
    gold = "#B8871E"
    gold_soft = "#D7BE80"
    green = "#2F7D4A"
    red = "#A94442"
    charcoal = "#1F1F1F"
    muted = "#8B857B"
    invalidation_color = red

    for i in range(len(closes)):
        open_price = opens[i]
        high_price = highs[i]
        low_price = lows[i]
        close_price = closes[i]
        is_up = close_price >= open_price

        ax.vlines(
            i,
            low_price,
            high_price,
            color="#222222",
            linewidth=1.05,
            zorder=3,
        )

        body_bottom = min(open_price, close_price)
        body_height = abs(close_price - open_price)
        if body_height == 0:
            body_height = max(last_price * 0.00025, 0.01)

        rectangle = Rectangle(
            (i - candle_width / 2, body_bottom),
            candle_width,
            body_height,
            facecolor=bullish_candle if is_up else bearish_candle,
            edgecolor="#202020",
            linewidth=1.05,
            zorder=4,
        )
        ax.add_patch(rectangle)

    future_bars = 12
    x_last = len(closes) - 1
    future_start = x_last + 2.2
    future_end = x_last + future_bars

    # Draw a detected model directly over the real candles.
    pattern_lines = setup.get("pattern_lines") or []
    if pattern_lines:
        pattern_lookback = setup.get("lookback", len(closes))
        x_offset = len(closes) - pattern_lookback
        for line in pattern_lines:
            xs = [x_offset + p[0] for p in line]
            ys = [p[1] for p in line]
            ax.plot(xs, ys, color="#202020", linewidth=2.6, alpha=0.98, zorder=5)
            ax.scatter(xs, ys, s=24, color=gold, edgecolor="#202020", linewidth=0.6, zorder=6)
        label_xy = setup.get("label_xy")
        if label_xy:
            chart_pattern_name = {
                "triangle_sym": "SYMMETRICAL TRIANGLE",
                "triangle_asc": "ASCENDING TRIANGLE",
                "triangle_desc": "DESCENDING TRIANGLE",
                "head_shoulders": "HEAD & SHOULDERS",
                "inverse_head_shoulders": "INVERSE H&S",
                "cup_handle": "CUP & HANDLE",
                "double_top": "DOUBLE TOP",
                "double_bottom": "DOUBLE BOTTOM",
            }.get(setup.get("type"), "PRICE PATTERN")
            ax.text(
                x_offset + label_xy[0], label_xy[1], chart_pattern_name,
                color="#8A6514", fontsize=15.0, fontweight="bold",
                ha="center", va="bottom",
                bbox={"boxstyle":"round,pad=0.28","facecolor":"#FFF9E7","edgecolor":gold,"linewidth":1.1},
                zorder=7,
            )

    direction = setup["direction"]
    if direction == "BULLISH":
        level_color = green
    elif direction == "BEARISH":
        level_color = red
    else:
        level_color = gold

    # Demand zone: draw only when repeated defended lows were detected.
    demand_zone = setup.get("demand_zone")
    if demand_zone:
        demand_lower = demand_zone["lower"]
        demand_upper = demand_zone["upper"]
        demand_start = max(0, x_last - 18)
        # Demand belongs to historical price action only; never extend it into the forecast area.
        ax.fill_between(
            [demand_start, x_last + 0.45],
            [demand_lower, demand_lower],
            [demand_upper, demand_upper],
            color=gold_soft, alpha=0.30, zorder=0,
        )
        zone_mid = (demand_lower + demand_upper) / 2
        ax.text(
            demand_start + 0.55, zone_mid,
            f"DEMAND  {demand_lower:.2f}–{demand_upper:.2f}",
            color="#202020", fontsize=13.5, fontweight="bold", va="center", ha="left", zorder=2,
            bbox={"boxstyle":"round,pad=0.16","facecolor":"#FFF8E8","edgecolor":"none","alpha":0.78},
        )

    # Confirmation level.
    confirm_x0 = future_start if direction == "NEUTRAL" else max(0, x_last - 2)
    ax.hlines(
        setup["trigger"],
        confirm_x0,
        future_end + 0.35,
        color=level_color,
        linewidth=2.8,
        linestyle="--",
        alpha=0.9,
        zorder=2,
    )
    ax.text(
        future_end + 0.55,
        setup["trigger"],
        f"CONFIRM  {setup['trigger']:.2f}",
        color=level_color,
        fontsize=18.5,
        va="center",
        fontweight="bold",
    )

    # Invalidation / risk line.
    ax.hlines(
        setup["invalidation"],
        max(0, x_last - 2),
        future_end + 0.35,
        color=invalidation_color,
        linewidth=2.4,
        linestyle="--",
        alpha=0.82,
        zorder=2,
    )
    ax.text(
        future_end + 0.55,
        setup["invalidation"],
        f"INVALID  {setup['invalidation']:.2f}",
        color=invalidation_color,
        fontsize=17.5,
        va="center",
    )

    # Show the four agreed targets.
    all_targets = setup["targets"][:4]
    chart_targets = all_targets
    for idx, target in enumerate(chart_targets, 1):
        ax.hlines(
            target,
            future_start + 0.8,
            future_end + 0.35,
            color=level_color,
            linewidth=2.2,
            linestyle=":",
            alpha=max(0.42, 0.72 - idx * 0.08),
            zorder=1,
        )
        ax.text(
            future_end + 0.55,
            target,
            f"T{idx}  {target:.2f}",
            color=level_color,
            fontsize=17.0,
            va="center",
        )

    # Expected path: FORECAST AREA ONLY. Never draw any projected segment on historical candles.
    if chart_targets:
        path_color = green if direction == "BULLISH" else red if direction == "BEARISH" else gold
        demand_zone = setup.get("demand_zone")
        trigger = setup["trigger"]
        atr_local = max(calculate_atr(highs, lows, closes, 14), last_price * 0.002)

        # A visual divider makes it impossible to confuse history with the forecast.
        ax.vlines(
            future_start - 0.55, min(lows), max(highs),
            color="#D8D3C8", linewidth=1.0, linestyle=(0, (2, 5)), alpha=0.65, zorder=0,
        )

        confirmed = False
        points_y = []

        if direction == "BULLISH":
            confirmed = last_price >= trigger
            # Path starts in the FUTURE, at the current price, never before the last candle.
            points_y = [last_price]
            if not confirmed:
                # Only a shallow retest is allowed. Do not invent a plunge to an old demand zone.
                retest = last_price - atr_local * 0.45
                if demand_zone:
                    demand_mid = (demand_zone["lower"] + demand_zone["upper"]) / 2
                    if 0 < last_price - demand_mid <= atr_local * 1.6:
                        retest = max(demand_mid, last_price - atr_local * 0.80)
                if retest < last_price - atr_local * 0.12:
                    points_y.append(retest)
                points_y.append(trigger)
            else:
                retest = max(trigger, last_price - atr_local * 0.45)
                if retest < last_price - atr_local * 0.12:
                    points_y.append(retest)
            future_targets = [t for t in chart_targets if t > max(last_price, trigger) + atr_local * 0.05]

        elif direction == "BEARISH":
            confirmed = last_price <= trigger
            points_y = [last_price]
            if not confirmed:
                retest = last_price + atr_local * 0.45
                ceiling = setup["invalidation"] - atr_local * 0.15
                retest = min(retest, ceiling)
                if retest > last_price + atr_local * 0.12:
                    points_y.append(retest)
                points_y.append(trigger)
            else:
                retest = min(trigger, last_price + atr_local * 0.45)
                if retest > last_price + atr_local * 0.12:
                    points_y.append(retest)
            future_targets = [t for t in chart_targets if t < min(last_price, trigger) - atr_local * 0.05]

        else:
            # Neutral means WAIT. Do not connect current price to a distant trigger.
            # The conditional path begins to the right of the last candle and ONLY at confirmation.
            points_y = [trigger]
            future_targets = chart_targets

        future_targets = future_targets[:4]
        if future_targets:
            # Keep the path readable: target 1, one shallow pause, then remaining targets.
            points_y.append(future_targets[0])
            if len(future_targets) > 1:
                anchor = trigger if direction == "NEUTRAL" or not confirmed else last_price
                pause = future_targets[0] + (anchor - future_targets[0]) * 0.22
                points_y.append(pause)
                points_y.extend(future_targets[1:])

        clean_points = []
        for y in points_y:
            if not clean_points or abs(y - clean_points[-1]) > atr_local * 0.10:
                clean_points.append(y)
        points_y = clean_points

        if len(points_y) >= 2:
            # IMPORTANT: every x coordinate is strictly AFTER the final real candle.
            path_x0 = future_start + 0.35
            path_x1 = future_end - 0.75
            step = (path_x1 - path_x0) / max(1, len(points_y) - 1)
            points_x = [path_x0 + i * step for i in range(len(points_y))]

            ax.plot(
                points_x, points_y,
                color=path_color, linewidth=2.7, alpha=0.95,
                linestyle=(0, (5, 4)) if direction == "NEUTRAL" else "-",
                solid_capstyle="round", solid_joinstyle="round", zorder=5,
            )
            ax.scatter(points_x, points_y, s=20, color=path_color, zorder=6)
            ax.annotate(
                "", xy=(points_x[-1], points_y[-1]), xytext=(points_x[-2], points_y[-2]),
                arrowprops={"arrowstyle":"-|>","color":path_color,"lw":2.8,"alpha":0.98}, zorder=6,
            )

            path_label = "EXPECTED PATH" if direction != "NEUTRAL" else "AFTER CONFIRM"
            label_x = points_x[0] + 0.15
            label_y = points_y[0] + atr_local * (0.65 if direction != "BEARISH" else 0.45)
            ax.text(
                label_x, label_y, path_label,
                color="#202020", fontsize=12.8, fontweight="bold", va="bottom", zorder=7,
                bbox={"boxstyle":"round,pad=0.16","facecolor":"#FFFDF8","edgecolor":"#D7BE80","linewidth":0.8,"alpha":0.96},
            )
    # Current price: small clean marker plus a faint guide line.
    ax.hlines(
        last_price,
        max(0, x_last - 8),
        future_end + 0.2,
        color="#8F8A82",
        linewidth=0.65,
        linestyle=(0, (3, 4)),
        alpha=0.65,
        zorder=1,
    )
    ax.text(
        x_last + 0.35,
        last_price,
        f"{last_price:.2f}",
        color="#111111",
        fontsize=13.5,
        va="center",
        bbox={
            "boxstyle": "round,pad=0.22",
            "facecolor": "#FFFFFF",
            "edgecolor": gold_soft,
            "linewidth": 0.8,
        },
        zorder=6,
    )

    title_direction = {
        "BULLISH": "BULLISH",
        "BEARISH": "BEARISH",
        "NEUTRAL": "NEUTRAL",
    }[direction]
    change_sign = "+" if visible_change >= 0 else ""

    ax.set_title(
        f"{symbol}   |   {timeframe}   |   ${last_price:.2f}   |   {title_direction}   |   {change_sign}{visible_change:.1f}%",
        fontsize=17.0,
        fontweight="bold",
        color="#111111",
        pad=13,
        loc="left",
    )

    # Minimal axes like the analyst reference.
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")
    ax.tick_params(axis="y", colors="#2A2A2A", labelsize=12.2, length=0, pad=4)
    ax.tick_params(axis="x", labelbottom=False, length=0)
    ax.grid(False)

    for side in ("top", "left", "bottom"):
        ax.spines[side].set_visible(False)
    ax.spines["right"].set_color("#BDB7AA")
    ax.spines["right"].set_linewidth(0.7)

    all_y = highs + lows + [setup["trigger"], setup["invalidation"]] + chart_targets
    if setup.get("demand_zone"):
        all_y += [setup["demand_zone"]["lower"], setup["demand_zone"]["upper"]]
    y_min = min(all_y)
    y_max = max(all_y)
    padding = max((y_max - y_min) * 0.075, last_price * 0.008)

    ax.set_xlim(-0.8, future_end + 3.2)
    ax.set_ylim(y_min - padding, y_max + padding)

    plt.tight_layout()

    image = io.BytesIO()
    plt.savefig(
        image,
        format="png",
        dpi=230,
        bbox_inches="tight",
        facecolor=fig.get_facecolor(),
    )
    plt.close(fig)
    image.seek(0)

    scenario_text = {
        "BULLISH": "إيجابي",
        "BEARISH": "سلبي",
        "NEUTRAL": "محايد / انتظار تأكيد",
    }[direction]

    targets_text = " → ".join(f"${value:.2f}" for value in all_targets)
    caption_lines = [
        f"📊 {symbol} — {timeframe_ar}",
        f"السعر: ${last_price:.2f}",
        f"النموذج: {setup['name_ar']}",
    ]
    if setup.get("pattern_lines"):
        caption_lines.append(
            f"حداثة النموذج: قبل {setup.get('bars_since_pattern', 0)} شموع"
        )
    caption_lines.extend([
        f"السيناريو: {scenario_text}",
        f"التأكيد: ${setup['trigger']:.2f}",
        f"الإبطال: ${setup['invalidation']:.2f}",
    ])
    if setup.get("demand_zone"):
        caption_lines.append(
            f"منطقة الطلب: ${setup['demand_zone']['lower']:.2f} - ${setup['demand_zone']['upper']:.2f}"
        )
    else:
        caption_lines.append("منطقة الطلب: لا توجد منطقة موثوقة حاليًا")
    caption_lines.extend([
        f"الأهداف: {targets_text}",
        f"القوة الفنية: {setup['confidence']}%",
        "",
        "⚠️ تحليل فني احتمالي، وليس توصية شراء أو بيع.",
    ])
    caption = "\n".join(caption_lines)

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

IMPORTANT_NEWS_RULES = [
    (
        10,
        [
            "bankruptcy", "chapter 11", "fraud", "sec charges", "criminal",
            "acquisition", "acquire", "merger", "buyout", "takeover",
            "fda approval", "fda rejects", "recall", "halted", "trading halt"
        ],
        "حدث جوهري جدًا"
    ),
    (
        9,
        [
            "earnings", "revenue", "guidance", "forecast", "outlook",
            "beats estimates", "misses estimates", "profit warning",
            "investigation", "subpoena", "lawsuit", "settlement",
            "ceo resigns", "ceo steps down"
        ],
        "نتائج أو تطور جوهري"
    ),
    (
        8,
        [
            "contract", "deal", "partnership", "approval", "launch",
            "price target", "downgrade", "upgrade", "layoffs",
            "job cuts", "offering", "share sale", "capital raise",
            "dividend", "buyback", "repurchase", "split"
        ],
        "خبر قد يؤثر على حركة السهم"
    ),
    (
        7,
        [
            "analyst", "target", "orders", "delivery", "deliveries",
            "production", "sales", "tariff", "export", "restriction",
            "regulatory", "antitrust"
        ],
        "تطور مهم يحتاج متابعة"
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
        raise ValueError("استجابة الأخبار غير متوقعة.")

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
    reason = "خبر عام"

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

    if symbol.lower() in combined:
        importance = min(
            10,
            importance + 1
        )

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
        sentiment_ar = "🟢 إيجابي"
    elif negative_hits > positive_hits:
        sentiment = "NEGATIVE"
        sentiment_ar = "🔴 سلبي"
    else:
        sentiment = "NEUTRAL"
        sentiment_ar = "⚪ محايد"

    category_ar = reason

    if "earnings" in combined or "revenue" in combined:
        category_ar = "نتائج أو توقعات مالية"
    elif any(
        x in combined
        for x in [
            "upgrade", "downgrade", "price target", "analyst"
        ]
    ):
        category_ar = "تغيير في رأي أو هدف محلل"
    elif any(
        x in combined
        for x in [
            "contract", "deal", "partnership"
        ]
    ):
        category_ar = "صفقة أو شراكة مهمة"
    elif any(
        x in combined
        for x in [
            "acquisition", "merger", "buyout", "takeover"
        ]
    ):
        category_ar = "استحواذ أو اندماج"
    elif any(
        x in combined
        for x in [
            "investigation", "lawsuit", "subpoena", "fraud"
        ]
    ):
        category_ar = "تطور قانوني أو تنظيمي"
    elif any(
        x in combined
        for x in [
            "launch", "approval", "approved"
        ]
    ):
        category_ar = "منتج أو موافقة جديدة"
    elif any(
        x in combined
        for x in [
            "layoffs", "job cuts"
        ]
    ):
        category_ar = "تغييرات تشغيلية مهمة"

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
        return "غير متوفر"

    try:
        dt = datetime.fromtimestamp(
            timestamp,
            ZoneInfo("Asia/Riyadh")
        )
        return dt.strftime(
            "%d/%m %I:%M %p"
        )
    except Exception:
        return "غير متوفر"


def format_news_item(item):
    headline = item["headline"]

    if len(headline) > 220:
        headline = (
            headline[:217]
            + "..."
        )

    return (
        f"📰 {item['symbol']}\n"
        f"{item['sentiment_ar']} | "
        f"🔥 الأهمية: {item['importance']}/10\n"
        f"📝 الخلاصة: {item['category_ar']}\n"
        f"📌 {headline}\n"
        f"🕐 {news_time_text(item['timestamp'])}\n"
        f"🏷️ المصدر: {item['source'] or 'غير متوفر'}"
    )


def format_news_results(results):
    if not results:
        return (
            "📰 أهم الأخبار\n\n"
            "✅ لا توجد أخبار مهمة جديدة "
            "على أسهمنا خلال الفترة الحالية."
        )

    blocks = [
        format_news_item(item)
        for item in results
    ]

    return (
        "📰 أهم أخبار أسهمنا\n"
        "🔥 يعرض الأخبار المهمة فقط\n"
        "━━━━━━━━━━━━━━\n\n"
        + "\n\n━━━━━━━━━━━━━━\n\n".join(blocks)
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
    # TEST MODE: chart only. No scan/news/earnings/opportunity actions are exposed.
    keyboard = [
        [
            InlineKeyboardButton(
                "🔮 اختبار الشارت",
                callback_data="chart"
            )
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


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
    # TEST MODE: intentionally no automatic monitors, news, earnings,
    # pending-watch checks, or unsolicited Telegram notifications.
    print("CHART TEST MODE — BACKGROUND TASKS DISABLED")


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
