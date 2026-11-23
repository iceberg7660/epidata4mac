/* EpiData Mac 平替 — 前端（通用：任意 .qes/.chk/.rec 项目）
 *
 * 录入页 = 以「当前录入」为中心的记录导航视图：
 *  - 正在录入的草稿常驻内存 + 磁盘（<rec>.draft.json，每格实时保存）
 *  - 上一份/下一份查看其他问卷只是临时镜头，草稿不受影响；
 *    「↩ 回到当前正在录入的问卷」随时回到原位续录
 *  - 输满自动跳格 / 空格 Enter 跳过（必填当场拦截）/ 错误弹窗 / 纯键盘
 *  - 保存直接写入 .rec（自动备份）；删除前生成完整备份 */
'use strict';

const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

let P = null;                 // /api/project 载荷
let fieldsByName = {};        // name -> field meta
let inputs = {};              // name -> <input>
let order = [];               // 录入顺序（= .rec 字段顺序）
let secEls = [];              // 目录节点 [{el, text, tocEl}]

/* 录入状态机：
   entry.active = 正在录入一份新问卷（草稿）
   entry.values / entry.cursor = 草稿内容与光标（查看其他问卷期间保持不变）
   viewing = 正在查看的已保存记录下标；null = 焦点在正在录入的那份 */
let entry = {active: false, values: null, cursor: ''};
let viewing = null;

/* ---------------- 基础 ---------------- */

async function api(path, body) {
  const opt = body ? {method: 'POST', headers: {'Content-Type': 'application/json'},
                      body: JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  const j = await r.json();
  if (!r.ok) throw Object.assign(new Error(j.error || r.statusText), {j});
  return j;
}

/* 悬停浮层（列头题目全文等） */
const tipEl = document.createElement('div');
tipEl.id = 'th-tip';
tipEl.hidden = true;
document.body.append(tipEl);

function showTip(html, ev) {
  tipEl.innerHTML = html;
  tipEl.hidden = false;
  const pad = 14;
  const x = Math.min(ev.clientX + pad, window.innerWidth - tipEl.offsetWidth - 8);
  const y = Math.min(ev.clientY + pad, window.innerHeight - tipEl.offsetHeight - 8);
  tipEl.style.left = Math.max(8, x) + 'px';
  tipEl.style.top = Math.max(8, y) + 'px';
}
function hideTip() { tipEl.hidden = true; }

let toastTimer = null;
function toast(msg, isErr = false, ms = 3200) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = isErr ? 'err' : '';
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

let audioCtx = null;
function beep() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.type = 'square'; o.frequency.value = 196;
    g.gain.value = 0.06;
    o.connect(g); g.connect(audioCtx.destination);
    o.start(); o.stop(audioCtx.currentTime + 0.13);
  } catch (e) { /* 忽略 */ }
}

function gbkLen(s) {
  let n = 0;
  for (const ch of s) n += ch.codePointAt(0) > 255 ? 2 : 1;
  return n;
}
function clampBytes(inp, w) {
  let v = inp.value;
  while (gbkLen(v) > w) v = v.slice(0, -1);
  if (v !== inp.value) inp.value = v;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
}

/* ---------------- 键盘友好的弹窗（Enter=主按钮，Esc=次按钮） ---------------- */

function showModal({title, html, buttons, danger = true}) {
  return new Promise(resolve => {
    const root = $('#modal-root');
    root.innerHTML =
      '<div class="modal-mask"></div>' +
      '<div class="modal-box" role="dialog" aria-modal="true">' +
      `<h3 class="${danger ? '' : 'info'}">${esc(title)}</h3>` +
      `<div class="modal-msg">${html}</div>` +
      '<div class="modal-btns"></div></div>';
    root.hidden = false;
    modalOpen = true;
    const prev = document.activeElement;
    const btnsEl = root.querySelector('.modal-btns');
    let settled = false;
    const done = v => {
      if (settled) return;
      settled = true;
      document.removeEventListener('keydown', onKey, true);
      root.hidden = true; root.innerHTML = '';
      modalOpen = false;
      if (prev && prev.focus) prev.focus();
      resolve(v);
    };
    const onKey = e => {
      if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); done(buttons[0].value); }
      else if (e.key === 'Escape' && buttons.length > 1) { e.preventDefault(); e.stopPropagation(); done(buttons[1].value); }
    };
    document.addEventListener('keydown', onKey, true);
    buttons.forEach((b, i) => {
      const el = document.createElement('button');
      el.textContent = b.label;
      if (i === 0) el.className = 'primary';
      el.onclick = () => done(b.value);
      btnsEl.append(el);
    });
    btnsEl.querySelector('button').focus();
  });
}

let modalOpen = false;        // 弹窗期间拦截录入键处理

function alertMsg(title, html, refocusInput) {
  return showModal({title, html, buttons: [{label: '重新输入 (Enter)', value: true}], danger: true})
    .then(() => {
      if (refocusInput) { refocusInput.focus(); refocusInput.select(); }
    });
}

/* ---------------- 录入计时（纯内存，不写入 .rec） ----------------
 * 当前用时：仅「正在录入草稿 + 停留在录入页 + 无弹窗」时按真实经过时间累加；
 * 无操作 5 秒 / 切面板 / 查看历史记录 / 弹窗 → 暂停，有操作或回到录入页后继续；
 * 平均录入时间 = 本次会话累计有效用时 ÷ 当前 .rec 记录总数（含历史记录）。
 */
const IDLE_MS = 5000;
const timing = {
  path: null, currentMs: 0, sessionMs: 0,
  lastTs: performance.now(), idle: false, idleTimer: null,
};

