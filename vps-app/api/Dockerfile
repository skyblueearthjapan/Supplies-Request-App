# api と worker で共用するイメージ。PDF 用に Chromium（Playwright）と日本語フォントを入れる。
# bookworm に固定（trixie では playwright の --with-deps が ttf-unifont 等を入れられず失敗する）
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Asia/Tokyo

RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-cjk fontconfig curl tzdata \
 && rm -rf /var/lib/apt/lists/*

# 日本語の既定フォントを Noto Sans CJK JP（明朝は Noto Serif CJK JP）にする
COPY fontconfig-local.conf /etc/fonts/local.conf
RUN fc-cache -f

WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
# pytest も入れる（VPS で `docker compose run --rm api pytest` の結合テストを行うため）
RUN pip install -r requirements-dev.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY tests ./tests
COPY pytest.ini ./

RUN useradd --create-home --uid 10001 app \
 && chown -R app:app /app \
 && chmod -R a+rX /ms-playwright
USER app

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=5 CMD curl -fs http://localhost:8000/healthz || exit 1

# 既定は api（DB の構成を最新にしてから起動）。worker は compose で command を差し替える。
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
