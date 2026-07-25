// ===== 実行内キャッシュ（1回のGAS実行＝1リクエストの間だけ有効） =====
// GASは google.script.run / トリガー1回ごとに新しい実行コンテキストになるため、
// モジュール変数のキャッシュは「その1リクエスト内」でのみ共有され、リクエストをまたいで残らない。
var _ssCache = null;        // スプレッドシートハンドル
var _schemaEnsured = false; // ensureSchema_ を実行済みか（1実行1回）
var _readCache = {};        // sheetName -> 行オブジェクト配列
var _settingsCache = null;  // 設定（key->value）

// 書き込み後はキャッシュを破棄して、同一実行内の後続読込が最新を見るようにする。
function invalidateCache_() {
  _readCache = {};
  _settingsCache = null;
}

function getSpreadsheet_() {
  if (_ssCache) {
    return _ssCache;
  }
  var properties = PropertiesService.getScriptProperties();
  var spreadsheetId = properties.getProperty(APP.PROP_SPREADSHEET_ID);
  var spreadsheet;

  if (spreadsheetId) {
    spreadsheet = SpreadsheetApp.openById(spreadsheetId);
  } else {
    // 保存先が未設定のとき: コンテナ（バインド先のスプレッドシート）があればそれを使う。
    // 孤立した別シートを勝手に作らない（過去にデータが別シートへ逸れた事象の再発防止）。
    // スタンドアロン運用などでコンテナが無い場合のみ新規作成する。
    spreadsheet = SpreadsheetApp.getActiveSpreadsheet() || SpreadsheetApp.create(APP.NAME + ' Data');
    properties.setProperty(APP.PROP_SPREADSHEET_ID, spreadsheet.getId());
  }

  if (!_schemaEnsured) {
    ensureSchema_(spreadsheet);
    _schemaEnsured = true;
  }
  _ssCache = spreadsheet;
  return spreadsheet;
}

function ensureSchema_(spreadsheet) {
  ensureSheet_(spreadsheet, SHEETS.REQUESTS, REQUEST_COLUMNS, REQUEST_HEADERS);
  ensureSheet_(spreadsheet, SHEETS.ITEMS, ITEM_COLUMNS, ITEM_HEADERS);
  ensureSheet_(spreadsheet, SHEETS.APPROVERS, APPROVER_COLUMNS, APPROVER_HEADERS);
  ensureSheet_(spreadsheet, SHEETS.HISTORY, HISTORY_COLUMNS, HISTORY_HEADERS);
  ensureSheet_(spreadsheet, SHEETS.SETTINGS, SETTING_COLUMNS, SETTING_HEADERS);
  ensureSheet_(spreadsheet, SHEETS.WORKERS_CACHE, WORKER_CACHE_COLUMNS, WORKER_CACHE_HEADERS);
  ensureSheet_(spreadsheet, SHEETS.RECIPIENTS, RECIPIENT_COLUMNS, RECIPIENT_HEADERS);
  ensureSheet_(spreadsheet, SHEETS.DEPT_SUPERVISORS, DEPT_SUPERVISOR_COLUMNS, DEPT_SUPERVISOR_HEADERS);
  seedSettings_(spreadsheet);
}

// 新しい日本語タブ名に対応する旧英語タブ名を返す（無ければ空）。
function legacyNameFor_(newName) {
  var legacy = '';
  Object.keys(LEGACY_SHEET_NAMES).forEach(function(oldName) {
    if (LEGACY_SHEET_NAMES[oldName] === newName) {
      legacy = oldName;
    }
  });
  return legacy;
}

