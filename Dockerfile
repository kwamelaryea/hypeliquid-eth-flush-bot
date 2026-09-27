FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEMO_MODE=true \
    API_HOST=0.0.0.0 \
    BOT_DATA_DIR=/app/data

WORKDIR /app

COPY requirements.lock requirements.txt ./
RUN pip install --no-cache-dir -r requirements.lock \
    && addgroup --system bot \
    && adduser --system --ingroup bot bot

COPY --chown=bot:bot . .
RUN mkdir -p /app/data && chown -R bot:bot /app/data

USER bot
EXPOSE 8080
CMD ["sh", "start.sh"]
