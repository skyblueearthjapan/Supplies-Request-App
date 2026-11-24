# 貯蔵品購入申請 VPS 版 運用手順（骨組み）

この文書は、デプロイ・戻し・秘密の置き場の手順です。値の正は `docs/vps-migration-plan.md` の 4-0 章です。作業の前に必ず読み合わせます。

## 最初に確かめること（毎回）

- `ssh lineworks-vps-user "hostname"` が `srv1508169` を返すこと。違えばすぐ中止します。
- `srv1300618` には絶対に触れません（別会社の本番が同居しています）。
- 置き場は `/opt/chozouhin`。ほかの `/opt/*` とコンテナには触れません。
- docker は `sg docker -c '...'` で動かします（lineworks ユーザーは sudo 不可）。

## 秘密の置き場

| もの | 置き場 | 備考 |
|---|---|---|
| `.env`（DB のパスワード、OAuth のクライアント secret、SESSION_SECRET、FERNET_KEY、TUNNEL_TOKEN） | VPS の `/opt/chozouhin/vps-app/.env`（`chmod 600`） | git に入れない。チャットに貼らない。見本は `.env.example` |
| 手元の控え | `.out/`（git から除外） | |
| 委任トークン（imaizumi@ の Gmail・ドライブ・スプレッドシート） | DB の `google_credentials` 表（FERNET_KEY で暗号化） | FERNET_KEY を変えたら、管理画面から許可し直す |

## 初回の準備（ディレクターが行う。実行前にオーナーへ値を見せて一声かける）

1. `/opt/chozouhin` を作り、lineworks ユーザーに渡す（root の作業）。
2. リポジトリを置き、`vps-app/.env` を `.env.example` から作って値を入れる（`chmod 600`）。
3. Cloudflare Tunnel `supplies-vps` を作り、`supplies.lineworks-local.net` → `http://chozouhin-web:80` に向ける。トークンを `.env` の `TUNNEL_TOKEN` へ。
4. Google Cloud（Work Report と同じプロジェクト）の OAuth クライアントに、リダイレクト URI を 2 つ登録する。
   - `https://supplies.lineworks-local.net/auth/callback`
   - `https://supplies.lineworks-local.net/auth/google-delegate/callback`
5. 起動（下の「デプロイ」）。
6. imaizumi@ でログインし、`/auth/google-delegate/start` を開いて Gmail・ドライブ・スプレッドシートを許可する。

## デプロイ

```sh
cd /opt/chozouhin && git pull
cd vps-app
TAG=$(git rev-parse --short HEAD)
sg docker -c "APP_IMAGE_TAG=$TAG docker compose build api"
sg docker -c "APP_IMAGE_TAG=$TAG docker compose up -d"
sg docker -c "docker compose ps"
sg docker -c "docker compose logs --tail=50 api worker"
```

- api は起動のたびに `alembic upgrade head` を流してから立ち上がります。DB の構成の変更とアプリの版は、続けて一度に出します。
- 設定の値が欠けていると、api と worker は起動しません（ログに欠けている名前が出ます）。
- 動作の確認: `sg docker -c "docker compose exec web wget -qO- http://127.0.0.1/healthz"` が `{"ok":true,...}`。
- web の IP を固定したため（api は web からの X-Forwarded-For だけを信じる）、ネットワーク `chozouhin-net` にサブネットを付けた。
  初めてこの版を出すとき・`CHOZOUHIN_NET_SUBNET` を変えたときは `docker compose down` → `up -d` でネットワークを作り直す。
- 委任の権限に `gmail.metadata`（送信済みメールのヘッダーを見るだけ）を足した。この版を出した後、管理画面から委任を許可し直す
  （許可し直すまでは、送信が時間切れになったメールは送信済みかを確かめられないため、送り直さずに失敗として残る）。

## 結合テスト（PostgreSQL）

```sh
sg docker -c "docker compose exec db psql -U chozouhin -c 'CREATE DATABASE chozouhin_test'"   # 初回だけ
sg docker -c "docker build --target test -t chozouhin-app:test ./api"   # 本番のイメージには tests と pytest が入っていない
sg docker -c "docker run --rm --network chozouhin-net -e TEST_DATABASE_URL=postgresql+psycopg://chozouhin:<pw>@db:5432/chozouhin_test chozouhin-app:test pytest"
```

DB 名に `test` を含まない URL は、テストが拒みます（表を消すため）。

## ロールバック

### アプリの版を戻す

```sh
cd /opt/chozouhin/vps-app
sg docker -c "docker image ls chozouhin-app"          # 前のタグを探す
sg docker -c "APP_IMAGE_TAG=<前のタグ> docker compose up -d api worker"
```

DB の構成を変えた版を戻すときは、先に `alembic downgrade <前の revision>` が要ります。戻せない変更（列の削除など）は出す前にダンプを取ります。

### DB を戻す（バックアップから）

```sh
ls -l backups/                                       # 0 バイトや極端に小さいものは使わない
sg docker -c "docker compose stop api worker"
sg docker -c "docker compose exec -T db pg_restore -U chozouhin -d chozouhin --clean --if-exists" < backups/chozouhin_YYYYMMDD_HHMM.dump
sg docker -c "docker compose start api worker"
```

### GAS 版へ戻す（切り替えから 5 営業日の間）

計画書 6-5 章と 9-3 章に従います。毎時の書き出し（`.env` の `EXPORT_HOURLY_UNTIL` に戻せる期間の終わりの日時を入れておく）で、今のスプレッドシートは最大 1 時間前の状態です。書き出しの結果は `sync_runs` 表に `kind=export` で残ります。GAS 版の「移転しました」を外し、トリガー 3 つ（`syncWorkersMaster`・`syncDeptSupervisorsMaster`・`runDeferredJobsSweep`）を再開します。詳しい手順は切り替えの前に書き足します。

## バックアップ

- `backup` コンテナが毎日 3:00 に `backups/chozouhin_YYYYMMDD_HHMM.dump` を作り、14 日より古いものを消します。起動直後にも 1 回取ります。
- 1KB 未満のダンプは失敗として消し、ログに `[backup] FAILED` を出します。`docker compose logs backup` で確かめます。
- VPS の外への写しの置き場は未定です（計画書 4-8）。

## 安全装置（APP_ENV=staging）

- メールはすべて `MAIL_REDIRECT_TO` へ寄せます。
- PDF は `STAGING_PDF_FOLDER_ID` の中にだけ書きます。本番の PDF フォルダー（`PROD_PDF_FOLDER_ID`）とその中には書きません。
- スプレッドシートの書き出しは `STAGING_SPREADSHEET_ID` へ。本番 `1VKbB13yhJPkyTYu7VYfHm-Zc3sl0nYp0npXUAxau4p4` には書きません。
- これらが本番を指していると、起動しません。書き込みの直前にも確かめ、本番なら止まります（`app/safety.py`）。
- `APP_ENV=production` への切り替えは、切り替えの日にオーナーの許可を得てからディレクターが行います。

## 確かめること

- 外への写しの置き場（4-8）
- 監視（外からの死活監視、失敗の通知先）
- GAS 版へ戻す手順の詳細
