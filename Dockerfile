FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --gid 10001 sigma \
    && useradd --uid 10001 --gid sigma --no-create-home --shell /usr/sbin/nologin sigma

COPY pyproject.toml README.md ./
COPY scripts ./scripts
RUN python -m pip install --no-cache-dir .

COPY dashboard ./dashboard
RUN mkdir -p /app/.cache/sigma-tape \
    && chown -R sigma:sigma /app

USER 10001:10001

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3).read()"]

ENTRYPOINT ["python", "-m", "scripts.live_price_adapter"]
CMD ["--host", "0.0.0.0", "--port", "8765", "--refresh-mode", "never"]
