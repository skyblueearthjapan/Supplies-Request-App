// ===== 実行内キャッシュ（1回のGAS実行＝1リクエストの間だけ有効） =====
// GASは google.script.run / トリガー1回ごとに新しい実行コンテキストになるため、
// モジュール変数のキャッシュは「その1リクエスト内」でのみ共有され、リクエストをまたいで残らない。
var _ssCache = null;        // スプレッドシートハンドル
var _schemaEnsured = false; // ensureSchema_ を実行済みか（1実行1回）
var _readCache = {};        // sheetName -> 行オブジェクト配列
var _settingsCache = null;  // 設定（key->value）

// 書き込み後はキャッシュを破棄して、同一実行内の後続読込が最新を見るようにする。
// あわせてデータ版数を進め、リクエストをまたぐ共有キャッシュ（下記）を無効にする。
function invalidateCache_() {
  _readCache = {};
  _settingsCache = null;
  _wroteInThisExecution = true;
  _sharedCacheContext = null;
  // 溜め置きされた書き込みをシートへ確定させてから版数を進める（逆だと、新しい版数で古いシートを読まれうる）。
  SpreadsheetApp.flush();
  bumpDataVersion_();
}

// シートを手で直したとき・Migration.gs の道具を使った後に、GASエディタから実行する。
// 共有キャッシュを捨てて、次の表示からシートの最新を読ませる。
function clearSharedCache() {
  assertAdmin_(getCurrentUser_());
  bumpDataVersion_();
  Logger.log('shared cache cleared');
}

// ===== リクエストをまたぐ共有キャッシュ（画面表示用の読み取りに限って使う） =====
// スプレッドシートは1回読むだけで0.15〜1.5秒かかるため、読み取り結果を CacheService に置く。
// - 使うのは useSharedReadCache_() を呼んだ読み取り専用の公開関数（一覧・件数・詳細・初期表示）だけ。
//   書き込みを伴う処理は、行番号のずれや古い値の書き戻しを避けるため必ずシートを直接読む。
// - アプリが書き込むたびに invalidateCache_ がデータ版数を進めるので、アプリ経由の変更は即座に反映される。
// - シートを手で直接編集した場合だけ、最大 SHARED_CACHE_SECONDS の間は古い表示が残る。
var SHARED_CACHE_SECONDS = 300;
var SHARED_CACHE_CHUNK = 90000; // CacheService の1値上限（100KB）未満に分割
var SHARED_CACHE_MAX_CHUNKS = 20;
var PROP_DATA_VERSION = 'DATA_VERSION';
var _sharedCacheEnabled = false;
var _wroteInThisExecution = false;
var _sharedCacheContext = null; // { spreadsheetId, version }

// 読み取り専用の公開関数の先頭で呼ぶ。書き込み処理の中から呼ばれた場合（末尾の return getRequestDetail など）は、
// 書き込み済みなら _wroteInThisExecution により共有キャッシュは使われない。
// 実行の入口（まだ何も読み書きしていない状態）でしか有効にしない。書き込み処理の途中で呼ばれても、
// その後の読み取りが共有キャッシュに切り替わって古い行番号で書き戻す、という事故を防ぐ。
function useSharedReadCache_() {
  if (!_wroteInThisExecution && Object.keys(_readCache).length === 0 && !_settingsCache) {
    _sharedCacheEnabled = true;
  }
}

function sharedCacheContext_() {
  if (!_sharedCacheEnabled || _wroteInThisExecution) {
    return null;
  }
  if (_sharedCacheContext) {
    return _sharedCacheContext;
  }
  try {
    var props = PropertiesService.getScriptProperties().getProperties();
    var spreadsheetId = props[APP.PROP_SPREADSHEET_ID];
    if (!spreadsheetId) {
      return null; // 初回セットアップ前は使わない
    }
    var version = props[PROP_DATA_VERSION];
    if (!version) {
      version = newDataVersion_();
      PropertiesService.getScriptProperties().setProperty(PROP_DATA_VERSION, version);
    }
    _sharedCacheContext = { spreadsheetId: spreadsheetId, version: version };
    return _sharedCacheContext;
  } catch (error) {
    Logger.log('shared cache context failed: ' + error.message);
    return null;
  }
}

function newDataVersion_() {
  return Date.now().toString(36) + Math.floor(Math.random() * 1e6).toString(36);
}

