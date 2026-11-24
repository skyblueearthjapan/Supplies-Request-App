#!/usr/bin/env node
// 画面（static/index.html）をサーバー無しで確かめるための見本サーバー。
// GAS 版と同じ形の固定データを返す。業務の決まり（権限・宛先など）は再現しない。
//
//   node vps-app/web/dev/mock-server.js [port]        既定 8765
//   MOCK_LEGACY_LIST=1  getRequests を GAS 版の古い形（配列）で返す
//   MOCK_ADMIN=0        管理者でない利用者として返す
//
// 呼び出しの記録: GET /__calls（関数名・引数・X-Idempotency-Key）。GET /__reset で記録を消す。
'use strict';

const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = Number(process.argv[2]) || 8765;
const STATIC_DIR = path.join(__dirname, '..', 'static');
const ADMIN = process.env.MOCK_ADMIN !== '0';
const LEGACY_LIST = process.env.MOCK_LEGACY_LIST === '1';

// ---- GAS 版の Constants.gs と同じ値 ----
const REASONS = [
  { code: 'A', label: '4月以降も都度購入を認めた消耗品（ウエス、オイル等）' },
  { code: 'B', label: '対象工番が特殊案件で新規に購入しなければ対応できないもの' },
  { code: 'C', label: '年度末に購入した消耗品を使い切った場合' },
  { code: 'D', label: '工具等を修理する場合' },
  { code: 'E', label: '工具等を修理するよりも新規で購入したほうが安価な場合' },
  { code: 'F', label: '社長が特別に認可した場合' }
];
const UNIT_OPTIONS = ['個', '本', '枚', '台', '箱', 'ケース', 'セット', '袋', '缶', '巻', '束', '組', '双', 'kg', 'L', 'm'];
const STATUS_LABELS = { '承認中': '承認中', '差戻し': '差戻し', '完了': '完了', '取消': '取消' };
const STEP_LABELS = { '上席': '上席', '購買見積': '購買（見積）', '総務部長': '総務部長', '社長': '社長', '購買手配': '購買', '申請者': '申請者', '完了済': '完了' };
const ACTION_LABELS = { '申請': '申請', '承認': '承認', '差戻': '差戻し', '再申請': '再申請', '更新': '更新', '完了処理': '完了', '社長決裁へ': '社長決裁へ', '金額確定': '金額確定', '金額不要至急': '金額不要・至急', 'PDF作成': 'PDF作成', '取消処理': '取消', '前段戻し': '前段へ戻す', '却下': '却下', '明細分離': '明細を分離' };
const CATEGORY_LABELS = { 'メカ': 'メカ', '電気': '電気', '一般不明': '一般・不明' };
const USER = { email: 'imaizumi@lineworks-local.info', name: 'imaizumi' };

// ---- 固定データ ----
let seq = 0;
function pad(n) { return String(n).padStart(2, '0'); }
function nowStr() {
  const d = new Date(Date.now() + 9 * 3600 * 1000);
  return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) + ' ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()) + ':' + pad(d.getUTCSeconds());
}

function mkRequest(id, o) {
  const route = o.route || ['上席', '購買見積', '総務部長', '購買手配'];
  return Object.assign({
    requestId: id,
    createdAt: '2026-11-20 09:00:00', updatedAt: o.updatedAt || '2026-11-20 09:00:00',
    submittedAt: '2026-11-20 09:00:00', completedAt: '',
    applicantEmail: 'yamada@lineworks-local.info', applicantName: '山田 太郎', department: '製造部',
    requestDate: '2026-11-20', reasonCode: 'A', reasonDetail: '在庫が残り僅かのため補充します。',
    category: 'メカ', totalAmount: 0, status: '承認中', currentStep: '上席',
    currentApproverEmail: 'sato@lineworks-local.info', currentApproverName: '佐藤 次郎',
    routeJson: route, amountWaived: false, pdfFileId: '', pdfUrl: '', splitFromRequestId: '', version: '1.0.0'
  }, o, { route: route });
}

