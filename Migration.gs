// ============================================================
// データベース移行ツール（1回限りの運用作業用）
//
// 背景: getSpreadsheet_() は DATA_SPREADSHEET_ID（スクリプトプロパティ）が指す
// スプレッドシートを使う。初回にプロパティ未設定だったため、コンテナ（バインド先A）
// ではなく自動生成された別シートBにデータが入ってしまった。
//
// このツールで「現在のDB（B）→ 本来のDB（A・コンテナ）」へ全データを移し、
// 参照先をAへ切り替える。元データ（B）は読み取るだけで削除しない（元に戻せる）。
//
// 使い方（Apps Script エディタ）:
//   1) showCurrentDatabaseId        … 現在の保存先IDを確認（Bになっているはず）
//   2) migrateDatabaseToIntendedSheet … AへコピーしてAに切替（実行ログに結果表示）
//   3) showCurrentDatabaseId        … 保存先がAに変わったことを確認
// ============================================================

// 本来データベースにしたいスプレッドシート（コンテナ／バインド先A）
var INTENDED_DATA_SPREADSHEET_ID = '1VKbB13yhJPkyTYu7VYfHm-Zc3sl0nYp0npXUAxau4p4';

// 現在の保存先（DATA_SPREADSHEET_ID）を表示する。
function showCurrentDatabaseId() {
  var id = PropertiesService.getScriptProperties().getProperty(APP.PROP_SPREADSHEET_ID);
  var msg = '現在のDATA_SPREADSHEET_ID: ' + (id || '(未設定)') +
    (id ? '\nURL: https://docs.google.com/spreadsheets/d/' + id + '/edit' : '');
  Logger.log(msg);
  return msg;
}

// 移行対象のデータシート（申請系＋マスタ系）。移行と行数レポートで共用する。
function migrationSheetNames_() {
  return [
    SHEETS.REQUESTS, SHEETS.ITEMS, SHEETS.APPROVERS, SHEETS.HISTORY,
    SHEETS.SETTINGS, SHEETS.WORKERS_CACHE, SHEETS.RECIPIENTS, SHEETS.DEPT_SUPERVISORS
  ];
}

// 移行の前後で突き合わせるための読み取り専用レポート。何も変更しない。
// 実行前と実行後に流し、各シートの行数が一致することを目視で確認するために使う。
function reportDatabaseRowCounts() {
  var currentId = PropertiesService.getScriptProperties().getProperty(APP.PROP_SPREADSHEET_ID);
  if (!currentId) {
    throw new Error('現在のDATA_SPREADSHEET_IDが未設定です。');
  }
  var current = SpreadsheetApp.openById(currentId);
  var intended = SpreadsheetApp.openById(INTENDED_DATA_SPREADSHEET_ID);
  var rowsOf = function(spreadsheet, name) {
    var sheet = spreadsheet.getSheetByName(name);
    return sheet ? Math.max(0, sheet.getLastRow() - 1) + ' 行' : '（シートなし）';
  };

  var lines = [
    '現在の保存先: ' + currentId,
    '移行先(コンテナ): ' + INTENDED_DATA_SPREADSHEET_ID,
    currentId === INTENDED_DATA_SPREADSHEET_ID ? '※ 既に統一済みです。' : '※ まだ分かれています。',
    '',
    'シート名 : 現在の保存先 / 移行先'
  ];
  migrationSheetNames_().forEach(function(name) {
    lines.push('・' + name + ' : ' + rowsOf(current, name) + ' / ' + rowsOf(intended, name));
  });

  var text = lines.join('\n');
  Logger.log(text);
  return text;
}