// columns: 内部キー（位置ベースの読み書き用・英語）。headers: row1に書く表示ラベル（日本語）。
function ensureSheet_(spreadsheet, name, columns, headers) {
  var labels = headers || columns;
  var sheet = spreadsheet.getSheetByName(name);

  // 旧英語名タブが残っていれば新日本語名へ改名して引き継ぐ（移行前アクセス／デプロイ順序事故の自己修復）。
  if (!sheet) {
    var legacyName = legacyNameFor_(name);
    if (legacyName) {
      var legacy = spreadsheet.getSheetByName(legacyName);
      if (legacy) {
        legacy.setName(name);
        sheet = legacy;
      }
    }
  }
  if (!sheet) {
    sheet = spreadsheet.insertSheet(name);
  }

  var currentMaxColumns = sheet.getMaxColumns();
  if (currentMaxColumns < labels.length) {
    sheet.insertColumnsAfter(currentMaxColumns, labels.length - currentMaxColumns);
  }

  var existing = sheet.getRange(1, 1, 1, labels.length).getValues()[0];
  var needsHeader = existing.join('') === '' || existing.some(function(value, index) {
    return value !== labels[index];
  });

  if (needsHeader) {
    sheet.getRange(1, 1, 1, labels.length).setValues([labels]);
    sheet.setFrozenRows(1);
    sheet.getRange(1, 1, 1, labels.length).setFontWeight('bold');
  }
}

function seedSettings_(spreadsheet) {
  var existing = getSettingsFromSpreadsheet_(spreadsheet);
  var sheet = spreadsheet.getSheetByName(SHEETS.SETTINGS);
  var toAppend = DEFAULT_SETTINGS.filter(function(setting) {
    return !Object.prototype.hasOwnProperty.call(existing, setting.key);
  });
  if (toAppend.length === 0) {
    return;
  }
  var startRow = sheet.getLastRow() + 1;
  var values = toAppend.map(function(setting) {
    return SETTING_COLUMNS.map(function(column) {
      return setting[column] || '';
    });
  });
  sheet.getRange(startRow, 1, values.length, SETTING_COLUMNS.length).setValues(values);
}

function getSheet_(sheetName) {
  return getSpreadsheet_().getSheetByName(sheetName);
}

function readObjects_(sheetName, columns) {
  if (Object.prototype.hasOwnProperty.call(_readCache, sheetName)) {
    return _readCache[sheetName];
  }
  var sheet = getSheet_(sheetName);
  var lastRow = sheet.getLastRow();
  var result;
  if (lastRow < 2) {
    result = [];
  } else {
    var values = sheet.getRange(2, 1, lastRow - 1, columns.length).getValues();
    result = values.map(function(row, index) {
      var object = { _rowNumber: index + 2 };
      columns.forEach(function(column, columnIndex) {
        object[column] = normalizeCellValue_(row[columnIndex]);
      });
      return object;
    });
  }
  _readCache[sheetName] = result;
  return result;
}

function appendObject_(sheetName, object, columns) {
  var sheet = getSheet_(sheetName);
  var values = columns.map(function(column) {
    return Object.prototype.hasOwnProperty.call(object, column) ? object[column] : '';
  });
  sheet.appendRow(values);
  invalidateCache_();
  return object;
}

// 複数行を1回の setValues でまとめて追記する（appendRow ループより高速）。
function appendRows_(sheetName, objects, columns) {
  if (!objects || objects.length === 0) {
    return;
  }
  var sheet = getSheet_(sheetName);
  var startRow = sheet.getLastRow() + 1;
  var values = objects.map(function(object) {
    return columns.map(function(column) {
      return Object.prototype.hasOwnProperty.call(object, column) ? object[column] : '';
    });
  });
  sheet.getRange(startRow, 1, values.length, columns.length).setValues(values);
  invalidateCache_();
}

function updateObjectById_(sheetName, columns, idColumn, idValue, patch) {
  var rows = readObjects_(sheetName, columns);
  var row = rows.find(function(candidate) {
    return String(candidate[idColumn]) === String(idValue);
  });

  if (!row) {
    throw new Error('更新対象が見つかりません: ' + idValue);
  }

  var merged = {};
  columns.forEach(function(column) {
    merged[column] = Object.prototype.hasOwnProperty.call(patch, column) ? patch[column] : row[column];
  });

  getSheet_(sheetName)
    .getRange(row._rowNumber, 1, 1, columns.length)
    .setValues([columns.map(function(column) {
      return merged[column];
    })]);

  invalidateCache_();
  merged._rowNumber = row._rowNumber;
  return merged;
}

