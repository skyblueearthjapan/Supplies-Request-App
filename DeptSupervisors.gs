// DeptSupervisors.gs - 部署別 上長マスタ（部署ごとに複数の上長を許可し、誰でも承認できる）

function getDeptSupervisors_() {
  return readObjects_(SHEETS.DEPT_SUPERVISORS, DEPT_SUPERVISOR_COLUMNS)
    .map(function(row) {
      return {
        department: sanitizeText_(row.department, 100),
        email: normalizeEmail_(row.email),
        name: sanitizeText_(row.name, 100),
        active: row.active === '' ? true : parseBoolean_(row.active)
      };
    });
}

// 申請者・部署に対応する上長集合を解決する。
// 優先順位: DeptSupervisors の部署一致 → '*'（全社）→ ApproverMaster の上長へフォールバック。
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
      return { email: row.email, name: row.name || '上長' };
    });
  }

  // フォールバック: 承認者マスタの上長（単一）。
  var rule = findApproverRule_(applicantEmail, department);
  if (rule && rule.supervisorEmail) {
    return [{ email: normalizeEmail_(rule.supervisorEmail), name: rule.supervisorName || '上長' }];
  }
  return [];
}

function isSupervisorFor_(email, applicantEmail, department) {
  var target = normalizeEmail_(email);
  return resolveSupervisors_(applicantEmail, department).some(function(s) {
    return s.email === target;
  });
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
