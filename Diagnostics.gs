// Diagnostics.gs - 速度調査用（GASエディタから手で実行する。シートへの書き込みはしない。
// 共有キャッシュ（CacheService）と、未設定ならデータ版数（ScriptProperties）には書き込む）

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
  var fresh = {
    tabs: JSON.stringify(getTabCounts()),
    list: JSON.stringify(getRequests({ mode: 'all' })),
    bootstrap: JSON.stringify(getBootstrap())
  };
  var sampleId = getRequests({ mode: 'all' })[0];
  sampleId = sampleId ? sampleId.requestId : '';
  if (sampleId) {
    fresh.detail = JSON.stringify(getRequestDetail(sampleId));
  }

  // 共有キャッシュ（CacheService）経由の時間。1回目で詰めて、2回目で当たりを測る。
  ['1回目（詰める）', '2回目（当たり）'].forEach(function(round) {
    lines.push('--- 共有キャッシュ ' + round + ' ---');
    resetExecutionCache_();
    lap('getTabCounts', function() { getTabCounts(); });
    resetExecutionCache_();
    lap('getBootstrap', function() { getBootstrap(); });
    resetExecutionCache_();
    lap('getRequests(all)', function() { getRequests({ mode: 'all' }); });
    if (sampleId) {
      resetExecutionCache_();
      lap('getRequestDetail', function() { getRequestDetail(sampleId); });
    }
  });

  // キャッシュ経由の結果が、シートを直接読んだ結果と一致するか。
  resetExecutionCache_();
  var cached = {
    tabs: JSON.stringify(getTabCounts()),
    list: JSON.stringify(getRequests({ mode: 'all' })),
    bootstrap: JSON.stringify(getBootstrap())
  };
  if (sampleId) {
    cached.detail = JSON.stringify(getRequestDetail(sampleId));
  }
  Object.keys(fresh).forEach(function(key) {
    lines.push('一致 ' + key + ': ' + (fresh[key] === cached[key] ? 'OK' : '★不一致'));
  });
  lines.push('合計: ' + (Date.now() - t0) + 'ms');
  Logger.log(lines.join('\n'));
}

function resetExecutionCache_() {
  _readCache = {};
  _settingsCache = null;
}
