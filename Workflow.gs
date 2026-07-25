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
      enableEmailNotifications: emailNotificationsEnabled_()
    },
    reasons: REASONS,
    unitOptions: UNIT_OPTIONS,
    defaultUnit: DEFAULT_UNIT,
    unitOther: UNIT_OTHER,
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
      category: normalized.category,
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
    // 差戻しプールの編集は認証済みユーザーなら誰でも可（申請者本人に限らない）。
    // 「保存」は差戻し状態のまま内容だけ更新する（再提出は別操作 resubmitRequest）。
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
      totalAmount: normalized.totalAmount,
      category: normalized.category
    });
    replaceItems_(requestId, normalized.items);
    addHistory_({
      requestId: requestId,
      actorEmail: user.email,
      actorName: user.name,
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

// 承認前（購買の金額入力より前）に、認証済みユーザー（申請者本人に限らず購買担当や他の人も可）が
// 申請明細（型式・品名など非金額項目）だけを修正する。差戻し→再申請（updateRequestDraft +
// resubmitRequest）と異なり、経路・ステップ・承認済みの状態は一切変えない（＝軽微な記入ミス修正）。
// 変更内容は「実際に編集した人」を操作者として履歴に旧→新で残し、現在の承認者へ通知する。
// 編集可否は canEditInReview_ が判定する（ロック内で最新行を再読込して確認）。
function updateRequestInReview(requestId, payload) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var request = requireRequest_(requestId);
    if (!canEditInReview_(request, user)) {
      throw new Error('この申請は現在修正できません。承認中かつ購買が金額を入力する前の申請のみ、明細を修正できます。');
    }

    var oldItems = readObjects_(SHEETS.ITEMS, ITEM_COLUMNS)
      .filter(function(item) {
        return item.requestId === requestId;
      })
      .sort(function(a, b) {
        return parseNumber_(a.lineNo) - parseNumber_(b.lineNo);
      });

    var newItems = normalizeEditableItems_(payload);
    var summary = describeItemChanges_(oldItems, newItems);
    // 実質的な変更が無ければ何も書き込まない（明細の再挿入・updatedAt更新・履歴・通知を行わない）。
    if (!summary) {
      return getRequestDetail(requestId);
    }

    var now = nowString_();
    replaceItems_(requestId, newItems);
    // normalizeEditableItems_ が明細の単価・金額を0に落とすため、申請ヘッダの合計も0へ揃える。
    // （recallStep で購買見積へ戻した案件は totalAmount が残っており、揃えないと
    //   「明細合計0なのに合計金額に残骸が出る」不整合になる。合計＝明細金額の総和を保つ）
    updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', requestId, {
      updatedAt: now,
      totalAmount: 0
    });

    addHistory_({
      requestId: requestId,
      actorEmail: user.email,
      actorName: user.name,
      action: ACTION.UPDATE,
      fromStatus: request.status,
      toStatus: request.status,
      fromStep: request.currentStep,
      toStep: request.currentStep,
      comment: summary
    });

    // 変更後の最新行で現承認者（上席ステップは全上席）へ通知する。編集者自身は宛先から除外する。
    sendRequestUpdatedEmail_(getRequestById_(requestId), summary, user.name, user.email);

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
    // 差戻しプールからの再提出は認証済みユーザーなら誰でも可（申請者本人に限らない）。
    if (request.status !== STATUS.RETURNED) {
      throw new Error('差戻し中の申請のみ再申請できます。');
    }

    var approverRule = requireApproverRule_(request.applicantEmail, request.department);
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
      actorName: user.name,
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
      // 取消（論理削除）済は対応系・ステップ別の各セクションから除外する。
      // 申請者本人の「自分の申請(mine)」と「全申請(all)」では取消バッジ付きで残す。
      if (request.status === STATUS.CANCELLED && mode !== 'mine' && mode !== 'all') {
        return false;
      }
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
      // 差戻しプール：差戻し中の申請を全員に表示（編集・再提出・削除は誰でも可）。
      if (mode === 'returned') {
        return request.status === STATUS.RETURNED;
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
      canDelete: canDelete_(request, user),
      canEdit: canEdit_(request, user),
      canResubmit: canResubmit_(request, user),
      canDeletePool: canDeletePool_(request, user),
      canEditInReview: canEditInReview_(request, user),
      canGeneratePdf: canGeneratePdf_(request, user),
      canPresident: canPresident_(request, user),
      canTabletApprove: canApprove_(request, user),
      canQuote: canApprove_(request, user) && request.currentStep === STEPS.PURCHASING_QUOTE,
      canArrange: canApprove_(request, user) && request.currentStep === STEPS.PURCHASING,
      canRecall: canRecall_(request, user),
      canSplitItems: canSplitItems_(request, user, items.length)
    },
    stepTitles: resolveStepTitles_(request, history),
    purchasingStampName: stripRoleParen_(roleNameTitle_(request.applicantEmail, request.department, STEPS.PURCHASING).name)
  };
}

// ===== 明細の部分差戻し（申請の分割） =====
// 選択された明細を「1枚の差戻し済み申請」として切り出し、元申請からは取り除く。
// 差戻しプールは status === 差戻し の申請を並べるだけなので、分離先は通常の差戻し申請として
// そのまま編集・再提出・削除できる（新しい状態もタブも増やさない）。

// 分割可否。分割は承認と同じ操作の中で行うため、承認できる人にだけ開く。
// 明細1件の申請を分割すると残明細が0件になり「明細1行以上」という不変条件を壊すので除外する。
function canSplitItems_(request, user, itemCount) {
  if (request.status !== STATUS.IN_REVIEW) {
    return false;
  }
  if (SPLITTABLE_STEPS.indexOf(request.currentStep) === -1) {
    return false;
  }
  if (!(parseNumber_(itemCount) > 1)) {
    return false;
  }
  return canApprove_(request, user) || canPresident_(request, user);
}

