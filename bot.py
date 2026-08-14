import os
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters
from deep_translator import GoogleTranslator

TOKEN = os.environ.get("BOT_TOKEN")

translator = GoogleTranslator(source="ar", target="en")


async def translate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    if not text:
        return

    try:
        translated = translator.translate(text)

        await update.message.reply_text(
            f"🇬🇧 الترجمة:\n{translated}"
        )

    except Exception:
        await update.message.reply_text(
            "تعذر ترجمة الرسالة حاليًا، حاولي مرة أخرى."
        )


def main():
    if not TOKEN:
        raise ValueError("BOT_TOKEN is not set")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            translate_message
        )
    )

    print("Option Vision Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
