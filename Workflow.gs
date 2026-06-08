function getBootstrap() {
  var user = getCurrentUser_();
  var settings = getSettings_();
  var approvers = readObjects_(SHEETS.APPROVERS, APPROVER_COLUMNS).map(toClientApprover_);
  var admin = isAdmin_(user.email);

  return {
    appName: APP.NAME,
    version: APP.VERSION,
    user: user,
    isAdmin: admin,
    settings: {
      thresholdAmount: getThresholdAmount_(),
      pdfFolderId: settings.pdfFolderId || '',
      adminEmails: settings.adminEmails || '',
      enableEmailNotifications: String(settings.enableEmailNotifications || 'true') !== 'false'
    },
    reasons: REASONS,
    statusLabels: STATUS_LABELS,
    stepLabels: STEP_LABELS,
    actionLabels: ACTION_LABELS,
    approvers: admin ? approvers : [],
    deptSupervisors: admin ? getDeptSupervisors_() : [],
    recipients: admin ? getRecipients_() : [],
    workers: getWorkers_(),
    departments: getDepartments_(approvers),
    myApproverRule: findApproverRule_(user.email, '')
  };
}

function createRequest(payload) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var normalized = normalizeRequestPayload_(payload, user);
    var approverRule = requireApproverRule_(user.email, normalized.department);
    var route = buildRoute_(normalized.totalAmount, approverRule);
    var firstStep = route[0];
    var supervisors = [];
    var firstApprover;
    if (firstStep === STEPS.SUPERVISOR) {
      supervisors = resolveSupervisors_(user.email, normalized.department);
      if (supervisors.length === 0) {
        throw new Error('上席承認者が未設定です。部署別 上席マスタ（または承認者マスタの上席）を設定してください。');
      }
      firstApprover = { email: supervisors[0].email, name: supervisors[0].name };
    } else {
      firstApprover = getApproverForStep_(approverRule, firstStep);
    }
    var now = nowString_();
    var requestId = createId_(APP.REQUEST_ID_PREFIX);

    appendObject_(SHEETS.REQUESTS, {
      requestId: requestId,
      createdAt: now,
      updatedAt: now,
      submittedAt: now,
      completedAt: '',
      applicantEmail: user.email,
      applicantName: normalized.applicantName,
      department: normalized.department,
      requestDate: normalized.requestDate,
      reasonCode: normalized.reasonCode,
      reasonDetail: normalized.reasonDetail,
      totalAmount: normalized.totalAmount,
      status: STATUS.IN_REVIEW,
      currentStep: firstStep,
      currentApproverEmail: firstApprover.email,
      currentApproverName: firstApprover.name,
      routeJson: JSON.stringify(route),
      pdfFileId: '',
      pdfUrl: '',
      version: APP.VERSION
    }, REQUEST_COLUMNS);

    replaceItems_(requestId, normalized.items);
    addHistory_({
      requestId: requestId,
      actorEmail: user.email,
      actorName: normalized.applicantName,
      action: ACTION.SUBMIT,
      toStatus: STATUS.IN_REVIEW,
      toStep: firstStep,
      comment: ''
    });

    var request = getRequestById_(requestId);
    if (firstStep === STEPS.SUPERVISOR) {
      supervisors.forEach(function(s) {
        sendApprovalRequestEmail_(request, s);
      });
    } else {
      sendApprovalRequestEmail_(request, firstApprover);
    }
    return getRequestDetail(requestId);
  } finally {
    lock.releaseLock();
  }
}

function updateRequestDraft(requestId, payload) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var request = requireRequest_(requestId);
    if (normalizeEmail_(request.applicantEmail) !== user.email) {
      throw new Error('申請者のみ修正できます。');
    }
    if (request.status !== STATUS.RETURNED) {
      throw new Error('差戻し中の申請のみ修正できます。');
    }

    var normalized = normalizeRequestPayload_(payload, user);
    var now = nowString_();
    updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', requestId, {
      updatedAt: now,
      applicantName: normalized.applicantName,
      department: normalized.department,
      requestDate: normalized.requestDate,
      reasonCode: normalized.reasonCode,
      reasonDetail: normalized.reasonDetail,
      totalAmount: normalized.totalAmount
    });
    replaceItems_(requestId, normalized.items);
    addHistory_({
      requestId: requestId,
      actorEmail: user.email,
      actorName: normalized.applicantName,
      action: ACTION.UPDATE,
      fromStatus: request.status,
      toStatus: request.status,
      fromStep: request.currentStep,
      toStep: request.currentStep,
      comment: '申請内容を修正'
    });

    return getRequestDetail(requestId);
  } finally {
    lock.releaseLock();
  }
}

function resubmitRequest(requestId) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var request = requireRequest_(requestId);
    if (normalizeEmail_(request.applicantEmail) !== user.email) {
      throw new Error('申請者のみ再申請できます。');
    }
    if (request.status !== STATUS.RETURNED) {
      throw new Error('差戻し中の申請のみ再申請できます。');
    }

    var approverRule = requireApproverRule_(user.email, request.department);
    var route = buildRoute_(0, approverRule);
    var firstStep = route[0];
    var supervisors = [];
    var firstApprover;
    if (firstStep === STEPS.SUPERVISOR) {
      supervisors = resolveSupervisors_(request.applicantEmail, request.department);
      if (supervisors.length === 0) {
        throw new Error('上席承認者が未設定です。部署別 上席マスタ（または承認者マスタの上席）を設定してください。');
      }
      firstApprover = { email: supervisors[0].email, name: supervisors[0].name };
    } else {
      firstApprover = getApproverForStep_(approverRule, firstStep);
    }
    var now = nowString_();
    updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', requestId, {
      updatedAt: now,
      submittedAt: now,
      totalAmount: 0,
      status: STATUS.IN_REVIEW,
      currentStep: firstStep,
      currentApproverEmail: firstApprover.email,
      currentApproverName: firstApprover.name,
      routeJson: JSON.stringify(route),
      pdfFileId: '',
      pdfUrl: ''
    });
    addHistory_({
      requestId: requestId,
      actorEmail: user.email,
      actorName: request.applicantName,
      action: ACTION.RESUBMIT,
      fromStatus: request.status,
      toStatus: STATUS.IN_REVIEW,
      fromStep: request.currentStep,
      toStep: firstStep,
      comment: ''
    });

    var updated = getRequestById_(requestId);
    if (firstStep === STEPS.SUPERVISOR) {
      supervisors.forEach(function(s) {
        sendApprovalRequestEmail_(updated, s);
      });
    } else {
      sendApprovalRequestEmail_(updated, firstApprover);
    }
    return getRequestDetail(requestId);
  } finally {
    lock.releaseLock();
  }
}

