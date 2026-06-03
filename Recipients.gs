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
        active: row.active === '' ? true : parseBoolean_(row.active)
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
        active: String(input.active === '' || input.active === undefined ? true : parseBoolean_(input.active))
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
