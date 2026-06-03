function getSpreadsheet_() {
  var properties = PropertiesService.getScriptProperties();
  var spreadsheetId = properties.getProperty(APP.PROP_SPREADSHEET_ID);
  var spreadsheet;

  if (spreadsheetId) {
    spreadsheet = SpreadsheetApp.openById(spreadsheetId);
  } else {
    spreadsheet = SpreadsheetApp.create(APP.NAME + ' Data');
    properties.setProperty(APP.PROP_SPREADSHEET_ID, spreadsheet.getId());
  }

  ensureSchema_(spreadsheet);
  return spreadsheet;
}

function ensureSchema_(spreadsheet) {
  ensureSheet_(spreadsheet, SHEETS.REQUESTS, REQUEST_COLUMNS);
  ensureSheet_(spreadsheet, SHEETS.ITEMS, ITEM_COLUMNS);
  ensureSheet_(spreadsheet, SHEETS.APPROVERS, APPROVER_COLUMNS);
  ensureSheet_(spreadsheet, SHEETS.HISTORY, HISTORY_COLUMNS);
  ensureSheet_(spreadsheet, SHEETS.SETTINGS, SETTING_COLUMNS);
  ensureSheet_(spreadsheet, SHEETS.WORKERS_CACHE, WORKER_CACHE_COLUMNS);
  ensureSheet_(spreadsheet, SHEETS.RECIPIENTS, RECIPIENT_COLUMNS);
  ensureSheet_(spreadsheet, SHEETS.DEPT_SUPERVISORS, DEPT_SUPERVISOR_COLUMNS);
  seedSettings_(spreadsheet);
}

function ensureSheet_(spreadsheet, name, columns) {
  var sheet = spreadsheet.getSheetByName(name) || spreadsheet.insertSheet(name);
  var currentMaxColumns = sheet.getMaxColumns();
  if (currentMaxColumns < columns.length) {
    sheet.insertColumnsAfter(currentMaxColumns, columns.length - currentMaxColumns);
  }

  var existing = sheet.getRange(1, 1, 1, columns.length).getValues()[0];
  var needsHeader = existing.join('') === '' || existing.some(function(value, index) {
    return value !== columns[index];
  });

  if (needsHeader) {
    sheet.getRange(1, 1, 1, columns.length).setValues([columns]);
    sheet.setFrozenRows(1);
    sheet.getRange(1, 1, 1, columns.length).setFontWeight('bold');
  }
}

function seedSettings_(spreadsheet) {
  var existing = getSettingsFromSpreadsheet_(spreadsheet);
  var sheet = spreadsheet.getSheetByName(SHEETS.SETTINGS);
  DEFAULT_SETTINGS.forEach(function(setting) {
    if (!Object.prototype.hasOwnProperty.call(existing, setting.key)) {
      sheet.appendRow(SETTING_COLUMNS.map(function(column) {
        return setting[column] || '';
      }));
    }
  });
}

function getSheet_(sheetName) {
  return getSpreadsheet_().getSheetByName(sheetName);
}

function readObjects_(sheetName, columns) {
  var sheet = getSheet_(sheetName);
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    return [];
  }

  var values = sheet.getRange(2, 1, lastRow - 1, columns.length).getValues();
  return values.map(function(row, index) {
    var object = { _rowNumber: index + 2 };
    columns.forEach(function(column, columnIndex) {
      object[column] = normalizeCellValue_(row[columnIndex]);
    });
    return object;
  });
}

function appendObject_(sheetName, object, columns) {
  var sheet = getSheet_(sheetName);
  var values = columns.map(function(column) {
    return Object.prototype.hasOwnProperty.call(object, column) ? object[column] : '';
  });
  sheet.appendRow(values);
  return object;
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

  merged._rowNumber = row._rowNumber;
  return merged;
}

function deleteObjectsByColumn_(sheetName, columns, columnName, value) {
  var rows = readObjects_(sheetName, columns);
  var sheet = getSheet_(sheetName);
  rows
    .filter(function(row) {
      return String(row[columnName]) === String(value);
    })
    .sort(function(a, b) {
      return b._rowNumber - a._rowNumber;
    })
    .forEach(function(row) {
      sheet.deleteRow(row._rowNumber);
    });
}

function getSettings_() {
  return getSettingsFromSpreadsheet_(getSpreadsheet_());
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
}

function replaceItems_(requestId, items) {
  deleteObjectsByColumn_(SHEETS.ITEMS, ITEM_COLUMNS, 'requestId', requestId);
  items.forEach(function(item, index) {
    appendObject_(SHEETS.ITEMS, {
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
      note: item.note
    }, ITEM_COLUMNS);
  });
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