function fmtDur(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const pad = n => String(n).padStart(2, '0');
  const h = Math.floor(s / 3600);
  return h ? `${pad(h)}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`
           : `${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
}
function timingActive() {
  return !!P && entry.active && viewing === null && !modalOpen &&
         !document.hidden && !$('#panel-entry').hidden;
}
function renderTiming() {
  const active = timingActive() && !timing.idle;
  $('#timer-value').textContent = fmtDur(timing.currentMs);
  $('#timer-state').textContent = !timingActive() ? '已暂停' : (timing.idle ? '已暂停（5 秒无操作）' : '');
  $('#timer-main').classList.toggle('paused', !active);
  const n = P ? P.records.length : 0;
  $('#timer-avg').textContent = n
    ? `平均录入时间 ${fmtDur(timing.sessionMs / n)} / 份（本文件 ${n} 份记录）`
    : '平均录入时间 — / 份（本文件暂无记录）';
}
function noteActivity() {
  if (!timingActive()) return;
  timing.idle = false;
  clearTimeout(timing.idleTimer);
  timing.idleTimer = setTimeout(() => { timing.idle = true; renderTiming(); }, IDLE_MS);
  renderTiming();
}
function resetCurrentTiming() {
  timing.currentMs = 0;
  noteActivity();
}
function resetTimingForRec(path) {
  timing.path = path;
  timing.currentMs = 0;
  timing.sessionMs = 0;
  timing.idle = false;
  clearTimeout(timing.idleTimer);
  renderTiming();
}
// 录入页内任意交互都算「有操作」：打字、按键、点击、滚动
['keydown', 'input', 'pointerdown', 'wheel', 'scroll'].forEach(evt =>
  document.addEventListener(evt, noteActivity, true));
setInterval(() => {
  const now = performance.now();
  if (timingActive() && !timing.idle) {
    timing.currentMs += now - timing.lastTs;
    timing.sessionMs += now - timing.lastTs;
  }
  timing.lastTs = now;
  renderTiming();
}, 250);

/* ---------------- 面板切换 ---------------- */

function showPanel(name) {
  $$('.panel').forEach(p => { p.hidden = p.id !== 'panel-' + name; });
  $$('nav button').forEach(b => b.classList.toggle('active', b.dataset.panel === name));
  // 回到录入页时，若正在录入草稿，光标自动回到原位
  if (name === 'entry' && P && entry.active && viewing === null) {
    const pos = entry.cursor || order[0];
    if (pos && inputs[pos]) focusField(pos, true);
  }
  // 每次进入浏览页都用最新数据重渲染（录入保存/删除后列表立即反映）
  if (name === 'browse' && P) renderBrowse();
  // 进入导出页时同步记录总数
  if (name === 'export' && P) {
    $('#exp-to').max = P.records.length;
    $('#exp-to').value = P.records.length;
  }
  // 回到录入页视为一次新操作：解除空闲暂停，重新开始 5 秒窗口
  if (name === 'entry') noteActivity();
}

$$('nav button').forEach(b => {
  b.onclick = () => { if (!b.disabled) showPanel(b.dataset.panel); };
});

/* ---------------- 打开文件 ---------------- */

$('#btn-scan').onclick = scan;
$('#dir-input').addEventListener('keydown', e => { if (e.key === 'Enter') scan(); });

$('#btn-pickdir').onclick = async () => {
  const btn = $('#btn-pickdir');
  btn.disabled = true;
  try {
    const r = await api('/api/pickdir', {});
    if (r.dir) { $('#dir-input').value = r.dir; await scan(); }
    else if (r.error) toast(r.error, true);
  } catch (e) { toast(e.message, true); }
  btn.disabled = false;
};

$('#btn-pickrec').onclick = async () => {
  const btn = $('#btn-pickrec');
  btn.disabled = true;
  try {
    const r = await api('/api/pickrec', {});
    if (r.path) { $('#dir-input').value = r.dir; await openRec(r.path); }
    else if (r.error) toast(r.error, true);
  } catch (e) { toast(e.message, true); }
  btn.disabled = false;
};

async function scan() {
  let r;
  try {
    r = await api('/api/scan', {dir: $('#dir-input').value.trim()});
  } catch (e) { toast(e.message, true); return; }
  $('#dir-input').value = r.dir;
  const list = $('#rec-list');
  list.innerHTML = r.recs.length
    ? r.recs.map(x => `<div class="rec-item" data-p="${esc(x.path)}">
        <span class="name">${esc(x.name)}</span>
        <span class="meta">${x.size.toLocaleString()} 字节 · ${esc(x.mtime)}</span>
      </div>`).join('')
    : '<p class="muted">该目录没有 .rec 文件</p>';
  list.querySelectorAll('.rec-item').forEach(el => { el.onclick = () => openRec(el.dataset.p); });
  fillSel($('#sel-qes'), r.qes_list, r.dir);
  fillSel($('#sel-chk'), r.chk_list, r.dir);
}

function fillSel(sel, paths, dir) {
  sel.innerHTML = '<option value="">（自动配对）</option>' +
    paths.map(p => {
      const rel = p.startsWith(dir + '/') ? p.slice(dir.length + 1) : p;
      return `<option value="${esc(p)}">${esc(rel)}</option>`;
    }).join('');
}

async function openRec(path) {
  const body = {path};
  if ($('#sel-qes').value) body.qes = $('#sel-qes').value;
  if ($('#sel-chk').value) body.chk = $('#sel-chk').value;
  try {
    P = await api('/api/open', body);
  } catch (e) { toast(e.message, true); return; }
  chkIgnoreMissing = !!P.ignore_missing;
  chkVars = {};
  chkEvalErrors.clear();
  chkReportedCount = 0;
  resetTimingForRec(P.path);          // 换 .rec 文件：计时与累计都清零

  fieldsByName = {}; inputs = {};
  P.fields.forEach(f => { fieldsByName[f.name] = f; });
  order = P.order;

  $('#fileinfo').textContent =
    `${P.path} — ${P.fields.length} 个字段 · ${P.records.length} 条记录` +
    (P.qes_path ? ` · 版面 ${P.qes_path.split('/').pop()}` : ' · （无 .qes，平铺表单）');
  $$('nav button').forEach(b => { b.disabled = false; });

  renderEntryView();
  renderBrowse();
  renderToc();
  renderExportDialog();
  showPanel('entry');
  if (P.warn && P.warn.length) toast(P.warn.join('；'), true);

  if (P.draft) await resumeDraft();
  else startNew();
}

/* ---------------- 问卷渲染（输入框前显示题号芯片） ---------------- */

function makeInput(f) {
  const inp = document.createElement('input');
  inp.className = 'qin';
  inp.dataset.name = f.name;
  inp.size = Math.min(Math.max(3, f.width), 40);
  inp.autocomplete = 'off';
  if (f.tc === 19) inp.placeholder = 'yyyy/mm/dd';
  inp.title = (f.name + (f.text ? '：' + f.text : '')) +
    (f.rng ? `（范围 ${f.rng[0]}~${f.rng[1]}）` : '') +
    (f.legal.length ? `（合法值 ${f.legal.join('/')})` : '');
  if (f.repeat) inp.classList.add('repeat');
  inp.addEventListener('input', () => {
    clampBytes(inp, f.width);
    inp.classList.toggle('filled', !!inp.value.trim());
    updateFieldStatus(f);
    // 输满自动跳格（按字段宽度；触发与 Enter 相同的校验+跳转逻辑）
    if (gbkLen(inp.value) >= f.width && document.activeElement === inp && !modalOpen) {
      advance(f.name);
    }
  });
  inp.addEventListener('keydown', onKey);
  // 回溯进入已填写的格子时内容自动全选：直接打字即覆盖，无需退格
  inp.addEventListener('focus', () => {
    if (inp.value) inp.select();
    updateFieldStatus(f);
  });
  inp.addEventListener('blur', () => sendDraft());
  inputs[f.name] = inp;
  return inp;
}

/* 底部状态栏：当前格子的字段名 / 类型与范围 / 长度（同原版 EpiData） */
function updateFieldStatus(f) {
  if (!f) return;
  $('#fs-name').textContent = f.name + (f.text ? ` ${f.text}` : '');
  let type;
  if (f.tc === 19) type = '日期: yyyy/mm/dd';
  else if (f.legal && f.legal.length) type = `合法值: ${f.legal.join('/')}`;
  else if (f.kind === '#') {
    type = f.rng ? `数值: ${f.rng[0]} ~ ${f.rng[1]}` : '数值';
    if (f.decimals > 0) type += `，保留 ${f.decimals} 位小数`;
  } else type = '文本';
  const cur = inputs[f.name] ? inputs[f.name].value.trim() : '';
  if (f.legal_labels && cur && f.legal_labels[cur]) type += ` · ${f.legal_labels[cur]}`;
  $('#fs-type').textContent = type;
  $('#fs-len').textContent = `长度: ${f.width}`;
}

/* 章节层级（与服务器 _sec_level 一致）：单字母=大标题，字母+数字=小标题，其余=文档标题 */
function secLevel(text) {
  const t = (text || '').trim();
  if (/^[A-Z]\s/.test(t)) return 'big';
  if (/^[A-Z][0-9]/.test(t)) return 'sub';
  return 'title';
}

function renderEntryView() {
  const view = $('#qes-view');
  view.innerHTML = '';
  secEls = [];
  const frag = document.createDocumentFragment();

  const addSection = text => {
    const lv = secLevel(text);
    const d = document.createElement('div');
    d.className = 'qline' + (lv !== 'title' ? ' qsec-' + lv : '');
    d.textContent = text || '（未分组）';
    frag.append(d);
    secEls.push({el: d, text: text || '', tocEl: null});
  };

  if (P.lines) {
    // 问卷文本逐行渲染；题号来自 .qes 文本中的 {标记}（显示时不带花括号）；
    // 章节标题行按层级（大标题/小标题/文档标题）加特别显示
    const secAt = {};
    P.sections.forEach(s => { secAt[s.row] = s; });
    const lineDivs = [];
    P.lines.forEach((parts, i) => {
      const div = document.createElement('div');
      div.className = 'qline';
      const lv = secAt[i + 1] ? secAt[i + 1].level : '';
      if (lv && lv !== 'title') div.classList.add('qsec-' + lv);
      let hasField = false;
      parts.forEach(p => {
        if (p.t === 'text') {
          if (p.s) div.append(document.createTextNode(p.s));
        } else if (fieldsByName[p.n]) {
          hasField = true;
          div.append(makeInput(fieldsByName[p.n]));
        }
      });
      lineDivs[i] = div;
      frag.append(div);            // 空行也渲染，保持原版的段落间隔
    });
    secEls = P.sections
      .map(s => ({el: lineDivs[s.row - 1], text: s.text, level: s.level, tocEl: null}))
      .filter(x => x.el);
  } else {
    let curSec = null;
    P.fields.forEach(f => {
      if (f.section !== curSec) {
        curSec = f.section;
        addSection(curSec);
      }
      const div = document.createElement('div');
      div.className = 'qline';
      const label = document.createElement('span');
      label.textContent = `${f.name} ${f.text ? ' ' + f.text : ''}：`;
      div.append(label, makeInput(f));
      frag.append(div);
    });
  }
  view.append(frag);
}

function renderToc() {
  const toc = $('#toc');
  toc.innerHTML = '';
  if (!secEls.length) { toc.hidden = true; return; }
  toc.hidden = false;
  secEls.forEach(s => {
    const d = document.createElement('div');
    d.textContent = s.text || '（未分组）';
    d.title = s.text || '';
    d.onclick = () => {
      secEls.forEach(x => x.tocEl.classList.toggle('active', x === s));
      scrollWithOffset(s.el);
    };
    toc.append(d);
    s.tocEl = d;
  });
}

/* 滚动定位时预留顶部固定栏（标题栏+工具栏）的高度，避免目标被盖住/滑过头 */
function scrollWithOffset(el) {
  const OFFSET = 155;   // 标题栏 + 悬浮计时条 + 固定工具栏的高度
  const y = el.getBoundingClientRect().top + window.scrollY - OFFSET;
  window.scrollTo({top: Math.max(0, y), behavior: 'smooth'});
}

function markToc(name) {
  const f = fieldsByName[name];
  if (!f) return;
  secEls.forEach(s => s.tocEl.classList.toggle('active', s.text === f.section));
}

/* ---------------- 校验、跳转与录入流 ---------------- */

const NUM_RE = /^-?\d+(\.\d+)?$/;

/* RANGE 支持 -INF / INF 与 dd/mm/yyyy 日期区间（EpiData 官方文档） */
function chkNum(s) {
  const t = String(s).trim().toUpperCase();
  if (t === 'INF' || t === 'INFINITY') return Infinity;
  if (t === '-INF' || t === '-INFINITY') return -Infinity;
  return Number(s);
}
function chkDateKey(s) {
  const m = String(s).trim().match(/^(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})$/);
  if (!m) return null;
  let y = +m[3];
  if (y < 100) y += y < 50 ? 2000 : 1900;
  return y * 10000 + (+m[2]) * 100 + (+m[1]);
}

function clientValidate(f, raw) {
  const v = (raw || '').trim();
  if (!v) return null;                     // 空值由必填当场拦截 / 跳转语义决定
  if (f.tc === 19) {
    if (f.legal.includes(v)) return null;   // 日期字段的 LEGAL / 缺失码
    const m = v.match(/^(\d{4})[\/\-](\d{1,2})[\/\-](\d{1,2})$/);
    if (!m) return '日期格式应为 yyyy/mm/dd';
    const d = new Date(+m[1], +m[2] - 1, +m[3]);
    if (d.getMonth() !== +m[2] - 1 || d.getDate() !== +m[3]) return '日期不存在';
    if (f.rng) {
      const key = chkDateKey(v), lo = chkDateKey(f.rng[0]), hi = chkDateKey(f.rng[1]);
      if (key !== null && lo !== null && hi !== null && (key < lo || key > hi))
        return `超出日期范围 ${f.rng[0]} ~ ${f.rng[1]}`;
    }
    return null;
  }
  // RANGE + LEGAL 同时存在时，LEGAL 是范围外的附加合法码（如 A03 的 9 = 不适用），
  // 不是「只能取 LEGAL 里的值」；只有 LEGAL 没有 RANGE 时才是穷举白名单。
  const legalOk = f.legal.length > 0 && f.legal.includes(v);
  if (f.legal.length && !f.rng && !legalOk) return `合法值仅为: ${f.legal.join(' / ')}`;
  if (f.kind === '#') {
    if (!NUM_RE.test(v) && !legalOk) return `数值字段，${v} 不是有效数字`;
    if (!legalOk) {
      if (f.decimals === 0 && v.includes('.')) return '整数字段，不能包含小数点';
      if (f.decimals > 0) {
        const fr = (v.split('.')[1] || '');
        if (fr.length > f.decimals) return `最多保留 ${f.decimals} 位小数`;
      }
    }
    if (f.rng && !legalOk) {
      const nv = chkNum(v, false), lo = chkNum(f.rng[0], false), hi = chkNum(f.rng[1], false);
      if (typeof nv !== 'number' || Number.isNaN(nv)) return `数值字段，${v} 不是有效数字`;
      const bad = typeof lo === 'number' && typeof hi === 'number' &&
                  !Number.isNaN(lo) && !Number.isNaN(hi) && (nv < lo || nv > hi);
      if (bad)
        return `超出范围 ${f.rng[0]} ~ ${f.rng[1]}` +
               (f.legal.length ? `（合法附加码：${f.legal.join(' / ')}）` : '');
    }
  }
  return null;
}

function setBad(inp, msg) {
  inp.classList.add('bad');
  inp.title = msg;
}
function clearBad(inp) {
  if (inp.classList.contains('bad')) {
    inp.classList.remove('bad');
    const f = fieldsByName[inp.dataset.name];
    inp.title = f ? (f.name + (f.text ? '：' + f.text : '')) : '';
  }
}

function val(name) { const i = inputs[name]; return i ? i.value.trim() : ''; }

function onKey(e) {
  if (modalOpen) return;
  if (e.key === 'Enter') {
    e.preventDefault();
    advance(e.target.dataset.name);
  } else if (e.key === 'ArrowDown') {
    e.preventDefault();
    move(+1, e.target.dataset.name);
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    move(-1, e.target.dataset.name);
  }
}

function move(dir, name) {
  const f = fieldsByName[name];
  // 方向键向前离开空必填字段同样当场拦截（向后回改不拦）
  if (dir > 0 && f && f.mustenter && !inputs[name].value.trim()) {
    setBad(inputs[name], '必填字段，不能为空'); beep();
    alertMsg(`「${f.name}」${f.text ? ' ' + f.text : ''} 为必填字段（MUSTENTER）`,
             '不能空着跳过，请输入内容后继续。', inputs[name]);
    return;
  }
  const i = order.indexOf(name) + dir;
  if (i >= 0 && i < order.length) focusField(order[i]);
}

/* 小数位自动补齐：###.# 字段录入 170 → 170.0（已有小数或位数超限则不动） */
function padDecimals(v, dec) {
  if (dec <= 0) return v;
  if (!v.includes('.')) return String(parseInt(v, 10)) + '.' + '0'.repeat(dec);
  let [ip, fr] = v.split('.');
  ip = String(parseInt(ip || '0', 10));
  if (fr.length < dec) fr = fr.padEnd(dec, '0');
  return ip + '.' + fr;
}

/* AFTER ENTRY 的 pos() 条件赋值：按来源字段内容自动填充目标字段（同原版 EpiData） */
function applyAfterEntryLets(name) {
  const f = fieldsByName[name];
  if (!f || !f.lets || !f.lets.length) return;
  for (const [srcName, needle, target, onFound, onMissing] of f.lets) {
    const src = inputs[srcName], inp = inputs[target];
    if (!src || !inp) continue;
    inp.value = src.value.includes(needle) ? onFound : onMissing;
    inp.classList.toggle('filled', !!inp.value.trim());
    clearBad(inp);
  }
}

/* ---- EpiData CHECK 表达式求值（官方运算符与常用函数） ------------------- */
const CHK_MISSING = Symbol('chk-missing');
let chkIgnoreMissing = false;
let chkVars = {};
const chkEvalErrors = new Set();     // 解析失败的 CHECK 表达式（去重后提示）
let chkReportedCount = 0;
let chkEntryCheckError = '';         // AFTER ENTRY 中 CHECK 未通过的提示

function chkIsNumString(s) {
  const t = String(s).trim();
  return t !== '' && !Number.isNaN(Number(t)) && chkDateKey(t) === null;
}
function chkDateNum(y, mo, d) {
  return Math.floor((Date.UTC(y, mo - 1, d) - Date.UTC(1899, 11, 31)) / 86400000) + 1;
}
function chkNumToDate(n) {
  const dt = new Date(Date.UTC(1899, 11, 31) + (Math.floor(n) - 1) * 86400000);
  const p = x => String(x).padStart(2, '0');
  return `${p(dt.getUTCDate())}/${p(dt.getUTCMonth() + 1)}/${dt.getUTCFullYear()}`;
}
function chkTodayNum() {
  const d = new Date();
  return chkDateNum(d.getFullYear(), d.getMonth() + 1, d.getDate());
}
function chkToNum(v, ignore) {
  if (v === CHK_MISSING || v === undefined || v === null) return ignore ? 0 : CHK_MISSING;
  if (typeof v === 'number') return v;
  if (typeof v === 'boolean') return v ? 1 : 0;
  const s = String(v).trim();
  const k = chkDateKey(s);
  if (k !== null) return chkDateNum(Math.floor(k / 10000), Math.floor(k / 100) % 100, k % 100);
  const n = Number(s);
  return Number.isNaN(n) ? (ignore ? 0 : CHK_MISSING) : n;
}
function chkNum(v, ignore) {
  const flag = ignore === undefined ? chkIgnoreMissing : ignore;
  const x = chkToNum(v, flag);
  return x === CHK_MISSING ? (flag ? 0 : CHK_MISSING) : x;
}
function chkTruth(v) {
  if (v === CHK_MISSING || v === undefined || v === null) return false;
  if (typeof v === 'number') return v !== 0;
  if (typeof v === 'boolean') return v;
  return !['', 'N', 'F', 'NO', 'FALSE', '0'].includes(String(v).trim().toUpperCase());
}
function chkArith(op, a, b) {
  if ((a === CHK_MISSING || b === CHK_MISSING) && !chkIgnoreMissing) return CHK_MISSING;
  const astr = typeof a === 'string' && !chkIsNumString(a) && chkDateKey(a) === null;
  const bstr = typeof b === 'string' && !chkIsNumString(b) && chkDateKey(b) === null;
  if (op === '+' && (astr || bstr)) return String(a === CHK_MISSING ? '' : a) +
                                        String(b === CHK_MISSING ? '' : b);
  const an = chkNum(a), bn = chkNum(b);
  if (typeof an !== 'number' || typeof bn !== 'number') return CHK_MISSING;
  switch (op) {
    case '+': return an + bn;
    case '-': return an - bn;
    case '*': return an * bn;
    case '/': return bn === 0 ? CHK_MISSING : an / bn;
    case 'div': return bn === 0 ? CHK_MISSING : Math.trunc(an / bn);
    case 'mod': return bn === 0 ? CHK_MISSING : an % bn;
    case '^': return Math.pow(an, bn);
    default: return CHK_MISSING;
  }
}
function chkCompare(a, op, b) {
  if (a === CHK_MISSING || b === CHK_MISSING) {
    if (op === '=') return a === b;
    if (op === '<>') return a !== b;
    return false;
  }
  const an = chkToNum(a), bn = chkToNum(b);
  const numeric = an !== CHK_MISSING && bn !== CHK_MISSING &&
    (typeof a !== 'string' || chkIsNumString(a) || chkDateKey(a) !== null) &&
    (typeof b !== 'string' || chkIsNumString(b) || chkDateKey(b) !== null);
  const x = numeric ? [an, bn] : [String(a).toLowerCase(), String(b).toLowerCase()];
  switch (op) {
    case '=': return x[0] === x[1];
    case '<>': return x[0] !== x[1];
    case '<': return x[0] < x[1];
    case '>': return x[0] > x[1];
    case '<=': return x[0] <= x[1];
    case '>=': return x[0] >= x[1];
    default: return false;
  }
}
function chkFieldValue(name) {
  if (inputs[name]) {
    const s = inputs[name].value.trim();
    return s === '' ? CHK_MISSING : s;
  }
  return Object.prototype.hasOwnProperty.call(chkVars, name) ? chkVars[name] : CHK_MISSING;
}
function chkDateParts(v) {
  const n = chkNum(v, false);
  if (typeof n !== 'number' || !Number.isFinite(n)) return null;
  const dt = new Date(Date.UTC(1899, 11, 31) + (Math.floor(n) - 1) * 86400000);
  const w = dt.getUTCDay();
  return {y: dt.getUTCFullYear(), m: dt.getUTCMonth() + 1, d: dt.getUTCDate(),
          w: w === 0 ? 7 : w};
}
function chkExpandArgs(args) {
  const out = [];
  for (const a of args) {
    if (typeof a === 'string' && a.includes('-')) {
      const [s, e] = a.split('-');
      const i = order.indexOf(s), j = order.indexOf(e);
      if (i >= 0 && j >= 0) {
        for (let k = i; k <= j; k++) out.push(chkFieldValue(order[k]));
        continue;
      }
    }
    out.push(a);
  }
  return out;
}
function chkCall(name, args) {
  const one = () => chkNum(args[0], false);
  const str = () => args[0] === CHK_MISSING || args[0] === undefined ? '' : String(args[0]);
  switch (name) {
    case 'ABS': { const n = one(); return n === CHK_MISSING ? n : Math.abs(n); }
    case 'SQR': { const n = one(); return n === CHK_MISSING ? n : n * n; }
    case 'SQRT': { const n = one(); return n === CHK_MISSING ? n : Math.sqrt(n); }
    case 'SIN': return Math.sin(one());
    case 'COS': return Math.cos(one());
    case 'TAN': return Math.tan(one());
    case 'ARCTAN': return Math.atan(one());
    case 'EXP': return Math.exp(one());
    case 'LN': return Math.log(one());
    case 'LOG10': return Math.log10(one());
    case 'INT': { const n = one(); return n === CHK_MISSING ? n : Math.trunc(n); }
    case 'INTEGER': { const n = parseInt(str(), 10); return Number.isNaN(n) ? CHK_MISSING : n; }
    case 'ROUND': { const n = one(); return n === CHK_MISSING ? n : Math.sign(n) * Math.round(Math.abs(n)); }
    case 'TRUNC': { const n = one(); return n === CHK_MISSING ? n : Math.trunc(n); }
    case 'FLOAT': return one();
    case 'FRAC': { const n = one(); return n === CHK_MISSING ? n : n - Math.trunc(n); }
    case 'POWER': return Math.pow(chkNum(args[0], false), chkNum(args[1], false));
    case 'PI': return Math.PI;
    case 'STRING': return str();
    case 'UPPER': return str().toUpperCase();
    case 'LOWER': return str().toLowerCase();
    case 'LENGTH': return str().length;
    case 'COPY': {
      const s = str(), i = chkNum(args[1], false), c = chkNum(args[2], false);
      return s.substr(Math.max(0, (i || 1) - 1), c || 0);
    }
    case 'POS': return String(args[1] === undefined ? '' : args[1]).toLowerCase()
                      .indexOf(str().toLowerCase()) + 1;   // 未找到返回 0
    case 'DATE': return chkDateNum(chkNum(args[2], false), chkNum(args[1], false), chkNum(args[0], false));
    case 'DAY': { const p = chkDateParts(args[0]); return p ? p.d : CHK_MISSING; }
    case 'MONTH': { const p = chkDateParts(args[0]); return p ? p.m : CHK_MISSING; }
    case 'YEAR': { const p = chkDateParts(args[0]); return p ? p.y : CHK_MISSING; }
    case 'DAYOFWEEK': { const p = chkDateParts(args[0]); return p ? p.w : CHK_MISSING; }
    case 'WEEKNUM': {
      const p = chkDateParts(args[0]);
      if (!p) return CHK_MISSING;
      const start = new Date(Date.UTC(p.y, 0, 1));
      return Math.floor((Date.UTC(p.y, p.m - 1, p.d) - start.getTime()) / 604800000) + 1;
    }
    case 'TODAY': return chkTodayNum();
    case 'NOW': {
      const d = new Date();
      return chkTodayNum() + (d.getHours() * 60 + d.getMinutes()) / 1440;
    }
    case 'TIME2NUM': { const n = one(); return n === CHK_MISSING ? n : (n % 24) / 24; }
    case 'NUM2TIME': { const n = one(); return n === CHK_MISSING ? n : Math.round((n % 1) * 100); }
    case 'RANGE': {
      const a = chkNum(args[0], false), lo = chkNum(args[1], false), hi = chkNum(args[2], false);
      return a >= lo && a <= hi;
    }
    case 'SUM': case 'MEAN': case 'MIN': case 'MAX': case 'COUNTMISSING': {
      const vals = chkExpandArgs(args);
      if (name === 'COUNTMISSING') return vals.filter(v => v === CHK_MISSING).length;
      const nums = vals.map(v => chkNum(v, false)).filter(v => typeof v === 'number');
      if (!nums.length) return CHK_MISSING;
      if (name === 'SUM') return nums.reduce((a, b) => a + b, 0);
      if (name === 'MEAN') return nums.reduce((a, b) => a + b, 0) / nums.length;
      return name === 'MIN' ? Math.min(...nums) : Math.max(...nums);
    }
    default: return CHK_MISSING;
  }
}
function chkTokenize(src) {
  const out = [];
  const s = String(src);
  let i = 0;
  while (i < s.length) {
    const c = s[i];
    if (/\s/.test(c)) { i++; continue; }
    if (c === '*' && s[i + 1] === '*') break;
    if (/[0-9]/.test(c) || (c === '.' && /[0-9]/.test(s[i + 1] || ''))) {
      let j = i;
      while (j < s.length && /[0-9.]/.test(s[j])) j++;
      out.push({t: 'num', v: Number(s.slice(i, j))});
      i = j; continue;
    }
    if (c === '"' || c === "'") {
      let j = i + 1, buf = '';
      while (j < s.length && s[j] !== c) {
        if (s[j] === '\\' && j + 1 < s.length) { buf += s[j + 1]; j += 2; }
        else buf += s[j++];
      }
      out.push({t: 'str', v: buf});
      i = j + 1; continue;
    }
    if (/[A-Za-z_]/.test(c)) {
      let j = i;
      while (j < s.length && /[A-Za-z0-9_]/.test(s[j])) j++;
      out.push({t: 'id', v: s.slice(i, j)});
      i = j; continue;
    }
    const two = s.slice(i, i + 2);
    if (['<>', '!=', '<=', '>='].includes(two)) { out.push({t: 'op', v: two}); i += 2; continue; }
    if ('+-*/^=()<>,.@'.includes(c)) { out.push({t: 'op', v: c}); i++; continue; }
    throw new Error('未知字符 ' + c);
  }
  return out;
}
class ChkExpr {
  constructor(src) { this.toks = chkTokenize(src); this.i = 0; }
  peek() { return this.toks[this.i]; }
  eat(v) {
    const t = this.peek();
    if (!t) return false;
    const same = t.t === 'id' ? t.v.toLowerCase() === String(v).toLowerCase() : t.v === v;
    if (same) { this.i++; return true; }
    return false;
  }
  expect(v) { if (!this.eat(v)) throw new Error('缺少 ' + v); }
  parse() { const v = this.or(); if (this.i < this.toks.length) throw new Error('多余字符'); return v; }
  or() { let a = this.xor(); while (this.eat('or')) { const b = this.xor(); a = chkTruth(a) || chkTruth(b); } return a; }
  xor() { let a = this.and(); while (this.eat('xor')) { const b = this.and(); a = chkTruth(a) !== chkTruth(b); } return a; }
  and() { let a = this.not(); while (this.eat('and')) { const b = this.not(); a = chkTruth(a) && chkTruth(b); } return a; }
  not() { if (this.eat('not')) return !chkTruth(this.not()); return this.cmp(); }
  cmp() {
    let a = this.add();
    const ops = ['=', '<>', '!=', '<=', '>=', '<', '>'];
    while (ops.some(o => this.eat(o))) {
      const op = this.toks[this.i - 1].v;
      a = chkCompare(a, op === '!=' ? '<>' : op, this.add());
    }
    return a;
  }
  add() {
    let a = this.mul();
    while (this.eat('+') || this.eat('-')) {
      const op = this.toks[this.i - 1].v;
      a = chkArith(op, a, this.mul());
    }
    return a;
  }
  mul() {
    let a = this.unary();
    while (this.eat('*') || this.eat('/') || this.eat('div') || this.eat('mod')) {
      const op = this.toks[this.i - 1].v;
      a = chkArith(op, a, this.unary());
    }
    return a;
  }
  unary() {
    if (this.eat('-')) { const v = chkNum(this.unary(), false); return typeof v === 'number' ? -v : v; }
    if (this.eat('+')) return this.unary();
    return this.pow();
  }
  pow() {
    const a = this.primary();
    if (this.eat('^')) {
      const b = chkNum(this.pow(), false);
      const n = chkNum(a, false);
      return typeof n === 'number' && typeof b === 'number' ? Math.pow(n, b) : CHK_MISSING;
    }
    return a;
  }
  primary() {
    const t = this.peek();
    if (!t) throw new Error('表达式不完整');
    if (this.eat('(')) { const v = this.or(); this.expect(')'); return v; }
    if (t.t === 'num') { this.i++; return t.v; }
    if (t.t === 'str') { this.i++; return t.v; }
    if (this.eat('.')) return CHK_MISSING;
    if (this.eat('@')) {
      const id = this.peek();
      if (!id || id.t !== 'id') throw new Error('@ 后应为字段名');
      this.i++;
      return inputs[id.v] ? inputs[id.v].value.trim() : '';
    }
    if (t.t === 'id') {
      this.i++;
      const up = t.v.toUpperCase();
      if (this.eat('(')) {
        const args = [];
        if (!this.eat(')')) {
          do { args.push(this.or()); } while (this.eat(','));
          this.expect(')');
        }
        return chkCall(up, args);
      }
      if (up === 'PI') return Math.PI;
      if (up === 'TRUE') return true;
      if (up === 'FALSE') return false;
      if (up === 'TODAY') return chkTodayNum();
      if (up === 'NOW') return chkCall('NOW', []);
      return chkFieldValue(t.v);
    }
    throw new Error('无法解析 ' + t.v);
  }
}
function chkEval(src) {
  try { return new ChkExpr(src).parse(); }
  catch (e) {
    chkEvalErrors.add(`${String(src).slice(0, 50)} → ${e.message}`);
    return undefined;                  // 调用方回退，但错误会被提示，不静默失效
  }
}
function chkReportEvalErrors() {
  if (chkEvalErrors.size <= chkReportedCount) return;
  chkReportedCount = chkEvalErrors.size;
  toast('部分 CHECK 表达式未能解析：' + [...chkEvalErrors][0], true, 6000);
}

/* ---- 通用 .chk AFTER ENTRY 动作执行：IF/ELSE、GOTO、LET、CLEAR、HELP ---- */
function chkResolveValue(raw, target) {
  const s = String(raw == null ? '' : raw);
  if (!s) return '';
  const v = chkEval(s);
  if (v === undefined) return s === '.' ? '' : s;   // 表达式解析失败：按字面量处理
  if (v === CHK_MISSING) return '';
  if (typeof v === 'boolean') return v ? 'Y' : 'N';
  const tf = target && fieldsByName[target];
  if (typeof v === 'number' && tf && tf.tc === 19) return chkNumToDate(v);
  return String(v);
}
/* 区分字面量与字段/表达式：未加引号的 F、N 等应作为字符串字面量 */
function chkCondRhs(c) {
  const t = String(c.value_raw || c.value || '').trim();
  const quoted = t.length >= 2 && ((t[0] === '"' && t.at(-1) === '"') ||
                                   (t[0] === "'" && t.at(-1) === "'"));
  const ident = /^[A-Za-z_][A-Za-z0-9_]*$/.test(t);
  const known = quoted || /^-?\d+(\.\d+)?$/.test(t) || t === '.' ||
    /^(TODAY|NOW|TRUE|FALSE|PI)$/i.test(t) ||
    (ident && (inputs[t] || Object.prototype.hasOwnProperty.call(chkVars, t))) ||
    /[()@+\-*/^<>=]/.test(t) || t.includes(' ');
  return known ? t : JSON.stringify(t);
}
function chkCondTrue(c) {
  const kind = c.k === 'if' ? c.kind : c.k;
  if (kind === 'pos') {
    const s = inputs[c.field] ? inputs[c.field].value.toLowerCase() : '';
    const p = s.indexOf(String(c.value).toLowerCase()) + 1;   // pos 未找到返回 0
    const t = Number(c.threshold === undefined ? 0 : c.threshold);
    switch (c.op) {
      case '=': return p === t;
      case '<>': return p !== t;
      case '>': return p > t;
      case '>=': return p >= t;
      case '<': return p < t;
      case '<=': return p <= t;
      default: return p !== 0;
    }
  }
  if (kind === 'cmp') {
    const v = chkEval(`${c.field} ${c.op} ${chkCondRhs(c)}`);
    if (v !== undefined) return chkTruth(v);
    // 表达式解析失败时的简单回退
    const left = inputs[c.field] ? inputs[c.field].value.trim() : '';
    const right = chkResolveValue(c.value);
    return chkCompare(left, c.op, right);
  }
  if (kind === 'expr') {
    const v = chkEval(c.value);
    return v === undefined ? false : chkTruth(v);
  }
  return false;
}
function runChkActions(actions, owner) {
  let gotoTarget = null;
  (actions || []).forEach(a => {
    if (a.k === 'if') {
      const g = runChkActions(chkCondTrue(a) ? a.then : a.else, owner);
      if (g !== null) gotoTarget = g;
    } else if (a.k === 'goto') {
      gotoTarget = a.target || owner;
    } else if (a.k === 'clear') {
      const n = a.target || owner;
      if (inputs[n]) {
        inputs[n].value = '';
        inputs[n].classList.remove('filled');
        clearBad(inputs[n]);
      }
    } else if (a.k === 'let') {
      const inp = inputs[a.target];
      if (inp) {
        inp.value = chkResolveValue(a.value, a.target);
        inp.classList.toggle('filled', !!inp.value.trim());
        clearBad(inp);
      } else {
        chkVars[a.target] = chkEval(a.value);
      }
    } else if (a.k === 'check') {
      if (!chkTruth(chkEval(a.value)) && !chkEntryCheckError)
        chkEntryCheckError = a.text || 'CHECK 校验未通过';
    } else if (a.k === 'help') {
      toast(a.text, false, 5000);
    }
  });
  return gotoTarget;
}
/* AFTER RECORD：CHECK 表达式失败或 GOTO 都会阻止保存（同原版 EpiData） */
function runRecordActions(actions) {
  const res = {goto: null, failed: false, message: ''};
  (actions || []).forEach(a => {
    if (a.k === 'if') {
      const sub = runRecordActions(chkCondTrue(a) ? a.then : a.else);
      if (sub.goto !== null) res.goto = sub.goto;
      if (sub.failed && !res.failed) { res.failed = true; res.message = sub.message; }
    } else if (a.k === 'check') {
      if (!chkTruth(chkEval(a.value)) && !res.failed) {
        res.failed = true;
        res.message = a.text;
      }
    } else if (a.k === 'goto') {
      res.goto = a.target;
    } else if (a.k === 'help' || a.k === 'type') {
      if (a.text) toast(a.text, false, 5000);
    } else if (a.k === 'let' || a.k === 'clear') {
      runChkActions([a], order[0]);
    }
  });
  return res;
}
function chkTargetIndex(t, i) {
  const k = String(t || '').toLowerCase();
  if (k === '' ) return i;                       // GOTO 无参数 = 当前字段
  if (k === 'end') return order.length - 1;
  if (k === 'skipnextfield') return Math.min(i + 2, order.length - 1);
  if (k === 'write' || k === 'writerec') return -2;
  return order.indexOf(t);
}
/* JUMPS RESET：把跳过的字段按宽度填成指定字符（日期字段除外） */
function applyJumpReset(from, to, fill) {
  if (!fill || to <= from + 1) return;
  for (let k = from + 1; k < to; k++) {
    const fld = fieldsByName[order[k]], inp = inputs[order[k]];
    if (!fld || !inp || fld.tc === 19) continue;
    inp.value = String(fill).repeat(fld.width || 1).slice(0, fld.width || 1);
    inp.classList.add('filled');
  }
}

/* 确认当前格并前进：先校验（补小数/格式化日期），跳转规则优先，
   否则严格按问卷顺序到下一格（与原版 EpiData 一致，已有数据的格子不跳过） */
function advance(name) {
  const f = fieldsByName[name];
  const inp = inputs[name];
  if (!f || !inp) return;
  let v = inp.value.trim();
  // MISSINGVALUE：录入 "-" 离开字段时替换为第一缺失值
  if (v === '-' && f.missingvalues && f.missingvalues.length) {
    v = f.missingvalues[0];
    inp.value = v;
  }
  if (!v && f.mustenter) {
    setBad(inp, '必填字段，不能为空'); beep();
    alertMsg(`「${f.name}」${f.text ? ' ' + f.text : ''} 为必填字段（MUSTENTER）`,
             '不能空着跳过，请输入内容后按 Enter 继续。', inp);
    return;
  }
  if (f.tc === 19) {
    const d8 = v.match(/^(\d{4})(\d{2})(\d{2})$/);   // 20261122 → 2026/11/22
    if (d8) { v = `${d8[1]}/${d8[2]}/${d8[3]}`; inp.value = v; }
  }
  const msg = v ? clientValidate(f, v) : null;
  if (msg) {
    setBad(inp, msg); beep();
    alertMsg(`「${f.name}」${f.text ? ' ' + f.text : ''} 输入有误`,
             esc(msg) + (f.rng ? `\n允许范围：${f.rng[0]} ~ ${f.rng[1]}` : ''), inp);
    return;
  }
  if (v && f.kind === '#' && f.tc !== 19) {
    if (f.decimals > 0) {
      v = padDecimals(v, f.decimals);
    } else {
      v = String(parseInt(v, 10));           // 整数字段去前导零（0103120237 → 103120237）
    }
    if (inp.value !== v) inp.value = v;      // 跳转前规范化显示（同原版浮点/整数字段）
  }
  clearBad(inp);
  let actTarget = null;
  chkEntryCheckError = '';
  if (f.actions && f.actions.length) actTarget = runChkActions(f.actions, name);
  else applyAfterEntryLets(name);
  chkReportEvalErrors();
  if (chkEntryCheckError) {
    setBad(inp, chkEntryCheckError); beep();
    toast(chkEntryCheckError, true, 6000);
    return;
  }

  const i = order.indexOf(name);
  let next = -1, jumpFill = '';
  if (actTarget !== null) {
    next = chkTargetIndex(actTarget, i);
  } else if (f.autojump) {
    next = chkTargetIndex(f.autojump, i);
    jumpFill = f.jumps_reset || '';
  } else if (f.jumps && f.jumps[v] !== undefined) {
    next = chkTargetIndex(f.jumps[v], i);
    jumpFill = f.jumps_reset || '';
  } else if (f.gotos && f.gotos.length) {
    for (const g of f.gotos) {
      if (val(g[0]) === g[1]) {
        next = chkTargetIndex(g[2], i);
        if (next >= 0) break;
      }
    }
  }
  // AFTER ENTRY 的裸 GOTO：无条件跳转（JUMPS / 条件 GOTO 未命中时才生效）
  if (next < 0 && f.after_goto) next = chkTargetIndex(f.after_goto, i);
  if (next < 0) next = i + 1;                          // 顺序前进（不跳过已填格子）
  if (next === -2 || next >= order.length) { askSave(); return; }
  if (jumpFill) applyJumpReset(i, next, jumpFill);
  focusField(order[next], true);
  sendDraft();
}

function focusField(name, track = false, visited = null) {
  const inp = inputs[name];
  if (!inp) return;
  // BEFORE ENTRY 互跳时防止无限递归：同一字段只执行一次动作，但仍要完成聚焦
  const seen = visited || new Set();
  const already = seen.has(name);
  seen.add(name);
  // BEFORE ENTRY：进入字段时执行赋值/跳转（官方 CHECK 语义）
  const f = fieldsByName[name];
  const i = order.indexOf(name);
  if (!already && f && f.before_actions && f.before_actions.length) {
    chkEntryCheckError = '';
    const target = runChkActions(f.before_actions, name);
    chkReportEvalErrors();
    if (chkEntryCheckError) { toast(chkEntryCheckError, true, 6000); return; }
    if (target !== null) {
      const t = chkTargetIndex(target, i);
      if (t >= 0 && t < order.length && t !== i) {
        focusField(order[t], track, seen);
        return;
      }
    }
  }
  inp.scrollIntoView({behavior: 'smooth', block: 'center'});
  inp.focus();
  $('#entry-progress').textContent = `字段 ${i + 1} / ${order.length}`;
  markToc(name);
  updateFieldStatus(fieldsByName[name]);
  if (track && entry.active && viewing === null) entry.cursor = name;   // 记住录入原位
}

/* ---------------- 草稿（伴生 <rec>.draft.json，实时保存可续录） ---------------- */

let draftTimer = null;
function sendDraft() {
  if (!entry.active || viewing !== null) return;   // 只保存「正在录入」的草稿
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => {
    if (!P || !entry.active) return;
    api('/api/draft', {path: P.path, values: collectValues(), cursor: entry.cursor}).catch(() => {});
  }, 150);
}

async function resumeDraft() {
  const d = P.draft;
  const vals = d.values || {};
  const k = order.filter(n => vals[n] && vals[n].trim()).length;
  const go = await showModal({
    title: '发现未完成的录入草稿',
    html: `上次保存：${esc(d.saved_at || '未知时间')}<br>已填 ${k} 个字段，光标位于 ${esc(d.cursor || '开头')}。` +
          '<br>已写入 .rec 的记录不受影响，草稿只是未完成记录的暂存。',
    buttons: [{label: '继续录入 (Enter)', value: true}, {label: '放弃草稿 (Esc)', value: false}],
    danger: false,
  });
  if (go) {
    entry = {active: true, values: {...vals}, cursor: d.cursor || ''};
    viewing = null;
    P.fields.forEach(f => { inputs[f.name].value = vals[f.name] || ''; });
    refreshFilled();
    updateEntryUI();
    focusField(inputs[entry.cursor] ? entry.cursor : order[0], true);
    resetCurrentTiming();              // 续录也从 00:00 开始新的一份
  } else {
    await api('/api/draft/clear', {}).catch(() => {});
    startNew();
  }
}

/* ---------------- 记录状态机：录入中 / 查看其他问卷 ---------------- */

function refreshFilled() {
  order.forEach(n => inputs[n].classList.toggle('filled', !!inputs[n].value.trim()));
}

function updateEntryUI() {
  const entrying = entry.active && viewing === null;
  const badge = $('#entry-mode');
  if (entrying) {
    badge.textContent = `正在录入（第 ${P.records.length + 1} 条）· 草稿自动保存`;
    badge.classList.remove('edit');
  } else {
    badge.textContent = `查看/编辑 记录 #${viewing + 1} / ${P.records.length}`;
    badge.classList.add('edit');
  }
  $('#btn-back-entry').hidden = !(entry.active && viewing !== null);
  $('#btn-del-rec').disabled = viewing === null;
  $('#btn-save').textContent = entrying ? '保存到 .rec (Ctrl+Enter)' : '保存修改 (Ctrl+Enter)';
}

