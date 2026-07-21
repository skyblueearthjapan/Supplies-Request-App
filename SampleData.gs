// SampleData.gs - デモ用サンプルデータの投入／削除
// 実データ（REQ- 接頭辞）には一切触れず、SAMPLE- 接頭辞の申請のみを対象にする。
// 管理者が管理画面または GAS エディタから seedSampleData() / clearSampleData() を実行する。

var SAMPLE_PREFIX = 'SAMPLE-';

// サンプル作業員マスタ（WorkersCache が空のときだけ投入）
var SAMPLE_WORKERS = [
  ['W001', '田中 健太', '製造一課', '本社工場', '工場'],
  ['W002', '鈴木 一郎', '製造二課', '新工場', '工場'],
  ['W003', '伊藤 さくら', '品質保証部', '本社工場', '事務所'],
  ['W004', '高橋 美咲', '総務部', '本社工場', '事務所'],
  ['W005', '渡辺 浩', '製造二課', '新工場', '工場'],
  ['W006', '佐藤 隆', '製造一課', '本社工場', '工場'],
  ['W007', '中村 大輔', '品質保証部', '本社工場', '事務所'],
  ['W008', '加藤 信', '購買部', '本社工場', '事務所'],
  ['W009', '山本 弘', '経営', '本社工場', '事務所']
];

function seedSampleData() {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    assertAdmin_(user);

    seedWorkersIfEmpty_();
    seedApproversIfEmpty_(user);
    seedDeptSupervisorsIfEmpty_(user);
    seedRecipientsIfEmpty_(user);
    ensureThresholdSetting_();

    clearSampleRows_();
    var requests = buildSampleRequests_(user);
    requests.forEach(function(sample) {
      insertSampleRequest_(sample);
    });

    return {
      inserted: requests.length,
      workers: getWorkers_().length,
      message: 'サンプル申請 ' + requests.length + ' 件を投入しました。'
    };
  } finally {
    lock.releaseLock();
  }
}

function clearSampleData() {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    assertAdmin_(user);
    var removed = clearSampleRows_();
    return { removed: removed, message: 'サンプル申請 ' + removed + ' 件を削除しました。' };
  } finally {
    lock.releaseLock();
  }
}

// ----- 投入ヘルパー -----

function seedWorkersIfEmpty_() {
  var existing = readObjects_(SHEETS.WORKERS_CACHE, WORKER_CACHE_COLUMNS).filter(function(row) {
    return row.workerCode;
  });
  if (existing.length > 0) {
    return;
  }
  var sheet = getSheet_(SHEETS.WORKERS_CACHE);
  sheet.getRange(2, 1, SAMPLE_WORKERS.length, WORKER_CACHE_COLUMNS.length).setValues(SAMPLE_WORKERS);
  invalidateCache_();
}

function seedApproversIfEmpty_(user) {
  var existing = readObjects_(SHEETS.APPROVERS, APPROVER_COLUMNS);
  if (existing.length > 0) {
    return;
  }
  // 単独テスト用に、全ロールを現在のユーザーに割り当てた全体デフォルトルール。
  appendObject_(SHEETS.APPROVERS, {
    department: '*',
    applicantEmail: '',
    applicantName: '',
    supervisorEmail: user.email,
    supervisorName: user.name + '（上席）',
    generalManagerEmail: user.email,
    generalManagerName: user.name + '（総務部長）',
    presidentEmail: user.email,
    presidentName: user.name + '（社長）',
    purchasingEmail: user.email,
    purchasingName: user.name + '（購買）',
    active: 'true',
    supervisorTitle: '係長',
    generalManagerTitle: '部長',
    presidentTitle: '社長',
    purchasingTitle: '主任'
  }, APPROVER_COLUMNS);
}