function getRequests(filter) {
  var user = getCurrentUser_();
  var input = filter || {};
  var mode = input.mode || 'mine';
  var query = sanitizeText_(input.query, 100).toLowerCase();
  var status = sanitizeText_(input.status, 50);
  var admin = isAdmin_(user.email);

  var rows = readObjects_(SHEETS.REQUESTS, REQUEST_COLUMNS)
    .filter(function(request) {
      if (mode === 'pending') {
        // 自分が承認者の案件 ＋ 社長承認待ち（総務部長/管理者が社長へ提示すべき案件）も残す
        // ＋ 各ステップは承認者集合（上席／購買・総務部長・社長の複数登録）の誰でも承認待ちに含める
        if (isCurrentApprover_(request, user.email)) {
          return true;
        }
        if (request.status === STATUS.IN_REVIEW &&
          isStepApproverFor_(user.email, request.applicantEmail, request.department, request.currentStep)) {
          return true;
        }
        return admin && isPresidentPendingRow_(request);
      }
      // 進捗確認のため、ステップ別一覧・全申請は全アカウントで閲覧可能（読み取り専用）。
      // 承認・金額入力などの操作可否は canApprove_ 等で別途制御されるため、見えても操作はできない。
      if (mode === 'supervisor') {
        return request.status === STATUS.IN_REVIEW && request.currentStep === STEPS.SUPERVISOR;
      }
      if (mode === 'quote') {
        return request.currentStep === STEPS.PURCHASING_QUOTE;
      }
      if (mode === 'gm') {
        return request.status === STATUS.IN_REVIEW && request.currentStep === STEPS.GENERAL_MANAGER;
      }
      if (mode === 'president') {
        return isPresidentPendingRow_(request);
      }
      if (mode === 'arrange') {
        return request.status === STATUS.IN_REVIEW && request.currentStep === STEPS.PURCHASING;
      }
      if (mode === 'all') {
        return true;
      }
      return normalizeEmail_(request.applicantEmail) === user.email;
    })
    .filter(function(request) {
      return !status || request.status === status;
    })
    .filter(function(request) {
      if (!query) {
        return true;
      }
      return [
        request.requestId,
        request.applicantName,
        request.department,
        request.reasonDetail,
        request.currentApproverName
      ].join(' ').toLowerCase().indexOf(query) !== -1;
    })
    .sort(function(a, b) {
      return String(b.updatedAt).localeCompare(String(a.updatedAt));
    })
    .slice(0, 200)
    .map(toClientRequest_);

  return rows;
}

function getRequestDetail(requestId) {
  var user = getCurrentUser_();
  var request = requireRequest_(requestId);
  assertReadable_(request, user);

  var items = readObjects_(SHEETS.ITEMS, ITEM_COLUMNS)
    .filter(function(item) {
      return item.requestId === requestId;
    })
    .sort(function(a, b) {
      return parseNumber_(a.lineNo) - parseNumber_(b.lineNo);
    });

  var history = readObjects_(SHEETS.HISTORY, HISTORY_COLUMNS)
    .filter(function(row) {
      return row.requestId === requestId;
    })
    .sort(function(a, b) {
      return String(a.happenedAt).localeCompare(String(b.happenedAt));
    })
    .map(function(row) {
      row.actionLabel = ACTION_LABELS[row.action] || row.action;
      row.fromStepLabel = STEP_LABELS[row.fromStep] || row.fromStep;
      row.toStepLabel = STEP_LABELS[row.toStep] || row.toStep;
      row.fromStatusLabel = STATUS_LABELS[row.fromStatus] || row.fromStatus;
      row.toStatusLabel = STATUS_LABELS[row.toStatus] || row.toStatus;
      return row;
    });

  return {
    request: toClientRequest_(request),
    items: items,
    history: history,
    permissions: {
      canApprove: canApprove_(request, user),
      canReturn: canApprove_(request, user),
      canEdit: canEdit_(request, user),
      canGeneratePdf: canGeneratePdf_(request, user),
      canPresident: canPresident_(request, user),
      canTabletApprove: canApprove_(request, user),
      canQuote: canApprove_(request, user) && request.currentStep === STEPS.PURCHASING_QUOTE,
      canArrange: canApprove_(request, user) && request.currentStep === STEPS.PURCHASING
    },
    stepTitles: resolveStepTitles_(request, history),
    purchasingStampName: stripRoleParen_(roleNameTitle_(request.applicantEmail, request.department, STEPS.PURCHASING).name)
  };
}

