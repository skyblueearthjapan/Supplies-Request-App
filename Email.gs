// 宛名のロール表記。
function stepRoleSalutationLabel_(step) {
  if (step === STEPS.SUPERVISOR) { return '上席'; }
  if (step === STEPS.GENERAL_MANAGER) { return '総務'; }
  if (step === STEPS.PRESIDENT) { return '社長'; }
  if (step === STEPS.PURCHASING || step === STEPS.PURCHASING_QUOTE) { return '購買'; }
  return 'ご担当';
}

// 氏名・役職から個別の宛名を作る。例: ('井嵐 太郎','係長','購買') → '購買 井嵐係長'。
// 氏名なし → '<ロール> ご担当者 様'、役職なし → '<ロール> <姓> 様'。
function approverSalutation_(name, title, roleLabel) {
  var surname = name ? String(name).trim().split(/\s+/)[0] : '';
  if (surname && title) { return roleLabel + ' ' + surname + title; }
  if (surname) { return roleLabel + ' ' + surname + ' 様'; }
  return roleLabel + ' ご担当者 様';
}

// 役職ステップの氏名・役職を承認者マスタから取得（部署別→'*' フォールバック、既定名は使わない）。
function roleNameTitle_(applicantEmail, department, step) {
  var field = stepRoleField_(step);
  if (!field) { return { name: '', title: '' }; }
  var rule = null;
  try { rule = findApproverRule_(applicantEmail, department); } catch (error) { rule = null; }
  var name = rule ? (rule[field + 'Name'] || '') : '';
  var title = rule ? (rule[field + 'Title'] || '') : '';
  if (!name) {
    var star = findStarRule_();
    if (star) {
      name = star[field + 'Name'] || name;
      if (!title) { title = star[field + 'Title'] || ''; }
    }
  }
  return { name: name, title: title };
}

// 指定ステップの宛名。SUPERVISOR は承認者オブジェクト（DeptSupervisors 由来）を優先。
function salutationForStep_(request, step, approver) {
  var label = stepRoleSalutationLabel_(step);
  if (step === STEPS.SUPERVISOR) {
    return approverSalutation_(approver ? approver.name : '', approver ? approver.title : '', label);
  }
  var nt = roleNameTitle_(request.applicantEmail, request.department, step);
  return approverSalutation_(nt.name, nt.title, label);
}

function sendApprovalRequestEmail_(request, approver, extraNote) {
  if (!approver || !approver.email) {
    return;
  }
  var clientRequest = toClientRequest_(request);
  var subject = (extraNote ? '【★至急・要社長決裁★】' : '') + '[貯蔵品購入申請] 承認依頼 ' + request.requestId;
  var lines = [
    salutationForStep_(request, request.currentStep, approver),
    ''
  ];
  if (extraNote) {
    lines.push(extraNote, '');
  }
  lines.push(
    '貯蔵品購入申請の承認依頼があります。',
    '',
    '申請番号: ' + request.requestId,
    '申請者: ' + request.applicantName + '（' + request.applicantEmail + '）',
    '部署: ' + request.department,
    '確定金額: ' + amountText_(request),
    '現在ステップ: ' + clientRequest.currentStepLabel,
    '',
    getRequestUrl_(request.requestId)
  );

  sendEmail_(approver.email, subject, lines.join('\n'));
}

function sendReturnedEmail_(request, comment) {
  var subject = '[貯蔵品購入申請] 差戻し ' + request.requestId;
  var body = [
    request.applicantName + ' 様',
    '',
    '貯蔵品購入申請が差戻しされました。',
    '',
    '申請番号: ' + request.requestId,
    'コメント: ' + comment,
    '',
    getRequestUrl_(request.requestId)
  ].join('\n');

  sendEmail_(request.applicantEmail, subject, body);
}

function sendCompletedEmail_(request) {
  var subject = '[貯蔵品購入申請] 完了 ' + request.requestId;
  var body = [
    request.applicantName + ' 様',
    '',
    '貯蔵品購入申請が完了しました。',
    '',
    '申請番号: ' + request.requestId,
    '確定金額: ' + amountText_(request),
    request.pdfUrl ? 'PDF: ' + request.pdfUrl : '',
    '',
    getRequestUrl_(request.requestId)
  ].filter(Boolean).join('\n');

  sendEmail_(request.applicantEmail, subject, body);
}

function buildGeneralAffairsBody_(request, heading) {
  var clientRequest = toClientRequest_(request);
  return [
    salutationForStep_(request, STEPS.GENERAL_MANAGER, null),
    '',
    heading,
    '',
    '申請番号: ' + request.requestId,
    '申請者: ' + request.applicantName + '（' + request.applicantEmail + '）',
    '部署: ' + request.department,
    '確定金額: ' + amountText_(request),
    '現在ステップ: ' + clientRequest.currentStepLabel,
    '',
    getRequestUrl_(request.requestId)
  ].join('\n');
}

