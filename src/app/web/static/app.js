const $ = (id) => document.getElementById(id);
const sessionKey = 'youju.sessions.v1';
const activeKey = 'youju.active.v1';
let sessions = [], active = null, busy = false, ready = false, selectedTurn = null;
const sidebarKey = 'youju.sidebar.v2';
const smallScreen = window.matchMedia('(max-width: 760px)');
const overlayEvidence = window.matchMedia('(max-width: 1150px)');
function formatBusinessTime(value) {
  if (typeof value !== 'string' || !value.trim()) return '未提供';
  if (!/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) return value;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return value;
  const parts = Object.fromEntries(new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
  }).formatToParts(date).map(part => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}
function syncBackdrop() {
  $('shell-backdrop').hidden = !((smallScreen.matches && !document.body.classList.contains('sidebar-collapsed')) || (overlayEvidence.matches && document.body.classList.contains('evidence-open')));
}
function setSidebar(expanded, persist = false) {
  document.body.classList.toggle('sidebar-collapsed', !expanded);
  $('sidebar').setAttribute('aria-hidden', String(!expanded));
  $('toggle-sidebar').setAttribute('aria-expanded', String(expanded));
  $('toggle-sidebar').setAttribute('aria-label', expanded ? '收起侧栏' : '展开侧栏');
  $('toggle-sidebar').title = expanded ? '收起侧栏' : '展开侧栏';
  if (persist && !smallScreen.matches) { try { localStorage.setItem(sidebarKey, expanded ? 'open' : 'closed'); } catch {} }
  syncBackdrop();
}
function setEvidence(open) {
  document.querySelector('.evidence-panel').classList.toggle('open', open);
  document.querySelector('.evidence-panel').setAttribute('aria-hidden', String(!open));
  document.body.classList.toggle('evidence-open', open);
  $('mobile-evidence').setAttribute('aria-expanded', String(open));
  if (open && smallScreen.matches) setSidebar(false);
  syncBackdrop();
}
function openEvidence(answer, sourceId, quote) { showSources(answer, sourceId, quote); setEvidence(true); $('close-evidence').focus(); }
function syncOptions() {
  const target = $('active-options'); target.replaceChildren();
  for (const [id, label, className] of [['evidence-only', '文档仅看证据', ''], ['market-replay', '快照回放 · 非实时', 'replay']]) {
    if (!$(id).checked) continue;
    const chip = element('button', `option-chip ${className}`, label);
    chip.append(element('span', '', '×')); chip.setAttribute('aria-label', `关闭${label}`);
    chip.disabled = busy; chip.onclick = () => { $(id).checked = false; syncOptions(); };
    target.append(chip);
  }
  target.hidden = !target.children.length;
}
function resizeQuestion() { $('question').style.height = 'auto'; $('question').style.height = `${Math.min($('question').scrollHeight, 180)}px`; }
function syncTheme() {
  const dark = window.youjuTheme.current() === 'dark';
  $('theme-toggle').setAttribute('aria-label', dark ? '切换到浅色' : '切换到深色');
  $('theme-toggle').setAttribute('aria-pressed', String(dark)); $('theme-toggle').title = dark ? '切换到浅色' : '切换到深色';
  document.querySelectorAll('.kline-chart').forEach(canvas => { if (canvas.chartBars) drawCandles(canvas, canvas.chartBars); });
}
function initializeShell() {
  function fitSidebar() { let expanded = true; try { expanded = localStorage.getItem(sidebarKey) !== 'closed'; } catch {} setSidebar(!smallScreen.matches && expanded); }
  fitSidebar(); smallScreen.addEventListener('change', fitSidebar); overlayEvidence.addEventListener('change', syncBackdrop);
  $('toggle-sidebar').onclick = () => { if (smallScreen.matches) setEvidence(false); setSidebar(document.body.classList.contains('sidebar-collapsed'), true); };
  $('theme-toggle').onclick = () => window.youjuTheme.toggle(); document.addEventListener('youju:theme', syncTheme); syncTheme();
  for (const id of ['evidence-only', 'market-replay']) $(id).onchange = syncOptions;
  $('question').oninput = resizeQuestion;
  document.addEventListener('click', event => { if (!$('query-options').contains(event.target)) $('query-options').open = false; });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    if ($('query-options').open) { $('query-options').open = false; $('query-options').querySelector('summary').focus(); }
    else if (document.body.classList.contains('evidence-open')) { setEvidence(false); $('mobile-evidence').focus(); }
    else if (smallScreen.matches) setSidebar(false);
  });
  $('shell-backdrop').onclick = () => { setEvidence(false); if (smallScreen.matches) setSidebar(false); };
}
const modeNames = { draft: '生成草稿', structured_answer: '原文事实', evidence_only: '仅原文证据', refused: '无法回答', clarification: '需要补充', unsupported: '暂未支持', generation_error: '生成失败', validation_failed: '检查未通过', market_data: '行情数据', market_error: '行情获取失败' };
function element(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
function saveSessions() { localStorage.setItem(sessionKey, JSON.stringify(sessions)); localStorage.setItem(activeKey, active || ''); }
function alertMessage(text) { $('alert').textContent = text; $('alert').hidden = !text; }
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers } });
  let payload; try { payload = await response.json(); } catch { throw new Error('服务未返回有效响应，请检查后端状态。'); }
  if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : `请求未完成（${response.status}），请稍后重试。`);
  return payload;
}
function setBusy(value) { busy = value; for (const id of ['send', 'new-session', 'reset-session', 'evidence-only', 'market-replay']) $(id).disabled = value || (id === 'send' && !ready); $('question').disabled = value; document.querySelectorAll('.example,.session-button').forEach(n => n.disabled = value); syncOptions(); }
function drawSessions() {
  $('conversation-title').textContent = sessions.find(s => s.id === active)?.title || '新会话';
  $('sessions').replaceChildren();
  for (const session of sessions) {
    const button = element('button', `session-button${session.id === active ? ' active' : ''}`, session.title || '新会话');
    button.disabled = busy; button.onclick = () => selectSession(session.id); $('sessions').append(button);
  }
}
function emptySources() {
  document.body.classList.remove('has-evidence');
  $('mobile-evidence').disabled = true; setEvidence(false);
  $('source-count').textContent = '0'; $('mobile-count').textContent = '0';
  const empty = element('div', 'empty-sources'); empty.append(element('span', '', '⌕'), element('h3', '', '证据会显示在这里'), element('p', '', '提交问题后，查看来源文档、物理页码与对应原文。')); $('sources').replaceChildren(empty);
}
function showSources(answer, selectedId, selectedQuote) {
  selectedTurn = answer;
  const sources = answer.sources || [];
  $('source-count').textContent = sources.length; $('mobile-count').textContent = sources.length;
  if (!sources.length) { emptySources(); return; }
  $('mobile-evidence').disabled = false;
  document.body.classList.add('has-evidence');
  $('sources').replaceChildren();
  for (const source of sources) {
    const card = element('article', `source-card${(selectedId || sources[0].source_id) === source.source_id ? ' selected' : ''}`);
    const top = element('button', 'source-top');
    top.append(element('div', 'source-number', source.source_id), element('div', 'source-title', `${source.stock_name} · ${source.title}`), element('div', 'source-meta', `PDF物理第 ${source.physical_page} 页 · ${source.report_period}报告期`));
    top.onclick = () => showSources(answer, source.source_id, source.source_id === selectedId ? selectedQuote : undefined);
    card.append(top);
    const cited = source.source_id === selectedId && selectedQuote ? { quote: selectedQuote } : (answer.claims || []).flatMap(c => c.citations || []).find(c => c.source_id === source.source_id);
    if (cited) card.append(element('div', 'source-quote', cited.quote));
    const footer = element('div', 'source-footer'); const link = element('a', '', '打开原文PDF ↗');
    link.href = `/documents/${encodeURIComponent(source.document_id)}.pdf#page=${Number(source.physical_page)}`; link.target = '_blank'; link.rel = 'noopener'; footer.append(link); card.append(footer, element('pre', 'source-content', source.content)); $('sources').append(card);
  }
  if (selectedId) $('sources').querySelector('.selected')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
function appendQuestion(question) { const wrapper = element('article', 'message'); wrapper.append(element('div', 'question-bubble', question)); $('messages').append(wrapper); return wrapper; }
function appendAnswer(wrapper, answer) {
  const brand = document.querySelector('.brand-name').textContent;
  const label = element('div', 'answer-label'); label.append(element('span', 'answer-logo', brand.slice(0, 1)), element('span', '', brand), element('span', `mode-badge ${answer.mode}`, answer.answer_method === 'verified_calculation' ? '核准计算' : modeNames[answer.mode] || answer.mode)); wrapper.append(label);
  const body = element('div', 'answer-body');
  if (answer.market) renderMarket(body, answer.market);
  for (const claim of answer.claims || []) {
    body.append(element('p', '', claim.text));
    const citedIds = new Set();
    for (const citation of claim.citations || []) {
      if (citedIds.has(citation.source_id)) continue; citedIds.add(citation.source_id);
      const source = (answer.sources || []).find(s => s.source_id === citation.source_id); if (!source) continue;
      const button = element('button', 'citation', `[${citation.source_id}] PDF第${source.physical_page}页 ↗`);
      button.onclick = () => openEvidence(answer, citation.source_id, citation.quote); body.append(button);
    }
  }
  body.append(element('p', 'answer-note', answer.mode === 'draft' ? '生成草稿，请结合引用原文核对。' : answer.message));
  if (!(answer.claims || []).length && (answer.sources || []).length) {
    const button = element('button', 'citation', `查看 ${answer.sources.length} 条原文证据 →`); button.onclick = () => openEvidence(answer); body.append(button);
  }
  wrapper.append(body); showSources(answer); wrapper.scrollIntoView({ behavior: 'smooth', block: 'end' });
}
async function newSession() {
  if (busy) return;
  setBusy(true);
  try { const response = await api('/api/sessions', { method: 'POST' }); active = response.session_id; sessions.unshift({ id: active, title: '新会话' }); saveSessions(); drawSessions(); restoreWelcome(); alertMessage(''); } catch (error) { alertMessage(error.message); } finally { setBusy(false); $('question').focus(); }
}
let welcome;
function restoreWelcome() { $('messages').replaceChildren(welcome); welcome.hidden = false; document.querySelector('.conversation').classList.add('is-empty'); selectedTurn = null; emptySources(); if (smallScreen.matches) setSidebar(false); resizeQuestion(); $('question').focus(); }
async function selectSession(id) {
  if (busy) return;
  try {
    const data = await api(`/api/sessions/${id}`); active = id; saveSessions(); drawSessions(); restoreWelcome();
    if (data.turns.length) { welcome.hidden = true; document.querySelector('.conversation').classList.remove('is-empty'); for (const turn of data.turns) appendAnswer(appendQuestion(turn.question), turn.answer); }
    if (smallScreen.matches) setSidebar(false);
  } catch (error) { sessions = sessions.filter(s => s.id !== id); active = null; saveSessions(); drawSessions(); alertMessage(error.message); }
}
async function send(forceEvidence = false) {
  const question = $('question').value.trim(); if (!question || busy || !ready) return;
  alertMessage('');
  if (!active) { await newSession(); if (!active) return; }
  const currentId = active; welcome.hidden = true; document.querySelector('.conversation').classList.remove('is-empty'); if (smallScreen.matches) setSidebar(false); $('query-options').open = false; const wrapper = appendQuestion(question); $('question').value = ''; resizeQuestion();
  const loading = element('div', 'loading'); loading.append(element('span', 'spinner'), element('span', '', '正在处理问题并核对数据…')); wrapper.append(loading); setBusy(true); wrapper.scrollIntoView({ behavior: 'smooth', block: 'end' });
  try {
    const response = await api('/api/chat', { method: 'POST', body: JSON.stringify({ session_id: currentId, question, evidence_only: forceEvidence === true || $('evidence-only').checked, market_replay: $('market-replay').checked }) });
    loading.remove(); appendAnswer(wrapper, response); const session = sessions.find(s => s.id === currentId); if (session && session.title === '新会话') session.title = question.slice(0, 18); saveSessions(); drawSessions();
  } catch (error) { loading.remove(); wrapper.append(element('p', 'alert', error.message)); $('question').value = question; } finally { setBusy(false); $('question').focus(); }
}
async function boot() {
  initializeShell();
  welcome = $('welcome');
  try { sessions = JSON.parse(localStorage.getItem(sessionKey) || '[]'); active = localStorage.getItem(activeKey) || null; } catch { sessions = []; }
  $('send').onclick = send; $('new-session').onclick = newSession;
  $('question').onkeydown = event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send(); } };
  $('reset-session').onclick = async () => { if (busy || !active || !confirm('清空当前会话的提问与回答？其他会话不受影响。')) return; try { await api(`/api/sessions/${active}/messages`, { method: 'DELETE' }); const s = sessions.find(s => s.id === active); if (s) s.title = '新会话'; saveSessions(); drawSessions(); restoreWelcome(); } catch (error) { alertMessage(error.message); } };
  $('mobile-evidence').onclick = () => setEvidence(!document.body.classList.contains('evidence-open')); $('close-evidence').onclick = () => { setEvidence(false); $('mobile-evidence').focus(); };
  drawSessions();
  try {
    const data = await api('/api/examples');
    const picks = ['财务概览', '在职员工数量合计', '最新股价'].map(word => data.examples.find(e => e.question.includes(word))).filter(Boolean);
    for (const example of (picks.length ? picks : data.examples).slice(0, 3)) { const button = element('button', 'example'); button.append(element('span', '', example.label), element('small', '', '↗')); button.onclick = () => { $('question').value = example.question; send(example.kind === 'evidence_only'); }; $('examples').append(button); }
    const health = await api('/api/health'); ready = health.status === 'ready'; $('coverage').textContent = `${health.companies}家公司 · ${health.documents}份文档`; $('generation-status').textContent = health.generation_configured ? '生成接口已配置' : '生成未配置 · 证据模式'; $('service-status').lastElementChild.textContent = ready ? '服务已连接' : '服务未就绪';
  } catch (error) { $('service-status').classList.add('offline'); $('service-status').lastElementChild.textContent = '服务未就绪'; $('generation-status').textContent = '请检查后端服务'; alertMessage(error.message); }
  setBusy(false);
  const preset = { financial: '宁德时代2025年财务概览及原文', employee: '中国神华2025年在职员工数量合计是多少人？', market: '贵州茅台最新股价、PE、PB和市值是多少？' }[new URLSearchParams(location.search).get('example')];
  if (typeof preset === 'string') { active = null; saveSessions(); drawSessions(); restoreWelcome(); $('question').value = preset; resizeQuestion(); }
  else if (active) await selectSession(active);
}
boot();