// 選択された明細参照を検証し、移動対象と残留対象に振り分ける（書き込みは一切しない）。
// refs: [{ itemId, lineNo, name }] / 戻り値: { moved: [...], remaining: [...] }（どちらも lineNo 昇順）
//
// itemId・lineNo・品名の3点セットで照合するのは、itemId が永続IDではないため。
// replaceItems_ は明細を全削除して再挿入し itemId を毎回発番し直すので、画面表示から操作までの間に
// 他者が明細を編集すると、同じ itemId が別の明細を指す（あるいは消える）。3点が揃わない限り
// 「画面に映っていた明細」と断定できないため、1件でも外れたら部分実行せず処理全体を中断する。
function resolveSplitSelection_(request, refs) {
  var items = getRequestItems_(request.requestId);

  var claimed = {};
  var moved = [];
  (refs || []).forEach(function(ref) {
    var matched = null;
    for (var i = 0; i < items.length; i++) {
      if (claimed[i]) {
        continue;
      }
      if (String(items[i].itemId) === String(ref && ref.itemId) &&
        String(items[i].lineNo) === String(ref && ref.lineNo) &&
        String(items[i].name) === String(ref && ref.name)) {
        claimed[i] = true;
        matched = items[i];
        break;
      }
    }
    if (!matched) {
      throw new Error('明細の情報が画面と一致しません。他の人が明細を更新した可能性があります。画面を再読込してからやり直してください。');
    }
    moved.push(matched);
  });

  if (moved.length === 0) {
    throw new Error('差し戻す明細を選択してください。');
  }

  var remaining = items.filter(function(item, index) {
    return !claimed[index];
  });
  // 明細0件の申請は normalizeRequestPayload_ 等が禁じている。全件差戻しは申請全体の差戻しで行う。
  if (remaining.length === 0) {
    throw new Error('すべての明細を差し戻す場合は、申請全体の差戻しをご利用ください。');
  }

  moved.sort(function(a, b) {
    return parseNumber_(a.lineNo) - parseNumber_(b.lineNo);
  });
  return { moved: moved, remaining: remaining };
}

// replaceItems_ は明細を全削除して再挿入するため、書き戻す明細は金額以外の項目も必ず渡す。
// unit を落とすと「分割した瞬間に残明細の単位が消える」。lineNo は replaceItems_ が振り直す。
function cloneItemForRewrite_(item) {
  return {
    name: item.name,
    model: item.model,
    maker: item.maker,
    quantity: parseNumber_(item.quantity),
    unit: item.unit,
    unitPrice: parseNumber_(item.unitPrice),
    amount: parseNumber_(item.amount),
    desiredDeliveryDate: item.desiredDeliveryDate,
    note: item.note
  };
}

// 分離先の申請・明細・履歴を作る。戻り値: 新しい requestId。
function createReturnedSplitRequest_(request, movedItems, comment, user) {
  var now = nowString_();
  var newId = createId_(APP.REQUEST_ID_PREFIX);
  // 経路は標準に戻す。再提出時に resubmitRequest が上席から組み直すため、
  // 元申請に社長決裁が挿入されていてもここでは引き継がない。
  var route = [STEPS.SUPERVISOR, STEPS.PURCHASING_QUOTE, STEPS.GENERAL_MANAGER, STEPS.PURCHASING];

  appendObject_(SHEETS.REQUESTS, {
    requestId: newId,
    createdAt: now,
    updatedAt: now,
    // 申請者・部署・理由・申請日は元申請から複製する（分離先も「申請者本人の申請」であり、
    // いつ申請されたものかを失うと差戻しプールでの優先順位付けができなくなる）。
    submittedAt: request.submittedAt,
    completedAt: '',
    applicantEmail: request.applicantEmail,
    applicantName: request.applicantName,
    department: request.department,
    requestDate: request.requestDate,
    reasonCode: request.reasonCode,
    reasonDetail: request.reasonDetail,
    // 差戻し＝やり直しのため金額はリセットする（resubmitRequest が合計を0にする既存挙動と揃える）。
    totalAmount: 0,
    status: STATUS.RETURNED,
    currentStep: STEPS.APPLICANT,
    currentApproverEmail: '',
    currentApproverName: '',
    routeJson: JSON.stringify(route),
    // 差戻し中はPDFを持たない（再提出後に改めて生成される）。
    pdfFileId: '',
    pdfUrl: '',
    version: APP.VERSION,
    amountWaived: 'false',
    category: request.category,
    splitFromRequestId: request.requestId
  }, REQUEST_COLUMNS);

  replaceItems_(newId, movedItems.map(function(item) {
    var clone = cloneItemForRewrite_(item);
    clone.unitPrice = 0;
    clone.amount = 0;
    return clone;
  }));

  // 「申請」履歴を先に1件入れる。PDFの申請者印は 申請／再申請 の履歴から押されるため、
  // これが無いと分離先の申請書だけ申請者印が空欄になる。操作者ではなく元申請の申請者名義で残す
  // （この明細を申請したのは分割を実行した承認者ではない）。
  addHistory_({
    requestId: newId,
    actorEmail: request.applicantEmail,
    actorName: request.applicantName,
    action: ACTION.SUBMIT,
    toStatus: STATUS.IN_REVIEW,
    fromStep: STEPS.APPLICANT,
    toStep: STEPS.SUPERVISOR,
    comment: request.requestId + ' から分離（元の申請日 ' + request.requestDate + '）'
  });

  // action は「差戻」にする。詳細画面のプールパネルは action === 差戻 の履歴から差戻し理由を
  // 拾うため、ここを「明細分離」にすると分離先で理由が表示されなくなる。
  addHistory_({
    requestId: newId,
    actorEmail: user.email,
    actorName: user.name,
    action: ACTION.RETURN,
    fromStatus: request.status,
    toStatus: STATUS.RETURNED,
    fromStep: request.currentStep,
    toStep: STEPS.APPLICANT,
    comment: comment + '（' + request.requestId + ' の明細から分離）'
  });

  return newId;
}

// 分離先の差戻し通知。書き込みがすべて終わってから呼ぶこと。
// 途中で失敗して元申請と分離先が食い違っている状態でメールだけ飛ぶのを避けるため、
// 作成処理（createReturnedSplitRequest_）からは切り離してある。
// メール送信の失敗が分割自体（DB更新済み）を巻き戻さないよう握りつぶす（returnRequest と同じ流儀）。
function notifySplitReturned_(newRequestId, stepAtSplit, comment, actorName) {
  var newRequest = getRequestById_(newRequestId);
  if (!newRequest) {
    return;
  }
  try {
    sendReturnedEmail_(newRequest, comment);
  } catch (mailError) {
    Logger.log('notifySplitReturned_: sendReturnedEmail_ failed: ' + mailError.message);
  }
  try {
    sendReturnedBroadcast_(newRequest, stepAtSplit, comment, actorName);
  } catch (broadcastError) {
    Logger.log('notifySplitReturned_: sendReturnedBroadcast_ failed: ' + broadcastError.message);
  }
}

