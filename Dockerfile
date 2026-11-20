# Bank Hub: one image for the app, the scheduled sync and the CLI.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    BANKHUB_DATA_DIR=/data BANKHUB_BIND=0.0.0.0 TZ=Europe/Bucharest

RUN pip install --no-cache-dir uv && useradd --create-home --uid 10001 bankhub && \
    mkdir -p /data && chown bankhub:bankhub /data
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv pip install --system --no-cache . && rm -rf /root/.cache

USER bankhub
VOLUME /data
EXPOSE 8041
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8041/')" || exit 1
CMD ["bankhub", "web"]