function renderMarket(body, market) {
  if (!market.kind) return;
  body.append(element('p', `market-mode ${market.data_mode}`, market.data_mode === 'replay' ? '快照回放 · 非实时行情' : '行情源获取 · 可能延迟，以业务时间为准'));
  for (const data of market.datasets || []) {
    const q = data.quote;
    body.append(element('h3', '', `${q.name}（${q.code}）`));
    if (market.kind === 'closing_price') {
      const close = data.closing_price;
      body.append(element('p', '', close.message));
      if (close.value) body.append(element('p', '', `${close.value.date} 收盘价：${close.value.price} 元（不复权）。`));
      else if (close.latest_confirmed) body.append(element('p', '', `最近已确认的收盘价：${close.latest_confirmed.date}，${close.latest_confirmed.price} 元（不复权；不是今日收盘价）。`));
      if (close.requested_date) body.append(element('p', 'answer-note', `查询日期：${close.requested_date}（北京时间）`));
    }
    else {
    const grid = element('div', 'market-grid');
    for (const [name, value, unit] of [['价格', q.price, '元'], ['PE（TTM）', q.pe_ttm, '倍'], ['PB', q.pb, '倍'], ['总市值', q.market_cap_yi, '亿元']]) {
      const cell = element('div', 'market-value'); cell.append(element('small', '', name), element('strong', '', value === null ? '源数据未提供' : `${value} ${unit}`)); grid.append(cell);
    }
    body.append(grid);
    }
    const source = element('a', 'market-source', `来源：${q.source} ↗`); source.href = q.source_url; source.target = '_blank'; source.rel = 'noopener'; body.append(source);
    body.append(element('p', 'answer-note', `业务时间：${formatBusinessTime(q.as_of)} · 获取时间：${formatBusinessTime(data.fetched_at)}（北京时间）`));
    if (market.kind === 'kline') {
      const chart = data.chart;
      body.append(element('p', '', `最近${chart.days}个已收盘交易日 · 前复权 · 涨跌幅 ${Number(chart.return_percent).toFixed(2)}%`));
      const canvas = element('canvas', 'kline-chart'); canvas.width = 960; canvas.height = 360; canvas.setAttribute('role', 'img'); canvas.setAttribute('aria-label', `${q.name}最近${chart.days}日前复权日K`); body.append(canvas); drawCandles(canvas, chart.bars);
      body.append(element('p', 'answer-note', `基准收盘：${chart.baseline.date} ${chart.baseline.close}元；末日收盘：${chart.bars.at(-1).date} ${chart.bars.at(-1).close}元。收益按末日收盘÷${chart.days}个交易日前收盘−1计算。`));
      const details = element('details', 'market-details'); details.append(element('summary', '', '查看逐日OHLC数据'));
      const table = element('table', 'market-table');
      const head = element('tr'); for (const text of ['日期', '开', '高', '低', '收']) head.append(element('th', '', text)); table.append(head);
      for (const bar of chart.bars) { const row = element('tr'); for (const key of ['date', 'open', 'high', 'low', 'close']) row.append(element('td', '', bar[key])); table.append(row); }
      const tableWrap = element('div', 'market-table-wrap'); tableWrap.append(table); details.append(tableWrap); body.append(details);
    }
  }
  if (market.can_replay && !(market.datasets || []).length) body.append(element('p', 'answer-note', '在输入框“查询选项”中启用“行情快照回放”，再提交原问题。'));
}

