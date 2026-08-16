import os
import threading
import requests
from datetime import date, timedelta
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ.get("BOT_TOKEN")
MARKETDATA_TOKEN = os.environ.get("MARKETDATA_TOKEN")

web = Flask(__name__)


@web.route("/")
def home():
    return "Option Vision Bot is running", 200


def run_web():
    port = int(os.environ.get("PORT", 10000))
    web.run(host="0.0.0.0", port=port)


def get_headers():
    return {
        "Authorization": f"Bearer {MARKETDATA_TOKEN}"
    }


def get_option_chain(symbol):
    url = f"https://api.marketdata.app/v1/options/chain/{symbol}/"

    today = date.today()
    from_date = (today + timedelta(days=5)).isoformat()
    to_date = (today + timedelta(days=30)).isoformat()

    params = {
        "from": from_date,
        "to": to_date
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


def get_stock_trend(symbol):
    url = f"https://api.marketdata.app/v1/stocks/candles/D/{symbol}/"

    params = {
        "countback": 30
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
            data.get("errmsg", "تعذر الحصول على حركة السهم.")
        )

    closes = data.get("c", [])
    volumes = data.get("v", [])

    if len(closes) < 20:
        raise ValueError("لا توجد شموع كافية لتحليل الاتجاه.")

    closes = [float(x) for x in closes]

    last_close = closes[-1]

    sma5 = sum(closes[-5:]) / 5
    sma10 = sum(closes[-10:]) / 10
    sma20 = sum(closes[-20:]) / 20

    change_5 = (
        (last_close - closes[-6]) / closes[-6] * 100
        if closes[-6] != 0
        else 0
    )

    if volumes and len(volumes) >= 20:
        recent_volume = float(volumes[-1])
        avg_volume20 = sum(
            float(x) for x in volumes[-20:]
        ) / 20

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

    return {
        "bias": bias,
        "label": label,
        "strength": strength,
        "last_close": last_close,
        "sma5": sma5,
        "sma10": sma10,
        "sma20": sma20,
        "change_5": change_5,
        "volume_ratio": volume_ratio,
    }


def main_menu():
    keyboard = [
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
                "ℹ️ طريقة الاستخدام",
                callback_data="help"
            )
        ],
    ]

    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 بوت تحليل واختيار عقود الأوبشن\n\n"
        "اختر الخدمة المطلوبة:",
        reply_markup=main_menu(),
    )


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "scan_options":
        context.user_data["mode"] = "scan"

        await query.message.reply_text(
            "🔎 اكتب رمز الشركة:\n\n"
            "مثال:\n"
            "TSLA"
        )

    elif query.data == "contract":
        context.user_data["mode"] = "contract"

        await query.message.reply_text(
            "📊 أرسل بيانات العقد بهذا الترتيب:\n\n"
            "SYMBOL CALL/PUT STRIKE DTE DELTA VOLUME OI SPREAD\n\n"
            "مثال:\n"
            "TSLA CALL 350 7 0.45 2500 8000 4"
        )

    elif query.data == "opportunity":
        context.user_data["mode"] = "opportunity"

        await query.message.reply_text(
            "🎯 أرسل بيانات الفرصة بهذا الشكل:\n\n"
            "SYMBOL DIRECTION MOMENTUM VOLUME TREND\n\n"
            "مثال:\n"
            "TSLA CALL 8 9 8"
        )

    elif query.data == "help":
        await query.message.reply_text(
            "ℹ️ البوت يبحث عن أفضل عقود من 5 إلى 30 يوم، "
            "ويقيم السيولة والـ Delta والـ Spread وVolume/OI، "
            "ثم يضيف اتجاه السهم إلى التقييم.",
            reply_markup=main_menu(),
        )