function deleteObjectsByColumn_(sheetName, columns, columnName, value) {
  var rows = readObjects_(sheetName, columns);
  var sheet = getSheet_(sheetName);
  var matched = rows
    .filter(function(row) {
      return String(row[columnName]) === String(value);
    })
    .sort(function(a, b) {
      return b._rowNumber - a._rowNumber;
    });
  if (matched.length === 0) {
    return;
  }
  matched.forEach(function(row) {
    sheet.deleteRow(row._rowNumber);
  });
  invalidateCache_();
}

function getSettings_() {
  if (_settingsCache) {
    return _settingsCache;
  }
  _settingsCache = getSettingsFromSpreadsheet_(getSpreadsheet_());
  return _settingsCache;
}

function getSettingsFromSpreadsheet_(spreadsheet) {
  var settingsSheet = spreadsheet.getSheetByName(SHEETS.SETTINGS);
  if (!settingsSheet || settingsSheet.getLastRow() < 2) {
    return {};
  }

  var values = settingsSheet.getRange(2, 1, settingsSheet.getLastRow() - 1, SETTING_COLUMNS.length).getValues();
  return values.reduce(function(settings, row) {
    if (row[0]) {
      settings[String(row[0])] = normalizeCellValue_(row[1]);
    }
    return settings;
  }, {});
}

function saveSettings_(input) {
  var allowedKeys = DEFAULT_SETTINGS.map(function(setting) {
    return setting.key;
  });
  var sheet = getSheet_(SHEETS.SETTINGS);
  var rows = readObjects_(SHEETS.SETTINGS, SETTING_COLUMNS);
  var byKey = rows.reduce(function(map, row) {
    map[row.key] = row;
    return map;
  }, {});

  allowedKeys.forEach(function(key) {
    if (!Object.prototype.hasOwnProperty.call(input, key)) {
      return;
    }

    var defaultSetting = DEFAULT_SETTINGS.find(function(setting) {
      return setting.key === key;
    });
    var value = String(input[key] === null || input[key] === undefined ? '' : input[key]).trim();

    if (byKey[key]) {
      sheet
        .getRange(byKey[key]._rowNumber, 1, 1, SETTING_COLUMNS.length)
        .setValues([[key, value, defaultSetting.description]]);
    } else {
      appendObject_(SHEETS.SETTINGS, { key: key, value: value, description: defaultSetting.description }, SETTING_COLUMNS);
    }
  });
  invalidateCache_();
}

function replaceItems_(requestId, items) {
  deleteObjectsByColumn_(SHEETS.ITEMS, ITEM_COLUMNS, 'requestId', requestId);
  var objects = items.map(function(item, index) {
    return {
      itemId: createId_('ITEM'),
      requestId: requestId,
      lineNo: index + 1,
      name: item.name,
      model: item.model,
      maker: item.maker,
      quantity: item.quantity,
      unitPrice: item.unitPrice,
      amount: item.amount,
      desiredDeliveryDate: item.desiredDeliveryDate,
      note: item.note,
      unit: item.unit || ''
    };
  });
  appendRows_(SHEETS.ITEMS, objects, ITEM_COLUMNS);
}

function addHistory_(input) {
  appendObject_(SHEETS.HISTORY, {
    historyId: createId_('HIS'),
    requestId: input.requestId,
    happenedAt: nowString_(),
    actorEmail: input.actorEmail || '',
    actorName: input.actorName || '',
    action: input.action,
    fromStatus: input.fromStatus || '',
    toStatus: input.toStatus || '',
    fromStep: input.fromStep || '',
    toStep: input.toStep || '',
    comment: input.comment || ''
  }, HISTORY_COLUMNS);
}

function normalizeCellValue_(value) {
  if (value instanceof Date) {
    return Utilities.formatDate(value, APP.TIME_ZONE, 'yyyy-MM-dd');
  }
  if (value === null || value === undefined) {
    return '';
  }
  return value;
}
