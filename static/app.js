'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const categories = ['餐饮','交通','住宿','办公','通讯','其他'];
const statusNames = {available:'可报销',draft:'草稿占用',submitted:'已提交',paid:'已报销',cancelled:'已撤销',rejected:'已退回'};
const batchNames = {...statusNames,draft:'草稿'};
const pageNames = {invoices:'我的票库',match:'智能匹配',batches:'报销单',settings:'项目与额度',data:'数据管理',groups:'群组管理',mailboxes:'邮箱收票',account:'账号设置'};
const fmt = cents => (Number(cents || 0) / 100).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
const localDate = (date = new Date()) => `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;
const currentMonth = () => localDate().slice(0,7);
let state = {invoices:[],projects:[],batches:[],quotas:[],summary:{},capabilities:{}};
let activePage = 'invoices';
let currentUser = null;
let activeGroupId = localStorage.getItem('invoice.activeGroup') || '';
let registrationOpen = false;
let authMode = 'login';
let mailboxData = null;
let mailboxLoading = false;
let receiptData = null;
let receiptLoading = false;
let receiptError = '';
let receiptGeneration = 0;
let scopeBusy = false;
let selected = new Set();
let filters = {q:'',category:'',status:'',project_id:'',date_from:'',date_to:'',min:'',max:''};
let batchFilter = '';
let matchValues = {target:'',direction:'above',category:'',project_id:'',date_from:'',date_to:'',buyer:'',quota_id:''};
let matchData = null;
let modalBusy = false;
let toastTimer;
const modal = $('#modal');
const preview = $('#original-preview');
const originalViewers = new Map();

function scopedURL(path) {
  if (!activeGroupId) return path;
  const base = path.split('?')[0];
  if (['/api/state','/api/import','/api/export','/api/backup','/api/mailboxes'].includes(base) || /^\/api\/invoices\/[^/]+$/.test(base)) {
    return `${path}${path.includes('?')?'&':'?'}group_id=${encodeURIComponent(activeGroupId)}`;
  }
  return path;
}
async function api(path, options = {}) {
  if (activeGroupId && options.method === 'POST' && ['/api/invoices','/api/projects','/api/quotas','/api/batches','/api/match'].includes(path) && options.body) {
    options = {...options,body:JSON.stringify({...JSON.parse(options.body),group_id:activeGroupId})};
  }
  path = scopedURL(path);
  const headers = {'X-Requested-With':'InvoiceDesk',...(options.headers || {})};
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(path,{...options,headers});
  if (!response.ok) {
    let data;
    try { data = await response.json(); } catch (_) { data = {}; }
    if (response.status === 401 && currentUser) { currentUser=null;resetReceiptAddresses();preview.close();modal.close();renderAuth(); }
    throw new Error(typeof data.detail === 'string' ? data.detail : `请求未完成（${response.status}），请重试。`);
  }
  return response.status === 204 ? null : response.json();
}
function toast(message) {
  clearTimeout(toastTimer);
  const el = $('#toast');
  el.textContent = message;
  el.classList.add('show');
  toastTimer = setTimeout(() => el.classList.remove('show'),4000);
}
function cents(value) {
  const text = String(value).trim();
  if (!/^\d+(\.\d{1,2})?$/.test(text)) throw new Error('金额请填写非负数字，最多保留两位小数。');
  const [whole, fraction = ''] = text.split('.');
  const result = Number(whole) * 100 + Number(fraction.padEnd(2,'0'));
  if (!Number.isSafeInteger(result)) throw new Error('金额超出可处理范围。');
  return result;
}
function projectName(id) { return state.projects.find(p => p.id === id)?.name || '未分配'; }
function projectOptions(value = '', allLabel = '全部项目') {
  return `<option value="">${allLabel}</option>${state.projects.map(p => `<option value="${esc(p.id)}" ${p.id === value ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}`;
}
function categoryOptions(value = '', allLabel = '全部类别') {
  return `<option value="">${allLabel}</option>${categories.map(c => `<option ${c === value ? 'selected' : ''}>${c}</option>`).join('')}`;
}
function quotaOptions(value = '') {
  return `<option value="">不使用额度</option>${state.quotas.map(q => `<option value="${esc(q.id)}" ${value === q.id ? 'selected' : ''}>${esc(q.month)} · ${esc(q.category)} · 剩余 ¥${fmt(q.remaining_cents)}</option>`).join('')}`;
}
function invoiceBadge(invoice) {
  if (invoice.status === 'available' && !invoice.verified) return '<span class="badge review">待核对</span>';
  return `<span class="badge ${esc(invoice.status)}">${esc(statusNames[invoice.status] || invoice.status)}</span>`;
}
function heading(eyebrow,title,subtitle,actions = '') {
  return `<div class="page-heading"><div><p class="eyebrow">${eyebrow}</p><h1>${title}</h1><p class="subtitle">${subtitle}</p></div><div class="actions">${actions}</div></div>`;
}
function empty(title,description,action = '',symbol = '▤') {
  return `<div class="empty-state"><div class="empty-symbol" aria-hidden="true">${symbol}</div><h3>${title}</h3><p>${description}</p>${action ? `<div class="actions">${action}</div>` : ''}</div>`;
}
async function refresh({render = true} = {}) {
  state = await api('/api/state');
  selected = new Set([...selected].filter(id => state.invoices.some(i => i.id === id && i.status === 'available' && i.verified)));
  currentUser = state.user || currentUser;
  updateWorkspaceChrome();
  $('#invoice-count').textContent = state.invoices.length;
  if (render) renderPage();
}
function navigate() {
  if (!currentUser) return;
  const requested = location.hash.slice(1);
  activePage = Object.hasOwn(pageNames,requested) ? requested : 'invoices';
  renderPage();
}
function renderPage() {
  document.body.classList.toggle('match-page',activePage==='match');
  $$('[data-page]').forEach(link => {
    const current = link.dataset.page === activePage;
    link.classList.toggle('active',current);
    if (current) link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current');
  });
  $('#breadcrumb').textContent = pageNames[activePage];
  document.title = `${pageNames[activePage]} · 票栈`;
  const renderers = {invoices:renderInvoices,match:renderMatch,batches:renderBatches,settings:renderSettings,data:renderData,groups:renderGroups,mailboxes:renderMailboxes,account:renderAccount};
  $('#main').innerHTML = renderers[activePage]();
  if (activePage === 'invoices') updateInvoiceTable();
  if (activePage === 'mailboxes' && mailboxData === null && !mailboxLoading) loadMailboxes();
  if (activePage === 'mailboxes' && receiptData === null && !receiptLoading && !receiptError) loadReceiptAddresses();
}
function renderInvoices() {
  const s = state.summary;
  const pending = s.pending_review ?? state.invoices.filter(i => !i.verified).length;
  const available = state.invoices.filter(i => i.verified && i.status === 'available');
  const submitted = state.invoices.filter(i => i.status === 'submitted');
  return heading('YOUR INVOICE LIBRARY',activeGroupId?'群组票库':'我的票库',activeGroupId?'查看分享至本组的发票，协作整理报销。':'收好每一张发票，让报销井井有条。',`<button class="btn" data-action="new-invoice">手动录入</button><button class="btn primary" data-action="import"><span class="plus">＋</span>导入发票</button>`) +
  `<section class="stats" aria-label="票库概览"><div class="stat featured"><div class="stat-label">可报销金额 <span>待使用</span></div><div class="stat-value"><small>¥</small>${fmt(s.available_cents)}</div><p class="stat-note">${available.length} 张已核对发票，随时可用</p></div><div class="stat"><div class="stat-label">待核对发票</div><div class="stat-value">${pending}<small> 张</small></div><p class="stat-note">核对票面信息后即可参与匹配</p></div><div class="stat"><div class="stat-label">已提交 · 等待报销</div><div class="stat-value"><small>¥</small>${fmt(s.submitted_cents)}</div><p class="stat-note">${submitted.length} 张发票已提交</p></div></section>
  <section class="upload-strip" id="drop-zone" aria-label="拖放导入发票"><div class="upload-mark" aria-hidden="true">↥</div><div class="upload-copy"><h3>把发票放进票栈</h3><p>拖入 PDF、图片、OFD、XML 或 ZIP 批量导入</p></div><button class="btn" data-action="import">选择文件</button></section>
  <section class="panel" aria-label="发票列表"><div class="filters"><label class="search-field"><span aria-hidden="true">⌕</span><input id="invoice-search" data-filter="q" value="${esc(filters.q)}" placeholder="搜索销售方、发票号码、抬头…" aria-label="搜索发票"></label><select data-filter="category" aria-label="筛选类别">${categoryOptions(filters.category)}</select><select data-filter="status" aria-label="筛选状态"><option value="">全部状态</option>${[['review','待核对'],['available','可报销'],['draft','草稿占用'],['submitted','已提交'],['paid','已报销']].map(([value,label])=>`<option value="${value}" ${filters.status===value?'selected':''}>${label}</option>`).join('')}</select><button class="btn small" data-action="toggle-filters" aria-expanded="false">更多筛选 <span aria-hidden="true">⌄</span></button><div class="filter-details" id="extra-filters" hidden><select data-filter="project_id" aria-label="筛选项目">${projectOptions(filters.project_id)}</select><input type="date" data-filter="date_from" value="${esc(filters.date_from)}" aria-label="最早开票日期"><input type="date" data-filter="date_to" value="${esc(filters.date_to)}" aria-label="最晚开票日期"><input inputmode="decimal" data-filter="min" value="${esc(filters.min)}" placeholder="最低金额（元）" aria-label="最低金额"><input inputmode="decimal" data-filter="max" value="${esc(filters.max)}" placeholder="最高金额（元）" aria-label="最高金额"><button class="btn small ghost" data-action="clear-filters">清空筛选</button></div></div><div class="table-meta"><span id="table-count"></span><span>人民币 CNY</span></div><div id="selection-bar" class="selection-bar" hidden></div><div id="invoice-table"></div></section>`;
}
function visibleInvoices() {
  return state.invoices.filter(i => {
    const q = filters.q.trim().toLocaleLowerCase();
    if (q && ![i.seller,i.number,i.buyer,i.note,i.file_name].join(' ').toLocaleLowerCase().includes(q)) return false;
    if (filters.category && i.category !== filters.category) return false;
    if (filters.project_id && i.project_id !== filters.project_id) return false;
    if (filters.status === 'review' && i.verified) return false;
    if (filters.status === 'available' && (i.status !== 'available' || !i.verified)) return false;
    if (filters.status && !['review','available'].includes(filters.status) && i.status !== filters.status) return false;
    if (filters.date_from && (!i.date || i.date < filters.date_from)) return false;
    if (filters.date_to && (!i.date || i.date > filters.date_to)) return false;
    if (filters.min !== '' && i.amount_cents < Number(filters.min)*100) return false;
    if (filters.max !== '' && i.amount_cents > Number(filters.max)*100) return false;
    return true;
  });
}
function updateInvoiceTable() {
  const invoices = visibleInvoices();
  $('#table-count').textContent = `共 ${invoices.length} 张发票${invoices.length !== state.invoices.length ? ` · 票库 ${state.invoices.length} 张` : ''}`;
  updateSelection();
  if (!invoices.length) {
    $('#invoice-table').innerHTML = state.invoices.length ? empty('没有符合条件的发票','调整筛选条件，再找找看。','<button class="btn" data-action="clear-filters">清空筛选</button>') : empty('你的第一张发票，从这里开始','导入电子发票，核对信息后，就可以匹配金额、整理报销单。','<button class="btn primary" data-action="import">导入第一张发票</button><button class="btn ghost" data-action="new-invoice">手动录入</button>');
    return;
  }
  const eligible = invoices.filter(i=>i.verified && i.status === 'available');
  $('#invoice-table').innerHTML = `<div class="table-scroll"><table><thead><tr><th><input type="checkbox" id="select-all" aria-label="选中当前筛选下所有可报销发票" ${eligible.length && eligible.every(i=>selected.has(i.id))?'checked':''} ${!eligible.length?'disabled':''}></th><th>发票信息</th><th>类别 / 项目</th><th>开票日期</th><th class="align-right">价税合计</th><th>状态</th><th class="align-right">操作</th></tr></thead><tbody>${invoices.map(i=>`<tr><td><input type="checkbox" data-select="${esc(i.id)}" aria-label="选择 ${esc(i.seller || i.file_name || '发票')}" ${selected.has(i.id)?'checked':''} ${i.verified && i.status === 'available'?'':'disabled'}></td><td><button class="link-button invoice-title" data-action="edit-invoice" data-id="${esc(i.id)}" title="${esc(i.seller || i.file_name)}">${esc(i.seller || i.file_name || '待补充销售方')}</button><span class="invoice-sub number">${esc(i.number || '待补充发票号码')}</span></td><td><span class="tag">${esc(i.category || '其他')}</span><span class="invoice-sub">${esc(projectName(i.project_id))}${activeGroupId && i.owner_name?' · '+esc(i.owner_name):''}</span></td><td class="number text-muted">${esc(i.date || '待核对')}</td><td class="align-right money">¥ ${fmt(i.amount_cents)}</td><td>${invoiceBadge(i)}</td><td class="align-right"><div class="invoice-row-actions"><button class="link-button" data-action="preview-original" data-url="${esc(i.file_url)}" data-original-id="${esc(i.id)}" data-filename="${esc(i.file_name)}" ${i.file_url?'':'disabled title="此记录没有原件"'}>预览</button><button class="link-button" data-action="edit-invoice" data-id="${esc(i.id)}">${i.status==='available' && i.can_edit!==false?'核对':'查看'}</button></div></td></tr>`).join('')}</tbody></table></div>`;
}
function updateSelection() {
  const bar = $('#selection-bar');
  if (!bar) return;
  bar.hidden = selected.size === 0;
  const total = state.invoices.filter(i => selected.has(i.id)).reduce((sum,i)=>sum+i.amount_cents,0);
  bar.innerHTML = `<span>已选 ${selected.size} 张</span><button class="link-button" data-action="clear-selection">取消选择</button><strong>¥ ${fmt(total)}</strong><button class="btn primary small" data-action="create-selected">创建报销草稿</button>`;
}
function renderMatch() {
  return `<div class="page-heading match-heading"><h1>智能匹配</h1><p class="subtitle">给出目标金额，找一组刚刚好的发票。</p></div>` + `<div class="two-column match-layout"><section class="panel match-form" tabindex="0" aria-label="匹配条件"><div class="panel-header"><h2>设置匹配条件</h2></div><form data-form="match" class="panel-body form-stack"><label class="field wide"><span>目标金额（元）</span><div class="currency-input"><input class="target-input" name="target" inputmode="decimal" placeholder="0.00" value="${esc(matchValues.target)}" required autocomplete="off" aria-label="目标金额（元）"></div><small>最多 100,000 元，支持精确到分</small></label><fieldset class="match-direction wide" aria-describedby="match-direction-help"><legend>匹配方式</legend><div class="match-direction-options"><label><input type="radio" name="direction" value="above" ${matchValues.direction==='above'?'checked':''}>稍多</label><label><input type="radio" name="direction" value="below" ${matchValues.direction==='below'?'checked':''}>稍少</label></div><small id="match-direction-help">稍多：恰好或超出最少；稍少：恰好或低于且最接近目标。</small></fieldset><label class="field"><span>发票类别</span><select name="category">${categoryOptions(matchValues.category)}</select></label><label class="field"><span>所属项目</span><select name="project_id">${projectOptions(matchValues.project_id)}</select></label><div class="form-grid wide"><label class="field"><span>开票日期 · 从</span><input type="date" name="date_from" value="${esc(matchValues.date_from)}"></label><label class="field"><span>至</span><input type="date" name="date_to" value="${esc(matchValues.date_to)}"></label></div><label class="field wide"><span>发票抬头</span><input name="buyer" value="${esc(matchValues.buyer)}" placeholder="填写完整抬头，精确匹配"></label><label class="field wide"><span>月度报销额度</span><select name="quota_id">${quotaOptions(matchValues.quota_id)}</select><small>选择额度后，匹配金额受剩余额度约束。</small></label><div class="inline-error wide" data-error></div><button class="btn primary wide" type="submit">开始匹配 <span aria-hidden="true">→</span></button><p class="hint wide">仅使用已核对、未被报销单占用的发票。</p></form></section><section id="match-results" aria-live="polite" tabindex="0" aria-label="匹配结果">${renderMatchResults()}</section></div>`;
}
function renderMatchResults() {
  if (!matchData) return `<div class="panel">${empty('让金额，刚好合适','填写目标金额与筛选条件，票栈会为你寻找可用发票组合。','','⌘')}</div>`;
  if (!matchData.combinations?.length) return `<div class="panel">${empty(matchData.direction==='below'?'暂无合适组合':'额度不足',`${esc(matchData.message)} 本次找到 ${matchData.candidate_count || 0} 张候选发票。`,'<a class="btn" href="#invoices">返回票库</a>','⌘')}</div>`;
  return `<div class="list-head"><h2>可选组合</h2><span class="result-count">${matchData.candidate_count} 张候选 · ${matchData.direction==='below'?'稍少':'稍多'} · ${matchData.combinations.length} 个方案</span></div>${matchData.combinations.map((combo,index)=>{
    const gap = combo.total_cents - matchData.target_cents;
    return `<article class="match-result"><div class="result-top"><div><h3>方案 ${String(index+1).padStart(2,'0')} ${gap===0?'<span class="badge available">精确匹配</span>':''}</h3><p>${gap===0?'金额刚好，正合所需':`${gap>0?'超出':'低于'}目标 ¥${fmt(Math.abs(gap))}`}</p></div><div class="result-amount">¥ ${fmt(combo.total_cents)}</div></div><div class="mini-list">${combo.invoices.map(i=>`<div class="mini-item"><div>${esc(i.seller || i.file_name || '未命名发票')}<div class="minor">${esc(i.date || '未填写日期')} · ${esc(i.category)}</div></div><span class="money">¥ ${fmt(i.amount_cents)}</span></div>`).join('')}</div><div class="result-footer"><span>${combo.invoice_ids.length} 张发票 · 创建后将被草稿占用</span><button class="btn primary small" data-action="use-combination" data-index="${index}">选择此组合</button></div></article>`;
  }).join('')}`;
}
function renderBatches() {
  const batches = state.batches.filter(b => !batchFilter || b.status === batchFilter);
  return heading('REIMBURSEMENT','报销单','从草稿到到账，记录每一次报销。','<a class="btn primary" href="#match">匹配发票建单</a>') + `<div class="tabs" role="group" aria-label="筛选报销单状态">${[['','全部'],['draft','草稿'],['submitted','已提交'],['paid','已报销'],['rejected','已退回'],['cancelled','已撤销']].map(([value,label])=>`<button class="${batchFilter===value?'active':''}" data-action="batch-filter" data-value="${value}">${label}</button>`).join('')}</div>${batches.length?`<div class="batch-grid">${batches.map(b=>`<article class="batch-card"><div class="card-top"><div><h3>${esc(b.name)}</h3><p class="number">${esc(String(b.created_at || '').slice(0,10))} 创建</p></div><span class="badge ${esc(b.status)}">${esc(batchNames[b.status])}</span></div><div class="batch-total">¥ ${fmt(b.total_cents)}</div><p class="batch-detail">${b.invoice_ids?.length || b.items?.length || 0} 张发票 · ${esc(projectName(b.project_id))} · ${esc(b.month || '')}</p><div class="batch-actions"><button class="btn small" data-action="batch-detail" data-id="${esc(b.id)}">查看明细</button><button class="btn small" data-action="download" data-url="/api/batches/${encodeURIComponent(b.id)}/export" data-filename="报销单.zip">导出报销包</button>${b.can_manage!==false && b.status==='draft'?`<button class="btn primary small" data-action="batch-status" data-id="${esc(b.id)}" data-status="submitted">标记已提交</button>`:''}${b.can_manage!==false && b.status==='submitted'?`<button class="btn primary small" data-action="batch-status" data-id="${esc(b.id)}" data-status="paid">标记已报销</button>`:''}</div></article>`).join('')}</div>`:`<div class="panel">${empty('还没有'+(batchFilter?batchNames[batchFilter]:'')+'报销单','在票库勾选发票，或通过智能匹配选择组合，即可创建一张报销草稿。','<a class="btn primary" href="#match">去匹配发票</a><a class="btn ghost" href="#invoices">去票库选票</a>','☷')}</div>`}`;
}
function renderSettings() {
  return heading('KEEP IT ORGANIZED','项目与额度','按项目归集，按月份安排报销。') + `<div class="settings-grid"><section class="panel"><div class="panel-header"><div><h2>项目</h2><p>给相关的发票一个共同归属</p></div><button class="btn small" data-action="new-project">＋ 新建</button></div>${state.projects.length?`<div class="simple-list">${state.projects.map(p=>`<div class="project-item"><div class="quota-head"><h3>${esc(p.name)}</h3><span class="project-count">${state.invoices.filter(i=>i.project_id===p.id).length} 张发票</span></div>${p.note?`<p>${esc(p.note)}</p>`:''}</div>`).join('')}</div>`:empty('项目，让发票更有条理','差旅、日常办公、客户项目，都可以单独归集。','<button class="btn" data-action="new-project">新建第一个项目</button>','⊞')}</section><section class="panel"><div class="panel-header"><div><h2>月度报销额度</h2><p>创建草稿时选择额度，即计入已用金额</p></div>${canManageQuota()?'<button class="btn small" data-action="new-quota">＋ 新建</button>':''}</div>${state.quotas.length?`<div class="simple-list">${state.quotas.map(q=>`<div class="quota-item"><div class="quota-head"><div><span class="number">${esc(q.month)}</span> <span class="tag">${esc(q.category)}</span></div><strong>¥ ${fmt(q.amount_cents)}</strong></div><div class="progress-track" role="progressbar" aria-label="${esc(q.month)} ${esc(q.category)}额度使用情况" aria-valuemin="0" aria-valuemax="${q.amount_cents}" aria-valuenow="${q.used_cents}"><div class="progress-fill" style="width:${Math.min(100,Math.max(0,q.amount_cents?q.used_cents/q.amount_cents*100:0))}%"></div></div><div class="quota-bottom"><span>已用 ¥${fmt(q.used_cents)} · 剩余 ¥${fmt(q.remaining_cents)}</span>${canManageQuota() && !state.batches.some(b=>b.quota_id===q.id)?`<button class="link-button" data-action="delete-quota" data-id="${esc(q.id)}">删除</button>`:''}</div></div>`).join('')}</div><div class="panel-body"><p class="hint">只有关联该额度的报销单才计入使用；草稿、已提交和已报销均占用额度，撤销或退回后释放。月份为报销月份。</p></div>`:empty('给这个月留一笔额度','按类别设置月度上限，选择发票时心里更有数。',canManageQuota()?'<button class="btn" data-action="new-quota">设置月度额度</button>':'','◷')}</section></div>`;
}
function renderAccount() {
  return heading('YOUR ACCOUNT','账号设置','管理你的个人资料和登录密码。') + `<div class="settings-grid"><section class="panel"><div class="panel-header"><h2>个人资料</h2></div><form data-form="profile" class="panel-body form-stack"><label class="field"><span>用户名</span><input name="name" value="${esc(currentUser.name)}" autocomplete="nickname" maxlength="80" required><small>支持中文，仅用于显示，不用于登录。</small></label><label class="field"><span>登录邮箱</span><input name="email" type="email" value="${esc(currentUser.email)}" autocomplete="email" maxlength="254" required></label><label class="field"><span>当前密码</span><input name="current_password" type="password" autocomplete="current-password" maxlength="128" required></label><p class="hint">修改邮箱后，请使用新邮箱登录，其他设备需要重新登录。收票地址与已绑定的收件箱保持原有归属。</p><div class="inline-error" data-error role="alert"></div><button class="btn primary" type="submit">保存资料</button></form></section><section class="panel"><div class="panel-header"><h2>修改密码</h2></div><form data-form="password" class="panel-body form-stack"><label class="field"><span>当前密码</span><input name="current_password" type="password" autocomplete="current-password" maxlength="128" required></label><label class="field"><span>新密码</span><input name="new_password" type="password" autocomplete="new-password" minlength="10" maxlength="128" required><small>10 至 128 个字符</small></label><label class="field"><span>确认新密码</span><input name="confirm_password" type="password" autocomplete="new-password" minlength="10" maxlength="128" required></label><p class="hint">修改成功后，当前设备保持登录，其他设备需要重新登录。</p><div class="inline-error" data-error role="alert"></div><button class="btn primary" type="submit">修改密码</button></form></section></div>`;
}
function renderData() {
  return heading('YOUR DATA, WITH YOU','数据管理','导出当前空间的发票账目，保存一份完整记录。') + `<div class="data-grid"><section class="data-card"><div class="data-icon" aria-hidden="true">▦</div><h2>导出票库台账</h2><p>将${activeGroupId?'当前群组共享':'个人'}发票的信息、金额、类别与报销状态导出为 Excel，方便查阅和进一步整理。</p><button class="btn primary" data-action="download" data-url="/api/export" data-filename="发票台账.xlsx">导出 Excel <span aria-hidden="true">↓</span></button><span class="file-type">.XLSX</span></section><section class="data-card"><div class="data-icon" aria-hidden="true">▣</div><h2>个人数据备份</h2><p>${activeGroupId?'完整备份仅在个人空间提供。切换到个人空间后，可以下载你自己的发票信息与原件。':'打包你自己的发票信息与原件，不包含其他用户的私人数据。建议定期备份到你信任的位置。'}</p>${activeGroupId?'<button class="btn" data-action="personal-space">切换到个人空间</button>':'<button class="btn" data-action="download" data-url="/api/backup" data-filename="票栈备份.zip">下载个人备份 <span aria-hidden="true">↓</span></button><span class="file-type">.ZIP</span>'}</section></div><div class="data-note"><strong>${activeGroupId?'当前群组':'你的个人空间'}</strong><br>共有 ${state.invoices.length} 张发票、${state.batches.length} 张报销单。个人发票仅本人可见；分享后的发票会对指定群组成员开放。${state.capabilities.ocr?'':' 图片和扫描件导入后需要手动补充信息。'}</div>`;
}
function openModal(title,subtitle,content,footer = '',formName = '',formData = '') {
  cleanupOriginalViewers(modal);
  modal.classList.toggle('invoice-dialog',formName==='invoice');
  modal.innerHTML = `<div class="modal-head"><div><h2 id="modal-title">${title}</h2>${subtitle?`<p>${subtitle}</p>`:''}</div><button class="icon-btn" data-action="close-modal" aria-label="关闭对话框">×</button></div>${formName?`<form data-form="${formName}" ${formData}>`:''}<div class="modal-body">${content}<div class="inline-error spaced" data-error></div></div>${footer?`<div class="modal-footer">${footer}</div>`:''}${formName?'</form>':''}`;
  if (!modal.open) modal.showModal();
  const focusTarget = $('input:not([type=hidden]):not(:disabled),select:not(:disabled),textarea:not(:disabled)',modal);
  if (focusTarget) focusTarget.focus({preventScroll:true});
}
function closeModal() { if (!modalBusy) modal.close(); }
function invoiceModal(id) {
  const i = state.invoices.find(x=>x.id===id) || {id:'',seller:'',buyer:'',date:localDate(),category:'其他',number:'',amount_cents:0,note:'',verified:false,status:'available'};
  const locked = i.id && (i.status !== 'available' || i.can_edit === false);
  const owned = !i.owner_id || i.owner_id === currentUser?.id;
  const disabled = locked?'disabled':'';
  const fields = `<div class="form-grid"><label class="field wide"><span>销售方</span><input name="seller" value="${esc(i.seller)}" placeholder="票面销售方名称" maxlength="200" ${disabled}></label><label class="field wide"><span>购买方 / 发票抬头</span><input name="buyer" value="${esc(i.buyer)}" placeholder="票面购买方名称" maxlength="200" ${disabled}></label><label class="field"><span>发票号码</span><input name="number" value="${esc(i.number)}" placeholder="保留完整号码" maxlength="100" ${disabled}></label><label class="field"><span>开票日期</span><input type="date" name="date" value="${esc(i.date)}" ${disabled}></label><label class="field"><span>价税合计（元）</span><input name="amount" value="${i.amount_cents?(i.amount_cents/100).toFixed(2):''}" inputmode="decimal" placeholder="0.00" ${disabled}></label><label class="field"><span>类别</span><select name="category" ${disabled}>${categories.map(c=>`<option ${c===i.category?'selected':''}>${c}</option>`).join('')}</select></label><label class="field wide"><span>所属项目</span><select name="project_id" ${disabled}>${projectOptions(i.project_id,'未分配项目')}</select></label><label class="field wide"><span>备注</span><textarea name="note" maxlength="1000" placeholder="用途、行程或其他说明" ${disabled}>${esc(i.note)}</textarea></label></div>`;
  const original = i.file_url?`<section class="invoice-original"><div class="original-link"><span>${esc(i.file_name || '发票原件')}</span>${originalActions(i,'','放大预览')}</div>${originalViewerMarkup(i.file_url,i.file_name)}</section>`:'';
  const review = i.review_note && !i.verified?`<p class="hint warn" style="margin-bottom:20px">${esc(i.review_note)}</p>`:'';
  const lockedNote = locked?`<p class="hint" style="margin-bottom:20px">${i.owner_id && !owned?'这张发票由 '+esc(i.owner_name || '群组成员')+' 分享，票面信息由上传者维护。':'这张发票已被报销单占用，当前仅可查看。'}</p>`:'';
  const hasHistory = state.batches.some(b => (b.invoice_ids || []).includes(i.id) || (b.items || []).some(item => item.id===i.id || item.invoice_id===i.id));
  openModal(i.id?(locked?'发票详情':'核对发票'):'手动录入发票',i.id?'查看原件，确认票面信息。':'填写票面信息后确认核对。',original+review+lockedNote+fields,`${i.id && owned?`<button type="button" class="btn" data-action="invoice-shares" data-id="${esc(i.id)}">群组分享</button>`:''}${i.id && !locked && !hasHistory?`<button type="button" class="btn danger left" data-action="delete-invoice" data-id="${esc(i.id)}">删除</button>`:''}<button type="button" class="btn" data-action="close-modal">${locked?'关闭':'取消'}</button>${!locked?'<button class="btn primary" type="submit">确认核对</button>':''}`,'invoice',`data-id="${esc(i.id)}"`);
  const viewer = $('[data-original-viewer]',modal);
  if (viewer) loadOriginalViewer(viewer);
}
function batchModal(invoiceIds,quotaId = '') {
  const invoices = state.invoices.filter(i=>invoiceIds.includes(i.id));
  if (!invoices.length) {toast('请先选择发票。');return;}
  const sharedProject = invoices.every(i=>i.project_id===invoices[0].project_id)?invoices[0].project_id:'';
  const quota = state.quotas.find(q=>q.id===quotaId);
  const total = invoices.reduce((sum,i)=>sum+i.amount_cents,0);
  const content = `<p class="import-summary">${invoices.length} 张发票 · 合计 ¥ ${fmt(total)}</p><div class="form-stack"><label class="field"><span>报销单名称</span><input name="name" value="${esc(localDate()+' 报销')}" maxlength="100" required></label><div class="form-grid"><label class="field"><span>所属项目</span><select name="project_id">${projectOptions(sharedProject,'未分配项目')}</select></label><label class="field"><span>报销月份</span><input type="month" name="month" value="${esc(quota?.month || currentMonth())}" required></label></div><label class="field"><span>关联月度额度</span><select name="quota_id">${quotaOptions(quotaId)}</select></label><p class="hint">创建草稿后，发票将被占用。选择额度后，草稿金额计入该月已用额度；月份与类别需符合所选额度。</p><details><summary class="link-button">查看 ${invoices.length} 张发票</summary><div class="mini-list">${invoices.map(i=>`<div class="mini-item"><div>${esc(i.seller || i.file_name)}<div class="minor">${esc(i.date)}</div></div><span class="money">¥ ${fmt(i.amount_cents)}</span></div>`).join('')}</div></details></div>`;
  openModal('创建报销草稿','确认这组发票的归属与报销月份。',content,'<button class="btn" type="button" data-action="close-modal">取消</button><button class="btn primary" type="submit">创建草稿</button>','batch',`data-ids="${esc(JSON.stringify(invoiceIds))}"`);
}
function originalActions(invoice, batchId = '', previewLabel = '预览') {
  if (!invoice.file_url) return '<span class="minor">无原件</span>';
  const attributes = `data-url="${esc(invoice.file_url)}" data-original-id="${esc(invoice.id)}" data-batch="${esc(batchId)}" data-filename="${esc(invoice.original_file_name || invoice.file_name || '发票原件')}"`;
  return `<div class="original-actions"><button type="button" class="btn small" data-action="preview-original" ${attributes}>${esc(previewLabel)}</button><button type="button" class="btn small" data-action="download" ${attributes} data-original-download="true">下载</button></div>`;
}
function originalViewerMarkup(url, name) {
  return `<div class="original-viewer" data-original-viewer data-url="${esc(url)}" data-filename="${esc(name)}"><div class="preview-toolbar" hidden><button type="button" class="btn small" data-action="preview-page" data-step="-1">上一页</button><span class="number" data-preview-pages aria-live="polite"></span><button type="button" class="btn small" data-action="preview-page" data-step="1">下一页</button></div><div class="preview-content" aria-live="polite"></div></div>`;
}
function cleanupOriginalViewers(root) {
  for (const [viewer, data] of originalViewers) {
    if (!root.contains(viewer)) continue;
    data.controller.abort();
    if (data.objectURL) URL.revokeObjectURL(data.objectURL);
    originalViewers.delete(viewer);
  }
}
async function loadOriginalViewer(viewer, page = 1) {
  cleanupOriginalViewers(viewer);
  const data = {controller:new AbortController(),objectURL:null,page};
  originalViewers.set(viewer,data);
  const signal = data.controller.signal;
  const name = viewer.dataset.filename || '原件';
  const extension = name.split('.').pop().toLowerCase();
  const content = $('.preview-content',viewer);
  const toolbar = $('.preview-toolbar',viewer);
  const buttons = $$('button',toolbar);
  toolbar.hidden = extension!=='pdf';
  buttons.forEach(button=>button.disabled=true);
  content.innerHTML='<div class="loading-state"><span class="spinner"></span><p>正在加载原件…</p></div>';
  if (!['pdf','png','jpg','jpeg','webp','xml'].includes(extension)) {
    content.innerHTML='<p class="preview-message">此格式暂不支持页内预览，请下载原件查看。</p>';
    return;
  }
  try {
    const response = await fetch(viewer.dataset.url+(extension==='pdf'?'?preview_page='+page:''),{signal});
    if (!response.ok) {const error=await response.json().catch(()=>({}));throw new Error(error.detail || '原件加载失败，请重试。');}
    let element;
    if (extension==='xml') {
      element=document.createElement('pre');element.className='preview-xml';element.textContent=await response.text();
    } else {
      const blob=await response.blob();
      if (signal.aborted) return;
      data.objectURL=URL.createObjectURL(blob);
      element=document.createElement('img');element.src=data.objectURL;
      element.alt=extension==='pdf'?'PDF 原件 · 第 '+page+' 页':name;
      element.addEventListener('error',()=>{content.textContent='图片无法显示，请下载原件查看。';});
    }
    if (signal.aborted) return;
    content.replaceChildren(element);content.scrollTop=0;
    if (extension==='pdf') {
      const total=Number(response.headers.get('X-Preview-Pages')) || 1;
      $('[data-preview-pages]',viewer).textContent=page+' / '+total;
      buttons[0].disabled=page<=1;buttons[1].disabled=page>=total;
    }
  } catch(error) {
    if (!signal.aborted) {content.textContent=error.message;toolbar.hidden=true;}
  }
}
async function previewOriginal(button) {
  cleanupOriginalViewers(preview);
  const name=button.dataset.filename;
  preview.innerHTML=`<div class="modal-head"><div><h2 id="preview-title">原件预览</h2><p>${esc(name)}</p></div><button type="button" class="icon-btn" data-action="close-preview" aria-label="关闭预览">×</button></div>${originalViewerMarkup(button.dataset.url,name)}<div class="modal-footer"><button type="button" class="btn" data-action="close-preview">返回</button><button type="button" class="btn primary" data-action="download" data-original-download="true" data-url="${esc(button.dataset.url)}" data-original-id="${esc(button.dataset.originalId)}" data-batch="${esc(button.dataset.batch || '')}" data-filename="${esc(name)}">下载原件</button></div>`;
  if (!preview.open) preview.showModal();
  await loadOriginalViewer($('[data-original-viewer]',preview));
}
preview.addEventListener('close',()=>{cleanupOriginalViewers(preview);preview.replaceChildren();});
modal.addEventListener('close',()=>cleanupOriginalViewers(modal));
preview.addEventListener('click',event=>{
  if(event.target!==preview)return;
  const rect=preview.getBoundingClientRect();
  if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)preview.close();
});
function batchDetail(id) {
  const b = state.batches.find(x=>x.id===id);
  if (!b) return;
  const quota = state.quotas.find(q=>q.id===b.quota_id);
  const items = b.items?.length?b.items:state.invoices.filter(i=>(b.invoice_ids || []).includes(i.id));
  const files = items.filter(i=>i.file_url);
  openModal(esc(b.name),`<span class="badge ${esc(b.status)}">${esc(batchNames[b.status])}</span>`, `<div class="batch-total">¥ ${fmt(b.total_cents)}</div><dl class="detail-grid"><div><dt>所属项目</dt><dd>${esc(projectName(b.project_id))}</dd></div><div><dt>报销月份</dt><dd>${esc(b.month)}</dd></div><div><dt>月度额度</dt><dd>${quota?esc(quota.month+' · '+quota.category):'未关联额度'}</dd></div><div><dt>发票数量</dt><dd>${items.length} 张</dd></div></dl>${files.length?`<section class="batch-download-options"><div class="batch-download-heading"><label class="check-label"><input type="checkbox" data-rename-originals><span>下载时重命名原件</span></label><button type="button" class="btn small" data-action="download" data-batch-export="true" data-originals-only="true" data-url="/api/batches/${encodeURIComponent(b.id)}/export">批量下载原件（${files.length}）</button></div><p class="hint">勾选后按「序号-类别-金额」命名，可逐张修改。未勾选时保留原文件名；重名文件自动加后缀。</p></section>`:''}<div class="mini-list batch-invoices">${items.map((i,index)=>`<div class="mini-item batch-invoice"><div class="batch-invoice-info">${esc(i.seller || i.file_name || '发票')}<div class="minor">${esc(i.date)} · ${esc(i.category)} · ${esc(i.number || '未填写号码')}</div>${i.file_url?`<label class="rename-field" data-rename-field hidden><span>下载文件名</span><div><input data-export-name="${esc(i.id)}" aria-label="第 ${index+1} 张原件的下载文件名" value="${esc(String(index+1).padStart(3,'0')+'-'+i.category+'-'+(i.amount_cents/100).toFixed(2))}" required maxlength="100" disabled><span>.${esc((i.original_file_name || i.file_name).split('.').pop())}</span></div></label>`:''}</div><div class="batch-invoice-actions"><span class="money">¥ ${fmt(i.amount_cents)}</span>${originalActions(i,b.id)}</div></div>`).join('')}</div>`,`${b.can_manage!==false && b.status==='draft'?`<button type="button" class="btn danger left" data-action="batch-status" data-id="${esc(b.id)}" data-status="cancelled">撤销草稿</button>`:''}${b.can_manage!==false && b.status==='submitted'?`<button type="button" class="btn danger left" data-action="batch-status" data-id="${esc(b.id)}" data-status="rejected">标记退回</button>`:''}<button type="button" class="btn" data-action="download" data-batch-export="true" data-url="/api/batches/${encodeURIComponent(b.id)}/export">导出报销包</button><button type="button" class="btn primary" data-action="close-modal">完成</button>`);
}

