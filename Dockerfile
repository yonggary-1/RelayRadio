FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime (Deno) to solve YouTube's challenges
COPY --from=denoland/deno:bin-2.9.8 /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .

ENV DATA_DIR=/data PORT=8000 PYTHONUNBUFFERED=1
EXPOSE 8000
VOLUME /data
CMD ["python", "main.py"]
