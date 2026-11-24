#!/usr/bin/env python3
"""貯蔵品購入申請 VPS 版の Cloudflare 登録（Tunnel・公開ホスト名・DNS だけ）。標準ライブラリのみ・再実行安全。

Cloudflare Access は使わない（計画書 9-2: 入口はアプリの Google ログインだけ）。
API トークンは Production-Schedule-Viewer の .env から読む（専用アカウント imaizumi@lineworks-local.info）。
TUNNEL_TOKEN は画面に出さず、--out のファイルにだけ書く。

  python setup_tunnel.py --dry-run          # 現状確認だけ（何も作らない）
  python setup_tunnel.py --out <file>       # 作成・更新
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.cloudflare.com/client/v4"
ZONE = "lineworks-local.net"
HOSTNAME = "supplies.lineworks-local.net"
TUNNEL_NAME = "supplies-vps"
SERVICE_URL = "http://web:80"  # docker compose の web サービス
TOKEN_ENV = Path(r"D:\projects\Production-Schedule-Viewer\remote-access\cloudflare\.env")

for _s in (sys.stdout, sys.stderr):
    if _s.encoding and _s.encoding.lower() not in ("utf-8", "utf8"):
        _s.reconfigure(encoding="utf-8")

args = sys.argv[1:]
DRY = "--dry-run" in args
OUT = Path(args[args.index("--out") + 1]) if "--out" in args else None
if not DRY and OUT is None:
    sys.exit("ERROR: --out <TUNNEL_TOKEN を書くファイル> を指定してください")

TOKEN = ""
for line in TOKEN_ENV.read_text(encoding="utf-8-sig").splitlines():
    if line.strip().startswith("CLOUDFLARE_API_TOKEN="):
        TOKEN = line.split("=", 1)[1].strip()
if not TOKEN:
    sys.exit("ERROR: CLOUDFLARE_API_TOKEN が見つかりません")


def api(method, path, body=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            doc = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR: {method} {path} -> HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    if not doc.get("success"):
        sys.exit(f"ERROR: {method} {path} -> {doc.get('errors')}")
    return doc["result"]


api("GET", "/user/tokens/verify")
accounts = api("GET", "/accounts?per_page=50")
account_id = accounts[0]["id"]
print(f"account: {accounts[0]['name']}")
zone = api("GET", f"/zones?name={ZONE}")[0]
print(f"zone: {zone['name']} ({zone['status']})")

recs = api("GET", f"/zones/{zone['id']}/dns_records?name={urllib.parse.quote(HOSTNAME)}") or []
tunnels = api("GET", f"/accounts/{account_id}/cfd_tunnel?name={TUNNEL_NAME}&is_deleted=false") or []
print(f"既存 DNS {HOSTNAME}: {[(r['type'], r['content']) for r in recs] or 'なし'}")
print(f"既存 Tunnel {TUNNEL_NAME}: {'あり' if tunnels else 'なし'}")

if recs and not all(r["content"].endswith(".cfargotunnel.com") for r in recs):
    sys.exit("ERROR: supplies に Tunnel 以外の DNS がある。ほかの用途の可能性があるので止める")
if recs and tunnels and not any(r["content"].startswith(tunnels[0]["id"]) for r in recs):
    sys.exit("ERROR: supplies の DNS が別の Tunnel を指している。止める")
if recs and not tunnels:
    sys.exit("ERROR: supplies の DNS が別の Tunnel を指している（supplies-vps が無い）。止める")
if DRY:
    print("--dry-run: ここまで")
    sys.exit(0)

tunnel = tunnels[0] if tunnels else api(
    "POST", f"/accounts/{account_id}/cfd_tunnel", {"name": TUNNEL_NAME, "config_src": "cloudflare"})
tunnel_id = tunnel["id"]
print(f"tunnel: {TUNNEL_NAME} (id={tunnel_id[:8]}…)")
api("PUT", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
    {"config": {"ingress": [{"hostname": HOSTNAME, "service": SERVICE_URL}, {"service": "http_status:404"}]}})
print(f"ingress: {HOSTNAME} -> {SERVICE_URL}")
body = {"type": "CNAME", "name": HOSTNAME, "content": f"{tunnel_id}.cfargotunnel.com", "proxied": True,
        "comment": "chozouhin supplies via cloudflared tunnel supplies-vps"}
if recs:
    api("PUT", f"/zones/{zone['id']}/dns_records/{recs[0]['id']}", body)
else:
    api("POST", f"/zones/{zone['id']}/dns_records", body)
print("dns: OK")
token = api("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token")
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(f"TUNNEL_TOKEN={token}\n", encoding="utf-8")
print(f"TUNNEL_TOKEN を書いた: {OUT}")
