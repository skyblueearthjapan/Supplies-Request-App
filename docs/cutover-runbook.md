# 切り替え当日の手順書（GAS 版 → VPS 版）

2026-11-25（水）17:30 に、本番を GAS 版から VPS 版（`https://supplies.lineworks-local.net`）へ切り替えます。
この手順書は、ディレクター（AI）とオーナーが上から順に実行するためのものです。各手順の「確かめ」が合わなければ、そこで止めて「戻す範囲」に従います。

- 決定事項の正: `docs/vps-migration-plan.md` 6 章・9 章。抜け調査: `docs/vps-migration-gap-audit.md` 3-6。運用: `vps-app/docs/RUNBOOK.md`。
- 戻せる期間: 切り替えから 5 営業日（2026-12-02（水）23:59 まで）。会社の休みが入るなら日付を直します【要確認】。
- コマンドの置き換えは `<...>` の所だけです。VPS のコマンドはすべて手元の PC から `ssh lineworks-vps-user` で流します。

## 事前に直すこと（コードを読んで見つけたもの）

| # | 内容 | 状態 |
|---|---|---|
| 1 | production では PDF をどのフォルダーにも書けた（safety が staging だけを照合していた）。設定 `pdfFolderId` を取り違えると本番の外に書いてしまう | **直した**（`app/safety.py`・`app/config.py`）。production でも `PROD_PDF_FOLDER_ID` を必須にし、その中にだけ書く。ステージング用のフォルダーへは書かない。あわせて、本番フォルダーの外（親・祖父母に `PROD_PDF_FOLDER_ID` が無い場所）にある古い PDF は、作り直しや取消のときもゴミ箱へ移さず、worker のログに警告を出すだけになる |
| 2 | 道具（`vps-app/tools/`）はイメージに入っていない。`import app` するには `PYTHONPATH=/app` が要る | 手順で対応（下のコマンドは、道具のフォルダーを読み取り専用で付けて動かす） |
| 3 | worker は起動した直後に書き出しを 1 回動かす（`EXPORT_HOURLY_UNTIL` の間は毎時）。起動した瞬間に本番のスプレッドシートが DB の内容で上書きされる | 手順で対応（worker を起動する前に、シートの控えを作り、api だけで安全装置を確かめる） |
| 4 | 設定 `pdfFolderId`（シートから移る値）と `.env` の `PROD_PDF_FOLDER_ID` が違うと、1 の直しで PDF が作れない（後処理が失敗して管理者へ通知が届く） | 手順 B-7 で照合する |
| 5 | 進行中の申請への送り直しの道具が無かった | **作った**（`vps-app/tools/resend_after_cutover.py`。テスト `api/tests/test_resend_after_cutover.py`） |
| 6 | 購買見積・購買手配の段の申請は、送り直しで PDF を新しく作る。GAS 版が作った PDF はドライブのゴミ箱へ移る（1 申請 1 ファイルの決まり。30 日は戻せる） | 仕様どおり。オーナーに伝えておく |
| 7 | 委任に `gmail.metadata` がまだ無い（送信が時間切れになったメールが、送り直されずに失敗として残る） | 事前 A-1 でオーナーが許可し直す |
| 8 | Google のログイン画面のアプリ名が「workreport」と出る（Work Report と同じ GCP プロジェクトのため） | オーナー判断（切り替えは止めない） |

確かめて問題が無かったもの:

- 書き出し先: production は本番 `1VKbB13yhJPkyTYu7VYfHm-Zc3sl0nYp0npXUAxau4p4` だけに書ける（許可リスト）。旧 DB・作業員マスタ・DeptApprovers にはどの環境からも書けない。
- メール: production では `MAIL_REDIRECT_TO` を使わない（転送は管理画面のテスト転送が有効な間だけ）。GAS 版のテスト転送はスクリプトプロパティにあり、DB へは移らない。
- worker の予定: マスタの同期は毎日 8 時台、書き出しは `EXPORT_HOURLY_UNTIL` まで毎時、その後は毎晩 2 時台。
- `STAGING_SPREADSHEET_ID`・`STAGING_PDF_FOLDER_ID` は production では書き込み先に使わない（1 の直しで、書かない先の照合に使う）。そのまま残します。