function approveRequest(requestId, comment) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var request = requireRequest_(requestId);
    if (!canApprove_(request, user)) {
      throw new Error('現在の承認者のみ承認できます。');
    }
    // 購買(見積)は金額入力が必須のため confirmQuote 経由のみ許可する。
    if (request.currentStep === STEPS.PURCHASING_QUOTE) {
      throw new Error('購買（見積）ステップでは見積金額を入力してください。');
    }
    // 購買(手配)は arrangeComplete 経由のみ許可する。
    if (request.currentStep === STEPS.PURCHASING) {
      throw new Error('購買ステップでは手配完了を実行してください。');
    }

    var route = jsonParse_(request.routeJson, []);
    var currentIndex = route.indexOf(request.currentStep);
    if (currentIndex === -1) {
      throw new Error('承認経路が不正です。管理者に確認してください。');
    }

    var curKey = request.currentStep;
    var now = nowString_();
    var nextStep = route[currentIndex + 1] || STEPS.DONE;
    var patch = {
      updatedAt: now
    };

    var nextApprover = { email: '', name: '' };
    if (nextStep === STEPS.DONE) {
      patch.status = STATUS.COMPLETED;
      patch.currentStep = STEPS.DONE;
      patch.currentApproverEmail = '';
      patch.currentApproverName = '';
      patch.completedAt = now;
    } else {
      var approverRule = requireApproverRule_(request.applicantEmail, request.department);
      nextApprover = getApproverForStep_(approverRule, nextStep);
      patch.status = STATUS.IN_REVIEW;
      patch.currentStep = nextStep;
      patch.currentApproverEmail = nextApprover.email;
      patch.currentApproverName = nextApprover.name;
    }

    updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', requestId, patch);
    addHistory_({
      requestId: requestId,
      actorEmail: user.email,
      actorName: user.name,
      action: nextStep === STEPS.DONE ? ACTION.COMPLETE : ACTION.APPROVE,
      fromStatus: request.status,
      toStatus: patch.status,
      fromStep: request.currentStep,
      toStep: patch.currentStep,
      comment: sanitizeText_(comment, 1000)
    });

    var updated = getRequestById_(requestId);
    dispatchApprovalNotifications_(updated, curKey, nextStep, nextApprover, user);

    return getRequestDetail(requestId);
  } finally {
    lock.releaseLock();
  }
}

// 承認後の通知振り分け（approveRequest と recordPresidentDecision の承認パスで共用）
// curKey: 今回承認したステップ / nextStep: 遷移先 / nextApprover: 次承認者
function dispatchApprovalNotifications_(updated, curKey, nextStep, nextApprover, user) {
  if (nextStep === STEPS.DONE) {
    completeAndNotify_(updated, user);
    return;
  }

  if (nextStep === STEPS.PURCHASING_QUOTE) {
    // 上席承認後、購買(見積)ステップへ。押印済PDFを生成し購買へ見積依頼を送付。
    var quoteFile = createRequestPdfInternal_(updated.requestId, user);
    updated = getRequestById_(updated.requestId);
    sendQuoteRequestEmail_(updated, quoteFile);
    return;
  }

  if (nextStep === STEPS.PURCHASING) {
    // 購買(手配)ステップに入った（総務部長または社長の承認後）。押印済PDFを生成し購買へ送付。
    var purchasingFile = createRequestPdfInternal_(updated.requestId, user);
    updated = getRequestById_(updated.requestId);
    sendPurchasingPdfEmail_(updated, purchasingFile);
    return;
  }

  // 次ステップが複数登録（総務部長/社長など）の場合は全員へ承認依頼を送る。
  var ruleForNext = null;
  try {
    ruleForNext = findApproverRule_(updated.applicantEmail, updated.department);
  } catch (ruleError) {
    ruleForNext = null;
  }
  var members = resolveStepApproversWithStar_(ruleForNext, nextStep);
  if (members.length > 0) {
    members.forEach(function(member) {
      sendApprovalRequestEmail_(updated, member);
    });
  } else {
    sendApprovalRequestEmail_(updated, nextApprover);
  }
}

// 手配完了 → 完了 時の副作用（PDF再生成・総務部通知・申請者完了メール）。
// dispatchApprovalNotifications_ の DONE 分岐と arrangeComplete の完了分岐で共用する。
function completeAndNotify_(updated, user) {
  var doneFile = createRequestPdfInternal_(updated.requestId, user);
  updated = getRequestById_(updated.requestId);
  var doneBody = buildGeneralAffairsBody_(updated, '貯蔵品購入申請の手配が完了しました。');
  notifyGeneralAffairs_(updated, '[貯蔵品購入申請] 手配完了 ' + updated.requestId, doneBody, doneFile);
  // 申請者へも完了を通知（従来挙動を維持）。
  sendCompletedEmail_(updated);
}