function drawCandles(canvas, bars) {
  const ctx = canvas.getContext('2d'), w = canvas.width, h = canvas.height, pad = 52;
  const low = Math.min(...bars.map(b => Number(b.low))), high = Math.max(...bars.map(b => Number(b.high))), range = high - low || 1;
  const y = price => h - pad - (Number(price) - low) / range * (h - pad * 2);
  canvas.chartBars = bars;
  const colors = getComputedStyle(document.documentElement);
  ctx.fillStyle = colors.getPropertyValue('--chart-bg').trim(); ctx.fillRect(0, 0, w, h); ctx.font = '16px sans-serif';
  for (let i = 0; i < 5; i++) { const value = low + range * i / 4, py = y(value); ctx.strokeStyle = colors.getPropertyValue('--chart-grid').trim(); ctx.beginPath(); ctx.moveTo(pad, py); ctx.lineTo(w - pad, py); ctx.stroke(); ctx.fillStyle = colors.getPropertyValue('--chart-text').trim(); ctx.fillText(value.toFixed(2), 2, py + 5); }
  const step = (w - pad * 2) / bars.length;
  bars.forEach((b, i) => { const x = pad + (i + 0.5) * step; ctx.fillStyle = ctx.strokeStyle = Number(b.close) >= Number(b.open) ? '#bf5b4a' : '#2d7e69'; ctx.beginPath(); ctx.moveTo(x, y(b.high)); ctx.lineTo(x, y(b.low)); ctx.stroke(); ctx.fillRect(x - Math.max(1, step * .65) / 2, Math.min(y(b.open), y(b.close)), Math.max(1, step * .65), Math.max(1, Math.abs(y(b.open) - y(b.close)))); });
  ctx.fillStyle = colors.getPropertyValue('--chart-text').trim(); ctx.fillText(bars[0].date, pad, h - 12); ctx.fillText(bars.at(-1).date, w - pad - 100, h - 12);
}
