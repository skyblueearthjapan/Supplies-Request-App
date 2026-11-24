"""画面・nginx・compose・バックアップの設定の確認（ファイルを読むだけ。リポジトリの中で動かすときだけ）。"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

VPS_DIR = Path(__file__).resolve().parents[2]
WEB = VPS_DIR / "web"

pytestmark = pytest.mark.skipif(not (WEB / "nginx.conf").exists(), reason="リポジトリの外（イメージの中）では確かめない")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_csp_has_no_unsafe_inline_script():
    conf = read(WEB / "nginx.conf")
    csp = re.search(r'Content-Security-Policy "([^"]+)"', conf).group(1)
    script_src = re.search(r"script-src ([^;]+);", csp).group(1)
    assert script_src.strip() == "'self'"


def test_index_html_has_no_inline_script_or_event_handlers():
    html = read(WEB / "static" / "index.html")
    for tag in re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.S):
        assert tag.strip() == ""  # 中身のある script は無い（src だけ）
    assert '<script src="/app.js"></script>' in html
    assert not re.search(r"\son[a-z]+\s*=", html)
    js = read(WEB / "static" / "app.js")
    assert not re.search(r"""\son[a-z]+=\\?["']""", js)  # 作る HTML にも onclick などを書かない


def test_nginx_rate_limit_and_client_ip():
    conf = read(WEB / "nginx.conf")
    assert "limit_req_zone $chozouhin_client_ip zone=chozouhin_api:10m rate=10r/s;" in conf
    assert "limit_req zone=chozouhin_api burst=30 nodelay;" in conf
    assert "$http_cf_connecting_ip" in conf
    assert conf.count("proxy_set_header X-Forwarded-For $chozouhin_client_ip;") == 3
    assert "$proxy_add_x_forwarded_for" not in conf
    assert "access_log /var/log/nginx/access.log chozouhin;" in conf


def test_compose_pins_cloudflared_and_limits_forwarded_ips():
    compose = read(VPS_DIR / "docker-compose.yml")
    image = re.search(r"cloudflare/cloudflared:(\S+)", compose).group(1)
    assert re.fullmatch(r"\d{4}\.\d+\.\d+", image)  # latest などでなく版を固定
    assert "FORWARDED_ALLOW_IPS: ${CHOZOUHIN_WEB_IP:-10.91.0.10}" in compose
    assert "ipv4_address: ${CHOZOUHIN_WEB_IP:-10.91.0.10}" in compose
    dockerfile = read(VPS_DIR / "api" / "Dockerfile")
    assert "--forwarded-allow-ips='*'" not in dockerfile


def test_runtime_image_has_no_tests_or_pytest():
    dockerfile = read(VPS_DIR / "api" / "Dockerfile")
    stages = re.split(r"^FROM ", dockerfile, flags=re.M)
    assert stages[-1].startswith("base AS runtime")  # 既定（最後）のステージは本番
    base = next(s for s in stages if s.startswith("python:"))
    assert "tests" not in re.sub(r"#.*", "", base) and "requirements-dev" not in base


def test_backup_runner_uses_umask_077():
    script = read(VPS_DIR / "backup" / "backup_entrypoint.sh")
    runner = script.split('cat > "$RUNNER" <<EOF', 1)[1].split("\nEOF", 1)[0]
    assert "\numask 077\n" in runner