// 元申請側の履歴。ステップは動かさないので from/to とも現在のステップ。
// 「何を」「どこへ」出したかを1行で残し、分離後にPDFや金額が変わった理由を追えるようにする。
function addSplitOutHistory_(request, movedItems, newRequestId, cleanComment, user) {
  var headName = movedItems[0] ? movedItems[0].name : '';
  var label = movedItems.length > 1
    ? '明細「' + headName + '」ほか計' + movedItems.length + '件'
    : '明細「' + headName + '」';
  addHistory_({
    requestId: request.requestId,
    actorEmail: user.email,
    actorName: user.name,
    action: ACTION.SPLIT_OUT,
    fromStatus: request.status,
    toStatus: request.status,
    fromStep: request.currentStep,
    toStep: request.currentStep,
    comment: label + 'を ' + newRequestId + ' として差し戻し: ' + cleanComment
  });
}

// 分割の前提チェック（承認系3エントリで共通）。ここでは書き込みを一切行わない。
// 全画面（社長決裁）モードは明細ごとのチェックボックスを置けない画面であり、他人の端末を
// 渡した状態での複雑な選択は誤操作リスクが高いため、returnItems が来ても受け付けない。
function assertSplitAllowed_(request, user, kiosk, cleanComment) {
  // truthy 判定にする。approveRequest / recordPresidentDecision は `!kiosk` で承認者チェックを
  // 飛ばすため、ここだけ `=== true` にすると「承認者チェックは飛ぶのに分割ガードは効かない」
  // という食い違いが生じる。両者の kiosk の解釈を必ず揃えること。
  if (kiosk) {
    throw new Error('全画面モードでは明細ごとの差戻しはできません。詳細画面から操作してください。');
  }
  if (!canSplitItems_(request, user, getRequestItems_(request.requestId).length)) {
    throw new Error('この申請では明細ごとの差戻しはできません。');
  }
  if (!cleanComment) {
    throw new Error('差し戻す明細がある場合は、理由（コメント）を入力してください。');
  }
}

// 承認と同時に行う明細分割（approveRequest / recordPresidentDecision で共用）。
// 分離先の作成 → 残明細の書き戻し → 合計の再計算 → 元申請への履歴 → 通知、までを済ませる。
// 呼び出し側は本関数の後に申請行を読み直してから承認遷移を続けること。戻り値: 新しい requestId。
// （購買見積は単価の適用と1回の replaceItems_ にまとめる必要があるため confirmQuote 側で個別に組む）
//
// 【順序の理由】GAS にトランザクションは無く、スプレッドシート書き込みは一時エラーで落ちうる。
// 元申請から明細を削ってから分離先の作成に失敗すると、差し戻す明細はどこにも存在しなくなり復元できない。
// 逆順（分離先を先に作る）なら、途中で落ちても最悪「両方に同じ明細がある」だけで済み、人手で戻せる。
// 消失より重複を選ぶ。
// ただしこの保証が及ぶのは「分離する明細」だけである点に注意。残明細は replaceItems_ が
// 全削除→再挿入するため、その間に落ちれば失われる（これは分割に限らず、明細を書き戻す
// 既存のすべての操作＝明細修正・金額確定などが元々持っている性質）。
function splitItemsOnApproval_(request, returnItems, cleanComment, user, kiosk) {
  assertSplitAllowed_(request, user, kiosk, cleanComment);
  var selection = resolveSplitSelection_(request, returnItems);

  var newId = createReturnedSplitRequest_(request, selection.moved, cleanComment, user);

  replaceItems_(request.requestId, selection.remaining.map(cloneItemForRewrite_));
  // 合計は常に「残明細の金額の総和」。金額未確定の段階なら自然に0になる。
  var total = 0;
  selection.remaining.forEach(function(item) {
    total += parseNumber_(item.amount);
  });
  updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', request.requestId, {
    updatedAt: nowString_(),
    totalAmount: total
  });

  addSplitOutHistory_(request, selection.moved, newId, cleanComment, user);
  notifySplitReturned_(newId, request.currentStep, cleanComment, user.name);
  return newId;
}

// 分割で明細が変わった元申請のPDFを、遷移先の都合で再生成されない場合に限って作り直す。
// dispatchApprovalNotifications_ がPDFを生成するのは次が 購買見積／購買手配／完了 のときだけで、
// 総務部長→社長 では生成されない。二重生成を避けるため、生成される遷移では何もしない。
// 既にPDFを持つ申請だけが対象（上席ステップではまだPDFが無い）。
function regeneratePdfAfterSplit_(updated, nextStep, user) {
  var pdfWillBeGenerated = (nextStep === STEPS.PURCHASING_QUOTE || nextStep === STEPS.PURCHASING || nextStep === STEPS.DONE);
  if (!pdfWillBeGenerated && updated.pdfFileId) {
    createRequestPdfInternal_(updated.requestId, user);
  }
}

