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
  REQUESTS: 'Requests',
  ITEMS: 'RequestItems',
  APPROVERS: 'ApproverMaster',
  HISTORY: 'StatusHistory',
  SETTINGS: 'Settings'
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
  'version'
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
  'active'
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

var STATUS = Object.freeze({
  IN_REVIEW: 'IN_REVIEW',
  RETURNED: 'RETURNED',
  COMPLETED: 'COMPLETED'
});

var STATUS_LABELS = Object.freeze({
  IN_REVIEW: '承認中',
  RETURNED: '差戻し',
  COMPLETED: '完了'
});

var STEPS = Object.freeze({
  SUPERVISOR: 'SUPERVISOR',
  GENERAL_MANAGER: 'GENERAL_MANAGER',
  PRESIDENT: 'PRESIDENT',
  PURCHASING: 'PURCHASING',
  APPLICANT: 'APPLICANT',
  DONE: 'DONE'
});

var STEP_LABELS = Object.freeze({
  SUPERVISOR: '上席者',
  GENERAL_MANAGER: '総務部長',
  PRESIDENT: '社長',
  PURCHASING: '購買',
  APPLICANT: '申請者',
  DONE: '完了'
});

var ACTION = Object.freeze({
  SUBMIT: 'SUBMIT',
  APPROVE: 'APPROVE',
  RETURN: 'RETURN',
  RESUBMIT: 'RESUBMIT',
  UPDATE: 'UPDATE',
  COMPLETE: 'COMPLETE',
  PDF_GENERATE: 'PDF_GENERATE'
});

var ACTION_LABELS = Object.freeze({
  SUBMIT: '申請',
  APPROVE: '承認',
  RETURN: '差戻し',
  RESUBMIT: '再申請',
  UPDATE: '更新',
  COMPLETE: '完了',
  PDF_GENERATE: 'PDF作成'
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
