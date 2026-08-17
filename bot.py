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


SCAN_SYMBOLS = [
    "SPY",
    "QQQ",
    "AMD",
    "TSLA",
    "NVDA",
    "MRVL",
    "ARM",
    "AVGO",
    "MU",
    "GS",
    "META",
    "IWM",
    "AAPL",
    "GOOGL",
    "MSFT",
    "AMZN",
    "SMCI",
    "SNOW",
    "SHOP",
    "BA",
    "CRM",
    "CAT",
    "PLTR",
    "ORCL",
    "OPEN",
    "IBIT",
    "MSTR",
    "COIN",
    "SPCX",
    "SKHY",
]


TOP10_CACHE = {
    "time": 0,
    "results": None,
}

PENDING_WATCHES = {}


# =========================================================
# ACCESS
# =========================================================

def _allowed(update):

    user = update.effective_user

    return (
        user is not None
        and user.id in ALLOWED_USERS
    )


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
# REAL EXPIRATION DATE
# =========================================================

def format_expiry_timestamp(expiration):

    expiry = datetime.fromtimestamp(
        int(expiration),
        ZoneInfo("America/New_York")
    )

    return (
        f"{expiry.day} "
        f"{expiry.strftime('%b %Y')}"
    )


# =========================================================
# MARKET HOURS
# =========================================================

def is_us_market_open():

    ny_now = datetime.now(
        ZoneInfo("America/New_York")
    )

    if ny_now.weekday() >= 5:
        return False

    current_time = ny_now.time()

    market_open = dt_time(9, 30)
    market_close = dt_time(16, 0)

    return (
        market_open
        <= current_time
        < market_close
    )


# =========================================================
# MARKET DATA
# =========================================================

def get_headers():

    return {
        "Authorization":
            f"Bearer {MARKETDATA_TOKEN}"
    }


def get_option_chain(symbol):

    url = (
        f"https://api.marketdata.app/"
        f"v1/options/chain/{symbol}/"
    )

    today = datetime.now(
        ZoneInfo("America/New_York")
    ).date()

    from_date = (
        today + timedelta(days=5)
    ).isoformat()

    to_date = (
        today + timedelta(days=30)
    ).isoformat()

    params = {
        "from": from_date,
        "to": to_date,
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
            data.get(
                "errmsg",
                "لم يتم الحصول على بيانات الخيارات."
            )
        )

    return data


# =========================================================
# STOCK TREND
# =========================================================

