// Jobs.gs - PDF作成・メール送信の後回し（承認などのボタンを押した人を待たせない）
//
// 承認などの書き込み処理は、ロックの中で状態を更新したあと、PDF作成とメール送信に
// 10秒以上かかっていた。これらを「後回しの仕事」として ScriptProperties に積み、
// 画面には先に結果を返す。積んだ仕事は次の2通りで実行する。
// 1. 画面（Client.html の gas()）が結果を受け取った直後に runDeferredJobs(ids) を裏で呼ぶ（通常はこちらで即時）。
// 2. 画面を閉じられた等で残った仕事は、5分ごとのトリガー runDeferredJobsSweep が拾う。
// 仕事は取り出し時にロックの中で削除するので、同じ仕事が二重に実行されることはない（最大1回）。
// 失敗した仕事は再試行しない（PDF作成済み・一部のメール送信済みのまま再実行すると二重送信になるため）。
// 管理者へメールで知らせ、要点だけを JOBFAILED_ に残す。
//
// 遅れて実行されたときに申請の状態が変わっていたら（リコール・却下など）、古い通知やPDFを出さずに捨てる。

var JOB_PREFIX = 'JOB_';
var JOB_FAILED_PREFIX = 'JOBFAILED_';
var JOB_MAX_BYTES = 8500;                   // ScriptProperties の1値上限（9KB）未満
var JOB_SWEEP_MIN_AGE_MS = 2 * 60 * 1000;   // 画面からの即時実行と取り合わないよう、2分以上前の仕事だけ拾う
var JOB_RUN_BUDGET_MS = 4 * 60 * 1000;      // GASの実行上限（6分）に当たる前に新しい仕事の取り出しをやめる

// 後回しにしてよい関数の一覧。引数はすべて JSON にできる値（申請行のスナップショット等）であること。
var DEFERRABLE_JOBS = {
  sendApprovalRequestEmail_: function(args) { return sendApprovalRequestEmail_.apply(null, args); },
  sendRequestUpdatedEmail_: function(args) { return sendRequestUpdatedEmail_.apply(null, args); },
  sendRejectedEmail_: function(args) { return sendRejectedEmail_.apply(null, args); },
  sendReturnedEmail_: function(args) { return sendReturnedEmail_.apply(null, args); },
  sendReturnedBroadcast_: function(args) { return sendReturnedBroadcast_.apply(null, args); },
  notifyGeneralAffairs_: function(args) { return notifyGeneralAffairs_.apply(null, args); },
  notifySplitReturned_: function(args) { return notifySplitReturned_.apply(null, args); },
  dispatchApprovalNotifications_: function(args) { return dispatchApprovalNotifications_.apply(null, args); },
  completeAndNotify_: function(args) { return completeAndNotify_.apply(null, args); },
  regeneratePdfAfterSplit_: function(args) { return regeneratePdfAfterSplit_.apply(null, args); }
};

var _deferredJobIds = [];       // この実行で積んだ仕事（getRequestDetail が画面へ返す）
var _inDeferredJob = false;     // 後回しの仕事を実行中か（ロックを持たずに走るので、PDFの書き込みで自分でロックを取る）
var _requestBaseUrlOverride = ''; // 見回りトリガーから送るメールでも、本番 /exec のURLを使うため
var _jobSeq = 0;

// fnName(args...) を後回しにする。guardRequestId を渡すと、積んだ時点のその申請の状態（状態・ステップ）を記録し、
// 実行時に変わっていれば実行しない。積めなかったときは従来どおりその場で実行する。
function defer_(fnName, args, guardRequestId) {
  if (!DEFERRABLE_JOBS[fnName]) {
    throw new Error('後回しにできない関数です: ' + fnName);
  }
  var job = { fn: fnName, args: args || [], createdAt: Date.now(), baseUrl: currentServiceUrl_() };
  if (guardRequestId) {
    var current = getRequestById_(guardRequestId);
    if (current) {
      job.guard = { requestId: guardRequestId, status: current.status, currentStep: current.currentStep };
    }
  }
  var id = JOB_PREFIX + Date.now() + '_' + ('00' + (_jobSeq++)).slice(-3) + '_' +
    Math.floor(Math.random() * 1e6).toString(36);
  var json = JSON.stringify(job);
  var bytes = Utilities.newBlob(json).getBytes().length;
  if (bytes > JOB_MAX_BYTES) {
    Logger.log('defer_: job too large, running now: ' + fnName + ' (' + bytes + ' bytes)');
    return DEFERRABLE_JOBS[fnName](job.args);
  }
  try {
    PropertiesService.getScriptProperties().setProperty(id, json);
  } catch (error) {
    Logger.log('defer_: store failed, running now: ' + fnName + ': ' + error.message);
    return DEFERRABLE_JOBS[fnName](job.args);
  }
  _deferredJobIds.push(id);
}

