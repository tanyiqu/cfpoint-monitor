/* =====================================================================
   CF 点监控 · 前端逻辑（原生 JS，无外部依赖）
   ===================================================================== */
(function () {
  'use strict';

  /* ------------------------------------------------------------ 工具 */
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.prototype.slice.call((root || document).querySelectorAll(sel));

  const COIN_LABELS = {
    cfPoint: 'CF 点',
    cfCoin: '火线币',
    gamePoint: 'GP 点',
    redGem: '红宝石',
    blueGem: '蓝宝石',
    yellowGem: '黄宝石'
  };

  const STATUS_MAP = {
    ok: { text: '正常', cls: 'ok' },
    expired: { text: 'Cookie 失效', cls: 'bad' },
    error: { text: '采集异常', cls: 'bad' },
    bind: { text: '未绑定大区', cls: 'warn' },
    unknown: { text: '待检测', cls: 'mute' },
    disabled: { text: '已停用', cls: 'mute' },
    busy: { text: '查询中', cls: 'info' }
  };

  const EVENT_META = {
    change: { icon: '⇅', label: 'CF点变动' },
    digest: { icon: '⏱', label: '定时汇总' },
    expired: { icon: '!', label: 'Cookie 失效' },
    recover: { icon: '✓', label: '恢复' },
    error: { icon: '!', label: '采集异常' },
    info: { icon: 'i', label: '系统' },
    test: { icon: '✉', label: '测试' }
  };

  /* 事件类型 -> 胶囊配色，日志表格与类型筛选共用 */
  const KIND_PILL = {
    change: 'info',
    digest: 'info',
    expired: 'bad',
    error: 'bad',
    recover: 'ok'
  };

  const PUSH_META = {
    success: { text: '已推送', cls: 'ok' },
    failed: { text: '推送失败', cls: 'bad' },
    skipped: { text: '未推送', cls: 'mute' },
    none: { text: '—', cls: 'mute' }
  };

  const state = {
    accounts: [],
    settings: {},
    summary: {},
    monitor: {},
    view: 'dashboard',
    fetchedAt: 0,
    settingsDirty: false,
    webhookDirty: false,
    timer: null
  };

  function esc(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fmtNum(n) {
    if (n === null || n === undefined || n === '') return '—';
    const num = Number(n);
    if (!isFinite(num)) return esc(n);
    return num.toLocaleString('zh-CN');
  }

  function parseTime(str) {
    if (!str) return null;
    const d = new Date(String(str).replace(' ', 'T'));
    return isNaN(d.getTime()) ? null : d;
  }

  function timeAgo(str) {
    const d = parseTime(str);
    if (!d) return '—';
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    return Math.floor(diff / 86400) + ' 天前';
  }

  function shortTime(str) {
    const d = parseTime(str);
    if (!d) return '—';
    const now = new Date();
    const pad = (v) => String(v).padStart(2, '0');
    const hm = pad(d.getHours()) + ':' + pad(d.getMinutes());
    if (d.toDateString() === now.toDateString()) return hm;
    return (d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + hm;
  }

  function durText(sec) {
    if (sec === null || sec === undefined) return '—';
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return sec + ' 秒';
    if (sec < 3600) return Math.round(sec / 60) + ' 分钟';
    const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
    return m ? h + ' 小时 ' + m + ' 分' : h + ' 小时';
  }

  function intervalText(sec) {
    sec = Number(sec) || 300;
    if (sec < 60) return sec + ' 秒';
    if (sec < 3600) return (Math.round(sec / 6) / 10) + ' 分钟';
    return (Math.round(sec / 360) / 10) + ' 小时';
  }

  /* 把 daily_times（"06:00,18:00"）切成数组，兼容中文逗号与空格 */
  function splitDailyTimes(text) {
    return String(text || '')
      .split(/[,;，；\s]+/)
      .map((s) => s.trim())
      .filter(Boolean);
  }

  /* 采集模式的一句话说明，总览与设置页共用 */
  function scheduleLabel(monitor) {
    const m = monitor || state.monitor || {};
    if (m.schedule_mode === 'daily') {
      const times = splitDailyTimes(m.daily_times).join(' / ') || '—';
      return '每天 ' + times;
    }
    return '每 ' + intervalText(m.poll_interval) + '采集';
  }

  /* ------------------------------------------------------------ 请求 */
  async function api(path, options) {
    const opt = Object.assign({ headers: {} }, options || {});
    if (opt.body && typeof opt.body !== 'string') {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(opt.body);
    }
    let resp, data;
    try {
      resp = await fetch(path, opt);
    } catch (e) {
      return { ok: false, message: '网络错误：' + e.message };
    }
    try {
      data = await resp.json();
    } catch (e) {
      data = { ok: false, message: '服务端返回异常（HTTP ' + resp.status + '）' };
    }
    if (!resp.ok && data && data.message === undefined) data.message = 'HTTP ' + resp.status;
    return data;
  }

  /* ------------------------------------------------------------ 提示 */
  function toast(message, type, ms) {
    const el = document.createElement('div');
    el.className = 'toast ' + (type || '');
    el.innerHTML = '<span>' + esc(message) + '</span><span class="x">×</span>';
    $('#toasts').appendChild(el);
    const close = () => { el.remove(); };
    el.querySelector('.x').onclick = close;
    setTimeout(close, ms || 3600);
  }

  /* ------------------------------------------------------------ 确认框 */
  function confirmAction(opts) {
    return new Promise((resolve) => {
      const mask = document.createElement('div');
      mask.className = 'modal-mask';
      mask.innerHTML =
        '<div class="modal" style="max-width:420px">' +
          '<div class="modal-head"><div><h3>' + esc(opts.title) + '</h3>' +
          (opts.desc ? '<p>' + esc(opts.desc) + '</p>' : '') + '</div></div>' +
          '<div class="modal-foot" style="border-top:0;background:#fff;justify-content:flex-end">' +
            '<button class="btn" data-act="cancel">取消</button>' +
            '<button class="btn ' + (opts.danger ? 'btn-danger' : 'btn-primary') + '" data-act="ok">' +
              esc(opts.okText || '确定') + '</button>' +
          '</div>' +
        '</div>';
      document.body.appendChild(mask);
      const done = (v) => { mask.remove(); resolve(v); };
      mask.addEventListener('click', (e) => {
        if (e.target === mask || e.target.dataset.act === 'cancel') done(false);
        if (e.target.dataset.act === 'ok') done(true);
      });
    });
  }

  /* ------------------------------------------------------------ 图形 */
  function sparkline(values, color) {
    if (!values || values.length < 2) {
      return '<span class="muted small" style="font-size:11px">暂无走势</span>';
    }
    const w = 108, h = 32, pad = 3;
    const min = Math.min.apply(null, values);
    const max = Math.max.apply(null, values);
    const flat = max === min;
    const span = (max - min) || 1;
    const step = (w - pad * 2) / (values.length - 1);
    const pts = values.map((v, i) => [
      pad + i * step,
      flat ? h / 2 : h - pad - ((v - min) / span) * (h - pad * 2)
    ]);
    const d = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
    const up = values[values.length - 1] >= values[0];
    const c = color || (up ? '#e5484d' : '#12a150');
    const last = pts[pts.length - 1];
    return '<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" style="display:block">' +
      '<path d="' + d + '" fill="none" stroke="' + c + '" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>' +
      '<circle cx="' + last[0].toFixed(1) + '" cy="' + last[1].toFixed(1) + '" r="2.4" fill="' + c + '"/>' +
      '</svg>';
  }

  function lineChart(history) {
    if (!history || history.length < 2) {
      return '<div class="muted small" style="padding:26px 0;text-align:center">历史数据不足，多采集几次即可看到走势</div>';
    }
    const values = history.map((h) => Number(h.cfpoint) || 0);
    const w = 640, h = 130, padX = 8, padY = 14;
    const min = Math.min.apply(null, values);
    const max = Math.max.apply(null, values);
    const flat = max === min;
    const span = (max - min) || Math.max(1, max * 0.02);
    const step = (w - padX * 2) / (values.length - 1);
    const x = (i) => padX + i * step;
    const y = (v) => (flat ? h / 2 : h - padY - ((v - min) / span) * (h - padY * 2));
    const d = values.map((v, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ',' + y(v).toFixed(1)).join(' ');
    const area = d + ' L' + x(values.length - 1).toFixed(1) + ',' + h + ' L' + padX + ',' + h + ' Z';
    const up = values[values.length - 1] >= values[0];
    const c = up ? '#e5484d' : '#12a150';
    const gid = 'g' + Math.random().toString(36).slice(2, 8);
    let grid = '';
    for (let i = 0; i <= 3; i++) {
      const gy = padY + (i * (h - padY * 2)) / 3;
      grid += '<line x1="0" y1="' + gy.toFixed(1) + '" x2="' + w + '" y2="' + gy.toFixed(1) +
        '" stroke="#eef0f3" stroke-width="1" vector-effect="non-scaling-stroke"/>';
    }
    return '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">' +
      '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0%" stop-color="' + c + '" stop-opacity="0.16"/>' +
      '<stop offset="100%" stop-color="' + c + '" stop-opacity="0"/>' +
      '</linearGradient></defs>' + grid +
      '<path d="' + area + '" fill="url(#' + gid + ')" stroke="none"/>' +
      '<path d="' + d + '" fill="none" stroke="' + c + '" stroke-width="2" ' +
      'vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>' +
      '<circle cx="' + x(values.length - 1).toFixed(1) + '" cy="' + y(values[values.length - 1]).toFixed(1) +
      '" r="3" fill="' + c + '" vector-effect="non-scaling-stroke"/></svg>' +
      '<div class="cap" style="display:flex;justify-content:space-between;font-size:11px;color:#8b95a5;margin-top:2px">' +
      '<span>最低 ' + fmtNum(min) + '</span><span>' + esc(shortTime(history[0].created_at)) +
      ' → ' + esc(shortTime(history[history.length - 1].created_at)) + '</span><span>最高 ' + fmtNum(max) + '</span></div>';
  }

  /* ------------------------------------------------------------ 状态渲染 */
  function statusPill(status, enabled) {
    if (enabled === false) return '<span class="pill mute">已停用</span>';
    const meta = STATUS_MAP[status] || STATUS_MAP.unknown;
    return '<span class="pill ' + meta.cls + '">' + meta.text + '</span>';
  }

  function accountValue(acc) {
    if (acc.cfpoint === null || acc.cfpoint === undefined) return '<span class="muted">—</span>';
    return fmtNum(acc.cfpoint);
  }

  function cookieAge(acc) {
    const h = acc.cookie_age_hours;
    if (h === null || h === undefined) return '—';
    if (h < 24) return Math.round(h) + ' 小时';
    return Math.round(h / 24) + ' 天';
  }

  /* ------------------------------------------------------------ 视图切换 */
  const VIEW_META = {
    dashboard: { title: '总览', desc: '实时监控账号 CF 点变化，变动时通过 Webhook 推送' },
    accounts: { title: '账号管理', desc: '添加账号、粘贴 Cookie、随时实时预览' },
    webhook: { title: 'Webhook', desc: '配置推送目标与触发场景' },
    settings: { title: '采集设置', desc: '选择实时 / 定时采集模式，配置频率与提醒方式' },
    logs: { title: '事件日志', desc: '每一次变动与推送结果都有记录' }
  };

  function switchView(view, fromHash) {
    if (!VIEW_META[view]) view = 'dashboard';
    state.view = view;
    $$('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
    $$('.view').forEach((v) => v.classList.toggle('hidden', v.id !== 'view-' + view));
    const meta = VIEW_META[view];
    $('#pageTitle').textContent = meta.title;
    $('#pageDesc').textContent = meta.desc;
    if (!fromHash) history.replaceState(null, '', '#/' + view);
    if (view === 'logs') loadLogs();
    if (view === 'webhook') renderSample();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  /* 支持 #/accounts、#/accounts/3（直接打开某账号实时预览）等深链接 */
  function applyHash() {
    const parts = (location.hash || '').replace(/^#\/?/, '').split('/').filter(Boolean);
    switchView(parts[0] || 'dashboard', true);
    if (parts[0] === 'accounts' && parts[1]) {
      const exists = state.accounts.some((a) => String(a.id) === String(parts[1]));
      if (exists) openPreview(parts[1]);
    }
  }

  /* ------------------------------------------------------------ 数据加载 */
  async function loadState(silent) {
    const data = await api('/api/state');
    if (!data.ok) {
      if (!silent) toast(data.message || '加载失败', 'bad');
      $('#sideStatus').textContent = '未连接后端';
      $('#sideDot').classList.add('off');
      return;
    }
    state.accounts = data.accounts || [];
    state.settings = data.settings || {};
    state.summary = data.summary || {};
    state.monitor = data.monitor || {};
    state.fetchedAt = Date.now();

    if (!state.settingsDirty || !state.webhookDirty) fillSettingsForm();
    renderDashboard();
    renderAccounts();
    renderRunner();
    renderWebhookVisibility();

    const bad = state.accounts.filter((a) => a.enabled && (a.status === 'expired' || a.status === 'error' || a.status === 'bind')).length;
    $('#navBadge').classList.toggle('hidden', bad === 0);
  }

  /* ------------------------------------------------------------ 总览 */
  function renderDashboard() {
    const s = state.summary || {};
    $('#statAccounts').textContent = fmtNum(s.total_accounts || 0);
    $('#statAccountsSub').textContent = '其中 ' + (s.disabled || 0) + ' 个已停用';
    $('#statPoints').textContent = fmtNum(s.total_cfpoint || 0);
    $('#statOk').textContent = fmtNum(s.ok || 0);
    $('#statBad').textContent = fmtNum(s.bad || 0);

    const grid = $('#dashCards');
    if (!state.accounts.length) {
      grid.innerHTML = '<div class="empty" style="grid-column:1/-1">' +
        '<div class="big">还没有添加账号</div>' +
        '<div>点击右上角「添加账号」，粘贴浏览器里复制的 Cookie 即可开始监控</div>' +
        '<div style="margin-top:14px"><button class="btn btn-primary" data-act="add">添加第一个账号</button></div></div>';
      return;
    }
    grid.innerHTML = state.accounts.map((a) => {
      const delta = a.spark && a.spark.length > 1 ? a.spark[a.spark.length - 1] - a.spark[a.spark.length - 2] : 0;
      const deltaHtml = delta ? '<span class="small" style="color:' + (delta > 0 ? '#e5484d' : '#12a150') + '">' +
        (delta > 0 ? '+' : '') + fmtNum(delta) + '</span>' : '';
      return '<div class="acct-card" data-preview="' + a.id + '">' +
        '<div class="top"><div style="min-width:0"><div class="name">' + esc(a.name) + '</div>' +
        '<div class="role">' + (a.role_name ? esc(a.role_name) : '角色信息待获取') +
        (a.level ? ' · Lv.' + esc(a.level) : '') + '</div></div>' +
        statusPill(a.status, a.enabled) + '</div>' +
        '<div class="mid"><div><div class="num">' + accountValue(a) + '<small>CF 点</small></div>' +
        '<div style="height:14px">' + deltaHtml + '</div></div>' +
        '<div>' + sparkline(a.spark) + '</div></div>' +
        '<div class="foot"><span>更新于 ' + esc(timeAgo(a.last_check)) + '</span>' +
        '<span>Cookie ' + esc(cookieAge(a)) + '</span></div></div>';
    }).join('');

    grid.querySelectorAll('[data-act="add"]').forEach((b) => b.onclick = () => openAccountModal(null));
  }

  function renderRunner() {
    const m = state.monitor || {};
    const paused = m.paused;
    $('#runDot').classList.toggle('off', !m.running || paused);
    $('#runText').textContent = !m.running ? '后台未运行' : (paused ? '采集已暂停' : '后台调度中');
    $('#runInterval').textContent = scheduleLabel(m);
    $('#runActivity').textContent = m.last_activity || '';
    $('#btnPause').innerHTML = paused
      ? '<svg><use href="#i-play"/></svg><span>恢复采集</span>'
      : '<svg><use href="#i-pause"/></svg><span>暂停采集</span>';
    $('#btnPause').dataset.paused = paused ? '1' : '0';
    $('#sideDot').classList.toggle('off', !m.running || paused);
    $('#sideStatus').textContent = !m.running ? '后台未运行' : (paused ? '已暂停' : '运行中 · 监控 ' + (state.summary.ok || 0) + ' 个账号');
    tickCountdown();
  }

  function tickCountdown() {
    const m = state.monitor || {};
    if (!m.running || m.paused) {
      $('#runNext').textContent = '—';
      $('#runKeep').textContent = '—';
      $('#sideNext').textContent = '—';
      $('#sideKeep').textContent = '—';
      return;
    }
    const elapsed = (Date.now() - state.fetchedAt) / 1000;
    const nextPoll = m.next_poll_in == null ? null : m.next_poll_in - elapsed;
    const nextKeep = m.next_keep_in == null ? null : m.next_keep_in - elapsed;
    const t1 = nextPoll == null ? '—' : (nextPoll <= 0 ? '即将开始' : durText(nextPoll));
    const t2 = !m.keepalive_enabled ? '已关闭' : (nextKeep == null ? '—' : (nextKeep <= 0 ? '即将开始' : durText(nextKeep)));
    $('#runNext').textContent = t1;
    $('#runKeep').textContent = t2;
    $('#sideNext').textContent = t1;
    $('#sideKeep').textContent = t2;
  }

  /* ------------------------------------------------------------ 账号表 */
  function renderAccounts() {
    const rows = $('#acctRows');
    $('#acctCount').textContent = state.accounts.length ? '共 ' + state.accounts.length + ' 个' : '';
    if (!state.accounts.length) {
      rows.innerHTML = '<tr><td colspan="7"><div class="empty"><div class="big">暂无账号</div>' +
        '<div>添加账号后即可自动采集 CF 点</div></div></td></tr>';
      return;
    }
    rows.innerHTML = state.accounts.map((a) => {
      const msg = a.status_msg && a.status !== 'ok' ? '<div class="cell-sub" style="color:#c05555;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(a.status_msg) + '">' + esc(a.status_msg) + '</div>' : '';
      return '<tr>' +
        '<td><div class="cell-title">' + esc(a.name) + '</div><div class="cell-sub">' +
        (a.remark ? esc(a.remark) : (a.openid_tail ? 'openid …' + esc(a.openid_tail) : '—')) + '</div></td>' +
        '<td><div>' + (a.role_name ? esc(a.role_name) : '<span class="muted">—</span>') + '</div>' +
        '<div class="cell-sub">' + (a.level ? 'Lv.' + esc(a.level) : '') +
        (a.play_cnt ? (a.level ? ' · ' : '') + fmtNum(a.play_cnt) + ' 场' : '') + '</div></td>' +
        '<td class="num"><b>' + accountValue(a) + '</b></td>' +
        '<td>' + statusPill(a.status, a.enabled) + msg + '</td>' +
        '<td class="small muted">' + esc(cookieAge(a)) + '</td>' +
        '<td class="small muted">' + esc(timeAgo(a.last_check)) + '</td>' +
        '<td><div class="row-actions">' +
          '<button class="btn btn-sm btn-icon btn-ghost" data-act="preview" data-id="' + a.id + '" title="实时预览"><svg><use href="#i-eye"/></svg></button>' +
          '<button class="btn btn-sm btn-icon btn-ghost" data-act="refresh" data-id="' + a.id + '" title="立即采集"><svg><use href="#i-refresh"/></svg></button>' +
          '<button class="btn btn-sm btn-icon btn-ghost" data-act="keepalive" data-id="' + a.id + '" title="手动保活"><svg><use href="#i-heart"/></svg></button>' +
          '<button class="btn btn-sm btn-icon btn-ghost" data-act="edit" data-id="' + a.id + '" title="编辑"><svg><use href="#i-edit"/></svg></button>' +
          '<button class="btn btn-sm btn-icon btn-ghost" data-act="delete" data-id="' + a.id + '" title="删除"><svg><use href="#i-trash"/></svg></button>' +
        '</div></td></tr>';
    }).join('');
  }

  /* ------------------------------------------------------------ 设置表单 */
  function fillSettingsForm() {
    $$('[data-setting]').forEach((el) => {
      // 用户正在编辑的表单不回填，避免自动刷新把输入冲掉
      if (el.closest('#view-webhook') && state.webhookDirty) return;
      if (el.closest('#view-settings') && state.settingsDirty) return;
      const key = el.dataset.setting;
      if (!(key in state.settings)) return;
      const val = state.settings[key];
      if (el.type === 'checkbox') {
        el.checked = val === '1' || val === true;
      } else if (key === 'jitter') {
        const pct = Math.round((parseFloat(val) || 0) * 100);
        el.value = pct;
        $('#jitterVal').textContent = pct + '%';
      } else {
        el.value = val;
      }
    });
    syncIntervalChips();
    syncScheduleChips();
    renderWebhookVisibility();
  }

  function collectSettings() {
    const out = {};
    $$('[data-setting]').forEach((el) => {
      const key = el.dataset.setting;
      if (el.type === 'checkbox') out[key] = el.checked ? '1' : '0';
      else if (key === 'jitter') out[key] = (Number(el.value) / 100).toFixed(2);
      else out[key] = el.value;
    });
    return out;
  }

  function syncIntervalChips() {
    const cur = Number($('#intervalInput').value) || 0;
    $$('#intervalChips .chip').forEach((c) => c.classList.toggle('active', Number(c.dataset.interval) === cur));
  }

  /* ------------------------------------------------------------ 采集模式
     模式与提醒方式都用 chip 交互，真正提交的值放在隐藏域里，
     这样 collectSettings / fillSettingsForm 的通用逻辑完全不用改。 */
  const MODE_HINT = {
    interval: '按固定间隔轮询全部账号，只在 CF 点发生变化时推送，适合盯实时变动',
    daily: '每天在设定的时间点各采集一轮，适合只关心日变化、想减少请求量的场景'
  };

  const DAILY_NOTIFY_HINT = {
    change: '只有 CF 点发生变化时才逐账号推送，没变化就不打扰',
    always: '每轮采集结束后发一条汇总，包含全部账号的当前 CF 点与变动，可在 Webhook 页单独开关'
  };

  function syncScheduleChips() {
    const mode = $('#scheduleMode').value === 'daily' ? 'daily' : 'interval';
    $$('#modeChips .chip').forEach((c) => c.classList.toggle('active', c.dataset.mode === mode));
    $('#modeIntervalBox').classList.toggle('hidden', mode !== 'interval');
    $('#modeDailyBox').classList.toggle('hidden', mode !== 'daily');
    $('#modeHint').textContent = MODE_HINT[mode];

    const notifyMode = $('#dailyNotifyMode').value === 'always' ? 'always' : 'change';
    $$('#dailyNotifyChips .chip').forEach((c) => c.classList.toggle('active', c.dataset.notify === notifyMode));
    $('#dailyNotifyHint').textContent = DAILY_NOTIFY_HINT[notifyMode];

    // 面板右上角的一句话预览，用表单里的实时值，保存前也能看到效果
    $('#modeSummary').textContent = mode === 'daily'
      ? '每天 ' + (splitDailyTimes($('#dailyTimesInput').value).join(' / ') || '—')
      : '每 ' + intervalText($('#intervalInput').value) + '采集';
    syncDailyChips();
  }

  function syncDailyChips() {
    const list = splitDailyTimes($('#dailyTimesInput').value);
    $$('#dailyChips .chip').forEach((c) => c.classList.toggle('active', list.indexOf(c.dataset.time) >= 0));
  }

  function renderWebhookVisibility() {
    const type = $('#webhookType').value;
    $('#secretField').classList.toggle('hidden', type !== 'dingtalk');
    $('#tplField').classList.toggle('hidden', type !== 'custom');
    const hints = {
      generic: '以 JSON 形式 POST 到你配置的地址，字段结构见下方示例',
      wecom: '企业微信群 → 群设置 → 群机器人 → 添加 → 复制 Webhook 地址',
      dingtalk: '钉钉群 → 智能群助手 → 添加机器人 → 自定义 → 复制 Webhook',
      feishu: '飞书群 → 设置 → 群机器人 → 添加 → 自定义机器人',
      serverchan: '填写 https://sctapi.ftqq.com/SCTxxxxxx.send（Server酱密钥在地址里）',
      bark: '填写 https://api.day.app/你的Key，iOS 端会收到通知',
      custom: '完全自定义请求体，任何 HTTP 服务都能对接'
    };
    $('#webhookTypeHint').textContent = hints[type] || '';
    renderSample();
  }

  function samplePayload(type) {
    const message = '【CF点变动】主号\n角色：孤影\n3,956 → 4,000（+44）\n时间：2026-11-03 11:20:00';
    const title = 'CF点变动：主号 3,956 → 4,000';
    if (type === 'wecom' || type === 'dingtalk') {
      return JSON.stringify({ msgtype: 'text', text: { content: message } }, null, 2);
    }
    if (type === 'feishu') {
      return JSON.stringify({ msg_type: 'text', content: { text: message } }, null, 2);
    }
    if (type === 'serverchan') {
      return JSON.stringify({ title: title, desp: message }, null, 2);
    }
    if (type === 'bark') {
      return JSON.stringify({ title: title, body: message, group: 'CF点监控' }, null, 2);
    }
    if (type === 'custom') {
      const tpl = ($('[data-setting="webhook_body_tpl"]') || {}).value || '';
      return tpl || '（在上方填写自定义模板后这里会显示渲染结果）';
    }
    return JSON.stringify({
      event: 'change',
      event_label: 'CF点变动',
      title: title,
      message: message,
      account: { id: 1, name: '主号', role: '孤影' },
      cfpoint: { before: 3956, after: 4000, delta: 44, current: 4000 },
      time: '2026-11-03 11:20:00',
      extra: '+44'
    }, null, 2);
  }

  function renderSample() {
    const el = $('#payloadSample');
    if (!el) return;
    el.textContent = samplePayload($('#webhookType').value);
  }

  /* ------------------------------------------------------------ 账号弹窗 */
  function openAccountModal(account) {
    const isEdit = !!account;
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.innerHTML =
      '<div class="modal wide">' +
        '<div class="modal-head"><div><h3>' + (isEdit ? '编辑账号' : '添加账号') + '</h3>' +
        '<p>' + (isEdit ? '更换 Cookie 后会自动验证并补齐角色信息' : '粘贴完整 Cookie，系统会自动解析 openid / access_token') + '</p></div>' +
        '<button class="close-x" data-act="close">×</button></div>' +
        '<div class="modal-body">' +
          '<div class="form-grid">' +
            '<div class="field"><label>账号名称</label>' +
              '<input type="text" id="fName" placeholder="留空则自动使用角色名" value="' + esc(isEdit ? account.name : '') + '">' +
              '<div class="hint">仅用于在后台区分，不影响查询</div></div>' +
            '<div class="field"><label>备注 <span class="muted small">（可选）</span></label>' +
              '<input type="text" id="fRemark" placeholder="例如：大号 / 小号 / 朋友号" value="' + esc(isEdit ? account.remark : '') + '"></div>' +
            '<div class="field full"><label>Cookie' + (isEdit ? ' <span class="muted small">（留空则不修改）</span>' : '') + '</label>' +
              '<textarea class="code" id="fCookie" rows="6" placeholder="ptui_loginuin=...; acctype=qc; openid=...; access_token=...; appid=...;"></textarea>' +
              '<div class="hint">' + (isEdit
                ? '如需更换登录态，粘贴新的完整 Cookie；不填则保持原样。<a href="#" data-act="showcookie">查看当前 Cookie</a>'
                : 'F12 → Network → 筛选 ide → 点开对 cf.ams.game.qq.com/ide/ 的 POST 请求 → Request Headers → 复制整行 Cookie') + '</div></div>' +
            (isEdit ? '<div class="field full"><label class="switch"><input type="checkbox" id="fEnabled"' +
              (account.enabled ? ' checked' : '') + '><span class="track"></span>' +
              '<span class="txt">启用该账号</span></label>' +
              '<div class="hint">停用后不再参与采集，历史数据保留</div></div>' : '') +
          '</div>' +
          '<div id="modalAlert"></div>' +
        '</div>' +
        '<div class="modal-foot">' +
          '<span class="small muted" id="modalStatus"></span>' +
          '<div class="spacer"></div>' +
          '<button class="btn" data-act="close">取消</button>' +
          '<button class="btn btn-primary" data-act="save">' + (isEdit ? '保存' : '添加并验证') + '</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(mask);

    const close = () => mask.remove();
    const alertBox = $('#modalAlert', mask);
    const statusEl = $('#modalStatus', mask);
    const saveBtn = $('[data-act="save"]', mask);

    mask.addEventListener('click', (e) => {
      const act = e.target.dataset.act;
      if (e.target === mask || act === 'close') return close();
      if (act === 'showcookie') {
        e.preventDefault();
        api('/api/accounts/' + account.id + '/cookie').then((d) => {
          if (d.ok) { $('#fCookie', mask).value = d.cookie; toast('已填入当前 Cookie'); }
          else toast(d.message || '读取失败', 'bad');
        });
      }
      if (act === 'save') submit(false);
    });

    async function submit(force) {
      const cookie = $('#fCookie', mask).value.trim();
      const name = $('#fName', mask).value.trim();
      const remark = $('#fRemark', mask).value.trim();
      if (!isEdit && !cookie) { alertBox.innerHTML = '<div class="alert warn">请先粘贴 Cookie</div>'; return; }
      if (isEdit && !cookie && !name && remark === account.remark) {
        // 仍允许提交启停状态
      }
      saveBtn.disabled = true;
      saveBtn.innerHTML = '<span class="spin"></span> 处理中…';
      statusEl.textContent = '正在验证 Cookie…';
      alertBox.innerHTML = '';

      let data;
      if (isEdit) {
        const payload = { remark: remark };
        if (name) payload.name = name;
        if (cookie) payload.cookie = cookie;
        const enabledEl = $('#fEnabled', mask);
        if (enabledEl) payload.enabled = enabledEl.checked;
        data = await api('/api/accounts/' + account.id, { method: 'PUT', body: payload });
      } else {
        data = await api('/api/accounts', { method: 'POST', body: { name, remark, cookie, force: !!force } });
      }

      saveBtn.disabled = false;
      saveBtn.textContent = isEdit ? '保存' : '添加并验证';
      statusEl.textContent = '';

      if (data.ok) {
        close();
        toast(isEdit ? '账号已更新' : '账号已添加', 'ok');
        loadState(true);
        return;
      }

      if (data.need_force) {
        alertBox.innerHTML = '<div class="alert">' + esc(data.message) + '</div>';
        const btn = document.createElement('button');
        btn.className = 'btn btn-sm btn-danger';
        btn.style.margin = '0 0 16px 20px';
        btn.textContent = '仍然保存（稍后自行更换 Cookie）';
        btn.onclick = () => submit(true);
        alertBox.appendChild(btn);
        return;
      }
      alertBox.innerHTML = '<div class="alert">' + esc(data.message || '保存失败') + '</div>';
    }

    setTimeout(() => { const el = $('#fCookie', mask); if (el && !isEdit) el.focus(); }, 60);
  }

  /* ------------------------------------------------------------ 预览弹窗 */
  async function openPreview(id) {
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.innerHTML =
      '<div class="modal wide">' +
        '<div class="modal-head"><div><h3>实时预览</h3><p>直接请求接口拉取最新数据，不写入采集历史</p></div>' +
        '<button class="close-x" data-act="close">×</button></div>' +
        '<div class="modal-body no-pad"><div class="empty"><span class="spin" style="border-color:#ddd;border-top-color:#f2701f;width:18px;height:18px"></span>' +
        '<div style="margin-top:10px">正在连接 CF 接口…</div></div></div>' +
        '<div class="modal-foot"><span class="small muted" id="pvNote"></span><div class="spacer"></div>' +
        '<button class="btn" data-act="close">关闭</button>' +
        '<button class="btn btn-primary" data-act="refresh">立即采集并入库</button></div>' +
      '</div>';
    document.body.appendChild(mask);
    const close = () => {
      mask.remove();
      if (/^#\/accounts\/\d+/.test(location.hash)) history.replaceState(null, '', '#/accounts');
    };
    mask.addEventListener('click', (e) => {
      if (e.target === mask || e.target.dataset.act === 'close') return close();
      if (e.target.dataset.act === 'refresh') {
        api('/api/accounts/' + id + '/refresh', { method: 'POST' }).then((d) => {
          if (d.ok) {
            toast(d.result && d.result.ok ? '已采集：' + fmtNum(d.result.cfpoint) : (d.result.message || '采集失败'),
              d.result && d.result.ok ? 'ok' : 'bad');
            loadState(true);
            close();
            openPreview(id);
          } else toast(d.message || '失败', 'bad');
        });
      }
    });

    const data = await api('/api/accounts/' + id + '/preview');
    const body = $('.modal-body', mask);
    const note = $('#pvNote', mask);
    if (!data.ok && !data.account) {
      body.innerHTML = '<div class="alert">' + esc(data.message || '预览失败') + '</div>';
      return;
    }
    const acc = data.account || {};
    note.textContent = data.duration != null ? '耗时 ' + data.duration + 's · ' + (data.fetched_at || '') : '';

    if (!data.ok) {
      const hints = {
        expired: '登录态已过期，请重新登录活动页并复制新的 Cookie，在「编辑」里更新。',
        bind: '该账号还没有绑定游戏大区与角色，请先在游戏内或活动页完成绑定。',
        cookie: 'Cookie 不完整，缺少 openid / access_token。',
        network: '网络异常或接口超时，稍后再试。',
        error: '接口返回了非预期内容，稍后再试。'
      };
      body.innerHTML =
        '<div class="preview-hero"><div><div class="label">实时查询失败</div>' +
        '<div class="big" style="font-size:22px;color:#c0393d">' + esc(acc.name || '账号') + '</div></div>' +
        statusPill(acc.status, acc.enabled) + '</div>' +
        '<div class="alert">' + esc(data.message) + '</div>' +
        (hints[data.kind] ? '<div class="alert info" style="margin-top:0">' + esc(hints[data.kind]) + '</div>' : '');
      return;
    }

    const coins = data.coins || {};
    const personal = data.personal || {};
    const diff = data.diff;
    let deltaHtml = '<div class="delta flat">与上次记录持平</div>';
    if (diff !== null && diff !== undefined) {
      if (diff > 0) deltaHtml = '<div class="delta up">较上次记录 +' + fmtNum(diff) + '</div>';
      else if (diff < 0) deltaHtml = '<div class="delta down">较上次记录 ' + fmtNum(diff) + '</div>';
    } else {
      deltaHtml = '<div class="delta flat">首次采集，暂无对比</div>';
    }

    const coinCells = ['cfPoint', 'cfCoin', 'gamePoint', 'redGem', 'blueGem', 'yellowGem']
      .filter((k) => coins[k] !== undefined)
      .map((k) => '<div class="cell' + (k === 'cfPoint' ? ' primary' : '') + '">' +
        '<div class="k">' + COIN_LABELS[k] + '</div>' +
        '<div class="v">' + fmtNum(coins[k]) + '</div></div>').join('');

    body.innerHTML =
      '<div class="preview-hero">' +
        '<div><div class="label">当前 CF 点</div>' +
        '<div class="big">' + fmtNum(data.cfpoint) + '<small>CF</small></div>' + deltaHtml + '</div>' +
        '<div style="text-align:right"><div style="font-weight:650;font-size:14px">' + esc(acc.name) + '</div>' +
        '<div class="small muted">' + esc(acc.openid_tail ? 'openid …' + acc.openid_tail : '') + '</div>' +
        '<div style="margin-top:6px">' + statusPill('ok', true) + '</div></div>' +
      '</div>' +
      '<div class="coin-grid">' + (coinCells || '<div class="cell"><div class="k">无数据</div></div>') + '</div>' +
      '<dl class="kv-list">' +
        '<dt>角色名</dt><dd>' + esc(personal.roleName || acc.role_name || '—') + '</dd>' +
        '<dt>军衔等级</dt><dd>' + esc(personal.lev || acc.level || '—') + '</dd>' +
        '<dt>注册时间</dt><dd>' + esc(personal.regDate || acc.reg_date || '—') + '</dd>' +
        '<dt>对局次数</dt><dd>' + (personal.playCnt != null ? fmtNum(personal.playCnt) + ' 场' : (acc.play_cnt ? fmtNum(acc.play_cnt) + ' 场' : '—')) + '</dd>' +
        '<dt>上次记录</dt><dd>' + fmtNum(acc.cfpoint) + ' · ' + esc(timeAgo(acc.last_check)) + '</dd>' +
        '<dt>Cookie 已用</dt><dd>' + esc(cookieAge(acc)) + '</dd>' +
      '</dl>' +
      '<div class="chart-box"><div class="cap"><span>CF 点历史走势</span><span>' +
      (data.history ? data.history.length : 0) + ' 个采样点</span></div>' + lineChart(data.history) + '</div>';
  }

  /* ------------------------------------------------------------ 日志 */
  async function loadLogs() {
    const kind = $('#logFilter').value;
    const rows = $('#logRows');
    rows.innerHTML = '<tr><td colspan="4"><div class="empty">加载中…</div></td></tr>';
    const data = await api('/api/events?limit=200' + (kind ? '&kind=' + kind : ''));
    if (!data.ok) { rows.innerHTML = '<tr><td colspan="4"><div class="empty">加载失败</div></td></tr>'; return; }
    const events = data.events || [];
    if (!events.length) {
      rows.innerHTML = '<tr><td colspan="4"><div class="empty"><div class="big">暂无记录</div>' +
        '<div>CF 点发生变化、Cookie 失效或推送失败时都会记录在这里</div></div></td></tr>';
      return;
    }
    rows.innerHTML = events.map((e) => {
      const meta = EVENT_META[e.kind] || EVENT_META.info;
      const push = PUSH_META[e.push_status] || PUSH_META.none;
      return '<tr>' +
        '<td class="small muted mono">' + esc(e.created_at) + '</td>' +
        '<td><span class="pill ' + (KIND_PILL[e.kind] || 'mute') + '">' + meta.label + '</span></td>' +
        '<td><div class="cell-title" style="font-weight:550">' + esc(e.title) + '</div>' +
        '<div class="cell-sub" style="white-space:pre-wrap">' + esc(e.message || '') +
        (e.push_detail ? '<div class="muted" style="margin-top:3px;font-size:11px">' + esc(e.push_detail) + '</div>' : '') + '</div></td>' +
        '<td>' + (e.push_status && e.push_status !== 'none'
          ? '<span class="pill ' + push.cls + ' no-dot">' + push.text + '</span>'
          : '<span class="muted small">—</span>') + '</td></tr>';
    }).join('');
  }

  async function loadRecentEvents() {
    const data = await api('/api/events?limit=6');
    const box = $('#dashEvents');
    if (!data.ok || !data.events || !data.events.length) {
      box.innerHTML = '<div class="empty">暂无事件。CF 点发生变化时会在这里出现。</div>';
      return;
    }
    box.innerHTML = data.events.map((e) => {
      const meta = EVENT_META[e.kind] || EVENT_META.info;
      return '<div class="ev ' + esc(e.kind) + '">' +
        '<div class="ico">' + meta.icon + '</div>' +
        '<div class="body"><div class="t">' + esc(e.title) + '</div>' +
        '<div class="m">' + esc((e.message || '').split('\n').slice(0, 2).join(' · ')) + '</div></div>' +
        '<div class="when">' + esc(shortTime(e.created_at)) + '</div></div>';
    }).join('');
  }

  /* ------------------------------------------------------------ 事件绑定 */
  function bind() {
    $$('.nav-item').forEach((btn) => {
      btn.onclick = () => switchView(btn.dataset.view);
    });

    document.addEventListener('click', (e) => {
      const goto = e.target.closest('[data-goto]');
      if (goto) switchView(goto.dataset.goto);

      const preview = e.target.closest('[data-preview]');
      if (preview) { openPreview(preview.dataset.preview); return; }
    });

    $('#btnAdd').onclick = () => openAccountModal(null);
    $('#btnAddTop').onclick = () => openAccountModal(null);

    $('#btnReload').onclick = async () => {
      const btn = $('#btnReload');
      btn.disabled = true;
      await loadState(true);
      if (state.view === 'logs') loadLogs();
      loadRecentEvents();
      btn.disabled = false;
      toast('已刷新');
    };

    $('#btnPause').onclick = async () => {
      const paused = $('#btnPause').dataset.paused === '1';
      const data = await api('/api/monitor/pause', { method: 'POST', body: { paused: !paused } });
      if (data.ok) { toast(data.message, 'ok'); loadState(true); }
      else toast(data.message || '操作失败', 'bad');
    };

    // 账号表操作
    $('#acctRows').addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-act]');
      if (!btn) return;
      const id = btn.dataset.id;
      const acc = state.accounts.find((a) => String(a.id) === String(id));
      if (!acc) return;

      if (btn.dataset.act === 'preview') return openPreview(id);
      if (btn.dataset.act === 'edit') return openAccountModal(acc);
      if (btn.dataset.act === 'delete') {
        const yes = await confirmAction({
          title: '删除账号「' + acc.name + '」？',
          desc: '该账号的历史快照与保活记录会一起删除，且无法恢复。',
          okText: '删除', danger: true
        });
        if (!yes) return;
        const d = await api('/api/accounts/' + id, { method: 'DELETE' });
        toast(d.message || '已删除', d.ok ? 'ok' : 'bad');
        loadState(true);
        return;
      }
      if (btn.dataset.act === 'refresh') {
        btn.disabled = true;
        const d = await api('/api/accounts/' + id + '/refresh', { method: 'POST' });
        btn.disabled = false;
        if (d.ok && d.result) {
          if (d.result.ok) {
            toast(d.result.changed
              ? 'CF 点有变动：' + fmtNum(d.result.cfpoint)
              : '查询成功：' + fmtNum(d.result.cfpoint), 'ok');
          } else {
            toast(d.result.message || '查询失败', 'bad', 5000);
          }
          loadState(true);
        } else toast(d.message || '失败', 'bad');
        return;
      }
      if (btn.dataset.act === 'keepalive') {
        btn.disabled = true;
        const d = await api('/api/accounts/' + id + '/keepalive', { method: 'POST' });
        btn.disabled = false;
        toast(d.message || (d.ok ? '保活完成' : '保活失败'), d.ok ? 'ok' : 'bad');
        return;
      }
    });

    $('#btnRefreshAll').onclick = async () => {
      const list = state.accounts.filter((a) => a.enabled);
      if (!list.length) return toast('没有启用的账号', 'bad');
      const btn = $('#btnRefreshAll');
      btn.disabled = true;
      let okCount = 0;
      for (let i = 0; i < list.length; i++) {
        btn.innerHTML = '<span class="spin"></span>' + (i + 1) + '/' + list.length;
        const d = await api('/api/accounts/' + list[i].id + '/refresh', { method: 'POST' });
        if (d.ok && d.result && d.result.ok) okCount++;
        if (i < list.length - 1) await new Promise((r) => setTimeout(r, 800));
      }
      btn.disabled = false;
      btn.innerHTML = '<svg><use href="#i-refresh"/></svg>全部刷新';
      toast('已完成 ' + okCount + '/' + list.length + ' 个账号', okCount === list.length ? 'ok' : 'bad');
      loadState(true);
    };

    // 设置表单
    $$('#view-settings [data-setting], #view-webhook [data-setting]').forEach((el) => {
      el.addEventListener('input', () => {
        if (el.dataset.setting === 'jitter') {
          $('#jitterVal').textContent = el.value + '%';
        }
        if (el.dataset.setting === 'poll_interval') syncIntervalChips();
        if (el.dataset.setting === 'daily_times') syncScheduleChips();
        if (el.closest('#view-settings')) state.settingsDirty = true;
        if (el.closest('#view-webhook')) state.webhookDirty = true;
      });
      el.addEventListener('change', () => {
        if (el.closest('#view-settings')) state.settingsDirty = true;
        if (el.closest('#view-webhook')) state.webhookDirty = true;
        if (el.dataset.setting === 'webhook_type') renderWebhookVisibility();
        if (el.dataset.setting === 'webhook_body_tpl') renderSample();
      });
    });

    $$('#intervalChips .chip').forEach((chip) => {
      chip.onclick = () => {
        $('#intervalInput').value = chip.dataset.interval;
        syncIntervalChips();
        state.settingsDirty = true;
      };
    });

    // 采集模式切换：只改隐藏域，真正的排期由保存后服务端 reschedule 决定
    $$('#modeChips .chip').forEach((chip) => {
      chip.onclick = () => {
        $('#scheduleMode').value = chip.dataset.mode;
        state.settingsDirty = true;
        syncScheduleChips();
      };
    });

    // 定时时间点快捷 chip：点一下加入，再点一下移除（至少保留一个）
    $$('#dailyChips .chip').forEach((chip) => {
      chip.onclick = () => {
        const slot = chip.dataset.time;
        const list = splitDailyTimes($('#dailyTimesInput').value);
        const idx = list.indexOf(slot);
        if (idx >= 0) {
          if (list.length === 1) return toast('至少保留一个时间点', 'bad');
          list.splice(idx, 1);
        } else {
          list.push(slot);
        }
        list.sort();
        $('#dailyTimesInput').value = list.join(',');
        state.settingsDirty = true;
        syncScheduleChips();
      };
    });

    $$('#dailyNotifyChips .chip').forEach((chip) => {
      chip.onclick = () => {
        $('#dailyNotifyMode').value = chip.dataset.notify;
        state.settingsDirty = true;
        syncScheduleChips();
      };
    });

    $$('.js-save-settings').forEach((btn) => {
      btn.onclick = async () => {
        const d = await api('/api/settings', { method: 'POST', body: collectSettings() });
        if (d.ok) {
          state.settingsDirty = false;
          state.settings = d.settings;
          loadState(true);
          toast(d.message, 'ok', 4200);
        } else toast(d.message || '保存失败', 'bad');
      };
    });

    $('#btnWebhookSave').onclick = async () => {
      const d = await api('/api/settings', { method: 'POST', body: collectSettings() });
      if (d.ok) {
        state.webhookDirty = false;
        state.settings = d.settings;
        loadState(true);
        toast(d.message, 'ok', 4200);
      } else toast(d.message || '保存失败', 'bad');
    };

    $('#btnWebhookTest').onclick = async () => {
      const btn = $('#btnWebhookTest');
      const res = $('#webhookTestResult');
      btn.disabled = true;
      btn.innerHTML = '<span class="spin"></span>发送中…';
      res.textContent = '';
      const d = await api('/api/webhook/test', { method: 'POST', body: collectSettings() });
      btn.disabled = false;
      btn.innerHTML = '<svg><use href="#i-send"/></svg>发送测试';
      res.textContent = d.ok ? '✓ ' + (d.detail || '') : '✗ ' + (d.message || '') + (d.detail ? ' · ' + d.detail : '');
      res.style.color = d.ok ? '#12a150' : '#e5484d';
      toast(d.ok ? '测试推送已发送' : (d.message || '推送失败'), d.ok ? 'ok' : 'bad', 5000);
    };

    $('#logFilter').onchange = loadLogs;
    $('#btnLogRefresh').onclick = loadLogs;
    $('#btnLogClear').onclick = async () => {
      const yes = await confirmAction({ title: '清空全部事件日志？', desc: '仅删除日志记录，不影响账号与历史数据。', okText: '清空', danger: true });
      if (!yes) return;
      const d = await api('/api/events/clear', { method: 'POST' });
      toast(d.message || '已清空', 'ok');
      loadLogs();
      loadRecentEvents();
    };

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        const masks = $$('.modal-mask');
        if (masks.length) {
          masks[masks.length - 1].remove();
          if (/^#\/accounts\/\d+/.test(location.hash)) history.replaceState(null, '', '#/accounts');
        }
      }
    });

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) loadState(true);
    });
  }

  /* ------------------------------------------------------------ 启动 */
  async function boot() {
    bind();
    await loadState(false);
    applyHash();
    window.addEventListener('hashchange', applyHash);
    loadRecentEvents();
    setInterval(tickCountdown, 1000);
    setInterval(() => {
      if (!document.hidden) loadState(true);
    }, 6000);
    setInterval(() => {
      if (!document.hidden && state.view === 'dashboard') loadRecentEvents();
    }, 30000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
