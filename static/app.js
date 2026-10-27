'use strict';
const $ = id => document.getElementById(id);
const state = {file: null, sheets: [], items: [], issues: [], result: null, working: false, validationVersion: 0};
const fields = {material: '재질', spec: '규격', length: '길이 (mm)', quantity: '수량'};
const colors = ['#087f6b', '#2f9e8c', '#59b5a2', '#80c9b8', '#397f99', '#63a2b9'];
const fmt = value => Number(value).toLocaleString('ko-KR', {maximumFractionDigits: 3});
const esc = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));

function error(message) {
  $('alert').textContent = message;
  $('alert').hidden = false;
  $('alert').scrollIntoView({behavior: 'smooth', block: 'center'});
}
function clearError() { $('alert').hidden = true; }
function busy(on, title = '처리 중입니다', description = '잠시만 기다려 주세요.') {
  state.working = on;
  $('busy').hidden = !on;
  $('busy-title').textContent = title;
  $('busy-description').textContent = description;
  document.body.setAttribute('aria-busy', String(on));
}
async function responseError(response) {
  try { const data = await response.json(); return typeof data.detail === 'string' ? data.detail : '입력값을 확인해 주세요.'; }
  catch { return `요청에 실패했습니다 (${response.status}). 서버 상태를 확인해 주세요.`; }
}
async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let data;
    try { data = await response.json(); } catch { data = {detail: `요청 실패 (${response.status})`}; }
    const failure = new Error(typeof data.detail === 'string' ? data.detail : '입력값을 확인해 주세요.');
    failure.data = data;
    throw failure;
  }
  return response.json();
}
function clearResults() {
  state.result = null;
  $('exports').hidden = true;
  $('result-content').hidden = true;
  $('result-empty').hidden = false;
  $('result-badge').textContent = 'READY WHEN YOU ARE';
  $('step3').classList.remove('current');
}
function clearBOM() {
  state.validationVersion++;
  state.items = [];
  state.validationPending = false;
  state.eligibleRows = 0;
  state.exclusionToken = null;
  state.issues = [];
  $('issues-section').hidden = true;
  $('exclude-invalid').checked = false;
  $('bom-section').hidden = true;
  $('optimize').disabled = true;
  $('calculate-hint').textContent = 'BOM 데이터를 먼저 불러와 주세요.';
  $('step2').classList.remove('current');
  clearResults();
}
function fileForm() {
  const form = new FormData();
  form.append('file', state.file);
  return form;
}
function sheetForm() {
  const form = fileForm();
  form.append('sheet', $('sheet').value);
  form.append('header_row', $('header-row').value);
  form.append('kerf', $('kerf').value);
  return form;
}
function renderMapping(headers, mapping) {
  $('mapping').replaceChildren();
  for (const [field, label] of Object.entries(fields)) {
    const wrapper = document.createElement('label');
    wrapper.textContent = label;
    const select = document.createElement('select');
    select.id = `map-${field}`;
    const empty = new Option('열을 선택하세요', '');
    select.add(empty);
    headers.forEach((header, index) => {
      if (header) select.add(new Option(`${index + 1}열 · ${header}`, String(index)));
    });
    select.value = mapping[field] === undefined ? '' : String(mapping[field]);
    select.addEventListener('change', clearBOM);
    wrapper.append(select);
    $('mapping').append(wrapper);
  }
}
async function loadPreview() {
  clearBOM();
  const mapping = {};
  for (const field of Object.keys(fields)) {
    const value = $(`map-${field}`).value;
    if (value === '') throw new Error(`${fields[field]} 열을 선택해 주세요.`);
    mapping[field] = Number(value);
  }
  const form = sheetForm();
  form.append('mapping', JSON.stringify(mapping));
  const data = await api('/api/preview', {method: 'POST', body: form});
  if (data.errors.length) throw new Error('BOM의 다음 오류를 수정한 후 다시 업로드해 주세요. 일부 행만 계산하지 않습니다.\n' +
    data.errors.map(item => `${item.row}행 / ${item.title || '번호 없음'} / ${item.spec} / ${item.length}mm: ${item.message}`).join('\n'));
  state.items = data.items;
  const count = data.items.reduce((sum, item) => sum + item.quantity, 0);
  $('bom-count').textContent = `${fmt(data.items.length)}행 · ${fmt(count)}개`;
  $('bom-body').innerHTML = data.items.slice(0, 100).map((item, index) => `<tr><td>${item.source_row || index + 1}</td><td>${esc(item.title)}</td><td>${esc(item.material)}</td><td>${esc(item.spec)}</td><td class="numeric">${fmt(item.length)}</td><td class="numeric">${fmt(item.quantity)}</td></tr>`).join('');
  $('bom-more').textContent = `${data.items.length > 100 ? '화면에는 첫 100행을 표시합니다. ' : ''}빈 행 및 합계 행을 제외했습니다. ${data.skipped_rows?.map(r => `${r.row}행: ${r.reason}`).join(' / ') || ''} 길이 단위: mm`;
  $('bom-section').hidden = false;
  applyValidation(data);
  $('step2').classList.add('current');
}
async function loadFile(file) {
  if (state.working || !file) return;
  clearError(); clearBOM();
  state.file = null;
  $('mapping-panel').hidden = true;
  $('file-title').textContent = '엑셀 BOM 파일을 올려주세요';
  $('file-description').textContent = '파일을 여기로 끌어오거나 아래 버튼으로 선택하세요';
  if (!file.name.toLowerCase().endsWith('.xlsx')) return error('.xlsx 파일을 선택해 주세요.');
  if (file.size > 10 * 1024 * 1024) return error('엑셀 파일은 10MB 이하만 업로드할 수 있습니다.');
  state.file = file;
  busy(true, 'BOM을 읽고 있습니다', '시트와 재질·규격·길이·수량 열을 확인하고 있습니다.');
  try {
    const data = await api('/api/inspect', {method: 'POST', body: fileForm()});
    state.sheets = data.sheets;
    $('sheet').replaceChildren(...data.sheets.map(s => new Option(s.name, s.name)));
    $('sheet').value = data.suggested_sheet;
    const sheet = data.sheets.find(s => s.name === data.suggested_sheet);
    $('header-row').value = sheet.header_row;
    renderMapping(sheet.headers, sheet.mapping);
    $('mapping-panel').hidden = false;
    $('file-title').textContent = file.name;
    $('file-description').textContent = `${fmt(file.size / 1024)} KB · 시트와 열을 확인해 주세요`;
    if (Object.keys(sheet.mapping).length === 4) await loadPreview();
  } catch (e) { error(e.message); }
  finally { busy(false); }
}
$('file').addEventListener('change', event => { loadFile(event.target.files[0]); event.target.value = ''; });
['dragenter', 'dragover'].forEach(name => $('dropzone').addEventListener(name, event => {
  event.preventDefault(); $('dropzone').classList.add('dragover');
}));
['dragleave', 'drop'].forEach(name => $('dropzone').addEventListener(name, event => {
  event.preventDefault(); $('dropzone').classList.remove('dragover');
}));
$('dropzone').addEventListener('drop', event => loadFile(event.dataTransfer.files[0]));
$('sample').addEventListener('click', async () => {
  if (state.working) return;
  try {
    const response = await fetch('/api/sample');
    if (!response.ok) throw new Error(await responseError(response));
    await loadFile(new File([await response.blob()], 'sample_bom.xlsx', {type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}));
  } catch (e) { error(e.message); }
});
$('sheet').addEventListener('change', async () => {
  clearError(); clearBOM();
  const sheet = state.sheets.find(s => s.name === $('sheet').value);
  $('header-row').value = sheet.header_row;
  renderMapping(sheet.headers, sheet.mapping);
});
$('header-row').addEventListener('input', clearBOM);
$('headers').addEventListener('click', async () => {
  if (state.working) return;
  clearError(); clearBOM(); busy(true, '열을 다시 읽고 있습니다');
  try {
    const data = await api('/api/headers', {method: 'POST', body: sheetForm()});
    renderMapping(data.headers, data.mapping);
  } catch (e) { error(e.message); }
  finally { busy(false); }
});
$('preview').addEventListener('click', async () => {
  if (state.working) return;
  clearError(); busy(true, 'BOM 데이터를 확인하고 있습니다');
  try { await loadPreview(); } catch (e) { error(e.message); }
  finally { busy(false); }
});
$('kerf').addEventListener('input', revalidate);
$('time-limit').addEventListener('input', clearResults);
$('exclude-invalid').addEventListener('change', () => { clearResults(); updateCalculateButton(); });
$('optimize').addEventListener('click', async () => {
  if (state.working || !state.items.length) return;
  clearError(); clearResults();
  const kerf = $('kerf').value;
  if (kerf.trim() === '' || !Number.isFinite(Number(kerf)) || Number(kerf) < 0 || Number(kerf) > 100) return error('절단날 손실은 0~100mm로 입력해 주세요.');
  busy(true, '더 효율적인 절단 조합을 찾고 있습니다', `최대 ${$('time-limit').value}초 동안 재질·규격별로 계산합니다. 잠시만 기다려 주세요.`);
  try {
    state.result = await api('/api/optimize', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({items: state.items, kerf, time_limit: Number($('time-limit').value), exclude_invalid: $('exclude-invalid').checked, exclusion_token: state.exclusionToken})});
    renderResult();
    $('results-section').scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (e) { if (e.data?.issues) applyValidation(e.data); error(e.message); }
  finally { busy(false); }
});
function metric(id, value, unit) { $(id).innerHTML = `${fmt(value)}<em>${esc(unit)}</em>`; }
function renderResult() {
  const result = state.result, s = result.summary;
  $('result-empty').hidden = true;
  $('result-content').hidden = false;
  $('exports').hidden = false;
  $('result-badge').textContent = s.optimal ? '최적해 증명 완료' : '실행 가능 해 · 최적해 미확정';
  $('step3').classList.add('current');
  metric('metric-bars', s.bar_count, '본');
  metric('metric-loss', s.loss_rate, '%');
  metric('metric-remnant', s.total_remnant / 1000, 'm');
  metric('metric-pieces', s.piece_count, '개');
  $('metric-groups').textContent = `${s.group_count}개 재질·규격 그룹`;
  $('metric-time').textContent = `${s.elapsed_seconds}초 소요 · 수량 검증 완료`;
  $('orders-count').textContent = result.orders.length;
  $('patterns-count').textContent = result.patterns.length;
  $('orders-body').innerHTML = result.orders.map(o => `<tr><td>${esc(o.material_type)}</td><td>${esc(o.material)}</td><td>${esc(o.spec)}</td><td class="numeric">${fmt(o.stock_length)}</td><td class="numeric"><strong>${fmt(o.quantity)}</strong></td><td class="numeric">${fmt(o.total_length)}</td><td class="numeric">${fmt(o.loss_rate)}%</td></tr>`).join('');
  $('patterns-list').innerHTML = result.patterns.map(patternHTML).join('');
  $('result-settings').textContent = `${Object.entries(result.settings.stock_lengths_by_type).map(([k,v]) => `${k}: ${v.map(fmt).join(', ')}mm`).join(' / ')} · 날 손실 ${fmt(result.settings.kerf)}mm/개 · 잔재 > 30mm · 총 제품 ${fmt(s.total_parts_length)}mm · 총 날 손실 ${fmt(s.total_kerf_loss)}mm`;
  $('solver-status').innerHTML = result.statuses.map(r => `<li>${esc(r.material)} / ${esc(r.spec)} — ${esc(r.detail)}</li>`).join('');
  $('excluded-summary').textContent = result.excluded.length ? `${result.excluded.length}행 · ${fmt(s.excluded_piece_count)}개 제외 / 입력 ${fmt(s.input_piece_count)}개 = 생산 ${fmt(s.piece_count)}개 + 제외 ${fmt(s.excluded_piece_count)}개. 사용자가 제외를 선택한 부재입니다.` : '계산 제외 부재 없음 · 모든 입력 부재를 계산했습니다.';
  $('excluded-body').innerHTML = issueRows(result.excluded);
  selectTab('orders');
}
function patternHTML(p) {
  let bars = '', legend = '';
  p.cuts.forEach((cut, i) => {
    const width = cut.length * cut.quantity / p.stock_length * 100;
    const text = `${fmt(cut.length)} × ${cut.quantity}`;
    bars += `<div class="cut-segment" style="width:${width}%;background:${colors[i % colors.length]}" title="${text}개">${width > 9 ? text : ''}</div>`;
    legend += `<span><i style="background:${colors[i % colors.length]}"></i>${text}개</span>`;
  });
  const kerfWidth = p.kerf_loss / p.stock_length * 100, restWidth = p.remnant / p.stock_length * 100;
  bars += `<div class="cut-segment kerf-segment" style="width:${kerfWidth}%" title="날 손실 ${fmt(p.kerf_loss)}mm"></div><div class="cut-segment remnant-segment" style="width:${restWidth}%" title="잔재 ${fmt(p.remnant)}mm">${restWidth > 7 ? fmt(p.remnant) : ''}</div>`;
  return `<article class="pattern"><div class="pattern-heading"><div><span class="pattern-id">${esc(p.id)}</span><strong>${esc(p.material_type)} / ${esc(p.material)} / ${esc(p.spec)}</strong><small>원자재 ${fmt(p.stock_length)}mm</small></div><span class="repeat-badge">× ${p.bar_count}본 반복</span></div><div class="cut-bar" role="img" aria-label="${esc(p.id)} 절단 조합: ${p.cuts.map(c => `${fmt(c.length)}mm ${c.quantity}개`).join(', ')}, 잔재 ${fmt(p.remnant)}mm">${bars}</div><div class="pattern-detail"><div class="cut-legend">${legend}</div><span class="pattern-totals">날 손실 ${fmt(p.kerf_loss)} · 잔재 <strong>${fmt(p.remnant)}mm</strong> · 로스 ${fmt(p.loss_rate)}%</span></div></article>`;
}
function selectTab(name) {
  for (const tab of ['orders', 'patterns']) {
    $(`tab-${tab}`).classList.toggle('active', tab === name);
    $(`tab-${tab}`).setAttribute('aria-selected', String(tab === name));
    $(`${tab}-view`).hidden = tab !== name;
  }
}
['orders', 'patterns'].forEach(name => $(`tab-${name}`).addEventListener('click', () => selectTab(name)));
document.querySelectorAll('[data-format]').forEach(button => button.addEventListener('click', async () => {
  if (!state.result || state.working) return;
  clearError();
  const format = button.dataset.format;
  busy(true, `${format === 'pdf' ? 'PDF' : '엑셀'} 파일을 준비하고 있습니다`);
  try {
    const response = await fetch(`/api/results/${state.result.job_id}.${format}`);
    if (!response.ok) throw new Error(await responseError(response));
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url; link.download = `steel_cutting_plan.${format}`;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  } catch (e) { error(e.message); }
  finally { busy(false); }
}));