function startNew() {
  entry = {active: true, values: null, cursor: ''};
  viewing = null;
  chkVars = {};                        // DEFINE/LET 临时变量每条记录重置
  order.forEach(n => { inputs[n].value = ''; });
  const last = P.records[P.records.length - 1];
  if (last) {
    P.fields.forEach((f, k) => {
      if (f.repeat && last[k]) inputs[f.name].value = last[k];
    });
  }
  refreshFilled();
  updateEntryUI();
  focusField(order[0], true);
  resetCurrentTiming();                // 每份新问卷从 00:00 重新计时
}

/* 离开录入视图前快照草稿（只在自己正处于录入视图时快照） */
function snapshotEntry() {
  if (entry.active && viewing === null) {
    entry.values = collectValues();
  }
}

function viewRecord(i) {
  if (i < 0 || i >= P.records.length) { toast('没有这条记录'); return; }
  snapshotEntry();
  viewing = i;
  P.fields.forEach((f, k) => { inputs[f.name].value = P.records[i][k] || ''; });
  refreshFilled();
  updateEntryUI();
  showPanel('entry');
  focusField(order[0]);
}

function returnToEntry() {
  viewing = null;
  if (entry.values) {
    P.fields.forEach(f => { inputs[f.name].value = entry.values[f.name] || ''; });
  } else {
    order.forEach(n => { inputs[n].value = ''; });
  }
  refreshFilled();
  updateEntryUI();
  focusField(inputs[entry.cursor] ? entry.cursor : order[0], true);
  noteActivity();                      // 回到正在录入的那份：继续计时
}

