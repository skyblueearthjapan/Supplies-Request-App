# 貯蔵品購入申請アプリ VPS 版 実装仕様（エージェント共通の取り決め）

作成: 2026-11-24。読む順: この文書 → `docs/vps-migration-plan.md`（特に 4-0 と 9 章の決定事項）→ `docs/vps-migration-gap-audit.md`（3 章と 4 章）。
**方針: 今の GAS 版の機能と使い勝手をまったく残す。** 変えるのは計画書 9-1〜9-3 で決まった点だけ。迷ったら GAS 版のコードの動きに合わせ、ディレクターに報告する。

---

## 1. 置き場所とフォルダ

- コードは本リポジトリの `vps-app/`。GAS 版のファイル（ルートの *.gs / *.html）は**変更しない**（clasp の push 対象のため）。
- VPS: `srv1508169`（`ssh lineworks-vps-user`。作業前に `hostname` が `srv1508169` か確かめる）、置き場は `/opt/chozouhin`。sudo 不可、docker は `sg docker -c '...'`。ほかの `/opt/*` とコンテナに触れない。
- 公開 URL: `https://supplies.lineworks-local.net`（Cloudflare Tunnel `supplies-vps`）。

```
vps-app/
  docker-compose.yml        # db, api, worker, web, cloudflared, backup
  .env.example              # 秘密は .env（VPS 上、権限 600。git に入れない）
  api/
    Dockerfile              # python:3.12-slim + playwright(chromium) + fonts-noto-cjk。api と worker で共用
    pyproject.toml / requirements.txt
    app/
      main.py               # FastAPI。/api/rpc/{fn}、/auth/*、/healthz
      config.py             # 環境変数（APP_ENV=staging|production など）
      db.py / models.py     # SQLAlchemy 2.x
      rpc.py                # 画面から呼ぶ 22 関数の登録と呼び出し
      auth.py               # Google ログイン（OIDC）、セッション
      google_delegate.py    # imaizumi@ の委任トークン（Gmail/Drive/Sheets）
      domain/               # 業務ロジック（Workflow.gs / Helpers.gs / DeptSupervisors.gs / Recipients.gs の移植）
      services/mail.py      # 宛先の決め方（Email.gs の移植）と Gmail API 送信
      services/pdf.py       # Jinja2 テンプレート（PdfTemplate.html の移植）→ Chromium で PDF
      services/drive.py     # PDF をドライブの今のフォルダへ保存・旧版をゴミ箱へ
      services/sheets.py    # 毎晩（戻せる期間は毎時）今のスプレッドシートへ書き出し、移行の読み込み
      services/sync.py      # 作業員マスタ・DeptApprovers の同期（MasterData.gs / DeptSupervisors.gs の移植）
      jobs.py / worker.py   # 後回しの仕事（outbox）と定期処理
      templates/pdf.html.j2
    alembic/                # DB の構成
    tests/                  # pytest
  web/
    nginx.conf
    static/index.html       # Index.html + Styles.html + Client.html を 1 枚にしたもの（ほぼ移植）
  tools/
    migrate_from_sheets.py  # 本番スプレッドシート → DB（読むだけ）
  docs/RUNBOOK.md           # デプロイ・戻し・運用の手順
```

## 2. 画面とサーバーの取り決め（RPC）

- 画面の `gas(name, ...args)` を `fetch('/api/rpc/' + name, {method:'POST', body: JSON.stringify(args)})` に置き換える。**関数名・引数の並び・戻り値の JSON の形は GAS 版と同じ**にする（画面の手直しを最小にするため）。
- 応答: 成功は `200 {"ok": true, "result": ...}`、業務エラーは `400 {"ok": false, "error": "GAS 版と同じ日本語の文"}`。ログイン切れは `401`（画面はログインへ。戻り先を持つ）。
- 公開する関数（22）: `getBootstrap, getTabCounts, getRequests, getRequestDetail, createRequest, updateRequestDraft, updateRequestInReview, resubmitRequest, approveRequest, returnRequest, confirmQuote, confirmExpedited, arrangeComplete, recallStep, deleteRequest, generateRequestPdf, recordPresidentDecision, syncDeptSupervisorsNow, syncWorkersNow, saveApproverMaster, saveNotificationRecipients, saveSettings`。それ以外は呼べない。
- `runDeferredJobs` は不要（worker が実行）。詳細の `deferredJobs` の代わりに `pendingJobs: 件数` を返し、画面は件数が 0 になるまで数秒ごとに詳細を取り直す（入力中は描き直さない。今の `refreshDetailIfIdle` と同じ考え）。
- 日時は JSON では GAS 版と同じ文字列（`yyyy-MM-dd HH:mm:ss` / 日付は `yyyy-MM-dd`、日本時間、`T` や時差を付けない）。
- 二重押し対策: 書き込み系の呼び出しは画面が `X-Idempotency-Key` を付け、サーバーは同じキーの 2 回目に 1 回目の結果を返す。

## 3. DB（PostgreSQL 16）

- シートと 1 対 1 の表: `requests, request_items, approver_rules, history, settings, workers, notification_recipients, dept_supervisors`。列は Constants.gs の `*_COLUMNS` と同じ意味（DB は snake_case、JSON は今の camelCase キー）。
- 追加の列:
  - `requests.row_version`（更新回数。画面を開いた後の変更を見つける）
  - マスタ 3 表の `sort_order`（行の順番に意味がある）
  - `history.seq`（同じ秒の順番）
  - `request_items` の明細 ID は変わらない ID にする
