function sendApprovalRequestEmail_(request, approver) {
  if (!approver || !approver.email) {
    return;
  }
  var clientRequest = toClientRequest_(request);
  var subject = '[貯蔵品購入申請] 承認依頼 ' + request.requestId;
  var body = [
    approver.name + ' 様',
    '',
    '貯蔵品購入申請の承認依頼があります。',
    '',
    '申請番号: ' + request.requestId,
    '申請者: ' + request.applicantName + '（' + request.applicantEmail + '）',
    '部署: ' + request.department,
    '確定金額: ' + amountText_(request),
    '現在ステップ: ' + clientRequest.currentStepLabel,
    '',
    getRequestUrl_(request.requestId)
  ].join('\n');

  sendEmail_(approver.email, subject, body);
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
    '総務部 ご担当者 様',
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

function notifyGeneralAffairs_(request, subject, body, pdfFile) {
  var settings = getSettings_();
  if (String(settings.enableEmailNotifications || 'true') === 'false') {
    return;
  }

  var recipients = getRecipientEmails_(RECIPIENT_TYPES.GENERAL_AFFAIRS);
  if (recipients.length === 0) {
    var fallback = resolveGeneralManagerEmail_(request);
    if (fallback) {
      recipients = [fallback];
    }
  }
  if (recipients.length === 0) {
    return;
  }

  var options = {
    to: recipients.join(','),
    subject: subject,
    body: body,
    name: APP.NAME
  };
  if (pdfFile) {
    options.attachments = [pdfFile.getBlob()];
  }
  MailApp.sendEmail(options);
}

function sendPurchasingPdfEmail_(request, pdfFile) {
  var settings = getSettings_();
  if (String(settings.enableEmailNotifications || 'true') === 'false') {
    return;
  }

  var recipients = getRecipientEmails_(RECIPIENT_TYPES.PURCHASING);
  if (recipients.length === 0) {
    var fallback = resolvePurchasingEmail_(request);
    if (fallback) {
      recipients = [fallback];
    }
  }
  if (recipients.length === 0) {
    return;
  }

  var subject = '[貯蔵品購入申請] 手配依頼 ' + request.requestId;
  var body = [
    '購買 ご担当者 様',
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

  var options = {
    to: recipients.join(','),
    subject: subject,
    body: body,
    name: APP.NAME
  };
  if (pdfFile) {
    options.attachments = [pdfFile.getBlob()];
  }
  MailApp.sendEmail(options);
}

function sendQuoteRequestEmail_(request, pdfFile) {
  var settings = getSettings_();
  if (String(settings.enableEmailNotifications || 'true') === 'false') {
    return;
  }

  var recipients = getRecipientEmails_(RECIPIENT_TYPES.PURCHASING);
  if (recipients.length === 0) {
    var fallback = resolvePurchasingEmail_(request);
    if (fallback) {
      recipients = [fallback];
    }
  }
  if (recipients.length === 0) {
    return;
  }

  var subject = '[貯蔵品購入申請] 見積依頼 ' + request.requestId;
  var body = [
    '購買 ご担当者 様',
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

  var options = {
    to: recipients.join(','),
    subject: subject,
    body: body,
    name: APP.NAME
  };
  if (pdfFile) {
    options.attachments = [pdfFile.getBlob()];
  }
  MailApp.sendEmail(options);
}

function resolveGeneralManagerEmail_(request) {
  try {
    var rule = findApproverRule_(request.applicantEmail, request.department);
    if (rule && rule.generalManagerEmail) {
      return normalizeEmail_(rule.generalManagerEmail);
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
      return normalizeEmail_(rule.purchasingEmail);
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