// 購買(見積)ステップの操作。各明細の単価を入力し金額を確定する。
// 合計が10万円以上なら社長決裁を経路へ追加し、総務部長承認へ進める。
// items = [{ itemId, unitPrice }]
function confirmQuote(requestId, items, comment) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var request = requireRequest_(requestId);
    if (!canApprove_(request, user) || request.currentStep !== STEPS.PURCHASING_QUOTE) {
      throw new Error('購買担当者のみ見積金額を入力できます。');
    }

    var priceMap = {};
    (items || []).forEach(function(input) {
      if (input && input.itemId) {
        priceMap[input.itemId] = parseNumber_(input.unitPrice);
      }
    });

    var existingItems = readObjects_(SHEETS.ITEMS, ITEM_COLUMNS)
      .filter(function(item) {
        return item.requestId === requestId;
      })
      .sort(function(a, b) {
        return parseNumber_(a.lineNo) - parseNumber_(b.lineNo);
      });

    var total = 0;
    var rebuilt = existingItems.map(function(item) {
      var quantity = parseNumber_(item.quantity);
      var unitPrice = Object.prototype.hasOwnProperty.call(priceMap, item.itemId)
        ? priceMap[item.itemId]
        : parseNumber_(item.unitPrice);
      var amount = Math.round(quantity * unitPrice);
      total += amount;
      return {
        name: item.name,
        model: item.model,
        maker: item.maker,
        quantity: quantity,
        unitPrice: unitPrice,
        amount: amount,
        desiredDeliveryDate: item.desiredDeliveryDate,
        note: item.note
      };
    });

    if (!(total > 0)) {
      throw new Error('金額を入力してください。');
    }

    replaceItems_(requestId, rebuilt);

    var threshold = getThresholdAmount_();
    var over = total >= threshold;
    var newRoute = over
      ? [STEPS.SUPERVISOR, STEPS.PURCHASING_QUOTE, STEPS.GENERAL_MANAGER, STEPS.PRESIDENT, STEPS.PURCHASING]
      : [STEPS.SUPERVISOR, STEPS.PURCHASING_QUOTE, STEPS.GENERAL_MANAGER, STEPS.PURCHASING];

    var approverRule = requireApproverRule_(request.applicantEmail, request.department);
    var gm = getApproverForStep_(approverRule, STEPS.GENERAL_MANAGER);
    var cleanComment = sanitizeText_(comment, 1000);
    var now = nowString_();

    updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', requestId, {
      updatedAt: now,
      totalAmount: total,
      routeJson: JSON.stringify(newRoute),
      status: STATUS.IN_REVIEW,
      currentStep: STEPS.GENERAL_MANAGER,
      currentApproverEmail: gm.email,
      currentApproverName: gm.name
    });
    addHistory_({
      requestId: requestId,
      actorEmail: user.email,
      actorName: user.name,
      action: ACTION.QUOTE,
      fromStatus: STATUS.IN_REVIEW,
      toStatus: STATUS.IN_REVIEW,
      fromStep: STEPS.PURCHASING_QUOTE,
      toStep: STEPS.GENERAL_MANAGER,
      comment: ['見積金額 ' + formatCurrency_(total) + (over ? '（10万円以上）' : ''), cleanComment].filter(Boolean).join(' ')
    });

    var updated = getRequestById_(requestId);
    // 10万円以上（社長決裁しきい値以上）は、総務部長へ「要・社長決裁」を強くアピールする。
    var overBanner = [
      '━━━━━━━━━━━━━━━━━━━━━━',
      '🚨🚨🚨  要・社長決裁  🚨🚨🚨',
      '━━━━━━━━━━━━━━━━━━━━━━',
      '確定金額 ' + formatCurrency_(total) + ' は社長決裁しきい値（' + formatCurrency_(threshold) + '）以上です。',
      '総務部長の承認後、必ず【社長の承認】が必要です。至急ご対応ください。',
      '━━━━━━━━━━━━━━━━━━━━━━'
    ].join('\n');
    var gaSubject = over
      ? '【🚨至急・要社長決裁🚨】[貯蔵品購入申請] 金額確定 ' + updated.requestId + '（10万円以上）'
      : '[貯蔵品購入申請] 金額確定 ' + updated.requestId;
    var gaHeading = over
      ? overBanner + '\n\n購買が見積金額 ' + formatCurrency_(total) + ' を確定しました。'
      : '購買が見積金額 ' + formatCurrency_(total) + ' を確定しました。総務部長承認をお願いします。';
    notifyGeneralAffairs_(updated, gaSubject, buildGeneralAffairsBody_(updated, gaHeading), null);
    sendApprovalRequestEmail_(updated, gm, over ? overBanner : '');
    return getRequestDetail(requestId);
  } finally {
    lock.releaseLock();
  }
}

// 購買(見積)ステップの操作（金額確定不要・至急承認）。
// 価格が商社と確定済みの標準品（CO2・酸素・溶接ワイヤー・標準塗装色・標準作動油 等）で、
// 生産停止を避けるため金額確定せずに至急手配したい案件向け。金額は入力せず、
// 経路は通常どおり 総務部長承認→手配。総務部へは通常と区別した「至急承認」メールを送る。
function confirmExpedited(requestId, comment) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var request = requireRequest_(requestId);
    if (!canApprove_(request, user) || request.currentStep !== STEPS.PURCHASING_QUOTE) {
      throw new Error('購買担当者のみ操作できます。');
    }

    // 金額確定不要のため社長決裁は経由しない（総務部長承認→手配）。
    var newRoute = [STEPS.SUPERVISOR, STEPS.PURCHASING_QUOTE, STEPS.GENERAL_MANAGER, STEPS.PURCHASING];
    var approverRule = requireApproverRule_(request.applicantEmail, request.department);
    var gm = getApproverForStep_(approverRule, STEPS.GENERAL_MANAGER);
    var cleanComment = sanitizeText_(comment, 1000);
    var now = nowString_();

    updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', requestId, {
      updatedAt: now,
      amountWaived: 'true',
      routeJson: JSON.stringify(newRoute),
      status: STATUS.IN_REVIEW,
      currentStep: STEPS.GENERAL_MANAGER,
      currentApproverEmail: gm.email,
      currentApproverName: gm.name
    });
    addHistory_({
      requestId: requestId,
      actorEmail: user.email,
      actorName: user.name,
      action: ACTION.EXPEDITE,
      fromStatus: STATUS.IN_REVIEW,
      toStatus: STATUS.IN_REVIEW,
      fromStep: STEPS.PURCHASING_QUOTE,
      toStep: STEPS.GENERAL_MANAGER,
      comment: ['金額確定不要・至急手配を依頼', cleanComment].filter(Boolean).join(' ')
    });

    var updated = getRequestById_(requestId);
    var expediteBanner = [
      '━━━━━━━━━━━━━━━━━━━━━━',
      '⚡⚡⚡ 至急承認のお願い（金額確定不要）⚡⚡⚡',
      '━━━━━━━━━━━━━━━━━━━━━━',
      'この申請は金額確定が不要な標準品（価格は商社と確定済み）です。',
      '生産を止めないため、購買にて至急手配を開始したい案件です。',
      '金額確定は行いません。総務部長の至急承認をお願いします。',
      '━━━━━━━━━━━━━━━━━━━━━━'
    ].join('\n');
    var gaHeading = expediteBanner + '\n\n購買より、金額確定不要・至急手配の申請です。' +
      (cleanComment ? '\n備考: ' + cleanComment : '');
    notifyGeneralAffairs_(
      updated,
      '【⚡至急承認・金額確定不要⚡】[貯蔵品購入申請] ' + updated.requestId,
      buildGeneralAffairsBody_(updated, gaHeading),
      null
    );
    sendApprovalRequestEmail_(updated, gm, expediteBanner, '【⚡至急承認・金額確定不要⚡】');
    return getRequestDetail(requestId);
  } finally {
    lock.releaseLock();
  }
}

