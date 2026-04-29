FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim

# ca-certificates for HTTPS to Spreaker / Anthropic / OpenAI; curl for HEALTHCHECK.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

RUN adduser --system --uid 65532 nonroot

WORKDIR /app
COPY --from=builder /install /usr/local
COPY . .

USER nonroot
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD curl -fsS http://localhost:8080/health/live || exit 1

ENV HOST=0.0.0.0 PORT=8080 UVICORN_WORKERS=4

CMD ["sh", "-c", "uvicorn app.main:app --host $HOST --port $PORT --workers $UVICORN_WORKERS"]