// returnItems（第4引数）を渡すと、選択された明細だけを新しい差戻し申請へ切り出したうえで承認する。
function approveRequest(requestId, comment, kiosk, returnItems) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var request = requireRequest_(requestId);
    // kiosk=全画面承認モードでの代理記録時は本人以外でも可（運用・物理管理に依存）。
    if (!kiosk && !canApprove_(request, user)) {
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

    // 遷移先（経路・次の承認者）の解決は、分割より前に必ず済ませること。
    // GAS にトランザクションは無いため、分割（明細の移動・分離先の作成・メール送信）を確定させた後に
    // 「承認者マスタ未設定」などで throw すると、明細だけ分離されて承認は失敗という中途半端な状態が残る。
    // 経路も承認者も明細には依存しないので、先に解決しておけば分割後に落ちる要因が無くなる。
    var route = jsonParse_(request.routeJson, []);
    var currentIndex = route.indexOf(request.currentStep);
    if (currentIndex === -1) {
      throw new Error('承認経路が不正です。管理者に確認してください。');
    }

    var curKey = request.currentStep;
    var nextStep = route[currentIndex + 1] || STEPS.DONE;
    var nextApprover = { email: '', name: '' };
    if (nextStep !== STEPS.DONE) {
      var approverRule = requireApproverRule_(request.applicantEmail, request.department);
      nextApprover = getApproverForStep_(approverRule, nextStep, request.category);
    }

    // 残明細で totalAmount が再計算されるため、以降の承認処理は必ず分割後の申請行を見る。
    var splitHappened = false;
    if (Array.isArray(returnItems) && returnItems.length > 0) {
      splitItemsOnApproval_(request, returnItems, sanitizeText_(comment, 1000), user, kiosk);
      splitHappened = true;
      request = getRequestById_(requestId);
    }

    var now = nowString_();
    var patch = {
      updatedAt: now
    };

    if (nextStep === STEPS.DONE) {
      patch.status = STATUS.COMPLETED;
      patch.currentStep = STEPS.DONE;
      patch.currentApproverEmail = '';
      patch.currentApproverName = '';
      patch.completedAt = now;
    } else {
      patch.status = STATUS.IN_REVIEW;
      patch.currentStep = nextStep;
      patch.currentApproverEmail = nextApprover.email;
      patch.currentApproverName = nextApprover.name;
    }

    var actEmail = user.email, actName = user.name, cmt = sanitizeText_(comment, 1000);
    if (kiosk) {
      var ka = kioskAttribution_(request, user, comment);
      actEmail = ka.email; actName = ka.name; cmt = ka.comment;
    }

    updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', requestId, patch);
    addHistory_({
      requestId: requestId,
      actorEmail: actEmail,
      actorName: actName,
      action: nextStep === STEPS.DONE ? ACTION.COMPLETE : ACTION.APPROVE,
      fromStatus: request.status,
      toStatus: patch.status,
      fromStep: request.currentStep,
      toStep: patch.currentStep,
      comment: cmt
    });

    var updated = getRequestById_(requestId);
    dispatchApprovalNotifications_(updated, curKey, nextStep, nextApprover, user);
    if (splitHappened) {
      regeneratePdfAfterSplit_(updated, nextStep, user);
    }

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

// 購買(見積)ステップの操作。各明細の税抜単価を入力し金額を確定する。
// 単価・金額・totalAmount はすべて税抜。消費税・税込は表示時に算出する。
// 税抜小計が10万円（社長決裁しきい値・税抜基準）以上なら社長決裁を経路へ追加する。
// items = [{ itemId, unitPrice }] / returnItems = [{ itemId, lineNo, name }]（明細の部分差戻し）
function confirmQuote(requestId, items, comment, returnItems) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var request = requireRequest_(requestId);
    if (!canApprove_(request, user) || request.currentStep !== STEPS.PURCHASING_QUOTE) {
      throw new Error('購買担当者のみ見積金額を入力できます。');
    }

    var cleanComment = sanitizeText_(comment, 1000);
    // 分割の検証だけ先に済ませ、書き込みは単価適用と同じ1回の replaceItems_ にまとめる。
    // replaceItems_ は残明細の itemId も付け替えるため、先に分割してから単価を書くと
    // 古い itemId を参照することになり単価が付かない。
    // 見積パネルは詳細画面専用（全画面モードからは呼ばれない）ため kiosk 相当は常に false。
    var selection = null;
    if (Array.isArray(returnItems) && returnItems.length > 0) {
      assertSplitAllowed_(request, user, false, cleanComment);
      selection = resolveSplitSelection_(request, returnItems);
    }

    var priceMap = {};
    (items || []).forEach(function(input) {
      if (input && input.itemId) {
        priceMap[input.itemId] = parseNumber_(input.unitPrice);
      }
    });

    // 分割時は残明細だけを対象にする。分離される明細の単価が items に混じっていても使わない
    // （分離先は金額0でやり直すため）。合計も残明細だけの集計になり、社長決裁の要否判定が
    // 自然に分割後の金額で行われる。
    var existingItems = selection ? selection.remaining : getRequestItems_(requestId);

    var total = 0;
    var rebuilt = existingItems.map(function(item) {
      var quantity = parseNumber_(item.quantity);
      var unitPrice = Object.prototype.hasOwnProperty.call(priceMap, item.itemId)
        ? priceMap[item.itemId]
        : parseNumber_(item.unitPrice);
      var amount = Math.round(quantity * unitPrice);
      total += amount;
      // 明細は毎回まるごと作り直して書き戻すため、金額以外の項目も必ず引き継ぐこと。
      // unit を落とすと「金額を確定した瞬間に全明細の単位が消える」。
      return {
        name: item.name,
        model: item.model,
        maker: item.maker,
        quantity: quantity,
        unit: item.unit,
        unitPrice: unitPrice,
        amount: amount,
        desiredDeliveryDate: item.desiredDeliveryDate,
        note: item.note
      };
    });

    if (!(total > 0)) {
      throw new Error('金額を入力してください。');
    }

    // 遷移先（総務部長）の解決は分割・書き込みより前に済ませる。分割を確定させた後に
    // 「総務部長の承認者メールアドレスが未設定です」で throw すると、明細だけ分離されて
    // 金額確定は失敗、しかも分離先の通知も飛ばない、という中途半端な状態が残る。
    var approverRule = requireApproverRule_(request.applicantEmail, request.department);
    var gm = getApproverForStep_(approverRule, STEPS.GENERAL_MANAGER);

    // 分離先を先に作ってから元申請を書き換える（splitItemsOnApproval_ と同じ理由。
    // 元申請から明細を削った後に分離先の作成が失敗すると、その明細が復元できなくなる）。
    var splitRequestId = '';
    if (selection) {
      splitRequestId = createReturnedSplitRequest_(request, selection.moved, cleanComment, user);
    }

    // 残明細＋単価を1回で書き切る。
    replaceItems_(requestId, rebuilt);

    if (selection) {
      addSplitOutHistory_(request, selection.moved, splitRequestId, cleanComment, user);
    }

    var threshold = getThresholdAmount_();
    // total は税抜小計。しきい値も税抜基準のため、そのまま比較する。
    // 分割した場合の total は残明細の合計なので、社長決裁の要否は分割後の金額で判定される
    // （見積は金額を初めて確定させる工程であり、経路もここで新しく組み直すのが正しい）。
    var over = total >= threshold;
    var tax = taxAmount_(total);
    var gross = grossAmount_(total);
    var amountSummary = '税抜 ' + formatCurrency_(total) + '（消費税 ' + formatCurrency_(tax) + '／税込 ' + formatCurrency_(gross) + '）';
    var newRoute = over
      ? [STEPS.SUPERVISOR, STEPS.PURCHASING_QUOTE, STEPS.GENERAL_MANAGER, STEPS.PRESIDENT, STEPS.PURCHASING]
      : [STEPS.SUPERVISOR, STEPS.PURCHASING_QUOTE, STEPS.GENERAL_MANAGER, STEPS.PURCHASING];

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
      comment: ['見積金額 ' + amountSummary + (over ? '（税抜10万円以上）' : ''), cleanComment].filter(Boolean).join(' ')
    });

    var updated = getRequestById_(requestId);
    // 10万円以上（社長決裁しきい値以上）は、総務部長へ「要・社長決裁」を強くアピールする。
    var overBanner = [
      '━━━━━━━━━━━━━━━━━━━━━━',
      '🚨🚨🚨  要・社長決裁  🚨🚨🚨',
      '━━━━━━━━━━━━━━━━━━━━━━',
      '確定金額（税抜）' + formatCurrency_(total) + ' は社長決裁しきい値（税抜 ' + formatCurrency_(threshold) + '）以上です。',
      '総務部長の承認後、必ず【社長の承認】が必要です。至急ご対応ください。',
      '━━━━━━━━━━━━━━━━━━━━━━'
    ].join('\n');
    var gaSubject = over
      ? '【🚨至急・要社長決裁🚨】[貯蔵品購入申請] 金額確定 ' + updated.requestId + '（税抜10万円以上）'
      : '[貯蔵品購入申請] 金額確定 ' + updated.requestId;
    var gaHeading = over
      ? overBanner + '\n\n購買が見積金額 ' + amountSummary + ' を確定しました。'
      : '購買が見積金額 ' + amountSummary + ' を確定しました。総務部長承認をお願いします。';
    notifyGeneralAffairs_(updated, gaSubject, buildGeneralAffairsBody_(updated, gaHeading), null);
    sendApprovalRequestEmail_(updated, gm, over ? overBanner : '');
    // 見積確定の遷移先は常に総務部長で、この経路ではPDFが生成されない。
    // 分割で明細が変わった以上、既にPDFがある申請は必ず作り直す。
    if (selection) {
      regeneratePdfAfterSplit_(updated, STEPS.GENERAL_MANAGER, user);
      notifySplitReturned_(splitRequestId, STEPS.PURCHASING_QUOTE, cleanComment, user.name);
    }
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