function seedDeptSupervisorsIfEmpty_(user) {
  var existing = readObjects_(SHEETS.DEPT_SUPERVISORS, DEPT_SUPERVISOR_COLUMNS);
  if (existing.length > 0) {
    return;
  }
  // 単独テスト用に、全社（'*'）の上席を現在のユーザーに割り当てる。
  appendObject_(SHEETS.DEPT_SUPERVISORS, {
    department: '*',
    email: user.email,
    name: user.name + '（上席）',
    active: 'true',
    title: '係長'
  }, DEPT_SUPERVISOR_COLUMNS);
}

function seedRecipientsIfEmpty_(user) {
  var existing = readObjects_(SHEETS.RECIPIENTS, RECIPIENT_COLUMNS);
  if (existing.length > 0) {
    return;
  }
  appendObject_(SHEETS.RECIPIENTS, { type: RECIPIENT_TYPES.GENERAL_AFFAIRS, email: user.email, name: '総務部（サンプル）', active: 'true', sendAs: 'TO' }, RECIPIENT_COLUMNS);
  appendObject_(SHEETS.RECIPIENTS, { type: RECIPIENT_TYPES.PURCHASING, email: user.email, name: '購買担当（サンプル）', active: 'true', sendAs: 'TO' }, RECIPIENT_COLUMNS);
}

function ensureThresholdSetting_() {
  var settings = getSettings_();
  if (!settings.thresholdAmount) {
    saveSettings_({ thresholdAmount: String(APP.DEFAULT_THRESHOLD) });
  }
}

function clearSampleRows_() {
  var requests = readObjects_(SHEETS.REQUESTS, REQUEST_COLUMNS).filter(isSampleRow_);
  requests.forEach(function(request) {
    deleteObjectsByColumn_(SHEETS.ITEMS, ITEM_COLUMNS, 'requestId', request.requestId);
    deleteObjectsByColumn_(SHEETS.HISTORY, HISTORY_COLUMNS, 'requestId', request.requestId);
    deleteObjectsByColumn_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', request.requestId);
  });
  return requests.length;
}

function isSampleRow_(request) {
  return String(request.requestId || '').indexOf(SAMPLE_PREFIX) === 0;
}

function insertSampleRequest_(sample) {
  // 申請時は金額未確定。購買(見積)で各明細の単価が入力されると amount/合計が確定する。
  // sample.confirmedAmount を totalAmount に設定（0 は見積前で未確定）。
  // 明細の単価が入力済みのサンプルは item.unitPrice を持ち、amount = quantity × unitPrice。
  var total = parseNumber_(sample.confirmedAmount);

  appendObject_(SHEETS.REQUESTS, {
    requestId: sample.requestId,
    createdAt: sample.requestDate + ' 09:00:00',
    updatedAt: sample.updatedAt,
    submittedAt: sample.requestDate + ' 09:00:00',
    completedAt: sample.completedAt || '',
    applicantEmail: sample.applicantEmail,
    applicantName: sample.applicantName,
    department: sample.department,
    requestDate: sample.requestDate,
    reasonCode: sample.reasonCode,
    reasonDetail: sample.reasonDetail,
    totalAmount: total,
    status: sample.status,
    currentStep: sample.currentStep,
    currentApproverEmail: sample.currentApproverEmail || '',
    currentApproverName: sample.currentApproverName || '',
    routeJson: JSON.stringify(sample.route),
    pdfFileId: '',
    pdfUrl: '',
    version: APP.VERSION
  }, REQUEST_COLUMNS);

  replaceItems_(sample.requestId, sample.items.map(function(item) {
    var quantity = parseNumber_(item.quantity);
    var unitPrice = parseNumber_(item.unitPrice);
    return {
      name: item.name,
      model: item.model || '',
      maker: item.maker || '',
      quantity: quantity,
      unitPrice: unitPrice,
      amount: Math.round(quantity * unitPrice),
      desiredDeliveryDate: item.desiredDeliveryDate || '',
      note: item.note || ''
    };
  }));

  sample.history.forEach(function(h) {
    appendObject_(SHEETS.HISTORY, {
      historyId: createId_('HIS'),
      requestId: sample.requestId,
      happenedAt: h.at,
      actorEmail: h.actorEmail || '',
      actorName: h.actorName || '',
      action: h.action,
      fromStatus: h.fromStatus || '',
      toStatus: h.toStatus || '',
      fromStep: h.fromStep || '',
      toStep: h.toStep || '',
      comment: h.comment || ''
    }, HISTORY_COLUMNS);
  });
}