def contract_score(delta, volume, oi, spread_pct, dte):
    score = 0
    delta_abs = abs(delta)

    if 0.42 <= delta_abs <= 0.58:
        score += 25
    elif 0.35 <= delta_abs <= 0.65:
        score += 20
    elif 0.30 <= delta_abs <= 0.70:
        score += 12
    else:
        score += 5

    if volume >= 10000:
        score += 20
    elif volume >= 5000:
        score += 18
    elif volume >= 2000:
        score += 15
    elif volume >= 1000:
        score += 12
    elif volume >= 300:
        score += 7
    else:
        score += 3

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
    else:
        score += 2

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

    if 7 <= dte <= 14:
        score += 10
    elif 15 <= dte <= 21:
        score += 8
    elif 5 <= dte <= 6:
        score += 7
    elif 22 <= dte <= 30:
        score += 6

    if oi > 0:
        volume_oi_ratio = volume / oi

        if volume_oi_ratio >= 2:
            score += 10
        elif volume_oi_ratio >= 1:
            score += 8
        elif volume_oi_ratio >= 0.5:
            score += 5
        elif volume_oi_ratio >= 0.2:
            score += 3
        else:
            score += 1

    return min(score, 100)


def apply_trend_score(base_score, side, trend):
    bias = trend["bias"]

    if bias == "NEUTRAL":
        adjustment = 0

    elif side == bias:
        if trend["strength"] >= 4:
            adjustment = 5
        else:
            adjustment = 3

    else:
        if trend["strength"] >= 4:
            adjustment = -12
        else:
            adjustment = -7

    final_score = base_score + adjustment

    return max(0, min(final_score, 100)), adjustment


def rating(score):
    if score >= 92:
        return "🔥 استثنائي"
    elif score >= 85:
        return "🟢 ممتاز جدًا"
    elif score >= 75:
        return "🟢 قوي"
    elif score >= 65:
        return "🟡 جيد"
    elif score >= 55:
        return "🟠 متوسط"
    else:
        return "🔴 ضعيف"