const db = { requests: {}, items: {}, history: {}, pending: {} };
function put(r, items, history) {
  db.requests[r.requestId] = r;
  db.items[r.requestId] = items.map((it, i) => Object.assign({
    itemId: 'ITEM-' + r.requestId.slice(4) + '-' + i, requestId: r.requestId, lineNo: i + 1,
    name: '', model: '', maker: '', quantity: 1, unit: '個', unitPrice: 0, amount: 0, desiredDeliveryDate: '', note: ''
  }, it));
  db.history[r.requestId] = history;
}
function h(action, from, to, actor, at, comment) {
  return { historyId: 'HIS-' + (++seq), action: action, fromStep: from, toStep: to, actorEmail: actor.toLowerCase() + '@lineworks-local.info', actorName: actor, happenedAt: at, comment: comment || '' };
}

put(mkRequest('REQ-20261120090000-000001', { updatedAt: '2026-11-24 10:00:00' }),
  [{ name: 'ウエス', model: 'W-100', maker: '日本ウエス', quantity: 10, unit: '袋' }, { name: '作動油', model: 'HO-32', maker: '出光', quantity: 2, unit: '缶', note: '至急' }],
  [h('申請', '申請者', '上席', '山田 太郎', '2026-11-20 09:00:00')]);
db.pending['REQ-20261120090000-000001'] = 2; // 2 回取り直すと後処理が終わる（見回りの確認用）

put(mkRequest('REQ-20261121090000-000002', { currentStep: '購買見積', currentApproverName: '五十嵐', category: '電気', updatedAt: '2026-11-23 10:00:00' }),
  [{ name: 'ブレーカー', model: 'NF32', maker: '三菱電機', quantity: 3, unit: '個' }, { name: 'ケーブル', model: 'VCT 2sq', maker: '矢崎', quantity: 1.5, unit: 'm' }],
  [h('申請', '申請者', '上席', '山田 太郎', '2026-11-21 09:00:00'), h('承認', '上席', '購買見積', '佐藤 次郎', '2026-11-21 10:00:00')]);

put(mkRequest('REQ-20261122090000-000003', { currentStep: '社長', currentApproverName: '社長 一郎', totalAmount: 150000, route: ['上席', '購買見積', '総務部長', '社長', '購買手配'], updatedAt: '2026-11-22 15:00:00', pdfUrl: 'https://drive.google.com/file/d/mock/view' }),
  [{ name: '溶接機', model: 'YD-200', maker: 'パナソニック', quantity: 1, unit: '台', unitPrice: 150000, amount: 150000 }],
  [h('申請', '申請者', '上席', '山田 太郎', '2026-11-22 09:00:00'), h('承認', '上席', '購買見積', '佐藤 次郎', '2026-11-22 10:00:00'),
    h('金額確定', '購買見積', '総務部長', '五十嵐', '2026-11-22 11:00:00'), h('承認', '総務部長', '社長', '総務 花子', '2026-11-22 15:00:00')]);

put(mkRequest('REQ-20261123090000-000004', { status: '差戻し', currentStep: '申請者', currentApproverName: '', splitFromRequestId: 'REQ-20261121090000-000002', updatedAt: '2026-11-23 12:00:00' }),
  [{ name: '端子', model: 'R2-4', maker: 'ニチフ', quantity: 100, unit: '個' }],
  [h('明細分離', '購買見積', '申請者', '五十嵐', '2026-11-23 12:00:00', '型式の確認が必要です')]);
// 差戻しの理由として出る行
db.history['REQ-20261123090000-000004'].push(h('差戻', '購買見積', '申請者', '五十嵐', '2026-11-23 12:00:01', '型式の確認が必要です'));

