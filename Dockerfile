# syntax=docker/dockerfile:1.28@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
FROM python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
RUN pip install --prefix=/install .


FROM python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966 AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The base image's system pip is never used at runtime, and its vendored
# certifi/urllib3/msgpack/setuptools carry CVEs that scanners flag. Remove it,
# along with ensurepip's bundled wheel.
RUN python -m pip uninstall -y pip \
 && rm -rf /usr/local/lib/python3.*/ensurepip/_bundled \
 && useradd --create-home --uid 1000 app \
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
