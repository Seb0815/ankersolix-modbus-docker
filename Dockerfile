FROM python:3.12-slim AS builder

ARG ANKER_SOLIX_UPSTREAM_TAG=""

ENV PATH="/opt/venv/bin:$PATH" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN python -m venv /opt/venv

COPY pyproject.toml README.md ./
COPY src ./src

RUN if [ -n "$ANKER_SOLIX_UPSTREAM_TAG" ]; then \
        PYTHONPATH=src python -m anker_solix_mqtt.upstream_fetch \
            --tag "$ANKER_SOLIX_UPSTREAM_TAG"; \
    else \
        PYTHONPATH=src python -m anker_solix_mqtt.upstream_fetch; \
    fi
RUN pip install .


FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN addgroup --system app && adduser --system --ingroup app --home /app app

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv

USER app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import http.client,os; host=os.getenv('ANKER_WEB__HOST','0.0.0.0'); host={'0.0.0.0':'127.0.0.1','::':'::1'}.get(host,host); connection=http.client.HTTPConnection(host,int(os.getenv('ANKER_WEB__PORT','8080')),timeout=3); connection.request('GET','/healthz'); raise SystemExit(0 if connection.getresponse().status == 200 else 1)"]

ENTRYPOINT ["anker-solix-mqtt"]
