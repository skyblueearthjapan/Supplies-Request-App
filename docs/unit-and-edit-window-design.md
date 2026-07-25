# 設計書：明細の「単位」対応 と 金額確定前の編集範囲

対象要望（スタッフ）:

1. 部品ごとに単位が異なる（1ケース／1箱／1セット100個 等）のに、申請画面は数量しか持たない。
   プルダウンでの選択と、任意の単位の自由入力を両立したい。
2. 申請提出後でも、最終的な「金額確定」に至るまでは個数・単位などを変更できるようにしたい。

決定事項（2026-07-25 合意）:

- 単位UI = **セレクト＋「その他（自由入力）」で入力欄が出現**する方式。
- 候補リスト = **`Constants.gs` に固定**（`REASONS` / `CATEGORIES` と同じ流儀。bootstrap でクライアントへ配布）。
- 金額確定前の編集範囲 = **明細のみ**（部署・購入理由・品目区分は対象外。それらを直す場合は従来どおり差戻し→再申請）。

---

## 1. 単位（unit）の追加

### 1-1. データモデル

`Constants.gs`

```js
var ITEM_COLUMNS = [
  'itemId', 'requestId', 'lineNo', 'name', 'model', 'maker',
  'quantity', 'unitPrice', 'amount', 'desiredDeliveryDate', 'note',
  'unit'   // ← 末尾に追加
];
var ITEM_HEADERS = ['明細ID','申請ID','行番号','品名','型式','メーカー','数量','単価','金額','希望納期','備考','単位'];
```

**必ず末尾に追加する。** 読み書きは列インデックス（位置）ベースのため、途中挿入は既存全データの意味をずらす。
`ensureSheet_`（`Storage.gs:65`）がヘッダー不一致を検出して列を自動追加するため、移行スクリプトは不要。既存行の `unit` は空文字になる。

候補リストと既定値も同ファイルへ:

```js
// 明細の単位候補。網羅は狙わない（該当が無ければ「その他」で自由入力できる）。
var UNIT_OPTIONS = ['個','本','枚','台','箱','ケース','セット','袋','缶','巻','束','組','双','kg','L','m'];
var DEFAULT_UNIT = '個';
var UNIT_OTHER = '__other__';   // セレクトの番兵値。保存はしない
```

`getBootstrap()` の戻り値に `unitOptions: UNIT_OPTIONS, defaultUnit: DEFAULT_UNIT` を追加。

### 1-2. 値の扱い

| 場面 | 値 |
|---|---|
| 新規申請 | セレクトが既定で `個` を選択済み。必ず何らかの単位が入る |
| 自由入力 | 入力文字列をそのまま保存（例 `束(50本入)`）。`sanitizeText_(value, 20)` で20文字に制限 |
| 「その他」選択かつ入力が空 | クライアントで送信をブロック（toast「明細◯行目の単位を入力してください」） |
| 既存データ（unit が空） | **遡って `個` とみなさない。** 表示時は単位を出さず数量だけ表示する |

既存データの編集時の扱い：単位が空の旧明細は編集画面で「その他」＋空欄として開き、
保存時は上記の空チェックに引っかかる。**旧明細を編集して保存する際は単位の入力が必須になる。**
空を黙って `個` に変換すると、実際は箱・ケース単位だった明細を誤った単位で確定させてしまうため、
一度だけ入力を求める方を選ぶ（閲覧・承認・PDF出力は単位が空のままでも従来どおり通る）。

番兵値 `__other__` が万一サーバへ届いた場合の保険として、正規化関数を置く:

```js
function normalizeUnit_(value) {
  var unit = sanitizeText_(value, 20);
  return unit === UNIT_OTHER ? '' : unit;
}
```

### 1-3. 入力UI（申請フォーム／明細修正フォーム 共通）

`renderItemCards`（`Client.html:336`）の `.sub` グリッドは現在 3 列（数量／希望納期／備考は全幅）。
ここへ単位を差し込み、**数量 | 単位 | 希望納期** の 3 列にする。備考は従来どおり全幅。
`.icard-grid .sub { grid-template-columns: repeat(3, 1fr) }`（`Styles.html:237`）のままで収まり、
スマホでは既存のメディアクエリ（`Styles.html:458`）で 1 列に潰れる。**CSS 変更は不要。**

```
【通常時】
 数量        単位              希望納期
[  3  ]    [ ケース      ▼]   [2026-08-01]

【「その他（自由入力）」を選んだとき】
 数量        単位              希望納期
[  3  ]    [ その他       ▼]   [2026-08-01]
           [ 束(50本入)     ]   ← 入力欄が直下に出現・自動フォーカス
```

マークアップ:

```html
<div>
  <span class="mini-lbl">単位</span>
  <select class="control" data-unit-sel>
    <!-- UNIT_OPTIONS + <option value="__other__">その他（自由入力）</option> -->
  </select>
  <input class="control" data-f="unit" placeholder="例）束(50本入)" maxlength="20" hidden>
</div>
```