async function importFiles(files) {
  if (!files.length || modalBusy) return;
  openModal('正在导入发票',`共 ${files.length} 个文件，请稍候。`,'<div class="loading-state" style="padding:30px"><span class="spinner"></span><p>读取票面信息并检查重复文件…</p></div>');
  modalBusy = true;
  $('[data-action="close-modal"]',modal).disabled = true;
  try {
    const body = new FormData();
    for (const file of files) body.append('files',file);
    const result = await api('/api/import',{method:'POST',body});
    await refresh();
    const imported = result.imported || [], duplicates = result.duplicates || [], errors = result.errors || [];
    modalBusy = false;
    openModal('导入完成',activeGroupId?'原件已保存，并分享至当前群组。':'原件已保存到你的个人票栈。',`<p class="import-summary">已导入 ${imported.length} 张 · 跳过重复 ${duplicates.length} 个 · 失败 ${errors.length} 个</p>${imported.length?'<p class="hint">导入的发票需要核对票面信息，确认后才会参与金额匹配。</p>':''}${duplicates.length?`<h3 class="spaced">重复文件</h3><ul class="import-list">${duplicates.map(name=>`<li>${esc(typeof name==='string'?name:name.filename)}</li>`).join('')}</ul>`:''}${errors.length?`<h3 class="spaced">未导入的文件</h3><ul class="import-list">${errors.map(error=>`<li>${esc(error.filename)}：${esc(error.error)}</li>`).join('')}</ul>`:''}`,`${imported.length?'<button class="btn primary" data-action="review-imported">去核对发票</button>':'<button class="btn primary" data-action="close-modal">完成</button>'}`);
  } catch (error) {
    modalBusy = false;
    openModal('导入未完成','请检查文件后重试。',`<p class="inline-error">${esc(error.message)}</p>`,'<button class="btn primary" data-action="close-modal">知道了</button>');
  } finally {modalBusy = false;$('#file-input').value='';}
}
async function download(button) {
  if (button.disabled) return;
  let path = button.dataset.url;
  let options = {};
  const rename = $('[data-rename-originals]',modal)?.checked || false;
  if (button.dataset.batchExport) {
    const inputs = $$('[data-export-name]',modal);
    if (rename && inputs.some(input=>!input.reportValidity())) return;
    options = {method:'POST',headers:{'X-Requested-With':'InvoiceDesk','Content-Type':'application/json'},body:JSON.stringify({rename,originals_only:button.dataset.originalsOnly==='true',filenames:rename?Object.fromEntries(inputs.map(input=>[input.dataset.exportName,input.value])):{}})};
  } else if (button.dataset.originalDownload) {
    const params = new URLSearchParams({download:'true'});
    if (button.dataset.batch && rename) {
      const input = $$('[data-export-name]',modal).find(input=>input.dataset.exportName===button.dataset.originalId);
      if (input) {if(!input.reportValidity())return;params.set('filename',input.value);}
    }
    path += '?' + params;
  }
  const original = button.textContent;
  button.disabled = true;
  button.textContent = '正在准备…';
  try {
    const response = await fetch(scopedURL(path),options);
    if (!response.ok) {
      const error = await response.json().catch(()=>({}));
      throw new Error(typeof error.detail==='string'?error.detail:'导出失败，请重试。');
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    const disposition = response.headers.get('Content-Disposition') || '';
    const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    const plain = disposition.match(/filename="?([^";]+)"?/i);
    let name = button.dataset.filename || '票栈导出.zip';
    try { if (encoded) name=decodeURIComponent(encoded[1]); else if(plain) name=plain[1]; } catch (_) {}
    anchor.href=url;anchor.download=name;document.body.append(anchor);anchor.click();anchor.remove();
    setTimeout(()=>URL.revokeObjectURL(url),30000);
    toast('文件已准备好，请查看浏览器下载。');
  } catch(error) {toast(error.message);} finally {button.disabled=false;button.textContent=original;}
}
async function runMutation(button,work) {
  if (button.disabled || modalBusy) return;
  button.disabled=true;
  if (modal.contains(button)) modalBusy=true;
  try {await work();} catch(error) {
    const target=modal.open?$('[data-error]',modal):null;
    if(target)target.textContent=error.message;else toast(error.message);
  } finally {button.disabled=false;modalBusy=false;}
}
document.addEventListener('click',async event=>{
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const action=button.dataset.action,id=button.dataset.id;
  if(action==='import'){$('#file-input').click();return;}
  if(action==='close-preview'){preview.close();return;}
  if(action==='preview-page'){const viewer=button.closest('[data-original-viewer]');const data=originalViewers.get(viewer);if(!button.disabled&&data)await loadOriginalViewer(viewer,data.page+Number(button.dataset.step));return;}
  if(action==='preview-original'){await previewOriginal(button);return;}
  if(action==='close-modal'){closeModal();return;}
  if(action==='new-invoice'){invoiceModal();return;}
  if(action==='edit-invoice'){invoiceModal(id);return;}
  if(action==='toggle-filters'){
    const details=$('#extra-filters');details.hidden=!details.hidden;button.setAttribute('aria-expanded',String(!details.hidden));return;
  }
  if(action==='clear-filters'){filters={q:'',category:'',status:'',project_id:'',date_from:'',date_to:'',min:'',max:''};renderPage();return;}
  if(action==='clear-selection'){selected.clear();updateInvoiceTable();return;}
  if(action==='create-selected'){batchModal([...selected]);return;}
  if(action==='use-combination'){batchModal(matchData.combinations[Number(button.dataset.index)].invoice_ids,matchValues.quota_id);return;}
  if(action==='batch-filter'){batchFilter=button.dataset.value;renderPage();return;}
  if(action==='batch-detail'){batchDetail(id);return;}
  if(action==='download'){await download(button);return;}
  if(action==='review-imported'){closeModal();filters.status='review';if(activePage==='invoices')renderPage();else location.hash='invoices';return;}
  if(action==='new-project'){
    openModal('新建项目','按用途归集相关发票。','<div class="form-stack"><label class="field"><span>项目名称</span><input name="name" placeholder="例如：上海差旅" maxlength="100" required></label><label class="field"><span>备注（选填）</span><textarea name="note" maxlength="1000" placeholder="补充说明"></textarea></label></div>','<button class="btn" type="button" data-action="close-modal">取消</button><button class="btn primary" type="submit">创建项目</button>','project');return;
  }
  if(action==='new-quota'){
    openModal('设置月度额度','为一个月份与类别设置报销上限。',`<div class="form-stack"><label class="field"><span>报销月份</span><input name="month" type="month" value="${currentMonth()}" required></label><label class="field"><span>适用类别</span><select name="category"><option>全部</option>${categories.map(c=>`<option>${c}</option>`).join('')}</select></label><label class="field"><span>额度（元）</span><input name="amount" inputmode="decimal" placeholder="0.00" required></label><p class="hint">额度只统计创建报销草稿时主动关联它的报销单。</p></div>`,'<button class="btn" type="button" data-action="close-modal">取消</button><button class="btn primary" type="submit">保存额度</button>','quota');return;
  }
  if(action==='delete-invoice'){
    if(!confirm('删除这张发票及其原件？此操作无法撤销。'))return;
    await runMutation(button,async()=>{await api(`/api/invoices/${encodeURIComponent(id)}`,{method:'DELETE'});modal.close();await refresh();toast('发票已删除。');});return;
  }
  if(action==='delete-quota'){
    if(!confirm('删除这条未使用的月度额度？'))return;
    await runMutation(button,async()=>{await api(`/api/quotas/${encodeURIComponent(id)}`,{method:'DELETE'});await refresh();toast('额度已删除。');});return;
  }
  if(action==='batch-status'){
    const next=button.dataset.status;
    const prompts={submitted:'确认已向报销方提交这张报销单？',paid:'确认这笔报销款项已到账？',cancelled:'撤销这份草稿，并释放其中的发票和额度？',rejected:'标记为已退回，并释放其中的发票和额度？'};
    if(!confirm(prompts[next]))return;
    await runMutation(button,async()=>{await api(`/api/batches/${encodeURIComponent(id)}`,{method:'PATCH',body:JSON.stringify({status:next})});if(modal.open)modal.close();await refresh();matchData=null;toast(`报销单已${next==='cancelled'?'撤销':next==='rejected'?'标记退回':next==='paid'?'标记为已报销':'标记为已提交'}。`);});
  }
});
document.addEventListener('input',event=>{
  if(event.target.dataset.filter){filters[event.target.dataset.filter]=event.target.value;updateInvoiceTable();}
});
document.addEventListener('change',event=>{
  const el=event.target;
  if(el.matches('[data-rename-originals]')){$$('[data-rename-field]',modal).forEach(field=>{field.hidden=!el.checked;$('input',field).disabled=!el.checked;});}
  if(el.dataset.filter){filters[el.dataset.filter]=el.value;updateInvoiceTable();}
  if(el.dataset.select){if(el.checked)selected.add(el.dataset.select);else selected.delete(el.dataset.select);updateSelection();const all=$('#select-all');if(all){const eligible=visibleInvoices().filter(i=>i.verified&&i.status==='available');all.checked=eligible.length>0&&eligible.every(i=>selected.has(i.id));all.indeterminate=eligible.some(i=>selected.has(i.id))&&!all.checked;}}
  if(el.id==='select-all'){visibleInvoices().filter(i=>i.verified&&i.status==='available').forEach(i=>el.checked?selected.add(i.id):selected.delete(i.id));updateInvoiceTable();}
  if(el.id==='file-input')importFiles([...el.files]);
});
document.addEventListener('submit',async event=>{
  const form=event.target.closest('form[data-form]');
  if(!form)return;
  if (['login','register','profile','password','group','group-settings','transfer-group','dissolve-group','join-group','shares','mailbox','receipt-address'].includes(form.dataset.form)) {event.preventDefault();await submitOnlineForm(form);return;}
  event.preventDefault();
  const button=$('button[type=submit]',form);
  if(button.disabled || (modal.contains(form)&&modalBusy))return;
  const data=Object.fromEntries(new FormData(form));
  const errorBox=$('[data-error]',form);
  errorBox.textContent='';
  const originalText=button.textContent;
  button.disabled=true;button.textContent='正在处理…';
  const inModal=modal.contains(form);
  if(inModal)modalBusy=true;
  try {
    if(form.dataset.form==='invoice'){
      const id=form.dataset.id;
      const payload={number:data.number.trim(),seller:data.seller.trim(),buyer:data.buyer.trim(),date:data.date,category:data.category,project_id:data.project_id || null,note:data.note.trim(),amount_cents:data.amount?cents(data.amount):0,verified:true};
      if(!payload.seller||!payload.date||payload.amount_cents<=0)throw new Error('确认核对前，请补全销售方、开票日期和大于零的金额。');
      await api(id?`/api/invoices/${encodeURIComponent(id)}`:'/api/invoices',{method:id?'PATCH':'POST',body:JSON.stringify(payload)});
      modal.close();await refresh();matchData=null;toast('发票已确认核对。');
    }else if(form.dataset.form==='match'){
      matchData=null;
      matchValues={...data};
      const target=cents(data.target);
      if(target<=0||target>10000000)throw new Error('目标金额需大于 0 且不超过 100,000 元。');
      if(data.date_from&&data.date_to&&data.date_from>data.date_to)throw new Error('开始日期不能晚于结束日期。');
      const payload={target_cents:target,direction:data.direction};
      ['category','project_id','date_from','date_to','buyer','quota_id'].forEach(key=>{if(data[key])payload[key]=data[key];});
      $('#match-results').innerHTML='<div class="panel loading-state"><span class="spinner"></span><p>正在寻找合适的发票组合…</p></div>';
      const requestedScope=activeGroupId;
      const matchResponse=await api('/api/match',{method:'POST',body:JSON.stringify(payload)});
      if(requestedScope===activeGroupId){matchData=matchResponse;if ($('#match-results')) $('#match-results').innerHTML=renderMatchResults();}
    }else if(form.dataset.form==='batch'){
      await api('/api/batches',{method:'POST',body:JSON.stringify({name:data.name.trim(),invoice_ids:JSON.parse(form.dataset.ids),project_id:data.project_id || null,quota_id:data.quota_id || null,month:data.month})});
      selected.clear();matchData=null;modal.close();await refresh({render:false});location.hash='batches';if(activePage==='batches')renderPage();toast('报销草稿已创建。');
    }else if(form.dataset.form==='project'){
      await api('/api/projects',{method:'POST',body:JSON.stringify({name:data.name.trim(),note:data.note.trim()})});modal.close();await refresh();toast('项目已创建。');
    }else if(form.dataset.form==='quota'){
      await api('/api/quotas',{method:'POST',body:JSON.stringify({month:data.month,category:data.category,amount_cents:cents(data.amount)})});modal.close();await refresh();toast('额度已保存。');
    }
  }catch(error){errorBox.textContent=error.message;if(form.dataset.form==='invoice')errorBox.scrollIntoView({block:'nearest'});if(form.dataset.form==='match' && $('#match-results'))$('#match-results').innerHTML=renderMatchResults();}
  finally{button.disabled=false;button.textContent=originalText;modalBusy=false;}
});
modal.addEventListener('cancel',event=>{if(modalBusy)event.preventDefault();});
modal.addEventListener('click',event=>{if(event.target===modal){const rect=modal.getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom)closeModal();}});
document.addEventListener('dragover',event=>{const zone=event.target.closest('#drop-zone');if(zone){event.preventDefault();zone.classList.add('dragover');}});
document.addEventListener('dragleave',event=>{const zone=event.target.closest('#drop-zone');if(zone&&!zone.contains(event.relatedTarget))zone.classList.remove('dragover');});
document.addEventListener('drop',event=>{const zone=event.target.closest('#drop-zone');if(zone){event.preventDefault();zone.classList.remove('dragover');importFiles([...event.dataTransfer.files]);}});
window.addEventListener('hashchange',navigate);
$('#today').textContent=new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'long',day:'numeric',weekday:'short'}).format(new Date());

