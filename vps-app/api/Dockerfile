# api と worker で共用するイメージ。PDF 用に Chromium（Playwright）と日本語フォントを入れる。
# bookworm に固定（trixie では playwright の --with-deps が ttf-unifont 等を入れられず失敗する）
#
# ステージ:
# - runtime（既定。最後のステージ）: 本番のイメージ。tests と pytest は入れない。
# - test: runtime に pytest と tests を足したもの。VPS の結合テストで使う
#   （docker compose build で作るのは runtime だけ。test は `docker build --target test` で別に作る）。
FROM python:3.12-slim-bookworm AS base

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
COPY requirements.txt ./
RUN pip install -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

RUN useradd --create-home --uid 10001 app \
 && chown -R app:app /app \
 && chmod -R a+rX /ms-playwright

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=5 CMD curl -fs http://localhost:8000/healthz || exit 1

# 既定は api（DB の構成を最新にしてから起動）。worker は compose で command を差し替える。
# X-Forwarded-* を信じるのは FORWARDED_ALLOW_IPS（compose で web コンテナの固定 IP）からだけ。
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]


# ---- テスト用（本番には使わない） ----
FROM base AS test
COPY requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY tests ./tests
COPY pytest.ini ./
RUN chown -R app:app /app
USER app
CMD ["pytest"]


# ---- 本番（既定） ----
FROM base AS runtime
USER app