// discard=true の場合は「却下（削除）」：差戻しプールへ入れず直接 取消(CANCELLED) にする。
// 差し戻しプールの肥大化を防ぐため、二度と直さない案件を承認者が入口で捨てられるようにする。
// discard=false（既定）は従来の差戻し：RETURNED にして申請者へ戻す（プールで編集・再提出可能）。
function returnRequest(requestId, comment, kiosk, discard) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var request = requireRequest_(requestId);
    if (!kiosk && !canApprove_(request, user)) {
      throw new Error('現在の承認者のみ差戻し・却下できます。');
    }

    var cleanComment = sanitizeText_(comment, 1000);
    if (!cleanComment) {
      throw new Error(discard ? '却下理由（コメント）を入力してください。' : '差戻しコメントを入力してください。');
    }

    var actEmail = user.email, actName = user.name, histComment = cleanComment;
    if (kiosk) {
      var ka = kioskAttribution_(request, user, comment);
      actEmail = ka.email; actName = ka.name; histComment = ka.comment;
    }

    if (discard) {
      // PDFはDriveから破棄（却下済の申請書を残さない）。既に削除済みなどは無視。
      if (request.pdfFileId) {
        try {
          DriveApp.getFileById(request.pdfFileId).setTrashed(true);
        } catch (trashError) {
          Logger.log('returnRequest(却下): trash pdf skipped: ' + trashError.message);
        }
      }
      updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', requestId, {
        updatedAt: nowString_(),
        status: STATUS.CANCELLED,
        currentApproverEmail: '',
        currentApproverName: '',
        pdfFileId: '',
        pdfUrl: ''
      });
      addHistory_({
        requestId: requestId,
        actorEmail: actEmail,
        actorName: actName,
        action: ACTION.REJECT,
        fromStatus: request.status,
        toStatus: STATUS.CANCELLED,
        fromStep: request.currentStep,
        toStep: request.currentStep,
        comment: histComment
      });
      var rejected = getRequestById_(requestId);
      // メール送信の失敗が却下自体（DB更新済み）を巻き戻さないよう握りつぶす。
      try {
        sendRejectedEmail_(rejected, cleanComment);
      } catch (mailError) {
        Logger.log('sendRejectedEmail_ failed: ' + mailError.message);
      }
      return getRequestDetail(requestId);
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
      actorEmail: actEmail,
      actorName: actName,
      action: ACTION.RETURN,
      fromStatus: request.status,
      toStatus: STATUS.RETURNED,
      fromStep: request.currentStep,
      toStep: STEPS.APPLICANT,
      comment: histComment
    });

    var updated = getRequestById_(requestId);
    sendReturnedEmail_(updated, cleanComment);
    // 通知宛先マスタの全宛先へ、差戻しになった旨を明細付きで一斉共有する。
    // 差戻し段階は「申請者へ戻す前の」currentStep（＝request.currentStep）を渡す。
    // 一斉送信の失敗が差戻し自体（DB更新済み）を巻き戻さないよう握りつぶす。
    try {
      sendReturnedBroadcast_(request, request.currentStep, cleanComment, actName);
    } catch (broadcastError) {
      Logger.log('sendReturnedBroadcast_ failed: ' + broadcastError.message);
    }
    return getRequestDetail(requestId);
  } finally {
    lock.releaseLock();
  }
}