function currentGroup() { return (state.groups || []).find(g=>g.id===activeGroupId); }
function canManageQuota() { return !activeGroupId || ['owner','admin'].includes(currentGroup()?.role); }
function roleName(role) { return {owner:'群主',admin:'管理员',member:'成员'}[role] || role; }
function updateWorkspaceChrome() {
  document.body.classList.remove('auth-mode');
  const select=$('#space-select');
  select.innerHTML=`<option value="">个人空间</option>${(state.groups || []).map(g=>`<option value="${esc(g.id)}" ${activeGroupId===g.id?'selected':''}>${esc(g.name)}</option>`).join('')}`;
  select.value=activeGroupId;
  $('#workspace-name').textContent=currentGroup()?.name || '个人空间';
  pageNames.invoices=activeGroupId?'群组票库':'我的票库';
  $('#invoice-nav-label').textContent=pageNames.invoices;
  $('#account-name').textContent=currentUser?.name || '我的账号';
  $('#account-email').textContent=currentUser?.email || '';
  $('#account-avatar').textContent=(currentUser?.name || '票').slice(0,1);
}
function renderAuth() {
  document.body.classList.remove('match-page');
  document.body.classList.add('auth-mode');
  document.title='登录 · 票栈';
  const registering=authMode==='register' && registrationOpen;
  $('#main').innerHTML=`<div class="auth-layout"><div class="auth-intro"><a class="brand" href="#"><span class="brand-symbol" aria-hidden="true">票</span><span>票栈<small>个人与团队发票管理</small></span></a><div><p class="eyebrow">A PLACE FOR EVERY INVOICE</p><h1>收好发票，<br>从容报销。</h1><p>管理自己的发票，也和团队一起整理。<br>每一张票的归属、分享和报销状态，都清清楚楚。</p></div><small>个人票库 · 群组共享 · 邮箱收票</small></div><section class="auth-card"><h2>${registering?'创建你的票栈':'欢迎回来'}</h2><p class="auth-description">${registering?'创建账号，开始整理你的第一张发票。':'登录后，继续管理你的发票与报销。'}</p><form data-form="${registering?'register':'login'}" class="form-stack">${registering?'<label class="field"><span>用户名</span><input name="name" autocomplete="nickname" maxlength="80" required placeholder="怎么称呼你"><small>支持中文，仅用于显示。</small></label>':''}<label class="field"><span>邮箱</span><input name="email" type="email" autocomplete="username" maxlength="254" required placeholder="you@example.com"></label><label class="field"><span>密码</span><input name="password" type="password" autocomplete="${registering?'new-password':'current-password'}" ${registering?'minlength="10" maxlength="128"':''} required placeholder="${registering?'至少 10 个字符':'请输入密码'}"></label><div class="inline-error" data-error></div><button class="btn primary" type="submit">${registering?'创建账号':'登录'}</button></form>${registrationOpen?`<p class="auth-switch">${registering?'已有账号？':'第一次使用？'} <button class="link-button" data-action="auth-mode" data-mode="${registering?'login':'register'}">${registering?'立即登录':'创建账号'}</button></p>`:''}</section></div>`;
}
async function bootstrap() {
  try {
    const session=await api('/api/auth/status');
    if(currentUser?.id !== session.user?.id)resetReceiptAddresses();
    currentUser=session.user;
    registrationOpen=!!session.registration_open;
    if (!currentUser) {renderAuth();return;}
    try {await refresh({render:false});} catch(error) {
      if(!activeGroupId)throw error;
      activeGroupId='';localStorage.removeItem('invoice.activeGroup');await refresh({render:false});
    }
    navigate();
  } catch(error) {
    $('#main').innerHTML=`${heading('YOUR WORKSPACE','暂时无法打开票栈','请确认服务仍在运行，或稍后重试。')}<div class="panel">${empty('连接失败',esc(error.message),'<button class="btn primary" id="reload-app">重新连接</button>')}</div>`;
    $('#reload-app').addEventListener('click',()=>location.reload());
  }
}
async function switchGroup(groupId) {
  if(scopeBusy)return;
  scopeBusy=true;
  const old=activeGroupId;
  resetReceiptAddresses();
  activeGroupId=groupId;
  $('#space-select').disabled=true;
  try {
    await refresh({render:false});
    localStorage.setItem('invoice.activeGroup',activeGroupId);
    selected.clear();matchData=null;mailboxData=null;resetReceiptAddresses();
    filters={q:'',category:'',status:'',project_id:'',date_from:'',date_to:'',min:'',max:''};
    matchValues={target:'',direction:'above',category:'',project_id:'',date_from:'',date_to:'',buyer:'',quota_id:''};
    renderPage();toast(`已切换到${currentGroup()?.name || '个人空间'}。`);
  } catch(error) {activeGroupId=old;$('#space-select').value=old;toast(error.message);if(activePage==='mailboxes')loadReceiptAddresses();} finally {$('#space-select').disabled=false;scopeBusy=false;}
}
function renderGroups() {
  const groups=state.groups || [];
  return heading('SHARE WITH YOUR TEAM','群组管理','发票默认仅自己可见，按需分享给一个或多个群组。','<button class="btn" data-action="join-group">加入群组</button><button class="btn primary" data-action="new-group">＋ 创建群组</button>')+`<div class="hint group-hint">分享后，群组成员可以查看原件并使用发票报销。票面信息由上传者维护，同一张发票在所有群组共用报销状态，避免重复使用。</div>${groups.length?`<div class="batch-grid">${groups.map(g=>`<article class="batch-card"><div class="card-top"><div><h3>${esc(g.name)}</h3><p>${g.member_count || 1} 位成员</p></div><span class="badge ${g.role==='owner'?'available':''}">${esc(roleName(g.role))}</span></div><p class="batch-detail group-description">${esc(g.description || (g.id===activeGroupId?'当前所在的工作空间':'选择分享至本组的发票，组内协同报销。'))}</p><div class="batch-actions"><button class="btn primary small" data-action="open-group" data-id="${esc(g.id)}">进入票库</button><button class="btn small" data-action="group-settings" data-id="${esc(g.id)}">团队设置</button><button class="btn small" data-action="group-members" data-id="${esc(g.id)}">查看成员</button>${['owner','admin'].includes(g.role)?`<button class="btn small" data-action="invite-group" data-id="${esc(g.id)}">邀请成员</button>`:''}</div></article>`).join('')}</div>`:`<div class="panel">${empty('和团队一起整理发票','创建群组，或输入邀请码加入已有群组。你的个人发票不会自动对群组可见。','<button class="btn primary" data-action="new-group">创建第一个群组</button><button class="btn ghost" data-action="join-group">使用邀请码</button>','♧')}</div>`}`;
}
function groupSettings(groupId) {
  const group=(state.groups || []).find(g=>g.id===groupId);
  if(!group)return;
  const managing=['owner','admin'].includes(group.role);
  const disabled=managing?'':'disabled';
  const content=`<div class="form-stack"><label class="field"><span>团队名称</span><input name="name" value="${esc(group.name)}" maxlength="100" required ${disabled}></label><label class="field"><span>团队说明</span><textarea name="description" maxlength="1000" placeholder="填写团队用途或协作约定" ${disabled}>${esc(group.description || '')}</textarea></label><div class="group-settings-summary"><span>${group.member_count || 1} 位成员 · ${esc(roleName(group.role))}</span><button type="button" class="btn small" data-action="group-members" data-id="${esc(groupId)}">${managing?'管理成员':'查看成员'}</button></div><p class="hint">发票始终归上传者所有。退出或移除成员会停止其在本团队的分享，取消涉及其发票或由其创建的未完成报销单；原件保留，已报销状态不变。</p><div class="group-lifecycle">${group.role==='owner'?`<div><div><h3>转让团队</h3><p>选择一位成员接任群主，你将成为管理员，之后可退出。</p></div><button type="button" class="btn small" data-action="transfer-group" data-id="${esc(groupId)}">转让群主</button></div><div><div><h3>解散团队</h3><p>撤销所有分享，取消未完成报销；历史报销单由创建人保留。团队收票配置转回创建人并暂停。</p></div><button type="button" class="btn danger small" data-action="dissolve-group" data-id="${esc(groupId)}">解散团队</button></div>`:`<div><div><h3>退出团队</h3><p>个人发票保留，团队收票配置转回你的个人空间并暂停。</p></div><button type="button" class="btn danger small" data-action="leave-group" data-id="${esc(groupId)}" data-name="${esc(group.name)}">退出团队</button></div>`}</div></div>`;
  openModal('团队设置','管理团队资料、成员与协作关系。',content,`<button type="button" class="btn" data-action="close-modal">关闭</button>${managing?'<button type="submit" class="btn primary">保存设置</button>':''}`,managing?'group-settings':'',`data-id="${esc(groupId)}"`);
}
async function transferGroupModal(groupId) {
  const group=(state.groups || []).find(g=>g.id===groupId);
  if(!group || group.role!=='owner')return;
  const members=await api(`/api/groups/${encodeURIComponent(groupId)}/members`);
  const candidates=members.filter(m=>m.id!==currentUser.id);
  openModal('转让群主',esc(group.name),candidates.length?`<div class="form-stack"><label class="field"><span>新群主</span><select name="member_id" required><option value="">请选择团队成员</option>${candidates.map(m=>`<option value="${esc(m.id)}">${esc(m.name)} · ${esc(m.email)}</option>`).join('')}</select></label><p class="hint warn">新群主将拥有管理员设置、移除成员和解散团队等权限。你将成为管理员，发票归属保持不变。</p></div>`:'<p class="hint">团队中还没有其他成员，请先邀请一位成员加入。</p>',`<button type="button" class="btn" data-action="group-settings" data-id="${esc(groupId)}">返回设置</button>${candidates.length?'<button type="submit" class="btn primary">确认转让</button>':''}`,candidates.length?'transfer-group':'',`data-id="${esc(groupId)}"`);
}
function dissolveGroupModal(groupId) {
  const group=(state.groups || []).find(g=>g.id===groupId);
  if(!group || group.role!=='owner')return;
  openModal('解散团队',esc(group.name),`<div class="form-stack"><p class="hint warn">解散后无法恢复团队。所有发票停止在本团队共享，原件归原上传者保留；未完成报销单将取消，已报销状态不变。历史报销单由创建人保留，团队项目和额度删除。团队收票配置转回创建人的个人空间并暂停。</p><label class="field"><span>输入团队名称“${esc(group.name)}”确认解散</span><input name="confirmation" required autocomplete="off" maxlength="100"></label></div>`,`<button type="button" class="btn" data-action="group-settings" data-id="${esc(groupId)}">返回设置</button><button type="submit" class="btn danger">确认解散</button>`,'dissolve-group',`data-id="${esc(groupId)}" data-name="${esc(group.name)}"`);
}
function resetDepartedGroup(groupId) {
  selected.clear();matchData=null;mailboxData=null;resetReceiptAddresses();
  if(activeGroupId!==groupId)return;
  activeGroupId='';localStorage.removeItem('invoice.activeGroup');
  filters={q:'',category:'',status:'',project_id:'',date_from:'',date_to:'',min:'',max:''};
  matchValues={target:'',direction:'above',category:'',project_id:'',date_from:'',date_to:'',buyer:'',quota_id:''};
}
async function showMembers(groupId) {
  const group=(state.groups || []).find(g=>g.id===groupId);
  if(!group)return;
  const response=await api(`/api/groups/${encodeURIComponent(groupId)}/members`);
  const members=Array.isArray(response)?response:(response.members || []);
  const managing=['owner','admin'].includes(group.role);
  openModal(esc(group.name)+' · 成员',`${members.length} 位成员`, `<div class="members-list">${members.map(member=>`<div class="member-row"><div class="member-avatar">${esc((member.name || member.email).slice(0,1))}</div><div class="member-info"><strong>${esc(member.name || member.email)}${member.id===currentUser.id?' <span class="text-muted">（我）</span>':''}</strong><small>${esc(member.email)}</small></div><div class="member-actions"><span class="badge">${esc(roleName(member.role))}</span>${group.role==='owner' && member.role!=='owner' && member.id!==currentUser.id?`<button type="button" class="link-button" data-action="member-role" data-group="${esc(groupId)}" data-id="${esc(member.id)}" data-role="${member.role==='admin'?'member':'admin'}">${member.role==='admin'?'取消管理':'设为管理'}</button>`:''}${managing && member.role!=='owner' && member.id!==currentUser.id && (group.role==='owner'||member.role==='member')?`<button class="link-button danger-text" data-action="remove-member" data-group="${esc(groupId)}" data-id="${esc(member.id)}" data-name="${esc(member.name || member.email)}">移出</button>`:''}</div></div>`).join('')}</div>`,`${group.role!=='owner'?`<button class="btn danger left" data-action="leave-group" data-id="${esc(groupId)}" data-name="${esc(group.name)}">退出群组</button>`:''}${managing?`<button class="btn primary" data-action="invite-group" data-id="${esc(groupId)}">邀请成员</button>`:''}<button class="btn" data-action="close-modal">关闭</button>`);
}
async function showShares(invoiceId) {
  const response=await api(`/api/invoices/${encodeURIComponent(invoiceId)}/shares`);
  const ids=response.group_ids || [];
  const groups=state.groups || [];
  const content=groups.length?`<div class="form-stack"><p class="hint">勾选可查看这张发票的群组。组内成员可下载原件并创建报销单；取消分享后，已创建报销单的历史记录仍会保留。</p><div class="share-list">${groups.map(group=>`<label class="share-row"><input type="checkbox" name="group_ids" value="${esc(group.id)}" ${ids.includes(group.id)?'checked':''}><div><strong>${esc(group.name)}</strong><small>${group.member_count || 1} 位成员 · ${esc(roleName(group.role))}</small></div></label>`).join('')}</div></div>`:empty('你还没有加入群组','创建或加入群组后，就可以将指定发票分享给团队。','<button type="button" class="btn" data-action="go-groups">前往群组管理</button>','♧');
  openModal('发票分享','只向你选中的群组开放这张发票。',content,`<button type="button" class="btn" data-action="close-modal">取消</button>${groups.length?'<button class="btn primary" type="submit">保存分享设置</button>':''}`,'shares',`data-id="${esc(invoiceId)}"`);
}
function renderMailboxes() {
  const manageable=canManageQuota();
  const body=mailboxData===null?'<div class="panel loading-state"><span class="spinner"></span><p>正在读取邮箱配置…</p></div>':mailboxData.length?`<div class="batch-grid">${mailboxData.map(m=>`<article class="batch-card mailbox-card"><div class="card-top"><div><h3>${esc(m.name || m.address)}</h3><p>${esc(m.address)}</p></div><span class="badge ${m.enabled?'available':''}">${m.enabled?'自动收票':'已暂停'}</span></div><div class="mailbox-details"><div><span>收件文件夹</span><strong>${esc(m.folder || 'INBOX')}</strong></div><div><span>最近同步</span><strong>${m.last_sync?esc(formatTimestamp(m.last_sync)):'尚未同步'}</strong></div><div><span>票库归属</span><strong>${m.group_id?esc((state.groups || []).find(g=>g.id===m.group_id)?.name || '群组'):'个人空间'}</strong></div></div>${m.last_error?`<p class="inline-error mailbox-error">${esc(m.last_error)}</p>`:''}<div class="batch-actions">${manageable?`<button class="btn primary small" data-action="sync-mailbox" data-id="${esc(m.id)}">立即收票</button><button class="btn small" data-action="edit-mailbox" data-id="${esc(m.id)}">设置</button><button class="btn small" data-action="mailbox-results" data-id="${esc(m.id)}">收票记录</button>${m.last_error?`<button class="btn small" data-action="retry-mailbox" data-id="${esc(m.id)}">重试失败邮件</button>`:''}`:'<span class="text-muted">邮箱由群组管理员管理。</span>'}</div></article>`).join('')}</div>`:`<div class="panel">${empty('让新发票自己进票栈',manageable?'绑定收票邮箱，自动提取新邮件中的发票附件。首次绑定后只处理新到邮件。':'群组管理员可以配置一个专用邮箱，将新收到的发票分享至本组。',manageable?'<button class="btn primary" data-action="new-mailbox">绑定收票邮箱</button>':'','✉')}</div>`;
  return heading('INVOICES, DELIVERED','邮箱收票','把收到的发票，自动归入当前票库。')+`<section id="receipt-addresses-section" class="receipt-addresses-section" aria-label="域名收票地址">${renderReceiptAddresses()}</section><section class="mailbox-method" aria-label="已有邮箱收票"><div class="mailbox-method-heading"><div><h2>已有邮箱收票</h2><p>绑定自己的收件箱，通过 IMAP 自动提取发票附件。</p></div>${manageable?'<button class="btn" data-action="new-mailbox">＋ 绑定邮箱</button>':''}</div><div class="hint group-hint">每 60 秒检查新邮件，仅提取发票附件，不会删除邮件。识别结果统一进入待核对状态。群组邮箱收到的发票会自动分享至对应群组。</div><div id="imap-mailboxes">${body}</div></section>`;
}
function formatTimestamp(value) {
  const parsed=new Date(value);
  return Number.isNaN(parsed.getTime())?value:new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(parsed);
}
async function loadMailboxes() {
  if(mailboxLoading)return;
  mailboxLoading=true;
  const scope=activeGroupId;
  try {
    const data=await api('/api/mailboxes');
    if(scope!==activeGroupId)return;
    mailboxData=(data.mailboxes || []).filter(m=>(m.group_id || '')===activeGroupId);
    if(activePage==='mailboxes')$('#main').innerHTML=renderMailboxes();
  } catch(error) {
    if(currentUser && scope===activeGroupId && activePage==='mailboxes' && $('#imap-mailboxes'))$('#imap-mailboxes').innerHTML=`<div class="panel">${empty('暂时无法读取邮箱配置',esc(error.message),'<button class="btn" data-action="reload-mailboxes">重试</button>','✉')}</div>`;
  } finally {mailboxLoading=false;if(currentUser && scope!==activeGroupId && activePage==='mailboxes')loadMailboxes();}
}
function mailboxModal(id) {
  const m=(mailboxData || []).find(item=>item.id===id) || {name:'',address:'',host:'',port:993,username:'',folder:'INBOX',group_id:activeGroupId,enabled:true};
  const destinations=(state.groups || []).filter(g=>['owner','admin'].includes(g.role));
  openModal(id?'邮箱设置':'绑定收票邮箱','使用邮箱服务商提供的 IMAP 地址和邮箱授权码。',`<div class="form-grid"><label class="field"><span>邮箱名称</span><input name="name" value="${esc(m.name)}" placeholder="例如：工作收票邮箱" maxlength="100" required></label><label class="field"><span>邮箱地址</span><input name="address" value="${esc(m.address)}" type="email" placeholder="invoices@example.com" autocomplete="off" required></label><label class="field"><span>IMAP 服务器</span><input name="host" value="${esc(m.host)}" placeholder="imap.example.com" autocomplete="off" required><small>使用加密连接，端口 993。</small></label><label class="field"><span>登录用户名</span><input name="username" value="${esc(m.username)}" placeholder="通常与邮箱地址相同" autocomplete="off" required></label><label class="field wide"><span>邮箱授权码 / 应用专用密码</span><input name="password" type="password" autocomplete="new-password" placeholder="${id?'留空保留已保存的授权码':'从邮箱设置中获取授权码'}" ${id?'':'required'}><small>${id && m.has_password?'授权码已保存，不会回填。':'请先在邮箱设置中启用 IMAP，并生成专用授权码。'}</small></label><label class="field"><span>收件文件夹</span><input name="folder" value="${esc(m.folder || 'INBOX')}" placeholder="INBOX" required></label><label class="field"><span>发票保存至</span><select name="group_id" ${id?'disabled':''}><option value="">个人空间</option>${destinations.map(g=>`<option value="${esc(g.id)}" ${g.id===m.group_id?'selected':''}>${esc(g.name)}</option>`).join('')}</select></label><label class="check-label wide"><input type="checkbox" name="enabled" ${m.enabled?'checked':''}><span>启用自动收票，每 60 秒检查新邮件。</span></label><p class="hint wide">只处理绑定后新到的邮件，不回读历史邮件。${m.group_id?'发票将分享给所选群组的成员。':'邮件中的发票默认保存到你的个人票库。'}</p></div>`,`${id?`<button type="button" class="btn danger left" data-action="delete-mailbox" data-id="${esc(id)}">移除邮箱</button>`:''}<button class="btn" type="button" data-action="close-modal">取消</button><button class="btn primary" type="submit">${id?'保存设置':'绑定邮箱'}</button>`,'mailbox',`data-id="${esc(id || '')}" data-group="${esc(m.group_id || '')}"`);
}
async function submitOnlineForm(form) {
  const button=$('button[type=submit]',form);
  if(button.disabled || modalBusy)return;
  const original=button.textContent;
  const errorBox=$('[data-error]',form);
  errorBox.textContent='';
  const fields=new FormData(form),data=Object.fromEntries(fields);
  const kind=form.dataset.form;
  button.disabled=true;button.textContent='正在处理…';
  if(modal.contains(form))modalBusy=true;
  try {
    if(kind==='login'||kind==='register') {
      await api(`/api/auth/${kind}`,{method:'POST',body:JSON.stringify({email:data.email.trim(),password:data.password,...(kind==='register'?{name:data.name.trim()}:{})})});
      await bootstrap();
    }else if(kind==='profile'||kind==='password'){
      if(kind==='password' && data.new_password!==data.confirm_password)throw new Error('两次输入的新密码不一致。');
      const payload=kind==='profile'?{name:data.name.trim(),email:data.email.trim(),current_password:data.current_password}:{current_password:data.current_password,new_password:data.new_password};
      const result=await api(`/api/auth/${kind}`,{method:kind==='profile'?'PATCH':'POST',body:JSON.stringify(payload)});
      currentUser=result.user;state.user=result.user;
      updateWorkspaceChrome();
      $$('input[type="password"]',$('#main')).forEach(input=>{input.value='';});
      if(kind==='profile'){
        form.elements.name.value=currentUser.name;
        form.elements.email.value=currentUser.email;
      }
      toast(kind==='profile'?'个人资料已保存。':'密码已修改，其他设备需要重新登录。');
    }else if(kind==='group'){
      await api('/api/groups',{method:'POST',body:JSON.stringify({name:data.name.trim(),description:data.description.trim()})});modal.close();await refresh();toast('群组已创建。');
    }else if(kind==='group-settings'){
      await api(`/api/groups/${encodeURIComponent(form.dataset.id)}`,{method:'PATCH',body:JSON.stringify({name:data.name.trim(),description:data.description.trim()})});
      modal.close();await refresh();toast('团队设置已保存。');
    }else if(kind==='transfer-group'){
      await api(`/api/groups/${encodeURIComponent(form.dataset.id)}/transfer`,{method:'POST',body:JSON.stringify({member_id:data.member_id})});
      modal.close();await refresh();toast('群主已转让，你现在是管理员。');
    }else if(kind==='dissolve-group'){
      if(data.confirmation!==form.dataset.name)throw new Error('输入的团队名称不一致。');
      await api(`/api/groups/${encodeURIComponent(form.dataset.id)}`,{method:'DELETE'});
      resetDepartedGroup(form.dataset.id);modal.close();await refresh();toast('团队已解散，发票仍归原上传者所有。');
    }else if(kind==='join-group'){
      await api('/api/groups/join',{method:'POST',body:JSON.stringify({token:data.token.trim()})});modal.close();await refresh();toast('已加入群组。');
    }else if(kind==='shares'){
      await api(`/api/invoices/${encodeURIComponent(form.dataset.id)}/shares`,{method:'PUT',body:JSON.stringify({group_ids:fields.getAll('group_ids')})});modal.close();await refresh();matchData=null;toast('发票分享设置已保存。');
    }else if(kind==='receipt-address'){
      const payload={prefix:data.prefix.trim(),name:data.name.trim()};
      if(activeGroupId)payload.group_id=activeGroupId;
      await api('/api/receipt-addresses',{method:'POST',body:JSON.stringify(payload)});
      modal.close();resetReceiptAddresses();await loadReceiptAddresses();toast('收票地址已创建，新发票将自动归入待核对。');
    }else if(kind==='mailbox'){
      const id=form.dataset.id;
      const payload={name:data.name.trim(),address:data.address.trim(),host:data.host.trim(),port:993,username:data.username.trim(),folder:data.folder.trim(),group_id:id?(form.dataset.group || null):(data.group_id || null),enabled:data.enabled==='on'};
      if(data.password)payload.password=data.password;
      await api(id?`/api/mailboxes/${encodeURIComponent(id)}`:'/api/mailboxes',{method:id?'PATCH':'POST',body:JSON.stringify(payload)});
      modal.close();mailboxData=null;await loadMailboxes();toast(id?'邮箱设置已保存。':'邮箱已绑定，将从新收到的邮件开始收票。');
    }
  }catch(error){errorBox.textContent=error.message;}
  finally{button.disabled=false;button.textContent=original;modalBusy=false;}
}
document.addEventListener('change',event=>{if(event.target.id==='space-select')switchGroup(event.target.value);});
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-action]');
  if(!button)return;
  const action=button.dataset.action,id=button.dataset.id;
  if(action==='auth-mode'){authMode=button.dataset.mode;renderAuth();return;}
  if(action==='logout'){
    await runMutation(button,async()=>{await api('/api/auth/logout',{method:'POST'});currentUser=null;activeGroupId='';localStorage.removeItem('invoice.activeGroup');state={invoices:[],projects:[],quotas:[],batches:[],summary:{},capabilities:{},groups:[]};selected.clear();matchData=null;mailboxData=null;resetReceiptAddresses();modal.close();renderAuth();});return;
  }
  if(action==='new-group'){
    openModal('创建群组','邀请团队成员，共同整理指定的发票。','<div class="form-stack"><label class="field"><span>群组名称</span><input name="name" placeholder="例如：行政报销小组" maxlength="100" required></label><label class="field"><span>团队说明</span><textarea name="description" maxlength="1000" placeholder="填写团队用途或协作约定（选填）"></textarea></label></div>','<button type="button" class="btn" data-action="close-modal">取消</button><button type="submit" class="btn primary">创建群组</button>','group');return;
  }
  if(action==='join-group'){
    openModal('加入群组','向群主或管理员获取邀请码。','<label class="field"><span>邀请码</span><input name="token" placeholder="粘贴完整邀请码" autocomplete="off" required></label>','<button type="button" class="btn" data-action="close-modal">取消</button><button type="submit" class="btn primary">加入群组</button>','join-group');return;
  }
  if(action==='open-group'){await switchGroup(id);location.hash='invoices';return;}
  if(action==='personal-space'){await switchGroup('');return;}
  if(action==='go-groups'){modal.close();location.hash='groups';return;}
  if(action==='group-settings'){groupSettings(id);return;}
  if(action==='transfer-group'){await runMutation(button,()=>transferGroupModal(id));return;}
  if(action==='dissolve-group'){dissolveGroupModal(id);return;}
  if(action==='group-members'){await runMutation(button,()=>showMembers(id));return;}
  if(action==='invoice-shares'){await runMutation(button,()=>showShares(id));return;}
  if(action==='invite-group'){
    await runMutation(button,async()=>{
      const invitation=await api(`/api/groups/${encodeURIComponent(id)}/invites`,{method:'POST'});
      const group=(state.groups || []).find(g=>g.id===id);
      openModal('邀请加入 '+esc(group?.name || '群组'),'将邀请码发给你希望邀请的成员。',`<div class="form-stack"><label class="field"><span>邀请码</span><input id="invite-token" class="invite-token" value="${esc(invitation.token)}" readonly></label><p class="hint">${invitation.expires_at?'有效期至 '+esc(formatTimestamp(invitation.expires_at))+'。':''}持有邀请码的人可以加入群组，查看并使用分享至本组的发票。</p></div>`,'<button class="btn" data-action="close-modal">完成</button><button class="btn primary" data-action="copy-invite">复制邀请码</button>');
    });return;
  }
  if(action==='copy-invite'){
    try{await navigator.clipboard.writeText($('#invite-token').value);toast('邀请码已复制。');}catch(_){$('#invite-token').select();toast('请复制选中的邀请码。');}return;
  }
  if(action==='member-role'){
    await runMutation(button,async()=>{await api(`/api/groups/${encodeURIComponent(button.dataset.group)}/members/${encodeURIComponent(id)}`,{method:'PATCH',body:JSON.stringify({role:button.dataset.role})});await refresh();await showMembers(button.dataset.group);toast('成员角色已更新。');});return;
  }
  if(action==='leave-group'){
    if(!confirm(`退出“${button.dataset.name}”？你的发票将停止在本团队共享，涉及你的发票或由你创建的未完成报销单会取消。原件和已报销状态保留，团队收票配置转回你的个人空间并暂停。`))return;
    await runMutation(button,async()=>{
      await api(`/api/groups/${encodeURIComponent(id)}/members/${encodeURIComponent(currentUser.id)}`,{method:'DELETE'});
      resetDepartedGroup(id);
      modal.close();await refresh();toast('已退出群组，个人发票保留。');
    });return;
  }
  if(action==='remove-member'){
    if(!confirm(`将“${button.dataset.name}”移出团队？对方的发票将停止在本团队共享，涉及其发票或由其创建的未完成报销单会取消。原件及已报销状态保留，团队收票配置转回对方的个人空间并暂停。`))return;
    await runMutation(button,async()=>{await api(`/api/groups/${encodeURIComponent(button.dataset.group)}/members/${encodeURIComponent(id)}`,{method:'DELETE'});selected.clear();matchData=null;mailboxData=null;resetReceiptAddresses();await refresh();await showMembers(button.dataset.group);toast('成员已移出群组，发票仍归原上传者所有。');});return;
  }
  if(action==='reload-mailboxes'){mailboxData=null;await loadMailboxes();return;}
  if(action==='mailbox-results'){
    await runMutation(button,async()=>{
      const data=await api(`/api/mailboxes/${encodeURIComponent(id)}/results`);
      const names={imported:'已导入',duplicate:'重复跳过',ignored:'已忽略',failed:'失败'};
      const rows=data.results || [];
      openModal('收票记录','最近处理的邮件附件与结果。',rows.length?`<div class="mail-results">${rows.map(row=>`<div class="mail-result"><div><strong>${esc(row.filename || '邮件')}</strong><span class="badge ${row.status==='failed'?'rejected':row.status==='imported'?'available':''}">${esc(names[row.status] || row.status)}</span></div><small>${esc(formatTimestamp(row.created_at))}${row.error?' · '+esc(row.error):''}</small></div>`).join('')}</div>`:empty('暂无收票记录','绑定后收到新邮件时，处理结果会显示在这里。','','✉'),'<button class="btn primary" data-action="close-modal">完成</button>');
    });return;
  }
  if(action==='new-mailbox'){mailboxModal();return;}
  if(action==='edit-mailbox'){mailboxModal(id);return;}
  if(action==='delete-mailbox'){
    if(!confirm('移除这个收票邮箱？已导入的发票会保留，之后的新邮件将不再自动导入。'))return;
    await runMutation(button,async()=>{await api(`/api/mailboxes/${encodeURIComponent(id)}`,{method:'DELETE'});modal.close();mailboxData=null;await loadMailboxes();toast('邮箱已移除，已导入发票保留。');});return;
  }
  if(action==='sync-mailbox'||action==='retry-mailbox'){
    const original=button.textContent;button.textContent=action==='retry-mailbox'?'正在重试…':'正在收票…';
    await runMutation(button,async()=>{
      const result=await api(`/api/mailboxes/${encodeURIComponent(id)}/${action==='retry-mailbox'?'retry':'sync'}`,{method:'POST'});
      const count=value=>Array.isArray(value)?value.length:Number(value || 0);
      await refresh({render:false});mailboxData=null;await loadMailboxes();
      if(result.notice){toast(result.notice);return;}
      toast(`收票完成：导入 ${count(result.imported)} 张，重复 ${count(result.duplicates)} 个，失败 ${count(result.error_count ?? result.errors)} 个。`);
    });button.textContent=original;
  }
});