function collectValues() {
  const values = {};
  order.forEach(n => { values[n] = inputs[n].value.trim(); });
  return values;
}

/* ---------------- 记录导航：上一份 / 下一份 / 新增 / 删除 ---------------- */

$('#btn-prev-rec').onclick = () => {
  if (!P.records.length) { toast('还没有已保存的记录'); return; }
  if (viewing === null) { viewRecord(P.records.length - 1); return; }
  if (viewing === 0) { toast('已经是第一条记录'); return; }
  viewRecord(viewing - 1);
};

$('#btn-next-rec').onclick = () => {
  if (viewing === null) { toast('正在录入的就是下一份问卷'); return; }
  if (viewing >= P.records.length - 1) {
    if (entry.active) returnToEntry();     // 已到最后一条 → 回到正在录入的那份
    else startNew();
    return;
  }
  viewRecord(viewing + 1);
};

$('#btn-back-entry').onclick = returnToEntry;

$('#btn-new-rec').onclick = $('#btn-new').onclick = () => {
  showPanel('entry');
  if (entry.active) {
    if (viewing !== null) returnToEntry();
    else focusField(entry.cursor || order[0], true);
  } else {
    startNew();
  }
};

$('#btn-del-rec').onclick = async () => {
  if (viewing === null) return;
  const idx = viewing;
  const ok = await showModal({
    title: `删除记录 #${idx + 1}？`,
    html: '删除前会自动生成完整备份（.deleted-*.bak），可从备份找回。<br>' +
          '该记录将从 .rec 文件中移除，Windows EpiData 打开时不再显示。',
    buttons: [{label: '删除 (Enter)', value: true}, {label: '取消 (Esc)', value: false}],
  });
  if (!ok) return;
  try {
    const r = await api('/api/delete', {path: P.path, index: idx});
    P.records.splice(idx, 1);
    toast(`记录 #${idx + 1} 已删除，备份：${r.backup.split('/').pop()}`);
    renderBrowse();
    if (!P.records.length) {
      if (entry.active) returnToEntry(); else startNew();
    } else {
      viewRecord(Math.min(idx, P.records.length - 1));
    }
  } catch (e) { toast(e.message, true); }
};