put(mkRequest('REQ-20261110090000-000005', { status: '完了', currentStep: '完了済', currentApproverName: '', totalAmount: 12000, completedAt: '2026-11-15 10:00:00', updatedAt: '2026-11-15 10:00:00', pdfUrl: 'https://drive.google.com/file/d/mock2/view' }),
  [{ name: 'グリス', model: 'G-1', maker: '協同油脂', quantity: 4, unit: '缶', unitPrice: 3000, amount: 12000 }],
  [h('申請', '申請者', '上席', '山田 太郎', '2026-11-10 09:00:00'), h('承認', '上席', '購買見積', '佐藤 次郎', '2026-11-10 10:00:00'),
    h('金額確定', '購買見積', '総務部長', '五十嵐', '2026-11-11 10:00:00'), h('承認', '総務部長', '購買手配', '総務 花子', '2026-11-12 10:00:00'),
    h('完了処理', '購買手配', '完了済', '五十嵐', '2026-11-15 10:00:00')]);

put(mkRequest('REQ-20261109090000-000006', { status: '取消', totalAmount: 99999, updatedAt: '2026-11-09 12:00:00' }),
  [{ name: '取消した品', quantity: 1 }], [h('申請', '申請者', '上席', '山田 太郎', '2026-11-09 09:00:00')]);

// ページ送りの確認用に完了の申請を 60 件
for (let i = 0; i < 60; i++) {
  const id = 'REQ-202610' + pad(1 + (i % 28)) + '090000-' + String(100 + i).padStart(6, '0');
  put(mkRequest(id, { status: '完了', currentStep: '完了済', currentApproverName: '', totalAmount: 1000 * (i + 1), updatedAt: '2026-10-' + pad(1 + (i % 28)) + ' 09:00:00' }),
    [{ name: '消耗品' + (i + 1), quantity: 1, unitPrice: 1000 * (i + 1), amount: 1000 * (i + 1) }],
    [h('申請', '申請者', '上席', '山田 太郎', '2026-10-01 09:00:00')]);
}

// ---- 返す形（GAS 版 Helpers.gs toClientRequest_ / Workflow.gs getRequestDetail と同じ） ----
function toClient(r) {
  return Object.assign({}, r, {
    reasonLabel: (REASONS.find(x => x.code === r.reasonCode) || {}).label || '',
    categoryLabel: CATEGORY_LABELS[r.category] || '一般・不明',
    statusLabel: STATUS_LABELS[r.status] || r.status,
    currentStepLabel: STEP_LABELS[r.currentStep] || r.currentStep,
    isPresidentPending: r.status === '承認中' && r.currentStep === '社長',
    routeJson: undefined
  });
}
function detailOf(id) {
  const r = db.requests[id];
  if (!r) { throw new Error('申請が見つかりません: ' + id); }
  const items = db.items[id];
  const inReview = r.status === '承認中';
  const step = r.currentStep;
  const pending = db.pending[id] || 0;
  return {
    request: toClient(r),
    items: items,
    history: db.history[id].map(x => Object.assign({}, x, { actionLabel: ACTION_LABELS[x.action] || x.action })),
    permissions: {
      canApprove: inReview, canReturn: inReview,
      canDelete: ADMIN && r.status !== '取消', canEdit: r.status === '差戻し', canResubmit: r.status === '差戻し',
      canDeletePool: r.status === '差戻し',
      canEditInReview: inReview && (step === '上席' || step === '購買見積'),
      canGeneratePdf: ADMIN && r.status !== '取消', canPresident: inReview && step === '社長' && ADMIN,
      canTabletApprove: inReview, canQuote: inReview && step === '購買見積', canArrange: inReview && step === '購買手配',
      canRecall: inReview && step !== '上席',
      canSplitItems: inReview && ['上席', '購買見積', '総務部長', '社長'].indexOf(step) >= 0 && items.length >= 2
    },
    stepTitles: { '上席': '製造部長', '総務部長': '総務部長' },
    purchasingStampName: '五十嵐',
    pendingJobs: pending
  };
}

