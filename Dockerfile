FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py index.html login.html ./
RUN useradd -m appuser && chown -R appuser /app
USER appuser
ENV COOKIE_SECURE=1
# One worker + threads keeps the in-memory login lockout consistent.
CMD gunicorn -w 1 --threads 4 -t 900 -b 0.0.0.0:${PORT:-5000} app:app
