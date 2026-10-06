FROM python:3.11-slim

# ffmpeg/ffprobe 用于输出结果校验；修复过程本身不转码
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV RECOVER_JOBS_DIR=/data/jobs \
    FFPROBE_BIN=/usr/bin/ffprobe \
    FFMPEG_BIN=/usr/bin/ffmpeg \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data/jobs
VOLUME ["/data/jobs"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