状態管理は `formState.items[i].unit` に**実値のみ**を保持する（`__other__` は保持しない）。描画時:

- `unit` が `UNIT_OPTIONS` に含まれる → セレクトをその値に、入力欄は hidden
- `unit` が空でなく候補に無い → セレクトを `その他`、入力欄を表示して値をセット
- `unit` が空（既存データ）→ セレクトを `その他`、入力欄を表示して空のまま

イベント: 既存の一括バインドは `querySelectorAll('input[data-f]')`（`Client.html:360`）で input のみを拾うため、
自由入力欄は**追加実装なしでそのまま `formState` へ反映される**。セレクトにだけ個別バインドを足す:

- `その他` を選択 → 入力欄を表示・フォーカス、`unit = ''`
- それ以外を選択 → `unit = 選択値`、入力欄を hidden にして値をクリア

`emptyItem()`（`Client.html:188`）に `unit: DEFAULT_UNIT` を追加。
`renderForm`（`Client.html:228`）と `renderItemEditForm`（`Client.html:413`）の items マッピングにも `unit` を追加する（**ここを漏らすと編集画面を開いた瞬間に単位が消える**）。

### 1-4. 表示ルール（読み取り側）

共通ヘルパをクライアント／サーバの双方に置く。

```js
// 数量と単位の連結表示。単位が空（旧データ）なら数量のみ。
function qtyText(quantity, unit) { return unit ? quantity + ' ' + unit : String(quantity); }
```

| 箇所 | 対応 |
|---|---|
| 詳細画面の明細テーブル（`Client.html:727`） | **列は増やさず**数量セルを「3 ケース」に |
| 見積・金額入力パネル（`Client.html:873`） | 数量セルを「3 ケース」に。さらに単価入力欄の右へ `/ケース` を添え、ヘッダを「単価(税抜・単位あたり)」にする |
| PDFプレビュー（`Client.html:1105`）／PDF本体（`PdfTemplate.html:160`） | 数量セルを 2 段（上に数値・下に小さく単位）。列幅は 数量 8%→10%、備考 14%→12% |
| 差戻し一斉共有メール（`Email.gs:220`） | 「数量: 3 ケース」 |
| 履歴の差分要約（`Workflow.gs:1389`） | `fields` に `{ key: 'unit', label: '単位' }` を追加 → 「明細1 単位: 個→ケース」 |

> **列を増やさない理由**：詳細テーブルの合計行は `colspan="5"` / `colspan="2"`（`Client.html:41-45`）、PDF側は `colspan="4"` / `colspan="2"`（`Client.html:51-55`, `PdfTemplate.html:181-199`）で桁を合わせている。列追加はこの 6 箇所すべての colspan 修正を伴い、ズレると税額行が崩れる。数量セルへの併記なら colspan は無傷。
> PDF は 1 ページに収まるよう余白・行間を圧縮済み（コミット `a2d9c97`）のため、列追加は特に避ける。

### 1-5. サーバ側で `unit` を通す 5 箇所

明細は「毎回まるごと作り直して書き戻す」設計（`replaceItems_`）のため、**フィールドを列挙している箇所すべてに `unit` を通さないと値が消える。**

| # | 場所 | 内容 |
|---|---|---|
| 1 | `normalizeRequestPayload_`（`Workflow.gs:1306`） | 新規申請・差戻し再編集の明細に `unit: normalizeUnit_(item.unit)` |
| 2 | `normalizeEditableItems_`（`Workflow.gs:1355`） | 承認前の明細修正に同上 |
| 3 | `replaceItems_`（`Storage.gs:274`） | 書き込みオブジェクトに `unit: item.unit` |
| 4 | **`confirmQuote` の `rebuilt`（`Workflow.gs:555-572`）** | **最重要。ここを漏らすと「金額を確定した瞬間に全明細の単位が消える」** |
| 5 | `SampleData.gs`（`insertSampleRequest_:176` / `insertTestRequest_`） | サンプル・テストデータの明細に `unit` |

---

## 2. 金額確定前の編集範囲

### 2-1. 現状

要望2の骨格は**すでに実装済み**。`canEditInReview_`（`Workflow.gs:1550`）:

```js
status === 承認中 && !amountWaived && totalAmount <= 0 &&
(currentStep === 上席 || currentStep === 購買見積)
```

`confirmQuote` はステップを総務部長へ進め、`confirmExpedited` は `amountWaived` を立てるため、
**金額が確定した瞬間に編集窓が自動的に閉じる**。個数（数量）も既に編集対象。
編集者は本人に限らず認証済み全員（コミット `588609e`）、変更は旧→新の差分として履歴に残り、現承認者へ通知される。

### 2-2. 塞ぐギャップ（1段リコール後に編集できない）

