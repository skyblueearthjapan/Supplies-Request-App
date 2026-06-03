// DeptSupervisors.gs - 部署別 上席マスタ（部署ごとに複数の上席を許可し、誰でも承認できる）

function getDeptSupervisors_() {
  return readObjects_(SHEETS.DEPT_SUPERVISORS, DEPT_SUPERVISOR_COLUMNS)
    .map(function(row) {
      return {
        department: sanitizeText_(row.department, 100),
        email: normalizeEmail_(row.email),
        name: sanitizeText_(row.name, 100),
        title: sanitizeText_(row.title, 40),
        active: row.active === '' ? true : parseBoolean_(row.active)
      };
    });
}

// 申請者・部署に対応する上席集合を解決する。
// 優先順位: DeptSupervisors の部署一致 → '*'（全社）→ ApproverMaster の上席へフォールバック。
function resolveSupervisors_(applicantEmail, department) {
  var normalizedDepartment = sanitizeText_(department, 100);
  var active = getDeptSupervisors_().filter(function(row) {
    return row.active && row.email;
  });

  var matched = active.filter(function(row) {
    return row.department === normalizedDepartment;
  });
  if (matched.length === 0) {
    matched = active.filter(function(row) {
      return row.department === '*';
    });
  }

  if (matched.length > 0) {
    return matched.map(function(row) {
      return { email: row.email, name: row.name || '上席', title: row.title || '' };
    });
  }

  // フォールバック: 承認者マスタの上席（単一）。
  var rule = findApproverRule_(applicantEmail, department);
  if (rule && rule.supervisorEmail) {
    return [{ email: normalizeEmail_(rule.supervisorEmail), name: rule.supervisorName || '上席', title: rule.supervisorTitle || '' }];
  }
  return [];
}

function isSupervisorFor_(email, applicantEmail, department) {
  var target = normalizeEmail_(email);
  return resolveSupervisors_(applicantEmail, department).some(function(s) {
    return s.email === target;
  });
}

// 指定メールの上席の役職を、申請者・部署のコンテキストで解決する（印影下段用）。
function supervisorTitleByEmail_(email, applicantEmail, department) {
  var target = normalizeEmail_(email);
  var hit = resolveSupervisors_(applicantEmail, department).filter(function(s) {
    return s.email === target;
  })[0];
  return hit ? (hit.title || '') : '';
}

// 管理者による全置換保存。saveApproverMaster の delete-descending-then-append を踏襲する。
function saveDeptSupervisors_(rows) {
  var normalizedRows = (rows || [])
    .map(function(row) {
      var input = row || {};
      return {
        department: sanitizeText_(input.department, 100),
        email: normalizeEmail_(input.email),
        name: sanitizeText_(input.name, 100),
        active: String(input.active === '' || input.active === undefined ? true : parseBoolean_(input.active))
      };
    })
    .filter(function(row) {
      return row.department && row.email;
    });

  var existing = readObjects_(SHEETS.DEPT_SUPERVISORS, DEPT_SUPERVISOR_COLUMNS);
  var sheet = getSheet_(SHEETS.DEPT_SUPERVISORS);
  existing
    .sort(function(a, b) {
      return b._rowNumber - a._rowNumber;
    })
    .forEach(function(row) {
      sheet.deleteRow(row._rowNumber);
    });

  normalizedRows.forEach(function(row) {
    appendObject_(SHEETS.DEPT_SUPERVISORS, row, DEPT_SUPERVISOR_COLUMNS);
  });
}

// ===== 外部 DeptApprovers シートからの同期（作業員マスタ同様の読み取り専用ミラー） =====

// 管理者による手動同期（管理画面の「外部シートから同期」ボタン用）。
function syncDeptSupervisorsNow() {
  assertAdmin_(getCurrentUser_());
  return syncDeptSupervisorsMaster();
}

