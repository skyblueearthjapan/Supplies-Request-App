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
    var firstApprover = getApproverForStep_(approverRule, firstStep);
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
    sendApprovalRequestEmail_(request, firstApprover);
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
    var route = buildRoute_(parseNumber_(request.totalAmount), approverRule);
    var firstStep = route[0];
    var firstApprover = getApproverForStep_(approverRule, firstStep);
    var now = nowString_();
    updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', requestId, {
      updatedAt: now,
      submittedAt: now,
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
    sendApprovalRequestEmail_(updated, firstApprover);
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
        if (normalizeEmail_(request.currentApproverEmail) === user.email) {
          return true;
        }
        return admin && isPresidentPendingRow_(request);
      }
      if (mode === 'president') {
        return isPresidentPendingRow_(request) &&
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
      canPresident: canPresident_(request, user)
    }
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

    var route = jsonParse_(request.routeJson, []);
    var currentIndex = route.indexOf(request.currentStep);
    if (currentIndex === -1) {
      throw new Error('承認経路が不正です。管理者に確認してください。');
    }

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
    if (nextStep === STEPS.DONE) {
      createRequestPdfInternal_(requestId, user);
      updated = getRequestById_(requestId);
      sendCompletedEmail_(updated);
    } else {
      sendApprovalRequestEmail_(updated, nextApprover);
    }

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
  readObjects_(SHEETS.REQUESTS, REQUEST_COLUMNS).forEach(function(request) {
    var isPres = isPresidentPendingRow_(request);
    var mineApprove = normalizeEmail_(request.currentApproverEmail) === user.email;
    if (mineApprove || (admin && isPres)) {
      pending++;
    }
    if (isPres && (admin || mineApprove)) {
      president++;
    }
  });
  return { pending: pending, president: president };
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
    if (nextStep === STEPS.DONE) {
      createRequestPdfInternal_(requestId, user);
      updated = getRequestById_(requestId);
      sendCompletedEmail_(updated);
    } else {
      sendApprovalRequestEmail_(updated, nextApprover);
    }

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

    normalizedRows.forEach(function(row) {
      appendObject_(SHEETS.APPROVERS, row, APPROVER_COLUMNS);
    });

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
    var unitPrice = parseNumber_(item.unitPrice);
    var amount = Math.round(quantity * unitPrice);
    return {
      name: sanitizeText_(item.name, 200),
      model: sanitizeText_(item.model, 200),
      maker: sanitizeText_(item.maker, 200),
      quantity: quantity,
      unitPrice: unitPrice,
      amount: amount,
      desiredDeliveryDate: sanitizeText_(item.desiredDeliveryDate, 20),
      note: sanitizeText_(item.note, 500)
    };
  }).filter(function(item) {
    return item.name || item.model || item.maker || item.quantity || item.unitPrice || item.note;
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
    if (item.unitPrice < 0) {
      throw new Error('明細' + (index + 1) + '行目の単価が不正です。');
    }
  });

  return {
    applicantName: sanitizeText_(input.applicantName, 100) || user.name,
    department: department,
    requestDate: requestDate,
    reasonCode: reasonCode,
    reasonDetail: sanitizeText_(input.reasonDetail, 2000),
    items: items,
    totalAmount: items.reduce(function(total, item) {
      return total + item.amount;
    }, 0)
  };
}

function buildRoute_(totalAmount, approverRule) {
  var route = [STEPS.SUPERVISOR, STEPS.GENERAL_MANAGER];
  if (parseNumber_(totalAmount) > getThresholdAmount_()) {
    route.push(STEPS.PRESIDENT);
  }
  route.push(STEPS.PURCHASING);

  route.forEach(function(step) {
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

  return readObjects_(SHEETS.HISTORY, HISTORY_COLUMNS).some(function(row) {
    return row.requestId === request.requestId && normalizeEmail_(row.actorEmail) === user.email;
  });
}

function canApprove_(request, user) {
  return request.status === STATUS.IN_REVIEW &&
    normalizeEmail_(request.currentApproverEmail) === user.email;
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
  map[STEPS.SUPERVISOR] = { email: rule.supervisorEmail, name: rule.supervisorName || '上席者' };
  map[STEPS.GENERAL_MANAGER] = { email: rule.generalManagerEmail, name: rule.generalManagerName || '総務部長' };
  map[STEPS.PRESIDENT] = { email: rule.presidentEmail, name: rule.presidentName || '社長' };
  map[STEPS.PURCHASING] = { email: rule.purchasingEmail, name: rule.purchasingName || '購買' };

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
    active: row.active === '' ? true : parseBoolean_(row.active)
  };
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
    active: String(client.active)
  };
}
