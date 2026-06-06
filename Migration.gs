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

// 現在のDB → 本来のDB(A) へ移行して参照先を切り替える。
function migrateDatabaseToIntendedSheet() {
  return migrateDatabaseToSpreadsheet_(INTENDED_DATA_SPREADSHEET_ID);
}

// 現在のDBの全データシートを target へコピーし、DATA_SPREADSHEET_ID を target に切り替える。
function migrateDatabaseToSpreadsheet_(targetId) {
  if (!targetId) {
    throw new Error('移行先スプレッドシートIDが空です。');
  }
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

  var sheetNames = [
    SHEETS.REQUESTS, SHEETS.ITEMS, SHEETS.APPROVERS, SHEETS.HISTORY,
    SHEETS.SETTINGS, SHEETS.WORKERS_CACHE, SHEETS.RECIPIENTS, SHEETS.DEPT_SUPERVISORS
  ];

  var report = [];
  sheetNames.forEach(function(name) {
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
    '問題があれば、DATA_SPREADSHEET_ID を旧IDに戻せば元に戻せます。'
  ].join('\n');
  Logger.log(done);
  return done;
}
