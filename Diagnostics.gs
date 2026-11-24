// Diagnostics.gs - 速度調査用（GASエディタから手で実行する。読み取りのみ・書き込みなし）

// 1回のリクエストの中で、どの処理に何ミリ秒かかっているかを実行ログに出す。
function perfProbe() {
  var lines = [];
  var t0 = Date.now();
  function lap(label, fn) {
    var start = Date.now();
    var result = fn();
    lines.push(label + ': ' + (Date.now() - start) + 'ms' + (result !== undefined ? ' (' + result + ')' : ''));
    return result;
  }

  lap('Session.getActiveUser', function() { return Session.getActiveUser().getEmail() ? 'ok' : 'empty'; });
  lap('ScriptProperties.get', function() { PropertiesService.getScriptProperties().getProperty(APP.PROP_SPREADSHEET_ID); });
  lap('getSpreadsheet_ (openById + schema)', function() { getSpreadsheet_(); });
  lap('getSettings_', function() { return Object.keys(getSettings_()).length + ' keys'; });
  [
    [SHEETS.REQUESTS, REQUEST_COLUMNS],
    [SHEETS.ITEMS, ITEM_COLUMNS],
    [SHEETS.HISTORY, HISTORY_COLUMNS],
    [SHEETS.APPROVERS, APPROVER_COLUMNS],
    [SHEETS.DEPT_SUPERVISORS, DEPT_SUPERVISOR_COLUMNS],
    [SHEETS.WORKERS_CACHE, WORKER_CACHE_COLUMNS],
    [SHEETS.RECIPIENTS, RECIPIENT_COLUMNS]
  ].forEach(function(pair) {
    lap('read ' + pair[0], function() { return readObjects_(pair[0], pair[1]).length + ' rows'; });
  });
  lines.push('--- 以下はシート読み込み済み（同一実行内キャッシュあり）の計算時間 ---');
  lap('getTabCounts', function() { getTabCounts(); });
  lap('getBootstrap', function() { getBootstrap(); });
  lap('getRequests(all)', function() { return getRequests({ mode: 'all' }).length + ' rows'; });
  lines.push('合計: ' + (Date.now() - t0) + 'ms');
  Logger.log(lines.join('\n'));
}