function currentServiceUrl_() {
  try {
    return ScriptApp.getService().getUrl() || '';
  } catch (error) {
    return '';
  }
}

// 画面から呼ぶ。渡された仕事を順に実行する（すでに実行済み・取り出し済みのものは飛ばす）。
function runDeferredJobs(ids) {
  var list = (ids || []).filter(function(id) {
    return typeof id === 'string' && id.indexOf(JOB_PREFIX) === 0;
  });
  return runJobs_(list);
}

// 5分ごとのトリガーから呼ばれる。取り残された仕事を拾って実行する。
function runDeferredJobsSweep() {
  var now = Date.now();
  var props = PropertiesService.getScriptProperties().getProperties();
  var ids = Object.keys(props).filter(function(key) {
    if (key.indexOf(JOB_PREFIX) !== 0) {
      return false;
    }
    var createdAt = Number(key.split('_')[1]) || 0;
    return now - createdAt >= JOB_SWEEP_MIN_AGE_MS;
  }).sort();
  if (ids.length === 0) {
    return { ok: 0, failed: 0, skipped: 0 };
  }
  Logger.log('runDeferredJobsSweep: ' + ids.length + ' jobs');
  return runJobs_(ids);
}

function runJobs_(ids) {
  var started = Date.now();
  var result = { ok: 0, failed: 0, skipped: 0 };
  for (var i = 0; i < ids.length; i++) {
    if (Date.now() - started > JOB_RUN_BUDGET_MS) {
      Logger.log('runJobs_: time budget reached, leaving ' + (ids.length - i) + ' jobs for the sweep');
      break;
    }
    var job = claimJob_(ids[i]);
    if (!job) {
      continue;
    }
    // 仕事ごとにシートを読み直す（前の仕事のPDF書き込みや、ほかの人の操作を反映する）。
    _readCache = {};
    _settingsCache = null;
    if (!jobStillValid_(job)) {
      result.skipped++;
      continue;
    }
    _inDeferredJob = true;
    _requestBaseUrlOverride = job.baseUrl || '';
    try {
      DEFERRABLE_JOBS[job.fn](job.args || []);
      result.ok++;
    } catch (error) {
      result.failed++;
      handleJobFailure_(ids[i], job, error);
    } finally {
      _inDeferredJob = false;
      _requestBaseUrlOverride = '';
    }
  }
  return result;
}

// 積んだ後に申請の状態・ステップが変わっていたら（リコール・却下・別の承認など）実行しない。
function jobStillValid_(job) {
  if (!job.guard) {
    return true;
  }
  var current = getRequestById_(job.guard.requestId);
  if (!current || current.status !== job.guard.status || current.currentStep !== job.guard.currentStep) {
    Logger.log('deferred job skipped (state changed): ' + job.fn + ' ' + job.guard.requestId +
      ' expected ' + job.guard.status + '/' + job.guard.currentStep +
      ' now ' + (current ? current.status + '/' + current.currentStep : '(none)'));
    return false;
  }
  return true;
}

// ロックの中で取り出して消す（二重実行を防ぐ）。ロックが取れない・無いときは null（見回りが後で拾う）。
function claimJob_(id) {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) {
    return null;
  }
  try {
    var store = PropertiesService.getScriptProperties();
    var json = store.getProperty(id);
    if (!json) {
      return null;
    }
    store.deleteProperty(id);
    var job = JSON.parse(json);
    if (!DEFERRABLE_JOBS[job.fn]) {
      Logger.log('claimJob_: unknown job ' + job.fn);
      return null;
    }
    return job;
  } finally {
    lock.releaseLock();
  }
}

