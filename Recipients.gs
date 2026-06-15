// Recipients.gs - 通知宛先マスタ（総務部 / 購買）

function getRecipients_() {
  return readObjects_(SHEETS.RECIPIENTS, RECIPIENT_COLUMNS)
    .filter(function(row) {
      return row.email;
    })
    .map(function(row) {
      return {
        type: sanitizeText_(row.type, 50),
        email: normalizeEmail_(row.email),
        name: sanitizeText_(row.name, 100),
        active: row.active === '' ? true : parseBoolean_(row.active),
        sendAs: String(row.sendAs).toUpperCase() === 'CC' ? 'CC' : 'TO'
      };
    });
}

function getRecipientEmails_(type) {
  var seen = {};
  return getRecipients_()
    .filter(function(row) {
      return row.active && row.type === type && row.email;
    })
    .map(function(row) {
      return row.email;
    })
    .filter(function(email) {
      if (seen[email]) {
        return false;
      }
      seen[email] = true;
      return true;
    });
}

// 指定種別の有効な TO 宛先の表示名一覧（重複除去・表示名未設定はスキップ）。
// メール本文の宛名生成に使う。CC 宛先は宛名に含めないため対象外。
function getRecipientToNames_(type) {
  var seen = {};
  return getRecipients_()
    .filter(function(row) {
      return row.active && row.type === type && row.email && row.sendAs === 'TO' && row.name;
    })
    .filter(function(row) {
      if (seen[row.email]) {
        return false;
      }
      seen[row.email] = true;
      return true;
    })
    .map(function(row) {
      return row.name;
    });
}

// 指定種別の有効宛先を To / CC に振り分けて返す（重複除去・同一メールは先勝ち）。
function getRecipientsSplit_(type) {
  var to = [];
  var cc = [];
  var seen = {};
  getRecipients_()
    .filter(function(row) {
      return row.active && row.type === type && row.email;
    })
    .forEach(function(row) {
      if (seen[row.email]) {
        return;
      }
      seen[row.email] = true;
      if (row.sendAs === 'CC') {
        cc.push(row.email);
      } else {
        to.push(row.email);
      }
    });
  return { to: to, cc: cc };
}

function saveRecipients_(rows) {
  var normalizedRows = (rows || [])
    .map(function(row) {
      var input = row || {};
      var type = sanitizeText_(input.type, 50);
      if (!Object.prototype.hasOwnProperty.call(RECIPIENT_TYPES, type)) {
        throw new Error('通知宛先の区分が不正です: ' + type);
      }
      var email = normalizeEmail_(input.email);
      if (!email) {
        throw new Error('通知宛先のメールアドレスを入力してください。');
      }
      return {
        type: type,
        email: email,
        name: sanitizeText_(input.name, 100),
        active: String(input.active === '' || input.active === undefined ? true : parseBoolean_(input.active)),
        sendAs: String(input.sendAs).toUpperCase() === 'CC' ? 'CC' : 'TO'
      };
    });

  var existing = readObjects_(SHEETS.RECIPIENTS, RECIPIENT_COLUMNS);
  var sheet = getSheet_(SHEETS.RECIPIENTS);
  existing
    .sort(function(a, b) {
      return b._rowNumber - a._rowNumber;
    })
    .forEach(function(row) {
      sheet.deleteRow(row._rowNumber);
    });
  invalidateCache_();

  appendRows_(SHEETS.RECIPIENTS, normalizedRows, RECIPIENT_COLUMNS);
}
