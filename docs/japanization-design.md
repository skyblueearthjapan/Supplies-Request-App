# スプレッドシート日本語化 設計書（完全置換方式）

作成日: 2026-07-06
対象DB: `1VKbB13yhJPkyTYu7VYfHm-Zc3sl0nYp0npXUAxau4p4`
方針: **完全置換方式** — 定数値そのものを日本語化し、Client.html・SampleData・全既存データ・routeJson を移行する。

> ⚠️ これは設計書です。実装はまだ行いません。内容合意後に着手します。

---

## 1. 目的とゴール

スプレッドシートを直接見たときに、**タブ名・ヘッダー行・セル内の値**が可能な限り日本語で読めるようにする。
- アプリ画面は元々ほぼ日本語表示（`*_LABELS`）なので、UIの見た目は基本的に変わらない。
- 変わるのは「生のスプレッドシート」の可読性。

## 2. 変換しない（英語のまま維持する）もの ＝ 破壊防止のための決定

| 対象 | 理由 |
|---|---|
| **オブジェクトのプロパティキー**（`requestId`, `status`, `currentStep`, `route`, `category` 等） | Client.html / Workflow.gs / Helpers.gs 全体で `r.status` のように参照。日本語化するとアプリ全壊。**ヘッダーは「表示ラベル」だけ日本語化し、内部キーは英語維持**（読み書きは列インデックス＝位置ベースなので安全）。 |
| **Settings の `key` 列**（`thresholdAmount` 等） | コード内で55箇所、オブジェクトのキーとして使用。`description`列は既に日本語。 |
| **`sendAs`（TO / CC）** | メール送信の標準用語。`Recipients.gs` で `=== 'TO' / 'CC'` 比較。翻訳の意味が薄く、リスクのみ増える。 |
| **`active`（true / false）** | 真偽値。 |
| **`reasonCode`（A〜F）** | 単なるコード。表示は `REASONS` の日本語ラベル経由。※日本語化したい場合は §7 の任意対応。 |

## 3. 変換する対象と目標マッピング

### 3-1. タブ名（`SHEETS` 定数）※全8シートを日本語化（確定）

| 現在 | 変更後 |
|---|---|
| `Requests` | `申請` |
| `RequestItems` | `申請明細` |
| `StatusHistory` | `履歴` |
| `Settings` | `設定` |
| `ApproverMaster` | `承認者マスタ` |
| `WorkersCache` | `作業員マスタ` |
| `NotificationRecipients` | `通知宛先マスタ` |
| `DeptSupervisors` | `部署別上席マスタ` |

### 3-2. ヘッダー行（表示ラベルのみ日本語化。内部キーは英語維持）
`ensureSheet_` を「内部キー配列（英語・位置用）」と「表示ラベル配列（日本語・row1書き込み用）」の2本立てに変更する。

**Requests（申請）**
| 内部キー | ヘッダー表示 |
|---|---|
| requestId | 申請ID |
| createdAt | 作成日時 |
| updatedAt | 更新日時 |
| submittedAt | 申請日時 |
| completedAt | 完了日時 |
| applicantEmail | 申請者メール |
| applicantName | 申請者氏名 |
| department | 部署 |
| requestDate | 申請日 |
| reasonCode | 理由コード |
| reasonDetail | 理由詳細 |
| totalAmount | 合計金額 |
| status | ステータス |
| currentStep | 現在ステップ |
| currentApproverEmail | 現在承認者メール |
| currentApproverName | 現在承認者氏名 |
| routeJson | 承認経路(JSON) |
| pdfFileId | PDFファイルID |
| pdfUrl | PDF URL |
| version | バージョン |
| amountWaived | 金額免除 |
| category | 品目区分 |

**RequestItems（申請明細）**: itemId→明細ID / requestId→申請ID / lineNo→行番号 / name→品名 / model→型番 / maker→メーカー / quantity→数量 / unitPrice→単価 / amount→金額 / desiredDeliveryDate→希望納期 / note→備考

**StatusHistory（履歴）**: historyId→履歴ID / requestId→申請ID / happenedAt→発生日時 / actorEmail→操作者メール / actorName→操作者氏名 / action→操作 / fromStatus→変更前ステータス / toStatus→変更後ステータス / fromStep→変更前ステップ / toStep→変更後ステップ / comment→コメント

**Settings（設定）**: key→キー / value→値 / description→説明　※key列の「値」は英語維持

### 3-3. セル内コード値（完全置換の本体）
衝突による事故を防ぐため、**全列横断で一意な日本語文字列**を採用する（同一文字列を異なるenumで使い回さない）。

**STATUS（status列 / from・toStatus列）**
| コード | 変更後 |
|---|---|
| IN_REVIEW | 承認中 |
| RETURNED | 差戻し |
| COMPLETED | 完了 |
| CANCELLED | 取消 |

**STEPS（currentStep列 / from・toStep列 / routeJson配列）**
| コード | 変更後 |
|---|---|
| SUPERVISOR | 上席 |
| PURCHASING_QUOTE | 購買見積 |
| GENERAL_MANAGER | 総務部長 |
| PRESIDENT | 社長 |
| PURCHASING | 購買手配 |
| APPLICANT | 申請者 |
| DONE | 完了済 ← STATUSの「完了」と区別するため意図的に別文字列 |

