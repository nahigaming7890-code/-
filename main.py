import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from gtts import gTTS
import speech_recognition as sr
from pydub import AudioSegment

# === এখানে তোমার টোকেন বসাও ===
TOKEN = "8685009261:AAFwwhu1oXz1yEzEyT7rtpxiuSEKzo6Pq1o"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "হাই! আমি Text-to-Speech বট। 🎙️\n\n"
        "যেকোনো লেখা পাঠাও, আমি সাথে সাথে ভয়েস বানিয়ে দেবো।\n\n"
        "✅ বাংলা সাপোর্ট করে\n"
        "✅ ইংরেজি সাপোর্ট করে\n"
        "✅ ভিডিও পাঠালে টেক্সট বের করে দেবো\n\n"
        "যেমন লিখো: আমি বাংলাদেশকে ভালোবাসি"
    )

async def tts_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if not text:
        return

    if len(text) > 600:
        await update.message.reply_text("❌ লেখাটা অনেক বড়! ৬০০ অক্ষরের মধ্যে পাঠাও ভাই।")
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

        # === নতুন: ডাউনলোড বাটন সহ ভয়েস পাঠানো ===
        keyboard = [[InlineKeyboardButton("⬇️ ডাউনলোড (.mp3)", callback_data=f"dl|{filename}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_voice(
            voice=open(filename, 'rb'),
            caption=f"🗣️ {text[:150]}",
            reply_markup=reply_markup
        )
        # ফাইল ডিলিট করবো না, কারণ ডাউনলোড বাটনে দরকার
        # os.remove(filename)

    except Exception as e:
        print(f"Error: {e}")
        await update.message.reply_text("ভয়েস বানাতে সমস্যা হচ্ছে, আবার চেষ্টা করো।")


# === নতুন: ডাউনলোড বাটন হ্যান্ডলার ===
async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("dl|"):
        filename = data.split("|", 1)[1]
        if os.path.exists(filename):
            await query.message.reply_document(
                document=open(filename, 'rb'),
                filename=os.path.basename(filename),
                caption="⬇️ আপনার ভয়েস ফাইল (.mp3)"
            )
            # পাঠানোর পর ফাইল ডিলিট
            try:
                os.remove(filename)
            except:
                pass
        else:
            await query.message.reply_text("❌ ফাইলটা আর নেই, আবার টেক্সট পাঠাও।")


# === নতুন: ভিডিও থেকে টেক্সট বের করার হ্যান্ডলার ===
async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    await update.message.reply_text("⏳ ভিডিও প্রসেস হচ্ছে... একটু অপেক্ষা করো।")

    video = update.message.video or update.message.video_note or update.message.document
    if not video:
        await update.message.reply_text("❌ কোনো ভিডিও পাইনি।")
        return

    video_file = await video.get_file()
    video_path = f"video_{update.effective_user.id}.mp4"
    audio_path = f"audio_{update.effective_user.id}.wav"

    try:
        await video_file.download_to_drive(video_path)

        # ভিডিও → অডিও
        audio = AudioSegment.from_file(video_path)
        audio.export(audio_path, format="wav")

        # অডিও → টেক্সট
        recognizer = sr.Recognizer()
        with sr.AudioFile(audio_path) as source:
            audio_data = recognizer.record(source)

        # প্রথমে বাংলা, না হলে ইংরেজি
        try:
            text = recognizer.recognize_google(audio_data, language="bn-BD")
            lang_used = "বাংলা"
        except sr.UnknownValueError:
            try:
                text = recognizer.recognize_google(audio_data, language="en-US")
                lang_used = "English"
            except sr.UnknownValueError:
                text = None
                lang_used = None

        if text:
            await update.message.reply_text(f"📝 ভিডিও থেকে টেক্সট ({lang_used}):\n\n{text}")
        else:
            await update.message.reply_text("❌ ভিডিওতে স্পষ্ট কথা পাওয়া যায়নি।")

    except Exception as e:
        print(f"Video Error: {e}")
        await update.message.reply_text("❌ ভিডিও প্রসেস করতে সমস্যা হয়েছে।")
    finally:
        for p in (video_path, audio_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except:
                pass


if __name__ == "__main__":
    if TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ আগে TOKEN বসাও! BotFather থেকে টোকেন নিয়ে YOUR_BOT_TOKEN_HERE এর জায়গায় বসাও।")
    else:
        print("✅ Bot চলছে... টেলিগ্রামে গিয়ে মেসেজ দাও।")
        app = ApplicationBuilder().token(TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tts_handler))
        # === নতুন হ্যান্ডলার দুটি ===
        app.add_handler(CallbackQueryHandler(download_callback, pattern="^dl\\|"))
        app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE | filters.Document.VIDEO, video_handler))
        app.run_polling()