// ===== 「金額不要／至急」メールのテスト用シード（メール送信なし・TEST- 接頭辞） =====
// 金額入力待ち（購買見積）状態の申請を3件、シートへ直接書き込むだけで生成する。
// insertSampleRequest_ と同様に一切メールを送らない（申請→上席承認→見積依頼の各メールは発生しない）。
// 生成後、購買として画面で「金額不要／至急」を押すと、通常フロー通り総務部へ実メールが送られる。
// 実行: GAS エディタで seedExpediteTest() を実行（管理者のみ）。削除は clearExpediteTest()。
var TEST_PREFIX = 'TEST-';

function seedExpediteTest(departmentOverride) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var user = getCurrentUser_();
    assertAdmin_(user);

    var dept = pickTestDepartment_(departmentOverride);
    clearExpediteTestRows_();

    var defs = buildExpediteTestRequests_(dept);
    defs.forEach(insertTestRequest_);

    return {
      inserted: defs.length,
      department: dept,
      message: '金額入力待ちのテスト申請 ' + defs.length + ' 件を生成しました（部署: ' + dept + '／メール送信なし）。画面で「金額不要／至急」を押すと総務部へ実メールが送られます。'
    };
  } finally {
    lock.releaseLock();
  }
}

function clearExpediteTest() {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    assertAdmin_(getCurrentUser_());
    var removed = clearExpediteTestRows_();
    return { removed: removed, message: 'テスト申請 ' + removed + ' 件を削除しました。' };
  } finally {
    lock.releaseLock();
  }
}

function clearExpediteTestRows_() {
  var rows = readObjects_(SHEETS.REQUESTS, REQUEST_COLUMNS).filter(isTestRow_);
  rows.forEach(function(request) {
    deleteObjectsByColumn_(SHEETS.ITEMS, ITEM_COLUMNS, 'requestId', request.requestId);
    deleteObjectsByColumn_(SHEETS.HISTORY, HISTORY_COLUMNS, 'requestId', request.requestId);
    deleteObjectsByColumn_(SHEETS.REQUESTS, REQUEST_COLUMNS, 'requestId', request.requestId);
  });
  return rows.length;
}

function isTestRow_(request) {
  return String(request.requestId || '').indexOf(TEST_PREFIX) === 0;
}

// 承認者マスタから、総務部長メールが引ける具体的な部署を選ぶ（至急時の GM 解決を保証）。
// 具体部署ルールが無ければ '*'（全社）ルール前提で一般的な部署名を使う。
function pickTestDepartment_(override) {
  if (override) {
    return sanitizeText_(override, 100);
  }
  var rules = readObjects_(SHEETS.APPROVERS, APPROVER_COLUMNS)
    .map(toClientApprover_)
    .filter(function(r) { return r.active; });
  var concrete = rules.find(function(r) {
    return r.department && r.department !== '*' && r.generalManagerEmail;
  });
  return concrete ? concrete.department : '製造一課';
}