$('#btn-to-list').onclick = () => showPanel('browse');

/* ---------------- 保存 ---------------- */

async function askSave() {
  const keyCols = P.fields.slice(0, 6).map(f => `${f.name}=${val(f.name) || '空'}`);
  const nameA01 = fieldsByName['a01'] ? `姓名=${val('a01') || '空'}` : '';
  const ok = await showModal({
    title: viewing !== null ? `把修改保存到 记录 #${viewing + 1}？` : '这条问卷录完了，保存到 .rec？',
    html: esc(keyCols.join('  ')) + (nameA01 ? '<br>' + esc(nameA01) : '') +
          '<br><span style="color:#6b7280">保存后自动开始下一条；.rec 原有内容自动备份 .bak</span>',
    buttons: [{label: '保存 (Enter)', value: true}, {label: '继续修改 (Esc)', value: false}],
    danger: false,
  });
  if (ok) save();
}

async function save() {
  let values = collectValues();
  const errs = [];
  P.fields.forEach(f => {
    // 必填已由录入时的当场拦截保证；跳转合法跳过的必填允许留空（与 EpiData 一致）
    const m = clientValidate(f, values[f.name]);
    if (m) errs.push({f, msg: m});
  });
  if (errs.length) {
    const e0 = errs[0];
    const list = errs.slice(0, 5).map(x => `• ${x.f.name}${x.f.text ? ' ' + x.f.text : ''}：${x.msg}`).join('\n');
    setBad(inputs[e0.f.name], e0.msg); beep();
    await alertMsg(`还有 ${errs.length} 个字段未通过校验，不能保存`,
                   esc(list) + (errs.length > 5 ? `\n… 共 ${errs.length} 项` : ''), inputs[e0.f.name]);
    return;
  }
  // BEFORE RECORD 赋值/清除后重新收集值
  if (P.before_record_actions && P.before_record_actions.length) {
    runChkActions(P.before_record_actions, order[0]);
    values = collectValues();
  }
  // AFTER RECORD 的记录级检查：CHECK 不通过或 GOTO 都阻止保存
  const recCheck = runRecordActions(P.after_record_actions);
  chkReportEvalErrors();
  if (recCheck.failed || recCheck.goto !== null) {
    beep();
    toast(recCheck.message || '记录级检查未通过，已停止保存', true, 6000);
    const t = recCheck.goto ? chkTargetIndex(recCheck.goto, 0) : -1;
    if (t >= 0 && t < order.length) focusField(order[t], true);
    return;
  }
  const isEntrying = entry.active && viewing === null;
  try {
    const r = await api('/api/save', {path: P.path, index: viewing !== null ? viewing : null, values});
    P.records[r.index] = order.map(n => values[n]);
    if (isEntrying) {
      toast(`第 ${r.index + 1} 条已写入 .rec，继续录下一条`);
      startNew();                       // 服务器已清除草稿文件
    } else {
      toast(`记录 #${r.index + 1} 的修改已写回 .rec`);
      renderBrowse();
      updateEntryUI();
    }
  } catch (e) {
    toast(e.message, true);
    if (e.j && e.j.errs && e.j.errs.length) {
      const e0 = e.j.errs[0];
      setBad(inputs[e0.name], e0.msg); beep();
      alertMsg('服务器校验未通过', esc(`${e0.name} ${e0.text || ''}：${e0.msg}`), inputs[e0.name]);
    }
  }
}