function bumpDataVersion_() {
  try {
    PropertiesService.getScriptProperties().setProperty(PROP_DATA_VERSION, newDataVersion_());
  } catch (error) {
    // 失敗しても共有キャッシュは SHARED_CACHE_SECONDS で消えるので、業務の処理は止めない。
    Logger.log('bumpDataVersion_ failed: ' + error.message);
  }
}

// 列の並びや既定設定キーが違うコード（別バージョンの配備）とキャッシュを共有しないよう、
// 見出し定義の要約（schemaCacheKey_ と同じもの）もキーに入れる。
function sharedCacheKey_(context, name) {
  if (!context.schemaKey) {
    context.schemaKey = schemaCacheKey_(context.spreadsheetId);
  }
  return 'rows:' + context.schemaKey + ':' + context.version + ':' +
    Utilities.base64EncodeWebSafe(name, Utilities.Charset.UTF_8);
}

function readSharedCache_(name) {
  var context = sharedCacheContext_();
  if (!context) {
    return null;
  }
  try {
    var cache = CacheService.getScriptCache();
    var base = sharedCacheKey_(context, name);
    var count = Number(cache.get(base));
    if (!count) {
      return null;
    }
    var keys = [];
    for (var i = 0; i < count; i++) {
      keys.push(base + ':' + i);
    }
    var parts = cache.getAll(keys);
    var packed = '';
    for (var j = 0; j < keys.length; j++) {
      if (!parts[keys[j]]) {
        return null; // 一部だけ追い出された
      }
      packed += parts[keys[j]];
    }
    var json = Utilities.ungzip(Utilities.newBlob(Utilities.base64Decode(packed), 'application/x-gzip'))
      .getDataAsString('UTF-8');
    return JSON.parse(json);
  } catch (error) {
    Logger.log('readSharedCache_ failed (' + name + '): ' + error.message);
    return null;
  }
}

function writeSharedCache_(name, value) {
  var context = sharedCacheContext_();
  if (!context) {
    return;
  }
  try {
    var packed = Utilities.base64Encode(
      Utilities.gzip(Utilities.newBlob(JSON.stringify(value), 'application/json')).getBytes());
    var chunkCount = Math.ceil(packed.length / SHARED_CACHE_CHUNK) || 1;
    if (chunkCount > SHARED_CACHE_MAX_CHUNKS) {
      return; // 大きすぎるものはキャッシュしない（毎回シートから読む）
    }
    var base = sharedCacheKey_(context, name);
    var entries = {};
    for (var i = 0; i < chunkCount; i++) {
      entries[base + ':' + i] = packed.slice(i * SHARED_CACHE_CHUNK, (i + 1) * SHARED_CACHE_CHUNK);
    }
    entries[base] = String(chunkCount);
    CacheService.getScriptCache().putAll(entries, SHARED_CACHE_SECONDS);
  } catch (error) {
    Logger.log('writeSharedCache_ failed (' + name + '): ' + error.message);
  }
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
    // 見出し確認は8シート分のAPI呼び出しになり、毎リクエスト数秒かかっていた。
    // 確認済みの印をスクリプトキャッシュに置き、シート構成が変わらない間は省略する。
    if (!schemaMarkedOk_(spreadsheet.getId())) {
      ensureSchema_(spreadsheet);
      markSchemaOk_(spreadsheet.getId());
    }
    _schemaEnsured = true;
  }
  _ssCache = spreadsheet;
  return spreadsheet;
}

var SCHEMA_CACHE_SECONDS = 21600; // CacheService の上限（6時間）

// 見出し定義が変わったら（デプロイで列を追加したら）別キーになり、初回に必ず ensureSchema_ が走る。
function schemaCacheKey_(spreadsheetId) {
  var signature = [
    REQUEST_HEADERS, ITEM_HEADERS, APPROVER_HEADERS, HISTORY_HEADERS, SETTING_HEADERS,
    WORKER_CACHE_HEADERS, RECIPIENT_HEADERS, DEPT_SUPERVISOR_HEADERS
  ].map(function(headers) {
    return headers.join('|');
  }).join('#') + '#' + DEFAULT_SETTINGS.map(function(setting) {
    return setting.key;
  }).join('|');
  var digest = Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, signature, Utilities.Charset.UTF_8);
  return 'schemaOk:' + spreadsheetId + ':' + Utilities.base64EncodeWebSafe(digest);
}