// 購買(手配)ステップの操作。承認が揃った案件の手配を完了し、案件をクローズする。
function arrangeComplete(requestId, comment) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var request = requireRequest_(requestId);
    if (!canApprove_(request, user) || request.currentStep !== STEPS.PURCHASING) {
      throw new Error('購買担当者のみ手配完了できます。');
    }
    // 手配完了時点で確定金額が必須（旧フローで見積を経ていない案件の¥0完了・社長決裁迂回を防ぐ）。
    // ただし「金額不要・至急承認」で進めた案件は金額確定不要のため対象外。
    if (!(parseNumber_(request.totalAmount) > 0) && !parseBoolean_(request.amountWaived)) {
      throw new Error('確定金額が未入力です。見積（金額入力）が未完了の案件は、差戻し→再申請してください。');
    }

    var cleanComment = sanitizeText_(comment, 1000);
    var now = nowString_();
    updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', requestId, {
      updatedAt: now,
      status: STATUS.COMPLETED,
      currentStep: STEPS.DONE,
      currentApproverEmail: '',
      currentApproverName: '',
      completedAt: now
    });
    addHistory_({
      requestId: requestId,
      actorEmail: user.email,
      actorName: user.name,
      action: ACTION.COMPLETE,
      fromStatus: STATUS.IN_REVIEW,
      toStatus: STATUS.COMPLETED,
      fromStep: STEPS.PURCHASING,
      toStep: STEPS.DONE,
      comment: cleanComment
    });

    var updated = getRequestById_(requestId);
    completeAndNotify_(updated, user);
    return getRequestDetail(requestId);
  } finally {
    lock.releaseLock();
  }
}

function returnRequest(requestId, comment) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var request = requireRequest_(requestId);
    if (!canApprove_(request, user)) {
      throw new Error('現在の承認者のみ差戻しできます。');
    }

    var cleanComment = sanitizeText_(comment, 1000);
    if (!cleanComment) {
      throw new Error('差戻しコメントを入力してください。');
    }

    updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', requestId, {
      updatedAt: nowString_(),
      status: STATUS.RETURNED,
      currentStep: STEPS.APPLICANT,
      currentApproverEmail: '',
      currentApproverName: ''
    });
    addHistory_({
      requestId: requestId,
      actorEmail: user.email,
      actorName: user.name,
      action: ACTION.RETURN,
      fromStatus: request.status,
      toStatus: STATUS.RETURNED,
      fromStep: request.currentStep,
      toStep: STEPS.APPLICANT,
      comment: cleanComment
    });

    var updated = getRequestById_(requestId);
    sendReturnedEmail_(updated, cleanComment);
    return getRequestDetail(requestId);
  } finally {
    lock.releaseLock();
  }
}

function getTabCounts() {
  var user = getCurrentUser_();
  var admin = isAdmin_(user.email);
  var pending = 0;
  var president = 0;
  var quote = 0;
  var supervisor = 0;
  var gm = 0;
  var arrange = 0;
  readObjects_(SHEETS.REQUESTS, REQUEST_COLUMNS).forEach(function(request) {
    var inReview = request.status === STATUS.IN_REVIEW;
    var isPres = isPresidentPendingRow_(request);
    var isQuote = request.currentStep === STEPS.PURCHASING_QUOTE;
    // ステップ別の件数は全アカウント共通（一覧が全員に見えるため、バッジも全体件数で揃える）。
    if (inReview && request.currentStep === STEPS.SUPERVISOR) {
      supervisor++;
    }
    if (isQuote) {
      quote++;
    }
    if (inReview && request.currentStep === STEPS.GENERAL_MANAGER) {
      gm++;
    }
    if (isPres) {
      president++;
    }
    if (inReview && request.currentStep === STEPS.PURCHASING) {
      arrange++;
    }
    // pending は「自分が対応すべき件数」（個人別）。タブには未使用だが従来どおり返す。
    var mineApprove = isCurrentApprover_(request, user.email);
    var mineMember = (inReview || isPres) &&
      isStepApproverFor_(user.email, request.applicantEmail, request.department, request.currentStep);
    if (mineApprove || mineMember || (admin && isPres)) {
      pending++;
    }
  });
  return { pending: pending, president: president, quote: quote, supervisor: supervisor, gm: gm, arrange: arrange };
}

function recordPresidentDecision(requestId, decision, comment) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var request = requireRequest_(requestId);
    if (!canPresident_(request, user)) {
      throw new Error('社長または管理者（総務部長）のみ社長決裁を記録できます。');
    }

    // 社長が複数登録されている場合、操作者が社長集合の一員なら本人として記録する
    // （代表 currentApproverEmail と異なっていても代理扱いにしない）。社長でない管理者のみ代理。
    var presidentMember = isCurrentApprover_(request, user.email) ||
      isStepApproverFor_(user.email, request.applicantEmail, request.department, STEPS.PRESIDENT);
    var presidentName = presidentMember ? user.name : (request.currentApproverName || STEP_LABELS[STEPS.PRESIDENT]);
    var presidentEmail = presidentMember ? user.email : request.currentApproverEmail;
    var asProxy = !presidentMember;
    var operatorNote = asProxy ? '（' + user.name + 'が社長決裁モードで記録）' : '';
    var cleanComment = sanitizeText_(comment, 1000);
    var now = nowString_();

    if (decision === 'return') {
      if (!cleanComment) {
        throw new Error('差戻し理由を入力してください。');
      }
      updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', requestId, {
        updatedAt: now,
        status: STATUS.RETURNED,
        currentStep: STEPS.APPLICANT,
        currentApproverEmail: '',
        currentApproverName: ''
      });
      addHistory_({
        requestId: requestId,
        actorEmail: presidentEmail,
        actorName: presidentName,
        action: ACTION.RETURN,
        fromStatus: request.status,
        toStatus: STATUS.RETURNED,
        fromStep: STEPS.PRESIDENT,
        toStep: STEPS.APPLICANT,
        comment: [cleanComment, operatorNote].filter(Boolean).join(' ')
      });
      var returned = getRequestById_(requestId);
      sendReturnedEmail_(returned, cleanComment);
      return getRequestDetail(requestId);
    }

    if (decision !== 'approve') {
      throw new Error('不正な操作です。');
    }

    var route = jsonParse_(request.routeJson, []);
    var currentIndex = route.indexOf(STEPS.PRESIDENT);
    if (currentIndex === -1) {
      throw new Error('承認経路が不正です。管理者に確認してください。');
    }

    var nextStep = route[currentIndex + 1] || STEPS.DONE;
    var patch = { updatedAt: now };
    var nextApprover = { email: '', name: '' };
    if (nextStep === STEPS.DONE) {
      patch.status = STATUS.COMPLETED;
      patch.currentStep = STEPS.DONE;
      patch.currentApproverEmail = '';
      patch.currentApproverName = '';
      patch.completedAt = now;
    } else {
      var approverRule = requireApproverRule_(request.applicantEmail, request.department);
      nextApprover = getApproverForStep_(approverRule, nextStep);
      patch.status = STATUS.IN_REVIEW;
      patch.currentStep = nextStep;
      patch.currentApproverEmail = nextApprover.email;
      patch.currentApproverName = nextApprover.name;
    }

    updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', requestId, patch);
    addHistory_({
      requestId: requestId,
      actorEmail: presidentEmail,
      actorName: presidentName,
      action: nextStep === STEPS.DONE ? ACTION.COMPLETE : ACTION.APPROVE,
      fromStatus: request.status,
      toStatus: patch.status,
      fromStep: STEPS.PRESIDENT,
      toStep: patch.currentStep,
      comment: [cleanComment, operatorNote].filter(Boolean).join(' ')
    });

    var updated = getRequestById_(requestId);
    dispatchApprovalNotifications_(updated, STEPS.PRESIDENT, nextStep, nextApprover, user);

    return getRequestDetail(requestId);
  } finally {
    lock.releaseLock();
  }
}

