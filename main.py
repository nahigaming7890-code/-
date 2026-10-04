import os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from gtts import gTTS

# === এখানে তোমার টোকেন বসাও ===
TOKEN = "8685009261:AAELJIny99OtAinnm9e564MFH50fa1lCpQk"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "হাই! আমি Text-to-Speech বট। 🎙️\n\n"
        "যেকোনো লেখা পাঠাও, আমি সাথে সাথে ভয়েস বানিয়ে দেবো।\n\n"
        "✅ বাংলা সাপোর্ট করে\n"
        "✅ ইংরেজি সাপোর্ট করে\n\n"
        "যেমন লিখো: আমি বাংলাদেশকে ভালোবাসি"
    )

async def tts_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not text:
        return

    if len(text) > 600:
        await update.message.reply_text("❌ লেখাটা অনেক বড়! ৬০০ অক্ষরের মধ্যে পাঠাও ভাই।")
        return

    # টাইপিং দেখাবে
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="record_voice")

    # ভাষা অটো ডিটেক্ট
    is_bangla = any('\u0980' <= c <= '\u09FF' for c in text)
    lang = 'bn' if is_bangla else 'en'

    try:
        filename = f"tts_{update.effective_user.id}.mp3"
        tts = gTTS(text=text, lang=lang, slow=False)
        tts.save(filename)

        await update.message.reply_voice(
            voice=open(filename, 'rb'),
            caption=f"🗣️ {text[:150]}"
        )
        os.remove(filename)

    except Exception as e:
        print(f"Error: {e}")
        await update.message.reply_text("ভয়েস বানাতে সমস্যা হচ্ছে, আবার চেষ্টা করো।")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"⚠️ Exception: {context.error}")


if __name__ == "__main__":
   def main():
    if not TOKEN or TOKEN == "PUT_YOUR_NEW_BOT_TOKEN_HERE":
        print(
            "\n❌ BOT TOKEN পাওয়া যায়নি!\n"
            "BOT_TOKEN environment variable সেট করো "
            "অথবা TOKEN-এ নতুন token বসাও.\n")
    else:
        print("✅ Bot চলছে... টেলিগ্রামে গিয়ে মেসেজ দাও।")
        app = ApplicationBuilder().token(TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tts_handler))
        app.add_error_handler(error_handler)

        # ✅ Conflict fix:
        # - drop_pending_updates=True : পুরনো pending update ফেলে দেবে
        # - allowed_updates          : শুধু দরকারি update নেবে
        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )
