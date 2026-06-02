function generateRequestPdf(requestId) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    var request = requireRequest_(requestId);
    if (!canGeneratePdf_(request, user)) {
      throw new Error('完了済み申請のみPDFを作成できます。');
    }
    createRequestPdfInternal_(requestId, user);
    return getRequestDetail(requestId);
  } finally {
    lock.releaseLock();
  }
}

function createRequestPdfInternal_(requestId, actor) {
  var detail = buildPdfDetail_(requestId);
  var template = HtmlService.createTemplateFromFile('PdfTemplate');
  template.detail = detail;
  template.helpers = {
    escapeHtml: escapeHtml_,
    currency: formatCurrency_,
    statusLabel: function(status) {
      return STATUS_LABELS[status] || status;
    },
    stepLabel: function(step) {
      return STEP_LABELS[step] || step;
    },
    actionLabel: function(action) {
      return ACTION_LABELS[action] || action;
    }
  };

  var html = template.evaluate().getContent();
  var fileName = detail.request.requestId + '_' + sanitizeFileName_(detail.request.applicantName) + '_貯蔵品購入申請書.pdf';
  var blob = Utilities
    .newBlob(html, MimeType.HTML, fileName + '.html')
    .getAs(MimeType.PDF)
    .setName(fileName);
  var file = getPdfFolder_().createFile(blob);

  updateObjectById_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', requestId, {
    updatedAt: nowString_(),
    pdfFileId: file.getId(),
    pdfUrl: file.getUrl()
  });
  addHistory_({
    requestId: requestId,
    actorEmail: actor ? actor.email : '',
    actorName: actor ? actor.name : '',
    action: ACTION.PDF_GENERATE,
    fromStatus: STATUS.COMPLETED,
    toStatus: STATUS.COMPLETED,
    fromStep: STEPS.DONE,
    toStep: STEPS.DONE,
    comment: file.getUrl()
  });

  return file;
}

function buildPdfDetail_(requestId) {
  var request = requireRequest_(requestId);
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
    });

  return {
    request: toClientRequest_(request),
    items: items,
    history: history,
    generatedAt: nowString_(),
    thresholdAmount: getThresholdAmount_()
  };
}
