import os
import threading
import requests
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


def get_option_chain(symbol):
    url = f"https://api.marketdata.app/v1/options/chain/{symbol}/"

    headers = {
        "Authorization": f"Bearer {MARKETDATA_TOKEN}"
    }

    params = {
        "from": "5",
        "to": "30"
    }

    response = requests.get(
        url,
        headers=headers,
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
            "ℹ️ البوت يقدر على:\n\n"
            "🔎 البحث عن أفضل العقود\n"
            "📊 تقييم عقد أوبشن\n"
            "🎯 تقييم فرصة\n\n"
            "البحث يعطيك أفضل 5 عقود حسب معايير السيولة "
            "والـ Delta والـ DTE والـ Spread."
            ,
            reply_markup=main_menu(),
        )


def contract_score(delta, volume, oi, spread_pct, dte):
    score = 0

    delta_abs = abs(delta)

    if 0.35 <= delta_abs <= 0.65:
        score += 25
    elif 0.25 <= delta_abs <= 0.75:
        score += 15
    else:
        score += 5

    if volume >= 1000:
        score += 20
    elif volume >= 300:
        score += 12
    else:
        score += 5

    if oi >= 2000:
        score += 20
    elif oi >= 500:
        score += 12
    else:
        score += 5

    if spread_pct <= 5:
        score += 20
    elif spread_pct <= 10:
        score += 10
    else:
        score += 2

    if 5 <= dte <= 30:
        score += 15
    elif 2 <= dte <= 45:
        score += 8
    else:
        score += 3

    return min(score, 100)


def rating(score):
    if score >= 85:
        return "🔥 ممتاز جدًا"
    elif score >= 70:
        return "🟢 قوي"
    elif score >= 55:
        return "🟡 متوسط"
    else:
        return "🔴 ضعيف"


def get_top_contracts(data):
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
            raise ValueError(f"بيانات {field} غير موجودة في الاستجابة.")

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

            if mid <= 0 or ask <= 0:
                continue

            spread_pct = ((ask - bid) / mid) * 100

            if volume < 100:
                continue

            if oi < 200:
                continue

            if abs(delta) < 0.20 or abs(delta) > 0.80:
                continue

            if spread_pct > 15:
                continue

            score = contract_score(
                delta,
                volume,
                oi,
                spread_pct,
                dte
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
                    "score": score,
                }
            )

        except (TypeError, ValueError, IndexError):
            continue

    contracts.sort(
        key=lambda x: (
            x["score"],
            x["volume"],
            x["oi"]
        ),
        reverse=True
    )

    return contracts[:5]


def format_top_contracts(symbol, contracts):
    if not contracts:
        return (
            f"🔎 نتائج البحث عن {symbol}\n\n"
            "❌ لم أجد عقودًا مناسبة حسب الفلاتر الحالية.\n\n"
            "جرّبي سهمًا آخر أو نخفف الفلاتر."
        )

    message = (
        f"🔎 أفضل 5 عقود لـ {symbol}\n"
        f"━━━━━━━━━━━━━━\n\n"
    )

    for index, contract in enumerate(contracts, start=1):

        message += (
            f"{index}️⃣ {contract['side']} "
            f"{contract['strike']:g}\n"
            f"⭐ التقييم: {contract['score']}/100 "
            f"{rating(contract['score'])}\n"
            f"📅 DTE: {contract['dte']}\n"
            f"📈 Delta: {contract['delta']:.2f}\n"
            f"💰 Mid: ${contract['mid']:.2f}\n"
            f"📊 Volume: {contract['volume']:,}\n"
            f"📚 OI: {contract['oi']:,}\n"
            f"↔️ Spread: {contract['spread_pct']:.1f}%\n"
            f"━━━━━━━━━━━━━━\n"
        )

    return message


async def analyze_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                f"🔎 جاري البحث عن أفضل عقود {symbol}...\n\n"
                "⏳ لحظة..."
            )

            data = get_option_chain(symbol)

            contracts = get_top_contracts(data)

            result = format_top_contracts(
                symbol,
                contracts
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
            trend = float(parts[4])

            score = round(
                (
                    momentum * 0.4
                    + volume * 0.3
                    + trend * 0.3
                ) * 10
            )

            score = min(score, 100)

            await update.message.reply_text(
                f"🎯 تقييم الفرصة\n\n"
                f"السهم: {symbol}\n"
                f"الاتجاه: {direction}\n\n"
                f"الزخم: {momentum}/10\n"
                f"قوة التداول: {volume}/10\n"
                f"الاتجاه الفني: {trend}/10\n\n"
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

    except requests.exceptions.RequestException:
        await update.message.reply_text(
            "⚠️ تعذر الاتصال ببيانات السوق حاليًا.\n\n"
            "تأكدي من MARKETDATA_TOKEN ثم جربي مرة ثانية.",
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