function touch(id, patch, hist) {
  const r = db.requests[id];
  if (!r) { throw new Error('申請が見つかりません: ' + id); }
  Object.assign(r, patch, { updatedAt: nowStr() });
  if (hist) { db.history[id].push(hist); }
  db.pending[id] = 1; // 書き込みの後は後処理が 1 件ある（1 回の取り直しで終わる）
  return detailOf(id);
}
function nextStep(r) {
  const i = r.route.indexOf(r.currentStep);
  return i >= 0 && i < r.route.length - 1 ? r.route[i + 1] : '完了済';
}

let delegate = { granted: false, email: '', grantedAt: '' };
let mailRedirect = { active: false, to: '', until: '', forced: false };
let failedJobs = [
  { jobId: 'JOB-1', kind: 'mail', label: '承認依頼メール', requestId: 'REQ-20261121090000-000002', failedAt: '2026-11-24 08:00:00', attempts: 5, error: 'Gmail API: 403 insufficient permission' },
  { jobId: 'JOB-2', kind: 'pdf', label: 'PDF 作成', requestId: 'REQ-20261122090000-000003', failedAt: '2026-11-24 08:05:00', attempts: 3, error: 'Drive API: timeout' }
];
const approvers = [
  { department: '*', purchasingEmail: 'igarashi@lineworks-local.info', purchasingName: '五十嵐', purchasingTitle: '購買', purchasingElecEmail: 'denki@lineworks-local.info', purchasingElecName: '電気 太郎', purchasingElecTitle: '', generalManagerEmail: 'soumu@lineworks-local.info', generalManagerName: '総務 花子', generalManagerTitle: '総務部長', presidentEmail: 'president@lineworks-local.info', presidentName: '社長 一郎', presidentTitle: '代表取締役', active: true }
];
let recipients = [
  { type: '総務部', sendAs: 'TO', email: 'soumu@lineworks-local.info', name: '総務部', active: true },
  { type: '購買', sendAs: 'TO', email: 'igarashi@lineworks-local.info', name: '五十嵐', active: true }
];
let settings = { thresholdAmount: 100000, pdfFolderId: '1AbCdEfGhIjK', adminEmails: 'imaizumi@lineworks-local.info', enableEmailNotifications: true };

function bootstrap() {
  return {
    appName: '貯蔵品購入申請', version: '1.0.0', user: USER, isAdmin: ADMIN,
    settings: settings,
    reasons: REASONS, unitOptions: UNIT_OPTIONS, defaultUnit: '個', unitOther: '__other__',
    statusLabels: STATUS_LABELS, stepLabels: STEP_LABELS, actionLabels: ACTION_LABELS,
    approvers: ADMIN ? approvers : [],
    deptSupervisors: ADMIN ? [{ department: '製造部', email: 'sato@lineworks-local.info', name: '佐藤 次郎', title: '製造部長', notifyEmail: '', active: true }] : [],
    recipients: ADMIN ? recipients : [],
    workers: [{ name: '山田 太郎', dept: '製造部' }, { name: '鈴木 花子', dept: '製造部' }, { name: '田中 一郎', dept: '電気課' }],
    departments: ['製造部', '電気課'],
    myApproverRule: null,
    // VPS 版で足す項目
    delegate: delegate,
    mailRedirect: mailRedirect,
    syncStatus: {
      workers: { lastSyncedAt: '2026-11-24 08:10:00', ok: true, count: 3, error: '' },
      deptSupervisors: { lastSyncedAt: '2026-11-24 08:12:00', ok: false, count: 0, error: 'DeptApprovers のタブが見つかりません' }
    }
  };
}

