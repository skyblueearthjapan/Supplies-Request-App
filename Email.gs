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
  // 氏名欄に「辻野（総務部長）」のように役職がカッコ付きで入っている場合があるため、
  // 全角・半角のカッコ内を除去してから姓を取り出す（例「総務 辻野（総務部長）部長」を防ぐ）。
  var cleaned = name ? String(name).replace(/[（(][^）)]*[）)]/g, '').trim() : '';
  var surname = cleaned ? cleaned.split(/\s+/)[0] : '';
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

function sendApprovalRequestEmail_(request, approver, extraNote, subjectPrefix) {
  if (!approver || !approver.email) {
    return;
  }
  var clientRequest = toClientRequest_(request);
  var prefix = subjectPrefix || (extraNote ? '【★至急・要社長決裁★】' : '');
  var subject = prefix + '[貯蔵品購入申請] 承認依頼 ' + request.requestId;
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

  // 上席マスタに「通知メール」が設定されていれば、認証メール(To)に加えて Cc で追加通知する。
  var cc = '';
  if (approver.notifyEmail) {
    var notify = normalizeEmail_(approver.notifyEmail);
    if (notify && notify !== normalizeEmail_(approver.email)) {
      cc = notify;
    }
  }
  sendEmail_(approver.email, subject, lines.join('\n'), cc);
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

// 通知宛先マスタの有効な全宛先を、種別横断で To / CC に振り分けて返す。
// 各行の送信区分（sendAs）に従う。同一メールが To と CC の双方に登場する場合は To を優先し、
// CC からは除外する（登録順に依らず To が勝つよう2パスで判定）。
function getAllRecipientsSplit_() {
  var rows = getRecipients_().filter(function(row) {
    return row.active && row.email;
  });
  var toSeen = {};
  var to = [];
  rows.forEach(function(row) {
    if (row.sendAs !== 'CC' && !toSeen[row.email]) {
      toSeen[row.email] = true;
      to.push(row.email);
    }
  });
  var ccSeen = {};
  var cc = [];
  rows.forEach(function(row) {
    if (row.sendAs === 'CC' && !toSeen[row.email] && !ccSeen[row.email]) {
      ccSeen[row.email] = true;
      cc.push(row.email);
    }
  });
  return { to: to, cc: cc };
}

// 申請明細を行番号順で取得する。
function getRequestItems_(requestId) {
  return readObjects_(SHEETS.ITEMS, ITEM_COLUMNS)
    .filter(function(item) {
      return item.requestId === requestId;
    })
    .sort(function(a, b) {
      return parseNumber_(a.lineNo) - parseNumber_(b.lineNo);
    });
}

// 差戻しになった「段階」を分かりやすい語に変換する。
function returnedStagePhrase_(step) {
  if (step === STEPS.SUPERVISOR) { return '上席承認'; }
  if (step === STEPS.PURCHASING_QUOTE) { return '購買（見積・金額入力）'; }
  if (step === STEPS.GENERAL_MANAGER) { return '総務部長承認'; }
  if (step === STEPS.PRESIDENT) { return '社長決裁'; }
  if (step === STEPS.PURCHASING) { return '購買手配'; }
  return STEP_LABELS[step] || String(step || '');
}

// 明細の金額表記。購買確定前（0）は「未定」。
function itemMoneyText_(value) {
  return parseNumber_(value) > 0 ? formatCurrency_(value) : '未定';
}

// 差戻し時、通知宛先マスタの全宛先へ「誰の・どの案件が・どの段階で差戻しになったか」を明細付きで一斉共有する。
// stepAtReturn は差戻し時点のステップ（申請者へ戻す前の currentStep）を渡すこと。
function sendReturnedBroadcast_(request, stepAtReturn, comment, actorName) {
  var settings = getSettings_();
  if (String(settings.enableEmailNotifications || 'true') === 'false') {
    return;
  }
  // 通知宛先マスタの送信区分どおり To / CC で送る。
  // 申請者には個別の差戻しメールが届くため、一斉共有の宛先からは除外する。
  var applicant = normalizeEmail_(request.applicantEmail);
  var split = getAllRecipientsSplit_();
  var to = split.to.filter(function(email) { return email !== applicant; });
  var cc = split.cc.filter(function(email) { return email !== applicant; });
  if (to.length === 0 && cc.length === 0) {
    return;
  }
  if (to.length === 0) {
    // CC のみのとき、MailApp は To を必須とするため CC を To に昇格。
    to = cc;
    cc = [];
  }

  var applicantName = request.applicantName || '（不明）';
  var stage = returnedStagePhrase_(stepAtReturn);
  var items = getRequestItems_(request.requestId);
  var subject = '[貯蔵品購入申請] 差戻し共有 ' + request.requestId;

  var lines = [];
  lines.push(applicantName + ' さんが申請した貯蔵品購入申請が、' + stage + 'の段階で差戻しになりました。');
  lines.push('');
  lines.push('申請番号: ' + request.requestId);
  lines.push('申請者　: ' + applicantName + '（' + request.applicantEmail + '）');
  lines.push('部署　　: ' + (request.department || '（未設定）'));
  lines.push('差戻し段階: ' + stage);
  if (actorName) {
    lines.push('差戻し者: ' + actorName);
  }
  lines.push('理由　　: ' + (comment || '（コメントなし）'));
  lines.push('確定金額: ' + amountText_(request));
  lines.push('');
  lines.push('― 申請明細（品名／型式／数量／単価(税抜)／金額(税抜)） ―');
  if (items.length === 0) {
    lines.push('（明細なし）');
  } else {
    items.forEach(function(it, index) {
      lines.push(
        (index + 1) + '. ' + it.name +
        '／型式: ' + (it.model || '—') +
        '／数量: ' + (parseNumber_(it.quantity) || 0) +
        '／単価: ' + itemMoneyText_(it.unitPrice) +
        '／金額: ' + itemMoneyText_(it.amount)
      );
    });
  }
  var breakdown = amountBreakdownLines_(request);
  if (breakdown.length > 0) {
    lines.push('');
    breakdown.forEach(function(line) { lines.push(line); });
  }
  lines.push('');
  lines.push(getRequestUrl_(request.requestId));

  var options = {
    to: to.join(','),
    subject: subject,
    body: lines.join('\n'),
    name: APP.NAME
  };
  if (cc.length > 0) {
    options.cc = cc.join(',');
  }
  MailApp.sendEmail(options);
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

// 手配依頼・見積依頼メールの宛名。品目区分の主担当購買（TO 宛先）の表示名をそのまま使う。
// 「様」等の敬称は表示名側で付与する運用のため付けない。CC 宛先は宛名に含めない。
// 複数 TO は改行で並べ、表示名付き TO が無ければ従来の汎用宛名にフォールバック。
function purchasingSalutation_(request) {
  var roles = purchasingTypesForCategory_(request.category);
  var names = getRecipientToNames_(roles.primary);
  if (names.length === 0) {
    return approverSalutation_('', '', stepRoleSalutationLabel_(STEPS.PURCHASING));
  }
  return names.join('\n');
}

function sendPurchasingPdfEmail_(request, pdfFile) {
  var subject = '[貯蔵品購入申請] 手配依頼 ' + request.requestId;
  var body = [
    purchasingSalutation_(request),
    '',
    '押印済の申請書PDFを添付します。手配をお願いします。',
    '',
    '申請番号: ' + request.requestId,
    '申請者: ' + request.applicantName + '（' + request.applicantEmail + '）',
    '部署: ' + request.department,
    '品目区分: ' + categoryLabel_(request),
    '確定金額: ' + amountText_(request),
    '',
    getRequestUrl_(request.requestId)
  ].join('\n');
  sendPurchasingByCategory_(request, subject, body, pdfFile);
}

function sendQuoteRequestEmail_(request, pdfFile) {
  var subject = '[貯蔵品購入申請] 見積依頼 ' + request.requestId;
  var body = [
    purchasingSalutation_(request),
    '',
    '見積をお願いします。金額確定後、アプリ内で各明細の単価をご入力ください。',
    '押印済の申請書PDFを添付します。',
    '',
    '申請番号: ' + request.requestId,
    '申請者: ' + request.applicantName + '（' + request.applicantEmail + '）',
    '部署: ' + request.department,
    '品目区分: ' + categoryLabel_(request),
    '確定金額: ' + amountText_(request),
    '',
    getRequestUrl_(request.requestId)
  ].join('\n');
  sendPurchasingByCategory_(request, subject, body, pdfFile);
}

function categoryLabel_(request) {
  return CATEGORY_LABELS[normalizeCategory_(request.category)];
}

// 品目区分に応じて主担当購買（TO）ともう一方の購買（CC）を決める。
// 電気 → 電気購買が主担当、メカ／一般・不明 → メカ購買（一般兼用）が主担当。
function purchasingTypesForCategory_(category) {
  if (normalizeCategory_(category) === CATEGORIES.ELEC) {
    return { primary: RECIPIENT_TYPES.PURCHASING_ELEC, secondary: RECIPIENT_TYPES.PURCHASING };
  }
  return { primary: RECIPIENT_TYPES.PURCHASING, secondary: RECIPIENT_TYPES.PURCHASING_ELEC };
}

// 種別ごとのフォールバック宛先（通知宛先マスタが空のとき承認者マスタを参照）。
function purchasingFallbackByType_(type, request) {
  return type === RECIPIENT_TYPES.PURCHASING_ELEC
    ? resolvePurchasingElecEmail_(request)
    : resolvePurchasingEmail_(request);
}

// 見積依頼・手配依頼メールを品目区分で振り分けて1通送る。
// TO  = 主担当購買（区分の担当）の To 宛先（無ければ承認者マスタの該当購買メールにフォールバック、
//       それも無く CC のみなら CC を To に昇格）。
// CC  = 主担当の CC 宛先 ＋ もう一方の購買（共有用、無ければ承認者マスタにフォールバック）。
//       To と重複する宛先は除外する。
function sendPurchasingByCategory_(request, subject, body, pdfFile) {
  var settings = getSettings_();
  if (String(settings.enableEmailNotifications || 'true') === 'false') {
    return;
  }

  var roles = purchasingTypesForCategory_(request.category);
  var primarySplit = getRecipientsSplit_(roles.primary);
  var to = primarySplit.to.slice();
  var cc = primarySplit.cc.slice();

  if (to.length === 0) {
    var fb = splitEmails_(purchasingFallbackByType_(roles.primary, request));
    if (fb.length > 0) {
      to = fb;
    } else if (cc.length > 0) {
      // 主担当が CC のみ設定のとき、To を確保するため CC を昇格。
      to = cc;
      cc = [];
    }
  }

  // もう一方の購買は共有のため必ず CC へ（通知宛先が無ければ承認者マスタにフォールバック）。
  var secondaryEmails = getRecipientEmails_(roles.secondary);
  if (secondaryEmails.length === 0) {
    secondaryEmails = splitEmails_(purchasingFallbackByType_(roles.secondary, request));
  }

  var seen = {};
  to.forEach(function(email) { seen[email] = true; });
  var mergedCc = [];
  cc.concat(secondaryEmails).forEach(function(email) {
    if (email && !seen[email]) {
      seen[email] = true;
      mergedCc.push(email);
    }
  });

  if (to.length === 0 && mergedCc.length === 0) {
    return;
  }
  if (to.length === 0) {
    to = mergedCc;
    mergedCc = [];
  }

  var options = {
    to: to.join(','),
    subject: subject,
    body: body,
    name: APP.NAME
  };
  if (mergedCc.length > 0) {
    options.cc = mergedCc.join(',');
  }
  if (pdfFile) {
    options.attachments = [pdfFile.getBlob()];
  }
  MailApp.sendEmail(options);
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

function resolvePurchasingElecEmail_(request) {
  try {
    var rule = findApproverRule_(request.applicantEmail, request.department);
    if (rule && rule.purchasingElecEmail) {
      return splitEmails_(rule.purchasingElecEmail).join(',');
    }
  } catch (error) {
    // ignore
  }
  return '';
}

// 確定金額の表記。totalAmount は税抜のため、税込に換算して表記する。
// 購買確定前（0）は「未確定（購買確定前）」を返す。
function amountText_(request) {
  var net = parseNumber_(request.totalAmount);
  if (net > 0) {
    return formatCurrency_(grossAmount_(net)) + '（税込）';
  }
  if (parseBoolean_(request.amountWaived)) {
    return '金額確定不要（至急手配）';
  }
  return '未確定（購買確定前）';
}

// 金額内訳（小計・消費税・合計）の3行。確定前・金額免除の場合は空配列。
function amountBreakdownLines_(request) {
  var net = parseNumber_(request.totalAmount);
  if (!(net > 0)) {
    return [];
  }
  return [
    '小計（税抜）　: ' + formatCurrency_(net),
    '消費税（10%） : ' + formatCurrency_(taxAmount_(net)),
    '合計（税込）　: ' + formatCurrency_(grossAmount_(net))
  ];
}

function sendEmail_(to, subject, body, cc) {
  var settings = getSettings_();
  if (String(settings.enableEmailNotifications || 'true') === 'false') {
    return;
  }
  if (!to) {
    return;
  }
  var options = {
    to: to,
    subject: subject,
    body: body,
    name: APP.NAME
  };
  if (cc) {
    options.cc = cc;
  }
  MailApp.sendEmail(options);
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