## 全体の流れ（目安）

| 時刻 | 手順 | 利用者から見ると |
|---|---|---|
| 〜12:00 | A. 事前の準備 | 今までどおり GAS 版を使える |
| 17:30 | B-1〜B-3 GAS 版を移転モードにし、後処理を流しきり、トリガーを止める | GAS 版を開くと「移りました」と出る |
| 17:40 | B-4〜B-8 VPS の DB を空にして最終移行し、照合する | どちらも使えない（VPS は止めている） |
| 18:10 | B-9〜B-12 本番の設定で起動し、書き出しと送り直し | VPS 版が使える |
| 18:30 | B-13〜B-15 オーナーの確認、完了の案内 | |

---

## A. 事前の準備（当日 12:00 まで）

### A-0. VPS を取り違えていないか（VPS の作業のたびに最初に行う）

```sh
ssh lineworks-vps-user "hostname"
```

- 確かめ: `srv1508169` と出ること。違う名前（特に `srv1300618`）なら、すぐに中止します。

### A-1. 委任の許可し直し（オーナー）

1. オーナーが imaizumi@ で `https://supplies.lineworks-local.net/auth/google-delegate/start` を開き、すべて許可します。
2. ディレクターが許可の中身を確かめます。

```sh
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose exec -T db psql -U chozouhin -d chozouhin"' <<'SQL'
SELECT account_email, granted_at, scopes LIKE '%gmail.metadata%' AS has_metadata, scopes LIKE '%gmail.send%' AS has_send,
       scopes LIKE '%/auth/drive%' AS has_drive, scopes LIKE '%spreadsheets%' AS has_sheets
FROM google_credentials;
SQL
```

- 確かめ: `imaizumi@lineworks-local.info` で、4 つとも `t`。
- 合わないとき: もう一度許可してもらいます。`has_metadata` だけが `f` のままなら、切り替えは進めてよいです（時間切れのメールが手動扱いになるだけ）。

### A-2. ステージングのテスト用データの扱い

決めたこと: 切り替えでは VPS の DB の業務の表を空にし、本番のスプレッドシートから移し直します。そのため次のものは引き継ぎません。

- 受け入れテストの申請 4 件（`REQ-20261124213504-873155` ほか、【受け入れテスト】）と、ステージングに移した 219 件
- ステージングの後処理・送信記録・同期の記録（`jobs`・`mail_log`・`sync_runs`）

次のものは残します。

- 委任のトークン（`google_credentials`）とログイン中のセッション（`sessions`）
- ステージング用の PDF フォルダー・書き出し先のシート（消さずに置いておく。片付けは見守りの後）

### A-3. このブランチを VPS へ出す（ステージングのまま）

`main` に取り込んだ後に行います（安全装置の直しと送り直しの道具が入る）。

```sh
# VPS の /opt/chozouhin は git のフォルダーではない。手元の main を git archive で送る（.env は上書きされない）
git archive --format=tar HEAD vps-app | ssh lineworks-vps-user 'test "$(hostname)" = srv1508169 || exit 9; rm -rf /tmp/chz && mkdir -p /tmp/chz && tar -x -C /tmp/chz && cp -a /tmp/chz/vps-app/. /opt/chozouhin/ && rm -rf /tmp/chz && echo synced'
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose build api" && sg docker -c "docker compose up -d" && sg docker -c "docker compose ps"'
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose exec -T web wget -qO- http://127.0.0.1/healthz"'
```

- 確かめ: `{"ok":true,"env":"staging"}`。`ls /opt/chozouhin/tools/resend_after_cutover.py` があること。
- 前のイメージのタグを控えます（戻すとき用）: `ssh lineworks-vps-user 'sg docker -c "docker image ls chozouhin-app"'`
- 失敗したら: RUNBOOK の「アプリの版を戻す」で前のタグに戻します。GAS 版には影響しません。