def get_top_contracts(data, trend):
    contracts = []

    fields = [
        "optionSymbol",
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
                f"بيانات {field} غير موجودة في الاستجابة."
            )

    count = len(data["optionSymbol"])

    for i in range(count):
        try:
            option_symbol = data["optionSymbol"][i]
            side = str(data["side"][i]).upper()

            strike = float(data["strike"][i])
            dte = int(data["dte"][i])

            bid = data["bid"][i]
            ask = data["ask"][i]
            mid = data["mid"][i]
            volume = data["volume"][i]
            oi = data["openInterest"][i]
            delta = data["delta"][i]

            if (
                bid is None
                or ask is None
                or mid is None
                or volume is None
                or oi is None
                or delta is None
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

            if mid <= 0 or ask <= 0 or bid < 0:
                continue

            spread_pct = ((ask - bid) / mid) * 100

            if volume < 100:
                continue

            if oi < 200:
                continue

            if abs(delta) < 0.25 or abs(delta) > 0.75:
                continue

            if spread_pct > 12:
                continue

            volume_oi_ratio = volume / oi if oi > 0 else 0

            base_score = contract_score(
                delta,
                volume,
                oi,
                spread_pct,
                dte
            )

            final_score, trend_adjustment = apply_trend_score(
                base_score,
                side,
                trend
            )

            contracts.append(
                {
                    "option_symbol": option_symbol,
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
                    "trend_adjustment": trend_adjustment,
                    "score": final_score,
                }
            )

        except (TypeError, ValueError, IndexError):
            continue

    contracts.sort(
        key=lambda x: (
            -x["score"],
            x["spread_pct"],
            -x["volume"],
            -x["oi"]
        )
    )

    return contracts[:5]


def format_top_contracts(symbol, contracts, trend):
    if not contracts:
        return (
            f"🔎 نتائج البحث عن {symbol}\n\n"
            "❌ لم أجد عقودًا مناسبة حسب الفلاتر الحالية."
        )

    message = (
        f"🔎 أفضل 5 عقود لـ {symbol}\n"
        f"📅 نطاق الانتهاء: 5 - 30 يوم\n"
        f"📊 اتجاه السهم: {trend['label']}\n"
        f"💵 آخر إغلاق: ${trend['last_close']:.2f}\n"
        f"📈 حركة 5 جلسات: {trend['change_5']:+.2f}%\n"
        f"━━━━━━━━━━━━━━\n\n"
    )

    for index, contract in enumerate(contracts, start=1):
        if contract["trend_adjustment"] > 0:
            trend_text = (
                f"+{contract['trend_adjustment']} ✅ مع الاتجاه"
            )
        elif contract["trend_adjustment"] < 0:
            trend_text = (
                f"{contract['trend_adjustment']} ⚠️ عكس الاتجاه"
            )
        else:
            trend_text = "0 ➖ اتجاه محايد"

        message += (
            f"{index}️⃣ {contract['side']} "
            f"{contract['strike']:g}\n"
            f"⭐ التقييم النهائي: {contract['score']}/100 "
            f"{rating(contract['score'])}\n"
            f"🧮 تقييم العقد: {contract['base_score']}/100\n"
            f"🧭 تأثير الاتجاه: {trend_text}\n"
            f"📅 DTE: {contract['dte']}\n"
            f"📈 Delta: {contract['delta']:.2f}\n"
            f"💰 Mid: ${contract['mid']:.2f}\n"
            f"📊 Volume: {contract['volume']:,}\n"
            f"📚 OI: {contract['oi']:,}\n"
            f"⚡ Volume/OI: "
            f"{contract['volume_oi_ratio']:.2f}x\n"
            f"↔️ Spread: {contract['spread_pct']:.1f}%\n"
            f"━━━━━━━━━━━━━━\n"
        )

    return message


async def analyze_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    text = update.message.text.strip()
    mode = context.user_data.get("mode")

    try:
        if mode == "scan":
            symbol = text.upper().strip()

            if not symbol.isalpha() or len(symbol) > 6:
                await update.message.reply_text(
                    "⚠️ اكتبي رمز سهم صحيح.\n\n"
                    "مثال: TSLA"
                )
                return

            await update.message.reply_text(
                f"🔎 جاري تحليل {symbol}...\n\n"
                "📊 تحليل الاتجاه\n"
                "📅 فحص العقود 5 - 30 يوم\n"
                "⏳ لحظة..."
            )

            trend = get_stock_trend(symbol)
            data = get_option_chain(symbol)

            contracts = get_top_contracts(
                data,
                trend
            )

            result = format_top_contracts(
                symbol,
                contracts,
                trend
            )

            await update.message.reply_text(
                result,
                reply_markup=main_menu(),
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

            score = contract_score(
                delta,
                volume,
                oi,
                spread,
                dte
            )

            await update.message.reply_text(
                f"📊 تحليل العقد\n\n"
                f"السهم: {symbol}\n"
                f"الاتجاه: {direction}\n"
                f"Strike: {strike}\n"
                f"DTE: {dte} يوم\n"
                f"Delta: {delta}\n"
                f"Volume: {volume:,}\n"
                f"Open Interest: {oi:,}\n"
                f"Spread: {spread}%\n\n"
                f"⭐ النتيجة: {score}/100\n"
                f"{rating(score)}",
                reply_markup=main_menu(),
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

            score = round(
                (
                    momentum * 0.4
                    + volume * 0.3
                    + trend_score * 0.3
                ) * 10
            )

            score = min(score, 100)

            await update.message.reply_text(
                f"🎯 تقييم الفرصة\n\n"
                f"السهم: {symbol}\n"
                f"الاتجاه: {direction}\n\n"
                f"الزخم: {momentum}/10\n"
                f"قوة التداول: {volume}/10\n"
                f"الاتجاه الفني: {trend_score}/10\n\n"
                f"⭐ النتيجة: {score}/100\n"
                f"{rating(score)}",
                reply_markup=main_menu(),
            )

            context.user_data["mode"] = None

        else:
            await update.message.reply_text(
                "اضغطي /start أولًا واختاري نوع التحليل.",
                reply_markup=main_menu(),
            )

    except requests.exceptions.RequestException as e:
        print("MARKETDATA ERROR:", e)

        await update.message.reply_text(
            "⚠️ تعذر الاتصال ببيانات السوق حاليًا.",
            reply_markup=main_menu(),
        )

    except Exception as e:
        print("ERROR:", e)

        await update.message.reply_text(
            "⚠️ حصل خطأ أثناء تحليل البيانات.\n"
            "جربي مرة ثانية.",
            reply_markup=main_menu(),
        )


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")

    if not MARKETDATA_TOKEN:
        raise RuntimeError("MARKETDATA_TOKEN is missing")

    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CallbackQueryHandler(buttons)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            analyze_message
        )
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