// 現在のDB → 本来のDB(A・コンテナ) へ移行して参照先を切り替える。
//
// 【安全装置】INTENDED_DATA_SPREADSHEET_ID は本番コンテナのIDを直書きしている。
// サンドボックス（コピーしたスクリプト）でうっかり実行すると、テストデータで本番の
// コンテナを上書きしてしまう。バインド先が移行先と一致するとき（＝本番）だけ実行を許す。
function migrateDatabaseToIntendedSheet() {
  var container = null;
  try {
    container = SpreadsheetApp.getActiveSpreadsheet();
  } catch (error) {
    container = null;
  }
  if (container && container.getId() !== INTENDED_DATA_SPREADSHEET_ID) {
    throw new Error(
      'このスクリプトのバインド先（' + container.getId() + '）が移行先（' +
      INTENDED_DATA_SPREADSHEET_ID + '）と一致しません。' +
      'サンドボックスから本番コンテナを上書きしようとしている可能性があります。中止しました。'
    );
  }
  if (!container) {
    Logger.log('migrateDatabaseToIntendedSheet: バインド先を判定できませんでした。実行環境を確認してください。');
  }
  return migrateDatabaseToSpreadsheet_(INTENDED_DATA_SPREADSHEET_ID);
}

// 現在のDBの全データシートを target へコピーし、DATA_SPREADSHEET_ID を target に切り替える。
//
// 【ロックの理由】アプリの書き込み（申請・承認・金額確定など）はすべて同じスクリプトロックを
// 取ってから行う。ロックを取らずにコピーすると、コピー済みのシートへ利用者の書き込みが入った直後に
// 参照先を切り替えてしまい、その申請だけが移行先に存在しない＝消えたように見える。
// ロックを取れば、利用者の操作は「コピー前に完了して移行に含まれる」か「切替後に新DBへ書かれる」
// のどちらかになり、取りこぼしが起きない。
function migrateDatabaseToSpreadsheet_(targetId) {
  if (!targetId) {
    throw new Error('移行先スプレッドシートIDが空です。');
  }
  var lock = LockService.getScriptLock();
  lock.waitLock(60000);
  try {
    var props = PropertiesService.getScriptProperties();
    var currentId = props.getProperty(APP.PROP_SPREADSHEET_ID);
    if (!currentId) {
      throw new Error('現在のDATA_SPREADSHEET_IDが未設定です。先にアプリを一度開いてから実行してください。');
    }
    if (currentId === targetId) {
      var sameMsg = '既に対象スプレッドシート（' + targetId + '）を使用しています。移行は不要です。';
      Logger.log(sameMsg);
      return sameMsg;
    }

    var source = SpreadsheetApp.openById(currentId);
    var target = SpreadsheetApp.openById(targetId);

    var report = [];
    migrationSheetNames_().forEach(function(name) {
      var src = source.getSheetByName(name);
      if (!src) {
        report.push('・' + name + ': 元になし→スキップ');
        return;
      }
      var lastRow = src.getLastRow();
      var lastCol = src.getLastColumn();
      var dst = target.getSheetByName(name) || target.insertSheet(name);
      dst.clear();
      if (lastRow > 0 && lastCol > 0) {
        var values = src.getRange(1, 1, lastRow, lastCol).getValues();
        dst.getRange(1, 1, lastRow, lastCol).setValues(values);
        dst.setFrozenRows(1);
      }
      report.push('・' + name + ': ' + Math.max(0, lastRow - 1) + '行 コピー');
    });

    // 参照先を新スプレッドシート(A)へ切り替え。
    props.setProperty(APP.PROP_SPREADSHEET_ID, targetId);

    var done = [
      '✅ データベース移行が完了しました。',
      '旧（コピー元・そのまま残置）: ' + currentId,
      '新（これからの保存先）      : ' + targetId,
      '',
      report.join('\n'),
      '',
      'アプリの参照先を新スプレッドシートに切り替えました。',
      '画面を再読込して、申請一覧・承認者マスタが表示されることを確認してください。',
      '問題があれば、DATA_SPREADSHEET_ID を旧IDに戻せば元に戻せます（旧DBは変更していません）。'
    ].join('\n');
    Logger.log(done);
    return done;
  } finally {
    lock.releaseLock();
  }
}