### A-4. 新しい GAS 版（移転モード入り）を本番のデプロイにする

プロパティ `MIGRATED_TO_URL` が無い間は、今と同じ動きです。手元の PC（PowerShell）で行います。

```powershell
cd D:\projects\chozouhin-shinsei-app
git pull
npm run check
npx clasp show-authorized-user          # imaizumi@lineworks-local.info であること
npx clasp push -f
npx clasp create-version "migration mode"   # 出た版の番号を控える（@95 の見込み）
npx clasp list-deployments              # 本番 /exec の deploymentId（AKfycbx_ZlT…）を確かめる
npx clasp update-deployment <本番の deploymentId> --versionNumber <控えた版の番号> --description "migration mode"
npx clasp list-deployments --json       # 本番の versionNumber が控えた番号になったこと
```

- 説明文は英数字にします（日本語だと反映されないことがあった）。
- 確かめ: 本番の `/exec` を開き、今までどおり一覧が出ること。
- 失敗したら: `npx clasp update-deployment <本番の deploymentId> --versionNumber 94 --description "rollback 94"` で @94 に戻します。

### A-5. 利用者への予告（オーナーが送る）

下の「利用者への案内文」の予告を、当日の朝までに送ります。

---

## B. 当日 17:30 からの手順

作業の記録を残す場所を VPS に作っておきます。

```sh
ssh lineworks-vps-user "hostname"   # srv1508169
ssh lineworks-vps-user 'mkdir -p /opt/chozouhin/cutover && ls -ld /opt/chozouhin/cutover'
```

### B-1. GAS 版を移転モードにする（17:30）

GAS エディタ（本番のプロジェクト）で、関数 `enterMigrationMode` を選んで実行します。引数なしで `https://supplies.lineworks-local.net` が入ります。

- 確かめ:
  - 実行ログに「移転モードにしました: https://supplies.lineworks-local.net」。
  - 本番の `/exec` を開くと「新しいアドレスへ移りました」と出ること。
  - `/exec?requestId=REQ-…`（実在の番号）を開き、「新しいアドレスで開く」のボタンを押すと `https://supplies.lineworks-local.net/?requestId=REQ-…` が開くこと（この時点では VPS は staging のデータ）。
  - 押さずに自動で移る仕掛けも入れてありますが、GAS の枠（iframe）の中ではブラウザが止めることがあります。自動で移らなくても問題ありません。
- 失敗したら: `exitMigrationMode` を実行すれば元どおりです。

この時点から GAS 版の画面の操作は、すべて「このアプリは新しいアドレスへ移りました」で止まります。

**1〜2 分待ってから B-2 へ進みます。** 移転モードにする直前に押された操作が、後回しの仕事（JOB_*）を積み終えるのを待つためです。

### B-2. GAS の後回しの仕事を流しきる

GAS エディタで `drainDeferredJobsForMigration` を実行します。

- 確かめ: ログが「残っている仕事（JOB_*）: 0 件」。
  - 0 件でないときは、もう一度実行します（1 回 4 分までしか流さないため）。
  - 「失敗した仕事（JOBFAILED_*）」が 1 件以上なら、ログの行（申請番号と関数名）を控えます。その申請は B-14 でオーナーと手で連絡します。
- 戻す範囲: ここで止めるなら、B-1 を戻すだけです。

### B-3. GAS のトリガー 3 つを消す

GAS エディタで `removeAllTriggersForMigration` を実行します。

- 確かめ: 「削除したトリガー」に `syncWorkersMaster`・`syncDeptSupervisorsMaster`・`runDeferredJobsSweep` の 3 つが出ること。GAS エディタの「トリガー」画面が空であること。
- 戻す範囲: `exitMigrationMode` → `restoreTriggersAfterRollback`。

### B-3b. もう一度、後回しの仕事を流しきる

B-2 の後に積まれた仕事が無いかを確かめるため、GAS エディタで `drainDeferredJobsForMigration` をもう一度実行します。

- 確かめ: 「流した仕事: 0 件」と「残っている仕事（JOB_*）: 0 件」。0 件でなければ、0 件になるまで繰り返します。
- 新しく「失敗した仕事（JOBFAILED_*）」が出ていれば、B-2 と同じく控えて B-14 で扱います。

