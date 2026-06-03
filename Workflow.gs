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
        // ＋ 上席ステップは部署の上席集合の誰でも承認待ちに含める
        if (normalizeEmail_(request.currentApproverEmail) === user.email) {
          return true;
        }
        if (request.status === STATUS.IN_REVIEW && request.currentStep === STEPS.SUPERVISOR &&
          isSupervisorFor_(user.email, request.applicantEmail, request.department)) {
          return true;
        }
        return admin && isPresidentPendingRow_(request);
      }
      if (mode === 'supervisor') {
        return request.status === STATUS.IN_REVIEW && request.currentStep === STEPS.SUPERVISOR &&
          (admin || normalizeEmail_(request.currentApproverEmail) === user.email ||
            isSupervisorFor_(user.email, request.applicantEmail, request.department));
      }
      if (mode === 'quote') {
        return request.currentStep === STEPS.PURCHASING_QUOTE &&
          (admin || normalizeEmail_(request.currentApproverEmail) === user.email);
      }
      if (mode === 'gm') {
        return request.status === STATUS.IN_REVIEW && request.currentStep === STEPS.GENERAL_MANAGER &&
          (admin || normalizeEmail_(request.currentApproverEmail) === user.email);
      }
      if (mode === 'president') {
        return isPresidentPendingRow_(request) &&
          (admin || normalizeEmail_(request.currentApproverEmail) === user.email);
      }
      if (mode === 'arrange') {
        return request.status === STATUS.IN_REVIEW && request.currentStep === STEPS.PURCHASING &&
          (admin || normalizeEmail_(request.currentApproverEmail) === user.email);
      }
      if (mode === 'all') {
        return admin;
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
    stepTitles: resolveStepTitles_(request, history)
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

  sendApprovalRequestEmail_(updated, nextApprover);
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
    notifyGeneralAffairs_(
      updated,
      '[貯蔵品購入申請] 金額確定 ' + updated.requestId,
      buildGeneralAffairsBody_(updated, '購買が見積金額 ' + formatCurrency_(total) + ' を確定しました。総務部長承認をお願いします。'),
      null
    );
    sendApprovalRequestEmail_(updated, gm);
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
    if (!(parseNumber_(request.totalAmount) > 0)) {
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
    var mineApprove = normalizeEmail_(request.currentApproverEmail) === user.email;
    var mineSupervisor = inReview && request.currentStep === STEPS.SUPERVISOR &&
      isSupervisorFor_(user.email, request.applicantEmail, request.department);
    if (mineApprove || mineSupervisor || (admin && isPres)) {
      pending++;
    }
    if (inReview && request.currentStep === STEPS.SUPERVISOR && (admin || mineApprove || mineSupervisor)) {
      supervisor++;
    }
    if (isQuote && (admin || mineApprove)) {
      quote++;
    }
    if (inReview && request.currentStep === STEPS.GENERAL_MANAGER && (admin || mineApprove)) {
      gm++;
    }
    if (isPres && (admin || mineApprove)) {
      president++;
    }
    if (inReview && request.currentStep === STEPS.PURCHASING && (admin || mineApprove)) {
      arrange++;
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

    var presidentName = request.currentApproverName || STEP_LABELS[STEPS.PRESIDENT];
    var presidentEmail = request.currentApproverEmail;
    var asProxy = normalizeEmail_(presidentEmail) !== user.email;
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
    (isAdmin_(user.email) || normalizeEmail_(request.currentApproverEmail) === user.email);
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

function canRead_(request, user) {
  if (normalizeEmail_(request.applicantEmail) === user.email ||
    normalizeEmail_(request.currentApproverEmail) === user.email ||
    isAdmin_(user.email)) {
    return true;
  }

  if (request.currentStep === STEPS.SUPERVISOR &&
    isSupervisorFor_(user.email, request.applicantEmail, request.department)) {
    return true;
  }

  return readObjects_(SHEETS.HISTORY, HISTORY_COLUMNS).some(function(row) {
    return row.requestId === request.requestId && normalizeEmail_(row.actorEmail) === user.email;
  });
}

function canApprove_(request, user) {
  if (request.status !== STATUS.IN_REVIEW) {
    return false;
  }
  if (request.currentStep === STEPS.SUPERVISOR) {
    // 上席ステップは部署の上席集合の誰でも承認できる。
    return isSupervisorFor_(user.email, request.applicantEmail, request.department) ||
      normalizeEmail_(request.currentApproverEmail) === user.email;
  }
  return normalizeEmail_(request.currentApproverEmail) === user.email;
}

function canEdit_(request, user) {
  return request.status === STATUS.RETURNED &&
    normalizeEmail_(request.applicantEmail) === user.email;
}

function canGeneratePdf_(request, user) {
  return request.status === STATUS.COMPLETED && canRead_(request, user);
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

function getApproverForStep_(rule, step) {
  var map = {};
  map[STEPS.SUPERVISOR] = { email: rule.supervisorEmail, name: rule.supervisorName || '上席' };
  map[STEPS.GENERAL_MANAGER] = { email: rule.generalManagerEmail, name: rule.generalManagerName || '総務部長' };
  map[STEPS.PRESIDENT] = { email: rule.presidentEmail, name: rule.presidentName || '社長' };
  map[STEPS.PURCHASING] = { email: rule.purchasingEmail, name: rule.purchasingName || '購買' };
  map[STEPS.PURCHASING_QUOTE] = { email: rule.purchasingEmail, name: rule.purchasingName || '購買' };

  var approver = map[step];
  if (!approver || !approver.email) {
    throw new Error(STEP_LABELS[step] + 'の承認者メールアドレスが未設定です。');
  }
  approver.email = normalizeEmail_(approver.email);
  return approver;
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
    generalManagerEmail: normalizeEmail_(row.generalManagerEmail),
    generalManagerName: sanitizeText_(row.generalManagerName, 100),
    presidentEmail: normalizeEmail_(row.presidentEmail),
    presidentName: sanitizeText_(row.presidentName, 100),
    purchasingEmail: normalizeEmail_(row.purchasingEmail),
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