function resetReceiptAddresses() {
  receiptGeneration+=1;
  receiptData=null;
  receiptError='';
  receiptLoading=false;
}
function renderReceiptAddresses() {
  const canCreate=!!receiptData?.configured && canManageQuota();
  const header=`<div class="panel-header"><div><h2>域名收票地址</h2><p>使用专属地址收发票，无需绑定个人邮箱。</p></div><div class="actions"><button class="btn small" data-action="refresh-receipt-addresses" ${receiptLoading?'disabled':''}>${receiptLoading?'刷新中…':'刷新'}</button>${canCreate?'<button class="btn primary small" data-action="new-receipt-address">＋ 创建地址</button>':''}</div></div>`;
  if(receiptError)return `<div class="panel">${header}<div class="panel-body"><p class="inline-error">${esc(receiptError)}</p></div></div>`;
  if(!receiptData)return `<div class="panel">${header}<div class="receipt-loading"><span class="spinner" aria-hidden="true"></span><p>正在读取收票地址…</p></div></div>`;
  if(!receiptData.configured)return `<div class="panel">${header}<div class="receipt-unconfigured"><h3>尚未启用域名收票</h3><p>启用后，可以创建专属收票地址。你也可以使用下方的已有邮箱收票。</p></div></div>`;
  const addresses=receiptData.addresses || [];
  const cards=addresses.map(address=>`<article class="receipt-address-card"><div class="receipt-address-head"><div><h3>${esc(address.name || address.prefix)}</h3><p>${address.last_received?'最近收票 '+esc(formatTimestamp(address.last_received)):'尚未收到邮件'}</p></div><span class="badge ${address.enabled?'available':''}">${address.enabled?'收票中':'已暂停'}</span></div><div class="receipt-address-copy"><input id="receipt-address-${esc(address.id)}" aria-label="${esc(address.name || address.prefix)}的收票地址" value="${esc(address.address)}" readonly spellcheck="false"><button class="btn small" data-action="copy-receipt-address" data-id="${esc(address.id)}">复制</button></div><div class="receipt-address-footer"><span>${activeGroupId?'归入 '+esc(currentGroup()?.name || '当前群组'):'归入个人票库'}</span><div class="actions"><button class="link-button" data-action="receipt-deliveries" data-id="${esc(address.id)}">收票记录</button>${address.can_manage?`<button class="link-button ${address.enabled?'danger-text':''}" data-action="toggle-receipt-address" data-id="${esc(address.id)}" data-enabled="${address.enabled?'false':'true'}">${address.enabled?'暂停收票':'启用收票'}</button>`:''}</div></div></article>`).join('');
  const content=addresses.length?`<div class="receipt-address-grid">${cards}</div>`:empty('一个地址，接住新发票',canCreate?'创建一个专属地址，开票时填写或将发票邮件转发至这里，新发票将自动识别后进入待核对。':'群组管理员可以创建专属收票地址，新发票将自动归入本组待核对。',canCreate?'<button class="btn primary" data-action="new-receipt-address">创建收票地址</button>':'','✉');
  return `<div class="panel">${header}${content}${addresses.length?'<p class="receipt-section-note">发往启用地址的新发票将自动识别后进入待核对。地址创建后保持不变，可以随时暂停收票。</p>':''}</div>`;
}
function refreshReceiptSection() {
  const section=$('#receipt-addresses-section');
  if(currentUser && activePage==='mailboxes' && section)section.innerHTML=renderReceiptAddresses();
}
async function loadReceiptAddresses() {
  if(receiptLoading || !currentUser)return;
  const generation=receiptGeneration,scope=activeGroupId,userId=currentUser.id;
  receiptLoading=true;receiptError='';refreshReceiptSection();
  const isCurrent=()=>generation===receiptGeneration && scope===activeGroupId && userId===currentUser?.id;
  try {
    const data=await api('/api/receipt-addresses');
    if(!isCurrent())return;
    receiptData={...data,addresses:(data.addresses || []).filter(address=>(address.group_id || '')===scope)};
  } catch(error) {
    if(isCurrent())receiptError=error.message;
  } finally {
    if(isCurrent()){receiptLoading=false;refreshReceiptSection();}
  }
}
function receiptAddressModal() {
  if(!receiptData?.configured || !canManageQuota())return;
  const domain=receiptData.domain;
  const target=currentGroup()?.name || '个人票库';
  openModal('创建收票地址','给个人或团队准备一个专属的发票收件地址。',`<div class="form-stack"><label class="field"><span>地址名称</span><input name="name" placeholder="例如：差旅发票" maxlength="100" required></label><label class="field"><span>自定义前缀</span><div class="receipt-prefix-input"><input name="prefix" placeholder="例如：travel" maxlength="64" autocomplete="off" autocapitalize="none" spellcheck="false" required aria-describedby="receipt-prefix-help"><span>@${esc(domain)}</span></div><small id="receipt-prefix-help">最多 64 位，可用英文字母、数字、点、下划线或短横线。地址创建后无法更名，可暂停收票。</small></label><div class="receipt-destination"><span>发票归入</span><strong>${esc(target)}</strong></div><p class="hint">发送至这个地址的新发票将自动识别后进入待核对。${activeGroupId?'群组成员可以查看并使用归入本组的发票。':'发票默认仅你自己可见。'}</p></div>`,'<button class="btn" type="button" data-action="close-modal">取消</button><button class="btn primary" type="submit">创建地址</button>','receipt-address');
}
async function showReceiptDeliveries(addressId) {
  const address=receiptData?.addresses?.find(item=>item.id===addressId);
  if(!address)return;
  const generation=receiptGeneration;
  const response=await api(`/api/receipt-addresses/${encodeURIComponent(addressId)}/deliveries`);
  if(generation!==receiptGeneration)return;
  const deliveries=response.deliveries || [];
  const deliveryNames={queued:'等待处理',processing:'识别中',completed:'处理完成',failed:'处理失败'};
  const resultNames={imported:'已导入',duplicate:'重复跳过',ignored:'已忽略',failed:'失败'};
  const content=deliveries.length?`<div class="receipt-deliveries">${deliveries.map(delivery=>`<article class="receipt-delivery"><div class="receipt-delivery-head"><div><strong>${esc(formatTimestamp(delivery.received_at))}</strong><span class="badge ${delivery.status==='failed'?'rejected':delivery.status==='completed'?'available':''}">${esc(deliveryNames[delivery.status] || delivery.status)}</span></div>${delivery.status==='failed' && address.can_manage?`<button class="btn small" data-action="retry-receipt-delivery" data-id="${esc(addressId)}" data-delivery="${esc(delivery.id)}">重试失败项</button>`:''}</div>${delivery.results?.length?`<div class="receipt-results">${delivery.results.map(result=>`<div class="mail-result"><div><strong>${esc(result.filename || '邮件附件')}</strong><span class="badge ${result.status==='failed'?'rejected':result.status==='imported'?'available':''}">${esc(resultNames[result.status] || result.status)}</span></div>${result.error?`<small class="${result.status==='failed'?'danger-text':''}">${esc(result.error)}</small>`:''}</div>`).join('')}</div>`:`<p class="receipt-delivery-note">${delivery.status==='queued'?'已收到邮件，等待提取发票。':delivery.status==='processing'?'正在提取和识别发票，稍后可刷新查看结果。':delivery.status==='failed'?'本次处理未完成，可以重试。':'未提取到发票附件。'}</p>`}</article>`).join('')}</div>`:empty('还没有收票记录','将发票邮件发送或转发至这个地址，处理结果会显示在这里。','','✉');
  openModal('域名收票记录',esc(address.address),content,`<button class="btn" data-action="receipt-deliveries" data-id="${esc(addressId)}">刷新记录</button><button class="btn primary" data-action="close-modal">完成</button>`);
}
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-action]');
  if(!button)return;
  const action=button.dataset.action,id=button.dataset.id;
  if(action==='refresh-receipt-addresses'){await loadReceiptAddresses();return;}
  if(action==='new-receipt-address'){receiptAddressModal();return;}
  if(action==='copy-receipt-address'){
    const address=receiptData?.addresses?.find(item=>item.id===id);
    if(!address)return;
    try{await navigator.clipboard.writeText(address.address);toast('收票地址已复制。');}
    catch(_){const input=document.getElementById(`receipt-address-${id}`);if(input){input.focus();input.select();toast('请复制选中的收票地址。');}}
    return;
  }
  if(action==='toggle-receipt-address'){
    const enabled=button.dataset.enabled==='true';
    if(!enabled && !confirm('暂停这个地址收票？暂停后，新发来的邮件不会自动导入；已有发票会保留。'))return;
    await runMutation(button,async()=>{await api(`/api/receipt-addresses/${encodeURIComponent(id)}`,{method:'PATCH',body:JSON.stringify({enabled})});resetReceiptAddresses();await loadReceiptAddresses();toast(enabled?'已启用这个地址收票。':'收票地址已暂停。');});return;
  }
  if(action==='receipt-deliveries'){await runMutation(button,()=>showReceiptDeliveries(id));return;}
  if(action==='retry-receipt-delivery'){
    const original=button.textContent;button.textContent='正在提交…';
    await runMutation(button,async()=>{await api(`/api/receipt-addresses/${encodeURIComponent(id)}/retry`,{method:'POST',body:JSON.stringify({delivery_id:button.dataset.delivery})});await showReceiptDeliveries(id);toast('已加入重试队列，稍后刷新记录查看结果。');});
    button.textContent=original;
  }
});

bootstrap();