### B-4. 本番のスプレッドシートの控えを作る

worker を起動すると本番のシートが DB の内容で上書きされるため、その前の控えを残します。

- Google ドライブで本番のスプレッドシート（`1VKbB13yhJPkyTYu7VYfHm-Zc3sl0nYp0npXUAxau4p4`）を開き、「ファイル → コピーを作成」。
- 名前: `【切替前の控え】貯蔵品購入申請 2026-11-25`。共有はしません。
- 確かめ: コピーの「申請」タブの行数が、元と同じこと。

### B-5. VPS の api と worker を止め、DB の控えを取る

```sh
ssh lineworks-vps-user "hostname"   # srv1508169
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose stop api worker" && sg docker -c "docker compose ps"'
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose exec -T db pg_dump -U chozouhin -Fc chozouhin" > /opt/chozouhin/cutover/before-cutover-20261125.pgdump && ls -l /opt/chozouhin/cutover/before-cutover-20261125.pgdump'
```

- 確かめ: api と worker が `exited`。db・web・cloudflared・backup は動いたまま。ダンプが 1KB より大きいこと。
- ダンプは `backups/` に置きません（backup コンテナが `chozouhin_*.dump` を 14 日で消すため）。
- 戻す範囲: `sg docker -c "docker compose start api worker"` で staging に戻ります。

### B-6. DB の業務の表を空にする

`google_credentials`（委任）と `sessions`（ログイン）と `alembic_version` は残します。

```sh
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose exec -T db psql -U chozouhin -d chozouhin -v ON_ERROR_STOP=1"' <<'SQL'
BEGIN;
TRUNCATE requests, request_items, history, approver_rules, settings, workers, notification_recipients,
         dept_supervisors, jobs, mail_log, idempotency_keys, sync_runs RESTART IDENTITY;
COMMIT;
SELECT (SELECT count(*) FROM requests) AS requests, (SELECT count(*) FROM jobs) AS jobs,
       (SELECT count(*) FROM google_credentials) AS delegate;
SQL
```

- 確かめ: `requests=0`・`jobs=0`・`delegate=1`。
- 戻す範囲: B-5 のダンプから戻します（RUNBOOK「DB を戻す」の `pg_restore`。ファイルは `/opt/chozouhin/cutover/before-cutover-20261125.pgdump`）。

### B-7. 本番から最終移行する（dry-run → 本番）

まず読むだけで確かめます。

```sh
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose run --rm --no-deps -T -e PYTHONPATH=/app -v /opt/chozouhin/tools:/app/tools:ro api python tools/migrate_from_sheets.py --source sheets --dry-run" | tee /opt/chozouhin/cutover/migrate-dryrun.json'
```

- 確かめ: 「止めました」が出ないこと。`summary.counts.requests` が前回の 219 件以上で、`excludedRequestIds` が `SAMPLE-`・`TEST-EXP-` だけ。`warnings` を読んで、知らないものが無いこと。

続けて書き込みます。

```sh
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose run --rm --no-deps -T -e PYTHONPATH=/app -v /opt/chozouhin/tools:/app/tools:ro api python tools/migrate_from_sheets.py --source sheets" | tee /opt/chozouhin/cutover/migrate-result.json'
```

- 確かめ: 最後の JSON の `verifyProblems` が `[]`、`droppedColumns` が `{}`。
- 失敗したら: 「移行先の DB が空ではありません」なら B-6 をやり直します。それ以外は B-6 に戻して原因を直すか、「C. 戻す」へ進みます。

### B-8. 照合する（件数・金額・状態ごと）