function insertTestRequest_(def) {
  var now = nowString_();
  // 現在の承認者（購買）を解決できれば凍結表示に使う。解決できなくても admin は canApprove_ で操作可能。
  var purchasing = { email: '', name: '購買' };
  try {
    var rule = findApproverRule_(def.applicantEmail, def.department);
    if (rule) {
      var resolved = getApproverForStep_(rule, STEPS.PURCHASING_QUOTE, def.category);
      if (resolved && resolved.email) { purchasing = resolved; }
    }
  } catch (e) { /* 解決できなくても生成は継続 */ }

  appendObject_(SHEETS.REQUESTS, {
    requestId: def.requestId,
    createdAt: def.requestDate + ' 09:00:00',
    updatedAt: now,
    submittedAt: def.requestDate + ' 09:00:00',
    completedAt: '',
    applicantEmail: def.applicantEmail,
    applicantName: def.applicantName,
    department: def.department,
    requestDate: def.requestDate,
    reasonCode: def.reasonCode,
    reasonDetail: def.reasonDetail,
    totalAmount: 0,
    status: STATUS.IN_REVIEW,
    currentStep: STEPS.PURCHASING_QUOTE,
    currentApproverEmail: purchasing.email,
    currentApproverName: purchasing.name || '購買',
    routeJson: JSON.stringify([STEPS.SUPERVISOR, STEPS.PURCHASING_QUOTE, STEPS.GENERAL_MANAGER, STEPS.PURCHASING]),
    pdfFileId: '',
    pdfUrl: '',
    version: APP.VERSION,
    amountWaived: 'false',
    category: def.category
  }, REQUEST_COLUMNS);

  // 申請時は金額未確定のため unitPrice/amount は 0。
  replaceItems_(def.requestId, def.items.map(function(item) {
    return {
      name: item.name,
      model: item.model || '',
      maker: item.maker || '',
      quantity: parseNumber_(item.quantity),
      unitPrice: 0,
      amount: 0,
      desiredDeliveryDate: item.desiredDeliveryDate || '',
      note: item.note || ''
    };
  }));

  // 履歴は「申請」→「上席承認（見積へ）」の2件のみ（シート書き込みのみ・メールなし）。
  appendObject_(SHEETS.HISTORY, testHistoryRow_(def.requestId, def.requestDate + ' 09:00:00',
    def.applicantEmail, def.applicantName, ACTION.SUBMIT, '', STATUS.IN_REVIEW, STEPS.APPLICANT, STEPS.SUPERVISOR, 'テスト用に生成'), HISTORY_COLUMNS);
  appendObject_(SHEETS.HISTORY, testHistoryRow_(def.requestId, now,
    def.supervisorEmail, def.supervisorName, ACTION.APPROVE, STATUS.IN_REVIEW, STATUS.IN_REVIEW, STEPS.SUPERVISOR, STEPS.PURCHASING_QUOTE, 'テスト用（上席承認済み・見積依頼）'), HISTORY_COLUMNS);
}

function testHistoryRow_(requestId, at, actorEmail, actorName, action, fromStatus, toStatus, fromStep, toStep, comment) {
  return {
    historyId: createId_('HIS'),
    requestId: requestId,
    happenedAt: at,
    actorEmail: actorEmail || '',
    actorName: actorName || '',
    action: action,
    fromStatus: fromStatus || '',
    toStatus: toStatus || '',
    fromStep: fromStep || '',
    toStep: toStep || '',
    comment: comment || ''
  };
}

