FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
# Node >=22 is required by yt-dlp for YouTube challenges
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py index.html login.html ./
RUN useradd -m appuser && chown -R appuser /app
USER appuser
ENV COOKIE_SECURE=1 BEHIND_PROXY=1
# One worker + threads: downloads and login lockout are kept in memory.
CMD gunicorn -w 1 --threads 8 -t 900 -b 0.0.0.0:${PORT:-5000} app:app