def get_stock_trend(symbol):

    url = (
        f"https://api.marketdata.app/"
        f"v1/stocks/candles/D/{symbol}/"
    )

    response = requests.get(
        url,
        headers=get_headers(),
        params={"countback": 30},
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    if data.get("s") != "ok":

        raise ValueError(
            "تعذر الحصول على حركة السهم."
        )

    closes = data.get("c", [])
    volumes = data.get("v", [])

    if len(closes) < 21:

        raise ValueError(
            "لا توجد شموع كافية لتحليل الاتجاه."
        )

    closes = [
        float(x)
        for x in closes
    ]

    last_close = closes[-1]

    sma5 = sum(closes[-5:]) / 5
    sma10 = sum(closes[-10:]) / 10
    sma20 = sum(closes[-20:]) / 20

    change_3 = (
        (
            last_close - closes[-4]
        )
        / closes[-4]
        * 100
        if closes[-4] != 0
        else 0
    )

    change_5 = (
        (
            last_close - closes[-6]
        )
        / closes[-6]
        * 100
        if closes[-6] != 0
        else 0
    )

    change_10 = (
        (
            last_close - closes[-11]
        )
        / closes[-11]
        * 100
        if closes[-11] != 0
        else 0
    )

    if volumes and len(volumes) >= 20:

        volume_values = [
            float(x)
            for x in volumes
        ]

        recent_volume = volume_values[-1]

        avg_volume20 = (
            sum(volume_values[-20:])
            / 20
        )

        volume_ratio = (
            recent_volume / avg_volume20
            if avg_volume20 > 0
            else 1
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

    if (
        bullish_points >= 3
        and bullish_points > bearish_points
    ):

        bias = "CALL"
        label = "🟢 صاعد"
        strength = bullish_points

    elif (
        bearish_points >= 3
        and bearish_points > bullish_points
    ):

        bias = "PUT"
        label = "🔴 هابط"
        strength = bearish_points

    else:

        bias = "NEUTRAL"
        label = "🟡 محايد"

        strength = max(
            bullish_points,
            bearish_points
        )

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

    momentum_score = min(
        momentum_score,
        10
    )

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

    url = (
        f"https://api.marketdata.app/"
        f"v1/stocks/candles/15/{symbol}/"
    )

    response = requests.get(
        url,
        headers=get_headers(),
        params={"countback": 30},
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    if data.get("s") != "ok":

        raise ValueError(
            "تعذر الحصول على شموع 15 دقيقة."
        )

    closes = data.get("c", [])

    if len(closes) < 6:

        raise ValueError(
            "بيانات 15 دقيقة غير كافية."
        )

    closes = [
        float(x)
        for x in closes
    ]

    last_price = closes[-1]

    sma5 = (
        sum(closes[-5:])
        / 5
    )

    change_3bars = (
        (
            last_price - closes[-4]
        )
        / closes[-4]
        * 100
        if closes[-4] != 0
        else 0
    )

    up_bars = 0
    down_bars = 0

    recent = closes[-5:]

    for i in range(
        1,
        len(recent)
    ):

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

def get_raw_chart_data(
    symbol,
    resolution,
    countback
):

    url = (
        f"https://api.marketdata.app/"
        f"v1/stocks/candles/{resolution}/{symbol}/"
    )

    response = requests.get(
        url,
        headers=get_headers(),
        params={
            "countback": countback
        },
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    if data.get("s") != "ok":

        raise ValueError(
            data.get(
                "errmsg",
                "تعذر الحصول على بيانات الشارت."
            )
        )

    opens = data.get("o", [])
    highs = data.get("h", [])
    lows = data.get("l", [])
    closes = data.get("c", [])
    timestamps = data.get("t", [])

    count = min(
        len(opens),
        len(highs),
        len(lows),
        len(closes),
        len(timestamps)
    )

    if count < 20:

        raise ValueError(
            "بيانات الشارت غير كافية."
        )

    return {
        "opens": [
            float(x)
            for x in opens[:count]
        ],

        "highs": [
            float(x)
            for x in highs[:count]
        ],

        "lows": [
            float(x)
            for x in lows[:count]
        ],

        "closes": [
            float(x)
            for x in closes[:count]
        ],

        "timestamps": [
            int(x)
            for x in timestamps[:count]
        ],
    }


# =========================================================
# 4 HOUR AGGREGATION
# =========================================================

def aggregate_4hour_data(
    hourly_data
):

    opens = hourly_data["opens"]
    highs = hourly_data["highs"]
    lows = hourly_data["lows"]
    closes = hourly_data["closes"]
    timestamps = hourly_data["timestamps"]

    grouped = {}

    ny_tz = ZoneInfo(
        "America/New_York"
    )

    for i in range(
        len(closes)
    ):

        dt = datetime.fromtimestamp(
            timestamps[i],
            ny_tz
        )

        market_start_minutes = (
            9 * 60 + 30
        )

        current_minutes = (
            dt.hour * 60
            + dt.minute
        )

        minutes_from_open = (
            current_minutes
            - market_start_minutes
        )

        if minutes_from_open < 0:
            continue

        bucket = (
            minutes_from_open
            // 240
        )

        key = (
            dt.date(),
            bucket
        )

        if key not in grouped:

            grouped[key] = {
                "open":
                    opens[i],

                "high":
                    highs[i],

                "low":
                    lows[i],

                "close":
                    closes[i],

                "timestamp":
                    timestamps[i],
            }

        else:

            grouped[key]["high"] = max(
                grouped[key]["high"],
                highs[i]
            )

            grouped[key]["low"] = min(
                grouped[key]["low"],
                lows[i]
            )

            grouped[key]["close"] = (
                closes[i]
            )

    values = list(
        grouped.values()
    )

    values.sort(
        key=lambda x:
            x["timestamp"]
    )

    if len(values) < 20:

        raise ValueError(
            "بيانات 4 ساعات غير كافية."
        )

    return {
        "opens": [
            item["open"]
            for item in values
        ],

        "highs": [
            item["high"]
            for item in values
        ],

        "lows": [
            item["low"]
            for item in values
        ],

        "closes": [
            item["close"]
            for item in values
        ],

        "timestamps": [
            item["timestamp"]
            for item in values
        ],
    }


def get_chart_data(
    symbol,
    resolution
):

    if resolution == "15":

        return get_raw_chart_data(
            symbol,
            "15",
            90
        )

    if resolution == "60":

        return get_raw_chart_data(
            symbol,
            "60",
            90
        )

    if resolution == "240":

        hourly_data = get_raw_chart_data(
            symbol,
            "60",
            260
        )

        return aggregate_4hour_data(
            hourly_data
        )

    return get_raw_chart_data(
        symbol,
        "D",
        100
    )


# =========================================================
# INDICATORS
# =========================================================

def moving_average(
    values,
    period
):

    result = []

    for i in range(
        len(values)
    ):

        start = max(
            0,
            i - period + 1
        )

        window = values[
            start:i + 1
        ]

        result.append(
            sum(window)
            / len(window)
        )

    return result


def calculate_recent_volatility(
    closes
):

    returns = []

    recent = closes[-21:]

    for i in range(
        1,
        len(recent)
    ):

        previous = recent[
            i - 1
        ]

        current = recent[
            i
        ]

        if previous <= 0:
            continue

        returns.append(
            math.log(
                current / previous
            )
        )

    if len(returns) < 2:
        return 0.01

    mean_return = (
        sum(returns)
        / len(returns)
    )

    variance = (
        sum(
            (
                r
                - mean_return
            ) ** 2
            for r in returns
        )
        / (
            len(returns)
            - 1
        )
    )

    return max(
        math.sqrt(
            variance
        ),
        0.001
    )


def linear_trend_pct(
    closes,
    lookback=20
):

    values = closes[
        -lookback:
    ]

    n = len(
        values
    )

    if n < 5:
        return 0

    x_mean = (
        n - 1
    ) / 2

    y_values = [
        math.log(
            max(
                value,
                0.0001
            )
        )
        for value in values
    ]

    y_mean = (
        sum(y_values)
        / n
    )

    numerator = 0
    denominator = 0

    for i, y in enumerate(
        y_values
    ):

        numerator += (
            (i - x_mean)
            * (y - y_mean)
        )

        denominator += (
            i - x_mean
        ) ** 2

    if denominator == 0:
        return 0

    return (
        numerator
        / denominator
    )


def calculate_atr(
    highs,
    lows,
    closes,
    period=14
):

    true_ranges = []

    start = max(
        1,
        len(closes) - period
    )

    for i in range(
        start,
        len(closes)
    ):

        tr = max(
            highs[i] - lows[i],
            abs(
                highs[i]
                - closes[i - 1]
            ),
            abs(
                lows[i]
                - closes[i - 1]
            )
        )

        true_ranges.append(
            tr
        )

    if not true_ranges:

        return max(
            closes[-1] * 0.01,
            0.01
        )

    return (
        sum(true_ranges)
        / len(true_ranges)
    )


# =========================================================
# FORECAST ENGINE
# =========================================================

def interpolate_path(
    pivot_points,
    future_bars
):

    path = [
        None
    ] * (
        future_bars + 1
    )

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

        distance = (
            end_index
            - start_index
        )

        if distance <= 0:
            continue

        for step in range(
            distance + 1
        ):

            ratio = (
                step / distance
            )

            value = (
                start_price
                + (
                    end_price
                    - start_price
                )
                * ratio
            )

            path[
                start_index + step
            ] = value

    previous = (
        pivot_points[0][1]
    )

    for i in range(
        len(path)
    ):

        if path[i] is None:

            path[i] = previous

        previous = path[i]

    return path


def build_forecast(
    closes,
    highs,
    lows,
    resolution
):

    last_price = closes[-1]

    sma10 = moving_average(
        closes,
        10
    )

    sma20 = moving_average(
        closes,
        20
    )

    volatility = (
        calculate_recent_volatility(
            closes
        )
    )

    atr = calculate_atr(
        highs,
        lows,
        closes,
        14
    )

    trend_slope = (
        linear_trend_pct(
            closes,
            20
        )
    )

    recent_change_5 = (
        (
            closes[-1]
            - closes[-6]
        )
        / closes[-6]
        if (
            len(closes) >= 6
            and closes[-6] != 0
        )
        else 0
    )

    ma_bias = (
        (
            sma10[-1]
            - sma20[-1]
        )
        / last_price
        if last_price > 0
        else 0
    )

    momentum_component = (
        recent_change_5
        / 5
    )

    drift = (
        trend_slope * 0.50
        + momentum_component * 0.25
        + ma_bias * 0.25
    )

    if resolution == "D":

        future_bars = 10

    else:

        future_bars = 12

    recent_highs = highs[
        -20:
    ]

    recent_lows = lows[
        -20:
    ]

    resistance = max(
        recent_highs
    )

    support = min(
        recent_lows
    )

    zone_width = max(
        atr * 0.30,
        last_price * 0.0025
    )

    resistance_low = (
        resistance
        - zone_width
    )

    resistance_high = (
        resistance
        + zone_width
    )

    support_low = (
        support
        - zone_width
    )

    support_high = (
        support
        + zone_width
    )

    direction_score = 0

    if last_price > sma10[-1]:
        direction_score += 1
    else:
        direction_score -= 1

    if sma10[-1] > sma20[-1]:
        direction_score += 1
    else:
        direction_score -= 1

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

    price_range = max(
        resistance
        - support,
        atr * 3
    )

    if direction_score >= 2:

        scenario = "BULLISH"
        scenario_ar = "🟢 صاعد"

        pullback = max(
            sma10[-1],
            last_price
            - atr * 0.70
        )

        first_push = max(
            resistance,
            last_price
            + atr * 1.10
        )

        retest = max(
            last_price,
            first_push
            - atr * 0.65
        )

        target2 = max(
            first_push
            + atr * 1.25,
            resistance
            + atr
        )

        pivot_points = [
            (
                0,
                last_price
            ),
            (
                max(
                    1,
                    round(
                        future_bars * 0.22
                    )
                ),
                pullback
            ),
            (
                max(
                    2,
                    round(
                        future_bars * 0.48
                    )
                ),
                first_push
            ),
            (
                max(
                    3,
                    round(
                        future_bars * 0.68
                    )
                ),
                retest
            ),
            (
                future_bars,
                target2
            ),
        ]

        target1 = first_push

        invalidation = (
            support_low
        )

    elif direction_score <= -2:

        scenario = "BEARISH"
        scenario_ar = "🔴 هابط"

        bounce = min(
            sma10[-1],
            last_price
            + atr * 0.70
        )

        first_drop = min(
            support,
            last_price
            - atr * 1.10
        )

        retest = min(
            last_price,
            first_drop
            + atr * 0.65
        )

        target2 = min(
            first_drop
            - atr * 1.25,
            support
            - atr
        )

        pivot_points = [
            (
                0,
                last_price
            ),
            (
                max(
                    1,
                    round(
                        future_bars * 0.22
                    )
                ),
                bounce
            ),
            (
                max(
                    2,
                    round(
                        future_bars * 0.48
                    )
                ),
                first_drop
            ),
            (
                max(
                    3,
                    round(
                        future_bars * 0.68
                    )
                ),
                retest
            ),
            (
                future_bars,
                target2
            ),
        ]

        target1 = first_drop

        invalidation = (
            resistance_high
        )

    else:

        scenario = "SIDEWAYS"
        scenario_ar = "🟡 عرضي"

        upper_test = min(
            resistance,
            last_price
            + price_range * 0.30
        )

        lower_test = max(
            support,
            last_price
            - price_range * 0.30
        )

        final_price = (
            (
                upper_test
                + lower_test
            )
            / 2
        )

        pivot_points = [
            (
                0,
                last_price
            ),
            (
                max(
                    1,
                    round(
                        future_bars * 0.30
                    )
                ),
                upper_test
            ),
            (
                max(
                    2,
                    round(
                        future_bars * 0.60
                    )
                ),
                lower_test
            ),
            (
                future_bars,
                final_price
            ),
        ]

        target1 = (
            upper_test
        )

        target2 = (
            lower_test
        )

        invalidation = (
            support_low
        )

    expected = interpolate_path(
        pivot_points,
        future_bars
    )

    upper = []
    lower = []

    for step, projected in enumerate(
        expected
    ):

        if step == 0:

            uncertainty = 0

        else:

            uncertainty = (
                volatility
                * math.sqrt(
                    step
                )
                * 0.55
            )

        upper.append(
            projected
            * math.exp(
                uncertainty
            )
        )

        lower.append(
            projected
            * math.exp(
                -uncertainty
            )
        )

    expected_end = (
        expected[-1]
    )

    change_pct = (
        (
            expected_end
            - last_price
        )
        / last_price
        * 100
        if last_price > 0
        else 0
    )

    confidence_raw = (
        abs(direction_score)
        + (
            abs(drift)
            / max(
                volatility,
                0.001
            )
        )
    )

    confidence = min(
        88,
        max(
            48,
            round(
                50
                + confidence_raw * 5
            )
        )
    )

    return {
        "expected":
            expected,

        "upper":
            upper,

        "lower":
            lower,

        "future_bars":
            future_bars,

        "pivot_points":
            pivot_points,

        "scenario":
            scenario,

        "scenario_ar":
            scenario_ar,

        "target1":
            target1,

        "target2":
            target2,

        "invalidation":
            invalidation,

        "support":
            support,

        "resistance":
            resistance,

        "support_low":
            support_low,

        "support_high":
            support_high,

        "resistance_low":
            resistance_low,

        "resistance_high":
            resistance_high,

        "change_pct":
            change_pct,

        "confidence":
            confidence,

        "volatility":
            volatility,

        "atr":
            atr,
    }


# =========================================================
# FORECAST CHART
# =========================================================

def make_chart(
    symbol,
    resolution
):

    data = get_chart_data(
        symbol,
        resolution
    )

    opens = data["opens"]
    highs = data["highs"]
    lows = data["lows"]
    closes = data["closes"]

    candle_count = min(
        55,
        len(closes)
    )

    opens = opens[
        -candle_count:
    ]

    highs = highs[
        -candle_count:
    ]

    lows = lows[
        -candle_count:
    ]

    closes = closes[
        -candle_count:
    ]

    sma10 = moving_average(
        closes,
        10
    )

    sma20 = moving_average(
        closes,
        20
    )

    forecast = build_forecast(
        closes,
        highs,
        lows,
        resolution
    )

    last_price = closes[-1]

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

    fig, ax = plt.subplots(
        figsize=(12, 7)
    )

    fig.patch.set_facecolor(
        "#0B0F14"
    )

    ax.set_facecolor(
        "#0B0F14"
    )

    candle_width = 0.62

    for i in range(
        len(closes)
    ):

        open_price = opens[i]
        high_price = highs[i]
        low_price = lows[i]
        close_price = closes[i]

        if (
            close_price
            >= open_price
        ):

            candle_color = (
                "#22C55E"
            )

        else:

            candle_color = (
                "#EF4444"
            )

        ax.vlines(
            i,
            low_price,
            high_price,
            color=candle_color,
            linewidth=1.0,
            zorder=3
        )

        body_bottom = min(
            open_price,
            close_price
        )

        body_height = abs(
            close_price
            - open_price
        )

        if body_height == 0:

            body_height = max(
                last_price
                * 0.0003,
                0.01
            )

        rectangle = Rectangle(
            (
                i
                - candle_width / 2,
                body_bottom
            ),
            candle_width,
            body_height,
            facecolor=candle_color,
            edgecolor=candle_color,
            linewidth=1,
            zorder=3
        )

        ax.add_patch(
            rectangle
        )

    historical_x = list(
        range(
            len(closes)
        )
    )

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

    future_start = (
        len(closes)
        - 1
    )

    future_end = (
        future_start
        + forecast[
            "future_bars"
        ]
    )

    zone_start = max(
        0,
        len(closes)
        - 18
    )

    ax.fill_between(
        [
            zone_start,
            future_end + 1
        ],
        [
            forecast[
                "resistance_low"
            ],
            forecast[
                "resistance_low"
            ]
        ],
        [
            forecast[
                "resistance_high"
            ],
            forecast[
                "resistance_high"
            ]
        ],
        color="#EF4444",
        alpha=0.16,
        zorder=1
    )

    ax.fill_between(
        [
            zone_start,
            future_end + 1
        ],
        [
            forecast[
                "support_low"
            ],
            forecast[
                "support_low"
            ]
        ],
        [
            forecast[
                "support_high"
            ],
            forecast[
                "support_high"
            ]
        ],
        color="#22C55E",
        alpha=0.16,
        zorder=1
    )

    ax.text(
        future_end + 0.4,
        forecast[
            "resistance"
        ],
        "RESISTANCE",
        color="#F87171",
        fontsize=9,
        va="center"
    )

    ax.text(
        future_end + 0.4,
        forecast[
            "support"