// 1段リコール（1つ前のステージへ戻す）。
// 誤操作リカバリ用。差戻し（申請者へ全戻し）とは別に、承認チェーン内で1段だけ巻き戻す。
// 操作できるのは「現在の承認者／このステップへ直前に進めた本人（購買担当者など）／管理者」。
function recallStep(requestId, comment) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var request = requireRequest_(requestId);
    var route = jsonParse_(request.routeJson, []);

    if (!canRecall_(request, user)) {
      throw new Error('この申請を前段へ戻す権限がありません。');
    }

    var currentIndex = route.indexOf(request.currentStep);
    if (currentIndex <= 0) {
      throw new Error('先頭ステップのため前段へ戻せません。申請者へ戻す場合は「差戻し」をご利用ください。');
    }

    var cleanComment = sanitizeText_(comment, 1000);
    if (!cleanComment) {
      throw new Error('戻す理由（コメント）を入力してください。');
    }

    var prevStep = route[currentIndex - 1];
    var now = nowString_();
    var approverRule = requireApproverRule_(request.applicantEmail, request.department);

    var patch = { updatedAt: now, status: STATUS.IN_REVIEW };

    // 購買（見積）まで戻す場合は、金額確定不要フラグを解除し経路を標準へ再構築する
    // （誤って挿入された社長決裁を除去）。totalAmount は意図的に保持する：
    // 次の confirmQuote が totalAmount と閾値で社長決裁の要否を再判定・再挿入するため、
    // 金額をリセットしなくても決裁ルートは正しく復元される。
    if (prevStep === STEPS.PURCHASING_QUOTE) {
      patch.amountWaived = 'false';
      patch.routeJson = JSON.stringify([STEPS.SUPERVISOR, STEPS.PURCHASING_QUOTE, STEPS.GENERAL_MANAGER, STEPS.PURCHASING]);
    }

    var resolved = resolveStepApproverForRecall_(request, prevStep, approverRule);
    patch.currentStep = prevStep;
    patch.currentApproverEmail = resolved.approver.email;
    patch.currentApproverName = resolved.approver.name;

    updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', requestId, patch);
    addHistory_({
      requestId: requestId,
      actorEmail: user.email,
      actorName: user.name,
      action: ACTION.RECALL,
      fromStatus: request.status,
      toStatus: STATUS.IN_REVIEW,
      fromStep: request.currentStep,
      toStep: prevStep,
      comment: cleanComment
    });

    // 戻し先ステップの承認者（複数登録に対応）へ再承認依頼を送る。
    var updated = getRequestById_(requestId);
    resolved.members.forEach(function(member) {
      sendApprovalRequestEmail_(updated, member);
    });

    return getRequestDetail(requestId);
  } finally {
    lock.releaseLock();
  }
}

// 1段リコールの権限判定。承認中かつ先頭でない案件を、
// 現在の承認者／直前にこのステップへ進めた本人／管理者が戻せる。
function canRecall_(request, user) {
  if (request.status !== STATUS.IN_REVIEW) {
    return false;
  }
  var route = jsonParse_(request.routeJson, []);
  var currentIndex = route.indexOf(request.currentStep);
  if (currentIndex <= 0) {
    return false;
  }
  // 管理者／現在の承認者（canApprove_ に内包）
  if (canApprove_(request, user)) {
    return true;
  }
  // このステップへ直前に進めた本人（例: 誤って至急送信した購買担当者）
  var advancer = lastAdvanceActorEmail_(request.requestId, request.currentStep);
  return !!advancer && advancer === normalizeEmail_(user.email);
}

// 現在のステップへ「前進で」進めた直前の履歴の操作者メールを返す（無ければ ''）。
function lastAdvanceActorEmail_(requestId, currentStep) {
  var forwardActions = [ACTION.SUBMIT, ACTION.RESUBMIT, ACTION.APPROVE, ACTION.QUOTE, ACTION.EXPEDITE];
  var rows = readObjects_(SHEETS.HISTORY, HISTORY_COLUMNS)
    .filter(function(row) {
      return row.requestId === requestId &&
        row.toStep === currentStep &&
        forwardActions.indexOf(row.action) !== -1;
    })
    .sort(function(a, b) {
      return String(a.happenedAt).localeCompare(String(b.happenedAt));
    });
  var last = rows[rows.length - 1];
  return last ? normalizeEmail_(last.actorEmail) : '';
}

// リコール先ステップの承認者を解決する。上席は上席マスタ、その他は承認者マスタ。
// 戻り値: { approver: {email,name}, members: [{email,name,...}] }（members は再承認依頼の宛先）。
function resolveStepApproverForRecall_(request, step, approverRule) {
  if (step === STEPS.SUPERVISOR) {
    var supervisors = resolveSupervisors_(request.applicantEmail, request.department);
    if (supervisors.length === 0) {
      throw new Error('上席承認者が未設定です。部署別 上席マスタ（または承認者マスタの上席）を設定してください。');
    }
    return {
      approver: { email: supervisors[0].email, name: supervisors[0].name },
      members: supervisors
    };
  }
  var approver = getApproverForStep_(approverRule, step, request.category);
  var members = resolveStepApproversWithStar_(approverRule, step, request.category);
  return { approver: approver, members: members.length > 0 ? members : [approver] };
}

// 申請の取消（論理削除）。レコードと履歴は監査のため残し、status を CANCELLED にする。
// PDF はDriveから破棄し参照をクリアする（取消済の申請書を残さない）。
// 権限: 管理者は常時、申請者は自分の申請かつ完了前まで（canDelete_）。
function deleteRequest(requestId, comment) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var request = requireRequest_(requestId);
    if (!canDelete_(request, user)) {
      throw new Error('この申請を取り消す権限がありません。');
    }

    // PDFはDriveから破棄（取消済の申請書を残さない）。既に削除済みなどは無視。
    if (request.pdfFileId) {
      try {
        DriveApp.getFileById(request.pdfFileId).setTrashed(true);
      } catch (trashError) {
        Logger.log('deleteRequest: trash pdf skipped: ' + trashError.message);
      }
    }

    var cleanComment = sanitizeText_(comment, 1000);
    updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', requestId, {
      updatedAt: nowString_(),
      status: STATUS.CANCELLED,
      currentApproverEmail: '',
      currentApproverName: '',
      pdfFileId: '',
      pdfUrl: ''
    });
    addHistory_({
      requestId: requestId,
      actorEmail: user.email,
      actorName: user.name,
      action: ACTION.CANCEL,
      fromStatus: request.status,
      toStatus: STATUS.CANCELLED,
      fromStep: request.currentStep,
      toStep: request.currentStep,
      comment: cleanComment
    });

    return getRequestDetail(requestId);
  } finally {
    lock.releaseLock();
  }
}

