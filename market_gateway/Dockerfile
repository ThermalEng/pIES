FROM python:3.12-slim

ARG PIP_INDEX_URL=""

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid 10001 pies \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin pies

COPY sdk /tmp/sdk
COPY market /tmp/market
COPY market_gateway /tmp/market_gateway

RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --no-cache-dir -i "$PIP_INDEX_URL" /tmp/sdk /tmp/market /tmp/market_gateway; \
    else \
        pip install --no-cache-dir /tmp/sdk /tmp/market /tmp/market_gateway; \
    fi \
    && rm -rf /tmp/sdk /tmp/market /tmp/market_gateway \
    && chown -R pies:pies /app

USER 10001:10001

EXPOSE 8020

CMD ["uvicorn", "pies_market_gateway.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8020"]