function isPresidentPendingRow_(request) {
  return request.status === STATUS.IN_REVIEW && request.currentStep === STEPS.PRESIDENT;
}

function canPresident_(request, user) {
  return isPresidentPendingRow_(request) &&
    (isAdmin_(user.email) || isCurrentApprover_(request, user.email) ||
      isStepApproverFor_(user.email, request.applicantEmail, request.department, STEPS.PRESIDENT));
}

function saveSettings(input) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    assertAdmin_(user);

    saveSettings_({
      thresholdAmount: String(Math.max(1, parseNumber_(input.thresholdAmount || APP.DEFAULT_THRESHOLD))),
      pdfFolderId: sanitizeText_(input.pdfFolderId, 256),
      adminEmails: splitEmails_(input.adminEmails || '').join(','),
      enableEmailNotifications: String(Boolean(input.enableEmailNotifications))
    });

    return getBootstrap();
  } finally {
    lock.releaseLock();
  }
}

function saveApproverMaster(rows) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    assertAdmin_(user);

    var normalizedRows = (rows || [])
      .map(toServerApprover_)
      .filter(function(row) {
        return row.department || row.applicantEmail;
      });

    var existing = readObjects_(SHEETS.APPROVERS, APPROVER_COLUMNS);
    var sheet = getSheet_(SHEETS.APPROVERS);
    existing
      .sort(function(a, b) {
        return b._rowNumber - a._rowNumber;
      })
      .forEach(function(row) {
        sheet.deleteRow(row._rowNumber);
      });
    invalidateCache_();

    appendRows_(SHEETS.APPROVERS, normalizedRows, APPROVER_COLUMNS);

    return getBootstrap();
  } finally {
    lock.releaseLock();
  }
}

function saveDeptSupervisors(rows) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    assertAdmin_(user);
    saveDeptSupervisors_(rows);
    return getBootstrap();
  } finally {
    lock.releaseLock();
  }
}

function saveNotificationRecipients(rows) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    assertAdmin_(user);
    saveRecipients_(rows);
    return getBootstrap();
  } finally {
    lock.releaseLock();
  }
}

function normalizeRequestPayload_(payload, user) {
  var input = payload || {};
  var requestDate = sanitizeText_(input.requestDate, 20) || todayString_();
  var department = sanitizeText_(input.department, 100);
  var reasonCode = sanitizeText_(input.reasonCode, 1);
  var validReason = REASONS.some(function(reason) {
    return reason.code === reasonCode;
  });

  if (!department) {
    throw new Error('部署を入力してください。');
  }
  if (!validReason) {
    throw new Error('購入理由A-Fを選択してください。');
  }

  var items = (input.items || []).map(function(item) {
    var quantity = parseNumber_(item.quantity);
    // 申請時は価格概念なし。単価は任意（既定0）、金額は常に0で保存する。
    // 金額は購買が confirmQuote で各明細の単価を入力し確定する。
    var unitPrice = parseNumber_(item.unitPrice);
    return {
      name: sanitizeText_(item.name, 200),
      model: sanitizeText_(item.model, 200),
      maker: sanitizeText_(item.maker, 200),
      quantity: quantity,
      unitPrice: unitPrice,
      amount: 0,
      desiredDeliveryDate: sanitizeText_(item.desiredDeliveryDate, 20),
      note: sanitizeText_(item.note, 500)
    };
  }).filter(function(item) {
    return item.name || item.model || item.maker || item.quantity || item.note;
  });

  if (items.length === 0) {
    throw new Error('明細を1行以上入力してください。');
  }

  items.forEach(function(item, index) {
    if (!item.name) {
      throw new Error('明細' + (index + 1) + '行目の品名を入力してください。');
    }
    if (item.quantity <= 0) {
      throw new Error('明細' + (index + 1) + '行目の数量を1以上にしてください。');
    }
  });

  return {
    applicantName: sanitizeText_(input.applicantName, 100) || user.name,
    department: department,
    requestDate: requestDate,
    reasonCode: reasonCode,
    reasonDetail: sanitizeText_(input.reasonDetail, 2000),
    items: items,
    totalAmount: 0
  };
}

function buildRoute_(totalAmount, approverRule) {
  // 申請時は金額未確定。経路は 上席→購買(見積)→総務部長→購買(手配)。
  // 社長決裁は購買が見積金額を入力した時点で 10万円以上の場合に confirmQuote が
  // 総務部長と購買(手配)の間へ追加する。
  var route = [STEPS.SUPERVISOR, STEPS.PURCHASING_QUOTE, STEPS.GENERAL_MANAGER, STEPS.PURCHASING];

  // 上席(SUPERVISOR)の承認者は DeptSupervisors に存在しうる（ApproverMaster.supervisorEmail は空可）。
  // 存在検証は createRequest/resubmitRequest が resolveSupervisors_ で行うため、ここでは検証しない。
  route.forEach(function(step) {
    if (step === STEPS.SUPERVISOR) {
      return;
    }
    getApproverForStep_(approverRule, step);
  });

  return route;
}