// 取消可否。取消済は不可。管理者は常時、申請者本人は完了前（COMPLETED以外）まで。
function canDelete_(request, user) {
  if (request.status === STATUS.CANCELLED) {
    return false;
  }
  if (isAdmin_(user.email)) {
    return true;
  }
  // 差戻しプールは肥大化防止のため、差戻し中は認証済みユーザーなら誰でも削除（取消）可。
  if (request.status === STATUS.RETURNED && !!(user && user.email)) {
    return true;
  }
  return normalizeEmail_(request.applicantEmail) === user.email &&
    request.status !== STATUS.COMPLETED;
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
  var returned = 0;
  readObjects_(SHEETS.REQUESTS, REQUEST_COLUMNS).forEach(function(request) {
    // 取消（論理削除）済はどのバッジ件数にも数えない。
    if (request.status === STATUS.CANCELLED) {
      return;
    }
    // 差戻しプールの件数（全員共通）。
    if (request.status === STATUS.RETURNED) {
      returned++;
    }
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
  return { pending: pending, president: president, quote: quote, supervisor: supervisor, gm: gm, arrange: arrange, returned: returned };
}

// returnItems（第5引数）は decision === 'approve' のときだけ受け付ける。
// 全体差戻し（decision === 'return'）では申請ごと差し戻すため明細選択は無意味であり、無視する。
function recordPresidentDecision(requestId, decision, comment, kiosk, returnItems) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var request = requireRequest_(requestId);
    // kiosk=全画面承認モードでの代理記録時は本人以外でも可。代理は履歴に注記される。
    if (!kiosk && !canPresident_(request, user)) {
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
      // 社長決裁段階での差戻しも、通知宛先マスタの全宛先へ一斉共有する。
      try {
        sendReturnedBroadcast_(request, STEPS.PRESIDENT, cleanComment, presidentName);
      } catch (broadcastError) {
        Logger.log('sendReturnedBroadcast_ failed: ' + broadcastError.message);
      }
      return getRequestDetail(requestId);
    }

    if (decision !== 'approve') {
      throw new Error('不正な操作です。');
    }

    // 遷移先の解決は分割より前に済ませる（approveRequest と同じ理由。分割を確定させた後に
    // 承認者マスタ未設定などで throw すると、明細だけ分離されて承認は失敗という状態が残る）。
    var route = jsonParse_(request.routeJson, []);
    var currentIndex = route.indexOf(STEPS.PRESIDENT);
    if (currentIndex === -1) {
      throw new Error('承認経路が不正です。管理者に確認してください。');
    }

    var nextStep = route[currentIndex + 1] || STEPS.DONE;
    var nextApprover = { email: '', name: '' };
    if (nextStep !== STEPS.DONE) {
      var approverRule = requireApproverRule_(request.applicantEmail, request.department);
      nextApprover = getApproverForStep_(approverRule, nextStep, request.category);
    }

    // 以降は分割後の申請行を見る。updatedAt は分割で更新されるため、ここで採り直す。
    var splitHappened = false;
    if (Array.isArray(returnItems) && returnItems.length > 0) {
      splitItemsOnApproval_(request, returnItems, cleanComment, user, kiosk);
      splitHappened = true;
      request = getRequestById_(requestId);
      now = nowString_();
    }

    var patch = { updatedAt: now };
    if (nextStep === STEPS.DONE) {
      patch.status = STATUS.COMPLETED;
      patch.currentStep = STEPS.DONE;
      patch.currentApproverEmail = '';
      patch.currentApproverName = '';
      patch.completedAt = now;
    } else {
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
    if (splitHappened) {
      regeneratePdfAfterSplit_(updated, nextStep, user);
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
  // 品目区分は必須。未選択（空・未知の値）は既定に倒さずエラーにする。
  // 格納コードは日本語化済みのため、CATEGORIES の「値」の集合で照合する（キー照合は不可）。
  var category = sanitizeText_(input.category, 20);
  var validCategories = Object.keys(CATEGORIES).map(function(key) {
    return CATEGORIES[key];
  });
  var reasonCode = sanitizeText_(input.reasonCode, 1);
  var validReason = REASONS.some(function(reason) {
    return reason.code === reasonCode;
  });

  if (!department) {
    throw new Error('部署を入力してください。');
  }
  if (validCategories.indexOf(category) === -1) {
    throw new Error('品目区分（メカ／電気／一般・不明）を選択してください。');
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
      unit: normalizeUnit_(item.unit),
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
    category: category,
    requestDate: requestDate,
    reasonCode: reasonCode,
    reasonDetail: sanitizeText_(input.reasonDetail, 2000),
    items: items,
    totalAmount: 0
  };
}

// 承認前の明細修正（updateRequestInReview）用に、明細だけを検証・正規化する。
// 申請者は単価・金額を扱わないため normalizeRequestPayload_ と同じく amount は常に0で保存する
// （編集窓は金額入力前なので unitPrice も0のまま）。品名必須・数量1以上・1行以上を強制する。
function normalizeEditableItems_(payload) {
  var input = payload || {};
  var items = (input.items || []).map(function(item) {
    return {
      name: sanitizeText_(item.name, 200),
      model: sanitizeText_(item.model, 200),
      maker: sanitizeText_(item.maker, 200),
      quantity: parseNumber_(item.quantity),
      unit: normalizeUnit_(item.unit),
      // 申請者は単価・金額を扱わない。細工されたリクエストで unitPrice が送られても
      // 無条件で0に落とす（confirmQuote の単価フォールバックへ混入させない防御）。
      unitPrice: 0,
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
  return items;
}

// 明細の旧→新差分を人間可読な履歴コメントに整形する（例「明細1 型式: A→B、品名: ○→△」）。
// 行は位置（行番号順）で対応づける。行の増減は追加／削除として記す。実質変更が無ければ空文字。
function describeItemChanges_(oldItems, newItems) {
  var fields = [
    { key: 'name', label: '品名' },
    { key: 'model', label: '型式' },
    { key: 'maker', label: 'メーカー' },
    { key: 'quantity', label: '数量' },
    { key: 'unit', label: '単位' },
    { key: 'desiredDeliveryDate', label: '希望納期' },
    { key: 'note', label: '備考' }
  ];
  var lines = [];
  var max = Math.max(oldItems.length, newItems.length);
  for (var i = 0; i < max; i++) {
    var oldItem = oldItems[i];
    var newItem = newItems[i];
    if (!oldItem && newItem) {
      lines.push('明細' + (i + 1) + ': 追加（' + (newItem.name || '—') + '）');
      continue;
    }
    if (oldItem && !newItem) {
      lines.push('明細' + (i + 1) + ': 削除（' + (oldItem.name || '—') + '）');
      continue;
    }
    var changes = [];
    fields.forEach(function(field) {
      var before = compareValue_(oldItem[field.key]);
      var after = compareValue_(newItem[field.key]);
      if (before !== after) {
        changes.push(field.label + ': ' + (before || '—') + '→' + (after || '—'));
      }
    });
    if (changes.length > 0) {
      lines.push('明細' + (i + 1) + ' ' + changes.join('、'));
    }
  }
  return lines.join(' ／ ');
}

// 差分比較用の値正規化。数値・文字列の表記ゆれ（例 2 と "2"）を吸収し、前後空白を除く。
function compareValue_(value) {
  return String(value == null ? '' : value).trim();
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

// キオスク代理（全画面承認モードを他人の端末で操作）時の記録属性。
// ハンコ・履歴には現ステップの正規承認者を記録し、操作者は注記で明示する。
function kioskAttribution_(request, user, comment) {
  return {
    email: request.currentApproverEmail || user.email,
    name: request.currentApproverName || user.name,
    comment: [sanitizeText_(comment, 1000), '（' + user.name + 'の端末で代理記録）'].filter(Boolean).join(' ')
  };
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

// 差戻しプールの操作可否。閲覧と同じく認証済みユーザーなら誰でも可（申請者本人に限らない）。
// 編集（保存）・再提出・削除で共通の判定：差戻し中(RETURNED)であること。操作者は履歴に記録される。
function canEdit_(request, user) {
  return request.status === STATUS.RETURNED && !!(user && user.email);
}

// 差戻しプールからの再提出可否（編集と同条件）。
function canResubmit_(request, user) {
  return request.status === STATUS.RETURNED && !!(user && user.email);
}

// 差戻しプールからの削除（取消）可否。プールの肥大化防止のため差戻し中は誰でも削除可。
// 通常の取消（canDelete_：申請者本人・管理者）とは別枠。
function canDeletePool_(request, user) {
  return request.status === STATUS.RETURNED && !!(user && user.email);
}

// 承認前の明細修正（updateRequestInReview）の可否。閲覧と同じく認証済みユーザーなら誰でも可
// （申請者本人に限らない。購買担当や他部署の人が記入ミスを直せるようにする運用）。ただし承認中で、
// かつ購買が金額を確定する前（currentStep が 上席／購買見積、金額免除でない）に限る。
// confirmQuote/confirmExpedited が実行されるとステップが総務部長へ進む or amountWaived が立つため、
// この窓は金額入力の瞬間に自動的に閉じる（承認済み金額・経路との矛盾を防ぐ）。編集者は履歴に記録される。
//
// 判定はステップ基準に一本化する（totalAmount では判定しない）。経路は
// 上席→購買見積→総務部長→(社長)→購買手配 で固定され、金額が確定すると必ず総務部長へ進むため、
// 「ステップが上席／購買見積である」ことが「金額未確定である」ことと同値になる。
// 唯一の例外が recallStep で購買見積へ戻したケース（totalAmount を意図的に保持する）で、
// 金額を入れ直すために戻したのに明細を直せない、という状態を避けるためステップで判定する。
function canEditInReview_(request, user) {
  return !!(user && user.email) &&
    request.status === STATUS.IN_REVIEW &&
    !parseBoolean_(request.amountWaived) &&
    (request.currentStep === STEPS.SUPERVISOR || request.currentStep === STEPS.PURCHASING_QUOTE);
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
// category（任意）: 購買ステップの代表者を品目区分で切り替えるために渡す。
function resolveStepApprovers_(rule, step, category) {
  if (!rule) { return []; }
  var field = stepRoleField_(step);
  if (!field) { return []; }
  // 購買ステップ（見積／手配）は、品目区分に関わらずメカ購買・電気購買の双方が
  // アプリ上で操作（見積入力・手配完了・承認）できる。メール宛先の振り分け（TO/CC）は
  // 区分で行うが、操作権限は両購買に開放する。
  // 代表（先頭＝currentApproverName／通知先）は品目区分に追従させる：電気区分は電気購買、
  // メカ・一般／不明はメカ購買を代表にする。category 未指定時は従来どおりメカ購買を優先。
  if (field === 'purchasing') {
    var mech = approverMembersFromField_(rule, 'purchasing', stepRoleDefaultName_(step));
    var elec = approverMembersFromField_(rule, 'purchasingElec', '電気購買');
    var ordered = normalizeCategory_(category) === CATEGORIES.ELEC
      ? elec.concat(mech)
      : mech.concat(elec);
    var seen = {};
    var members = [];
    ordered.forEach(function(m) {
      if (m.email && !seen[m.email]) { seen[m.email] = true; members.push(m); }
    });
    return members;
  }
  return approverMembersFromField_(rule, field, stepRoleDefaultName_(step));
}

// 承認者ルールの <field>Email / <field>Name / <field>Title から承認者配列を作る。
function approverMembersFromField_(rule, field, defaultName) {
  var name = rule[field + 'Name'] || defaultName || '';
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
function resolveStepApproversWithStar_(rule, step, category) {
  var approvers = resolveStepApprovers_(rule, step, category);
  if (approvers.length > 0) {
    return approvers;
  }
  if (!rule || rule.department !== '*') {
    var star = findStarRule_();
    if (star) {
      return resolveStepApprovers_(star, step, category);
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

// category（任意）: 購買ステップの代表者（currentApproverName/Email・通知先）を品目区分で切り替える。
function getApproverForStep_(rule, step, category) {
  if (step === STEPS.SUPERVISOR) {
    var supervisor = { email: normalizeEmail_(rule.supervisorEmail), name: rule.supervisorName || '上席' };
    if (!supervisor.email) {
      throw new Error(STEP_LABELS[step] + 'の承認者メールアドレスが未設定です。');
    }
    return supervisor;
  }
  // 複数登録時は先頭を代表として currentApproverEmail / 通知の既定にする。
  // 部署別ルールに該当役職が無ければ '*' にフォールバックする。
  var approvers = resolveStepApproversWithStar_(rule, step, category);
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
    purchasingElecEmail: normalizeEmailList_(row.purchasingElecEmail),
    purchasingElecName: sanitizeText_(row.purchasingElecName, 100),
    active: row.active === '' ? true : parseBoolean_(row.active),
    supervisorTitle: sanitizeText_(row.supervisorTitle, 40),
    generalManagerTitle: sanitizeText_(row.generalManagerTitle, 40),
    presidentTitle: sanitizeText_(row.presidentTitle, 40),
    purchasingTitle: sanitizeText_(row.purchasingTitle, 40),
    purchasingElecTitle: sanitizeText_(row.purchasingElecTitle, 40)
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
    purchasingElecEmail: client.purchasingElecEmail,
    purchasingElecName: client.purchasingElecName,
    active: String(client.active),
    supervisorTitle: client.supervisorTitle,
    generalManagerTitle: client.generalManagerTitle,
    presidentTitle: client.presidentTitle,
    purchasingTitle: client.purchasingTitle,
    purchasingElecTitle: client.purchasingElecTitle
  };
}