// 通知宛先マスタの種別ごとに、To / CC を振り分けて1通送信する共通処理。
// 宛先が無ければ fallbackEmail を To に。To が空（CCのみ）の場合は CC を To に昇格して必ず送信先を確保する。
function sendTypedNotification_(type, fallbackEmail, subject, body, pdfFile) {
  var settings = getSettings_();
  if (String(settings.enableEmailNotifications || 'true') === 'false') {
    return;
  }

  var split = getRecipientsSplit_(type);
  var to = split.to.slice();
  var cc = split.cc.slice();

  if (to.length === 0 && cc.length === 0) {
    if (fallbackEmail) {
      to = [fallbackEmail];
    } else {
      return;
    }
  }
  if (to.length === 0) {
    // CC のみ設定のとき、MailApp は To を必須とするため CC を To に昇格。
    to = cc;
    cc = [];
  }

  var options = {
    to: to.join(','),
    subject: subject,
    body: body,
    name: APP.NAME
  };
  if (cc.length > 0) {
    options.cc = cc.join(',');
  }
  if (pdfFile) {
    options.attachments = [pdfFile.getBlob()];
  }
  MailApp.sendEmail(options);
}

function notifyGeneralAffairs_(request, subject, body, pdfFile) {
  sendTypedNotification_(RECIPIENT_TYPES.GENERAL_AFFAIRS, resolveGeneralManagerEmail_(request), subject, body, pdfFile);
}

function sendPurchasingPdfEmail_(request, pdfFile) {
  var subject = '[貯蔵品購入申請] 手配依頼 ' + request.requestId;
  var body = [
    salutationForStep_(request, STEPS.PURCHASING, null),
    '',
    '押印済の申請書PDFを添付します。手配をお願いします。',
    '',
    '申請番号: ' + request.requestId,
    '申請者: ' + request.applicantName + '（' + request.applicantEmail + '）',
    '部署: ' + request.department,
    '確定金額: ' + amountText_(request),
    '',
    getRequestUrl_(request.requestId)
  ].join('\n');
  sendTypedNotification_(RECIPIENT_TYPES.PURCHASING, resolvePurchasingEmail_(request), subject, body, pdfFile);
}

function sendQuoteRequestEmail_(request, pdfFile) {
  var subject = '[貯蔵品購入申請] 見積依頼 ' + request.requestId;
  var body = [
    salutationForStep_(request, STEPS.PURCHASING, null),
    '',
    '見積をお願いします。金額確定後、アプリ内で各明細の単価をご入力ください。',
    '押印済の申請書PDFを添付します。',
    '',
    '申請番号: ' + request.requestId,
    '申請者: ' + request.applicantName + '（' + request.applicantEmail + '）',
    '部署: ' + request.department,
    '確定金額: ' + amountText_(request),
    '',
    getRequestUrl_(request.requestId)
  ].join('\n');
  sendTypedNotification_(RECIPIENT_TYPES.PURCHASING, resolvePurchasingEmail_(request), subject, body, pdfFile);
}

function resolveGeneralManagerEmail_(request) {
  try {
    var rule = findApproverRule_(request.applicantEmail, request.department);
    if (rule && rule.generalManagerEmail) {
      return splitEmails_(rule.generalManagerEmail).join(',');
    }
  } catch (error) {
    // ignore
  }
  return '';
}

function resolvePurchasingEmail_(request) {
  try {
    var rule = findApproverRule_(request.applicantEmail, request.department);
    if (rule && rule.purchasingEmail) {
      return splitEmails_(rule.purchasingEmail).join(',');
    }
  } catch (error) {
    // ignore
  }
  return '';
}

// 確定金額の表記。購買確定前（0）は「未確定（購買確定前）」を返す。
function amountText_(request) {
  return parseNumber_(request.totalAmount) > 0
    ? formatCurrency_(request.totalAmount)
    : '未確定（購買確定前）';
}

function sendEmail_(to, subject, body) {
  var settings = getSettings_();
  if (String(settings.enableEmailNotifications || 'true') === 'false') {
    return;
  }
  if (!to) {
    return;
  }
  MailApp.sendEmail({
    to: to,
    subject: subject,
    body: body,
    name: APP.NAME
  });
}

function getRequestUrl_(requestId) {
  var baseUrl = '';
  try {
    baseUrl = ScriptApp.getService().getUrl();
  } catch (error) {
    baseUrl = '';
  }
  return baseUrl ? baseUrl + '?requestId=' + encodeURIComponent(requestId) : 'WebアプリURLはデプロイ後に有効になります。';
}
