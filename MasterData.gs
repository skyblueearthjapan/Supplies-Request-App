// MasterData.gs - 外部作業員マスタからの日次同期キャッシュ
// EXTERNAL_MASTER_SPREADSHEET_ID の指定gidシートを WorkersCache へ複製する。

function syncWorkersMaster() {
  var src = getMasterSheetByGid_(MASTER_WORKER_GID);
  if (!src) {
    Logger.log('syncWorkersMaster: source sheet not found (gid=' + MASTER_WORKER_GID + ')');
    return { count: 0, error: 'source sheet not found' };
  }

  var lastRow = src.getLastRow();
  // グリッド幅を超える getRange は例外になるため実際の列数にクランプ（不足列は undefined → '' 扱い）。
  var lastCol = Math.min(6, Math.max(1, src.getMaxColumns()));
  if (lastRow < 2) {
    // ソースが空（取得失敗・フィルタ表示等の一時的な空を含む）の場合は既存キャッシュを保持する。
    Logger.log('syncWorkersMaster: source empty, keeping existing cache');
    return { count: 0, skipped: true };
  }

  var values = src.getRange(2, 1, lastRow - 1, lastCol).getValues();
  var rows = values
    .filter(function(r) {
      return r[0];
    })
    .map(function(r) {
      return [
        sanitizeText_(r[0], 100),  // A 作業員コード
        sanitizeText_(r[1], 100),  // B 氏名
        sanitizeText_(r[2], 100),  // C 部署
        sanitizeText_(r[4], 100),  // E 拠点
        sanitizeText_(r[5], 100)   // F スタッフ種類
      ];
    });

  if (rows.length === 0) {
    // 有効行ゼロのときも既存キャッシュを消さない。
    Logger.log('syncWorkersMaster: no valid rows, keeping existing cache');
    return { count: 0, skipped: true };
  }

  writeWorkersCache_(rows);
  Logger.log('syncWorkersMaster: ' + rows.length + ' rows synced');
  return { count: rows.length };
}

function writeWorkersCache_(rows) {
  var sheet = getSheet_(SHEETS.WORKERS_CACHE);
  var cacheLastRow = sheet.getLastRow();
  if (cacheLastRow >= 2) {
    sheet.getRange(2, 1, cacheLastRow - 1, WORKER_CACHE_COLUMNS.length).clearContent();
  }
  if (rows.length > 0) {
    sheet.getRange(2, 1, rows.length, WORKER_CACHE_COLUMNS.length).setValues(rows);
  }
}

function getMasterSheetByGid_(gid) {
  var ss = SpreadsheetApp.openById(EXTERNAL_MASTER_SPREADSHEET_ID);
  var sheets = ss.getSheets();
  for (var i = 0; i < sheets.length; i++) {
    if (sheets[i].getSheetId() === gid) {
      return sheets[i];
    }
  }
  return null;
}

function getWorkers_() {
  return readObjects_(SHEETS.WORKERS_CACHE, WORKER_CACHE_COLUMNS)
    .filter(function(row) {
      return row.workerCode;
    })
    .map(function(row) {
      return {
        code: sanitizeText_(row.workerCode, 100),
        name: sanitizeText_(row.name, 100),
        dept: sanitizeText_(row.dept, 100),
        location: sanitizeText_(row.location, 100),
        staffType: sanitizeText_(row.staffType, 100)
      };
    });
}

function ensureMasterSyncTrigger_() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'syncWorkersMaster') {
      return false;
    }
  }
  ScriptApp.newTrigger('syncWorkersMaster')
    .timeBased()
    .atHour(8)
    .everyDays(1)
    .inTimezone(APP.TIME_ZONE)
    .create();
  return true;
}
