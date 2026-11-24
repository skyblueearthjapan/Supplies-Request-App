// Cutover.gs - VPS 版への切り替え（移転モード）
//
// スクリプトプロパティ MIGRATED_TO_URL に新しいアプリの URL が入っている間は「移転モード」になる。
// - doGet: アプリの代わりに移転の案内を返す（?requestId= 付きなら新しい URL の同じ申請へ移る）。
// - 画面から呼ばれる関数: getCurrentUser_ が例外を投げ、読み書きを止める。
// - トリガー（syncWorkersMaster / syncDeptSupervisorsMaster / runDeferredJobsSweep）: 何もせずに戻る。
// プロパティが無ければ、今までと同じ動き。戻すときは exitMigrationMode → restoreTriggersAfterRollback。
//
// 下の運用関数は GAS エディタから手で実行する（管理者のみ）。手順は docs/cutover-runbook.md。

var MIGRATED_TO_URL_KEY = 'MIGRATED_TO_URL';
var MIGRATION_TRIGGER_FUNCTIONS = ['syncWorkersMaster', 'syncDeptSupervisorsMaster', 'runDeferredJobsSweep'];

var MIGRATED_TO_URL_PATTERN = /^https:\/\/[^\s"'<>]+$/;

function getMigratedToUrl_() {
  return String(PropertiesService.getScriptProperties().getProperty(MIGRATED_TO_URL_KEY) || '').trim();
}

function isValidMigratedToUrl_(url) {
  return MIGRATED_TO_URL_PATTERN.test(String(url || ''));
}

function migratedRequestUrl_(baseUrl, requestId) {
  var base = baseUrl.replace(/\/+$/, '');
  return requestId ? base + '/?requestId=' + encodeURIComponent(requestId) : base + '/';
}

// doGet の代わりに返す案内。iframe（GAS の HtmlService）の外へ出るため target=_top を使う。
// クリックなしの window.top の移動はブラウザが止めることがあるため、主はボタンを押してもらう（自動の移動は動けばよい）。
// プロパティの値が URL の形でなければ、リンクを出さずに固定の文言だけを返す（手で入れた値の誤りでおかしな先へ送らない）。
function renderMigratedPage_(baseUrl, requestId) {
  if (!isValidMigratedToUrl_(baseUrl)) {
    Logger.log('renderMigratedPage_: invalid ' + MIGRATED_TO_URL_KEY + ', showing fixed message');
    return HtmlService.createHtmlOutput(
      '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="font-family:sans-serif;margin:40px 16px">' +
      '<p>' + escapeHtml_(APP.NAME) + ' は新しいアドレスへ移りました。新しいアドレスは管理者にお問い合わせください。</p>' +
      '</body></html>')
      .setTitle(APP.NAME + '（移転しました）')
      .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
  }
  var target = migratedRequestUrl_(baseUrl, requestId);
  var autoMove = requestId
    ? '<script>setTimeout(function(){try{window.top.location.href=' +
      JSON.stringify(target).replace(/</g, '\\u003c') + ';}catch(e){}},1000);</script>'
    : '';
  var html = [
    '<!DOCTYPE html><html><head><meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    '<style>body{font-family:sans-serif;margin:40px 16px;line-height:1.7;color:#222}',
    '.go{display:inline-block;padding:16px 32px;font-size:20px;font-weight:bold;color:#fff;background:#2f4f8f;',
    'border-radius:8px;text-decoration:none}.url{font-size:14px;color:#555;word-break:break-all}</style></head><body>',
    '<h1 style="font-size:20px">' + escapeHtml_(APP.NAME) + ' は新しいアドレスへ移りました</h1>',
    '<p>' + (requestId ? '申請 ' + escapeHtml_(requestId) + ' を開くには、' : '') + '下のボタンを押してください。</p>',
    '<p><a class="go" href="' + escapeHtml_(target) + '" target="_top">新しいアドレスで開く</a></p>',
    '<p class="url">新しいアドレス: ' + escapeHtml_(target) + '<br>ブックマークをお使いの方は、差し替えをお願いします。</p>',
    autoMove,
    '</body></html>'
  ].join('');
  return HtmlService.createHtmlOutput(html)
    .setTitle(APP.NAME + '（移転しました）')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

// 画面から呼ばれる関数用（getCurrentUser_ の先頭で呼ぶ）。移転モードなら書き込みも読み込みも止める。
function assertNotMigrated_() {
  var url = getMigratedToUrl_();
  if (url) {
    throw new Error('このアプリは新しいアドレスへ移りました: ' + url);
  }
}

// トリガー用。移転モードなら true（呼び出し側は何もせずに戻る）。
function skipBecauseMigrated_(fnName) {
  if (!getMigratedToUrl_()) {
    return false;
  }
  Logger.log(fnName + ': migrated to ' + getMigratedToUrl_() + ', skipped');
  return true;
}

// 運用関数の管理者確認（移転モードでも動かせるよう、getCurrentUser_ の確認を通さない）。
function assertCutoverAdmin_() {
  assertAdmin_(getCurrentUserUnchecked_());
}

// ===== GAS エディタから実行する運用関数 =====

// 例: enterMigrationMode('https://supplies.lineworks-local.net')
// 引数なしで実行したときは、新しいアプリの既定の URL を使う。
function enterMigrationMode(url) {
  assertCutoverAdmin_();
  var value = String(url || 'https://supplies.lineworks-local.net').trim().replace(/\/+$/, '');
  if (!isValidMigratedToUrl_(value)) {
    throw new Error('URL は https:// で始まる形で渡してください: ' + value);
  }
  PropertiesService.getScriptProperties().setProperty(MIGRATED_TO_URL_KEY, value);
  invalidateCache_();
  Logger.log('移転モードにしました: ' + value);
  return value;
}

// 戻すとき。プロパティを消して、今までと同じ動きに戻す。トリガーは restoreTriggersAfterRollback で戻す。
function exitMigrationMode() {
  assertCutoverAdmin_();
  PropertiesService.getScriptProperties().deleteProperty(MIGRATED_TO_URL_KEY);
  invalidateCache_();
  Logger.log('移転モードを外しました。');
  return true;
}

// 残っている後回しの仕事（JOB_*）を流しきり、残数と失敗（JOBFAILED_*）をログに出す。
// 4 分の上限で残ったときは「もう一度実行してください」と出るので、残り 0 件になるまで繰り返す。
function drainDeferredJobsForMigration() {
  assertCutoverAdmin_();
  var store = PropertiesService.getScriptProperties();
  var ids = Object.keys(store.getProperties()).filter(function(key) {
    return key.indexOf(JOB_PREFIX) === 0;
  }).sort();
  var result = ids.length ? runJobs_(ids) : { ok: 0, failed: 0, skipped: 0 };
  var props = store.getProperties();
  var remaining = [];
  var failed = [];
  Object.keys(props).sort().forEach(function(key) {
    if (key.indexOf(JOB_PREFIX) === 0) {
      remaining.push(key);
    } else if (key.indexOf(JOB_FAILED_PREFIX) === 0) {
      failed.push(key + ' ' + props[key]);
    }
  });
  var lines = [
    '流した仕事: ' + ids.length + ' 件（成功 ' + result.ok + ' / 失敗 ' + result.failed + ' / 取りやめ ' + result.skipped + '）',
    '残っている仕事（JOB_*）: ' + remaining.length + ' 件' + (remaining.length ? '（もう一度実行してください）' : ''),
    '失敗した仕事（JOBFAILED_*）: ' + failed.length + ' 件'
  ].concat(failed);
  Logger.log(lines.join('\n'));
  return { ran: ids.length, result: result, remaining: remaining.length, failed: failed };
}

// トリガー 3 つ（作業員マスタ・上席マスタの同期と、後回しの仕事の見回り）を削除する。
function removeAllTriggersForMigration() {
  assertCutoverAdmin_();
  var removed = [];
  ScriptApp.getProjectTriggers().forEach(function(trigger) {
    var fn = trigger.getHandlerFunction();
    if (MIGRATION_TRIGGER_FUNCTIONS.indexOf(fn) !== -1) {
      ScriptApp.deleteTrigger(trigger);
      removed.push(fn + ' (' + trigger.getUniqueId() + ')');
    }
  });
  var left = ScriptApp.getProjectTriggers().map(function(trigger) {
    return trigger.getHandlerFunction();
  });
  Logger.log('削除したトリガー: ' + (removed.length ? removed.join(', ') : 'なし') +
    '\n残っているトリガー: ' + (left.length ? left.join(', ') : 'なし'));
  return removed;
}

// 戻すとき（exitMigrationMode の後）。トリガー 3 つを作り直す（あるものはそのまま）。
function restoreTriggersAfterRollback() {
  assertCutoverAdmin_();
  var result = installSyncTriggers();
  Logger.log('トリガーを作り直しました（true は新しく作ったもの）: ' + JSON.stringify(result));
  return result;
}