// ============================================================
// スプレッドシート日本語化 移行ツール（1回限りの運用作業用・冪等）
//
// 目的: タブ名・ヘッダー行・セル内の英語コード（status/step/action/category/type）を
//       日本語へ変換する。旧英語コード→新日本語コードのマップはこのファイルに固定で持つ
//       （定数の変更に依存せず、何度実行しても安全＝冪等）。
//
// 使い方（Apps Script エディタ）:
//   1) 事前にスプレッドシートを「コピーを作成」でバックアップ
//   2) migrateSpreadsheetToJapanese を実行 → 実行ログに変換件数を表示
//   3) アプリを再読込して申請一覧・承認・PDF・履歴が正しく表示されるか確認
// ============================================================

// 旧英語コード → 新日本語コード（Constants.gs の現行値と一致させること）
var JP_STATUS_MAP = { 'IN_REVIEW': '承認中', 'RETURNED': '差戻し', 'COMPLETED': '完了', 'CANCELLED': '取消' };
var JP_STEP_MAP = {
  'SUPERVISOR': '上席', 'PURCHASING_QUOTE': '購買見積', 'GENERAL_MANAGER': '総務部長',
  'PRESIDENT': '社長', 'PURCHASING': '購買手配', 'APPLICANT': '申請者', 'DONE': '完了済'
};
var JP_ACTION_MAP = {
  'SUBMIT': '申請', 'APPROVE': '承認', 'RETURN': '差戻', 'RESUBMIT': '再申請', 'UPDATE': '更新',
  'COMPLETE': '完了処理', 'ESCALATE': '社長決裁へ', 'QUOTE': '金額確定', 'EXPEDITE': '金額不要至急',
  'PDF_GENERATE': 'PDF作成', 'CANCEL': '取消処理'
};
var JP_CATEGORY_MAP = { 'MECH': 'メカ', 'ELEC': '電気', 'GENERAL': '一般不明' };
var JP_RECIPIENT_TYPE_MAP = { 'GENERAL_AFFAIRS': '総務部', 'PURCHASING': '購買', 'PURCHASING_ELEC': '電気購買' };

// 各シートの移行定義（旧タブ名・新タブ名・列キー・ヘッダー・値変換対象）
function jpSheetMigrations_() {
  return [
    { legacy: 'Requests', name: SHEETS.REQUESTS, columns: REQUEST_COLUMNS, headers: REQUEST_HEADERS,
      convert: { status: JP_STATUS_MAP, currentStep: JP_STEP_MAP, category: JP_CATEGORY_MAP }, routeCol: 'routeJson' },
    { legacy: 'RequestItems', name: SHEETS.ITEMS, columns: ITEM_COLUMNS, headers: ITEM_HEADERS },
    { legacy: 'ApproverMaster', name: SHEETS.APPROVERS, columns: APPROVER_COLUMNS, headers: APPROVER_HEADERS },
    { legacy: 'StatusHistory', name: SHEETS.HISTORY, columns: HISTORY_COLUMNS, headers: HISTORY_HEADERS,
      convert: { action: JP_ACTION_MAP, fromStatus: JP_STATUS_MAP, toStatus: JP_STATUS_MAP, fromStep: JP_STEP_MAP, toStep: JP_STEP_MAP } },
    { legacy: 'Settings', name: SHEETS.SETTINGS, columns: SETTING_COLUMNS, headers: SETTING_HEADERS },
    { legacy: 'WorkersCache', name: SHEETS.WORKERS_CACHE, columns: WORKER_CACHE_COLUMNS, headers: WORKER_CACHE_HEADERS },
    { legacy: 'NotificationRecipients', name: SHEETS.RECIPIENTS, columns: RECIPIENT_COLUMNS, headers: RECIPIENT_HEADERS,
      convert: { type: JP_RECIPIENT_TYPE_MAP } },
    { legacy: 'DeptSupervisors', name: SHEETS.DEPT_SUPERVISORS, columns: DEPT_SUPERVISOR_COLUMNS, headers: DEPT_SUPERVISOR_HEADERS }
  ];
}

// マップに旧コードとして存在すれば新コードへ、無ければ（＝既に新コード等）そのまま返す。冪等。
function jpMapValue_(value, map) {
  if (value === null || value === undefined || value === '') {
    return value;
  }
  var key = String(value);
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : value;
}