```sh
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose exec -T db psql -U chozouhin -d chozouhin"' <<'SQL'
SELECT (SELECT count(*) FROM requests) AS requests, (SELECT count(*) FROM request_items) AS items,
       (SELECT count(*) FROM history) AS history, (SELECT sum(total_amount) FROM requests) AS total_amount,
       (SELECT sum(amount) FROM request_items) AS item_amount;
SELECT status, count(*), sum(total_amount) FROM requests GROUP BY status ORDER BY status;
SELECT current_step, count(*) FROM requests WHERE status = '承認中' GROUP BY current_step ORDER BY current_step;
SELECT key, value FROM settings WHERE key IN ('pdfFolderId', 'adminEmails', 'enableEmailNotifications', 'thresholdAmount') ORDER BY key;
SQL
ssh lineworks-vps-user 'grep -E "^PROD_PDF_FOLDER_ID=" /opt/chozouhin/.env'
```

- 確かめ（`/opt/chozouhin/cutover/migrate-dryrun.json` の `summary` と比べる）:
  - `counts` の requests・request_items・history と件数が同じ。
  - `totalAmount`・`itemAmountTotal` と金額が同じ（DB は小数点以下の 0 が付くだけ）。
  - `statusCounts`・`amountByStatus`・`inReviewStepCounts` と、状態ごと・段ごとの件数と金額が同じ。
  - 本番のシートの「申請」タブの行数（見出しを除く）から `excludedRequestIds` の件数を引いた数が、requests と同じ。
  - `pdfFolderId` が `PROD_PDF_FOLDER_ID` と同じ（`1HzP6wEFPA6-sAXrEekICCy6uaACfq_1W` の見込み）。`enableEmailNotifications` が `true`。
- 合わないとき: 先へ進まず、B-6 からやり直すか「C. 戻す」へ進みます。`pdfFolderId` だけが違うときは、オーナーにどちらが正しいかを確かめて `.env` の `PROD_PDF_FOLDER_ID` を合わせます（DB の設定は管理画面からも直せます）。

### B-9. `.env` を本番の値にする

```sh
ssh lineworks-vps-user 'cd /opt/chozouhin && cp -p .env .env.before-cutover && sed -i -e "s/^APP_ENV=.*/APP_ENV=production/" -e "s/^EXPORT_HOURLY_UNTIL=.*/EXPORT_HOURLY_UNTIL=2026-12-02 23:59/" .env && grep -E "^(APP_ENV|EXPORT_HOURLY_UNTIL|PROD_PDF_FOLDER_ID|STAGING_SPREADSHEET_ID|STAGING_PDF_FOLDER_ID|MAIL_REDIRECT_TO)=" .env && ls -l .env'
```

- 確かめ: `APP_ENV=production`、`EXPORT_HOURLY_UNTIL=2026-12-02 23:59`、`PROD_PDF_FOLDER_ID` が入っている。権限が `-rw-------`。
- ステージング用の値（`STAGING_*`・`MAIL_REDIRECT_TO`）は消さずに残します。production では書き込み先に使いません。
- `.env` の中身をチャットに貼りません（上の grep は ID だけを出す）。
- 戻す範囲: `cp -p .env.before-cutover .env`。

### B-10. api だけを起動し、安全装置を確かめる

worker はまだ起動しません（起動するとすぐ本番のシートへ書き出すため）。

```sh
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose up -d api" && sg docker -c "docker compose ps" && sg docker -c "docker compose exec -T web wget -qO- http://127.0.0.1/healthz"'
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose exec -T api python -"' <<'PY'
from app import safety
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import Setting

s = get_settings()
print("APP_ENV:", s.app_env, "/ hourly export until:", s.export_hourly_until)
print("export ->", safety.export_spreadsheet_id(s))
with get_sessionmaker()() as db:
    row = db.get(Setting, "pdfFolderId")
print("pdf root ->", safety.pdf_root_folder_id(row.value if row else "", s))
print("forced redirect ->", repr(safety.forced_mail_redirect(s)))
for bad in (s.staging_spreadsheet_id, "1xCkpAIB8wPq49mzTwy6WNj3i485eV9oGwnzE-7aREQo", "1iu5HoaknlW1W1HheeYv0jqcRq-aY0SyEE2seQd2pHkQ"):
    try:
        safety.assert_writable_spreadsheet(bad, s)
        print("NG: writable", bad)
    except safety.UnsafeWriteError:
        print("OK: refused", bad)
try:
    safety.assert_writable_drive_folder(s.staging_pdf_folder_id, settings=s)
    print("NG: staging folder writable")
except safety.UnsafeWriteError:
    print("OK: refused staging folder")
PY
```

