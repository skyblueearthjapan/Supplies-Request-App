var APP = Object.freeze({
  NAME: '貯蔵品購入申請',
  TIME_ZONE: 'Asia/Tokyo',
  VERSION: '1.0.0',
  REQUEST_ID_PREFIX: 'REQ',
  DEFAULT_THRESHOLD: 100000,
  PROP_SPREADSHEET_ID: 'DATA_SPREADSHEET_ID',
  PROP_PDF_FOLDER_ID: 'PDF_FOLDER_ID'
});

var SHEETS = Object.freeze({
  REQUESTS: '申請',
  ITEMS: '申請明細',
  APPROVERS: '承認者マスタ',
  HISTORY: '履歴',
  SETTINGS: '設定',
  WORKERS_CACHE: '作業員マスタ',
  RECIPIENTS: '通知宛先マスタ',
  DEPT_SUPERVISORS: '部署別上席マスタ'
});

// 旧英語タブ名 → 新日本語タブ名。既存スプレッドシートのタブ改名（移行・自己修復）に使う。
var LEGACY_SHEET_NAMES = Object.freeze({
  'Requests': SHEETS.REQUESTS,
  'RequestItems': SHEETS.ITEMS,
  'ApproverMaster': SHEETS.APPROVERS,
  'StatusHistory': SHEETS.HISTORY,
  'Settings': SHEETS.SETTINGS,
  'WorkersCache': SHEETS.WORKERS_CACHE,
  'NotificationRecipients': SHEETS.RECIPIENTS,
  'DeptSupervisors': SHEETS.DEPT_SUPERVISORS
});

var DEPT_SUPERVISOR_COLUMNS = ['department', 'email', 'name', 'active', 'title'];

// 外部マスタシート（読み取り専用、作業員マスタのソース）
var EXTERNAL_MASTER_SPREADSHEET_ID = '1iu5HoaknlW1W1HheeYv0jqcRq-aY0SyEE2seQd2pHkQ';
var MASTER_WORKER_GID = 684189184;

// 部署別 上席（承認者）マスタの外部ソース。DeptApprovers シートを同期して DeptSupervisors キャッシュへ取り込む。
var DEPT_APPROVER_SPREADSHEET_ID = '1Knx_kaQMZZams65J1oeSDaBeWUt8XXanNe94XSAHKFQ';
var DEPT_APPROVER_SHEET_NAME = 'DeptApprovers';

var WORKER_CACHE_COLUMNS = ['workerCode', 'name', 'dept', 'location', 'staffType'];

var RECIPIENT_COLUMNS = ['type', 'email', 'name', 'active', 'sendAs'];

var RECIPIENT_TYPES = Object.freeze({
  GENERAL_AFFAIRS: '総務部',
  PURCHASING: '購買',
  PURCHASING_ELEC: '電気購買'
});

// 申請の品目区分。メール送付先（メカ購買／電気購買）の振り分けに使う。
// メカ購買は一般兼用のため、GENERAL（一般・不明）は MECH と同じ宛先に送る。
var CATEGORIES = Object.freeze({
  MECH: 'メカ',
  ELEC: '電気',
  GENERAL: '一般不明'
});

// 表示ラベルは「セル格納コード」をキーにする（コード値の日本語化に追従）。
var CATEGORY_LABELS = Object.freeze({
  'メカ': 'メカ',
  '電気': '電気',
  '一般不明': '一般・不明'
});

var REQUEST_COLUMNS = [
  'requestId',
  'createdAt',
  'updatedAt',
  'submittedAt',
  'completedAt',
  'applicantEmail',
  'applicantName',
  'department',
  'requestDate',
  'reasonCode',
  'reasonDetail',
  'totalAmount',
  'status',
  'currentStep',
  'currentApproverEmail',
  'currentApproverName',
  'routeJson',
  'pdfFileId',
  'pdfUrl',
  'version',
  'amountWaived',
  'category'
];

var ITEM_COLUMNS = [
  'itemId',
  'requestId',
  'lineNo',
  'name',
  'model',
  'maker',
  'quantity',
  'unitPrice',
  'amount',
  'desiredDeliveryDate',
  'note'
];

var APPROVER_COLUMNS = [
  'department',
  'applicantEmail',
  'applicantName',
  'supervisorEmail',
  'supervisorName',
  'generalManagerEmail',
  'generalManagerName',
  'presidentEmail',
  'presidentName',
  'purchasingEmail',
  'purchasingName',
  'active',
  'supervisorTitle',
  'generalManagerTitle',
  'presidentTitle',
  'purchasingTitle',
  'purchasingElecEmail',
  'purchasingElecName',
  'purchasingElecTitle'
];

var HISTORY_COLUMNS = [
  'historyId',
  'requestId',
  'happenedAt',
  'actorEmail',
  'actorName',
  'action',
  'fromStatus',
  'toStatus',
  'fromStep',
  'toStep',
  'comment'
];

var SETTING_COLUMNS = ['key', 'value', 'description'];

