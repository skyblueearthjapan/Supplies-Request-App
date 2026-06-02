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
    '合計金額: ' + formatCurrency_(request.totalAmount),
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
    '合計金額: ' + formatCurrency_(request.totalAmount),
    request.pdfUrl ? 'PDF: ' + request.pdfUrl : '',
    '',
    getRequestUrl_(request.requestId)
  ].filter(Boolean).join('\n');

  sendEmail_(request.applicantEmail, subject, body);
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