- 確かめ:
  - healthz が `{"ok":true,"env":"production"}`。
  - `export -> 1VKbB13yhJPkyTYu7VYfHm-Zc3sl0nYp0npXUAxau4p4`
  - `pdf root -> 1HzP6wEFPA6-sAXrEekICCy6uaACfq_1W`（B-8 の値）
  - `forced redirect -> ''`
  - `OK: refused` が 4 行、`NG` が 0 行。
- 失敗したら: api のログ（`sg docker -c "docker compose logs --tail=50 api"`）を見ます。設定の不足なら直して再起動。直せなければ「C. 戻す」へ。

### B-11. worker を起動し、本番のシートへの書き出しを確かめる

```sh
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose up -d worker" && sleep 60 && sg docker -c "docker compose logs --tail=30 worker"'
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose exec -T db psql -U chozouhin -d chozouhin"' <<'SQL'
SELECT id, kind, started_at, ok, row_count, left(message, 200) AS message FROM sync_runs ORDER BY id DESC LIMIT 5;
SQL
```

- 確かめ:
  - `kind=export` の行が `ok=t` で、message が `1VKbB13yhJPkyTYu7VYfHm-Zc3sl0nYp0npXUAxau4p4:` で始まる。
  - 本番のシートの「申請」タブの行数（見出しを除く）が、B-4 の控えの行数から除外した申請（`migrate-dryrun.json` の `excludedRequestIds`。`SAMPLE-`・`TEST-EXP-`）の件数を引いた数と同じ。
  - 下の SQL で出る DB の最新の申請番号が、本番のシートの「申請」タブにある（シートで検索する）。
  - 注: 書き出しは DB の内容でタブを丸ごと置き換えるため、`SAMPLE-`・`TEST-EXP-` の行（申請・明細・履歴）は本番のシートから消えます。控えは B-4 のコピーに残ります。

```sh
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose exec -T db psql -U chozouhin -d chozouhin"' <<'SQL'
SELECT request_id, created_at FROM requests ORDER BY created_at DESC, request_id DESC LIMIT 1;
SQL
```

- 1 分で行が出ないときは、手でもう 1 回動かします。

```sh
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose exec -T worker python -"' <<'PY'
from app.db import get_sessionmaker
from app.services.sheets import run_scheduled_export
print(run_scheduled_export(get_sessionmaker()))
PY
```

- 失敗したら: worker を止め（`docker compose stop worker`）、B-4 の控えと本番のシートを見比べます。シートが壊れていれば、控えの各タブを本番へ写し戻してから「C. 戻す」へ進みます。

### B-12. 進行中の申請へ依頼を送り直す（dry-run → 本番）

```sh
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose run --rm --no-deps -T -e PYTHONPATH=/app -v /opt/chozouhin/tools:/app/tools:ro api python tools/resend_after_cutover.py --dry-run" | tee /opt/chozouhin/cutover/resend-dryrun.txt'
```

- 確かめ:
  - 「注意: すべて … へ転送されます」が出ないこと（出たら管理画面のテスト転送を止める）。
  - 申請の件数が、B-8 の承認中の件数と同じ。差戻し中の申請が出ていないこと。
  - 上席の段は上席マスタの全員、総務部長は代表 1 人、社長は社長、購買見積・購買手配は購買の宛先で「（PDF を作り直して添付）」付き。
  - 一覧をオーナーに見せて、送ってよいか一声かけます。

```sh
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose run --rm --no-deps -T -e PYTHONPATH=/app -v /opt/chozouhin/tools:/app/tools:ro api python tools/resend_after_cutover.py" | tee /opt/chozouhin/cutover/resend-result.txt'
ssh lineworks-vps-user 'cd /opt/chozouhin && sleep 120 && sg docker -c "docker compose exec -T db psql -U chozouhin -d chozouhin"' <<'SQL'
SELECT state, count(*) FROM jobs GROUP BY state ORDER BY state;
SELECT request_id, kind, sent, attachment_file_id <> '' AS with_pdf, left(to_addrs, 60) AS to_addrs, left(last_error, 80) AS err
FROM mail_log ORDER BY id;
SQL
```