function listRequests(filter) {
  const f = filter || {};
  const mode = f.mode || 'mine';
  const q = String(f.query || '').toLowerCase();
  let rows = Object.values(db.requests).filter(r => {
    if (r.status === '取消' && mode !== 'mine' && mode !== 'all') { return false; }
    if (mode === 'supervisor') { return r.status === '承認中' && r.currentStep === '上席'; }
    if (mode === 'quote') { return r.currentStep === '購買見積'; }
    if (mode === 'gm') { return r.status === '承認中' && r.currentStep === '総務部長'; }
    if (mode === 'president') { return r.status === '承認中' && r.currentStep === '社長'; }
    if (mode === 'arrange') { return r.status === '承認中' && r.currentStep === '購買手配'; }
    if (mode === 'returned') { return r.status === '差戻し'; }
    return true;
  }).filter(r => !f.status || r.status === f.status)
    .filter(r => !q || [r.requestId, r.applicantName, r.department, r.reasonDetail, r.currentApproverName].join(' ').toLowerCase().indexOf(q) >= 0)
    .sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)))
    .map(toClient);
  if (LEGACY_LIST) { return rows.slice(0, 200); }
  const pageSize = Math.min(Math.max(Number(f.pageSize) || 50, 1), 200);
  const page = Math.max(Number(f.page) || 1, 1);
  const tax = n => Math.floor(n * 0.1);
  return {
    rows: rows.slice((page - 1) * pageSize, page * pageSize),
    total: rows.length, page: page, pageSize: pageSize,
    summary: {
      inReview: rows.filter(r => r.status === '承認中').length,
      returned: rows.filter(r => r.status === '差戻し').length,
      openAmount: rows.filter(r => r.status !== '完了' && r.status !== '取消').reduce((t, r) => t + r.totalAmount + tax(r.totalAmount), 0)
    }
  };
}

function tabCounts() {
  const c = { pending: 0, president: 0, quote: 0, supervisor: 0, gm: 0, arrange: 0, returned: 0 };
  Object.values(db.requests).forEach(r => {
    if (r.status === '取消') { return; }
    if (r.status === '差戻し') { c.returned++; }
    const ir = r.status === '承認中';
    if (ir && r.currentStep === '上席') { c.supervisor++; }
    if (r.currentStep === '購買見積') { c.quote++; }
    if (ir && r.currentStep === '総務部長') { c.gm++; }
    if (ir && r.currentStep === '社長') { c.president++; }
    if (ir && r.currentStep === '購買手配') { c.arrange++; }
  });
  return c;
}

