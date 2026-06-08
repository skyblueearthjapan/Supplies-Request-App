function createId_(prefix) {
  var random = Math.floor(Math.random() * 1000000).toString().padStart(6, '0');
  return prefix + '-' + Utilities.formatDate(new Date(), APP.TIME_ZONE, 'yyyyMMddHHmmss') + '-' + random;
}

function nowString_() {
  return Utilities.formatDate(new Date(), APP.TIME_ZONE, 'yyyy-MM-dd HH:mm:ss');
}

function todayString_() {
  return Utilities.formatDate(new Date(), APP.TIME_ZONE, 'yyyy-MM-dd');
}

function sanitizeText_(value, maxLength) {
  var text = String(value === null || value === undefined ? '' : value).trim();
  if (maxLength && text.length > maxLength) {
    return text.slice(0, maxLength);
  }
  return text;
}

function normalizeEmail_(value) {
  return sanitizeText_(value, 256).toLowerCase();
}

// 氏名欄の役職カッコ（例「五十嵐（購買）」）を除去して姓名のみにする。
function stripRoleParen_(name) {
  return String(name || '').replace(/[（(][^）)]*[）)]/g, '').trim();
}

function parseNumber_(value) {
  if (value === '' || value === null || value === undefined) {
    return 0;
  }
  var number = Number(String(value).replace(/,/g, ''));
  return isFinite(number) ? number : 0;
}

function parseBoolean_(value) {
  return String(value).toLowerCase() === 'true' || value === true;
}

function getCurrentUser_() {
  var activeEmail = '';
  try {
    activeEmail = Session.getActiveUser().getEmail();
  } catch (error) {
    activeEmail = '';
  }

  var email = normalizeEmail_(activeEmail);
  if (!email) {
    throw new Error('ログイン中のGoogle Workspaceメールアドレスを取得できません。Webアプリの公開範囲を社内ドメインにしてください。');
  }

  return {
    email: email,
    name: email.split('@')[0]
  };
}

function splitEmails_(value) {
  return String(value || '')
    .split(/[,\n;]/)
    .map(function(email) {
      return normalizeEmail_(email);
    })
    .filter(Boolean);
}

// 承認者マスタの役職メール欄は「1部署に複数人」を許可するため、カンマ等区切りの
// メール一覧を保持する。重複を除き正規化して ", " 連結で1セルに格納する。
function normalizeEmailList_(value) {
  var seen = {};
  return splitEmails_(value).filter(function(email) {
    if (seen[email]) {
      return false;
    }
    seen[email] = true;
    return true;
  }).join(', ');
}

function isAdmin_(email) {
  var settings = getSettings_();
  var admins = splitEmails_(settings.adminEmails || '');
  return admins.length === 0 || admins.indexOf(normalizeEmail_(email)) !== -1;
}

function assertAdmin_(user) {
  if (!isAdmin_(user.email)) {
    throw new Error('管理者のみ実行できます。');
  }
}

function getThresholdAmount_() {
  var settings = getSettings_();
  var threshold = parseNumber_(settings.thresholdAmount);
  return threshold > 0 ? threshold : APP.DEFAULT_THRESHOLD;
}

function getPdfFolder_() {
  var settings = getSettings_();
  var properties = PropertiesService.getScriptProperties();
  var folderId = sanitizeText_(settings.pdfFolderId || properties.getProperty(APP.PROP_PDF_FOLDER_ID), 256);
  var folder;

  if (folderId) {
    folder = DriveApp.getFolderById(folderId);
  } else {
    folder = DriveApp.createFolder(APP.NAME + ' PDF');
    properties.setProperty(APP.PROP_PDF_FOLDER_ID, folder.getId());
    saveSettings_({ pdfFolderId: folder.getId() });
  }

  return folder;
}

// 申請日ごとのサブフォルダ（例「2026-06-06」）を取得（無ければ作成）。
// PDFが1フォルダに溢れないよう、日付フォルダに整理して保管する。
function getPdfFolderForDate_(dateStr) {
  var root = getPdfFolder_();
  var name = String(dateStr || '').slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(name)) {
    name = todayString_().slice(0, 10);
  }
  var existing = root.getFoldersByName(name);
  if (existing.hasNext()) {
    return existing.next();
  }
  return root.createFolder(name);
}

function jsonParse_(value, fallback) {
  if (!value) {
    return fallback;
  }
  try {
    return JSON.parse(value);
  } catch (error) {
    return fallback;
  }
}

function toClientRequest_(request) {
  return {
    requestId: request.requestId,
    createdAt: request.createdAt,
    updatedAt: request.updatedAt,
    submittedAt: request.submittedAt,
    completedAt: request.completedAt,
    applicantEmail: request.applicantEmail,
    applicantName: request.applicantName,
    department: request.department,
    requestDate: request.requestDate,
    reasonCode: request.reasonCode,
    reasonLabel: getReasonLabel_(request.reasonCode),
    reasonDetail: request.reasonDetail,
    totalAmount: parseNumber_(request.totalAmount),
    status: request.status,
    statusLabel: STATUS_LABELS[request.status] || request.status,
    currentStep: request.currentStep,
    currentStepLabel: STEP_LABELS[request.currentStep] || request.currentStep,
    currentApproverEmail: request.currentApproverEmail,
    currentApproverName: request.currentApproverName,
    route: jsonParse_(request.routeJson, []),
    isPresidentPending: request.status === STATUS.IN_REVIEW && request.currentStep === STEPS.PRESIDENT,
    amountWaived: parseBoolean_(request.amountWaived),
    pdfFileId: request.pdfFileId,
    pdfUrl: request.pdfUrl,
    version: request.version
  };
}

function getReasonLabel_(code) {
  var reason = REASONS.find(function(item) {
    return item.code === code;
  });
  return reason ? reason.label : '';
}

function formatCurrency_(value) {
  return '¥' + Math.round(parseNumber_(value)).toLocaleString('ja-JP');
}

function sanitizeFileName_(value) {
  return sanitizeText_(value, 120).replace(/[\\/:*?"<>|#%\u0000-\u001f]/g, '_') || '申請者';
}

function escapeHtml_(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
