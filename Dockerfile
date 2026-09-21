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

RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir .

RUN useradd -m -u 1000 trader \
    && mkdir -p /app/data_cache /app/results \
    && chown -R trader:trader /app
USER trader

VOLUME ["/app/data_cache", "/app/results"]

CMD ["python", "-m", "ztp.runner"]
