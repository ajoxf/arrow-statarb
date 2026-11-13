/* The desk — Arrow Trader's window frame over THIS engine.
 *
 * Every figure on these windows comes from the same endpoints the dashboard
 * reads (/api/engine/status, /api/ladder, /api/arrow-margin, /api/execution,
 * /api/exchange-orders, /api/desk/trades, /api/spread-history), and every
 * order goes through the same server paths — so the rules (the MANUAL / ALGO
 * lock, one position at a time, a close must match what is open) are enforced
 * by the server, and the buttons here only reflect them.
 *
 * House rules, from Arrow Trader: one shared modal, errors that stay until
 * dismissed, and an em dash for anything unmeasured — never a zero.
 */
(function () {
  'use strict';

  var DASH = '—';
  var LAYOUT_KEY = 'nexus-desk-layout-v6';   // v6: Mirror's Algo window per pair
  var PREF_KEY = 'nexus-desk-prefs-v1';

  // ── tiny helpers ───────────────────────────────────────────────────────
  function $(sel, root) { return (root || document).querySelector(sel); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }
  function num(v, d) {
    if (v == null || !isFinite(v)) { return DASH; }
    return Number(v).toLocaleString('en-IN', {minimumFractionDigits: d, maximumFractionDigits: d});
  }
  function inr(v, d) {
    if (v == null || !isFinite(v)) { return DASH; }
    var n = Number(v);
    return (n < 0 ? '−₹' : '₹') + Math.abs(n).toLocaleString('en-IN',
      {minimumFractionDigits: d == null ? 0 : d, maximumFractionDigits: d == null ? 0 : d});
  }
  function signed(v, d) {
    if (v == null || !isFinite(v)) { return DASH; }
    return (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(Number(v)).toFixed(d);
  }
  function ms(v) { return v == null ? DASH : (v >= 1000 ? (v / 1000).toFixed(2) + 's' : v + 'ms'); }
  function clock(t) {
    return t ? new Date(t * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'}) : DASH;
  }
  // Background polling must never starve the page. A browser opens only ~6
  // connections to one server; if polls pile up behind a slow endpoint, every
  // click and even a plain link waits behind them. So: a poll whose previous
  // request is still out is skipped, polls use at most 4 connections (the
  // rest stay free for what YOU click), and a request is cut off at 10 s.
  var inflight = {}, inflightN = 0, POLL_MAX = 6, slowUntil = 0;
  function getJSON(url, userAsked) {
    var key = url.split('?')[0];
    if (!userAsked && (inflight[key] || inflightN >= POLL_MAX)) {
      return Promise.reject(new Error('skipped: a request is still pending'));
    }
    var counted = !userAsked;
    inflight[key] = true; if (counted) { inflightN++; }
    var ctl = window.AbortController ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) { ctl.abort(); } slowUntil = Date.now() + 15000; }, 10000);
    return fetch(url, {cache: 'no-store', signal: ctl ? ctl.signal : undefined}).then(function (r) {
      if (!r.ok) { throw new Error('HTTP ' + r.status); }
      return r.json();
    }).finally(function () {
      clearTimeout(timer); inflight[key] = false; if (counted) { inflightN--; }
    });
  }
  function postJSON(url, body) {
    return fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                       body: JSON.stringify(body || {})}).then(function (r) { return r.json(); });
  }
  function load(key, dflt) {
    try { return JSON.parse(localStorage.getItem(key)) || dflt; } catch (e) { return dflt; }
  }
  function save(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); } catch (e) {} }

  // ── toasts + the one modal (also used by the shared ALGO control) ─────
  function toast(msg, kind) {
    var box = $('#toasts');
    var t = document.createElement('div');
    t.className = 'toast ' + (kind === 'danger' ? 'error' : kind || 'info');
    t.innerHTML = '<span>' + esc(msg) + '</span><button class="toast-x" title="Dismiss">×</button>';
    t.querySelector('button').onclick = function () { t.remove(); };
    box.appendChild(t);
    // Errors stay until dismissed; information goes after a while.
    if (kind !== 'danger' && kind !== 'error') { setTimeout(function () { t.remove(); }, 6000); }
  }
  window.showToast = toast;

  // A script error must never leave a screen that silently ignores clicks.
  window.addEventListener('error', function (e) {
    toast('Page error: ' + (e.message || e) + ' — please screenshot this.', 'danger');
  });

  // The ALGO dialog is written inside the taskbar; lift it to the page so
  // nothing around the taskbar can trap or clip it.
  var algoModal = document.getElementById('algoCtlModal');
  if (algoModal && algoModal.parentNode !== document.body) { document.body.appendChild(algoModal); }

  // Esc, or a click on the dark backdrop, always closes an open dialog.
  function dismissDialogs() {
    var m = $('#modal');
    if (m && !m.classList.contains('hidden')) { $('#modal-cancel').click(); }
    if (algoModal && algoModal.classList.contains('show')) {
      var x = algoModal.querySelector('[data-bs-dismiss]');
      if (x) { x.click(); }
    }
  }
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { dismissDialogs(); } });
  [$('#modal'), algoModal].forEach(function (m) {
    if (m) { m.addEventListener('mousedown', function (e) { if (e.target === m) { dismissDialogs(); } }); }
  });

  // One-click trading by default: the dialog only appears with 'Confirm'
  // ticked on the ladder rail. A repeat of the SAME action within 0.5 s is
  // dropped — with no dialog in the way, a double-click must not send twice.
  function wantConfirm() { return prefs.confirmOrders === true; }
  var lastAction = {key: null, at: 0};
  function debounced(key) {
    var now = Date.now();
    if (lastAction.key === key && now - lastAction.at < 500) { return true; }
    lastAction = {key: key, at: now};
    return false;
  }
  function maybeConfirm(title, body, confirmText, tone) {
    return wantConfirm() ? confirmBox(title, body, confirmText, tone) : Promise.resolve(true);
  }

  function confirmBox(title, body, confirmText, tone) {
    var m = $('#modal');
    $('#modal-title').textContent = title;
    $('#modal-title').className = tone ? 'tone-' + tone : '';
    $('#modal-body').textContent = body;
    $('#modal-confirm').textContent = confirmText || 'Confirm';
    $('#modal-confirm').className = 'btn ' + (tone ? 'tone-' + tone : 'danger');
    m.classList.remove('hidden');
    return new Promise(function (resolve) {
      function done(v) {
        m.classList.add('hidden');
        $('#modal-confirm').onclick = $('#modal-cancel').onclick = null;
        resolve(v);
      }
      $('#modal-confirm').onclick = function () { done(true); };
      $('#modal-cancel').onclick = function () { done(false); };
    });
  }

  // ── windows ────────────────────────────────────────────────────────────
  var W = {};            // id -> {def, node, body}
  var layout = load(LAYOUT_KEY, {});
  var prefs = load(PREF_KEY, {});
  var topZ = 20;

  var DEFS = [
    // id, title, default left/top/width/height, builder
    // The Arrow-StatArb windows, as they were — open or close each from +.
    {id: 'signal', title: 'Signal & Position', x: 812, y: 2, w: 470, h: 360, note: 'z on both sides, position, levels'},
    {id: 'stats', title: 'Statistics & Filters', x: 1288, y: 2, w: 330, h: 360, note: 'lookback, edge filter, algo state'},
    {id: 'bands', title: 'Spread', x: 812, y: 368, w: 470, h: 272, note: 'the spread in candles, with the entry bands'},
    {id: 'charts', title: 'Z-Score & Spread', x: 812, y: 368, w: 470, h: 272, closed: true, note: 'z and spread lines'},
    {id: 'margin', title: 'Margin', x: 1624, y: 2, w: 296, h: 300, note: 'Arrow\'s pair margin and funds'},
    {id: 'fills', title: 'Fills & Slippage', x: 396, y: 646, w: 410, h: 250, flush: true, note: 'executed orders, slip per lot'},
    {id: 'workorders', title: 'Working Orders', x: 812, y: 646, w: 470, h: 250, flush: true, note: 'resting spread orders'},
    {id: 'trades', title: 'Trades', x: 1288, y: 368, w: 632, h: 272, flush: true, note: 'closed round trips'},
    {id: 'orders', title: 'Order Log (Arrow)', x: 1288, y: 646, w: 632, h: 250, flush: true, note: 'Arrow\'s own order book'},
    {id: 'settings', title: 'Settings', x: 120, y: 60, w: 900, h: 640, flush: true, iframe: '/settings?embed=1', closed: true, note: 'signal, algo, risk, costs'},
    {id: 'setup', title: 'Setup · pair', x: 160, y: 80, w: 900, h: 600, flush: true, iframe: '/?embed=1', closed: true, note: 'choose the two legs'},
    {id: 'analysis', title: 'Analysis', x: 200, y: 100, w: 1000, h: 640, flush: true, iframe: '/analysis?embed=1', closed: true, note: 'did reaching the entry z make money'},
  ];

  // Mirror's two windows PER PAIR: the ladder and its algo box. One pair:
  // ladder, algo box, then the Arrow windows beside them. More pairs: the
  // ladders in a row and the algo boxes stacked at the right — Mirror's desk.
  function pairDefs() {
    var n = ORDER.length, out = [];
    ORDER.forEach(function (k, i) {
      var lw = n <= 3 ? 390 : Math.max(280, Math.floor((1916 - 420) / n) - 2);
      var x0 = 2 + n * (lw + 2);
      out.push({id: 'ladder:' + k, pair: k, kind: 'ladder', title: 'Ladder', x: 2 + i * (lw + 2), y: 2, w: lw, h: 900, mirror: 'ladder'});
      // Mirror's Algo window beside the ladders; with several pairs, one
      // behind the other, each a step down — bring one forward from its tab
      out.push({id: 'algo:' + k, pair: k, kind: 'algo', title: 'Algo', mirror: 'algowin',
                x: n === 1 ? 396 : x0, y: n === 1 ? 2 : 2 + i * 26, w: 410, h: 640});
      out.push({id: 'fair:' + k, pair: k, kind: 'fair', title: 'Fair spread', mirror: 'fairwin', closed: true,
                x: n === 1 ? 810 : x0, y: n === 1 ? 2 : 680, w: 258, h: 300});
    });
    return out;
  }
  function allDefs() { return pairDefs().concat(DEFS); }
  function defOf(id) { return allDefs().filter(function (d) { return d.id === id; })[0]; }

  // The default layout is drawn for a ~1500 × 910 desktop; on a smaller or
  // zoomed screen it is scaled down to fit (a moved window keeps its place).
  function fit(d) {
    var dk = $('#desktop');
    var sx = Math.min(1, (dk.clientWidth - 4) / 1924), sy = Math.min(1, (dk.clientHeight - 4) / 1000);
    if (d.mirror === 'ladder') { sy = Math.min(1, (dk.clientHeight - 4) / d.h); }
    if (d.pair) { sx = Math.min(1, (dk.clientWidth - 4) / 1916); }
    return {x: Math.round(d.x * sx), y: Math.round(d.y * sy), w: Math.round(d.w * sx), h: Math.round(d.h * sy)};
  }

  function place(node, x, y, w, h) {
    node.style.left = Math.max(0, x) + 'px';
    node.style.top = Math.max(0, y) + 'px';
    if (w) { node.style.width = Math.max(220, w) + 'px'; }
    if (h) { node.style.height = Math.max(90, h) + 'px'; }
  }

  function openWindow(id) {
    var d = defOf(id);
    if (!d) { return; }
    if (W[id]) { raise(W[id].node); return; }
    var node = document.createElement('section');
    node.className = 'window floating sized' + (d.mirror ? ' ' + d.mirror : '') +
      (d.pair && d.kind === 'ladder' && d.pair !== ACTIVE ? ' inactive' : '');
    node.dataset.id = id;
    if (d.mirror) {
      node.innerHTML = (d.mirror === 'ladder' ? MIRROR_LADDER : d.mirror === 'algowin' ? MIRROR_ALGO : MIRROR_FAIR) +
        '<div class="grip"></div>';
    } else node.innerHTML =
      '<div class="titlebar"><span class="title">' + esc(d.title) + '</span>' +
      (d.iframe ? '<a class="winbtn" href="' + S.base + d.iframe.replace(/[?&]embed=1/, '') +
                  '" target="_blank" title="Open as a full page">↗</a>' : '') +
      '<span class="winbtns"><button class="winbtn close" title="Close">×</button></span></div>' +
      '<div class="wbody' + (d.flush ? ' flush' : '') + '"></div><div class="grip"></div>';
    $('#desktop').appendChild(node);
    var L = layout[id] || {};
    var F = fit(d);
    place(node, L.x != null ? L.x : F.x, L.y != null ? L.y : F.y, L.w || F.w, L.h || F.h);
    node.style.zIndex = L.z || ++topZ;
    topZ = Math.max(topZ, L.z || 0);
    var body = d.mirror ? node : node.querySelector('.wbody');
    W[id] = {def: d, node: node, body: body};
    node.querySelector('.close').onclick = function () { closeWindow(id); };
    if (d.mirror === 'fairwin') { node.style.height = 'auto'; if (!L.w) { node.style.width = ''; } }
    node.addEventListener('pointerdown', function (e) {
      if (d.pair) { setActive(d.pair); }
      raise(node); startDrag(e, node);
    });
    if (d.pair) {
      var ctx = P[d.pair];
      if (d.kind === 'ladder') { ctx.win = W[id]; BUILD.ladder(node, ctx); refreshLadder(ctx); }
      else if (d.kind === 'algo') { ctx.algoWin = W[id]; BUILD.algo(node, ctx); refreshAlgo(ctx); }
      else { ctx.fairWin = W[id]; BUILD.fair(node, ctx); refreshFair(ctx); }
    } else if (d.iframe) {
      body.innerHTML = '<iframe class="embed" src="' + S.base + d.iframe + '"></iframe>';
      retitle(id);
    } else {
      BUILD[id](body);
      retitle(id);
      REFRESH[id] && REFRESH[id]();
    }
    layout[id] = Object.assign({}, layout[id], {open: true});
    save(LAYOUT_KEY, layout);
    renderTabs();
  }

  function closeWindow(id) {
    if (!W[id]) { return; }
    var d = W[id].def;
    if (d.pair && P[d.pair]) { P[d.pair][{ladder: 'win', algo: 'algoWin', fair: 'fairWin'}[d.kind]] = null; }
    W[id].node.remove();
    delete W[id];
    layout[id] = Object.assign({}, layout[id], {open: false});
    save(LAYOUT_KEY, layout);
    renderTabs();
  }

  function raise(node) {
    node.style.zIndex = ++topZ;
    var id = node.dataset.id;
    layout[id] = Object.assign({}, layout[id], {z: topZ});
  }

  function startDrag(e, node) {
    if (e.button !== 0) { return; }
    var grip = e.target.closest('.grip');
    var bar = e.target.closest('.titlebar');
    if (!grip && !bar) { return; }
    if (!grip && e.target.closest('button, select, input, a')) { return; }
    var id = node.dataset.id;
    var box = node.getBoundingClientRect();
    var origin = $('#desktop').getBoundingClientRect();
    var sx = e.clientX, sy = e.clientY;
    node.classList.add('dragging');
    function move(ev) {
      if (grip) {
        place(node, box.left - origin.left, box.top - origin.top,
              box.width + ev.clientX - sx, box.height + ev.clientY - sy);
      } else {
        place(node, box.left - origin.left + ev.clientX - sx, box.top - origin.top + ev.clientY - sy);
      }
    }
    function drop() {
      node.classList.remove('dragging');
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', drop);
      layout[id] = Object.assign({}, layout[id], {
        x: parseInt(node.style.left, 10), y: parseInt(node.style.top, 10),
        w: node.offsetWidth, h: node.offsetHeight, z: parseInt(node.style.zIndex, 10) || topZ});
      save(LAYOUT_KEY, layout);
      if (id === 'charts') { resizeCharts(); }
    }
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', drop);
    e.preventDefault();
  }

  function renderTabs() {
    var tabs = $('#tabs');
    tabs.innerHTML = '';
    allDefs().forEach(function (d) {
      if (!W[d.id] || d.kind === 'fair') { return; }      // Mirror: a tab per ladder and per Algo window
      var b = document.createElement('button');
      var c = d.pair ? P[d.pair] : null;
      b.className = 'tab' + (c && c === S && d.kind === 'ladder' ? ' on' : '');
      var n = c && d.kind === 'ladder' ? ((c.wo && c.wo.active) || []).length : 0;
      b.innerHTML = esc(c ? pairName(c) + (d.kind === 'algo' ? ' · Algo' : '') : d.title) +
        (n ? '<span class="badge">' + n + '</span>' : '');
      b.onclick = function () { if (c) { setActive(c.key); } raise(W[d.id].node); };
      tabs.appendChild(b);
    });
    // Mirror's + menu: every pair's ladder and algo box, then the windows.
    // The Arrow-StatArb windows show the ACTIVE pair; ticked = open.
    var menu = $('#add-menu');
    var html = '<div class="menu-title">Ladders</div>';
    pairDefs().forEach(function (d) {
      var c = P[d.pair], legs = c.legs && c.legs.length ? c.legs : ((c.ladder && c.ladder.legs) || []);
      html += '<button data-id="' + d.id + '">' + esc(pairName(c) + (d.kind === 'algo' ? ' · Algo' : d.kind === 'fair' ? ' · Fair spread' : '')) +
        '<small>' + esc(d.kind === 'ladder' ? (legs[0] || 'legs not set') + ' / ' + (legs[1] || '—')
                        : d.kind === 'algo' ? 'signal & position, statistics, filters, backtest' : 'what the carry says, exit levels') +
        (W[d.id] ? ' · open' : '') + '</small></button>';
    });
    html += '<button data-href="/p/main/">+ New pair<small>add one on Setup → Pairs</small></button>';
    html += '<div class="menu-title">Arrow-StatArb windows' + (ORDER.length > 1 ? ' · ' + esc(pairName(S)) : '') + '</div>';
    html += DEFS.map(function (d) {
      return '<button data-id="' + d.id + '" data-toggle="1"><input type="checkbox" tabindex="-1"' + (W[d.id] ? ' checked' : '') +
        ' style="float:left;margin:2px 6px 0 0;pointer-events:none"> ' + esc(d.title) +
        '<small style="margin-left:22px">' + esc(d.note || '') + (W[d.id] ? ' · open' : '') + '</small></button>';
    }).join('');
    html += '<div class="menu-note">Ticked = open; untick to close it.' +
      (ORDER.length > 1 ? ' These windows show the pair whose ladder you clicked last.' : '') +
      ' Where you put a window is where it is after a reload; Tidy puts them back.</div>';
    menu.innerHTML = html;
    Array.prototype.forEach.call(menu.querySelectorAll('button[data-id]'), function (b) {
      b.onclick = function (e) {
        e.stopPropagation();
        var id = b.dataset.id;
        if (b.dataset.toggle && W[id]) { closeWindow(id); } else { openWindow(id); }
        if (!b.dataset.toggle) { menu.classList.add('hidden'); }
      };
    });
    var np = menu.querySelector('button[data-href]');
    if (np) { np.onclick = function () { window.location.href = np.dataset.href; }; }
  }

  $('#add-panel').onclick = function (e) { e.stopPropagation(); renderTabs(); $('#add-menu').classList.toggle('hidden'); };
  document.addEventListener('click', function (e) {
    if (!e.target.closest('#add-menu')) { $('#add-menu').classList.add('hidden'); }
  });
  $('#tidy').onclick = function () {
    layout = {};
    save(LAYOUT_KEY, layout);
    Object.keys(W).forEach(function (id) {
      var F = fit(W[id].def);
      place(W[id].node, F.x, F.y, F.w, F.h);
    });
    resizeCharts();
  };

  // ── the pairs: one state per pair; S is the ACTIVE one ─────────────────
  // Every pair is its own engine at /p/<key>/. A ladder and its algo box talk
  // to THEIR pair; the other windows (Signal & Position, the Spread chart,
  // Trades …) show the ACTIVE pair — the ladder last clicked.
  var P = {}, ORDER = [], ACTIVE = 'main';
  function newCtx(key, base) {
    return {key: key, base: base, name: '', legs: [], status: null, algo: null, ladder: null,
            statusAt: 0, wo: null, woSeen: null, armed: {buy: null, sell: null}, anchor: null,
            filtered: false, win: null, fairWin: null, algoWin: null};
  }
  P.main = newCtx('main', ''); ORDER = ['main'];
  var S = P.main;

  // A plain fetch('/api/…') from a window that follows the active pair (and
  // the shared ALGO control in the taskbar) goes to the ACTIVE pair. Calls
  // that name a pair (/p/<key>/api/…) and the registry pass through.
  (function () {
    var f = window.fetch;
    window.fetch = function (u, o) {
      if (typeof u === 'string' && u.indexOf('/api/') === 0 && u.indexOf('/api/pairs') !== 0) { u = S.base + u; }
      return f.call(this, u, o);
    };
  })();

  function setActive(key) {
    if (!P[key] || ACTIVE === key) { return; }
    ACTIVE = key; S = P[key];
    eachLadder(function (c) { c.win.node.classList.toggle('inactive', c !== S); });
    Object.keys(W).forEach(function (id) {
      var d = W[id].def;
      if (d.pair) { return; }
      retitle(id);
      if (d.iframe) { var fr = $('iframe', W[id].body); if (fr) { fr.src = S.base + d.iframe; } }
      else if (REFRESH[id]) { REFRESH[id](true); }
    });
    pollStatus(S); pollAlgo(S);
    renderTabs(); renderBanners();
  }
  function retitle(id) {
    var w = W[id]; if (!w || w.def.pair) { return; }
    var t = $('.titlebar .title', w.node);
    if (t) { t.textContent = w.def.title + (ORDER.length > 1 ? ' · ' + pairName(S) : ''); }
  }

  function algoState() { return (window.AlgoCtl && AlgoCtl.state) || S.algo; }

  function pollStatus(ctx) {
    var c = ctx || S;
    return getJSON(c.base + '/api/engine/status').then(function (d) {
      c.status = d; c.statusAt = Date.now();
      if (c === S) {
        $('#engine-banner').classList.add('hidden');
        REFRESH.signal && W.signal && REFRESH.signal();
        REFRESH.stats && W.stats && REFRESH.stats();
        renderBanners();
      }
      if (c.win && c.ladder) { renderFooter(c.win.node, c); applyLock(c); }
    }).catch(function (e) {
      if (c !== S) { return; }
      // A skipped poll is not a dead engine; only a snapshot older than 5 s is.
      if (c.statusAt && Date.now() - c.statusAt < 5000) { return; }
      if (/^skipped/.test(String(e && e.message)) && c.statusAt) { return; }
      var b = $('#engine-banner');
      b.textContent = 'The engine is not answering — prices and positions on this screen are NOT live.';
      b.classList.remove('hidden');
    });
  }

  function pollAlgo(ctx) {
    var c = ctx || S;
    return getJSON(c.base + '/api/algo/state').then(function (d) {
      c.algo = d; applyLock(c);
      if (c === S) { renderBanners(); }
    }).catch(function () {});
  }

  function renderBanners() {
    var a = S.algo || {};
    var b = $('#lock-banner');
    var msg = '';
    var who = ORDER.length > 1 ? ' for ' + pairName(S) : '';
    if (a.running) {
      msg = 'ALGO ON' + who + ' — manual orders are off on its ladder. Turn the algo OFF to trade it by hand.';
    } else if (a.algo_block) {
      msg = a.algo_block;
    }
    b.textContent = msg;
    b.classList.toggle('hidden', !msg);
    var lb = $('#link-badge');
    var st = S.status;
    var fresh = S.statusAt && Date.now() - S.statusAt < 5000;
    var feed = st && st.signal && st.signal.book;
    var slow = Date.now() < slowUntil;
    lb.className = 'link ' + (!fresh ? 'bad' : slow ? 'warn' : feed ? 'ok' : 'warn');
    lb.textContent = !fresh ? 'engine down' : slow ? 'server slow' : feed ? 'book live' : 'no book';
    lb.title = slow ? 'A request took over 10 s and was cut off — the server is slow to answer.'
                    : 'Arrow connection and price feed';
    $('#loop-stat').textContent = S.statusAt ? ((Date.now() - S.statusAt) / 1000).toFixed(1) + 's' : DASH;
  }

  // Manual controls follow the lock each pair's server reports: a ladder by
  // its own pair; the buttons in the other windows by the active pair.
  function lockButtons(root, c, skipLadders) {
    var a = c.algo || {};
    var blocked = !!a.manual_block;
    var flat = !(c.status && c.status.open_trade);
    Array.prototype.forEach.call(root.querySelectorAll('[data-manual]'), function (el) {
      if (skipLadders && el.closest('.window.ladder')) { return; }
      var closer = el.classList.contains('flatten') || el.classList.contains('pos-close');
      el.disabled = blocked || (closer && flat);
      el.title = blocked ? a.manual_block : (closer && flat) ? 'No open position' : (el.dataset.tip || '');
    });
    return blocked;
  }
  function applyLock(ctx) {
    var c = ctx || S;
    if (c.win) {
      var blocked = lockButtons(c.win.node, c, false);
      c.win.node.classList.toggle('locked-manual', blocked);
      var bar = $('.algo-lock', c.win.node);
      if (bar) { bar.hidden = !(c.algo && c.algo.running); }
    }
    if (c === S) { lockButtons(document, S, true); }
  }

  // ── manual orders (server enforces the lock and one-position rule) ────
  function manualOrder(direction, ctx) {
    var S = ctx || ACTIVE_CTX();
    var qty = lots(direction === 'LONG_SPREAD' ? 'BUY' : 'SELL', S);
    var lad = S.ladder || {};
    var px = direction === 'LONG_SPREAD' ? lad.buy_spread : lad.sell_spread;
    var words = (direction === 'LONG_SPREAD' ? 'BUY' : 'SELL') + ' ' + pairName(S) + ' · ' + qty +
      ' lot(s) · at ~' + num(px, 2) + ' (' + (direction === 'LONG_SPREAD' ? 'Ask A − Bid B' : 'Bid A − Ask B') + ')';
    if (debounced('mkt' + S.key + direction)) { return; }
    var go = wantConfirm()
      ? confirmBox((direction === 'LONG_SPREAD' ? 'BUY' : 'SELL') + ' the spread — market',
                   words + '\n\nTagged MANUAL. The algo cannot start while it is open.',
                   direction === 'LONG_SPREAD' ? 'BUY' : 'SELL', direction === 'LONG_SPREAD' ? 'buy' : 'sell')
      : Promise.resolve(true);
    go.then(function (ok) {
      if (!ok) { return; }
      postJSON(S.base + '/api/manual-trade/execute', {direction: direction, lots: qty}).then(function (r) {
        if (r.success) { toast('MANUAL ' + words + ' — ' + (r.message || 'sent'), 'success'); }
        else { toast('Not sent: ' + (r.error || 'refused'), 'danger'); }
        refreshAll(S);
      }).catch(function (e) { toast('Order request failed: ' + e, 'danger'); });
    });
  }
  function ACTIVE_CTX() { return S; }

  function closePosition(ctx) {
    var S = ctx || ACTIVE_CTX();
    var t = S.status && S.status.open_trade;
    if (!t) { toast('No open position on ' + pairName(S) + '.', 'info'); return; }
    if (debounced('close' + S.key)) { return; }
    maybeConfirm('Close position',
      'Close the ' + (t.owner || '') + ' ' + t.position_type + ' position on ' + pairName(S) + ' (' + t.quantity +
      ' lot(s)) now, at the ' + (t.position_type === 'LONG' ? 'Sell' : 'Buy') + ' spread.', 'Close')
      .then(function (ok) {
        if (!ok) { return; }
        postJSON(S.base + '/api/engine/close-position', {}).then(function (r) {
          toast(r.success ? 'Closed.' : 'Not closed: ' + (r.error || 'refused'), r.success ? 'success' : 'danger');
          refreshAll(S);
        });
      });
  }

  // ── working (resting) orders ──────────────────────────────────────────
  function clickMode() { return prefs.clickMode || 'join'; }

  function sideForColumn(col) {
    var join = clickMode() === 'join';
    if (col === 'ask') { return join ? 'SELL' : 'BUY'; }
    return join ? 'BUY' : 'SELL';
  }

  // Mirror's headers stay plain "Bids" / "Asks"; what a click sends is on
  // each cell's tooltip and in the ladder's own settings (⚙).
  function labelColumns(body) {
    body = body || (S.win && S.win.node);
    if (!body) { return; }
    var b = $('th.c-bid', body), a = $('th.c-ask', body);
    var bs = sideForColumn('bid'), as = sideForColumn('ask');
    if (b) { b.title = 'Bids — where the spread can be SOLD (Bid A − Ask B). A click here sends a ' + bs + '.'; }
    if (a) { a.title = 'Asks — where the spread can be BOUGHT (Ask A − Bid B). A click here sends a ' + as + '.'; }
  }


  function placeWorking(side, level, qtyOverride, ctx) {
    var S = ctx || ACTIVE_CTX();
    var qty = qtyOverride || lots(side, S);
    var lad = S.ladder || {};
    var now = side === 'SELL' ? lad.sell_spread : lad.buy_spread;
    var marketable = now != null && (side === 'SELL' ? now >= level : now <= level);
    var words = side + ' ' + pairName(S) + ' ' + qty + ' lot(s) at ' + num(level, 2) + ' — ' +
      (marketable ? 'the market is already there: it fires NOW (both legs at market)'
                  : 'fires when the ' + (side === 'SELL' ? 'Sell' : 'Buy') + ' spread reaches ' + num(level, 2) +
                    ' (now ' + num(now, 2) + ')');
    if (debounced('wo' + S.key + side + level)) { return; }
    var go = wantConfirm()
      ? confirmBox(side + ' the spread — ' + (marketable ? 'fires now' : 'working order (GTC)'),
                   words + '\n\nManual order: good till cancelled; turning the algo ON cancels it.',
                   side + ' ' + qty, side === 'SELL' ? 'sell' : 'buy')
      : Promise.resolve(true);
    go.then(function (ok) {
      if (!ok) { return; }
      postJSON(S.base + '/api/working-orders', {side: side, price: level, lots: qty}).then(function (r) {
        if (r.success) { toast((r.marketable ? 'Sent: ' : 'Working: ') + words, 'success'); }
        else { toast('Not placed: ' + (r.error || 'refused'), 'danger'); }
        refreshWorking(S);
      }).catch(function (e) { toast('Order request failed: ' + e, 'danger'); });
    });
  }

  function cancelWorking(ids, via, ctx) {
    var S = ctx || ACTIVE_CTX();
    var list = (S.wo && S.wo.active || []).filter(function (o) { return ids.indexOf(o.id) >= 0; });
    var words = list.map(function (o) { return o.side + ' ' + o.lots + ' @ ' + num(o.price, 2); }).join(', ');
    maybeConfirm('Cancel working order' + (ids.length > 1 ? 's' : ''), 'Cancel ' + (words || ids.join(', ')) + ' on ' + pairName(S) + '?', 'Cancel order')
      .then(function (ok) {
        if (!ok) { return; }
        Promise.all(ids.map(function (id) {
          return fetch(S.base + '/api/working-orders/' + encodeURIComponent(id) + '?via=' + encodeURIComponent(via || 'the desk'),
                       {method: 'DELETE'}).then(function (r) { return r.json(); });
        })).then(function () { toast('Cancelled: ' + words, 'info'); refreshWorking(S); });
      });
  }

  function cancelAllWorking(via, ctx) {
    var S = ctx || ACTIVE_CTX();
    via = typeof via === 'string' ? via : 'CANCEL ALL';
    var n = (S.wo && S.wo.active || []).length;
    if (!n) { toast('No working orders on ' + pairName(S) + '.', 'info'); return; }
    maybeConfirm('Cancel all working orders', 'Cancel all ' + n + ' working order(s) on ' + pairName(S) + '?', 'Cancel all')
      .then(function (ok) {
        if (!ok) { return; }
        postJSON(S.base + '/api/working-orders/cancel-all?via=' + encodeURIComponent(via), {via: via}).then(function (r) {
          toast((r.cancelled || 0) + ' working order(s) cancelled', 'info'); refreshWorking(S);
        });
      });
  }

  function refreshWorking(ctx) {
    var S = ctx || ACTIVE_CTX();
    return getJSON(S.base + '/api/working-orders').then(function (d) {
      var before = S.wo ? JSON.stringify(S.wo.active.map(function (o) { return o.id + o.status; })) : '';
      S.wo = d;
      // a fill or a rejection since last time → say so once
      (d.recent || []).forEach(function (o) {
        if (S.woSeen && !S.woSeen[o.id] && (o.status === 'filled' || o.status === 'rejected')) {
          toast(pairName(S) + ' — working order ' + o.status.toUpperCase() + ': ' + o.side + ' ' + o.lots + ' @ ' + num(o.price, 2) +
                (o.note ? ' — ' + o.note : ''), o.status === 'filled' ? 'success' : 'danger');
        }
      });
      S.woSeen = S.woSeen || {};
      (d.recent || []).forEach(function (o) { S.woSeen[o.id] = true; });
      if (S.win && S.ladder) { renderRail(S.win.node, S); renderFooter(S.win.node, S); }
      if (S === ACTIVE_CTX() && REFRESH.workorders) { REFRESH.workorders(); }
      var after = JSON.stringify(d.active.map(function (o) { return o.id + o.status; }));
      if (before !== after) { if (S.win && S.ladder) { drawLadder(S.win.node, S); } renderTabs(); }
    }).catch(function () {});
  }

  // ── builders + refreshers per window ──────────────────────────────────
  var BUILD = {}, REFRESH = {};

  // LADDER ------------------------------------------------------------------
  // Mirror-MT5's ladder window, markup and classes as Mirror draws them
  // (ladder.css is Mirror's own stylesheet): title bar, quote strip, the rail,
  // Work | Bids | Price | Asks | LTQ, the B: S: W: counts, and the footer
  // with the two legs' books. Every order still goes through THIS engine's
  // endpoints, so the MANUAL / ALGO lock and the one-position rule hold.
  var MIRROR_LADDER =
    '<div class="titlebar"><span class="swatch"></span><span class="title"></span>' +
    '<span class="route">arrow</span><span class="mode-badge"></span>' +
    '<span class="algo-switch"><button type="button" class="algo-btn off" title="This ladder\'s Algo — click to turn it on or off">ALGO OFF</button>' +
    '<span class="algo-menu" hidden><button type="button" data-algo="OFF">Off</button>' +
    '<button type="button" data-algo="ON" class="on-item">On</button></span></span>' +
    '<button type="button" class="chart-btn" title="Open this ladder\'s chart: the spread in candles with its bands">Chart</button>' +
    '<span class="winbtns"><button class="winbtn ladder-cog" title="This ladder\'s own settings">&#9881;</button>' +
    '<button class="winbtn close" title="Close this ladder">&times;</button></span></div>' +
    '<div class="quotestrip"><span class="netchg">—</span>' +
    '<div class="stats"><span><label>H:</label><b class="stat-h">—</b></span><span><label>L:</label><b class="stat-l">—</b></span></div>' +
    '<div class="stats"><span><label>O:</label><b class="stat-o">—</b></span>' +
    '<span title="Volume is per LEG"><label>V1:</label><b class="stat-v1">—</b> <label>V2:</label><b class="stat-v2">—</b></span></div>' +
    '<span class="ours" title="H / L / O are the spread\'s own 1-minute bars since the session opened (IST).">ours</span></div>' +
    '<div class="algo-lock" hidden>ALGO ON — manual orders are off on this ladder, closes included. Turn the Algo off in the title bar to trade by hand.</div>' +
    '<div class="body"><div class="rail">' +
    '<div class="route-box row" title="Leg A and leg B"><div class="route-leg route-a">A <b>—</b></div><div class="route-leg route-b">B <b>—</b></div></div>' +
    '<div class="mode-row"><select class="order-type" title="LIMIT: a click rests a working order at that level. MARKET: a click crosses both legs now.">' +
    '<option value="LIMIT">Limit</option><option value="MARKET">Market</option></select>' +
    '<select class="tif" title="GTC: until you cancel it. Working orders live in THIS process; turning the algo ON cancels them."><option value="GTC">GTC</option></select></div>' +
    '<div class="quoting-note" title="A working order is held here and fires both legs at market when the spread reaches it.">—</div>' +
    '<button class="quick buy-touch" data-manual data-tip="Buy the spread at the offer (Ask A − Bid B), now">BUY</button>' +
    '<button class="quick sell-touch" data-manual data-tip="Sell the spread at the bid (Bid A − Ask B), now">SELL</button>' +
    '<button class="quick flatten primary-exit" data-manual data-tip="Close the open position at market, now. It does not touch working orders — use CXL All for those.">CLOSE ALL</button>' +
    '<div class="close-lmt-row"><input class="close-limit" type="number" step="0.05" placeholder="Close @ limit" title="The SPREAD level to close the open position at. It rests as a working order on the closing side and waits there — CLOSE ALL crosses now.">' +
    '<button class="quick close-limit-go" data-manual data-tip="Rest a closing order for the open position at the price in the box">@ LMT</button></div>' +
    '<label class="inline-field qty-row"><span class="qty-buy">Qty BUY</span><input class="armed qty-box buy" type="number" min="1" step="1" title="The lots a BUY sends. The keypad sets both sides."></label>' +
    '<div class="keypad"><button class="qty" data-qty="1">1</button><button class="qty" data-qty="5">5</button>' +
    '<button class="qty" data-qty="10">10</button><button class="qty" data-qty="50">50</button>' +
    '<button class="qty" data-qty="100">100</button><button class="qty clr" data-qty="">CLR</button></div>' +
    '<label class="inline-field qty-row"><span class="qty-sell">Qty SELL</span><input class="sell-qty qty-box sell" type="number" min="1" step="1" title="The lots a SELL sends."></label>' +
    '<div class="rail-label">Working orders</div>' +
    '<div class="cxl-row"><button class="cxl cxl-s" title="Cancel this ladder\'s working SELL orders">S<sup class="count-s"></sup></button>' +
    '<button class="cxl cxl-all" title="Cancel every working order on this ladder">CXL All<sup class="count-all"></sup></button>' +
    '<button class="cxl cxl-b" title="Cancel this ladder\'s working BUY orders">B<sup class="count-b"></sup></button></div>' +
    '<label class="inline-field"><span>Increment</span><input class="increment" type="number" min="0" step="0.05" placeholder="tick"></label>' +
    '<div class="centre-row"><label class="check lock" title="Hold the ladder exactly where it is"><input type="checkbox" class="lock-scroll"> Lock</label>' +
    '<label class="check" title="Show only the rows that have something on them"><input type="checkbox" class="filter"> Filter</label>' +
    '<button class="btn recentre" title="Put the window back around the market now">Centre</button></div>' +
    '</div><div class="grid"><table><thead><tr>' +
    '<th class="c-work" title="Work — YOUR working orders at this level, blue for a buy and red for a sell. Double-click to pull them.">Work</th>' +
    '<th class="c-bid">Bids</th><th class="c-price" title="The spread, k × A − B, one row per increment">Price</th><th class="c-ask">Asks</th>' +
    '<th class="c-ltq" title="LTQ — the size of the most recent FILL at this price. A print, not an order.">LTQ</th>' +
    '</tr></thead><tbody></tbody></table>' +
    '<div class="counts"><span class="cnt-b">B:0</span><span class="cnt-s">S:0</span><span class="cnt-w">W:0</span></div></div></div>' +
    '<div class="ladder-settings" hidden><div class="ls-head"><b>Ladder settings</b><span class="ls-pair"></span>' +
    '<button type="button" class="btn ls-close">Close</button></div><div class="ls-body"></div>' +
    '<div class="ls-actions"><button type="button" class="btn ls-save">Apply</button>' +
    '<span class="ls-note hint">For this pair only. Applies at once; blank keeps the value it has.</span></div></div>' +
    '<div class="footer"><span class="feed" title="Age of the two legs\' quotes">—</span>' +
    '<button class="btn refresh-feed" title="Re-subscribe both legs at Arrow">↻ Feed</button>' +
    '<span class="pos">flat</span><span class="pnl">—</span><span class="clip"></span><span class="errors"></span>' +
    '<span class="legs">—</span></div>';

  function pairName(ctx) { return (ctx && ctx.name) || pairTitle(ctx && (ctx.legs && ctx.legs.length ? ctx.legs : ctx.ladder && ctx.ladder.legs)); }
  function eachLadder(fn) { ORDER.forEach(function (k) { if (P[k] && P[k].win) { fn(P[k]); } }); }
  function pairTitle(legs) {
    if (!legs || !legs[0]) { return 'Ladder'; }
    var re = /^([A-Z]+?)(\d{2})([A-Z]{3})(\d{2})/;
    var a = re.exec(legs[0]), b = re.exec(legs[1] || '');
    if (a && b && a[1] === b[1]) { return a[1] + ' ' + a[3] + '−' + b[3]; }
    return legs[0] + ' − ' + (legs[1] || '');
  }
  function lots(side, ctx) {
    var node = ctx && ctx.win && ctx.win.node;
    var box = node && $(side === 'SELL' ? '.sell-qty' : '.armed', node);
    return parseInt(box && box.value, 10) || 1;
  }

  // LADDER SETTINGS (⚙) -------------------------------------------------------
  // Mirror's per-ladder settings, filled with THIS pair's own settings (each
  // pair has its own settings file). Only fields Arrow's engine really has —
  // a control that changes nothing is not shown. Apply sends only what was
  // changed; blank keeps the value.
  var LS_GROUPS = [
    ['Trading', [
      {k: 'ui.orderType', label: 'Mode', type: 'select', opts: [['LIMIT', 'Limit'], ['MARKET', 'Market']], ui: 'orderType'},
      {k: 'ui.tif', label: 'Time in force', type: 'select', opts: [['GTC', 'GTC']], ui: 'tif', tip: 'working orders live until cancelled — turning the algo ON cancels them'},
      {k: 'ui.increment', label: 'Increment', type: 'num', ui: 'increment', tip: 'one ladder row; blank = the contracts’ tick'},
      {k: 'risk.lots_per_trade', label: 'Default qty', type: 'int', tip: 'lots one click sends — and the algo’s size'},
      {k: 'legs.leg_a', label: 'Leg A ratio', type: 'num', tip: 'k: spread = k × A − B. Changing it restarts the lookback'},
      {k: 'legs.leg_b', label: 'Leg B ratio', type: 'num'},
      {k: 'pairs.multiplier_a', label: 'Contract A', type: 'num', zeroAuto: true, tip: '₹ per point of ONE lot of leg A. Blank / 0 = from the contract (CRUDEOILM 10, GOLDM 10, SILVERM 5, COPPER 2,500)'},
      {k: 'pairs.multiplier_b', label: 'Contract B', type: 'num', zeroAuto: true},
      {k: 'ui.clickMode', label: 'Click', type: 'select', opts: [['join', 'Bids BUY'], ['touch', 'Bids SELL']], ui: 'clickMode', tip: 'Bids BUY: a click in Bids rests a BUY, in Asks a SELL. Bids SELL: the other way round'},
      {k: 'ui.confirmOrders', label: 'Confirm orders', type: 'check', ui: 'confirmOrders'}]],
    ['Exit', [
      {k: 'signal.exit_zscore', label: 'Back to mean', type: 'num', tip: 'the algo closes when z comes back to this'},
      {k: 'signal.stop_zscore', label: 'Z-stop at', type: 'num', tip: 'the algo closes when z stretches this far against it; 0 = off'},
      {k: 'exits.profit_target_inr', label: 'Profit target ₹', type: 'num', zeroOff: true},
      {k: 'exits.dollar_stop_inr', label: 'Stop ₹', type: 'num', zeroOff: true},
      {k: 'exits.tp_capital_pct', label: 'TP % of margin', type: 'num', zeroOff: true},
      {k: 'exits.stop_capital_pct', label: 'SL % of margin', type: 'num', zeroOff: true},
      {k: 'exits.stop_mode', label: 'Stop sized by', type: 'select', opts: [['inr', '₹ / % (above)'], ['atr', 'ATR']],
       tip: 'ATR: the stop is Stop × ATR beyond break-even, the ATR frozen when the trade opens. Needs candle bands'},
      {k: 'exits.target_mode', label: 'Target sized by', type: 'select', opts: [['inr', '₹ / % / z (above)'], ['atr', 'ATR']],
       tip: 'ATR: the target is Target × ATR beyond break-even, frozen at entry. Needs candle bands'},
      {k: 'exits.atr_period', label: 'ATR period', type: 'int', tip: 'candles: the spread’s close-to-close moves, Wilder-smoothed'},
      {k: 'exits.atr_stop_mult', label: 'Stop × ATR', type: 'num'},
      {k: 'exits.atr_target_mult', label: 'Target × ATR', type: 'num'},
      {k: 'filters.brokerage_per_lot', label: 'Comm/lot', type: 'num', tip: '₹ per lot per leg, each way'},
      {k: 'filters.slippage_per_lot', label: 'Slippage', type: 'num', tip: 'slippage allowance: ₹ per lot per leg, each way — a budget, not a measurement'}]],
    ['Algo', [
      {k: 'signal.entry_zscore', label: 'Entry z', type: 'num'},
      {k: 'signal.trade_direction', label: 'Direction', type: 'select', opts: [['both', 'Both'], ['sell_only', 'H to L only'], ['buy_only', 'L to H only']]},
      {k: 'signal.band', label: 'Candle', type: 'select', opts: [['ticks', 'ticks'], ['5m', '5 min'], ['15m', '15 min']], tip: 'the bands: a rolling window of ticks, or Bollinger bands on 5 / 15-minute candles'},
      {k: 'signal.band_length', label: 'Length (candles)', type: 'int'},
      {k: 'signal.confirmation_ticks', label: 'Confirm (ticks)', type: 'int'},
      {k: 'signal.max_entry_zscore', label: 'Max entry z', type: 'num', zeroOff: true},
      {k: 'signal.entry_mode', label: 'Entry', type: 'select', opts: [['reentry', 'armed'], ['touch', 'touch']], tip: 'armed: arm at the entry z, enter on the way back in; touch: enter at the entry z'},
      {k: 'signal.reentry_back', label: 'Re-entry back by', type: 'num'},
      {k: 'signal.reentry_window_pct', label: 'Re-entry %', type: 'num', tip: 're-entry window: how far back towards the mean it may still enter'},
      {k: 'execution.cooldown_sec', label: 'Cooldown (min)', type: 'num', scale: 60},
      {k: 'signal.min_signal_minutes', label: 'Warm-up (min)', type: 'num'},
      {k: 'risk.max_daily_loss', label: 'Daily loss limit', type: 'num', zeroOff: true},
      {k: 'risk.loss_streak_pause_at', label: 'Losses in a row', type: 'int', zeroOff: true},
      {k: 'filters.min_edge_multiple', label: 'Edge filter ×', type: 'num', zeroOff: true},
      {k: 'exits.profit_target_sigma_frac', label: 'Capture fraction', type: 'num', zeroAuto: true, tip: 'how much of the stretch the edge filter expects to keep; 0 = 0.5'},
      {k: 'regime.enabled', label: 'Regime filter', type: 'check'},
      {k: 'filters.half_life_min_sec', label: 'Half-life min', type: 'num', scale: 60, zeroOff: true, tip: 'minutes'},
      {k: 'filters.half_life_max_sec', label: 'Half-life max', type: 'num', scale: 60, zeroOff: true, tip: 'minutes'}]],
    ['Carry', [
      {k: 'pairs.pair_type', label: 'Pair type', type: 'select', opts: [['SPOT_FUTURE', 'Spot vs Fut'], ['FUTURE_FUTURE', 'Calendar'], ['RELATED', 'Related']], tip: 'Calendar: same underlying, two expiries. Related: two instruments — no fair spread'},
      {k: 'pairs.risk_free_rate', label: 'Carry %/yr', type: 'num', scale: 0.01, tip: 'carry rate, % a year — used only for the Fair spread reading'}]]
  ];

  function lsBuild(node, S) {
    var body = $('.ls-body', node);
    body.innerHTML = LS_GROUPS.map(function (g) {
      return '<div class="ls-group"><h4>' + g[0] + (g[0] === 'Algo' ? ' <small class="ls-algo-state"></small>' : '') + '</h4>' +
        g[1].map(function (f) {
          var id = 'ls-' + f.k.replace('.', '-');
          var ctl = f.type === 'select'
            ? '<select data-k="' + f.k + '">' + f.opts.map(function (o) { return '<option value="' + o[0] + '">' + esc(o[1]) + '</option>'; }).join('') + '</select>'
            : f.type === 'check' ? '<input type="checkbox" data-k="' + f.k + '">'
            : '<input type="number" step="any" data-k="' + f.k + '"' + (f.zeroOff ? ' placeholder="off"' : f.zeroAuto ? ' placeholder="auto"' : '') + '>';
          return '<label class="lsf' + (f.type === 'check' ? ' check-row' : '') + '"' + (f.tip ? ' title="' + esc(f.tip) + '"' : '') +
            '><span>' + esc(f.label) + '</span>' + ctl + '</label>';
        }).join('') + '</div>';
    }).join('');
  }

  function lsValue(f, settings, legs, node, S) {
    if (f.ui) {
      if (f.ui === 'increment') { return $('.increment', node).value; }
      if (f.ui === 'tif') { return 'GTC'; }
      if (f.ui === 'orderType') { return prefs.orderType || 'LIMIT'; }
      if (f.ui === 'clickMode') { return clickMode(); }
      if (f.ui === 'confirmOrders') { return wantConfirm(); }
    }
    if (f.k === 'signal.band') {
      var sg = settings.signal || {};
      return sg.band_source === 'candles' ? (sg.band_timeframe || '15m') : 'ticks';
    }
    if (f.k.indexOf('legs.') === 0) { return ((legs || {})[f.k.slice(5)] || {}).ratio; }
    var p = f.k.split('.'), v = (settings[p[0]] || {})[p[1]];
    if (v == null) { return ''; }
    if (f.scale && typeof v === 'number') { v = +(v / f.scale).toFixed(4); }
    if ((f.zeroOff || f.zeroAuto) && v === 0) { return ''; }
    return v;
  }

  function lsOpen(node, S) {
    var ls = $('.ladder-settings', node);
    if (!$('.ls-group', ls)) { lsBuild(ls, S); }
    $('.ls-pair', ls).textContent = ' · ' + pairName(S);
    Promise.all([getJSON(S.base + '/api/settings', true), getJSON(S.base + '/api/leg-assignments', true)]).then(function (r) {
      var settings = r[0] || {}, legs = r[1] || {};
      S.lsLoaded = {};
      LS_GROUPS.forEach(function (g) {
        g[1].forEach(function (f) {
          var el = ls.querySelector('[data-k="' + f.k + '"]');
          var v = lsValue(f, settings, legs, node, S);
          if (f.type === 'check') { el.checked = !!v; } else { el.value = v == null ? '' : v; }
          S.lsLoaded[f.k] = f.type === 'check' ? !!v : String(v == null ? '' : v);
        });
      });
      var st = $('.ls-algo-state', ls);
      st.textContent = (S.algo && S.algo.running) ? '— ON (' + (S.algo.mode === 'live' ? 'LIVE' : 'dry-run') + '): changes apply on its next tick' : '— off';
      S.lsLegs = legs;
      ls.hidden = false;
    }).catch(function (e) { toast('Settings not loaded: ' + e, 'danger'); });
  }

  function lsApply(node, S) {
    var ls = $('.ladder-settings', node);
    var body = {}, legChange = null, n = 0;
    LS_GROUPS.forEach(function (g) {
      g[1].forEach(function (f) {
        var el = ls.querySelector('[data-k="' + f.k + '"]');
        var now = f.type === 'check' ? el.checked : String(el.value);
        if (now === S.lsLoaded[f.k]) { return; }
        if (f.type !== 'check' && f.type !== 'select' && now === '' && !f.zeroOff && !f.zeroAuto) { return; }  // blank keeps
        n++;
        if (f.ui) {
          if (f.ui === 'increment') { $('.increment', node).value = now; }
          else if (f.ui === 'orderType') { prefs.orderType = now; node.classList.toggle('mode-market', now === 'MARKET'); }
          else if (f.ui === 'clickMode') { prefs.clickMode = now; eachLadder(function (c) { labelColumns(c.win.node); }); }
          else if (f.ui === 'confirmOrders') { prefs.confirmOrders = now; }
          save(PREF_KEY, prefs);
          return;
        }
        if (f.k === 'signal.band') {
          var sg = body.signal = body.signal || {};
          if (now === 'ticks') { sg.band_source = 'ticks'; } else { sg.band_source = 'candles'; sg.band_timeframe = now; }
          return;
        }
        if (f.k.indexOf('legs.') === 0) {
          legChange = legChange || {};
          legChange[f.k.slice(5)] = parseFloat(now) || 1;
          return;
        }
        var p = f.k.split('.');
        var v = f.type === 'select' ? now : f.type === 'check' ? now
          : (now === '' ? 0 : (f.type === 'int' ? parseInt(now, 10) : parseFloat(now)) * (f.scale || 1));
        (body[p[0]] = body[p[0]] || {})[p[1]] = v;
      });
    });
    if (!n) { toast('Nothing changed.', 'info'); return; }
    var jobs = [];
    if (Object.keys(body).length) {
      jobs.push(postJSON(S.base + '/api/settings', body).then(function (r) {
        if (r && r.success === false) { throw new Error(r.error || 'refused'); }
      }));
    }
    if (legChange) {
      var L = S.lsLegs || {}, out = {};
      ['leg_a', 'leg_b'].forEach(function (lk) {
        if (L[lk] && L[lk].mapping_id) {
          out[lk] = {mapping_id: L[lk].mapping_id, ratio: legChange[lk] != null ? legChange[lk] : (L[lk].ratio || 1)};
        }
      });
      jobs.push(postJSON(S.base + '/api/leg-assignments', out).then(function (r) {
        if (r && r.success === false) { throw new Error(r.error || 'refused'); }
      }));
    }
    Promise.all(jobs).then(function () {
      toast(pairName(S) + ' — ' + n + ' setting(s) applied.', 'success');
      lsOpen(node, S); refreshLadder(S); refreshAlgo(S); refreshFair(S);
    }).catch(function (e) { toast('Not applied: ' + e.message, 'danger'); lsOpen(node, S); });
  }

  BUILD.ladder = function (node, ctx) {
    var S = ctx;
    node.querySelector('.close').onclick = function () { closeWindow('ladder:' + S.key); };
    $('.buy-touch', node).onclick = function () { manualOrder('LONG_SPREAD', S); };
    $('.sell-touch', node).onclick = function () { manualOrder('SHORT_SPREAD', S); };
    $('.flatten', node).onclick = function () { closePosition(S); };
    $('.close-limit-go', node).onclick = function () {
      var t = S.status && S.status.open_trade;
      var lv = parseFloat($('.close-limit', node).value);
      if (!t) { toast('No open position to close.', 'info'); return; }
      if (!isFinite(lv)) { toast('Type the spread level to close at.', 'info'); return; }
      placeWorking(t.position_type === 'LONG' ? 'SELL' : 'BUY', lv, t.quantity, S);
    };
    Array.prototype.forEach.call(node.querySelectorAll('.keypad .qty'), function (b) {
      b.onclick = function () {
        var q = b.dataset.qty ? Number(b.dataset.qty) : null;
        S.armed.buy = S.armed.sell = q;
        $('.armed', node).value = q || (S.ladder && S.ladder.lots) || '';
        $('.sell-qty', node).value = q || (S.ladder && S.ladder.lots) || '';
        renderRail(node, S);
      };
    });
    $('.armed', node).oninput = function (e) { S.armed.buy = Number(e.target.value) || null; };
    $('.sell-qty', node).oninput = function (e) { S.armed.sell = Number(e.target.value) || null; };
    $('.order-type', node).value = prefs.orderType || 'LIMIT';
    $('.order-type', node).onchange = function (e) {
      prefs.orderType = e.target.value; save(PREF_KEY, prefs); renderRail(node, S);
      node.classList.toggle('mode-market', e.target.value === 'MARKET');
    };
    node.classList.toggle('mode-market', (prefs.orderType || 'LIMIT') === 'MARKET');
    $('.cxl-all', node).onclick = function () { cancelAllWorking('CXL All on the ladder rail', S); };
    $('.cxl-b', node).onclick = function () { cancelSide('BUY', S); };
    $('.cxl-s', node).onclick = function () { cancelSide('SELL', S); };
    $('.lock-scroll', node).onchange = function (e) {
      S.anchor = e.target.checked && S.ladder && S.ladder.rows && S.ladder.rows.length
        ? S.ladder.rows[Math.floor(S.ladder.rows.length / 2)].level : null;
    };
    $('.filter', node).onchange = function (e) { S.filtered = e.target.checked; if (S.ladder) { drawLadder(node, S); } };
    $('.recentre', node).onclick = function () {
      S.anchor = null; $('.lock-scroll', node).checked = false; refreshLadder(S);
    };
    $('.refresh-feed', node).onclick = function () {
      postJSON(S.base + '/api/desk/refresh-feed', {}).then(function (r) {
        toast(r.success ? 'Feed re-subscribed for both legs.' : 'Feed: ' + (r.error || 'not restarted'), r.success ? 'success' : 'danger');
      });
    };
    // The Algo's switch, on the ladder: Off / On. On asks first when the
    // pair is LIVE; the engine still refuses what it cannot do.
    var menu = $('.algo-menu', node);
    $('.algo-btn', node).onclick = function (e) { e.stopPropagation(); menu.hidden = !menu.hidden; };
    Array.prototype.forEach.call(menu.querySelectorAll('button'), function (b) {
      b.onclick = function (e) { e.stopPropagation(); menu.hidden = true; setAlgo(S, b.dataset.algo === 'ON'); };
    });
    document.addEventListener('click', function () { menu.hidden = true; });
    $('.chart-btn', node).onclick = function () { setActive(S.key); openWindow('bands'); };
    var ls = $('.ladder-settings', node);
    $('.ladder-cog', node).onclick = function () { if (ls.hidden) { lsOpen(node, S); } else { ls.hidden = true; } };
    $('.ls-close', node).onclick = function () { ls.hidden = true; };
    $('.ls-save', node).onclick = function () { lsApply(node, S); };
    labelColumns(node);
    var tb = $('tbody', node);
    tb.ondblclick = function (e) {
      var w = e.target.closest('td.work');
      if (w && w.dataset.wo) { cancelWorking(w.dataset.wo.split(','), 'double-click on the ladder Work cell', S); }
    };
    tb.onclick = function (e) {
      var w = e.target.closest('td.work');
      if (w && w.dataset.wo) {
        if (!debounced('markhint')) { toast('Double-click the Work cell to pull that working order.', 'info'); }
        return;
      }
      var td = e.target.closest('td[data-col]');
      if (!td) { return; }
      if ((S.algo || {}).manual_block) { toast(S.algo.manual_block, 'danger'); return; }
      var level = Number(td.parentNode.dataset.level);
      if (!isFinite(level)) { return; }
      var side = sideForColumn(td.dataset.col);
      if ((prefs.orderType || 'LIMIT') === 'MARKET') {
        manualOrder(side === 'BUY' ? 'LONG_SPREAD' : 'SHORT_SPREAD', S);
      } else {
        placeWorking(side, level, null, S);
      }
    };
    applyLock(S);
  };

  function cancelSide(side, ctx) {
    var S = ctx;
    var ids = ((S.wo && S.wo.active) || []).filter(function (o) { return o.side === side; })
      .map(function (o) { return o.id; });
    if (!ids.length) { toast('No working ' + side + ' orders.', 'info'); return; }
    cancelWorking(ids, 'CXL ' + side[0] + ' on the ladder rail', S);
  }

  function rowsThatFit(node) {
    var g = $('.grid', node);
    var h = g ? g.clientHeight - 38 : 0;          // header + counts
    var n = Math.floor(h / 17);
    n = Math.max(9, Math.min(81, n || 29));
    return n % 2 ? n : n - 1;
  }

  function refreshLadder(ctx) {
    var S = ctx;
    if (!S.win) { return Promise.resolve(); }
    var node = S.win.node;
    var inc = $('.increment', node).value;
    var q = S.base + '/api/ladder?count=' + rowsThatFit(node) + (inc ? '&increment=' + encodeURIComponent(inc) : '') +
            (S.anchor != null ? '&anchor=' + S.anchor : '');
    return getJSON(q).then(function (d) {
      S.ladder = d;
      if (d.legs) { S.legs = d.legs; }
      renderRail(node, S);
      drawLadder(node, S);
      renderFooter(node, S);
      applyLock(S);
    }).catch(function () {});
  }

  function renderRail(node, ctx) {
    var S = ctx;
    var d = S.ladder || {};
    var title = pairName(S);
    $('.title', node).textContent = title;
    $('.ls-pair', node).textContent = ' · ' + title;
    if (d.legs) {
      $('.route-a b', node).innerHTML = esc(d.legs[0]) + '<div class="hint">arrow</div>';
      $('.route-b b', node).innerHTML = esc(d.legs[1]) + '<div class="hint">arrow</div>';
    }
    var mode = prefs.orderType || 'LIMIT';
    var algoOn = !!(S.algo || {}).running;
    var live = (S.algo || {}).mode === 'live';
    var ab = $('.algo-btn', node);
    ab.textContent = algoOn ? (live ? 'ALGO LIVE' : 'ALGO DRY-RUN') : 'ALGO OFF';
    ab.className = 'algo-btn ' + (algoOn ? (live ? 'live' : 'dry') : 'off');
    var oi = $('.algo-menu .on-item', node);
    oi.textContent = live ? 'LIVE — it trades' : 'On — DRY-RUN: simulated, nothing sent';
    oi.className = 'on-item' + (live ? ' live' : '');
    var badge = $('.mode-badge', node);
    badge.textContent = mode + ' · GTC' + (algoOn ? ' · AUTO' : '');
    badge.classList.toggle('auto', algoOn);
    badge.title = algoOn ? 'The algo is ON for this pair: it enters and exits by itself; manual orders are off.'
      : 'the mode a click sends, and how long a working order lives';
    $('.quoting-note', node).textContent = mode === 'MARKET'
      ? 'MARKET: both legs cross at once'
      : 'LIMIT: rests here · both legs cross when the spread reaches it';
    ['buy', 'sell'].forEach(function (side) {
      var box = $(side === 'buy' ? '.armed' : '.sell-qty', node);
      if (document.activeElement !== box) { box.value = S.armed[side] || d.lots || ''; }
      box.classList.toggle('on', !!S.armed[side]);
      var btn = $(side === 'buy' ? '.buy-touch' : '.sell-touch', node);
      btn.textContent = (side === 'buy' ? 'BUY ' : 'SELL ') + (S.armed[side] || d.lots || '');
    });
    Array.prototype.forEach.call(node.querySelectorAll('.keypad .qty'), function (b) {
      var q = parseFloat(b.dataset.qty);
      b.classList.toggle('on', S.armed.buy === q && S.armed.sell === q);
    });
    var act = (S.wo && S.wo.active) || [];
    var nb = act.filter(function (o) { return o.side === 'BUY'; }).length;
    var ns = act.filter(function (o) { return o.side === 'SELL'; }).length;
    $('.count-b', node).textContent = nb || '';
    $('.count-s', node).textContent = ns || '';
    $('.count-all', node).textContent = (nb + ns) || '';
    $('.cxl-b', node).disabled = !nb; $('.cxl-s', node).disabled = !ns; $('.cxl-all', node).disabled = !(nb + ns);
    var inc = $('.increment', node);
    if (document.activeElement !== inc && !inc.value && d.increment) { inc.placeholder = String(d.increment); }
    var s = d.session || {};
    var dig = 4;
    var nc = $('.netchg', node);
    nc.textContent = s.net_change == null ? DASH : (s.net_change > 0 ? '+' : '') + Number(s.net_change).toFixed(dig);
    nc.classList.toggle('up', s.net_change > 0);
    $('.stat-h', node).textContent = s.high == null ? DASH : Number(s.high).toFixed(dig);
    $('.stat-l', node).textContent = s.low == null ? DASH : Number(s.low).toFixed(dig);
    $('.stat-o', node).textContent = s.open == null ? DASH : Number(s.open).toFixed(dig);
    var lb = d.legbook || {};
    $('.stat-v1', node).textContent = (lb.a && lb.a.volume != null) ? lb.a.volume : DASH;
    $('.stat-v2', node).textContent = (lb.b && lb.b.volume != null) ? lb.b.volume : DASH;
  }

  function renderFooter(node, ctx) {
    var S = ctx;
    var d = S.ladder || {};
    var lb = d.legbook || {};
    var ages = [lb.a && lb.a.age, lb.b && lb.b.age].filter(function (x) { return x != null; });
    var worst = ages.length ? Math.max.apply(null, ages) : null;
    var feed = $('.feed', node);
    var badge = worst == null ? 'no quote' : (worst > 30 ? 'stale ' + worst.toFixed(0) + 's' : 'OK · ' + worst.toFixed(1) + 's');
    feed.textContent = badge;
    feed.className = 'feed ' + (worst == null ? 'bad' : worst > 30 ? 'warn' : 'ok');
    var t = S.status && S.status.open_trade;
    $('.pos', node).textContent = t
      ? (t.position_type === 'LONG' ? '+' : '-') + t.quantity + ' @ ' + (t.entry_spread == null ? DASH : Number(t.entry_spread).toFixed(4))
      : 'flat';
    var pnl = $('.pnl', node);
    var v = t ? t.unrealized_pnl : null;
    pnl.textContent = v == null ? DASH : inr(v, 2);
    pnl.classList.toggle('up', v > 0); pnl.classList.toggle('down', v < 0);
    $('.errors', node).textContent = d.error || '';
    $('.legs', node).innerHTML = legFeed(d);
    var c = $('.counts', node);
    var act = (S.wo && S.wo.active) || [];
    $('.cnt-b', c).textContent = 'B:' + (t && t.position_type === 'LONG' ? 1 : 0);
    $('.cnt-s', c).textContent = 'S:' + (t && t.position_type === 'SHORT' ? 1 : 0);
    $('.cnt-w', c).textContent = 'W:' + act.length;
  }

  // Mirror's leg book, at the bottom of the window: each leg's bid, ask, the
  // width it is charging and how long since it changed; under a rule, the
  // spread's own bid and ask (H → L / L → H) and its round turn.
  function legFeed(d) {
    var lb = d.legbook || {};
    if (!lb.a || lb.a.bid == null && lb.a.ask == null) {
      return '<span class="bad">no quote from either leg yet</span>';
    }
    function f4(v) { return v == null || !isFinite(v) ? DASH : Number(v).toFixed(4); }
    function cell(v, k) { return '<td class="' + k + '">' + f4(v) + '</td>'; }
    function width(b, a) { return '<td class="c-width">' + (b == null || a == null ? DASH : f4(a - b)) + '</td>'; }
    function age(s) {
      return s == null ? '<td class="age c-age">' + DASH + '</td>'
        : '<td class="age c-age' + (s > 5 ? ' bad' : '') + '">' + Number(s).toFixed(1) + 's</td>';
    }
    function line(lab, L) {
      return '<tr><th>' + lab + '</th><td class="sym" title="' + esc(L.symbol || '?') + '">' + esc(L.symbol || '?') + '</td>' +
        cell(L.bid, 'c-bid') + cell(L.ask, 'c-ask') + width(L.bid, L.ask) + age(L.age) + '</tr>';
    }
    return '<table class="legbook"><thead><tr><th></th><th class="sym">leg</th><th class="c-bid">Bid</th>' +
      '<th class="c-ask">Ask</th><th class="c-width" title="the bid-ask each leg is charging — and on the spread row, one round turn of both books">Spread</th>' +
      '<th class="c-age">Age</th></tr></thead><tbody>' + line('A', lb.a) + line('B', lb.b) +
      '<tr class="spread-hint"><th></th><td class="sym"></td>' +
      '<td class="c-bid hint-down" title="The BID is where the spread is SOLD. A short is in profit as the spread falls: High to Low.">H &rarr; L</td>' +
      '<td class="c-ask hint-up" title="The ASK is where the spread is BOUGHT. A long is in profit as the spread rises: Low to High.">L &rarr; H</td>' +
      '<td class="c-width"></td><td class="c-age"></td></tr>' +
      '<tr class="spread"><th></th><td class="sym">spread</td>' + cell(d.sell_spread, 'c-bid') + cell(d.buy_spread, 'c-ask') +
      width(d.sell_spread, d.buy_spread) + '<td class="age c-age"></td></tr></tbody></table>';
  }

  function nearestRow(rows, v) {
    if (v == null || !rows.length) { return null; }
    var best = null, dist = Infinity;
    rows.forEach(function (r) { var x = Math.abs(r.level - v); if (x < dist) { dist = x; best = r.level; } });
    return best;
  }

  function drawLadder(node, ctx) {
    var S = ctx;
    var d = S.ladder || {};
    var rows = d.rows || [];
    var tb = $('tbody', node);
    var inc = d.increment || 0;
    var dig = inc && inc < 1 ? (String(inc).split('.')[1] || '').length || 2 : 0;
    // Working orders in the Work column: their lots, tinted buy / sell. An
    // order off the visible ladder sits on the edge row with an arrow.
    var woAt = {}, woTip = {};
    if (rows.length) {
      var top = rows[0].level, bot = rows[rows.length - 1].level, half = inc / 2;
      ((S.wo && S.wo.active) || []).forEach(function (o) {
        var lv, arrow = '';
        if (o.price > top + half) { lv = top; arrow = '↑'; }
        else if (o.price < bot - half) { lv = bot; arrow = '↓'; }
        else { lv = nearestRow(rows, o.price); }
        (woAt[lv] = woAt[lv] || []).push({o: o, arrow: arrow});
        woTip[lv] = (woTip[lv] ? woTip[lv] + ' · ' : '') + o.side + ' ' + o.lots + ' @ ' + num(o.price, 2) +
          (o.status === 'held' ? ' (NOT live: ' + o.note + ')' : '') + ' — double-click to pull';
      });
    }
    var lp = d.last_print;
    var sell = d.sell_spread, buy = d.buy_spread;
    var join = clickMode() === 'join';
    var bidTip = join ? 'BUY (resting) at ' : 'SELL at ';
    var askTip = join ? 'SELL (resting) at ' : 'BUY at ';
    tb.innerHTML = rows.map(function (r) {
      var inBid = sell != null && r.level <= sell + 1e-9;
      var inAsk = buy != null && r.level >= buy - 1e-9;
      var wo = woAt[r.level] || [];
      if (S.filtered && !wo.length && !r.is_best_bid && !r.is_best_ask) { return ''; }
      var cls = [];
      if (inBid) { cls.push('in-bid'); }
      if (inAsk) { cls.push('in-ask'); }
      if (r.is_best_bid) { cls.push('market-line'); }
      if (r.is_mid) { cls.push('mid-line'); }
      if (r.is_best_ask) { cls.push('best-ask'); }
      var wq = wo.reduce(function (s, x) { return s + (Number(x.o.lots) || 0); }, 0);
      var wside = wo.length ? wo[0].o.side.toLowerCase() : '';
      var held = wo.some(function (x) { return x.o.status === 'held'; });
      var isLast = lp && lp.level != null && Math.abs(lp.level - r.level) < (inc || 1) / 2;
      var bidTxt = r.bid_size != null ? r.bid_size : (r.is_best_bid ? '▲' : '');
      var askTxt = r.ask_size != null ? r.ask_size : (r.is_best_ask ? '▼' : '');
      return '<tr class="' + cls.join(' ') + '" data-level="' + r.level + '">' +
        '<td class="work' + (wo.length ? ' ' + wside : '') + (held ? ' held' : '') + '"' +
          (wo.length ? ' data-wo="' + wo.map(function (x) { return x.o.id; }).join(',') + '" title="' + esc(woTip[r.level]) + '"' : '') +
          '>' + (wq ? wq + wo.map(function (x) { return x.arrow; }).join('') : '') + '</td>' +
        '<td data-col="bid" class="bid' + (r.is_best_bid ? ' has-qty' : '') + '" title="' + esc(bidTip + num(r.level, dig)) + '">' +
          (inBid ? bidTxt : '') + '</td>' +
        '<td class="price' + (isLast ? ' last-trade' : '') + '">' + Number(r.level).toFixed(dig) + '</td>' +
        '<td data-col="ask" class="ask' + (r.is_best_ask ? ' has-qty' : '') + '" title="' + esc(askTip + num(r.level, dig)) + '">' +
          (inAsk ? askTxt : '') + '</td>' +
        '<td class="ltq' + (isLast ? ' print' : '') + '">' + (isLast && lp.lots != null ? Number(lp.lots).toFixed(2) : '') + '</td></tr>';
    }).join('');
  }

  // ALGO BOX ----------------------------------------------------------------
  // Mirror's per-pair floating window: "Fair spread" and "Exit", B and S.
  var MIRROR_FAIR =
    '<div class="titlebar"><span class="swatch"></span><span class="title fw-pair"></span>' +
    '<span class="winbtns"><button class="winbtn fw-cog" title="This ladder&#8217;s own settings">&#9881;</button>' +
    '<button class="winbtn close" title="Close — and turn this window off for this ladder">&times;</button></span></div>' +
    '<div class="fw-body">' +
    '<div class="fair" title="What the carry says this basis should be. A reference reading — it feeds nothing.">' +
    '<label class="rail-label">Fair spread<button type="button" class="cog open-exits" title="Settings — this ladder&#8217;s own settings">&#9881;</button></label>' +
    '<table class="exits fairs"><tr class="heads"><th></th><th class="col-b" title="Buying the spread">B</th><th class="col-s" title="Selling the spread">S</th></tr>' +
    '<tr><th>Fair</th><td class="fair-buy">—</td><td class="fair-sell">—</td></tr>' +
    '<tr><th>Gap</th><td class="gap-buy">—</td><td class="gap-sell">—</td></tr></table>' +
    '<div class="fair-kind hint"></div><div class="fair-note hint"></div>' +
    '<div class="fair-warn" hidden><span class="fair-warn-text"></span><button type="button" class="fair-fix" hidden></button></div></div>' +
    '<div class="exit"><label class="rail-label">Exit<button type="button" class="cog open-exits" title="Set commission, the slippage allowance and the target — Settings">&#9881;</button></label>' +
    '<table class="exits"><tr><th>B-A Sp</th><td class="x-width" colspan="2">—</td></tr>' +
    '<tr><th>Comm</th><td class="x-comm" colspan="2">—</td></tr>' +
    '<tr hidden><th>Slip</th><td class="x-slip" colspan="2">—</td></tr>' +
    '<tr hidden><th>Swap</th><td class="x-swap" colspan="2">—</td></tr>' +
    '<tr><th>P% on M</th><td class="x-target" colspan="2">—</td></tr>' +
    '<tr hidden><th title="target distance in spread, against the session range">Reach</th><td class="x-reach" colspan="2">—</td></tr>' +
    '<tr class="heads"><th></th><th class="col-b" title="Buying the spread">B</th><th class="col-s" title="Selling the spread">S</th></tr>' +
    '<tr><th>B/E</th><td class="be-buy">—</td><td class="be-sell">—</td></tr>' +
    '<tr><th>TP</th><td class="tp-buy">—</td><td class="tp-sell">—</td></tr>' +
    '<tr><th>o/n</th><td class="x-overnight" colspan="2">—</td></tr>' +
    '<tr><th>Auto</th><td class="auto-route-state" colspan="2">off</td></tr></table>' +
    '<div class="exit-note hint"></div></div></div>';

  // Mirror's digits: as many as the ladder's increment needs.
  function digitsFor(increment) {
    if (!increment) { return 4; }
    return Math.min(6, Math.max(0, Math.ceil(-Math.log10(increment))));
  }
  function fmtD(v, d) { return v == null || !isFinite(v) ? DASH : Number(v).toFixed(d); }

  BUILD.fair = function (node, ctx) {
    var S = ctx;
    node.querySelector('.close').onclick = function () { closeWindow('fair:' + S.key); };
    Array.prototype.forEach.call(node.querySelectorAll('.fw-cog, .open-exits'), function (b) {
      b.onclick = function () { setActive(S.key); openWindow('settings'); };
    });
  };
  function refreshFair(ctx) {
    var S = ctx;
    if (!S.fairWin) { return Promise.resolve(); }
    var node = S.fairWin.node;
    return getJSON(S.base + '/api/desk/exit').then(function (d) {
      var dig = digitsFor(S.ladder && S.ladder.increment);
      function set(sel, txt, tip) {
        var el = $(sel, node); if (!el) { return; }
        el.textContent = txt; if (tip != null) { el.title = tip; }
      }
      $('.fw-pair', node).textContent = pairName(S);
      // Fair spread — Mirror's two cells and its gap colouring.
      set('.fair-buy', fmtD(d.fair, dig)); set('.fair-sell', fmtD(d.fair, dig));
      ['buy', 'sell'].forEach(function (side) {
        var cell = $('.gap-' + side, node), gap = d['gap_' + side];
        if (gap == null) { cell.textContent = DASH; cell.className = 'gap-' + side; cell.title = ''; return; }
        cell.textContent = (gap > 0 ? '+' : '') + fmtD(gap, dig);
        cell.className = 'gap-' + side + ' ' + (gap > 0 ? 'down' : 'up');
        cell.title = gap > 0 ? 'the market is RICH to its own carry here' : 'the market is CHEAP to its own carry here';
      });
      set('.fair-kind', d.fair_kind || '', d.fair_note || '');
      set('.fair-note', d.days_to_expiry != null ? d.days_to_expiry + 'd to expiry' : '');
      // Exit — Mirror's rows; B/E and TP for a buy at the ask and a sell at the bid.
      set('.x-width', fmtD(d.width, dig), 'one round turn of both legs’ bid-ask, measured live');
      set('.x-comm', d.cost_inr == null ? DASH : inr(d.cost_inr, 2),
          'the round-trip cost of ' + (d.lots || 1) + ' lot(s): brokerage, slippage allowance, STT and charges');
      set('.x-target', d.target_money == null ? DASH : inr(d.target_money, 2),
          d.target_pct ? d.target_pct + '% of ' + inr(d.margin, 2) + ' margin'
                       : 'the algo exits at the mean (z ' + (d.exit_z == null ? '0.0' : d.exit_z) + '); set a ₹ profit target in Settings for a fixed one');
      var b = d.buy || {}, s2 = d.sell || {};
      set('.be-buy', fmtD(b.break_even, dig), 'bid — a long leaves on the bid, so this is the bid you need back');
      set('.be-sell', fmtD(s2.break_even, dig), 'ask — a short buys back at the offer, so this is the ask you need');
      set('.tp-buy', fmtD(b.target, dig)); set('.tp-sell', fmtD(s2.target, dig));
      set('.x-overnight', d.overnight || DASH, 'what happens to an OPEN POSITION at the session cutoff');
      var a = $('.auto-route-state', node);
      var t = S.status && S.status.open_trade, lv = t && t.spread_levels;
      var out = t && S.algo && S.algo.running && lv && lv.take_profit != null ? lv.take_profit : null;
      a.textContent = out != null ? 'out ' + fmtD(out, dig) : (S.algo && S.algo.running ? 'on' : 'off');
      a.className = 'auto-route-state' + (out != null ? ' armed' : '');
      a.title = S.algo && S.algo.running ? 'on: the algo enters and exits this pair by itself' : 'off';
      var short = d.target_pct && d.margin ? d.target_pct + '% of ' + inr(d.margin, 0)
                : (b.break_even != null ? 'B/E only' : '');
      if (t && lv && (lv.take_profit != null || lv.break_even != null)) {
        short = t.position_type[0] + ' out ' + fmtD(lv.take_profit != null ? lv.take_profit : lv.break_even, 4);
      }
      set('.exit-note', short);
    }).catch(function () {});
  }

  // ALGO WINDOW --------------------------------------------------------------
  // Mirror-MT5's Algo window, one per ladder: SIGNAL & POSITION, STATISTICS,
  // FILTERS, the last signal blocked, the last order, and a backtest — filled
  // from this pair's engine (/api/desk/algo). Nothing in it trades.
  var MIRROR_ALGO =
    '<div class="titlebar"><span class="swatch"></span><span class="title aw-pair"></span><span class="aw-mode"></span>' +
    '<span class="winbtns"><button class="winbtn aw-cog" title="This ladder&#8217;s Algo settings">&#9881;</button>' +
    '<button class="winbtn close" title="Close this window — the Algo itself is switched in the ladder&#8217;s title bar">&times;</button></span></div>' +
    '<div class="aw-body"><div class="aw-section aw-signal"></div><div class="aw-section aw-stats"></div>' +
    '<div class="aw-section aw-filters"></div>' +
    '<div class="aw-section aw-backtest"><div class="aw-head">Backtest</div><div class="aw-bt-controls">' +
    '<select class="aw-bt-days" title="How much history, from Arrow\'s own candles"><option value="3">last 3 days</option>' +
    '<option value="5" selected>last 5 days</option><option value="10">last 10 days</option></select>' +
    '<button type="button" class="aw-bt-run" title="Replay the history through this ladder\'s Algo, with its settings as they are now. Nothing is sent.">Run backtest</button></div>' +
    '<div class="aw-bt-out hint">What these settings would have done — nothing is sent.</div></div></div>';

  function setAlgo(ctx, on) {
    var live = (ctx.algo || {}).mode === 'live';
    var go = on && live
      ? confirmBox('Algo LIVE — ' + pairName(ctx), 'The Algo will place REAL orders on ' + pairName(ctx) +
                   ' by itself, and manual orders on its ladder go off.', 'Turn LIVE on', 'sell')
      : Promise.resolve(true);
    go.then(function (ok) {
      if (!ok) { return; }
      postJSON(ctx.base + (on ? '/api/algo/start' : '/api/algo/stop'), {}).then(function (r) {
        if (r && r.success === false) { toast('Algo ' + (on ? 'not started' : 'not stopped') + ': ' + (r.error || 'refused'), 'danger'); }
        else { toast(pairName(ctx) + ' — Algo ' + (on ? 'ON' : 'OFF'), 'info'); }
        if (on && !ctx.algoWin) { openWindow('algo:' + ctx.key); }
        pollAlgo(ctx); refreshAlgo(ctx);
      });
    });
  }

  BUILD.algo = function (node, ctx) {
    var S = ctx;
    node.querySelector('.close').onclick = function () { closeWindow('algo:' + S.key); };
    $('.aw-cog', node).onclick = function () {
      if (S.win) { lsOpen(S.win.node, S); } else { setActive(S.key); openWindow('settings'); }
    };
    $('.aw-bt-run', node).onclick = function () {
      var days = parseInt($('.aw-bt-days', node).value, 10) || 5;
      var out = $('.aw-bt-out', node), btn = $('.aw-bt-run', node);
      out.className = 'aw-bt-out hint'; out.textContent = 'running on the last ' + days + ' days…'; btn.disabled = true;
      getJSON(S.base + '/api/desk/backtest?days=' + days, true).then(function (d) {
        btn.disabled = false;
        if (!d.ok) { out.className = 'aw-bt-out down'; out.textContent = 'could not run: ' + (d.reason || 'unknown'); return; }
        out.className = 'aw-bt-out'; out.innerHTML = backtestHtml(d);
      }).catch(function (e) { btn.disabled = false; out.className = 'aw-bt-out down'; out.textContent = String(e); });
    };
  };

  function refreshAlgo(ctx) {
    var S = ctx;
    if (!S.algoWin) { return Promise.resolve(); }
    return getJSON(S.base + '/api/desk/algo').then(function (b) { renderAlgoWindow(S.algoWin.node, S, b); })
      .catch(function () {});
  }

  function fmt2(v, d) { return v == null || !isFinite(v) ? DASH : Number(v).toFixed(d == null ? 4 : d); }
  function zs(v) { return v == null || isNaN(v) ? DASH : (v > 0 ? '+' : '') + Number(v).toFixed(2); }
  function moneyOr(v) { return v == null ? DASH : inr(v, 2); }
  function kv(label, value, cls, title, wide) {
    return '<div class="aw-kv' + (wide ? ' wide' : '') + '"' + (title ? ' title="' + esc(title) + '"' : '') +
      '><span>' + label + '</span><b' + (cls ? ' class="' + cls + '"' : '') + '>' + value + '</b></div>';
  }
  function awBadge(text, tone, title) {
    return '<span class="aw-badge ' + tone + '"' + (title ? ' title="' + esc(title) + '"' : '') + '>' + text + '</span>';
  }
  function sideWords(side) { return side === 'SELL' ? 'H to L' : (side === 'BUY' ? 'L to H' : (side || '')); }
  var ALGO_EXIT_WORDS = {target: 'back to the mean', stop: 'z-stop', profit_target: 'profit target',
                         dollar_stop: 'stop loss', time_stop: 'time stop', hard_time_stop: 'hard time stop',
                         trailing_stop: 'trailing stop'};
  function levelWords(level, worth, d) {
    return fmt2(level, d) + (worth == null ? '' : ' (' + (worth > 0 ? '+' : '') + inr(worth, 2) + ')');
  }
  function positionWords(p, d) { return (p.side || '?') + ' @ ' + fmt2(p.entry_spread, d); }

  function algoLine(b, d) {
    if (b.state === 'IN_POSITION') {
      var f = (b.positions || [])[0] || {};
      return 'in ' + positionWords(f, d) + ' — TP ' + (f.tp == null ? DASH : levelWords(f.tp, f.tp_money, d)) +
        ' · SL ' + (f.sl == null ? ((b.params || {}).stop_loss_on ? DASH : 'off') : levelWords(f.sl, f.sl_money, d));
    }
    if (b.state === 'BLOCKED') { return 'held: ' + (b.blocked || b.status || ''); }
    if (b.state === 'OFF') { return 'Algo off — switch it on in the ladder’s title bar'; }
    return b.status && b.status !== 'stopped' ? b.status : 'watching';
  }
  function warmupHtml(w) {
    if (!w || !w.need_sec) { return ''; }
    var done = !!w.done, pct = done ? 100 : Math.floor(100 * w.sec / w.need_sec);
    return '<div class="aw-data aw-warmup" title="No entry until the Algo has watched this long of live prices. Time with no price does not count.">' +
      '<span>Live</span><div class="aw-bar"><div class="' + (done ? 'ok' : 'wait') + '" style="width:' + pct + '%"></div></div>' +
      '<span>' + (done ? 'warmed up' : 'warming up ' + Math.floor(w.sec / 60) + '/' + Math.round(w.need_sec / 60) + ' min') + '</span></div>';
  }
  function progressHtml(f, d) {
    var entry = f.entry_spread;
    var toTp = f.tp == null ? null : Math.abs(f.tp - entry);
    var toSl = f.sl == null ? null : Math.abs(entry - f.sl);
    var split = (toTp && toSl) ? toSl / (toSl + toTp) : (toSl ? 0.5 : 0);
    var p = f.progress;
    var at = p == null ? null : (p >= 0 ? split + p * (1 - split) : split + p * split);
    var fill = at === null ? '' : '<div class="ap-fill ' + (p >= 0 ? 'up' : 'down') + '" style="left:' +
      (Math.min(at, split) * 100) + '%;width:' + (Math.abs(at - split) * 100) + '%"></div>' +
      '<div class="ap-mark" style="left:calc(' + (at * 100) + '% - 1px)"></div>';
    return '<div class="algo-progress" title="' + esc(positionWords(f, d) + ', closing at ' + fmt2(f.closing_spread, d)) + '">' +
      '<div class="ap-pct">' + (p == null ? DASH : (p >= 0 ? Math.round(p * 100) + '% to TP' : Math.round(-p * 100) + '% to SL')) + '</div>' +
      '<div class="ap-track">' + fill + '<div class="ap-entry" style="left:' + (split * 100) + '%"></div></div>' +
      '<div class="ap-ends"><span>' + (toSl === null ? 'no SL' : 'SL ' + levelWords(f.sl, f.sl_money, d)) + '</span><span>' +
      (toTp === null ? 'no TP' : 'TP ' + levelWords(f.tp, f.tp_money, d)) + '</span></div></div>';
  }
  function lastOrderHtml(b) {
    var last = (b.recent || [])[0];
    var html = '<div class="aw-blocked aw-last-order"><div class="aw-head2">Last order</div>';
    if (!last) { return html + '<div class="hint">none yet</div></div>'; }
    var what = esc(last.action || '') + ' ' + esc(sideWords(last.side)) + (last.z == null ? '' : ' z ' + zs(last.z)) +
      ' · ' + new Date(last.at * 1000).toLocaleTimeString();
    var outcome = last.mode !== 'LIVE' ? '<div class="hint">dry-run — nothing sent</div>'
      : (last.done ? '<div class="up">sent — done</div>' : '<div class="down">REFUSED — ' + esc(last.result || 'no reason given') + '</div>');
    return html + '<div><b>' + what + '</b></div>' + outcome + '</div>';
  }
  function backtestHtml(data) {
    var s = data.summary || {}, plain = data.without_protections || {};
    function line(x) {
      return (x.trades || 0) + ' trade(s) · ' + (x.wins || 0) + ' won, ' + (x.losses || 0) + ' lost · net ' +
        moneyOr(x.net) + ' · worst run ' + moneyOr(x.max_drawdown);
    }
    function when(at) {
      return at ? new Date(at * 1000).toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : DASH;
    }
    var held = Object.keys(data.held || {}).map(function (r) { return [r, data.held[r]]; })
      .sort(function (a, b) { return b[1] - a[1]; }).slice(0, 3);
    var rows = (data.trades || []).slice(-8).reverse().map(function (t) {
      return '<tr><td>' + when(t.opened_at) + '</td><td>' + sideWords(t.side) + '</td><td>' + zs(t.entry_z) + '</td><td>' +
        esc(ALGO_EXIT_WORDS[t.reason] || t.reason || '') + '</td><td class="' + (t.pnl > 0 ? 'up' : (t.pnl < 0 ? 'down' : '')) + '">' +
        moneyOr(t.pnl) + '</td></tr>';
    }).join('');
    return '<div><b>These settings:</b> ' + line(s) + '</div>' +
      '<div class="hint">With every entry gate off: ' + line(plain) + '</div>' +
      (held.length ? '<div class="hint">Held back most by: ' + held.map(function (h) { return esc(h[0]) + ' (' + h[1] + ')'; }).join('; ') + '</div>' : '') +
      (rows ? '<table class="aw-bt-trades"><tr><th>Entered</th><th>Side</th><th>z</th><th>Exit</th><th>P&amp;L</th></tr>' + rows + '</table>' : '') +
      '<div class="hint" title="' + esc((data.caveats || []).join('; ')) + '">' + (s.candles || 0) + ' candles of Arrow history, ' +
      when(s.from) + ' – ' + when(s.to) + ' · hover for what a backtest cannot see</div>';
  }

  function renderAlgoWindow(node, S, b) {
    var params = b.params || {}, filters = b.filters || {};
    var digits = digitsFor(S.ladder && S.ladder.increment);
    $('.aw-pair', node).textContent = pairName(S) + ' · Algo';
    var mode = $('.aw-mode', node);
    mode.textContent = b.mode === 'OFF' ? 'OFF' : (b.mode === 'LIVE' ? 'LIVE' : 'DRY-RUN');
    mode.className = 'aw-mode ' + (b.mode === 'LIVE' ? 'live' : 'dry');
    mode.style.display = b.mode === 'OFF' ? 'none' : '';
    mode.title = b.mode === 'LIVE' ? 'the Algo TRADES this ladder — manual orders on it are off'
      : 'dry-run: the Algo runs, its orders are simulated — nothing is sent';
    var entryZ = params.entry_z, direction = params.direction || 'BOTH';
    function tile(side) {
      var sell = side === 'SELL';
      var z = sell ? b.z_sell : b.z_buy;
      var hit = z != null && entryZ && (sell ? z >= entryZ : z <= -entryZ);
      var off = (sell && direction === 'L_TO_H') || (!sell && direction === 'H_TO_L');
      return '<div class="aw-tile ' + (sell ? 'sell' : 'buy') + (hit ? ' hit' : '') + (off ? ' off' : '') + '" title="' +
        (sell ? 'What SELLING the spread gets now (the bid side). The Algo sells when this z reaches +' + entryZ + ', and closes a long here.'
              : 'What BUYING the spread costs now (the offer side). The Algo buys when this z reaches −' + entryZ + ', and closes a short here.') +
        '"><div class="aw-tile-head">' + (sell ? 'H to L' : 'L to H') + (off ? ' <small>(entries off)</small>' : '') +
        '</div><div class="aw-tile-price">' + fmt2(sell ? b.sell_spread : b.buy_spread, digits) +
        '</div><div class="aw-tile-z" title="How stretched the spread is: entry at ±' + (entryZ || '?') + '">' + zs(z) + '</div>' +
        entryLine(sell) + '</div>';
    }
    function entryLine(sell) {
      if (!params.reentry_on) {
        return '<div class="aw-tile-entry">' + (sell ? 'short' : 'long') + ' at ' + (sell ? '≥ +' : '≤ −') +
          (entryZ || '?') + ' (' + fmt2(sell ? b.upper : b.lower, digits) + ')</div>';
      }
      var inZ = Math.max(0, (entryZ || 0) - (params.reentry_back || 0));
      var wp = params.reentry_window_pct == null ? 50 : params.reentry_window_pct;
      var outZ = inZ * (1 - wp / 100);
      var priced = !(b.mean == null || !b.sigma);
      var level = priced ? b.mean + (sell ? 1 : -1) * inZ * b.sigma : null;
      var outLevel = priced ? b.mean + (sell ? 1 : -1) * outZ * b.sigma : null;
      var armed = (b.armed || {})[sell ? 'SELL' : 'BUY'];
      return '<div class="aw-tile-entry" title="Armed when the stretch reaches ' + (sell ? '+' : '−') + entryZ +
        '; entered on the way back in, between ' + (sell ? '+' : '−') + inZ.toFixed(2) + ' and ' + (sell ? '+' : '−') +
        outZ.toFixed(2) + '. Past ' + outZ.toFixed(2) + ' it disarms - too close to the mean to trade.">' +
        (armed ? '<b class="aw-armed">ARMED</b> ' : 'arm ' + (sell ? '≥ +' : '≤ −') + entryZ + ' · ') +
        (sell ? 'short' : 'long') + ' back at ' + (sell ? '+' : '−') + inZ.toFixed(2) + ' (' + fmt2(level, digits) + ') to ' +
        (sell ? '+' : '−') + outZ.toFixed(2) + ' (' + fmt2(outLevel, digits) + ')</div>';
    }
    var held = b.positions || [], first = held[0] || null;
    var position = first ? (first.source && first.source !== 'ALGO' ? 'YOUR ' : '') + (first.side === 'BUY' ? 'LONG' : 'SHORT') +
      ' ' + fmt2(first.quantity, 2) : 'FLAT';
    var html = '<div class="aw-head">Signal &amp; Position</div><div class="aw-tiles">' + tile('SELL') + tile('BUY') +
      '<div class="aw-pos ' + (first ? (first.side === 'BUY' ? 'long' : 'short') : 'flat') + '">' + position + '</div></div>' +
      '<div class="aw-line" title="' + esc(b.blocked || b.status || '') + '">' + esc(algoLine(b, digits)) + '</div>';
    if (first) {
      var delta = (first.closing_spread == null || first.entry_spread == null) ? null : first.closing_spread - first.entry_spread;
      var good = delta === null ? '' : ((first.side === 'BUY' ? delta >= 0 : delta <= 0) ? 'up' : 'down');
      function legRow(leg) {
        var en = first['leg_' + leg + '_entry'], now = first['leg_' + leg + '_now'];
        var ch = (en == null || now == null) ? null : now - en;
        return kv('Leg ' + leg.toUpperCase() + ' (' + (first['leg_' + leg + '_side'] || '?') + ')',
                  fmt2(en, 2) + ' → ' + fmt2(now, 2) + (ch === null ? '' : ' <small>(' + (ch > 0 ? '+' : '') + fmt2(ch, 2) + ')</small>'),
                  '', 'the leg’s fill → the price it would close at now', true);
      }
      html += '<div class="aw-grid">' + kv('Entry spread', fmt2(first.entry_spread, digits)) +
        kv('Δ spread', delta === null ? DASH : (delta > 0 ? '+' : '') + fmt2(delta, digits), good,
           'closing price now − entry: a LONG makes money when this is positive, a SHORT when it is negative') +
        kv('Entry z', zs(first.entry_z)) +
        kv('Net P&amp;L', moneyOr(first.net_pnl), first.net_pnl == null ? '' : (first.net_pnl >= 0 ? 'up' : 'down')) +
        legRow('a') + legRow('b') +
        kv('Age', first.age_sec == null ? DASH : Math.floor(first.age_sec / 60) + 'm ' + Math.floor(first.age_sec % 60) + 's') +
        kv('Size', fmt2(first.quantity, 2) + ' lot(s)') + '</div>' +
        kv('Levels', 'BE ' + fmt2(first.break_even, digits) + ' · TP ' + (first.tp == null ? DASH : levelWords(first.tp, first.tp_money, digits)) +
           ' · SL ' + (first.sl == null ? (params.stop_loss_on ? DASH : 'off') : levelWords(first.sl, first.sl_money, digits)) +
           (first.stop_mode === 'ATR' || first.target_mode === 'ATR'
             ? ' <small>(ATR ' + fmt2(first.entry_atr, digits) + ' at entry: ' +
               (first.target_mode === 'ATR' ? 'TP ' + first.atr_target_mult + '×' : 'TP ₹') + ', ' +
               (first.stop_mode === 'ATR' ? 'SL ' + first.atr_stop_mult + '×' : 'SL ₹') + ')</small>' : ''), '',
           'compare with the CLOSING price: the Sell spread for a LONG, the Buy spread for a SHORT') + progressHtml(first, digits);
    }
    $('.aw-signal', node).innerHTML = html;

    var regime = filters.regime || {};
    var count = b.count || 0, needed = b.needed || 0;
    var pct = needed ? Math.min(100, Math.round(count * 100 / needed)) : 0;
    var regimeText = regime.state === 'TRENDING' ? 'Trend ' + ((regime.slope || 0) > 0 ? '↑' : '↓')
      : regime.state === 'RANGE' ? 'Mean-rev' : regime.state === 'COLLECTING' ? 'Collect' : DASH;
    var candles = b.timeframe && b.timeframe !== 'ticks';
    $('.aw-stats', node).innerHTML = '<div class="aw-head">Statistics</div><div class="aw-grid">' +
      kv('Mean (EMA)', fmt2(b.mean, digits)) + kv('Std dev', fmt2(b.sigma, digits)) +
      kv('ATR(' + (b.atr_period || 14) + ')', fmt2(b.atr, digits), '', 'the average size of one candle’s move in the spread (Wilder)') +
      kv('Half-life', filters.half_life_minutes == null ? DASH : Math.round(filters.half_life_minutes) + ' min', '',
         'how long a stretch takes to halve') +
      kv('Regime', regimeText, regime.state === 'TRENDING' ? 'down' : (regime.state === 'RANGE' ? 'up' : ''),
         regime.efficiency_ratio == null ? '' : 'efficiency ' + Number(regime.efficiency_ratio).toFixed(2) + ', ' + regime.crossings + ' mean crossings') +
      kv('Candles', candles ? b.timeframe + ' × ' + needed : 'ticks') +
      kv('Band', fmt2(b.lower, digits) + ' … ' + fmt2(b.upper, digits)) +
      '</div><div class="aw-data"><span>Data</span><div class="aw-bar"><div class="' + (b.ready ? 'ok' : 'wait') +
      '" style="width:' + (b.ready ? 100 : pct) + '%"></div></div><span>' +
      (b.ready ? (candles ? 'candles ready' : 'ready') : count + '/' + needed + (candles ? ' candles' : ' samples')) +
      '</span></div>' + warmupHtml(b.warmup);

    var edge = filters.edge || {}, cost = filters.cost || {}, band = filters.half_life_band || [0, 0];
    function onOff(on, ok, yes, no, why) {
      if (!on) { return awBadge('OFF', 'off', why); }
      if (ok == null) { return awBadge('—', 'wait', why); }
      return awBadge(ok ? yes : no, ok ? 'ok' : 'bad', why);
    }
    var trending = regime.state === 'TRENDING';
    var day = b.day || {}, last = b.last_blocked;
    $('.aw-filters', node).innerHTML = '<div class="aw-head">Filters</div><div class="aw-badges">' +
      '<div>' + onOff(edge.on, edge.ok, '✓', '✗', 'expected capture against the round-trip cost, at the entry z') + '<small>Edge</small></div>' +
      '<div>' + (regime.on ? awBadge(trending ? 'TR' : (regime.state === 'RANGE' ? 'MR' : 'WAIT'),
                                     trending ? 'bad' : (regime.state === 'RANGE' ? 'ok' : 'wait')) : awBadge('OFF', 'off')) + '<small>Regime</small></div>' +
      '<div>' + awBadge('OFF', 'off', 'Arrow-StatArb has no trend filter') + '<small>Trend</small></div>' +
      '<div>' + awBadge(filters.ready ? 'YES' : 'NO', filters.ready ? 'ok' : 'wait') + '<small>Ready</small></div>' +
      ((band[0] || band[1]) ? '<div>' + awBadge(Math.round(band[0] || 0) + '–' + (band[1] ? Math.round(band[1]) : '∞'), 'off') +
        '<small>HL min</small></div>' : '') +
      '</div><div class="aw-grid">' +
      kv('Capture / cost', (edge.ratio == null ? DASH : Number(edge.ratio).toFixed(2) + '×') + ' / req ' +
         (edge.required == null ? DASH : edge.required + '×'), edge.ok === true ? 'up' : (edge.ok === false ? 'down' : ''),
         'expected capture ' + moneyOr(edge.capture) + ' (' + (params.edge_capture_frac || 0.5) + ' × entry z × σ) against the round trip, at the entry z', true) +
      kv('Round trip', moneyOr(cost.total), '', 'brokerage, STT and charges ' + moneyOr(cost.commission) + ' + slippage allowance ' + moneyOr(cost.slippage)) +
      kv('Today', (day.trades || 0) + (params.max_trades_day ? '/' + params.max_trades_day : '') + ' trades · ' +
         (day.losses_row || 0) + ' in a row · ' + moneyOr(day.pnl), '', 'the day’s limits stop entries, never exits', true) +
      kv('Size', fmt2(filters.qty || params.algo_qty || 1, 2) + ' lot(s)') +
      '</div><div class="aw-blocked"><div class="aw-head2">Last signal blocked</div>' + (last
        ? '<div><b>' + esc(sideWords(last.side)) + '</b> z ' + zs(last.z) + ' · ' + new Date(last.at * 1000).toLocaleTimeString() +
          '</div><div>' + esc(last.reason || '') + '</div>'
        : '<div class="hint">none yet</div>') + '</div>' + lastOrderHtml(b);
  }

  // WORKING ORDERS ---------------------------------------------------------------
  BUILD.workorders = function (body) {
    body.innerHTML = '<div style="padding:4px 8px;display:flex;gap:6px;align-items:center">' +
      '<span class="muted wo-sum">—</span><button class="btn wo-all" style="margin-left:auto">Cancel all</button></div>' +
      '<table class="dtable"><thead><tr><th></th><th>Side</th><th class="num">Lots</th><th class="num">Price</th>' +
      '<th>Fires when</th><th>Status</th></tr></thead><tbody class="wo-act"></tbody></table>' +
      '<div class="sec" style="margin:6px 8px 2px">Recent</div>' +
      '<table class="dtable"><tbody class="wo-rec"></tbody></table>';
    $('.wo-all', body).onclick = function () { cancelAllWorking('Cancel all in the Working Orders window'); };
    // The ✕ cancels the order that was under the pointer when it was PRESSED:
    // the id is taken on mousedown and must match on click.
    var pressed = null;
    $('.wo-act', body).onmousedown = function (e) {
      var b = e.target.closest('button[data-id]');
      pressed = b ? b.dataset.id : null;
    };
    $('.wo-act', body).onclick = function (e) {
      var b = e.target.closest('button[data-id]');
      if (b && pressed && b.dataset.id === pressed) { cancelWorking([pressed], '✕ in the Working Orders window'); }
      pressed = null;
    };
  };
  REFRESH.workorders = function () {
    if (!W.workorders || !S.wo) { return Promise.resolve(); }
    var body = W.workorders.body, lad = S.ladder || {};
    var act = S.wo.active || [];
    $('.wo-sum', body).textContent = act.length + ' working · GTC · manual';
    // Rebuild the rows ONLY when an order is added, removed or changes state —
    // a list redrawn every second moves the ✕ buttons under the pointer. The
    // live price is updated in place.
    var key = act.map(function (o) { return o.id + o.status + o.note; }).join('|');
    var tb = $('.wo-act', body);
    if (tb.dataset.key === key && act.length) {
      Array.prototype.forEach.call(tb.querySelectorAll('.wo-now'), function (el) {
        var o = act.filter(function (x) { return x.id === el.dataset.id; })[0];
        if (o) { el.textContent = '(' + num(o.side === 'SELL' ? lad.sell_spread : lad.buy_spread, 2) + ')'; }
      });
    } else {
    tb.dataset.key = key;
    tb.innerHTML = act.map(function (o) {
      var now = o.side === 'SELL' ? lad.sell_spread : lad.buy_spread;
      var cond = (o.side === 'SELL' ? 'Sell ≥ ' : 'Buy ≤ ') + num(o.price, 2) +
        ' <span class="muted wo-now" data-id="' + esc(o.id) + '">(' + num(now, 2) + ')</span>';
      return '<tr><td><button class="btn wo-x" data-id="' + esc(o.id) + '" title="Cancel ' + o.side + ' ' + o.lots +
          ' @ ' + num(o.price, 2) + '">✕</button></td>' +
        '<td class="' + (o.side === 'SELL' ? 'down' : '') + '"><b>' + o.side + '</b></td>' +
        '<td class="num">' + o.lots + '</td><td class="num">' + num(o.price, 2) + '</td><td>' + cond + '</td>' +
        '<td title="' + esc(o.note || '') + '">' + (o.status === 'held' ? '<span class="pill warn">HELD</span> ' +
          esc(o.note) : '<span class="pill ok">WORKING</span>') + '</td></tr>';
    }).join('') || '<tr><td colspan="6" class="muted" style="text-align:center;padding:8px">' +
      'No working orders — click any price on the ladder to place one</td></tr>';
    }
    $('.wo-rec', body).innerHTML = (S.wo.recent || []).slice(0, 8).map(function (o) {
      return '<tr><td>' + o.side + ' ' + o.lots + ' @ ' + num(o.price, 2) + '</td><td><span class="pill ' +
        (o.status === 'filled' ? 'ok' : o.status === 'rejected' ? 'bad' : '') + '">' + o.status.toUpperCase() +
        '</span></td><td class="muted" style="white-space:normal">' + esc(o.note || '') + '</td></tr>';
    }).join('') || '<tr><td class="muted">—</td></tr>';
    return Promise.resolve();
  };

  // SIGNAL & POSITION ----------------------------------------------------------
  BUILD.signal = function (body) {
    body.innerHTML =
      '<div class="sides">' +
      ' <div class="side sell"><div class="lbl">SELL SPREAD <small>Bid A − Ask B</small></div>' +
      '   <div class="px s-px">—</div><div class="zl">Z-SCORE</div><div class="z s-z">—</div>' +
      '   <div class="en s-en"></div><div class="en">short entry · long exit</div></div>' +
      ' <div class="side buy"><div class="lbl">BUY SPREAD <small>Ask A − Bid B</small></div>' +
      '   <div class="px b-px">—</div><div class="zl">Z-SCORE</div><div class="z b-z">—</div>' +
      '   <div class="en b-en"></div><div class="en">long entry · short exit</div></div>' +
      '</div>' +
      '<div class="sec">Position</div><div class="pos-box muted">flat</div>' +
      '<div style="margin-top:6px"><button class="btn pos-close" data-manual data-tip="Close the open position at market">Close position</button>' +
      ' <span class="muted algo-status"></span></div>';
    $('.pos-close', body).onclick = function () { closePosition(S); };
    applyLock();
  };

  REFRESH.signal = function () {
    if (!W.signal || !S.status) { return; }
    var body = W.signal.body, g = S.status.signal || {};
    var entry = Number(g.entry_threshold || 2);
    var td = g.trade_direction || 'both';
    function side(cls, px, z, armed, on, enTxt) {
      var box = $('.side.' + cls, body);
      $('.' + cls[0] + '-px', body).textContent = num(px, 2);
      var zEl = $('.' + cls[0] + '-z', body);
      zEl.textContent = z == null ? DASH : signed(z, 2);
      zEl.className = 'z ' + cls[0] + '-z' + (armed && on ? ' hot ' + cls : '');
      var en = $('.' + cls[0] + '-en', body);
      en.textContent = on ? enTxt : 'entries OFF (' + (cls === 'sell' ? 'Buy' : 'Sell') + ' spread only)';
      en.className = 'en ' + cls[0] + '-en' + (on ? '' : ' offline');
      box.className = 'side ' + cls + (armed && on ? ' armed' : '') + (on ? '' : ' off');
    }
    side('sell', g.sell_spread, g.z_sell, g.z_sell != null && g.z_sell >= entry, td !== 'buy_only',
         'short at ≥ +' + entry.toFixed(2));
    side('buy', g.buy_spread, g.z_buy, g.z_buy != null && g.z_buy <= -entry, td !== 'sell_only',
         'long at ≤ −' + entry.toFixed(2));
    var t = S.status.open_trade;
    var pb = $('.pos-box', body);
    if (!t) {
      pb.className = 'pos-box muted'; pb.textContent = 'flat';
    } else {
      var lv = t.spread_levels || {};
      var closePx = t.position_type === 'LONG' ? g.sell_spread : g.buy_spread;
      var dlt = (closePx != null && t.entry_spread != null) ? closePx - t.entry_spread : null;
      var good = dlt == null ? '' : ((t.position_type === 'LONG') === (dlt >= 0) ? 'up' : 'down');
      pb.className = 'pos-box';
      pb.innerHTML =
        '<div style="margin-bottom:4px"><span class="owner ' + String(t.owner || '').toLowerCase() + '">' +
        esc(t.owner || '') + '</span> <b>' + esc(t.position_type) + '</b> ' + t.quantity + ' lot(s)' +
        ' · closes on the <b>' + (t.position_type === 'LONG' ? 'Sell' : 'Buy') + '</b> spread</div>' +
        '<div class="kv">' +
        '<span>Entry spread</span><span>' + num(t.entry_spread, 2) + '</span>' +
        '<span>Closing price now</span><span>' + num(closePx, 2) + '</span>' +
        '<span>Δ spread</span><span class="' + good + '">' + signed(dlt, 2) + '</span>' +
        '<span>Net P&amp;L</span><span class="' + (t.unrealized_pnl >= 0 ? 'up' : 'down') + '">' + inr(t.unrealized_pnl, 0) + '</span>' +
        '<span>Levels</span><span>BE ' + num(lv.break_even, 2) + ' · TP ' + num(lv.take_profit, 2) + ' · SL ' + num(lv.stop, 2) + '</span>' +
        '<span>Target / Stop (₹)</span><span>' + inr(t.exit_target_usd) + ' / ' + inr(t.exit_stop_usd) + '</span>' +
        '<span>Entry z</span><span>' + (t.entry_zscore == null ? DASH : signed(t.entry_zscore, 2)) + '</span>' +
        '</div>';
    }
    var a = S.algo || {};
    $('.algo-status', body).textContent = a.running ? ('algo: ' + (a.status || '')) : '';
  };

  // STATISTICS & FILTERS -------------------------------------------------------
  BUILD.stats = function (body) {
    body.innerHTML =
      '<div class="sec" style="margin-top:0">Lookback (warm-up)</div>' +
      '<div class="bar st-bar"><i></i></div><div class="muted st-warm" style="margin:3px 0 6px"></div>' +
      '<div class="kv st-kv"></div>' +
      '<div class="sec">Edge filter</div><div class="kv st-edge"></div>' +
      '<div class="sec">Algo</div><div class="st-algo muted"></div>';
  };

  REFRESH.stats = function () {
    if (!W.stats || !S.status) { return; }
    var body = W.stats.body, g = S.status.signal || {};
    var have = g.history_sec || 0, need = g.min_history_sec || 0;
    var pct = need ? Math.min(100, have / need * 100) : 100;
    var bar = $('.st-bar', body);
    bar.classList.toggle('ready', !!g.data_ready);
    bar.firstChild.style.width = (g.data_ready ? 100 : pct) + '%';
    var TFS = {'5m': 300, '15m': 900, '1h': 3600, '4h': 14400};
    if (g.band_source === 'candles') {
      // candle mode: the gate is N candles of the chosen timeframe
      var sec = TFS[g.band_timeframe] || 900;
      var hc = Math.round(have / sec), nc = Math.round(need / sec);
      $('.st-warm', body).textContent = (g.data_ready
        ? 'Ready — ' + hc + ' candles (' + g.band_timeframe + ', need ' + nc + ')'
        : hc + ' of ' + nc + ' candles (' + g.band_timeframe + ') — loading history');
    } else {
      $('.st-warm', body).textContent = g.data_ready
        ? 'Ready — ' + Math.floor(have / 60) + ' min sampled'
        : Math.floor(have / 60) + ' of ' + Math.round(need / 60) + ' min sampled' +
          (g.sample_status ? ' · not sampling: ' + g.sample_status : '');
    }
    $('.st-kv', body).innerHTML =
      '<span>Mean</span><span>' + num(g.spread_mean, 2) + '</span>' +
      '<span>Std dev (σ)</span><span>' + num(g.spread_std, 3) + '</span>' +
      '<span>Half-life</span><span>' + (g.half_life == null ? DASH : g.half_life.toFixed(1) + ' min') + '</span>' +
      '<span>Tick (measured)</span><span>' + (g.measured_interval_sec == null ? DASH : Number(g.measured_interval_sec).toFixed(2) + 's') + '</span>' +
      '<span>Quotes / min</span><span>' + (g.quote_rate_per_min == null ? DASH : g.quote_rate_per_min) + '</span>' +
      '<span>Regime</span><span>' + esc(g.regime || DASH) + '</span>' +
      '<span>Trade direction</span><span>' + esc({both: 'Both', sell_only: 'High → Low only', buy_only: 'Low → High only'}[g.trade_direction] || DASH) + '</span>';
    var ok = g.std_filter_ok;
    $('.st-edge', body).innerHTML =
      '<span>Capture ÷ cost</span><span>' + (g.std_ratio == null ? DASH : g.std_ratio.toFixed(2) + '×') +
        ' / req ' + (g.std_ratio_required == null ? DASH : g.std_ratio_required + '×') + '</span>' +
      '<span>Round-trip cost</span><span>' + inr(g.round_trip_cost_usd) + '</span>' +
      '<span>Expected capture</span><span>' + inr(g.expected_capture_usd) + '</span>' +
      '<span>Status</span><span><span class="pill ' + (ok === true ? 'ok' : ok === false ? 'bad' : '') + '">' +
        (ok === true ? 'OK' : ok === false ? 'BLOCKED' : DASH) + '</span></span>';
    var a = S.algo || {};
    $('.st-algo', body).textContent = (a.running ? 'ON · ' : 'OFF · ') + (a.status || DASH);
  };

  // BOLLINGER BANDS ------------------------------------------------------------
  // The spread's candles with the TradingView BB (EMA basis): close, EMA(N),
  // EMA ± entry·σ — the current candle included and marked. The timeframe
  // picker is a VIEW; the badge says which bands the algo actually trades.
  var bandsChart = null;
  BUILD.bands = function (body) {
    body.innerHTML =
      '<div class="bb-bar" style="display:flex;gap:6px;align-items:center;font-size:10px;height:20px">' +
      '  <select class="bb-tf" style="font-size:10px"><option value="">algo timeframe</option>' +
      '    <option value="5m">5 min</option><option value="15m">15 min</option>' +
      '    <option value="1h">1 hour</option><option value="4h">4 hours</option></select>' +
      '  <select class="bb-last" style="font-size:10px"><option value="40">40 candles</option>' +
      '    <option value="80">80 candles</option><option value="150">150 candles</option>' +
      '    <option value="300">300 candles</option></select>' +
      '  <span class="pill bb-src" style="margin-left:auto">—</span></div>' +
      '<div style="height:calc(100% - 40px)"><canvas class="bb-c"></canvas></div>' +
      '<div class="bb-note muted" style="font-size:10px;height:18px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis"></div>';
    var tf = $('.bb-tf', body), last = $('.bb-last', body);
    tf.value = prefs.bbTf || '';
    last.value = String(prefs.bbLast || 80);
    tf.onchange = function () { prefs.bbTf = tf.value; save(PREF_KEY, prefs); REFRESH.bands(); };
    last.onchange = function () { prefs.bbLast = Number(last.value); save(PREF_KEY, prefs); REFRESH.bands(); };
    if (!window.Chart) { $('.bb-c', body).parentNode.innerHTML = '<div class="muted">Charts unavailable.</div>'; return; }
    bandsChart = window.SpreadCandlesChart
      ? new Chart($('.bb-c', body), (function () {
          var c = SpreadCandlesChart.config({fontSize: 9, xTicks: 5, maintainAspectRatio: false});
          return c;
        })())
      : null;
  };
  REFRESH.bands = function () {
    if (!W.bands || !bandsChart) { return Promise.resolve(); }
    var body = W.bands.body;
    var q = '/api/candles?last=' + (prefs.bbLast || 80) + (prefs.bbTf ? '&tf=' + prefs.bbTf : '');
    return getJSON(q).then(function (d) {
      var pts = d.points || [], e = Number(d.entry_zscore || 2);
      var fmt = function (t) {
        return new Date(t * 1000).toLocaleString('en-IN', {timeZone: 'Asia/Kolkata', day: '2-digit',
          month: 'short', hour: '2-digit', minute: '2-digit', hour12: false});
      };
      var entry = null, t = S.status && S.status.open_trade;
      if (t) { entry = Number(t.entry_spread != null ? t.entry_spread : t.entry_price); }
      SpreadCandlesChart.set(bandsChart, pts, e, (entry != null && isFinite(entry)) ? entry : null, fmt);
      var algoTf = (S.status && S.status.signal && S.status.signal.band_timeframe) || d.timeframe;
      var src = $('.bb-src', body), onCandles = d.band_source === 'candles';
      src.textContent = onCandles ? 'ALGO USES CANDLES · ' + algoTf : 'ALGO USES TICKS';
      src.className = 'pill bb-src ' + (onCandles && d.timeframe === algoTf ? 'ok' : '');
      src.title = onCandles ? (d.timeframe === algoTf ? 'These are the bands the algo trades on.'
                                                     : 'The algo trades the ' + algoTf + ' bands — this is a view of ' + d.label + '.')
                            : 'The algo uses the tick window. Settings → Signal Parameters → Band source.';
      var b = d.bands || {}, st = (d.status || {})[d.timeframe] || {}, lp = pts[pts.length - 1];
      $('.bb-note', body).textContent = d.label + ' · BB(' + d.length + ', EMA) ±' + e + 'σ · ' +
        (b.ready ? 'EMA ' + num(b.mean, 2) + ' · σ ' + num(b.std, 2) : 'need ' + d.length + ' candles (' + (b.count || 0) + ')') +
        (lp ? ' · now ' + num(lp.close, 2) + ' @ ' + fmt(lp.t) : '') +
        ' · history ' + (st.state || 'pending') + ' · ' + new Date().toLocaleTimeString('en-IN', {hour12: false});
      $('.bb-note', body).title = st.detail || '';
    }).catch(function () {});
  };

  // CHARTS ---------------------------------------------------------------------
  var charts = {};
  BUILD.charts = function (body) {
    body.innerHTML = '<div class="ch-bar muted" style="font-size:10px;height:18px">Show last ' +
                     '<select class="ch-range" style="font-size:10px">' +
                     '<option value="300">5 min</option><option value="900">15 min</option>' +
                     '<option value="3600">1 hour</option><option value="0">whole window</option></select>' +
                     ' <span class="ch-note"></span></div>' +
                     '<div style="height:calc(48% - 9px)"><canvas class="c-z"></canvas></div>' +
                     '<div style="height:calc(48% - 9px);margin-top:1%"><canvas class="c-s"></canvas></div>';
    var rng = $('.ch-range', body);
    rng.value = String(prefs.chartSec != null ? prefs.chartSec : 900);
    rng.onchange = function () { prefs.chartSec = Number(rng.value); save(PREF_KEY, prefs); REFRESH.charts(); };
    if (!window.Chart) { body.innerHTML = '<div class="muted">Charts unavailable.</div>'; return; }
    function base() {
      return {animation: false, responsive: true, maintainAspectRatio: false,
              plugins: {legend: {display: false}}, elements: {point: {radius: 0}},
              scales: {x: {display: false}, y: {ticks: {font: {size: 10}}}}};
    }
    charts.z = new Chart($('.c-z', body), {type: 'line', data: {labels: [], datasets: [
      {data: [], borderColor: '#1f7ac2', borderWidth: 1.5},
      {data: [], borderColor: '#b83232', borderDash: [4, 3], borderWidth: 1},
      {data: [], borderColor: '#b83232', borderDash: [4, 3], borderWidth: 1}]}, options: base()});
    charts.s = new Chart($('.c-s', body), {type: 'line', data: {labels: [], datasets: [
      {data: [], borderColor: '#6a4fa3', borderWidth: 1.5}]}, options: base()});
  };
  REFRESH.charts = function () {
    if (!W.charts || !charts.z) { return Promise.resolve(); }
    var sec = prefs.chartSec != null ? prefs.chartSec : 900;
    return getJSON('/api/spread-history?n=600' + (sec ? '&sec=' + sec : '')).then(function (d) {
      var note = $('.ch-note', W.charts.body);
      if (note) { note.textContent = '· updated ' + new Date().toLocaleTimeString(); }
      var z = d.zscores || [], s = d.spreads || [];
      var e = Number(((S.status || {}).signal || {}).entry_threshold || 2);
      var lab = z.map(function (_, i) { return i; });
      charts.z.data.labels = lab;
      charts.z.data.datasets[0].data = z;
      charts.z.data.datasets[1].data = z.map(function () { return e; });
      charts.z.data.datasets[2].data = z.map(function () { return -e; });
      // Early warm-up z's (tiny σ) can be ±15; keep the ±entry band readable.
      var lim = e + 2.5;
      charts.z.options.scales.y.min = -lim;
      charts.z.options.scales.y.max = lim;
      charts.z.update('none');
      charts.s.data.labels = s.map(function (_, i) { return i; });
      charts.s.data.datasets[0].data = s;
      charts.s.update('none');
    }).catch(function () {});
  };
  function resizeCharts() {
    Object.keys(charts).forEach(function (k) { try { charts[k].resize(); } catch (e) {} });
    if (bandsChart) { try { bandsChart.resize(); } catch (e) {} }
  }

  // MARGIN ---------------------------------------------------------------------
  BUILD.margin = function (body) { body.innerHTML = '<div class="kv mg-acc"></div><div class="sec">Pair margin</div><div class="kv mg-pair"></div><div class="muted mg-err" style="margin-top:4px"></div>'; };
  REFRESH.margin = function () {
    if (!W.margin) { return Promise.resolve(); }
    var body = W.margin.body;
    return getJSON('/api/arrow-margin').then(function (m) {
      $('.mg-acc', body).innerHTML =
        '<span>Ledger cash</span><span>' + inr(m.cash, 2) + '</span>' +
        '<span>Available margin</span><span>' + inr(m.available, 2) + '</span>' +
        '<span>Margin used</span><span>' + inr(m.used, 2) + (m.utilisation_pct == null ? '' : ' (' + m.utilisation_pct.toFixed(1) + '%)') + '</span>';
      var p = m.pair || {};
      $('.mg-pair', body).innerHTML =
        '<span>Lots per leg</span><span>' + (p.lots == null ? DASH : p.lots) + '</span>' +
        '<span>Required (both legs)</span><span>' + inr(p.basket_total, 2) + '</span>' +
        '<span>SPAN / Exposure</span><span>' + inr(p.basket_span) + ' / ' + inr(p.basket_exposure) + '</span>' +
        '<span>Leg A / Leg B alone</span><span>' + inr(p.leg_a_outright) + ' / ' + inr(p.leg_b_outright) + '</span>' +
        '<span>Spread benefit</span><span>' + inr(p.spread_benefit) + '</span>' +
        '<span>Trades covered</span><span>' + (p.headroom_trades == null ? DASH : p.headroom_trades) + '</span>';
      var routes = (p.routes || []).filter(function (r) { return !r.matches_orders; });
      $('.mg-err', body).textContent = p.error ? 'Arrow margin calculator: ' + p.error
        : routes.length ? 'Margin answered as ' + routes.map(function (r) { return r.symbol + ' → ' + r.exchange + ' / ' + r.identifier; }).join(', ') +
          ', but orders are sent as ' + routes[0].order_exchange + ' / trading symbol. Verify with one test lot.' : '';
    }).catch(function () {});
  };

  // FILLS & SLIPPAGE -----------------------------------------------------------
  BUILD.fills = function (body) {
    body.innerHTML = '<div class="kv fl-sum" style="padding:4px 8px;grid-template-columns:repeat(5,auto 1fr)"></div>' +
      '<table class="dtable"><thead><tr><th>Date</th><th>Source</th><th>Trade</th><th>Symbol</th><th>Side</th><th>Lots</th>' +
      '<th>Fired</th><th>Filled</th><th class="num">Took</th><th class="num">Touch</th><th class="num">Fill</th>' +
      '<th class="num">Slip pts</th><th class="num">Slip ₹</th><th>How</th></tr></thead><tbody></tbody></table>';
  };
  REFRESH.fills = function () {
    if (!W.fills) { return Promise.resolve(); }
    var body = W.fills.body;
    return getJSON('/api/execution').then(function (d) {
      var s = d.stats || {};
      $('.fl-sum', body).innerHTML =
        '<span>Avg slip / order / lot</span><span class="' + (s.avg_slippage_inr_per_lot_order > 0 ? 'down' : 'up') + '">' + inr(s.avg_slippage_inr_per_lot_order) + '</span>' +
        '<span>Setting</span><span>' + inr(d.configured_slippage_per_lot) + '</span>' +
        '<span>Avg to fill</span><span>' + ms(s.avg_fill_ms) + '</span>' +
        '<span>Leg gap avg/max</span><span>' + ms(s.avg_leg_gap_ms) + ' / ' + ms(s.max_leg_gap_ms) + '</span>' +
        '<span>Orders</span><span>' + (s.orders == null ? DASH : s.orders) + '</span>';
      var rows = [];
      (d.events || []).forEach(function (e) { (e.legs || []).forEach(function (l) { rows.push([e, l]); }); });
      $('tbody', body).innerHTML = rows.slice(0, 300).map(function (x) {
        var e = x[0], l = x[1], sp = l.slippage, c = sp > 0 ? 'down' : sp < 0 ? 'up' : '';
        var how = [String(l.order_type || '').toUpperCase(), l.amend_count ? l.amend_count + ' amend' : '',
                   l.escalated ? '→ MKT' : '', l.error ? '⚠ ' + l.error : ''].filter(Boolean).join(' · ');
        return '<tr><td>' + esc(e.date || '') + '</td><td><span class="owner ' + (e.source || 'manual') + '">' +
          esc((e.source || 'manual').toUpperCase()) + '</span></td><td>' + (e.label === 'Close' ? 'Exit' : 'Entry') +
          ' <span class="muted">' + esc(e.mode || '') + '</span></td><td>' + esc(l.symbol || DASH) + '</td>' +
          '<td class="' + (String(l.side).toLowerCase() === 'buy' ? '' : 'down') + '"><b>' + esc(String(l.side || '').toUpperCase()) + '</b></td>' +
          '<td class="num">' + (l.lots == null ? DASH : l.lots) + '</td><td>' + clock(l.sent_at) + '</td><td>' + clock(l.filled_at) + '</td>' +
          '<td class="num">' + ms(l.fill_ms) + '</td><td class="num">' + num(l.ref_price, 2) + '</td><td class="num">' + num(l.fill_price, 2) + '</td>' +
          '<td class="num ' + c + '">' + signed(sp, 2) + '</td><td class="num ' + c + '">' + inr(l.slippage_inr) + '</td>' +
          '<td class="muted">' + esc(how) + '</td></tr>';
      }).join('') || '<tr><td colspan="14" class="muted" style="text-align:center;padding:10px">No executed orders yet</td></tr>';
    }).catch(function () {});
  };

  // TRADES ---------------------------------------------------------------------
  BUILD.trades = function (body) {
    body.innerHTML = '<div style="padding:4px 8px" class="tr-sum muted"></div>' +
      '<table class="dtable"><thead><tr><th>Closed</th><th>Opened by</th><th>Closed by</th><th>Side</th><th class="num">Lots</th>' +
      '<th class="num">Entry</th><th class="num">Exit</th><th class="num">Net ₹</th><th>Reason</th></tr></thead><tbody></tbody></table>';
  };
  REFRESH.trades = function () {
    if (!W.trades) { return Promise.resolve(); }
    var body = W.trades.body;
    return getJSON('/api/desk/trades').then(function (d) {
      $('.tr-sum', body).innerHTML = (d.count || 0) + ' closed · total ' +
        '<b class="' + (d.total_pnl >= 0 ? 'up' : 'down') + '">' + inr(d.total_pnl, 0) + '</b>' +
        (d.open ? ' · <span class="owner ' + (d.open.source || 'manual') + '">' + esc((d.open.source || 'manual').toUpperCase()) +
                  '</span> ' + esc(String(d.open.direction || '').replace('_SPREAD', '')) + ' open' : '');
      $('tbody', body).innerHTML = (d.trips || []).map(function (r) {
        var np = Number(r.net_pnl || 0);
        var es = r.entry_source || 'manual', xs = r.exit_source || r.source || 'manual';
        return '<tr><td>' + esc(r.time || (r.ts ? new Date(r.ts * 1000).toLocaleString() : DASH)) + '</td>' +
          '<td><span class="owner ' + es + '">' + es.toUpperCase() + '</span></td>' +
          '<td><span class="owner ' + xs + '">' + xs.toUpperCase() + '</span></td>' +
          '<td>' + esc(String(r.direction || '').replace('_SPREAD', '')) + '</td><td class="num">' + (r.lots || DASH) + '</td>' +
          '<td class="num">' + num(r.entry_spread, 2) + '</td><td class="num">' + num(r.exit_spread, 2) + '</td>' +
          '<td class="num ' + (np >= 0 ? 'up' : 'down') + '">' + inr(np, 0) + '</td><td class="muted">' + esc(r.exit_reason || DASH) + '</td></tr>';
      }).join('') || '<tr><td colspan="9" class="muted" style="text-align:center;padding:10px">No completed trades yet</td></tr>';
    }).catch(function () {});
  };

  // ORDER LOG ------------------------------------------------------------------
  BUILD.orders = function (body) {
    body.innerHTML = '<div style="padding:4px 8px"><button class="btn ol-refresh">Refresh</button> <span class="muted ol-note"></span></div>' +
      '<table class="dtable"><thead><tr><th>Time</th><th>Symbol</th><th>Exch</th><th>Side</th><th>Product</th><th>Type</th>' +
      '<th class="num">Qty</th><th class="num">Filled</th><th class="num">Avg</th><th class="num">Limit</th><th>Status</th><th>Order ID</th></tr></thead><tbody></tbody></table>';
    $('.ol-refresh', body).onclick = function () { REFRESH.orders(true); };
  };
  REFRESH.orders = function (force) {
    if (!W.orders || !force) { return Promise.resolve(); }
    var body = W.orders.body;
    return getJSON('/api/exchange-orders', true).then(function (d) {
      $('.ol-note', body).textContent = d.note || ((d.orders || []).length + ' order(s) today');
      $('tbody', body).innerHTML = (d.orders || []).map(function (o) {
        var side = String(o.side || '').toUpperCase();
        return '<tr><td>' + esc(o.time || DASH) + '</td><td>' + esc(o.symbol || DASH) + '</td><td>' + esc(o.exchange || DASH) + '</td>' +
          '<td class="' + (side.indexOf('B') === 0 ? '' : 'down') + '"><b>' + esc(side) + '</b></td><td>' + esc(o.product || DASH) + '</td>' +
          '<td>' + esc(o.order_type || DASH) + '</td><td class="num">' + (o.qty == null ? DASH : o.qty) + '</td>' +
          '<td class="num">' + (o.fill_qty || DASH) + '</td><td class="num">' + num(o.fill_price || null, 2) + '</td>' +
          '<td class="num">' + num(o.price || null, 2) + '</td><td>' + esc(o.status || DASH) + '</td>' +
          '<td class="muted">' + esc(String(o.order_id || '').slice(-10)) + '</td></tr>';
      }).join('') || '<tr><td colspan="12" class="muted" style="text-align:center;padding:10px">No orders today</td></tr>';
    }).catch(function (e) { toast('Order log: ' + e, 'danger'); });
  };

  // ── polling ────────────────────────────────────────────────────────────
  // Every pair's ladder, working orders and algo state; the ACTIVE pair's
  // status every second and the others' every three.
  // Staggered, a pair at a time: with a cap on requests in flight, the
  // same pair must not always come last and lose its turn.
  function each(fn) {
    ORDER.forEach(function (k, i) { if (P[k]) { setTimeout(function () { fn(P[k]); }, i * 110); } });
  }
  function refreshAll(ctx) {
    var c = ctx || S;
    pollAlgo(c); pollStatus(c); refreshLadder(c); refreshWorking(c);
    if (c !== S) { return; }
    // the slower windows a moment later, so start-up never queues behind them
    setTimeout(function () { REFRESH.margin(); REFRESH.fills(); REFRESH.trades(); }, 1200);
  }
  setInterval(function () { pollStatus(S); }, 1000);
  setInterval(function () { each(function (c) { if (c !== S) { pollStatus(c); } }); }, 3000);
  setInterval(function () { each(pollAlgo); }, 2000);
  setInterval(function () { each(refreshLadder); }, 700);
  setInterval(function () { each(refreshFair); }, 2000);
  setInterval(function () { each(refreshAlgo); }, 1500);
  setInterval(function () { REFRESH.charts(); }, 2000);
  setInterval(function () { REFRESH.bands(); }, 3000);
  setInterval(function () { each(refreshWorking); }, 1000);
  setInterval(function () { REFRESH.margin(); }, 15000);
  setInterval(function () { REFRESH.fills(); REFRESH.trades(); }, 10000);
  setInterval(renderBanners, 1000);

  // ── start: the pairs, then what was open last time, else the default set ─
  function start(list) {
    (list || []).forEach(function (p) {
      if (!p.enabled) { return; }
      if (!P[p.key]) { P[p.key] = newCtx(p.key, p.base); ORDER.push(p.key); }
      P[p.key].base = p.base;
      P[p.key].name = p.name || '';
      var L = p.legs || {};
      if (L.leg_a && L.leg_b) { P[p.key].legs = [L.leg_a, L.leg_b]; }
    });
    var many = ORDER.length > 1;
    pairDefs().forEach(function (d) {
      var L = layout[d.id];
      if (L && L.open != null ? L.open : !d.closed) { openWindow(d.id); }
    });
    DEFS.forEach(function (d) {
      var L = layout[d.id];
      // With several pairs the ladders fill the screen: the Arrow windows
      // start closed (tick them open from +) unless they were open before.
      var open = L && L.open != null ? L.open : (!d.closed && !many);
      if (open) { openWindow(d.id); }
    });
    renderTabs();
    each(function (c) { refreshAll(c); });
    REFRESH.charts();
    REFRESH.bands();
    REFRESH.orders(true);
  }
  fetch('/p/main/api/pairs', {cache: 'no-store'}).then(function (r) { return r.json(); })
    .then(function (d) { start(d.pairs); })
    .catch(function () { start([]); });
})();
