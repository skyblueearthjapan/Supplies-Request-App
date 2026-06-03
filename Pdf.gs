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
  template.reasons = REASONS;
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
    },
    surname: function(name) {
      return String(name || '').trim().split(/\s+/)[0] || '';
    },
    dateOnly: function(value) {
      return String(value || '').slice(0, 10);
    },
    // データネーム印の中央帯（元号年.月.日）。例: 2026-05-27 → 令8. 5. 27
    eraDate: function(value) {
      var s = String(value || '').slice(0, 10);
      var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
      if (!m) {
        return '';
      }
      var y = Number(m[1]);
      var n = y - 2018; // 令和元年 = 2019
      var era = n >= 1 ? ('令' + n) : String(y % 100);
      return era + '. ' + Number(m[2]) + '. ' + Number(m[3]);
    },
    // 決裁印の下段。承認者マスタの役職があればそれを、無ければ役割名を返す。
    stepTitle: function(stepKey) {
      var t = detail.stepTitles && detail.stepTitles[stepKey];
      return t ? t : (STEP_LABELS[stepKey] || stepKey);
    },
    // 印面に収まるよう文字数で氏名フォントを縮小。
    nameFont: function(name) {
      var n = String(name || '').replace(/\s+/g, '').length;
      if (n <= 2) {
        return '9pt';
      }
      if (n === 3) {
        return '7.6pt';
      }
      return '6.4pt';
    },
    stampFor: function(stepKey) {
      var history = detail.history;
      var i;
      if (stepKey === STEPS.APPLICANT) {
        for (i = history.length - 1; i >= 0; i--) {
          if (history[i].action === ACTION.SUBMIT || history[i].action === ACTION.RESUBMIT) {
            return { name: history[i].actorName || history[i].actorEmail, date: history[i].happenedAt, kind: 'apply' };
          }
        }
        return null;
      }
      for (i = history.length - 1; i >= 0; i--) {
        if (history[i].fromStep === stepKey &&
          (history[i].action === ACTION.APPROVE || history[i].action === ACTION.COMPLETE || history[i].action === ACTION.RETURN || history[i].action === ACTION.QUOTE)) {
          return { name: history[i].actorName || history[i].actorEmail, date: history[i].happenedAt, kind: history[i].action === ACTION.RETURN ? 'ng' : 'approve' };
        }
      }
      return null;
    },
    stampSteps: [STEPS.APPLICANT].concat(detail.request.route || []),
    over: (detail.request.route || []).indexOf(STEPS.PRESIDENT) !== -1,
    barcode: function(seed) {
      var h = 7;
      var s = String(seed);
      var out = '';
      for (var i = 0; i < 46; i++) {
        h = (h * 31 + (s.charCodeAt(i % s.length) || 13)) >>> 0;
        var w = (1 + (h % 3)) * 1.1;
        out += '<i style="width:' + w.toFixed(2) + 'px;opacity:' + (i % 7 === 0 ? '0.35' : '1') + '"></i>';
      }
      return out;
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
  // PDFは購買到達時（IN_REVIEW/PURCHASING）にも生成するため、履歴は実際の状態を記録する。
  addHistory_({
    requestId: requestId,
    actorEmail: actor ? actor.email : '',
    actorName: actor ? actor.name : '',
    action: ACTION.PDF_GENERATE,
    fromStatus: detail.request.status,
    toStatus: detail.request.status,
    fromStep: detail.request.currentStep,
    toStep: detail.request.currentStep,
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
    stepTitles: resolveStepTitles_(request, history),
    generatedAt: nowString_(),
    thresholdAmount: getThresholdAmount_()
  };
}