- 確かめ: 「積みました: N 件」（N は dry-run の申請の件数）。2 分ほどで `jobs` がすべて `done`、`mail_log` がすべて `sent=t`。購買の段は `with_pdf=t`。
- 二度目の実行は「止めました: 送り直しは実行済みです」で止まります（二重には送りません）。
- 失敗したら: `failed` の仕事は管理画面の「失敗した後処理」から送り直せます。`skipped` は送る前に誰かが操作した申請なので、そのままでよいです。

### B-13. 管理者による動作の確認（オーナー）

オーナーが imaizumi@ でログインして確かめます。

- 一覧の件数と、各タブの件数バッジが B-8 の段ごとの件数と合う。
- 承認中の申請を 1 件開き、明細・履歴・PDF のリンクが出る。購買の段の申請は、新しい PDF のリンクになっている。
- GAS 版の古いリンク（以前のメールにある `/exec?requestId=…`）を開き、「新しいアドレスで開く」のボタンを押すと、新しいアプリの同じ申請が開く（自動で移るかは問いません）。
- 管理画面:
  - テスト転送が「無効」。
  - 失敗した後処理が 0 件。
  - 委任の状態に `gmail.metadata` を含む。
- 本番のデータでは、承認などの書き込みは試しません。どうしても試すなら、オーナー自身の新規申請を 1 件作り、取消まで行います。
- GAS の「実行数」画面で、17:30 以降に `runDeferredJobsSweep`・`syncWorkersMaster`・`syncDeptSupervisorsMaster` が動いていない。

確かめが済んだら、利用者へ完了の案内を送ります（下の案内文）。

### B-14. B-2 で失敗が残っていた申請

B-2 のログに `JOBFAILED_*` があれば、その申請番号を新しいアプリで開き、オーナーと相談して次のどれかを行います。

- 関係者へ手で連絡する。
- 新しいアプリの詳細画面から「PDF再作成」を行う。

### B-15. 社内ポータルのリンク（後日でもよい）

GAS 版の移転のページが新しい URL へ案内するので、ポータルのリンクを今日差し替えなくても利用者は迷いません。差し替える前に、オーナーへ声をかけます。

**仕組み（2026-11-24 調査済み）:** ポータル（`.../s/AKfycbx2eyJMOYP9o--GPBuhY-pj071IIR6Kqb_0xALwwNzdLQZux0dIAlL3P9EoCucnzXA/exec`、版 @18）は、スプレッドシート「社内ダッシュボード」（`1DdhbJuz-zyS_2pKcM8y6Xf2pHofuzwNDgoW9eWcB0yg`、imaizumi@ の持ち物）に付いた GAS（scriptId `14cIfzsfoEb2Fn-VfjfsXU4zsiWJglHeLkxCKGhKLVlweVJAytemiMxaY`）です。開くたびに **APP_MASTER シート**（3 行目が見出し、4 行目から中身）を読み、`enabled=TRUE` かつ `status=OK` の行をボタンにします。キャッシュも再デプロイも要りません。

**差し替え:** APP_MASTER の **15 行目**（label「貯蔵品購入部品申請」、key「Supplies Request App」）の **F 列（url）** を、`https://script.google.com/a/macros/lineworks-local.info/s/AKfycbx_ZlT-k9DiaZA6RvRhDcaXGiKzPoikO-OhorWBybzfyP3J5-QKv_A0UXg02YfoB3X61g/exec` から `https://supplies.lineworks-local.net/` に書き換えるだけです。行の位置がずれていることがあるので、label で探してから書き換えます。

- 確かめ: ポータルを開き直し、「貯蔵品購入部品申請」の行き先が `https://supplies.lineworks-local.net/` になっていること。
- 戻すとき: F 列を元の GAS の `/exec` に戻す。