function requireRequest_(requestId) {
  var request = getRequestById_(requestId);
  if (!request) {
    throw new Error('申請が見つかりません。');
  }
  return request;
}

function getRequestById_(requestId) {
  return readObjects_(SHEETS.REQUESTS, REQUEST_COLUMNS).find(function(row) {
    return row.requestId === requestId;
  }) || null;
}

function assertReadable_(request, user) {
  if (!canRead_(request, user)) {
    throw new Error('この申請を閲覧する権限がありません。');
  }
}

// 認証済みユーザーは全申請を閲覧できる（進捗確認用・読み取り専用）。
// 承認・金額入力・差戻し等の操作可否は canApprove_ 等で別途制御される。
function canRead_(request, user) {
  return !!(user && user.email);
}

// 申請に直接関与している人か（申請者／現承認者／管理者／現ステップ承認者集合／過去の操作者）。
// PDF再作成など、閲覧より強い操作の可否判定に使う。
function isInvolved_(request, user) {
  if (normalizeEmail_(request.applicantEmail) === user.email ||
    isCurrentApprover_(request, user.email) ||
    isAdmin_(user.email)) {
    return true;
  }
  if (isStepApproverFor_(user.email, request.applicantEmail, request.department, request.currentStep)) {
    return true;
  }
  return readObjects_(SHEETS.HISTORY, HISTORY_COLUMNS).some(function(row) {
    return row.requestId === request.requestId && normalizeEmail_(row.actorEmail) === user.email;
  });
}

// 現在の担当者一致判定。currentApproverEmail は通常は単一メールだが、旧ルートでは
// 複数購買がカンマ連結で保存された案件が存在する。リストとして所属判定することで
// 単一値・複数値の双方を正しく扱い、既存案件も救済する。
function isCurrentApprover_(request, userEmail) {
  var target = normalizeEmail_(userEmail);
  if (!target) {
    return false;
  }
  return splitEmails_(request.currentApproverEmail).indexOf(target) !== -1;
}

function canApprove_(request, user) {
  if (request.status !== STATUS.IN_REVIEW) {
    return false;
  }
  // システム管理者（管理者メールに登録された複数名）は、確認・デバッグ・レビュー・代行のため
  // 進行中の各ステップで操作できる（金額入力・承認・手配完了など）。実行者は履歴に記録される。
  if (isAdmin_(user.email)) {
    return true;
  }
  // 凍結された担当者（currentApproverEmail）一致に加え、現在の承認者マスタ／上席マスタの
  // 集合に含まれる人も承認できる。これにより「1部署に複数人・誰でも対応可」を実現する。
  if (isCurrentApprover_(request, user.email)) {
    return true;
  }
  return isStepApproverFor_(user.email, request.applicantEmail, request.department, request.currentStep);
}

function canEdit_(request, user) {
  return request.status === STATUS.RETURNED &&
    normalizeEmail_(request.applicantEmail) === user.email;
}

function canGeneratePdf_(request, user) {
  return request.status === STATUS.COMPLETED && isInvolved_(request, user);
}

function requireApproverRule_(applicantEmail, department) {
  var rule = findApproverRule_(applicantEmail, department);
  if (!rule) {
    throw new Error('承認者マスタが未設定です。管理画面で部署または申請者の承認者を登録してください。');
  }
  return rule;
}

function findApproverRule_(applicantEmail, department) {
  var normalizedApplicantEmail = normalizeEmail_(applicantEmail);
  var normalizedDepartment = sanitizeText_(department, 100);
  var rows = readObjects_(SHEETS.APPROVERS, APPROVER_COLUMNS)
    .map(toClientApprover_)
    .filter(function(row) {
      return row.active;
    });

  return rows.find(function(row) {
    return row.applicantEmail && row.applicantEmail === normalizedApplicantEmail;
  }) || rows.find(function(row) {
    return normalizedDepartment && row.department === normalizedDepartment && !row.applicantEmail;
  }) || rows.find(function(row) {
    return row.department === '*' && !row.applicantEmail;
  }) || null;
}

// 役職ステップ（購買/総務部長/社長）に対応する承認者マスタのフィールド接頭辞。
// 上席(SUPERVISOR)は DeptSupervisors 管理のため対象外（'' を返す）。
function stepRoleField_(step) {
  if (step === STEPS.GENERAL_MANAGER) { return 'generalManager'; }
  if (step === STEPS.PRESIDENT) { return 'president'; }
  if (step === STEPS.PURCHASING || step === STEPS.PURCHASING_QUOTE) { return 'purchasing'; }
  return '';
}

function stepRoleDefaultName_(step) {
  if (step === STEPS.GENERAL_MANAGER) { return '総務部長'; }
  if (step === STEPS.PRESIDENT) { return '社長'; }
  if (step === STEPS.PURCHASING || step === STEPS.PURCHASING_QUOTE) { return '購買'; }
  return STEP_LABELS[step] || '';
}

// 役職ステップの承認者集合を解決する（購買/総務部長/社長はメール欄に複数人を許可）。
// 戻り値: [{ email, name, title }]。先頭が代表（通知先・currentApproverEmail）。
function resolveStepApprovers_(rule, step) {
  if (!rule) { return []; }
  var field = stepRoleField_(step);
  if (!field) { return []; }
  var name = rule[field + 'Name'] || stepRoleDefaultName_(step);
  var title = rule[field + 'Title'] || '';
  return splitEmails_(rule[field + 'Email']).map(function(email) {
    return { email: email, name: name, title: title };
  });
}

// 全社デフォルト '*' ルール（申請者個別指定なし）を返す。
function findStarRule_() {
  return readObjects_(SHEETS.APPROVERS, APPROVER_COLUMNS)
    .map(toClientApprover_)
    .filter(function(row) {
      return row.active;
    })
    .find(function(row) {
      return row.department === '*' && !row.applicantEmail;
    }) || null;
}

