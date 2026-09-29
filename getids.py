import os
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder, MessageHandler, filters

load_dotenv()

async def show(update, ctx):
    u = update.effective_user
    print(u.id, u.full_name, u.username)
    await update.message.reply_text(f"{u.full_name}: {u.id}")

app = ApplicationBuilder().token("addtelegrambottokenhere").build()
app.add_handler(MessageHandler(filters.ALL, show))
app.run_polling()