$('#btn-save').onclick = save;
document.addEventListener('keydown', e => {
  if (modalOpen) return;
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && P && !$('#panel-entry').hidden) {
    e.preventDefault(); save();
  }
});

/* ---------------- 浏览 ---------------- */

/* 浏览 = Excel 式全字段表格：全部记录 × 全部字段，表头行/行号列冻结，隔行底纹 */
function renderBrowse() {
  const q = $('#filter').value.trim().toLowerCase();
  const idxs = P.records
    .map((r, i) => ({r, i}))
    .filter(x => !q || x.r.join('\u0001').toLowerCase().includes(q))
    .slice(0, 300);
  // 表头：字段名（同录入页题号芯片）+ 中文题目（同导出 Excel 的前两行）
  $('#rec-head').innerHTML = '<th class="g-idx">#</th>' +
    P.fields.map(f => `<th data-fname="${esc(f.name)}" data-ftext="${esc(f.text || '')}">${esc(f.name)}</th>`).join('');
  // 列头悬停 → 自绘浮层显示题目全文（原生 title 在部分内嵌浏览器中不显示）
  $('#rec-head').querySelectorAll('th[data-fname]').forEach(th => {
    const f = {name: th.dataset.fname, text: th.dataset.ftext};
    const show = ev => { if (f.text) showTip(`<b>${esc(f.name)}</b>${esc(' ' + f.text)}`, ev); };
    th.onmouseenter = show;
    th.onmousemove = show;
    th.onmouseleave = hideTip;
  });
  $('#rec-body').innerHTML = idxs.map(x =>
    `<tr data-i="${x.i}"><td class="g-idx">${x.i + 1}</td>` +
    P.fields.map((f, k) =>
      `<td${x.r[k] ? '' : ' class="g-empty"'}>${esc(x.r[k])}</td>`).join('') +
    `</tr>`).join('');
  $('#browse-count').textContent =
    `共 ${P.records.length} 条 · 显示前 ${idxs.length} 条（双击记录进入编辑）` +
    (idxs.length >= 300 ? ' · 最多显示 300 条，可用过滤缩小范围' : '');
  $('#rec-body').querySelectorAll('tr[data-i]').forEach(tr => {
    tr.ondblclick = () => viewRecord(+tr.dataset.i);
  });
}
/* 历史数据规范化：先检查报告，用户确认后执行（有问题保持原值并报告） */
$('#btn-normalize').onclick = async () => {
  try {
    const rep = await api('/api/normalize', {path: P.path, check: true});
    if (!rep.nchanges && !rep.nproblems) { toast('历史数据均已符合字段定义，无需规范化'); return; }
    const chg = rep.changes.slice(0, 8).map(c =>
      `• 记录#${c.index} ${c.name}: ${esc(c.old)} → ${esc(c.new)}`).join('\n');
    const prb = rep.problems.slice(0, 8).map(p =>
      `• 记录#${p.index} ${p.name} = ${esc(p.value)}（${esc(p.msg)}，将保持原值）`).join('\n');
    const html = (rep.nchanges ? `需规范化 ${rep.nchanges} 处：\n${chg}${rep.nchanges > 8 ? '\n…' : ''}` : '') +
      (rep.nproblems ? `\n\n⚠ 无法自动处理 ${rep.nproblems} 处（保持原值，不破坏数据）：\n${prb}${rep.nproblems > 8 ? '\n…' : ''}` : '');
    const ok = await showModal({
      title: rep.nchanges ? `检查完成：需规范化 ${rep.nchanges} 处` : '检查完成：无可规范化项',
      html: esc(html).replace(/\n/g, '<br>') || '—',
      buttons: rep.nchanges
        ? [{label: '执行规范化 (Enter)', value: true}, {label: '取消 (Esc)', value: false}]
        : [{label: '知道了 (Enter)', value: false}],
      danger: rep.nchanges > 0,
    });
    if (!ok || !rep.nchanges) return;
    const r = await api('/api/normalize', {path: P.path, apply: true});
    P = await api('/api/project');          // 重新拉取规范化后的数据
    toast(`规范化完成：已处理 ${r.applied} 处${r.problems ? `，${r.problems} 处问题保持原值` : ''}`, false, 8000);
    renderBrowse();
  } catch (e) { toast(e.message, true); }
};
$('#filter').addEventListener('input', () => renderBrowse());