function issueRows(issues) {
  return issues.map(e => `<tr><td>${e.row}</td><td>${esc(e.title || '—')}</td><td>${esc(e.material_type)}</td><td>${esc(e.spec)}</td><td class="numeric">${fmt(e.length)}</td><td>${fmt(e.quantity)}</td><td>${e.allowed_lengths.map(fmt).join(', ') || '—'}</td><td>${esc(e.reason)}</td></tr>`).join('');
}
function applyValidation(data) {
  state.validationPending = false;
  state.exclusionToken = data.exclusion_token;
  state.issues = data.issues || [];
  state.eligibleRows = data.eligible_rows ?? state.items.length - state.issues.length;
  $('exclude-invalid').checked = false;
  $('issues-section').hidden = !state.issues.length;
  $('issues-count').textContent = `${state.issues.length}행 · ${fmt(state.issues.reduce((n, e) => n + e.quantity, 0))}개`;
  $('issues-body').innerHTML = issueRows(state.issues);
  updateCalculateButton();
}
function updateCalculateButton() {
  const allowed = state.items.length && state.eligibleRows > 0 && !state.validationPending && (!state.issues.length || $('exclude-invalid').checked);
  $('optimize').disabled = !allowed;
  $('calculate-hint').textContent = !state.items.length ? 'BOM 데이터를 먼저 불러와 주세요.' : state.validationPending ? '조건을 다시 확인하고 있습니다.' : state.eligibleRows === 0 ? '계산 가능한 정상 부재가 없습니다. 예외 목록을 확인해 주세요.' : state.issues.length && !$('exclude-invalid').checked ? '예외 목록을 확인한 뒤 제외 여부를 선택해 주세요.' : `${state.eligibleRows}개 정상 행 준비 완료${state.issues.length ? ` · 예외 ${state.issues.length}행 제외 선택` : ''}`;
}
async function revalidate() {
  clearResults();
  if (!state.items.length) return;
  const version = ++state.validationVersion;
  state.validationPending = true;
  $('exclude-invalid').checked = false;
  updateCalculateButton();
  try {
    const data = await api('/api/validate', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({items: state.items, kerf: $('kerf').value})});
    if (version !== state.validationVersion) return;
    state.validationPending = false;
    applyValidation(data);
  } catch (e) {
    if (version === state.validationVersion) { $('optimize').disabled = true; error(e.message); }
  }
}
function renderSettings(settings) {
  const entries = Object.entries(settings.stock_lengths_by_type);
  $('stock-summary').innerHTML = entries.map(([kind, lengths]) => `<div><strong>${esc(kind)}</strong><span>${lengths.map(fmt).join(' / ')} mm</span></div>`).join('');
  $('stock-editors').replaceChildren();
  for (const [kind, lengths] of entries) {
    const label = document.createElement('label'); label.className = 'field-label'; label.textContent = kind;
    const input = document.createElement('input'); input.dataset.materialType = kind;
    input.value = lengths.join(', '); input.setAttribute('aria-label', `${kind} 발주 길이`);
    label.append(input); $('stock-editors').append(label);
  }
}
$('save-settings').addEventListener('click', async () => {
  if (state.working) return;
  clearError(); busy(true, '자재 종류별 발주 길이를 저장하고 있습니다');
  try {
    const stock_lengths_by_type = {};
    document.querySelectorAll('[data-material-type]').forEach(input => {
      stock_lengths_by_type[input.dataset.materialType] = input.value.split(/[,;\s]+/).filter(Boolean);
    });
    const saved = await api('/api/settings', {method: 'PUT', headers: {'Content-Type': 'application/json', 'X-Admin-Password': $('admin-password').value}, body: JSON.stringify({stock_lengths_by_type})});
    $('admin-password').value = '';
    renderSettings(saved);
    $('settings-status').textContent = '저장 완료 · 서버를 재시작해도 유지됩니다.';
    await revalidate();
  } catch (e) { error(e.message); }
  finally { busy(false); }
});
api('/api/settings').then(renderSettings).catch(e => error(e.message));