// ===== ヘッダー行の表示ラベル（日本語） =====
// 内部キー（*_COLUMNS）は英語のまま維持し、読み書きは列インデックス（位置）で行う。
// row1 に書き込む見出しテキストだけを日本語化する。各配列は対応する *_COLUMNS と同順・同数。
var REQUEST_HEADERS = [
  '申請ID', '作成日時', '更新日時', '申請日時', '完了日時',
  '申請者メール', '申請者氏名', '部署', '申請日', '理由コード',
  '理由詳細', '合計金額', 'ステータス', '現在ステップ', '現在承認者メール',
  '現在承認者氏名', '承認経路(JSON)', 'PDFファイルID', 'PDF URL', 'バージョン',
  '金額免除', '品目区分'
];

var ITEM_HEADERS = [
  '明細ID', '申請ID', '行番号', '品名', '型番', 'メーカー',
  '数量', '単価', '金額', '希望納期', '備考'
];

var APPROVER_HEADERS = [
  '部署', '申請者メール', '申請者氏名', '上席メール', '上席氏名',
  '総務部長メール', '総務部長氏名', '社長メール', '社長氏名',
  '購買メール', '購買氏名', '有効', '上席役職', '総務部長役職',
  '社長役職', '購買役職', '電気購買メール', '電気購買氏名', '電気購買役職'
];

var HISTORY_HEADERS = [
  '履歴ID', '申請ID', '発生日時', '操作者メール', '操作者氏名',
  '操作', '変更前ステータス', '変更後ステータス', '変更前ステップ', '変更後ステップ', 'コメント'
];

var SETTING_HEADERS = ['キー', '値', '説明'];

var WORKER_CACHE_HEADERS = ['作業員コード', '氏名', '部署', '勤務地', '区分'];

var RECIPIENT_HEADERS = ['種別', 'メール', '表示名', '有効', '送信区分'];

var DEPT_SUPERVISOR_HEADERS = ['部署', 'メール', '氏名', '有効', '役職'];

var STATUS = Object.freeze({
  IN_REVIEW: '承認中',
  RETURNED: '差戻し',
  COMPLETED: '完了',
  CANCELLED: '取消'
});

// 表示ラベルは「セル格納コード」をキーにする。bootstrap でクライアントにも送信される。
var STATUS_LABELS = Object.freeze({
  '承認中': '承認中',
  '差戻し': '差戻し',
  '完了': '完了',
  '取消': '取消'
});

var STEPS = Object.freeze({
  SUPERVISOR: '上席',
  PURCHASING_QUOTE: '購買見積',
  GENERAL_MANAGER: '総務部長',
  PRESIDENT: '社長',
  PURCHASING: '購買手配',
  APPLICANT: '申請者',
  DONE: '完了済'
});

var STEP_LABELS = Object.freeze({
  '上席': '上席',
  '購買見積': '購買（見積）',
  '総務部長': '総務部長',
  '社長': '社長',
  '購買手配': '購買',
  '申請者': '申請者',
  '完了済': '完了'
});

var ACTION = Object.freeze({
  SUBMIT: '申請',
  APPROVE: '承認',
  RETURN: '差戻',
  RESUBMIT: '再申請',
  UPDATE: '更新',
  COMPLETE: '完了処理',
  ESCALATE: '社長決裁へ',
  QUOTE: '金額確定',
  EXPEDITE: '金額不要至急',
  PDF_GENERATE: 'PDF作成',
  CANCEL: '取消処理',
  RECALL: '前段戻し'
});

var ACTION_LABELS = Object.freeze({
  '申請': '申請',
  '承認': '承認',
  '差戻': '差戻し',
  '再申請': '再申請',
  '更新': '更新',
  '完了処理': '完了',
  '社長決裁へ': '社長決裁へ',
  '金額確定': '金額確定',
  '金額不要至急': '金額不要・至急',
  'PDF作成': 'PDF作成',
  '取消処理': '取消',
  '前段戻し': '前段へ戻す'
});

var REASONS = [
  { code: 'A', label: '4月以降も都度購入を認めた消耗品（ウエス、オイル等）' },
  { code: 'B', label: '対象工番が特殊案件で新規に購入しなければ対応できないもの' },
  { code: 'C', label: '年度末に購入した消耗品を使い切った場合' },
  { code: 'D', label: '工具等を修理する場合' },
  { code: 'E', label: '工具等を修理するよりも新規で購入したほうが安価な場合' },
  { code: 'F', label: '社長が特別に認可した場合' }
];

var DEFAULT_SETTINGS = [
  { key: 'thresholdAmount', value: String(APP.DEFAULT_THRESHOLD), description: '社長決裁が必要になる合計金額（超過判定）' },
  { key: 'pdfFolderId', value: '', description: '完了PDFを保存するGoogle DriveフォルダID。空欄なら自動作成。' },
  { key: 'adminEmails', value: 'imaizumi@lineworks-local.info', description: '管理者メールアドレス。カンマ区切り。空欄の間は全員を初期管理者として扱う。' },
  { key: 'enableEmailNotifications', value: 'true', description: '承認依頼・差戻し・完了メールを送信する。' }
];
