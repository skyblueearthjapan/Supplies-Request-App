# 貯蔵品購入申請アプリ

Google Apps Script（GAS）で動作する、貯蔵品購入申請のデジタル申請アプリです。

## 構成

- フロントエンド: HtmlService + HTML/CSS/vanilla JavaScript
- サーバー: Apps Script V8
- データ保存: Google Spreadsheet
- PDF保存: Google Drive
- 通知: MailApp

## 初期セットアップ

1. Google Apps Scriptで新規プロジェクトを作成します。
2. `.clasp.json.example` を `.clasp.json` にコピーし、`scriptId` を作成したGASプロジェクトIDへ変更します。
3. 依存関係を入れてGASへ反映します。

```powershell
npm install
npm run push
```

4. Apps Scriptエディタで `setupApplication()` を1回実行します。
5. Webアプリとしてデプロイします。
   - 実行ユーザー: 自分
   - アクセスできるユーザー: ドメイン内のユーザー
6. 初回アクセス後、管理画面で以下を設定します。
   - 管理者メールアドレス
   - 承認者マスタ
   - 社長決裁しきい値（初期値: 100000）
   - PDF保存先フォルダID（空欄なら自動作成）

初期管理者は `imaizumi@lineworks-local.info` です。変更する場合は `Constants.gs` の `adminEmails` 初期値、または管理画面の管理者メールアドレスを更新してください。

管理者メールアドレスを空欄にした場合は、初期設定のため全ユーザーを管理者として扱います。運用開始前に必ず管理者を設定してください。

## 承認フロー

- 10万円以下: 申請者 → 上席者 → 総務部長 → 購買 → 完了
- 10万円超: 申請者 → 上席者 → 総務部長 → 社長 → 購買 → 完了
- 差戻し: 承認者がコメント付きで申請者へ戻し、申請者が修正して再申請

## 承認者マスタ

`ApproverMaster` は部署単位、または申請者単位で設定できます。

- `department`: 部署名。全体デフォルトにしたい場合は `*`。
- `applicantEmail`: 空欄なら部署単位ルール。入力すると該当申請者専用ルール。
- `supervisorEmail`: 上席者
- `generalManagerEmail`: 総務部長
- `presidentEmail`: 社長
- `purchasingEmail`: 購買担当

## clasp

このリポジトリは `clasp` 管理を前提にしています。`.clasp.json` は環境固有のためGit管理しません。

```powershell
npm run login
npm run push
npm run open
```

## ローカルチェック

```powershell
npm run check
```

このチェックはGAS実行環境を再現するものではなく、JavaScript構文と主要HTMLファイルの存在確認を行います。実際の動作確認はApps ScriptにpushしてWebアプリ上で行ってください。