/* ---------------- 导出：EpiData 式 text 导出对话框 ---------------- */

let expDir = '';            // 导出文件夹（默认 .rec 所在目录）
function defaultExportName() {
  return P ? P.path.split('/').pop().replace(/\.rec$/i, '') + '.txt' : 'export.txt';
}

function renderExportDialog() {
  if (!P) return;
  expDir = P.path.split('/').slice(0, -1).join('/');
  $('#exp-name').value = defaultExportName();
  $('#exp-dirinfo').textContent = '导出到文件夹：' + expDir;
  $('#exp-from').value = 1;
  $('#exp-to').value = P.records.length;
  $('#exp-to').max = P.records.length;
  $('#exp-filter').value = '';
  $('#exp-filter').disabled = true;
  $('#exp-usefilter').checked = false;
  $('#exp-result').textContent = '';
  $$('input[name=exp-fmt]').forEach(r => { r.checked = false; });   // 格式不默认，需先选择
  $('#exp-enc').value = 'gbk';
  $('#exp-enc').disabled = false;
  ['exp-quote', 'exp-header', 'exp-sep'].forEach(id => { $('#' + id).disabled = false; });
  expAnchor = '';
  const box = $('#exp-fields');
  box.innerHTML = '';
  const frag = document.createDocumentFragment();
  P.fields.forEach(f => {
    const lab = document.createElement('label');
    lab.dataset.name = f.name;
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = true; cb.dataset.name = f.name;
    lab.append(cb, document.createTextNode(f.name));
    frag.append(lab);
  });
  box.append(frag);
  // 点选某个字段 → 设为锚点（配合「此字段及之前/之后」按钮）
  box.addEventListener('change', e => {
    if (e.target.dataset && e.target.dataset.name) {
      expAnchor = e.target.dataset.name;
      box.querySelectorAll('label').forEach(l =>
        l.classList.toggle('anchor', l.dataset.name === expAnchor));
    }
  });
  box.addEventListener('mouseover', e => {
    const lab = e.target.closest('label[data-name]');
    if (!lab) return;
    const f = fieldsByName[lab.dataset.name];
    $('#exp-fieldinfo').textContent = f ? `${f.name}: ${f.text || ''}` : '';
  });
  $('#exp-fieldinfo').textContent = `${P.fields[0].name}: ${P.fields[0].text || ''}`;
}