**ACTION（action列）**
| コード | 変更後 |
|---|---|
| SUBMIT | 申請 |
| APPROVE | 承認 |
| RETURN | 差戻 ← STATUSの「差戻し」と区別 |
| RESUBMIT | 再申請 |
| UPDATE | 更新 |
| COMPLETE | 完了処理 ← STATUSの「完了」と区別 |
| ESCALATE | 社長決裁へ |
| QUOTE | 金額確定 |
| EXPEDITE | 金額不要至急 |
| PDF_GENERATE | PDF作成 |
| CANCEL | 取消処理 ← STATUSの「取消」と区別 |

**CATEGORIES（category列）**: MECH→メカ / ELEC→電気 / GENERAL→一般不明

**RECIPIENT_TYPES（NotificationRecipients.type列）**: GENERAL_AFFAIRS→総務部 / PURCHASING→購買 / PURCHASING_ELEC→電気購買
> ※このシートは§7対象。type列を変換する場合は §5-3 の検証修正が必須。

## 4. コード変更インベントリ（ファイル別）

| ファイル | 変更内容 | 規模 |
|---|---|---|
| **Constants.gs** | `SHEETS` 値、各enum（`STATUS`/`STEPS`/`ACTION`/`CATEGORIES`）の**値**を日本語へ。`*_LABELS` は表示用に維持（コード==ラベルとなり実質恒等だが、Client側の互換のため残す）。ヘッダー表示ラベル用の `*_HEADERS` 定数を新設。 | 中 |
| **Storage.gs** | `ensureSheet_` を「内部キー配列＋表示ラベル配列」対応に改修。`ensureSchema_` で各シートにラベルを渡す。ヘッダー一致判定もラベル基準に。**タブ改名対応**（旧名タブがあれば新名にrename）を追加。 | 中 |
| **Client.html** | ハードコードされた英語コード**36箇所**を新しい日本語値へ全置換（`'IN_REVIEW'`→`'承認中'`, `'SUPERVISOR'`→`'上席'`, `'MECH'`→`'メカ'`, `'DONE'`→`'完了済'` 等）。特に L800 の `['APPLICANT']…['DONE']`、L522/851 の `indexOf('PRESIDENT')`、L1276 のRECIPIENT_TYPES、L404 のステータスフィルタに注意。 | 大・要注意 |
| **SampleData.gs** | ハードコード**33箇所**（route配列・status・currentStep・history各コード）を新値へ。 | 中 |
| **Recipients.gs** | §5-3の検証修正（type値の検証を「キー照合」から「値照合」へ）。※type列を変換する場合のみ。 | 小 |
| **Migration.gs** | 新規移行関数 `migrateSpreadsheetToJapanese()` を追加（§5）。 | 中 |
| **Email.gs / Pdf.gs / PdfTemplate.html / Workflow.gs / Helpers.gs** | 定数経由のため**コード変更不要**（自動追従）。PdfTemplate.html の英語リテラル1件のみ要確認。 | 極小 |

## 5. データ移行スクリプト設計 `migrateSpreadsheetToJapanese()`

### 5-1. 原則
- **冪等**（再実行しても壊れない。既に日本語化済みの値はスキップ）
- **旧→新マップをスクリプト内にハードコード**（`SHEETS`定数に依存しない。デプロイ順序事故を防ぐ）
- **バックアップ前提**（実行前にスプレッドシートを複製）
- ログに変換件数を出力

### 5-2. 処理手順
1. `LockService` で排他ロック。
2. 各対象タブについて旧名で `getSheetByName` → 見つかれば新名へ `setName`（既に新名ならスキップ）。
3. ヘッダー行（row1）を日本語ラベルへ書き換え。
4. 値変換（列インデックス指定で該当セルのみ、旧コード→新コードで置換）:
   - **申請(Requests)**: `status`、`currentStep`、`category`、`routeJson`（JSON.parse→各要素をSTEPマップ変換→JSON.stringify）
   - **履歴(StatusHistory)**: `action`、`fromStatus`、`toStatus`、`fromStep`、`toStep`
   - （§7採用時）**NotificationRecipients**: `type`
5. 変換件数をLogger出力して完了メッセージを返す。

### 5-3. Recipients.gs 検証の必須修正
現状 `Recipients.gs:85`:
```js
if (!Object.prototype.hasOwnProperty.call(RECIPIENT_TYPES, type)) { ... }
```
`type` を **オブジェクトのキー**として検証している。値を日本語化するとキー（英語）と一致せず壊れる。
→ **値の集合で検証する**よう修正:
```js
var validTypes = Object.keys(RECIPIENT_TYPES).map(function(k){ return RECIPIENT_TYPES[k]; });
if (validTypes.indexOf(type) === -1) { ... }
```

## 6. デプロイ順序の危険と安全手順（最重要）