// ---- RPC（GAS 版 22 関数＋VPS 版で足す RPC） ----
const RPC = {
  getBootstrap: () => bootstrap(),
  getTabCounts: () => tabCounts(),
  getRequests: (filter) => listRequests(filter),
  getRequestDetail: (id) => {
    const d = detailOf(id);
    if (db.pending[id] > 0) {
      db.pending[id]--;
      if (db.pending[id] === 0 && !db.requests[id].pdfUrl) { db.requests[id].pdfUrl = 'https://drive.google.com/file/d/mock-new/view'; }
    }
    return d;
  },
  createRequest: (payload) => {
    if (!payload || !Array.isArray(payload.items)) { throw new Error('入力が不正です'); }
    const id = 'REQ-' + nowStr().replace(/[- :]/g, '') + '-' + String(Math.floor(Math.random() * 1e6)).padStart(6, '0');
    put(mkRequest(id, { applicantName: payload.applicantName, department: payload.department, requestDate: payload.requestDate, reasonCode: payload.reasonCode, reasonDetail: payload.reasonDetail, category: payload.category, updatedAt: nowStr() }),
      payload.items.filter(it => it.name), [h('申請', '申請者', '上席', payload.applicantName || '申請者', nowStr())]);
    db.pending[id] = 1;
    return detailOf(id);
  },
  updateRequestDraft: (id, payload) => {
    db.items[id] = (payload.items || []).filter(it => it.name).map((it, i) => Object.assign({ itemId: 'ITEM-x' + i, lineNo: i + 1, unitPrice: 0, amount: 0 }, it));
    return touch(id, { applicantName: payload.applicantName, department: payload.department, reasonCode: payload.reasonCode, reasonDetail: payload.reasonDetail, category: payload.category }, h('更新', '申請者', '申請者', USER.name, nowStr()));
  },
  updateRequestInReview: (id, payload) => {
    db.items[id] = (payload.items || []).filter(it => it.name).map((it, i) => Object.assign({ itemId: 'ITEM-y' + i, lineNo: i + 1, unitPrice: 0, amount: 0 }, it));
    return touch(id, { totalAmount: 0 }, h('更新', db.requests[id].currentStep, db.requests[id].currentStep, USER.name, nowStr()));
  },
  resubmitRequest: (id) => touch(id, { status: '承認中', currentStep: '上席', totalAmount: 0, amountWaived: false }, h('再申請', '申請者', '上席', USER.name, nowStr())),
  approveRequest: (id, comment, kiosk, returnItems, expectedStep) => {
    const r = db.requests[id];
    if (expectedStep && expectedStep !== r.currentStep) { throw new Error('この申請はすでに別の端末で処理されています。画面を開き直してください。'); }
    const from = r.currentStep, to = nextStep(r);
    return touch(id, { currentStep: to, status: to === '完了済' ? '完了' : '承認中' }, h('承認', from, to, USER.name, nowStr(), comment));
  },
  returnRequest: (id, comment, kiosk, discard, expectedStep) => {
    const r = db.requests[id];
    if (expectedStep && expectedStep !== r.currentStep) { throw new Error('この申請はすでに別の端末で処理されています。画面を開き直してください。'); }
    if (!String(comment || '').trim()) { throw new Error('差戻し理由（コメント）を入力してください'); }
    return touch(id, discard ? { status: '取消' } : { status: '差戻し', currentStep: '申請者' }, h(discard ? '却下' : '差戻', r.currentStep, '申請者', USER.name, nowStr(), comment));
  },
  confirmQuote: (id, items, comment) => {
    let total = 0;
    (items || []).forEach(x => {
      const it = db.items[id].find(i => i.itemId === x.itemId);
      if (it) { it.unitPrice = x.unitPrice; it.amount = Math.round(x.unitPrice * it.quantity); total += it.amount; }
    });
    return touch(id, { totalAmount: total, currentStep: '総務部長' }, h('金額確定', '購買見積', '総務部長', USER.name, nowStr(), comment));
  },
  confirmExpedited: (id, comment) => touch(id, { amountWaived: true, currentStep: '総務部長' }, h('金額不要至急', '購買見積', '総務部長', USER.name, nowStr(), comment)),
  arrangeComplete: (id, comment) => touch(id, { status: '完了', currentStep: '完了済' }, h('完了処理', '購買手配', '完了済', USER.name, nowStr(), comment)),
  recallStep: (id, comment) => {
    const r = db.requests[id];
    const i = r.route.indexOf(r.currentStep);
    const to = i > 0 ? r.route[i - 1] : r.currentStep;
    return touch(id, { currentStep: to }, h('前段戻し', r.currentStep, to, USER.name, nowStr(), comment));
  },
  deleteRequest: (id) => { touch(id, { status: '取消' }, h('取消処理', db.requests[id].currentStep, db.requests[id].currentStep, USER.name, nowStr())); return { ok: true }; },
  generateRequestPdf: (id) => touch(id, { pdfUrl: 'https://drive.google.com/file/d/mock-pdf/view' }, h('PDF作成', db.requests[id].currentStep, db.requests[id].currentStep, USER.name, nowStr())),
  recordPresidentDecision: (id, decision, comment, kiosk, returnItems, expectedStep) => {
    const r = db.requests[id];
    if (expectedStep && expectedStep !== r.currentStep) { throw new Error('この申請はすでに別の端末で処理されています。画面を開き直してください。'); }
    return decision === 'approve'
      ? touch(id, { currentStep: nextStep(r) }, h('承認', '社長', nextStep(r), USER.name, nowStr(), comment))
      : touch(id, { status: '差戻し', currentStep: '申請者' }, h('差戻', '社長', '申請者', USER.name, nowStr(), comment));
  },
  syncDeptSupervisorsNow: () => ({ count: 1 }),
  syncWorkersNow: () => ({ count: 3 }),
  saveApproverMaster: (rows) => { approvers.splice(0, approvers.length, ...rows); return { ok: true }; },
  saveNotificationRecipients: (rows) => { recipients = rows; return { ok: true }; },
  saveSettings: (input) => { settings = Object.assign({}, settings, input); return bootstrap(); },
  // ---- VPS 版で足す RPC（担当 W への依頼と同じ形） ----
  saveAdminAll: (rows, recipientRows, input) => {
    approvers.splice(0, approvers.length, ...rows);
    recipients = recipientRows;
    settings = Object.assign({}, settings, input);
    return bootstrap();
  },
  setMailRedirect: (minutes) => {
    const until = new Date(Date.now() + 9 * 3600 * 1000 + (Number(minutes) || 120) * 60000).toISOString().replace('T', ' ').slice(0, 19);
    mailRedirect = { active: true, to: USER.email, until: until, forced: false };
    return mailRedirect;
  },
  clearMailRedirect: () => { mailRedirect = { active: false, to: '', until: '', forced: false }; return mailRedirect; },
  listFailedJobs: () => failedJobs,
  retryFailedJob: (jobId) => { failedJobs = failedJobs.filter(j => j.jobId !== jobId); return { ok: true }; }
};