function exportSelectedFields() {
  return [...$('#exp-fields').querySelectorAll('input:checked')].map(c => c.dataset.name);
}

$('#btn-exp-all').onclick = () => {
  $('#exp-fields').querySelectorAll('input[type=checkbox]').forEach(c => { c.checked = true; });
};
$('#btn-exp-none').onclick = () => {
  $('#exp-fields').querySelectorAll('input[type=checkbox]').forEach(c => { c.checked = false; });
};
/* 锚点范围选择：基准字段 = 最近一次点选的字段 */
let expAnchor = '';
function selectAnchorRange(before) {
  if (!expAnchor || !order.includes(expAnchor)) {
    toast('请先在字段列表中点选一个字段作为基准', true);
    return;
  }
  const idx = order.indexOf(expAnchor);
  $('#exp-fields').querySelectorAll('input[type=checkbox]').forEach(c => {
    const i = order.indexOf(c.dataset.name);
    c.checked = before ? i <= idx : i >= idx;
  });
  const lab = $(`#exp-fields label[data-name="${expAnchor}"]`);
  if (lab) lab.scrollIntoView({behavior: 'smooth', block: 'nearest'});
}
$('#btn-exp-before').onclick = () => selectAnchorRange(true);
$('#btn-exp-after').onclick = () => selectAnchorRange(false);
$('#exp-usefilter').onchange = e => { $('#exp-filter').disabled = !e.target.checked; };

$('#btn-exp-dir').onclick = async () => {
  try {
    const r = await api('/api/pickdir', {});
    if (r.dir) {
      expDir = r.dir;
      $('#exp-dirinfo').textContent = '导出到文件夹：' + expDir;
    } else if (r.error) toast(r.error, true);
  } catch (e) { toast(e.message, true); }
};

/* 三种格式共用 记录范围/字段选择/编码；文本括号与分割符仅作用于 text */
const EXP_EXT = {txt: '.txt', xlsx: '.xlsx', csv: '.csv'};

function adjustExportExt(path, fmt) {
  return path.replace(/\.(txt|xlsx|csv)$/i, '') + EXP_EXT[fmt];
}

async function exportDialogAs(fmt) {
  const name = adjustExportExt($('#exp-name').value.trim(), fmt);
  if (!name) { toast('请填写文件名', true); return; }
  $('#exp-name').value = name;
  if (!expDir) { toast('请先选择导出文件夹', true); return; }
  const outPath = expDir.replace(/\/?$/, '/') + name;
  const fields = exportSelectedFields();
  if (!fields.length) { toast('请至少选择一个字段', true); return; }
  const useRange = $$('input[name=exp-range]').find(r => r.checked).value === 'range';
  const from = useRange ? (+$('#exp-from').value || 1) : 1;
  const to = useRange ? (+$('#exp-to').value || P.records.length) : P.records.length;
  const payload = {
    path: P.path, format: fmt, out_path: outPath, fields, from, to,
    encoding: $('#exp-enc').value,
    quote: $('#exp-quote').checked, sep: $('#exp-sep').value,
    header: $('#exp-header').checked,
    usefilter: $('#exp-usefilter').checked, filter: $('#exp-filter').value.trim(),
  };
  try {
    const r = await api('/api/export-text', payload);
    const ts = new Date().toTimeString().slice(0, 8);
    const msg = `[${ts}] 已导出 ${fmt.toUpperCase()}：${r.records} 条 × ${r.fields} 字段 → ${r.path}`;
    $('#exp-result').textContent = msg;
    toast('导出成功 ' + msg, false, 12000);
  } catch (e) { toast(e.message, true, 8000); }
}

// 先选格式：切换时自动调整扩展名/编码默认值，文本专属选项仅 text 可用
$$('input[name=exp-fmt]').forEach(r => {
  r.onchange = () => {
    const fmt = r.value;
    $('#exp-name').value = adjustExportExt($('#exp-name').value.trim(), fmt);
    $('#exp-enc').value = fmt === 'csv' ? 'utf8bom' : 'gbk';
    $('#exp-enc').disabled = fmt === 'xlsx';
    ['exp-quote', 'exp-header', 'exp-sep'].forEach(id => { $('#' + id).disabled = fmt !== 'txt'; });
  };
});

$('#btn-exp-ok').onclick = async () => {
  const sel = $$('input[name=exp-fmt]').find(r => r.checked);
  if (!sel) { toast('请先选择导出格式', true); return; }
  await exportDialogAs(sel.value);
};

/* ---------------- 初始化 ---------------- */

(async () => {
  try {
    const st = await api('/api/state');
    if (st.last_dir) {
      $('#dir-input').value = st.last_dir;
      scan();
    }
  } catch (e) { /* 忽略 */ }
})();
