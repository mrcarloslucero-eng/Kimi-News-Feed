FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ai_news_pipeline.py .

# Secrets (ANTHROPIC_API_KEY, EMAIL_USERNAME, EMAIL_PASSWORD, ...) are
# passed at runtime via --env-file or -e flags, never baked into the image.
CMD ["python", "ai_news_pipeline.py"]