### C-1. VPS の書き込みを止める

```sh
ssh lineworks-vps-user "hostname"   # srv1508169
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose stop api" && sg docker -c "docker compose ps"'
```

- VPS 版に読み取り専用の切り替えは無いため、api を止めます（画面は開けなくなります）。worker は最後の書き出しのために動かしたままにします。

### C-2. 最後の書き出しを動かして確かめる

```sh
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose exec -T worker python -"' <<'PY'
from app.db import get_sessionmaker
from app.services.sheets import run_scheduled_export
print(run_scheduled_export(get_sessionmaker()))
PY
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose exec -T db psql -U chozouhin -d chozouhin"' <<'SQL'
SELECT id, kind, started_at, ok, left(message, 200) AS message FROM sync_runs WHERE kind = 'export' ORDER BY id DESC LIMIT 3;
SELECT id, kind, request_id, state, left(last_error, 80) AS err FROM jobs WHERE state IN ('pending', 'running', 'failed') ORDER BY id;
SQL
ssh lineworks-vps-user 'cd /opt/chozouhin && sg docker -c "docker compose stop worker"'
```

- 確かめ: 一番新しい `export` が `ok=t` で、C-1 の後の時刻。本番のシートの「申請」タブの最終行が、VPS で最後に作られた申請と同じ。
- 2 つ目の SELECT に行があれば、送れていない通知です。申請番号を控え、GAS 版に戻った後に手で連絡します。

### C-3. GAS 版を元に戻す

GAS エディタで順に実行します。

1. `exitMigrationMode`
2. `restoreTriggersAfterRollback`（ログに 3 つとも `true`）
3. `clearSharedCache`（シートを書き換えた後の古い表示を消す）

- 確かめ: 本番の `/exec` で一覧が出て、VPS で最後に作られた申請が見えること。GAS エディタの「トリガー」画面に 3 つあること。
- ポータルのリンクを差し替えていたら、GAS 版の `/exec` に戻します。
- 利用者へ「元のアドレスに戻しました」と知らせます。
- 戻した後に VPS の `.env` を staging に戻すときは `cp -p .env.before-cutover .env`。VPS の api は、もう一度切り替えると決めるまで止めたままにします。

---

## 利用者への案内文（例）

### 予告（切り替えの前日か当日の朝）

> 件名: 【お知らせ】貯蔵品購入申請のアドレスが 11 月 25 日（水）から変わります
>
> 皆さま
>
> 貯蔵品購入申請のアプリを、11 月 25 日（水）17 時 30 分から新しいアドレスへ移します。画面の使い方は変わりません。
>
> - 新しいアドレス: https://supplies.lineworks-local.net
> - 17 時 30 分から 19 時ごろまでは、申請や承認ができません。
> - 承認の途中の申請は、そのまま新しいアプリへ引き継ぎます。担当の方には、あらためて依頼のメールが届きます。
> - 前のアドレスやメールのリンクを開いても、新しいアドレスへ案内されます。
>
> 分からないことがあれば、今泉までお知らせください。

### 完了の知らせ（B-13 の後）

> 件名: 【完了】貯蔵品購入申請は新しいアドレスで使えます
>
> 皆さま
>
> 貯蔵品購入申請のアプリの移転が終わりました。これからは次のアドレスをお使いください。
>
> https://supplies.lineworks-local.net
>
> - 社内の Google アカウントでログインしてください。
> - ブックマークをお使いの方は、差し替えをお願いします。
> - 承認待ちの申請がある方には、新しいアドレスから依頼のメールをお送りしています。
>
> 動きがおかしいときは、今泉までお知らせください。

## 確かめること

- 戻せる期間の終わり（2026-12-02 23:59）の間に会社の休みが無いか。あれば `EXPORT_HOURLY_UNTIL` をずらします。
- 案内文の差出人と問い合わせ先（今泉オーナーでよいか）。
- 戻した場合、VPS 版のアドレスを開いた人への案内（今は api を止めると画面が開けなくなるだけです）。