`recallStep` で「購買見積」へ戻すとき、`totalAmount` は意図的に保持される（`Workflow.gs:881-884`／次の `confirmQuote` が閾値を再判定するため）。
その結果 `totalAmount > 0` が残り、**金額を入れ直すために前段へ戻したのに明細を修正できない**状態になる。要望2の趣旨に反するため修正する。

**変更**：`totalAmount <= 0` の条件を撤去し、ステップ基準に一本化する。

```js
function canEditInReview_(request, user) {
  return !!(user && user.email) &&
    request.status === STATUS.IN_REVIEW &&
    !parseBoolean_(request.amountWaived) &&
    (request.currentStep === STEPS.SUPERVISOR || request.currentStep === STEPS.PURCHASING_QUOTE);
}
```

妥当性：経路は 上席 → 購買見積 → 総務部長 → (社長) → 購買手配 で固定であり、金額が確定すると必ず総務部長へ進む。
よって「ステップが上席／購買見積である」ことは「金額未確定である」ことと同値で、唯一の例外がリコール後の残骸。
`arrangeComplete` は `totalAmount > 0` を要求するが（`Workflow.gs:719`）、購買手配へ入るには `confirmQuote` / `confirmExpedited` を再通過するため ¥0 完了の抜け道は生じない。

**併せて** `updateRequestInReview` の更新パッチに `totalAmount: 0` を追加する。
`normalizeEditableItems_` は明細の単価・金額を 0 に落とすため、申請ヘッダの合計だけ古い値が残ると
「明細合計 0 なのに合計金額に残骸が出る」不整合になる。合計＝明細金額の総和という不変条件を保つ。

### 2-3. 対象外（今回やらないこと）

- 部署・申請者・品目区分・購入理由・理由詳細の編集（差戻し→再申請で従来どおり対応）
- 金額確定後の編集（仕様どおり決定事項として不可を維持）

---

## 3. 影響ファイルと作業順

| 順 | ファイル | 変更 |
|---|---|---|
| 1 | `Constants.gs` | `ITEM_COLUMNS` / `ITEM_HEADERS` に `unit`、`UNIT_OPTIONS` / `DEFAULT_UNIT` / `UNIT_OTHER` |
| 2 | `Helpers.gs` | `normalizeUnit_`、`qtyText_` |
| 3 | `Storage.gs` | `replaceItems_` に `unit` |
| 4 | `Workflow.gs` | `getBootstrap` に候補配布／`normalizeRequestPayload_`／`normalizeEditableItems_`／`confirmQuote` の `rebuilt`／`describeItemChanges_`／`canEditInReview_`／`updateRequestInReview` |
| 5 | `Email.gs` | 差戻し一斉共有メールの明細行 |
| 6 | `PdfTemplate.html` | 数量セル 2 段化、列幅 8%→10% ／ 備考 14%→12% |
| 7 | `Client.html` | `emptyItem` / `renderForm` / `renderItemEditForm` の items マッピング、`renderItemCards` の単位UI、送信前バリデーション、詳細テーブル・見積パネル・PDFプレビューの表示 |
| 8 | `SampleData.gs` | サンプル・テストデータに単位（ケース／缶／巻 など実物に近い値） |
| 9 | `README.md` | 明細項目に単位を追記 |

## 4. 検証観点

1. 新規申請 → プルダウンで `ケース` を選択 → 詳細・PDF・見積パネルすべてに「3 ケース」が出る
2. 「その他」→ `束(50本入)` を自由入力 → 保存・再表示・PDF で欠落しない
3. 「その他」を選んで空欄のまま申請 → クライアントで弾かれる
4. **購買が金額を確定 → 単位が消えていない**（`confirmQuote` の `rebuilt` 漏れ検出）
5. 承認前の明細修正で単位を変更 → 履歴に「明細1 単位: 個→ケース」が残り、現承認者へ通知が飛ぶ
6. 総務部長ステップまで進んだ申請では「明細を修正」ボタンが出ない（窓が閉じている）
7. 金額確定後に 1 段リコールで購買見積へ戻す → **明細を修正でき、合計金額が 0 にリセットされる**
8. 単位が空の既存申請を開く → 数量のみ表示され、レイアウトが崩れない
9. スマホ幅で明細カードが 1 列に潰れても単位欄が破綻しない
10. `npm run check`（構文チェック）が通る

## 5. 移行

スプレッドシートの手動移行は不要。`ensureSchema_` → `ensureSheet_` が「申請明細」タブに `単位` 列を自動追加する。
既存行は空欄のままで、表示側が空を許容する。**新旧コードが混在するとヘッダー不一致で列が書き換わるため、`clasp push` 後は本番 `/exec` デプロイの更新まで必ず一続きで行う**（`docs/handoff-2026-07-22.md` 参照：本番が旧バージョン固定になっている件）。