// 外部スプレッドシートの DeptApprovers シートを DeptSupervisors キャッシュへ取り込む。
// 想定列: 部署 / 承認者メール（カンマ区切り可）/ 氏名（任意）/ 有効フラグ（任意）。
// ヘッダ名で列を判定し、無ければ位置（A=部署, B=メール, C=氏名）で読む。
function syncDeptSupervisorsMaster() {
  var sheet = getDeptApproverSheet_();
  if (!sheet) {
    Logger.log('syncDeptSupervisorsMaster: source sheet not found (' + DEPT_APPROVER_SHEET_NAME + ')');
    return { count: 0, error: 'source sheet not found' };
  }

  var lastRow = sheet.getLastRow();
  var lastCol = Math.max(1, sheet.getLastColumn());
  if (lastRow < 2) {
    Logger.log('syncDeptSupervisorsMaster: source empty, keeping existing cache');
    return { count: 0, skipped: true };
  }

  var values = sheet.getRange(1, 1, lastRow, lastCol).getValues();
  var header = values[0].map(function(h) {
    return String(h === null || h === undefined ? '' : h).trim().toLowerCase();
  });
  var idx = {
    dept: findHeaderIndex_(header, ['部署', 'dept', 'department']),
    email: findHeaderIndex_(header, ['承認者メール', 'メール', 'mail', 'email', 'approveremail', 'approver']),
    name: findHeaderIndex_(header, ['氏名', '名前', 'approvername', 'name']),
    title: findHeaderIndex_(header, ['役職', 'title', 'position']),
    active: findHeaderIndex_(header, ['有効', 'active', 'enabled'])
  };
  if (idx.dept < 0) { idx.dept = 0; }
  if (idx.email < 0) { idx.email = 1; }

  var rows = [];
  for (var r = 1; r < values.length; r++) {
    var row = values[r];
    var dept = sanitizeText_(row[idx.dept], 100);
    if (!dept) { continue; }
    if (idx.active >= 0) {
      var activeRaw = String(row[idx.active] === null || row[idx.active] === undefined ? '' : row[idx.active]).trim().toLowerCase();
      if (activeRaw === 'false' || activeRaw === '0' || activeRaw === 'no' || activeRaw === '×' || activeRaw === '無効') {
        continue;
      }
    }
    var name = idx.name >= 0 ? sanitizeText_(row[idx.name], 100) : '';
    var title = idx.title >= 0 ? sanitizeText_(row[idx.title], 40) : '';
    var emailsRaw = String(row[idx.email] === null || row[idx.email] === undefined ? '' : row[idx.email]);
    emailsRaw.split(/[,;\n]/).forEach(function(part) {
      var email = normalizeEmail_(part);
      if (email) {
        // 列順 [department, email, name, active, title]
        rows.push([dept, email, name, 'true', title]);
      }
    });
  }

  if (rows.length === 0) {
    Logger.log('syncDeptSupervisorsMaster: no valid rows, keeping existing cache');
    return { count: 0, skipped: true };
  }

  writeDeptSupervisorsCache_(rows);
  Logger.log('syncDeptSupervisorsMaster: ' + rows.length + ' rows synced');
  return { count: rows.length };
}

function findHeaderIndex_(header, candidates) {
  for (var i = 0; i < header.length; i++) {
    for (var c = 0; c < candidates.length; c++) {
      if (header[i].indexOf(String(candidates[c]).toLowerCase()) !== -1) {
        return i;
      }
    }
  }
  return -1;
}

function getDeptApproverSheet_() {
  var ss = SpreadsheetApp.openById(DEPT_APPROVER_SPREADSHEET_ID);
  var sheet = ss.getSheetByName(DEPT_APPROVER_SHEET_NAME);
  if (sheet) {
    return sheet;
  }
  // 名称ゆれ対策（DeptApprovers / ApproverMap など）。
  var sheets = ss.getSheets();
  for (var i = 0; i < sheets.length; i++) {
    var n = String(sheets[i].getName() || '').toLowerCase();
    if (n.indexOf('approver') !== -1) {
      return sheets[i];
    }
  }
  return null;
}

function writeDeptSupervisorsCache_(rows) {
  var sheet = getSheet_(SHEETS.DEPT_SUPERVISORS);
  var cacheLastRow = sheet.getLastRow();
  if (cacheLastRow >= 2) {
    sheet.getRange(2, 1, cacheLastRow - 1, DEPT_SUPERVISOR_COLUMNS.length).clearContent();
  }
  if (rows.length > 0) {
    sheet.getRange(2, 1, rows.length, DEPT_SUPERVISOR_COLUMNS.length).setValues(rows);
  }
}

function ensureDeptSupervisorSyncTrigger_() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'syncDeptSupervisorsMaster') {
      return false;
    }
  }
  ScriptApp.newTrigger('syncDeptSupervisorsMaster')
    .timeBased()
    .atHour(8)
    .everyDays(1)
    .inTimezone(APP.TIME_ZONE)
    .create();
  return true;
}