**危険**: 新コード（日本語`SHEETS`定数）をデプロイした状態で誰かがアプリを開くと、`ensureSchema_` が「日本語名タブが無い」と判断し**空の日本語タブを新規作成**してしまう（旧英語タブのデータが取り残される）。

**対策**: `ensureSheet_` に**タブ改名対応**を組み込む（旧英語名タブが存在し新名が無ければ rename する）。これによりデプロイ順序に関わらず安全。

**推奨実行手順**:
1. **バックアップ**: スプレッドシートを「コピーを作成」で複製。スクリプトプロパティ `DATA_SPREADSHEET_ID` を控える。
2. コード一式をデプロイ（`clasp push`）。※`ensureSheet_`の改名対応込み。
3. Apps Scriptエディタで `migrateSpreadsheetToJapanese()` を1回実行。ログで件数確認。
4. アプリを再読込し、一覧・承認・PDF・メール送信が正常か確認（§8）。
5. サンプルデータで全ステータス網羅テスト（`seedSampleData()` → 各操作）。

## 7. スコープ確定（ユーザー承認済み・2026-07-06）

| 項目 | 決定 |
|---|---|
| 残り4シートのタブ名 | **日本語化する**（§3-1に反映済み） |
| NotificationRecipients.type の値変換 | **実施する**（§5-3の検証修正を必須で行う） |
| reasonCode（A〜F）のセル値 | **A〜F のまま維持** |
| sendAs（TO/CC） | **維持** |

## 8. 検証計画

- **静的**: `clasp push` 後、エディタでシンタックスエラーなし。Client.html の残存英語コードリテラルを grep で0件確認。
- **移行後の整合**: 各シートのヘッダー・タブ名・コード値が新マップ通りか目視＋件数ログ。
- **機能**: 申請作成→上席承認→購買見積（金額確定・社長決裁分岐）→総務部長→社長→購買手配→完了、差戻し、取消、PDF生成、承認依頼メールの各フローを一巡。
- **routeJson**: 既存申請の経路表示（ステッパー）が崩れないこと。
- **回帰**: `docs/manual-test.md` があれば併用。

## 9. ロールバック

- コード: `git revert`（現在 origin/main と完全同期済み）。
- データ: バックアップ複製から復元、または逆変換マップで `migrateSpreadsheetToJapanese()` の逆関数を用意。
- 参照先: `DATA_SPREADSHEET_ID` を旧IDへ戻せば従来DBに復帰可能。

## 9-2. 第2次調査で判明した追加の要対応（確定）

コード全体を精査した結果、当初設計に加えて以下の対応が必須と判明した。

1. **キー照合の罠（2箇所）** — 値を日本語化すると壊れるため値照合へ書き換える:
   - `Helpers.gs normalizeCategory_`: `hasOwnProperty(CATEGORIES, key)` かつ `toUpperCase()` → **値の集合で照合**し、`toUpperCase()`を除去。
   - `Recipients.gs:85`: `hasOwnProperty(RECIPIENT_TYPES, type)` → **値の集合で照合**。
2. **表示ラベルマップの再キー化** — `STATUS_LABELS` / `STEP_LABELS` / `ACTION_LABELS` / `CATEGORY_LABELS` はコードをキーとし、`bootstrap`でクライアントにも送信される。**新しい日本語コードをキーに張り替える**（表示テキストは現状維持）。
3. **CSSクラスは英語ASCIIのまま据え置き** — Styles.html の `.badge.IN_REVIEW` / `.cat-chip.MECH` / `.hicon.SUBMIT` 等は変更しない。Client.html 側で「日本語コード → 英語クラス」の変換マップ（`STATUS_CLASS` / `CAT_CLASS` / `ACTION_CLASS`）を新設し、`badge()` / `categoryChip()` / 履歴アイコン描画で使う。`class="badge IN_REVIEW"` のようなプレゼン専用の直書きクラスは英語のまま維持（データではなく色指定）。
4. **Client側マップの再キー化** — `STEP_DESC`（ステップ説明）/ `HIST_GLYPH`（操作アイコン）/ `currentStateLabel()` 内の `byStep` / クライアントの `RECIPIENT_TYPES` 配列（option value）/ `CATEGORY_OPTIONS` を新コードへ。
5. **PdfTemplate.html** — `helpers.statusLabel(req.status)` 経由のため自動追従。英語リテラルの残存を最終grepで確認。
6. **enum衝突は安全と確認済み** — 全比較がフィールド単位（status↔STATUS、step↔STEPS、action↔ACTION）で、enum跨ぎの比較は存在しない。§3-3の一意文字列採用は「生セルの曖昧さ回避」と将来の安全のための保険。

## 10. 想定リスク（完全置換方式）

- Client.html 36箇所の手直し漏れ → ステータスフィルタ・ステッパー・カテゴリバッジの不整合。**grep 0件確認で担保**。
- 既存 routeJson の変換漏れ → 経路表示崩れ。**移行スクリプトでparse変換**。
- デプロイ順序事故 → §6の改名対応で回避。
- enum文字列衝突 → §3-3で全列一意化して回避。
- Recipients検証破壊 → §5-3で修正。
