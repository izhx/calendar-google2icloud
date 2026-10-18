FROM python:3.12-alpine3.22
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_PROGRESS_BAR=off
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt \
    && addgroup -g 10001 calsync \
    && adduser -D -H -u 10001 -G calsync -s /sbin/nologin calsync \
    && mkdir /data && chown calsync:calsync /data
COPY sync.py google_api.py authorize_google.py ./
COPY tests ./tests
USER 10001:10001
ENV STATE_FILE=/data/state.json
CMD ["python", "sync.py"]