function jobRequestId_(job) {
  if (job.guard && job.guard.requestId) {
    return job.guard.requestId;
  }
  var first = (job.args || [])[0];
  if (first && typeof first === 'object' && first.requestId) {
    return first.requestId;
  }
  return typeof first === 'string' ? first : '';
}

function handleJobFailure_(id, job, error) {
  var message = String(error && error.message ? error.message : error);
  var requestId = jobRequestId_(job);
  Logger.log('deferred job failed: ' + job.fn + ' ' + requestId + ' (' + id + '): ' + message);
  // 引数の本体は残さず要点だけ（ScriptProperties の合計上限 500KB を圧迫しないため）。
  try {
    PropertiesService.getScriptProperties().setProperty(
      id.replace(JOB_PREFIX, JOB_FAILED_PREFIX),
      JSON.stringify({ fn: job.fn, requestId: requestId, error: message.slice(0, 500), failedAt: Date.now() }));
  } catch (storeError) {
    Logger.log('handleJobFailure_: store failed: ' + storeError.message);
  }
  notifyAdminsOfJobFailure_(job.fn, requestId, message);
}

// 失敗した仕事を管理者へ知らせる（メール自体が失敗の原因なら届かないので、ログにも残している）。
function notifyAdminsOfJobFailure_(fn, requestId, message) {
  try {
    var admins = splitEmails_(getSettings_().adminEmails || '');
    if (admins.length === 0) {
      return;
    }
    sendMail_({
      to: admins.join(','),
      subject: '[貯蔵品購入申請] 通知・PDFの後処理に失敗しました ' + requestId,
      body: [
        '承認などの後に行う通知メール・PDF作成が失敗しました（自動での再送はしません）。',
        '処理: ' + fn,
        '申請: ' + (requestId || '（不明）'),
        'エラー: ' + message,
        '',
        '申請の詳細画面から必要に応じて「PDF再作成」を行い、関係者へ連絡してください。',
        '失敗の一覧は GAS エディタで listFailedJobs() を実行すると確認できます。'
      ].join('\n'),
      name: APP.NAME
    });
  } catch (mailError) {
    Logger.log('notifyAdminsOfJobFailure_ failed: ' + mailError.message);
  }
}

// GASエディタから実行する運用関数。残っている仕事と失敗した仕事をログに出す。
function listFailedJobs() {
  assertAdmin_(getCurrentUser_());
  var props = PropertiesService.getScriptProperties().getProperties();
  var lines = [];
  Object.keys(props).sort().forEach(function(key) {
    if (key.indexOf(JOB_PREFIX) !== 0 && key.indexOf(JOB_FAILED_PREFIX) !== 0) {
      return;
    }
    try {
      var job = JSON.parse(props[key]);
      lines.push(key + ' ' + job.fn + ' ' + (job.requestId || jobRequestId_(job)) + (job.error ? ' error=' + job.error : ''));
    } catch (error) {
      lines.push(key + ' （読めない値）');
    }
  });
  Logger.log(lines.length ? lines.join('\n') : '後回しの仕事・失敗した仕事はありません。');
  return lines;
}

// GASエディタから実行する運用関数。確認し終えた失敗記録を消す。
function clearFailedJobs() {
  assertAdmin_(getCurrentUser_());
  var store = PropertiesService.getScriptProperties();
  var keys = Object.keys(store.getProperties()).filter(function(key) {
    return key.indexOf(JOB_FAILED_PREFIX) === 0;
  });
  keys.forEach(function(key) {
    store.deleteProperty(key);
  });
  Logger.log(keys.length + ' 件の失敗記録を消しました。');
  return keys.length;
}

function ensureDeferredJobTrigger_() {
  var triggers = ScriptApp.getProjectTriggers();
  for (var i = 0; i < triggers.length; i++) {
    if (triggers[i].getHandlerFunction() === 'runDeferredJobsSweep') {
      return false;
    }
  }
  ScriptApp.newTrigger('runDeferredJobsSweep')
    .timeBased()
    .everyMinutes(5)
    .create();
  return true;
}
