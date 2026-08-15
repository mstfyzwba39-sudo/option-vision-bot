import os
import threading
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from deep_translator import GoogleTranslator

TOKEN = os.environ.get("BOT_TOKEN")

web = Flask(__name__)

@web.route("/")
def home():
    return "Bot is running", 200

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web.run(host="0.0.0.0", port=port)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 أهلاً بك في بوت تحليل عقود الأوبشن\n\n"
        "أرسل النص أو الخبر بالعربي وسأترجمه لك إلى الإنجليزية 🇺🇸"
    )

async def translate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text

    try:
        translated = GoogleTranslator(
            source="auto",
            target="en"
        ).translate(text)

        await update.message.reply_text(
            f"🇬🇧 الترجمة:\n{translated}"
        )
    except Exception:
        await update.message.reply_text("حدث خطأ أثناء الترجمة، حاول مرة أخرى.")

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")

    threading.Thread(target=run_web, daemon=True).start()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, translate_message)
    )

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