// メインエントリ。DATA_SPREADSHEET_ID（無ければアクティブ）を対象に日本語化する。
function migrateSpreadsheetToJapanese() {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var props = PropertiesService.getScriptProperties();
    var id = props.getProperty(APP.PROP_SPREADSHEET_ID);
    var ss = id ? SpreadsheetApp.openById(id) : SpreadsheetApp.getActiveSpreadsheet();
    if (!ss) {
      throw new Error('対象スプレッドシートを特定できません。先にアプリを一度開いてから実行してください。');
    }

    var report = [];
    jpSheetMigrations_().forEach(function(cfg) {
      // タブ取得（新名優先、無ければ旧名を改名して引き継ぐ）
      var sheet = ss.getSheetByName(cfg.name);
      var renamed = false;
      if (!sheet) {
        var legacy = ss.getSheetByName(cfg.legacy);
        if (legacy) {
          legacy.setName(cfg.name);
          sheet = legacy;
          renamed = true;
        }
      }
      if (!sheet) {
        report.push('・' + cfg.name + ': シートなし→スキップ');
        return;
      }

      // ヘッダー行を日本語ラベルへ
      var maxCols = sheet.getMaxColumns();
      if (maxCols < cfg.headers.length) {
        sheet.insertColumnsAfter(maxCols, cfg.headers.length - maxCols);
      }
      sheet.getRange(1, 1, 1, cfg.headers.length).setValues([cfg.headers]).setFontWeight('bold');
      sheet.setFrozenRows(1);

      var lastRow = sheet.getLastRow();
      var dataRows = Math.max(0, lastRow - 1);
      var changed = 0;

      if (dataRows > 0 && cfg.convert) {
        Object.keys(cfg.convert).forEach(function(colKey) {
          var colIndex = cfg.columns.indexOf(colKey) + 1;
          if (colIndex <= 0) {
            return;
          }
          var range = sheet.getRange(2, colIndex, dataRows, 1);
          var values = range.getValues();
          var map = cfg.convert[colKey];
          var dirty = false;
          for (var i = 0; i < values.length; i++) {
            var before = values[i][0];
            var after = jpMapValue_(before, map);
            if (after !== before) {
              values[i][0] = after;
              dirty = true;
              changed++;
            }
          }
          if (dirty) {
            range.setValues(values);
          }
        });
      }

      // routeJson（ステップコードの配列）を変換
      if (dataRows > 0 && cfg.routeCol) {
        var rIndex = cfg.columns.indexOf(cfg.routeCol) + 1;
        if (rIndex > 0) {
          var rRange = sheet.getRange(2, rIndex, dataRows, 1);
          var rValues = rRange.getValues();
          var rDirty = false;
          for (var j = 0; j < rValues.length; j++) {
            var raw = rValues[j][0];
            if (!raw) {
              continue;
            }
            var arr;
            try {
              arr = JSON.parse(raw);
            } catch (e) {
              continue;
            }
            if (!Array.isArray(arr)) {
              continue;
            }
            var mapped = arr.map(function(step) {
              return jpMapValue_(step, JP_STEP_MAP);
            });
            var newRaw = JSON.stringify(mapped);
            if (newRaw !== String(raw)) {
              rValues[j][0] = newRaw;
              rDirty = true;
              changed++;
            }
          }
          if (rDirty) {
            rRange.setValues(rValues);
          }
        }
      }

      report.push('・' + cfg.name + (renamed ? '（改名）' : '') + ': ' + dataRows + '行 / セル変換 ' + changed + '件');
    });

    var done = [
      '✅ スプレッドシートの日本語化が完了しました。',
      '対象: ' + ss.getId(),
      '',
      report.join('\n'),
      '',
      'アプリを再読込して、申請一覧・承認・PDF・履歴が正しく表示されるか確認してください。'
    ].join('\n');
    Logger.log(done);
    return done;
  } finally {
    lock.releaseLock();
  }
}
