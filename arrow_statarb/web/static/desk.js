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
  var LAYOUT_KEY = 'nexus-desk-layout-v1';
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
  function getJSON(url) {
    return fetch(url, {cache: 'no-store'}).then(function (r) {
      if (!r.ok) { throw new Error('HTTP ' + r.status); }
      return r.json();
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

  function confirmBox(title, body, confirmText) {
    var m = $('#modal');
    $('#modal-title').textContent = title;
    $('#modal-body').textContent = body;
    $('#modal-confirm').textContent = confirmText || 'Confirm';
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
  var prefs = load(PREF_KEY, {confirmClicks: true});
  var topZ = 20;

  var DEFS = [
    // id, title, default left/top/width/height, builder
    {id: 'ladder', title: 'Ladder · A − B', x: 8, y: 8, w: 360, h: 640, flush: true},
    {id: 'signal', title: 'Signal & Position', x: 376, y: 8, w: 470, h: 360},
    {id: 'stats', title: 'Statistics & Filters', x: 854, y: 8, w: 330, h: 360},
    {id: 'charts', title: 'Z-Score & Spread', x: 376, y: 376, w: 470, h: 272},
    {id: 'margin', title: 'Margin', x: 1192, y: 8, w: 300, h: 300},
    {id: 'fills', title: 'Fills & Slippage', x: 8, y: 656, w: 838, h: 250, flush: true},
    {id: 'trades', title: 'Trades', x: 854, y: 376, w: 638, h: 272, flush: true},
    {id: 'orders', title: 'Order Log (Arrow)', x: 854, y: 656, w: 638, h: 250, flush: true},
    {id: 'settings', title: 'Settings', x: 120, y: 60, w: 900, h: 640, flush: true, iframe: '/settings?embed=1', closed: true},
    {id: 'setup', title: 'Setup · pair', x: 160, y: 80, w: 900, h: 600, flush: true, iframe: '/?embed=1', closed: true},
    {id: 'analysis', title: 'Analysis', x: 200, y: 100, w: 1000, h: 640, flush: true, iframe: '/analysis?embed=1', closed: true},
  ];

  function defOf(id) { return DEFS.filter(function (d) { return d.id === id; })[0]; }

  // The default layout is drawn for a ~1500 × 910 desktop; on a smaller or
  // zoomed screen it is scaled down to fit (a moved window keeps its place).
  function fit(d) {
    var dk = $('#desktop');
    var sx = Math.min(1, (dk.clientWidth - 8) / 1500), sy = Math.min(1, (dk.clientHeight - 8) / 910);
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
    node.className = 'window floating sized';
    node.dataset.id = id;
    node.innerHTML =
      '<div class="titlebar"><span class="title">' + esc(d.title) + '</span>' +
      (d.iframe ? '<a class="winbtn" href="' + d.iframe.replace(/[?&]embed=1/, '') +
                  '" target="_blank" title="Open as a full page">↗</a>' : '') +
      '<span class="winbtns"><button class="winbtn close" title="Close">×</button></span></div>' +
      '<div class="wbody' + (d.flush ? ' flush' : '') + '"></div><div class="grip"></div>';
    $('#desktop').appendChild(node);
    var L = layout[id] || {};
    var F = fit(d);
    place(node, L.x != null ? L.x : F.x, L.y != null ? L.y : F.y, L.w || F.w, L.h || F.h);
    node.style.zIndex = L.z || ++topZ;
    topZ = Math.max(topZ, L.z || 0);
    var body = node.querySelector('.wbody');
    W[id] = {def: d, node: node, body: body};
    node.querySelector('.close').onclick = function () { closeWindow(id); };
    node.addEventListener('pointerdown', function (e) { raise(node); startDrag(e, node); });
    if (d.iframe) {
      body.innerHTML = '<iframe class="embed" src="' + d.iframe + '"></iframe>';
    } else {
      BUILD[id](body);
      REFRESH[id] && REFRESH[id]();
    }
    layout[id] = Object.assign({}, layout[id], {open: true});
    save(LAYOUT_KEY, layout);
    renderTabs();
  }

  function closeWindow(id) {
    if (!W[id]) { return; }
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
    DEFS.forEach(function (d) {
      if (!W[d.id]) { return; }
      var b = document.createElement('button');
      b.className = 'tab';
      b.textContent = d.title;
      b.onclick = function () { raise(W[d.id].node); };
      tabs.appendChild(b);
    });
    var menu = $('#add-menu');
    menu.innerHTML = '<div class="menu-title">Open a window</div>' + DEFS.map(function (d) {
      return '<button data-id="' + d.id + '">' + esc(d.title) + (W[d.id] ? ' <small>open</small>' : '') + '</button>';
    }).join('');
    Array.prototype.forEach.call(menu.querySelectorAll('button[data-id]'), function (b) {
      b.onclick = function () { menu.classList.add('hidden'); openWindow(b.dataset.id); };
    });
  }

  $('#add-panel').onclick = function () { $('#add-menu').classList.toggle('hidden'); };
  $('#tidy').onclick = function () {
    layout = {};
    save(LAYOUT_KEY, layout);
    Object.keys(W).forEach(function (id) {
      var F = fit(W[id].def);
      place(W[id].node, F.x, F.y, F.w, F.h);
    });
    resizeCharts();
  };

  // ── shared state from the server ──────────────────────────────────────
  var S = {status: null, algo: null, ladder: null, statusAt: 0};

  function algoState() { return (window.AlgoCtl && AlgoCtl.state) || S.algo; }

  function pollStatus() {
    return getJSON('/api/engine/status').then(function (d) {
      S.status = d; S.statusAt = Date.now();
      $('#engine-banner').classList.add('hidden');
      REFRESH.signal && W.signal && REFRESH.signal();
      REFRESH.stats && W.stats && REFRESH.stats();
      renderBanners();
    }).catch(function () {
      var b = $('#engine-banner');
      b.textContent = 'The engine is not answering — prices and positions on this screen are NOT live.';
      b.classList.remove('hidden');
    });
  }

  function pollAlgo() {
    return getJSON('/api/algo/state').then(function (d) { S.algo = d; renderBanners(); applyLock(); })
      .catch(function () {});
  }

  function renderBanners() {
    var a = S.algo || {};
    var b = $('#lock-banner');
    var msg = '';
    if (a.running) {
      msg = 'ALGO ON — manual orders are off on every window. Turn the algo OFF to trade by hand.';
    } else if (a.algo_block) {
      msg = a.algo_block;
    }
    b.textContent = msg;
    b.classList.toggle('hidden', !msg);
    var lb = $('#link-badge');
    var st = S.status;
    var fresh = S.statusAt && Date.now() - S.statusAt < 5000;
    var feed = st && st.signal && st.signal.book;
    lb.className = 'link ' + (!fresh ? 'bad' : feed ? 'ok' : 'warn');
    lb.textContent = !fresh ? 'engine down' : feed ? 'book live' : 'no book';
    $('#loop-stat').textContent = S.statusAt ? ((Date.now() - S.statusAt) / 1000).toFixed(1) + 's' : DASH;
  }

  // Manual controls follow the lock the server reports.
  function applyLock() {
    var a = S.algo || {};
    var blocked = !!a.manual_block;
    var flat = !(S.status && S.status.open_trade);
    Array.prototype.forEach.call(document.querySelectorAll('[data-manual]'), function (el) {
      var closer = el.classList.contains('flatten') || el.classList.contains('pos-close');
      el.disabled = blocked || (closer && flat);
      el.title = blocked ? a.manual_block : (closer && flat) ? 'No open position' : (el.dataset.tip || '');
    });
    if (W.ladder) {
      W.ladder.node.classList.toggle('locked-manual', blocked);
      var note = $('.lock-note', W.ladder.body);
      if (note) { note.classList.toggle('hidden', !blocked); }
    }
  }

  // ── manual orders (server enforces the lock and one-position rule) ────
  function manualOrder(direction) {
    var qty = parseInt(($('.qty-in', W.ladder && W.ladder.body) || {}).value, 10) || 1;
    var lad = S.ladder || {};
    var px = direction === 'LONG_SPREAD' ? lad.buy_spread : lad.sell_spread;
    var words = (direction === 'LONG_SPREAD' ? 'BUY' : 'SELL') + ' the spread · ' + qty +
      ' lot(s) · at ~' + num(px, 2) + ' (' + (direction === 'LONG_SPREAD' ? 'Ask A − Bid B' : 'Bid A − Ask B') + ')';
    var go = prefs.confirmClicks !== false
      ? confirmBox('Manual order', words + '\n\nTagged MANUAL. The algo cannot start while it is open.',
                   direction === 'LONG_SPREAD' ? 'BUY' : 'SELL')
      : Promise.resolve(true);
    go.then(function (ok) {
      if (!ok) { return; }
      postJSON('/api/manual-trade/execute', {direction: direction, lots: qty}).then(function (r) {
        if (r.success) { toast('MANUAL ' + words + ' — ' + (r.message || 'sent'), 'success'); }
        else { toast('Not sent: ' + (r.error || 'refused'), 'danger'); }
        refreshAll();
      }).catch(function (e) { toast('Order request failed: ' + e, 'danger'); });
    });
  }

  function closePosition() {
    var t = S.status && S.status.open_trade;
    if (!t) { toast('No open position.', 'info'); return; }
    confirmBox('Close position',
      'Close the ' + (t.owner || '') + ' ' + t.position_type + ' position (' + t.quantity +
      ' lot(s)) now, at the ' + (t.position_type === 'LONG' ? 'Sell' : 'Buy') + ' spread.', 'Close')
      .then(function (ok) {
        if (!ok) { return; }
        postJSON('/api/engine/close-position', {}).then(function (r) {
          toast(r.success ? 'Closed.' : 'Not closed: ' + (r.error || 'refused'), r.success ? 'success' : 'danger');
          refreshAll();
        });
      });
  }

  // ── builders + refreshers per window ──────────────────────────────────
  var BUILD = {}, REFRESH = {};

  // LADDER ------------------------------------------------------------------
  var ladderAnchor = null;
  BUILD.ladder = function (body) {
    body.innerHTML =
      '<div class="body">' +
      '<div class="rail">' +
      '  <div class="lock-note hidden">ALGO ON — manual orders off</div>' +
      '  <div class="route-box row"><div class="route-leg route-a">A <b>—</b></div><div class="route-leg route-b">B <b>—</b></div></div>' +
      '  <button class="quick buy-touch" data-manual data-tip="Buy the spread at the Buy spread (Ask A − Bid B)">BUY</button>' +
      '  <button class="quick sell-touch" data-manual data-tip="Sell the spread at the Sell spread (Bid A − Ask B)">SELL</button>' +
      '  <button class="quick flatten" data-manual data-tip="Close the open position at market">CLOSE POSITION</button>' +
      '  <label class="inline-field qty-row"><span>Qty (lots)</span><input class="qty-in" type="number" min="1" step="1"></label>' +
      '  <label class="inline-field"><span>Increment</span><input class="inc-in" type="number" min="0" step="0.05" placeholder="tick"></label>' +
      '  <div class="centre-row"><label class="check"><input type="checkbox" class="lock-scroll"> Lock</label>' +
      '    <label class="check" title="Ask before every manual order"><input type="checkbox" class="confirm-clicks"> Confirm</label></div>' +
      '  <div class="rail-label">Position</div><div class="lad-pos muted">flat</div>' +
      '  <div class="rail-label">Sell / Buy</div><div class="lad-sb">—</div>' +
      '  <div class="lad-err muted"></div>' +
      '</div>' +
      '<div class="grid"><table><thead><tr>' +
      '  <th class="c-work" title="Your position\'s levels: ENTRY, BE (break-even), TP and SL">Mark</th>' +
      '  <th class="c-bid" title="SELL the spread: Bid A − Ask B. Size = what both books can do, in lots">Bids</th>' +
      '  <th class="c-price" title="The spread, k × A − B, one row per increment">Price</th>' +
      '  <th class="c-ask" title="BUY the spread: Ask A − Bid B. Size = what both books can do, in lots">Asks</th>' +
      '</tr></thead><tbody></tbody></table></div>' +
      '</div>';
    $('.buy-touch', body).onclick = function () { manualOrder('LONG_SPREAD'); };
    $('.sell-touch', body).onclick = function () { manualOrder('SHORT_SPREAD'); };
    $('.flatten', body).onclick = closePosition;
    var cc = $('.confirm-clicks', body);
    cc.checked = prefs.confirmClicks !== false;
    cc.onchange = function () { prefs.confirmClicks = cc.checked; save(PREF_KEY, prefs); };
    $('.lock-scroll', body).onchange = function (e) {
      ladderAnchor = e.target.checked && S.ladder && S.ladder.rows && S.ladder.rows.length
        ? S.ladder.rows[Math.floor(S.ladder.rows.length / 2)].level : null;
    };
    $('tbody', body).onclick = function (e) {
      var td = e.target.closest('td.bid, td.ask');
      if (!td) { return; }
      if ((S.algo || {}).manual_block) { toast(S.algo.manual_block, 'danger'); return; }
      if (!td.classList.contains('clickable')) {
        toast('Only the inside level trades now (the outlined cell). Resting orders at other levels come later.', 'info');
        return;
      }
      manualOrder(td.classList.contains('ask') ? 'LONG_SPREAD' : 'SHORT_SPREAD');
    };
    applyLock();
  };

  REFRESH.ladder = function () {
    if (!W.ladder) { return Promise.resolve(); }
    var body = W.ladder.body;
    var inc = $('.inc-in', body).value;
    var q = '/api/ladder?count=29' + (inc ? '&increment=' + encodeURIComponent(inc) : '') +
            (ladderAnchor != null ? '&anchor=' + ladderAnchor : '');
    return getJSON(q).then(function (d) {
      S.ladder = d;
      var qin = $('.qty-in', body);
      if (!qin.value && d.lots) { qin.value = d.lots; }
      if (d.legs) {
        $('.route-a b', body).textContent = d.legs[0];
        $('.route-b b', body).textContent = d.legs[1];
      }
      $('.lad-err', body).textContent = d.error || '';
      $('.lad-sb', body).innerHTML = '<span class="down">' + num(d.sell_spread, 2) + '</span> / <span style="color:var(--bid-strong)">' +
        num(d.buy_spread, 2) + '</span>';
      var pos = d.position;
      $('.lad-pos', body).innerHTML = pos
        ? '<span class="owner ' + pos.owner + '">' + pos.owner.toUpperCase() + '</span> ' +
          (pos.direction === 'LONG_SPREAD' ? 'LONG' : 'SHORT') + ' ' + pos.lots + ' lot(s)'
        : 'flat';
      drawLadder(body, d);
      applyLock();
    }).catch(function () {});
  };

  function nearestRow(rows, v) {
    if (v == null || !rows.length) { return null; }
    var best = null, dist = Infinity;
    rows.forEach(function (r) { var x = Math.abs(r.level - v); if (x < dist) { dist = x; best = r.level; } });
    return best;
  }

  function drawLadder(body, d) {
    var rows = d.rows || [];
    var tb = $('tbody', body);
    var dig = d.increment && d.increment < 1 ? 2 : 0;
    var marks = {}, markTips = {};
    var m = d.markers;
    if (m && rows.length) {
      // A level off the visible ladder is NOT drawn at the edge price as if it
      // were there: it goes on the edge row with an arrow (↑ above, ↓ below).
      var top = rows[0].level, bottom = rows[rows.length - 1].level;
      var half = (d.increment || 0) / 2;
      [['entry', 'ENTRY'], ['break_even', 'BE'], ['take_profit', 'TP'], ['stop', 'SL']].forEach(function (p) {
        var v = m[p[0]];
        if (v == null) { return; }
        var lv, tag = p[1];
        if (v > top + half) { lv = top; tag += '↑'; }
        else if (v < bottom - half) { lv = bottom; tag += '↓'; }
        else { lv = nearestRow(rows, v); }
        marks[lv] = (marks[lv] ? marks[lv] + ' ' : '') + tag;
        markTips[lv] = (markTips[lv] ? markTips[lv] + ' · ' : '') + p[1] + ' ' + num(v, 2);
      });
    }
    var sell = d.sell_spread, buy = d.buy_spread;
    var direction = d.trade_direction || 'both';
    tb.innerHTML = rows.map(function (r) {
      var cls = [];
      if (sell != null && r.level <= sell + 1e-9) { cls.push('in-bid'); }
      if (buy != null && r.level >= buy - 1e-9) { cls.push('in-ask'); }
      if (r.is_mid) { cls.push('mid-line'); }
      var mk = marks[r.level] || '';
      var mkCls = /SL/.test(mk) ? 'sl' : /TP/.test(mk) ? 'tp' : /BE/.test(mk) ? 'be' : mk ? 'entry' : '';
      var bidCell = r.level <= (sell == null ? -Infinity : sell + 1e-9);
      var askCell = r.level >= (buy == null ? Infinity : buy - 1e-9);
      var bidTxt = r.bid_size != null ? r.bid_size : (r.is_best_bid ? '▲' : '');
      var askTxt = r.ask_size != null ? r.ask_size : (r.is_best_ask ? '▼' : '');
      return '<tr class="' + cls.join(' ') + '">' +
        '<td class="work' + (mk ? ' mark ' + mkCls : '') + '"' + (mk ? ' title="' + esc(markTips[r.level]) + '"' : '') + '>' + esc(mk) + '</td>' +
        '<td class="' + (bidCell ? 'bid' : '') + (r.is_best_bid ? ' has-qty clickable' : '') + '"' +
          (r.is_best_bid ? ' title="SELL the spread here (Bid A − Ask B)' + (direction === 'buy_only' ? ' — note: the ALGO only buys; this is manual' : '') + '"' : '') + '>' +
          (bidCell ? bidTxt : '') + '</td>' +
        '<td class="price">' + num(r.level, dig) + '</td>' +
        '<td class="' + (askCell ? 'ask' : '') + (r.is_best_ask ? ' has-qty clickable' : '') + '"' +
          (r.is_best_ask ? ' title="BUY the spread here (Ask A − Bid B)"' : '') + '>' +
          (askCell ? askTxt : '') + '</td></tr>';
    }).join('');
  }

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
    $('.pos-close', body).onclick = closePosition;
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
    $('.st-warm', body).textContent = g.data_ready
      ? 'Ready — ' + Math.floor(have / 60) + ' min sampled'
      : Math.floor(have / 60) + ' of ' + Math.round(need / 60) + ' min sampled' +
        (g.sample_status ? ' · not sampling: ' + g.sample_status : '');
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

  // CHARTS ---------------------------------------------------------------------
  var charts = {};
  BUILD.charts = function (body) {
    body.innerHTML = '<div style="height:48%"><canvas class="c-z"></canvas></div>' +
                     '<div style="height:48%;margin-top:2%"><canvas class="c-s"></canvas></div>';
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
    return getJSON('/api/spread-history?n=240').then(function (d) {
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
  function resizeCharts() { Object.keys(charts).forEach(function (k) { try { charts[k].resize(); } catch (e) {} }); }

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
    return getJSON('/api/exchange-orders').then(function (d) {
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
  function refreshAll() {
    pollAlgo();
    pollStatus();
    REFRESH.ladder();
    REFRESH.margin();
    REFRESH.fills();
    REFRESH.trades();
  }
  setInterval(pollStatus, 1000);
  setInterval(pollAlgo, 2000);
  setInterval(function () { REFRESH.ladder(); }, 700);
  setInterval(function () { REFRESH.charts(); }, 5000);
  setInterval(function () { REFRESH.margin(); }, 15000);
  setInterval(function () { REFRESH.fills(); REFRESH.trades(); }, 10000);
  setInterval(renderBanners, 1000);

  // ── start: reopen what was open last time, else the default set ────────
  DEFS.forEach(function (d) {
    var L = layout[d.id];
    var open = L && L.open != null ? L.open : !d.closed;
    if (open) { openWindow(d.id); }
  });
  renderTabs();
  refreshAll();
  REFRESH.charts();
  REFRESH.orders(true);
})();
