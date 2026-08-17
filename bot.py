import os
import time
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

        continuation_label = (
            "🔥 مستمر بقوة"
        )

    elif continuation_score >= 2:

        continuation_label = (
            "🟢 مستمر"
        )

    elif continuation_score >= 0:

        continuation_label = (
            "🟡 متماسك"
        )

    else:

        continuation_label = (
            "⚠️ بدأ يضعف"
        )

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
            last_price
            - closes[-4]
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

def get_chart_data(
    symbol,
    resolution
):

    url = (
        f"https://api.marketdata.app/"
        f"v1/stocks/candles/{resolution}/{symbol}/"
    )

    if resolution == "15":
        countback = 60

    elif resolution == "60":
        countback = 60

    else:
        countback = 70

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

    if (
        len(opens) < 20
        or len(highs) < 20
        or len(lows) < 20
        or len(closes) < 20
    ):

        raise ValueError(
            "بيانات الشارت غير كافية."
        )

    opens = [
        float(x)
        for x in opens
    ]

    highs = [
        float(x)
        for x in highs
    ]

    lows = [
        float(x)
        for x in lows
    ]

    closes = [
        float(x)
        for x in closes
    ]

    return {
        "opens": opens,
        "highs": highs,
        "lows": lows,
        "closes": closes,
        "timestamps": timestamps,
    }


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


