FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir "python-telegram-bot>=21,<23" "openai>=1,<3" "python-dotenv>=1,<2"
COPY bot.py /app/bot.py
CMD ["python", "-u", "/app/bot.py"]