// 役職ステップの承認者集合を解決し、部署別ルールに該当役職が無ければ '*' にフォールバックする。
// これにより「購買は全社共通（*）で1回設定」すれば、部署別行で購買を空にしていても通る。
function resolveStepApproversWithStar_(rule, step) {
  var approvers = resolveStepApprovers_(rule, step);
  if (approvers.length > 0) {
    return approvers;
  }
  if (!rule || rule.department !== '*') {
    var star = findStarRule_();
    if (star) {
      return resolveStepApprovers_(star, step);
    }
  }
  return approvers;
}

// 指定メールが、その案件の現ステップ承認者集合に含まれるか。
// 上席は DeptSupervisors、購買/総務部長/社長は承認者マスタの複数メールで判定する。
// 凍結された currentApproverEmail ではなく現在のマスタを参照するため、後からマスタに
// 追加した担当者も既存案件を処理できる。
function isStepApproverFor_(email, applicantEmail, department, step) {
  if (step === STEPS.SUPERVISOR) {
    return isSupervisorFor_(email, applicantEmail, department);
  }
  var field = stepRoleField_(step);
  if (!field) { return false; }
  var target = normalizeEmail_(email);
  if (!target) { return false; }
  var rule = findApproverRule_(applicantEmail, department);
  return resolveStepApproversWithStar_(rule, step).some(function(approver) {
    return approver.email === target;
  });
}

function getApproverForStep_(rule, step) {
  if (step === STEPS.SUPERVISOR) {
    var supervisor = { email: normalizeEmail_(rule.supervisorEmail), name: rule.supervisorName || '上席' };
    if (!supervisor.email) {
      throw new Error(STEP_LABELS[step] + 'の承認者メールアドレスが未設定です。');
    }
    return supervisor;
  }
  // 複数登録時は先頭を代表として currentApproverEmail / 通知の既定にする。
  // 部署別ルールに該当役職が無ければ '*' にフォールバックする。
  var approvers = resolveStepApproversWithStar_(rule, step);
  if (approvers.length === 0 || !approvers[0].email) {
    throw new Error(STEP_LABELS[step] + 'の承認者メールアドレスが未設定です。');
  }
  return { email: approvers[0].email, name: approvers[0].name, title: approvers[0].title };
}

function getDepartments_(approvers) {
  var seen = {};
  return approvers
    .map(function(row) {
      return row.department;
    })
    .filter(function(department) {
      if (!department || seen[department]) {
        return false;
      }
      seen[department] = true;
      return true;
    });
}

function toClientApprover_(row) {
  return {
    department: sanitizeText_(row.department, 100),
    applicantEmail: normalizeEmail_(row.applicantEmail),
    applicantName: sanitizeText_(row.applicantName, 100),
    supervisorEmail: normalizeEmail_(row.supervisorEmail),
    supervisorName: sanitizeText_(row.supervisorName, 100),
    generalManagerEmail: normalizeEmailList_(row.generalManagerEmail),
    generalManagerName: sanitizeText_(row.generalManagerName, 100),
    presidentEmail: normalizeEmailList_(row.presidentEmail),
    presidentName: sanitizeText_(row.presidentName, 100),
    purchasingEmail: normalizeEmailList_(row.purchasingEmail),
    purchasingName: sanitizeText_(row.purchasingName, 100),
    active: row.active === '' ? true : parseBoolean_(row.active),
    supervisorTitle: sanitizeText_(row.supervisorTitle, 40),
    generalManagerTitle: sanitizeText_(row.generalManagerTitle, 40),
    presidentTitle: sanitizeText_(row.presidentTitle, 40),
    purchasingTitle: sanitizeText_(row.purchasingTitle, 40)
  };
}

// 決裁印の下段に出す役職を、申請の承認者ルールから解決する。
// 未設定や該当ルールなしのステップは空（描画側で役割名にフォールバック）。
function resolveStepTitles_(request, history) {
  var titles = {};
  var rule = null;
  try {
    rule = findApproverRule_(request.applicantEmail, request.department);
  } catch (error) {
    rule = null;
  }
  if (rule) {
    titles[STEPS.GENERAL_MANAGER] = rule.generalManagerTitle || '';
    titles[STEPS.PRESIDENT] = rule.presidentTitle || '';
    titles[STEPS.PURCHASING] = rule.purchasingTitle || '';
  }
  // 上席は本人別の役職（DeptSupervisors）を優先。押印者→現承認者の順で解決し、無ければ承認者マスタの上席役職。
  var supEmail = '';
  var hist = history || [];
  for (var i = hist.length - 1; i >= 0; i--) {
    if (hist[i].fromStep === STEPS.SUPERVISOR && (hist[i].action === ACTION.APPROVE || hist[i].action === ACTION.RETURN)) {
      supEmail = hist[i].actorEmail;
      break;
    }
  }
  if (!supEmail && request.currentStep === STEPS.SUPERVISOR) {
    supEmail = request.currentApproverEmail;
  }
  var supTitle = '';
  try {
    supTitle = supEmail ? supervisorTitleByEmail_(supEmail, request.applicantEmail, request.department) : '';
  } catch (error2) {
    supTitle = '';
  }
  titles[STEPS.SUPERVISOR] = supTitle || (rule ? (rule.supervisorTitle || '') : '');
  return titles;
}

function toServerApprover_(row) {
  var client = toClientApprover_(row || {});
  return {
    department: client.department,
    applicantEmail: client.applicantEmail,
    applicantName: client.applicantName,
    supervisorEmail: client.supervisorEmail,
    supervisorName: client.supervisorName,
    generalManagerEmail: client.generalManagerEmail,
    generalManagerName: client.generalManagerName,
    presidentEmail: client.presidentEmail,
    presidentName: client.presidentName,
    purchasingEmail: client.purchasingEmail,
    purchasingName: client.purchasingName,
    active: String(client.active),
    supervisorTitle: client.supervisorTitle,
    generalManagerTitle: client.generalManagerTitle,
    presidentTitle: client.presidentTitle,
    purchasingTitle: client.purchasingTitle
  };
}