// キャッシュは高速化のためだけに使う。読み書きに失敗しても業務の処理は止めない（読めなければ確認する）。
function schemaMarkedOk_(spreadsheetId) {
  try {
    return !!CacheService.getScriptCache().get(schemaCacheKey_(spreadsheetId));
  } catch (error) {
    Logger.log('schema cache get failed: ' + error.message);
    return false;
  }
}

function markSchemaOk_(spreadsheetId) {
  try {
    CacheService.getScriptCache().put(schemaCacheKey_(spreadsheetId), '1', SCHEMA_CACHE_SECONDS);
  } catch (error) {
    Logger.log('schema cache put failed: ' + error.message);
  }
}

// キャッシュ中に誰かがシートを手で消した・改名した場合の自己修復（従来は毎回 ensureSchema_ で直っていた）。
function repairSchema_() {
  var spreadsheet = _ssCache || getSpreadsheet_();
  ensureSchema_(spreadsheet);
  markSchemaOk_(spreadsheet.getId());
  invalidateCache_();
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
  var sheet = getSpreadsheet_().getSheetByName(sheetName);
  if (!sheet) {
    repairSchema_();
    sheet = getSpreadsheet_().getSheetByName(sheetName);
  }
  return sheet;
}

function readObjects_(sheetName, columns) {
  if (Object.prototype.hasOwnProperty.call(_readCache, sheetName)) {
    return _readCache[sheetName];
  }
  var cacheName = 'sheet:' + sheetName + ':' + columns.length;
  var values = readSharedCache_(cacheName);
  if (!values) {
    var sheet = getSheet_(sheetName);
    var lastRow = sheet.getLastRow();
    values = lastRow < 2 ? [] : sheet.getRange(2, 1, lastRow - 1, columns.length).getValues()
      .map(function(row) {
        return row.map(normalizeCellValue_);
      });
    writeSharedCache_(cacheName, values);
  }
  var result;
  if (values.length === 0) {
    result = [];
  } else {
    result = values.map(function(row, index) {
      var object = { _rowNumber: index + 2 };
      columns.forEach(function(column, columnIndex) {
        object[column] = row[columnIndex];
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
    return toCellValue_(column, Object.prototype.hasOwnProperty.call(object, column) ? object[column] : '');
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
      return toCellValue_(column, Object.prototype.hasOwnProperty.call(object, column) ? object[column] : '');
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
      return toCellValue_(column, merged[column]);
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
  var shared = readSharedCache_('settings');
  if (shared) {
    _settingsCache = shared;
    return _settingsCache;
  }
  var spreadsheet = getSpreadsheet_();
  var settings = getSettingsFromSpreadsheet_(spreadsheet);
  // 設定タブや設定行（特に adminEmails）が消えていると isAdmin_ が全員を管理者扱いするため、
  // 既定の設定キーが欠けていたらその場で修復して読み直す（ensureSchema_ のキャッシュ中でも直す）。
  var missing = DEFAULT_SETTINGS.some(function(setting) {
    return !Object.prototype.hasOwnProperty.call(settings, setting.key);
  });
  if (missing) {
    repairSchema_();
    settings = getSettingsFromSpreadsheet_(spreadsheet);
  }
  writeSharedCache_('settings', settings);
  _settingsCache = settings;
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

// setValues / appendRow は文字列を手入力と同じく解釈するため、「3/8」（配管サイズ）や
// コメントの「1/2」が日付に、「0123」（型式）が数値に化け、「=」始まりは数式になる。
// 自由入力の列だけ先頭に ' を付けて文字列のまま保存させる（' はセル値に残らない）。
// 日時・数値・enum の列は従来どおり（日時は Date 化を前提に normalizeCellValue_ で読んでいる）。
// 注意: 対象列の表示形式を「書式なしテキスト」に変えると ' が値として残るので、書式は触らないこと。
var FREE_TEXT_COLUMNS = Object.freeze({
  applicantName: true,
  department: true,
  reasonDetail: true,
  name: true,
  model: true,
  maker: true,
  note: true,
  unit: true,
  actorName: true,
  comment: true
});

function toCellValue_(column, value) {
  return FREE_TEXT_COLUMNS[column] === true ? asTextCell_(value) : value;
}

function asTextCell_(value) {
  if (value === null || value === undefined || value === '') {
    return '';
  }
  return "'" + String(value);
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
