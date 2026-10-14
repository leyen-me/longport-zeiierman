FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml requirements.txt ./
COPY ztp ./ztp
COPY scripts ./scripts

RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir .

RUN useradd -m -u 1000 trader \
    && mkdir -p /app/data_cache /app/results \
    && chown -R trader:trader /app
USER trader

VOLUME ["/app/data_cache", "/app/results"]

# 策略入口: 默认 ORB30 v2 (5min); 跑 ZTP 老策略则设 STRATEGY=ztp.runner
ENV STRATEGY=ztp.orb_runner
CMD ["sh", "-c", "exec python -m ${STRATEGY}"]