- 日時は `timestamptz` で持ち、JSON にするときに日本時間の文字列へ。古い行で時刻が欠けたものはそのまま（0 時）。
- 申請 ID などの形式は今と同じ（`REQ-yyyyMMddHHmmss-6桁`）。重複は一意制約で防ぐ。
- 後回しの仕事: `jobs`（種類・申請 ID・照合用の状態と段・状態・試行回数）と `mail_log`（1 通ずつ。宛先・件名・送信済みか・Gmail のメッセージ ID）。**状態の変更と同じトランザクションで積む。** 送り直しは未送信の mail_log だけ（決定事項）。
- 同時の操作は、申請単位の行ロック（`SELECT ... FOR UPDATE`）で守る。GAS 版の全体ロックは使わない。
- 委任トークンは `google_credentials` 表に暗号化して置く（鍵は環境変数）。

## 4. 環境と安全装置（最重要）

- `APP_ENV=staging`（本番の切り替えまではこれ）:
  - メールは**すべて** `MAIL_REDIRECT_TO`（imaizumi@lineworks-local.info）へ寄せる（件名に【テスト転送】、本来の To/Cc は本文の頭）。
  - PDF はステージング用のドライブのフォルダへ保存する。**本番の PDF フォルダには書かない。**
  - スプレッドシートの書き出しは、ステージング用のコピーへ行う。**本番のスプレッドシート `1VKbB13yhJPkyTYu7VYfHm-Zc3sl0nYp0npXUAxau4p4` には書かない（読むだけ）。**
  - 起動時とすべての書き込み直前に、書き込み先が本番の ID でないことを確かめ、本番なら例外で止める。
- `APP_ENV=production` は、切り替えの日にオーナーの許可を得てからディレクターが設定する。
- 管理者: 設定 `adminEmails` ＋ 環境変数 `BOOTSTRAP_ADMINS`（imaizumi@）。一覧が空でも全員を管理者にしない。
- ログイン: Google OIDC（Authorization Code、state・nonce・aud・iss・exp・email_verified・hd を検証）。`lineworks-local.info` だけ。署名付きの HttpOnly クッキー。ログイン前の URL（`?requestId=`）を state で持ち、ログイン後に戻す。

## 5. 業務ロジックの移植の決まり

- Workflow.gs / Helpers.gs / DeptSupervisors.gs / Recipients.gs / Email.gs / Pdf.gs の**動きを 1 つずつ移す。** エラー文言も同じ。抜け調査 3-1 の決まり（承認者マスタの探し方、`*` の補い、購買はメカ・電気両方、文字数を黙って切る、消費税は切り捨てで合計に 1 回、一覧の条件、分割・リコール・再申請・取消の細則など）を守る。
- 変えるもの（計画書 9 章の決定）:
  - 全画面承認は状態と段を確かめる（GAS @94 と同じ。加えて画面を開いた段の照合）
  - 押印は氏名（上席マスタ・承認者マスタの氏名）
  - まだ来ていない段の印は空欄（プレビューと PDF をそろえる）
  - PDF は 1 ページに収める。バーコードは QR（申請の詳細 URL。PDF とプレビューの両方）
  - 二重押し防止
  - 申請日の既定は日本時間の今日
  - 再申請で金額不要（至急）を元に戻す
  - 上席承認待ちは上席には自分の部署だけ（管理者・総務部長は全部署。バッジも同じ範囲）
  - 未完了金額から取消を除く
  - マイナスの単価をはじく
  - 書きかけの入力を消す前に確認を出す
  - 一覧はページ送り（全件）
  - 詳細画面だけ URL に申請番号
  - 失敗したメールは未送信分だけ送り直す
  - テスト転送の機能を管理画面に残す
- 変えないもの: 見積確定・至急の承認依頼は総務部長の代表 1 人だけ。数量の小数を許す。そのほか GAS 版と同じ。

## 6. テスト

- `vps-app/api/tests` に pytest。
- 業務ロジックは、SQLite（メモリー）でも動くように書く。行ロックなど PostgreSQL だけの機能は差し替えられるようにする。
- 本番と同じ PostgreSQL での結合テストは、VPS 上で `docker compose run --rm api pytest` で行う。
- 受け入れテストは抜け調査 4 章の項目。メールは 17 種それぞれ To・Cc・件名・本文を GAS 版と比べるテストを持つ。

## 7. 作業の分担（並行）

| 担当 | 範囲 | 先に要るもの |
|---|---|---|
| F（土台） | フォルダの形、docker、設定、DB と alembic、RPC の仕組み、ログイン、委任トークン、テストの土台 | なし（最初に作る） |
| W（業務） | domain/ の全ロジックと 22 関数、テスト | F |
| U（画面） | web/static/index.html（移植と決定事項の画面側）、nginx | F の RPC の形 |
| M（メール・PDF） | services/mail.py・pdf.py・drive.py、jobs.py・worker.py、テンプレート | F |
| D（データ） | tools/migrate_from_sheets.py、services/sheets.py（書き出し）、services/sync.py、テスト | F |

担当はそれぞれ git worktree で作業し、ディレクターがまとめる。他の担当の範囲のファイルは書き換えず、必要ならインターフェイスを決めて報告する。
