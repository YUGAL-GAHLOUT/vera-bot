FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py composer.py conversation_handlers.py ./
ENV PORT=8080
EXPOSE 8080
# single worker on purpose: all context/conversation state lives in process memory
CMD ["sh", "-c", "uvicorn bot:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
