FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SANDBOX_GATEWAY_HOST=0.0.0.0 \
    SANDBOX_GATEWAY_PORT=8787 \
    GATEWAY_DATA_ROOT=/data

WORKDIR /app

COPY packages/sandbox-contracts /app/packages/sandbox-contracts
COPY services/sandbox-gateway /app/services/sandbox-gateway

RUN pip install --no-cache-dir \
    /app/packages/sandbox-contracts \
    /app/services/sandbox-gateway \
    && useradd --create-home --uid 10001 testagent \
    && mkdir -p /data \
    && chown -R testagent:testagent /data

USER testagent
EXPOSE 8787

CMD ["testagent-sandbox-gateway"]