# =========================================================
# CANDLESTICK CHART
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

    sma10 = moving_average(
        closes,
        10
    )

    sma20 = moving_average(
        closes,
        20
    )

    last_price = closes[-1]

    recent_highs = highs[-20:]
    recent_lows = lows[-20:]

    resistance = max(
        recent_highs
    )

    support = min(
        recent_lows
    )

    if resolution == "15":

        timeframe = "15 MIN"

    elif resolution == "60":

        timeframe = "1 HOUR"

    else:

        timeframe = "DAILY"

    fig, ax = plt.subplots(
        figsize=(11, 7)
    )

    candle_width = 0.6

    for i in range(
        len(closes)
    ):

        open_price = opens[i]
        high_price = highs[i]
        low_price = lows[i]
        close_price = closes[i]

        if close_price >= open_price:

            candle_color = "#16a34a"

        else:

            candle_color = "#dc2626"

        ax.vlines(
            i,
            low_price,
            high_price,
            color=candle_color,
            linewidth=1
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

            body_height = 0.01

        rectangle = Rectangle(
            (
                i - candle_width / 2,
                body_bottom
            ),
            candle_width,
            body_height,
            facecolor=candle_color,
            edgecolor=candle_color,
            linewidth=1
        )

        ax.add_patch(
            rectangle
        )

    x = list(
        range(
            len(closes)
        )
    )

    ax.plot(
        x,
        sma10,
        linewidth=1.3,
        label="SMA 10"
    )

    ax.plot(
        x,
        sma20,
        linewidth=1.3,
        label="SMA 20"
    )

    ax.axhline(
        resistance,
        linestyle="--",
        linewidth=1.2,
        label=(
            f"Resistance "
            f"{resistance:.2f}"
        )
    )

    ax.axhline(
        support,
        linestyle="--",
        linewidth=1.2,
        label=(
            f"Support "
            f"{support:.2f}"
        )
    )

    ax.axhline(
        last_price,
        linestyle=":",
        linewidth=1,
        label=(
            f"Last "
            f"{last_price:.2f}"
        )
    )

    ax.set_title(
        (
            f"{symbol} | "
            f"{timeframe} | "
            f"${last_price:.2f}"
        ),
        fontsize=16,
        fontweight="bold"
    )

    ax.set_ylabel(
        "Price"
    )

    ax.set_xlim(
        -1,
        len(closes)
    )

    ax.grid(
        True,
        alpha=0.15
    )

    ax.legend(
        loc="best",
        fontsize=9
    )

    ax.tick_params(
        axis="x",
        labelbottom=False
    )

    plt.tight_layout()

    image = io.BytesIO()

    plt.savefig(
        image,
        format="png",
        dpi=160,
        bbox_inches="tight"
    )

    plt.close(
        fig
    )

    image.seek(0)

    if (
        last_price > sma10[-1]
        and sma10[-1] > sma20[-1]
    ):

        direction = (
            "🟢 صاعد"
        )

    elif (
        last_price < sma10[-1]
        and sma10[-1] < sma20[-1]
    ):

        direction = (
            "🔴 هابط"
        )

    else:

        direction = (
            "🟡 محايد"
        )

    distance_to_resistance = (
        (
            resistance
            - last_price
        )
        / last_price
        * 100
        if last_price > 0
        else 0
    )

    distance_to_support = (
        (
            last_price
            - support
        )
        / last_price
        * 100
        if last_price > 0
        else 0
    )

    caption = (
        f"📈 {symbol} — {timeframe}\n\n"

        f"💵 السعر: "
        f"${last_price:.2f}\n"

        f"📊 الاتجاه: "
        f"{direction}\n\n"

        f"🟢 الدعم: "
        f"${support:.2f} "
        f"({distance_to_support:.1f}%)\n"

        f"🔴 المقاومة: "
        f"${resistance:.2f} "
        f"({distance_to_resistance:.1f}%)\n\n"

        f"〰️ SMA10: "
        f"${sma10[-1]:.2f}\n"

        f"〰️ SMA20: "
        f"${sma20[-1]:.2f}"
    )

    return (
        image,
        caption
    )


def chart_timeframe_menu():

    keyboard = [
        [
            InlineKeyboardButton(
                "15 دقيقة",
                callback_data="chart_15"
            ),

            InlineKeyboardButton(
                "ساعة",
                callback_data="chart_60"
            ),

            InlineKeyboardButton(
                "يومي",
                callback_data="chart_D"
            ),
        ]
    ]

    return InlineKeyboardMarkup(
        keyboard
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

    return min(
        score,
        90
    )


def normalize_contract_score(
    raw_score
):

    return round(
        (raw_score / 90)
        * 100
    )


# =========================================================
# UNUSUAL ACTIVITY
# =========================================================

def unusual_activity_score(
    volume,
    oi
):

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

    return (
        score,
        ratio,
        label
    )


# =========================================================
# MARKET SCORE
# =========================================================

def apply_market_score(
    side,
    trend
):

    bias = trend["bias"]

    momentum = (
        trend["momentum_score"]
    )

    continuation = (
        trend["continuation_score"]
    )

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

def decision_status(
    contract,
    trend
):

    bias = trend["bias"]

    momentum = (
        trend["momentum_score"]
    )

    continuation = (
        trend["continuation_score"]
    )

    if bias == "NEUTRAL":

        return {
            "label":
                "🟡 انتظار تأكيد",

            "reason":
                "الاتجاه غير محسوم",

            "rank":
                1,
        }

    if contract["side"] != bias:

        return {
            "label":
                "🔴 استبعاد",

            "reason":
                "العقد عكس اتجاه السهم",

            "rank":
                0,
        }

    if continuation < 0:

        return {
            "label":
                "🔴 استبعاد",

            "reason":
                "استمرار الحركة بدأ يضعف",

            "rank":
                0,
        }

    if (
        contract["score"]
        < MIN_TOP_SCORE
    ):

        return {
            "label":
                "🟡 غير مؤهل للمراقبة",

            "reason":
                (
                    f"التقييم "
                    f"{contract['score']}/100 "
                    f"أقل من "
                    f"{MIN_TOP_SCORE}/100"
                ),

            "rank":
                1,
        }

    if (
        contract["uoa_score"]
        < MIN_TOP_UOA
    ):

        return {
            "label":
                "🟡 غير مؤهل للمراقبة",

            "reason":
                (
                    f"النشاط "
                    f"{contract['uoa_score']}/10 "
                    f"أقل من "
                    f"{MIN_TOP_UOA}/10"
                ),

            "rank":
                1,
        }

    if (
        momentum >= 6
        and continuation >= 2
        and contract["base_score"] >= 78
    ):

        return {
            "label":
                "🟢 تأكيد دخول",

            "reason":
                "الاتجاه والزخم والاستمرار متوافقون",

            "rank":
                2,
        }

    if momentum < 6:

        reason = (
            f"الزخم "
            f"{momentum}/10 "
            f"ويحتاج تأكيد حركة قصيرة"
        )

    elif continuation < 2:

        reason = (
            "ننتظر تأكيد استمرار الحركة"
        )

    elif contract["base_score"] < 78:

        reason = (
            f"جودة العقد "
            f"{contract['base_score']}/100 "
            f"وتحتاج تحسنًا"
        )

    else:

        reason = (
            "الفرصة جيدة لكنها تحتاج تأكيدًا إضافيًا"
        )

    return {
        "label":
            "🟡 انتظار تأكيد",

        "reason":
            reason,

        "rank":
            1,
    }


def effective_decision(
    contract,
    trend
):

    decision = decision_status(
        contract,
        trend
    )

    if (
        decision["rank"] == 2
        and not is_us_market_open()
    ):

        return {
            "label":
                "🟢 مرشح قوي — انتظار افتتاح السوق",

            "reason":
                "الشروط قوية لكن السوق الأمريكي مغلق",

            "rank":
                1,
        }

    return decision


# =========================================================
# TOP CONTRACTS
# =========================================================

def get_top_contracts(
    data,
    trend
):

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

            option_symbol = (
                data["optionSymbol"][i]
            )

            expiration = (
                data["expiration"][i]
            )

            expiry_date = (
                format_expiry_timestamp(
                    expiration
                )
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

            volume = (
                data["volume"][i]
            )

            oi = (
                data["openInterest"][i]
            )

            delta = (
                data["delta"][i]
            )

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

            if (
                ask <= 0
                or mid <= 0
                or bid < 0
            ):
                continue

            spread_pct = (
                (
                    ask - bid
                )
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

            base_score = (
                normalize_contract_score(
                    raw_score
                )
            )

            (
                uoa_score,
                volume_oi_ratio,
                uoa_label
            ) = unusual_activity_score(
                volume,
                oi
            )

            market_adjustment = (
                apply_market_score(
                    side,
                    trend
                )
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
                    round(
                        internal_score
                    ),
                    98
                )
            )

            contract = {
                "option_symbol":
                    option_symbol,

                "expiration":
                    expiration,

                "expiry_date":
                    expiry_date,

                "side":
                    side,

                "strike":
                    strike,

                "dte":
                    dte,

                "bid":
                    bid,

                "ask":
                    ask,

                "mid":
                    mid,

                "volume":
                    volume,

                "oi":
                    oi,

                "delta":
                    delta,

                "spread_pct":
                    spread_pct,

                "volume_oi_ratio":
                    volume_oi_ratio,

                "base_score":
                    base_score,

                "uoa_score":
                    uoa_score,

                "uoa_label":
                    uoa_label,

                "uoa_adjustment":
                    uoa_adjustment,

                "market_adjustment":
                    market_adjustment,

                "internal_score":
                    internal_score,

                "score":
                    display_score,
            }

            contract["decision"] = (
                decision_status(
                    contract,
                    trend
                )
            )

            contracts.append(
                contract
            )

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

    return contracts[
        :TOP_N_RESULTS
    ]


def analyze_symbol(symbol):

    trend = get_stock_trend(
        symbol
    )

    data = get_option_chain(
        symbol
    )

    contracts = get_top_contracts(
        data,
        trend
    )

    if not contracts:
        return None

    best = contracts[0]

    return {
        "symbol":
            symbol,

        "trend":
            trend,

        "contract":
            best,

        "contracts":
            contracts,

        "internal_score":
            best["internal_score"],
    }


# =========================================================
# WATCH
# =========================================================

def watch_key(
    chat_id,
    symbol
):

    return (
        f"{chat_id}:{symbol}"
    )


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
        "chat_id":
            chat_id,

        "symbol":
            symbol,

        "side":
            contract["side"],

        "strike":
            contract["strike"],

        "dte":
            contract["dte"],

        "expiration":
            contract["expiration"],

        "expiry_date":
            contract["expiry_date"],

        "option_symbol":
            contract["option_symbol"],

        "original_ask":
            contract["ask"],

        "created_at":
            now,

        "last_checked_at":
            now,
    }

    return True


def get_matching_contract(
    symbol,
    option_symbol
):

    data = get_option_chain(
        symbol
    )

    symbols = data.get(
        "optionSymbol",
        []
    )

    for i, item in enumerate(
        symbols
    ):

        if item != option_symbol:
            continue

        try:

            return {
                "ask":
                    float(
                        data["ask"][i]
                    ),

                "bid":
                    float(
                        data["bid"][i]
                    ),

                "mid":
                    float(
                        data["mid"][i]
                    ),

                "volume":
                    int(
                        data["volume"][i]
                    ),

                "oi":
                    int(
                        data["openInterest"][i]
                    ),
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

    last_price = (
        intraday["last_price"]
    )

    sma5 = (
        intraday["sma5"]
    )

    move = (
        intraday["change_3bars"]
    )

    up_bars = (
        intraday["up_bars"]
    )

    down_bars = (
        intraday["down_bars"]
    )

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


# =========================================================
# MONITOR
# =========================================================

async def monitor_pending(
    application
):

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
                    - watch[
                        "last_checked_at"
                    ]
                )

                if (
                    elapsed
                    < WATCH_INTERVAL_SECONDS
                ):

                    continue

                watch[
                    "last_checked_at"
                ] = now

                symbol = (
                    watch["symbol"]
                )

                side = (
                    watch["side"]
                )

                strike = (
                    watch["strike"]
                )

                expiry_date = (
                    watch["expiry_date"]
                )

                chat_id = (
                    watch["chat_id"]
                )

                trend = (
                    await asyncio.to_thread(
                        get_stock_trend,
                        symbol
                    )
                )

                if (
                    trend["bias"] != side
                    or
                    trend[
                        "continuation_score"
                    ] < 0
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

                (
                    status,
                    reason
                ) = (
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
                        watch[
                            "option_symbol"
                        ]
                    )
                )

                if not option_now:
                    continue

                ask_now = (
                    option_now["ask"]
                )

                if (
                    ask_now
                    > MAX_OPTION_ASK
                ):

                    await application.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "⚠️ تحقق التأكيد الفني "
                            "لكن السعر تجاوز $5\n\n"
                            f"{symbol} "
                            f"{side} "
                            f"{strike:g} | "
                            f"{expiry_date}\n"
                            f"💵 Ask "
                            f"${ask_now:.2f}"
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
                        f"💵 Ask "
                        f"${ask_now:.2f}\n"
                        f"📊 Volume "
                        f"{option_now['volume']:,}\n"
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

    cached = (
        TOP10_CACHE["results"]
    )

    if (
        cached is not None
        and (
            now
            - TOP10_CACHE["time"]
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

                    and
                    contract["score"]
                    >= MIN_TOP_SCORE

                    and
                    contract["uoa_score"]
                    >= MIN_TOP_UOA

                    and
                    contract["volume"]
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

            decision = (
                effective_decision(
                    contract,
                    trend
                )
            )

            if decision["rank"] == 0:
                continue

            result[
                "contract"
            ] = contract

            result[
                "internal_score"
            ] = (
                contract[
                    "internal_score"
                ]
            )

            result[
                "decision"
            ] = decision

            results.append(
                result
            )

        except Exception as e:

            print(
                f"SCAN ERROR "
                f"{symbol}: {e}"
            )

    results.sort(
        key=lambda x: (
            -x["decision"]["rank"],
            -x["internal_score"],
            -x["trend"][
                "momentum_score"
            ],
            -x["contract"][
                "uoa_score"
            ],
            x["contract"][
                "spread_pct"
            ]
        )
    )

    top10 = results[
        :TOP_N_RESULTS
    ]

    TOP10_CACHE["time"] = now
    TOP10_CACHE["results"] = top10

    return top10


# =========================================================
# MENU
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
                "📈 شارت السهم",
                callback_data="chart"
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
# FORMAT TOP 10
# =========================================================

def format_top10(results):

    if not results:

        return (
            "🏆 أفضل فرص اليوم\n\n"
            "❌ لا توجد فرص تحقق "
            "الشروط حاليًا."
        )

    message = (
        f"🏆 أفضل "
        f"{len(results)} فرص لليوم\n"
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

        contract = (
            item["contract"]
        )

        decision = (
            item["decision"]
        )

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


# =========================================================
# FORMAT CONTRACTS
# =========================================================

def format_top_contracts(
    symbol,
    contracts,
    trend
):

    qualified = [
        contract

        for contract
        in contracts

        if (
            contract["side"]
            == trend["bias"]

            and
            contract["score"]
            >= MIN_TOP_SCORE

            and
            contract["uoa_score"]
            >= MIN_TOP_UOA

            and
            contract["volume"]
            >= MIN_VOLUME

            and
            contract["decision"]["rank"]
            > 0
        )
    ]

    qualified = qualified[
        :TOP_N_RESULTS
    ]

    if not qualified:

        return (
            f"🔎 أفضل العقود لـ "
            f"{symbol}\n"

            f"📊 {trend['label']} | "
            f"زخم "
            f"{trend['momentum_score']}/10 | "
            f"{trend['continuation_label']}\n"

            f"💵 "
            f"${trend['last_close']:.2f}\n"

            f"━━━━━━━━━━━━━━\n\n"

            "❌ لا توجد عقود مؤهلة "
            "لشروطنا حاليًا."
        )

    message = (
        f"🔎 أفضل العقود لـ "
        f"{symbol}\n"

        f"📊 {trend['label']} | "
        f"زخم "
        f"{trend['momentum_score']}/10 | "
        f"{trend['continuation_label']}\n"

        f"💵 "
        f"${trend['last_close']:.2f}\n"

        f"━━━━━━━━━━━━━━\n\n"
    )

    for index, contract in enumerate(
        qualified,
        start=1
    ):

        decision = (
            effective_decision(
                contract,
                trend
            )
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

    decision = (
        effective_decision(
            contract,
            trend
        )
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

        "⏱️ التحقق كل 5 دقائق "
        "أثناء السوق"
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
        "🤖 بوت تحليل واختيار "
        "عقود الأوبشن\n\n"
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

    chat_id = (
        query.message.chat_id
    )

    if query.data == "top10":

        await query.message.reply_text(
            "🏆 جاري فحص 30 رمزًا مختارًا...\n"
            "⏳ تقدر تستخدم "
            "البوت أثناء الفحص."
        )

        try:

            results = (
                await asyncio.to_thread(
                    scan_top10
                )
            )

            await query.message.reply_text(
                format_top10(
                    results
                ),
                reply_markup=main_menu()
            )

            watches_added = []

            for item in results:

                contract = (
                    item["contract"]
                )

                trend = (
                    item["trend"]
                )

                added = (
                    add_pending_watch(
                        chat_id,
                        item["symbol"],
                        contract,
                        trend
                    )
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

                    + "\n\n".join(
                        lines
                    )

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
                "⚠️ تعذر إكمال "
                "الفحص حاليًا.",
                reply_markup=main_menu()
            )

    elif query.data == "scan_options":

        context.user_data[
            "mode"
        ] = "scan"

        await query.message.reply_text(
            "🔎 اكتب رمز الشركة:\n\n"
            "مثال:\nNVDA"
        )

    elif query.data == "contract":

        context.user_data[
            "mode"
        ] = "contract"

        await query.message.reply_text(
            "📊 أرسل بيانات العقد:\n\n"
            "SYMBOL CALL/PUT STRIKE "
            "DTE DELTA VOLUME OI SPREAD"
        )

    elif query.data == "opportunity":

        context.user_data[
            "mode"
        ] = "opportunity"

        await query.message.reply_text(
            "🎯 أرسل بيانات الفرصة:\n\n"
            "SYMBOL DIRECTION "
            "MOMENTUM VOLUME TREND"
        )

    elif query.data == "chart":

        context.user_data[
            "mode"
        ] = "chart"

        await query.message.reply_text(
            "📈 اكتب رمز السهم:\n\n"
            "مثال:\nTSLA"
        )

    elif query.data.startswith(
        "chart_"
    ):

        symbol = (
            context.user_data.get(
                "chart_symbol"
            )
        )

        if not symbol:

            await query.message.reply_text(
                "⚠️ اختر شارت السهم "
                "واكتب الرمز أولًا.",
                reply_markup=main_menu()
            )

            return

        resolution = (
            query.data.split(
                "_",
                1
            )[1]
        )

        await query.message.reply_text(
            f"📈 جاري تجهيز شارت "
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
                "⚠️ تعذر إنشاء الشارت حاليًا.",
                reply_markup=main_menu()
            )

    elif query.data == "help":

        await query.message.reply_text(
            "ℹ️ طريقة الاستخدام\n\n"

            "🏆 أفضل فرص اليوم: "
            "يفحص 30 رمزًا مختارًا.\n\n"

            "🔎 البحث اليدوي: "
            "اكتب رمز أي سهم.\n\n"

            "📈 شارت السهم: "
            "شموع 15 دقيقة أو ساعة أو يومي.\n\n"

            "🟢 تأكيد الدخول "
            "يظهر أثناء السوق فقط.\n\n"

            "🟢 مرشح قوي: "
            "انتظار افتتاح السوق.\n\n"

            "🟡 انتظار تأكيد: "
            "يدخل المراقبة تلقائيًا.\n\n"

            "⏱️ التحقق كل 5 دقائق "
            "أثناء السوق.\n\n"

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

    text = (
        update.message.text
        .strip()
    )

    chat_id = (
        update.effective_chat.id
    )

    mode = (
        context.user_data.get(
            "mode"
        )
    )

    try:

        if mode == "scan":

            symbol = (
                text.upper()
                .strip()
            )

            if (
                not symbol.isalpha()
                or len(symbol) > 6
            ):

                await update.message.reply_text(
                    "⚠️ اكتب رمز "
                    "سهم صحيح.\n\n"
                    "مثال: NVDA"
                )

                return

            await update.message.reply_text(
                f"🔎 جاري تحليل "
                f"{symbol}...\n"
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

            contracts = (
                get_top_contracts(
                    data,
                    trend
                )
            )

            qualified = [
                contract

                for contract
                in contracts

                if (
                    contract["side"]
                    == trend["bias"]

                    and
                    contract["score"]
                    >= MIN_TOP_SCORE

                    and
                    contract["uoa_score"]
                    >= MIN_TOP_UOA

                    and
                    contract["volume"]
                    >= MIN_VOLUME

                    and
                    contract["decision"]["rank"]
                    > 0
                )
            ]

            watched_contract = None

            for contract in qualified:

                decision = (
                    effective_decision(
                        contract,
                        trend
                    )
                )

                if (
                    decision["label"]
                    in [
                        "🟡 انتظار تأكيد",
                        "🟢 مرشح قوي — انتظار افتتاح السوق",
                    ]
                ):

                    watched_contract = (
                        contract
                    )

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

                added = (
                    add_pending_watch(
                        chat_id,
                        symbol,
                        watched_contract,
                        trend
                    )
                )

                if added:

                    await update.message.reply_text(
                        format_watch_added(
                            symbol,
                            watched_contract,
                            trend
                        )
                    )

            context.user_data[
                "mode"
            ] = None

        elif mode == "contract":

            parts = text.split()

            if len(parts) != 8:
                raise ValueError

            symbol = (
                parts[0].upper()
            )

            direction = (
                parts[1].upper()
            )

            strike = float(
                parts[2]
            )

            dte = int(
                parts[3]
            )

            delta = float(
                parts[4]
            )

            volume = int(
                parts[5]
            )

            oi = int(
                parts[6]
            )

            spread = float(
                parts[7]
            )

            raw_score = (
                contract_score(
                    delta,
                    volume,
                    oi,
                    spread,
                    dte
                )
            )

            score = (
                normalize_contract_score(
                    raw_score
                )
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

                f"⭐ الجودة: "
                f"{score}/100",

                reply_markup=main_menu()
            )

            context.user_data[
                "mode"
            ] = None

        elif mode == "opportunity":

            parts = text.split()

            if len(parts) != 5:
                raise ValueError

            symbol = (
                parts[0].upper()
            )

            direction = (
                parts[1].upper()
            )

            momentum = float(
                parts[2]
            )

            volume = float(
                parts[3]
            )

            trend_score = float(
                parts[4]
            )

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

                f"السهم: "
                f"{symbol}\n"

                f"الاتجاه: "
                f"{direction}\n"

                f"⭐ النتيجة: "
                f"{score}/100",

                reply_markup=main_menu()
            )

            context.user_data[
                "mode"
            ] = None

        elif mode == "chart":

            symbol = (
                text.upper()
                .strip()
            )

            if (
                not symbol.isalpha()
                or len(symbol) > 6
            ):

                await update.message.reply_text(
                    "⚠️ اكتب رمز سهم صحيح.\n\n"
                    "مثال: TSLA"
                )

                return

            context.user_data[
                "chart_symbol"
            ] = symbol

            context.user_data[
                "mode"
            ] = None

            await update.message.reply_text(
                f"📈 {symbol}\n\n"
                "اختر إطار الشارت:",
                reply_markup=chart_timeframe_menu()
            )

        else:

            await update.message.reply_text(
                "اضغط /start "
                "واختر الخدمة.",
                reply_markup=main_menu()
            )

    except (
        requests.exceptions
        .RequestException
    ) as e:

        print(
            "MARKETDATA ERROR:",
            e
        )

        await update.message.reply_text(
            "⚠️ تعذر الاتصال "
            "ببيانات السوق حاليًا.",
            reply_markup=main_menu()
        )

    except Exception as e:

        print(
            "ERROR:",
            e
        )

        await update.message.reply_text(
            "⚠️ حصل خطأ أثناء "
            "تحليل البيانات.",
            reply_markup=main_menu()
        )


# =========================================================
# START WATCHER
# =========================================================

async def post_init(
    application
):

    asyncio.create_task(
        monitor_pending(
            application
        )
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