// 金額入力待ちのテスト申請3件（価格が商社と確定済みの標準品＝至急手配の対象イメージ）。
function buildExpediteTestRequests_(dept) {
  var d = '2026-07-21';
  return [
    {
      requestId: 'TEST-EXP-001', requestDate: d,
      applicantEmail: 'test-exp1@example.com', applicantName: 'テスト申請者1',
      department: dept, category: CATEGORIES.MECH, reasonCode: 'A',
      reasonDetail: '【テスト】標準品の至急手配確認用。CO2・溶接ワイヤーの補充。',
      supervisorEmail: 'test-sup@example.com', supervisorName: 'テスト上席',
      items: [
        { name: '炭酸ガス（CO2）', model: '30kg', maker: '—', quantity: 4, note: '溶接用' },
        { name: '溶接ワイヤー', model: 'YM-28 1.2mm', maker: '日鉄溶接', quantity: 10, note: '20kg巻' }
      ]
    },
    {
      requestId: 'TEST-EXP-002', requestDate: d,
      applicantEmail: 'test-exp2@example.com', applicantName: 'テスト申請者2',
      department: dept, category: CATEGORIES.MECH, reasonCode: 'A',
      reasonDetail: '【テスト】標準品の至急手配確認用。酸素・アセチレンの補充。',
      supervisorEmail: 'test-sup@example.com', supervisorName: 'テスト上席',
      items: [
        { name: '酸素ガス', model: '7000L', maker: '—', quantity: 3, note: '' },
        { name: 'アセチレンガス', model: '7kg', maker: '—', quantity: 2, note: '' }
      ]
    },
    {
      requestId: 'TEST-EXP-003', requestDate: d,
      applicantEmail: 'test-exp3@example.com', applicantName: 'テスト申請者3',
      department: dept, category: CATEGORIES.GENERAL, reasonCode: 'A',
      reasonDetail: '【テスト】標準品の至急手配確認用。標準塗装色・標準作動油の補充。',
      supervisorEmail: 'test-sup@example.com', supervisorName: 'テスト上席',
      items: [
        { name: '標準塗装色（グレー）', model: '16kg', maker: '—', quantity: 2, note: '標準色' },
        { name: '標準作動油', model: 'ISO VG32 20L', maker: '—', quantity: 4, note: '' }
      ]
    }
  ];
}

// ----- サンプル申請の定義（全ステータス網羅。承認待ち系は現在ユーザーが操作可能） -----

