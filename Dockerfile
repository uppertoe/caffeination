# syntax=docker/dockerfile:1.25
FROM python:3.14-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
RUN pip install --prefix=/install .


FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --create-home --uid 1000 app \
 && mkdir -p /data \
 && chown app:app /data

# Overlay onto the interpreter's own prefix so scripts and site-packages land
# on the default import path — no PYTHONPATH pinned to a python version.
COPY --from=builder /install /usr/local
WORKDIR /app
COPY app ./app

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

# --proxy-headers + --forwarded-allow-ips=* so uvicorn trusts the reverse
# proxy's X-Forwarded-Proto/For (only the proxy can reach this port). This lets
# request.url.scheme resolve to https behind Caddy, so the identity cookie is
# correctly marked Secure.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
