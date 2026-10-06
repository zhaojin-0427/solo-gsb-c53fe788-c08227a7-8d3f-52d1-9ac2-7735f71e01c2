FROM python:3.11-slim

# ffmpeg provides ffprobe, used only to validate the rebuilt MP4. No
# transcoding is performed by the service.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV RECOVER_STORE=/data \
    RECOVER_TTL_SECONDS=3600 \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# shell form so uvicorn gets PID 1 signal behavior via exec
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