const calls = [];
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml' };

function send(res, status, body, type) {
  res.writeHead(status, { 'Content-Type': type || 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(typeof body === 'string' ? body : JSON.stringify(body));
}

http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/__calls') { return send(res, 200, calls); }
  if (url.pathname === '/__reset') { calls.length = 0; return send(res, 200, { ok: true }); }
  if (url.pathname.startsWith('/auth/')) {
    return send(res, 200, '<!doctype html><meta charset="utf-8"><title>mock</title><p>見本サーバーのログイン画面（' + url.pathname + '）。next=' + (url.searchParams.get('next') || '') + '</p>', 'text/html; charset=utf-8');
  }
  if (url.pathname.startsWith('/api/rpc/')) {
    const name = decodeURIComponent(url.pathname.slice('/api/rpc/'.length));
    let raw = '';
    req.on('data', c => { raw += c; });
    req.on('end', () => {
      let args = [];
      try { args = raw ? JSON.parse(raw) : []; } catch (e) { return send(res, 400, { ok: false, error: '入力が不正です' }); }
      calls.push({ name: name, args: args, idempotencyKey: req.headers['x-idempotency-key'] || '' });
      if (req.method !== 'POST' || !Object.prototype.hasOwnProperty.call(RPC, name)) { return send(res, 404, { ok: false, error: '不明な関数です: ' + name }); }
      try {
        send(res, 200, { ok: true, result: RPC[name].apply(null, Array.isArray(args) ? args : []) });
      } catch (e) {
        send(res, 400, { ok: false, error: e.message });
      }
    });
    return;
  }
  // 静的ファイル（nginx の try_files と同じく、無ければ index.html）
  const rel = url.pathname === '/' ? '/index.html' : url.pathname;
  const file = path.normalize(path.join(STATIC_DIR, rel));
  if (!file.startsWith(STATIC_DIR)) { return send(res, 403, 'forbidden', 'text/plain'); }
  fs.readFile(file, (err, buf) => {
    if (err) {
      return fs.readFile(path.join(STATIC_DIR, 'index.html'), (e2, idx) => send(res, e2 ? 404 : 200, e2 ? 'not found' : idx, TYPES['.html']));
    }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' });
    res.end(buf);
  });
}).listen(PORT, '127.0.0.1', () => {
  console.log('mock server: http://127.0.0.1:' + PORT + '/');
});