function buildSampleRequests_(user) {
  var me = user.email;
  var meName = user.name;
  // 標準経路（見積後・10万円未満）と社長決裁経路（10万円以上）。
  var STD = ['上席', '購買見積', '総務部長', '購買手配'];
  var PRES = ['上席', '購買見積', '総務部長', '社長', '購買手配'];

  return [
    {
      // 上席承認待ち。現在ユーザーが部署の上席として承認できる。金額・amount は未確定（0）。
      requestId: 'SAMPLE-2026-0014', requestDate: '2026-05-29', updatedAt: '2026-05-29 09:20:00',
      applicantEmail: 'takahashi@example.com', applicantName: '高橋 美咲', department: '総務部',
      reasonCode: 'A', reasonDetail: '事務用消耗品の補充です。上席承認をお待ちしています。',
      status: '承認中', currentStep: '上席', currentApproverEmail: me, currentApproverName: meName + '（上席）',
      route: STD, confirmedAmount: 0,
      items: [
        { name: 'ボールペン（黒）', model: 'BP-100', maker: 'ゼブラ', quantity: 50, desiredDeliveryDate: '2026-06-12', note: '' },
        { name: 'コピー用紙 A4', model: 'PPC-A4', maker: '日本製紙', quantity: 10, desiredDeliveryDate: '2026-06-12', note: '500枚×箱' }
      ],
      history: [
        { action: '申請', actorEmail: 'takahashi@example.com', actorName: '高橋 美咲', at: '2026-05-29 09:20:00', toStatus: '承認中', fromStep: '申請者', toStep: '上席' }
      ]
    },
    {
      // 購買(見積)待ち。現在ユーザーが各明細の単価を入力できる。金額・amount は未確定（0）。
      requestId: 'SAMPLE-2026-0012', requestDate: '2026-05-28', updatedAt: '2026-05-28 13:42:00',
      applicantEmail: 'tanaka@example.com', applicantName: '田中 健太', department: '製造一課',
      reasonCode: 'A', reasonDetail: '通常ラインで使用する消耗品の補充です。在庫が残り僅かのため。',
      status: '承認中', currentStep: '購買見積', currentApproverEmail: me, currentApproverName: meName + '（購買）',
      route: STD, confirmedAmount: 0,
      items: [
        { name: '工業用ウエス', model: 'WES-50', maker: '東洋ウエス', quantity: 20, desiredDeliveryDate: '2026-06-10', note: 'まとめ買い' },
        { name: '切削油', model: 'CUT-OIL2', maker: '日本グリス', quantity: 6, desiredDeliveryDate: '2026-06-10', note: '18L缶' },
        { name: '軍手', model: 'GUN-12', maker: '丸和', quantity: 50, desiredDeliveryDate: '2026-06-12', note: '' }
      ],
      history: [
        { action: '申請', actorEmail: 'tanaka@example.com', actorName: '田中 健太', at: '2026-05-28 09:14:00', toStatus: '承認中', fromStep: '申請者', toStep: '上席' },
        { action: '承認', actorEmail: 'sato@example.com', actorName: '佐藤 隆', at: '2026-05-28 13:42:00', fromStatus: '承認中', toStatus: '承認中', fromStep: '上席', toStep: '購買見積', comment: '内容確認しました。見積をお願いします。' }
      ]
    },
    {
      // 総務部長待ち（見積入力済み）。単価が入り合計46,000円が確定済み。
      requestId: 'SAMPLE-2026-0013', requestDate: '2026-05-28', updatedAt: '2026-05-29 10:05:00',
      applicantEmail: 'ito@example.com', applicantName: '伊藤 さくら', department: '品質保証部',
      reasonCode: 'C', reasonDetail: '年度末に使い切った検査用消耗品の補充。',
      status: '承認中', currentStep: '総務部長', currentApproverEmail: me, currentApproverName: meName + '（総務部長）',
      route: STD, confirmedAmount: 46000,
      items: [
        { name: '検査用手袋', model: 'NBR-M', maker: '川西工業', quantity: 10, unitPrice: 1200, desiredDeliveryDate: '2026-06-08', note: 'ニトリル Mサイズ' },
        { name: '精密拭き取り紙', model: 'KW-300', maker: '日本製紙', quantity: 8, unitPrice: 4250, desiredDeliveryDate: '2026-06-08', note: '' }
      ],
      history: [
        { action: '申請', actorEmail: 'ito@example.com', actorName: '伊藤 さくら', at: '2026-05-28 08:30:00', toStatus: '承認中', fromStep: '申請者', toStep: '上席' },
        { action: '承認', actorEmail: 'nakamura@example.com', actorName: '中村 大輔', at: '2026-05-28 11:00:00', fromStatus: '承認中', toStatus: '承認中', fromStep: '上席', toStep: '購買見積', comment: '見積をお願いします。' },
        { action: '金額確定', actorEmail: me, actorName: meName, at: '2026-05-29 10:05:00', fromStatus: '承認中', toStatus: '承認中', fromStep: '購買見積', toStep: '総務部長', comment: '見積金額 ¥46,000' }
      ]
    },
    {
      // 社長決裁待ち（見積で10万円以上、総務部長承認済み、社長へエスカレーション）。
      requestId: 'SAMPLE-2026-0011', requestDate: '2026-05-27', updatedAt: '2026-05-27 16:48:00',
      applicantEmail: 'suzuki@example.com', applicantName: '鈴木 一郎', department: '製造二課',
      reasonCode: 'B', reasonDetail: '特殊案件 工番 K-2261 専用の治具部材。既存在庫では対応不可。',
      status: '承認中', currentStep: '社長', currentApproverEmail: me, currentApproverName: meName + '（社長）',
      route: PRES, confirmedAmount: 182000,
      items: [
        { name: '精密バイス', model: 'PV-160', maker: 'スーパーツール', quantity: 1, unitPrice: 78000, desiredDeliveryDate: '2026-06-20', note: '工番K-2261' },
        { name: '超硬エンドミル', model: 'EM-6F', maker: '三菱マテリアル', quantity: 8, unitPrice: 9000, desiredDeliveryDate: '2026-06-18', note: '6mm 4枚刃' },
        { name: 'ダイヤル測定器', model: 'DG-30', maker: 'ミツトヨ', quantity: 1, unitPrice: 32000, desiredDeliveryDate: '2026-06-20', note: '' }
      ],
      history: [
        { action: '申請', actorEmail: 'suzuki@example.com', actorName: '鈴木 一郎', at: '2026-05-27 10:02:00', toStatus: '承認中', fromStep: '申請者', toStep: '上席' },
        { action: '承認', actorEmail: 'watanabe@example.com', actorName: '渡辺 浩', at: '2026-05-27 11:20:00', fromStatus: '承認中', toStatus: '承認中', fromStep: '上席', toStep: '購買見積', comment: '案件対応に必要と判断。見積をお願いします。' },
        { action: '金額確定', actorEmail: me, actorName: meName, at: '2026-05-27 15:10:00', fromStatus: '承認中', toStatus: '承認中', fromStep: '購買見積', toStep: '総務部長', comment: '見積金額 ¥182,000（10万円以上）' },
        { action: '承認', actorEmail: me, actorName: meName, at: '2026-05-27 16:48:00', fromStatus: '承認中', toStatus: '承認中', fromStep: '総務部長', toStep: '社長', comment: '確定金額を確認、社長決裁へ回します。' }
      ]
    },
    {
      // 購買(手配)待ち。承認が揃い、現在ユーザーが手配完了を実行できる。
      requestId: 'SAMPLE-2026-0009', requestDate: '2026-05-26', updatedAt: '2026-05-26 14:20:00',
      applicantEmail: me, applicantName: meName, department: '総務部',
      reasonCode: 'A', reasonDetail: '事務消耗品および清掃用品の月次補充です。',
      status: '承認中', currentStep: '購買手配', currentApproverEmail: me, currentApproverName: meName + '（購買）',
      route: STD, confirmedAmount: 58000,
      items: [
        { name: 'コピー用紙 A4', model: 'PPC-A4', maker: '日本製紙', quantity: 30, unitPrice: 600, desiredDeliveryDate: '2026-06-05', note: '500枚×箱' },
        { name: 'トナーカートリッジ', model: 'TN-291', maker: 'ブラザー', quantity: 4, unitPrice: 10000, desiredDeliveryDate: '2026-06-05', note: '' }
      ],
      history: [
        { action: '申請', actorEmail: me, actorName: meName, at: '2026-05-26 09:30:00', toStatus: '承認中', fromStep: '申請者', toStep: '上席' },
        { action: '承認', actorEmail: me, actorName: meName, at: '2026-05-26 09:31:00', fromStatus: '承認中', toStatus: '承認中', fromStep: '上席', toStep: '購買見積', comment: '' },
        { action: '金額確定', actorEmail: me, actorName: meName, at: '2026-05-26 11:00:00', fromStatus: '承認中', toStatus: '承認中', fromStep: '購買見積', toStep: '総務部長', comment: '見積金額 ¥58,000' },
        { action: '承認', actorEmail: me, actorName: meName, at: '2026-05-26 14:20:00', fromStatus: '承認中', toStatus: '承認中', fromStep: '総務部長', toStep: '購買手配', comment: '定例分。手配をお願いします。' }
      ]
    },
    {
      // 差戻し（総務部長が見積後に差戻し）。申請者が修正・再申請できる。
      requestId: 'SAMPLE-2026-0008', requestDate: '2026-05-25', updatedAt: '2026-05-26 10:25:00',
      applicantEmail: 'tanaka@example.com', applicantName: '田中 健太', department: '製造一課',
      reasonCode: 'E', reasonDetail: '使用中の電動ドリルが故障。修理より新規購入が安価と判断。',
      status: '差戻し', currentStep: '申請者', currentApproverEmail: '', currentApproverName: '',
      route: STD, confirmedAmount: 0,
      items: [
        { name: '充電式ドリルドライバ', model: 'DD-18V', maker: 'マキタ', quantity: 2, desiredDeliveryDate: '2026-06-15', note: 'バッテリ2個付' },
        { name: 'ドリルビットセット', model: 'BIT-21', maker: 'ボッシュ', quantity: 1, desiredDeliveryDate: '2026-06-15', note: '' }
      ],
      history: [
        { action: '申請', actorEmail: 'tanaka@example.com', actorName: '田中 健太', at: '2026-05-25 14:10:00', toStatus: '承認中', fromStep: '申請者', toStep: '上席' },
        { action: '承認', actorEmail: 'sato@example.com', actorName: '佐藤 隆', at: '2026-05-25 15:02:00', fromStatus: '承認中', toStatus: '承認中', fromStep: '上席', toStep: '購買見積', comment: '' },
        { action: '金額確定', actorEmail: me, actorName: meName, at: '2026-05-25 17:00:00', fromStatus: '承認中', toStatus: '承認中', fromStep: '購買見積', toStep: '総務部長', comment: '見積金額 ¥38,000' },
        { action: '差戻', actorEmail: me, actorName: meName, at: '2026-05-26 10:25:00', fromStatus: '承認中', toStatus: '差戻し', fromStep: '総務部長', toStep: '申請者', comment: '修理見積もりも添付してください。比較のうえ再申請をお願いします。' }
      ]
    },
    {
      // 完了（10万円以上で社長決裁を経て手配完了）。
      requestId: 'SAMPLE-2026-0007', requestDate: '2026-05-20', updatedAt: '2026-05-22 11:15:00', completedAt: '2026-05-22 11:15:00',
      applicantEmail: me, applicantName: meName, department: '総務部',
      reasonCode: 'D', reasonDetail: '受付エリアの空調フィルタ交換および関連部材。',
      status: '完了', currentStep: '完了済', currentApproverEmail: '', currentApproverName: '',
      route: PRES, confirmedAmount: 120000,
      items: [
        { name: '空調フィルタ', model: 'AF-90', maker: 'ダイキン', quantity: 6, unitPrice: 15000, desiredDeliveryDate: '2026-05-28', note: '' },
        { name: '交換工具一式', model: 'TK-5', maker: 'KTC', quantity: 1, unitPrice: 30000, desiredDeliveryDate: '2026-05-28', note: '' }
      ],
      history: [
        { action: '申請', actorEmail: me, actorName: meName, at: '2026-05-20 09:00:00', toStatus: '承認中', fromStep: '申請者', toStep: '上席' },
        { action: '承認', actorEmail: me, actorName: meName, at: '2026-05-20 09:05:00', fromStatus: '承認中', toStatus: '承認中', fromStep: '上席', toStep: '購買見積', comment: '' },
        { action: '金額確定', actorEmail: me, actorName: meName, at: '2026-05-20 14:30:00', fromStatus: '承認中', toStatus: '承認中', fromStep: '購買見積', toStep: '総務部長', comment: '見積金額 ¥120,000（10万円以上）' },
        { action: '承認', actorEmail: me, actorName: meName, at: '2026-05-20 16:00:00', fromStatus: '承認中', toStatus: '承認中', fromStep: '総務部長', toStep: '社長', comment: '' },
        { action: '承認', actorEmail: 'yamamoto@example.com', actorName: '山本 弘', at: '2026-05-21 08:40:00', fromStatus: '承認中', toStatus: '承認中', fromStep: '社長', toStep: '購買手配', comment: '了承。' },
        { action: '完了処理', actorEmail: me, actorName: meName, at: '2026-05-22 11:15:00', fromStatus: '承認中', toStatus: '完了', fromStep: '購買手配', toStep: '完了済', comment: '発注完了。納期5/28。' }
      ]
    }
  ];
}
