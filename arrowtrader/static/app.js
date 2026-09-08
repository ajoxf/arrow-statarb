/* The ladder, the Market Grid and the positions monitor.
 *
 * Everything on screen comes from ONE snapshot (/api/status), which the
 * coordinator publishes each poll — so the ladder cannot disagree with
 * the grid beside it, and the grid costs no extra MT5 round trips.
 * Everything the trader does goes out as a command (/api/command) and
 * is executed once by the coordinator.
 *
 * House rules, from the spec:
 *   - No native confirm() / alert() / prompt(). One shared modal.
 *   - Errors do not auto-hide. A failure that vanishes in 3s is missed.
 *   - Unmeasured is not zero: render an em dash.
 *   - Never send the operator to a log for a decision already made.
 */

(function () {
  'use strict';

  var REFRESH_MS = 300;
  var DASH = '—';

  var state = {
    snapshot: {pairs: {}, accounts: {}},
    open: [],            // panel ids in taskbar order
    closed: {},          // panels the TRADER closed — they stay closed
    reveal: null,        // a panel just opened: scroll it into view once
    active: null,
    // pair key -> {buy, sell}: a size per SIDE. They are usually the
    // same — the keypad sets both — but a desk that wants to lift 1 and
    // offer 5 can type each one, and the box each size sits in is
    // beside the button that will send it.
    armed: {},
    locked: {},          // pair key -> scroll is locked
    scrolledAt: {},      // pair key -> when the TRADER last scrolled it
    busyAt: {},          // pair key -> when the trader last TOUCHED it
    hovering: {},        // pair key -> the pointer is over its ladder
    centredAt: {},       // pair key -> when we last centred it on the mid
    centring: {},        // pair key -> that scroll event was ours
    centringAgain: {},   // pair key -> a centring is waiting on layout
    filtered: {},        // pair key -> hide rows with nothing on them
    monitorTab: 'positions',
    // Clicks that have been sent but are not in a snapshot yet. The
    // round trip is ~25ms plus the broker, but the ladder must show the
    // order the INSTANT it is clicked: a trader who cannot see their
    // click clicks again.
    pending: [],
    help: false,
    // Banners the trader has put away, by what they were ABOUT: the
    // same banner returns when the situation behind it changes.
    dismissed: {},
    // Dead orders already said out loud, by id: a refusal is said once
    // and then stays in the Working Orders tab.
    reported: {},
    // The one-key shortcuts, which a desk may not want at all: B and S
    // are orders, and a keyboard nobody meant to touch is a click
    // nobody meant to make.
    keysOff: false,
    // Sound on placed / filled / cancelled, and how many positions the
    // last snapshot had — a resting order that fills is a fill nobody
    // clicked for.
    soundOff: false,
    positionCount: null,
    // The journal, from the database. Not in the snapshot: it is the
    // BROKER's record, it outlives the process, and it is read on its
    // own slow clock rather than three times a second.
    fills: null,
    fillsAt: 0,
    fillsFilter: {ours: false},
    // The slippage report, over the session that is actually running.
    // Same treatment as the journal: recorded, not live, so it is read
    // on its own slow clock.
    // The coordinator's own publish clock, from the last snapshot: the
    // panels are redrawn when this MOVES, not on every poll.
    lastAt: null,
    slippage: null,
    slippageAt: 0,
    slippageAll: false
  };

  // -- plumbing -------------------------------------------------------

  function el(id) { return document.getElementById(id); }

  function fmt(value, digits) {
    if (value === null || value === undefined || value === '') return DASH;
    if (typeof value !== 'number') return String(value);
    if (!isFinite(value)) return DASH;
    return value.toFixed(digits === undefined ? 4 : digits);
  }

  // A SIZE, printed the way it was typed.
  //
  // Quantities are added and subtracted — a click walking down a stack
  // of positions is a chain of subtractions — and binary floats do not
  // survive that cleanly. `0.15 - 0.1` is 0.04999999999999999, and the
  // Working Orders panel printed exactly that beside a clean 0.1, which
  // reads as the size having been changed on the way to the broker. The
  // engine tidies the arithmetic now; this makes sure nothing on the
  // way to the screen can put the dust back.
  function qty(value) {
    if (value === null || value === undefined || value === '') return DASH;
    var number = Number(value);
    if (!isFinite(number)) return DASH;
    return String(parseFloat(number.toFixed(6)));
  }

  function escapeHtml(value) {
    // Symbols, comments and broker refusals are the broker's text, not
    // ours, and every one of them lands in innerHTML.
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function money(value) {
    if (value === null || value === undefined || !isFinite(value)) return DASH;
    var sign = value < 0 ? '-' : '';
    // Grouped, and grouped the INDIAN way: en-IN puts the separators at
    // 1,00,000 rather than 100,000. A desk reading lakhs off a screen
    // grouped in thousands re-reads every figure, which is exactly the
    // cost this function exists to avoid.
    return sign + '\u20B9' + Math.abs(value).toLocaleString('en-IN', {
      minimumFractionDigits: 2, maximumFractionDigits: 2});
  }

  function toast(message, kind) {
    var box = document.createElement('div');
    box.className = 'toast' + (kind === 'ok' ? ' ok' : '');
    box.innerHTML = '<span class="dismiss">&times;</span>';
    box.appendChild(document.createTextNode(message));
    // Errors used to stay until dismissed, on purpose — so that a
    // refusal could not scroll past unread. But a refusal that REPEATS
    // (a broker refusing every order, a leg that will not quote) built
    // a wall of identical boxes over the ladder, each needing its own
    // click. That buries the market behind the message about it.
    //
    // So errors clear too, just slower: long enough to read twice,
    // and still dismissable on click. Anything that must not be missed
    // belongs in the checklist or the banner, which persist properly,
    // rather than in a box that was always going to disappear.
    box.addEventListener('click', function () { box.remove(); });
    el('toasts').appendChild(box);
    window.setTimeout(function () { box.remove(); },
                      kind === 'ok' ? 4000 : 10000);
  }

  function ask(title, body, confirmLabel, onConfirm) {
    el('modal-title').textContent = title;
    el('modal-body').textContent = body;
    el('modal-confirm').textContent = confirmLabel;
    el('modal').classList.remove('hidden');
    el('modal').dataset.pending = '1';
    el('modal')._onConfirm = onConfirm;
  }

  function closeModal() {
    el('modal').classList.add('hidden');
    el('modal')._onConfirm = null;
  }

  // -- sound -------------------------------------------------------------
  //
  // Generated here, with WebAudio: no files, nothing fetched, nothing
  // that a blocked CDN or a missing asset can silence. Three events, and
  // they are deliberately different in shape rather than in pitch alone
  // — a desk hears these across a room and must not have to work out
  // which one it was.
  //
  //   placed     one short blip
  //   filled     two notes, rising — the one that means money moved
  //   cancelled  one low, flat note
  //
  // Off is a first-class setting: some desks want silence, and a sound
  // nobody asked for is worse than none.

  var TONES = {
    placed: [{hz: 880, ms: 70, gain: 0.05}],
    filled: [{hz: 660, ms: 80, gain: 0.07}, {hz: 1040, ms: 130, gain: 0.07}],
    cancelled: [{hz: 320, ms: 110, gain: 0.05}],
    refused: [{hz: 180, ms: 200, gain: 0.06}]
  };
  var audio = null;

  function sound(name) {
    if (state.soundOff) { return; }
    var tones = TONES[name];
    if (!tones) { return; }
    try {
      var Ctor = window.AudioContext || window.webkitAudioContext;
      if (!Ctor) { return; }
      audio = audio || new Ctor();
      // Browsers start the context suspended until a gesture. Every
      // sound here follows a click, so this resolves on the first one.
      if (audio.state === 'suspended') { audio.resume(); }
      var at = audio.currentTime;
      tones.forEach(function (tone) {
        var osc = audio.createOscillator();
        var gain = audio.createGain();
        osc.type = 'sine';
        osc.frequency.value = tone.hz;
        gain.gain.setValueAtTime(tone.gain, at);
        // Ramped down rather than cut: a square edge clicks.
        gain.gain.exponentialRampToValueAtTime(0.0001, at + tone.ms / 1000);
        osc.connect(gain).connect(audio.destination);
        osc.start(at);
        osc.stop(at + tone.ms / 1000);
        at += tone.ms / 1000;
      });
    } catch (e) {
      // No audio device, or a policy that forbids it. Never a reason
      // for anything on this screen to stop working.
    }
  }

  function send(kind, payload, then) {
    return fetch('/api/command', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({kind: kind, payload: payload || {}})
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok || !data.ok) {
          // The refusal's OWN words, on screen. Never "check the log".
          toast(data.error || 'the command was refused');
          return null;
        }
        return pollResult(data.id, then);
      });
    }).catch(function (error) {
      toast('could not reach the web process: ' + error.message);
    });
  }

  function soundFor(result) {
    /* What the ENGINE says happened, not what the click intended: a
     * market click that was refused must not sound like a fill. */
    if (!result) { return; }
    var data = result.data || {};
    if (result.ok === false || data.refused) { return sound('refused'); }
    if (data.position) { return sound('filled'); }
    if (data.order) { return sound('placed'); }
    if (data.cancelled !== undefined || data.closed !== undefined) {
      return sound('cancelled');
    }
  }

  function toastOutcome(result) {
    /* What the engine said, in the style that says which it was.
     *
     * A REASON IS NOT A REFUSAL. An error toast stays on the screen
     * until it is dismissed, on purpose — and an ordinary outcome
     * ("resting at 7.69 to CLOSE 2 position(s), then open 93") was
     * going out in exactly that style, so a click that did precisely
     * what it was asked read as a fault and sat there until somebody
     * clicked it away.
     */
    var data = (result && result.data) || {};
    if (result && result.ok === false) {
      return toast(result.error || 'the engine refused that');
    }
    if (data.refused) { return toast(data.reason || 'refused'); }
    if (data.reason) { return toast(data.reason, 'ok'); }
  }

  function pollResult(id, then, tries) {
    tries = tries || 0;
    return fetch('/api/result/' + id).then(function (r) { return r.json(); })
      .then(function (result) {
        if (result && result.pending) {
          if (tries > 20) { return null; }
          return new Promise(function (resolve) {
            window.setTimeout(function () {
              resolve(pollResult(id, then, tries + 1));
            }, 150);
          });
        }
        soundFor(result);
        toastOutcome(result);
        if (then) { then(result); }
        return result;
      });
  }

  // -- panels ---------------------------------------------------------

  function panelId(kind, key) { return kind + ':' + (key || ''); }

  function openPanel(id) {
    delete state.closed[id];
    if (state.open.indexOf(id) < 0) { state.open.push(id); }
    state.active = id;
    // ...and SHOW it. The desktop is a row that scrolls: with two
    // ladders and the grid already on it, a window opened at the end
    // sits off the right-hand edge, and "Positions opens into nothing"
    // is what that looks like from the outside.
    state.reveal = id;
    render();
  }

  function closePanel(id) {
    state.open = state.open.filter(function (other) { return other !== id; });
    // Closing is a decision, and it is remembered: a ladder that came
    // back on the next poll would be a window that cannot be closed.
    state.closed[id] = true;
    render();
  }

  function panelIdOf(node) {
    /* Which panel a window element IS. One definition, because three
     * copies of this drifting apart is how a window ends up impossible
     * to close, focus or move. */
    if (!node) { return null; }
    return node.classList.contains('market-grid') ? panelId('grid')
      : node.classList.contains('monitor') ? panelId('monitor')
        : node.classList.contains('settings') ? panelId('settings')
          : node.classList.contains('fairwin')
            ? panelId('fair', node.dataset.pair)
            : panelId('ladder', node.dataset.pair);
  }

  // -- moving the windows ------------------------------------------------
  //
  // Drag a title bar and the window goes where it is put, over the
  // others, and stays there across a reload. Two rules the reference
  // screen has and this needs for the same reason:
  //
  //   - A window can NEVER be dropped where it cannot be got back. The
  //     title bar is kept on screen at both edges, so there is always
  //     something left to grab.
  //   - Dragging is not clicking. A drag that started on a title bar
  //     never reaches the ladder underneath, and the ladder is where a
  //     click places an order.
  //
  // The layout is per browser, in localStorage: it is a preference
  // about this screen, not state the engine should ever be asked about.

  var LAYOUT_KEY = 'mt5trader.windows.v1';
  //: How much of a window must stay on the desktop, in pixels.
  var KEEP_VISIBLE_PX = 90;
  var layout = readLayout();
  var topZ = 10;
  //: Windows stack inside this band; the modal, the menu and the toasts
  //: live above it in ladder.css. topZ climbed with every click and
  //: never came back down, so after enough clicks a window passed the
  //: dialog and a Delete confirmation opened BEHIND the window that
  //: asked for it — a question nobody can answer and nothing to
  //: dismiss.
  var MAX_WINDOW_Z = 900;

  function nextZ() {
    if (topZ < MAX_WINDOW_Z) {
      topZ += 1;
      return topZ;
    }
    // At the ceiling: renumber from the bottom instead of climbing into
    // the dialog's band. The ORDER windows are stacked in is what the
    // operator arranged; only the numbers shrink.
    var nodes = Array.prototype.slice.call(
      document.querySelectorAll('.window')).filter(function (node) {
        return node.style.zIndex;
      });
    nodes.sort(function (a, b) {
      return (parseInt(a.style.zIndex, 10) || 0) -
             (parseInt(b.style.zIndex, 10) || 0);
    });
    topZ = 10;
    nodes.forEach(function (node) {
      topZ += 1;
      node.style.zIndex = topZ;
      var id = panelIdOf(node);
      if (layout[id]) { layout[id].z = topZ; }
    });
    topZ += 1;
    return topZ;
  }

  function readLayout() {
    try {
      return JSON.parse(window.localStorage.getItem(LAYOUT_KEY) || '{}') || {};
    } catch (e) {
      return {};                     // private mode, or a corrupt value
    }
  }

  function writeLayout() {
    try {
      window.localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
    } catch (e) {
      // A layout that cannot be saved is a layout that does not survive
      // a reload. Nothing else about the screen depends on it.
    }
  }

  function desktopBox() {
    return el('desktop').getBoundingClientRect();
  }

  function desktopOrigin() {
    /* Where a floating window's `left: 0` actually SITS on the screen.
     *
     * The desktop scrolls sideways (`overflow-x: auto`) and is the
     * containing block for every floating window, so `style.left` is
     * measured from the scrolled CONTENT origin. A bounding rect is
     * measured from the VISIBLE edge. Those two differ by exactly
     * `scrollLeft`, and a drag that reads one and writes the other
     * moves the window by the scroll distance in the wrong direction
     * — which reads as a window that runs away from the cursor.
     */
    var node = el('desktop');
    var rect = node.getBoundingClientRect();
    return {left: rect.left - node.scrollLeft,
            top: rect.top - node.scrollTop};
  }

  function clampTo(node, left, top) {
    var desktop = el('desktop');
    var width = node.offsetWidth || 200;
    // The right-hand stop is the edge of what the trader can SEE right
    // now, which is the scrolled viewport rather than the desktop's
    // own width. Clamping to the width alone pinned every window into
    // the first screenful: drag one past the fold and it sprang back.
    // Deliberately NOT scrollWidth — a floating window contributes to
    // that, so the ceiling would grow as the window approached it and
    // the window would never stop.
    var rightStop = desktop.scrollLeft + desktop.clientWidth
      - KEEP_VISIBLE_PX;
    var bottomStop = desktop.scrollTop + desktop.clientHeight - 24;
    return {
      left: Math.min(Math.max(left, KEEP_VISIBLE_PX - width),
                     Math.max(rightStop, 0)),
      // Never above the desktop: a title bar under the banner cannot be
      // grabbed at all.
      top: Math.min(Math.max(top, 0), Math.max(bottomStop, 0))
    };
  }

  function place(node, left, top) {
    var at = clampTo(node, left, top);
    node.classList.add('floating');
    node.style.left = at.left + 'px';
    node.style.top = at.top + 'px';
    return at;
  }

  function ensureGrip(node) {
    /* The corner every window is resized by. Added here rather than in
     * the template because the monitor, the grid and the settings page
     * build their own markup — one grip, added the same way to all of
     * them, is one behaviour to get right. */
    if (node.querySelector(':scope > .grip')) { return; }
    var grip = document.createElement('div');
    grip.className = 'grip';
    grip.title = 'Drag to resize this window';
    node.appendChild(grip);
  }

  function size(node, width, height) {
    var desktop = desktopBox();
    var w = Math.max(240, Math.min(width, desktop.width));
    var h = Math.max(120, Math.min(height, desktop.height));
    node.classList.add('sized');
    node.style.width = w + 'px';
    node.style.height = h + 'px';
    return {w: w, h: h};
  }

  //: The windows that are TOOLS rather than ladders. They are wide,
  //: they are opened and closed all day, and putting them at the end of
  //: the desktop row pushes everything else off the screen: the trader
  //: then scrolls sideways past every ladder to reach the one they
  //: wanted. They float over the ladders instead, in view, and can be
  //: dragged and resized like anything else.
  var FLOATING_BY_DEFAULT = ['grid:', 'monitor:', 'settings:'];

  function floatByDefault(node, id) {
    var desktop = desktopBox();
    // Sizes come from the VISIBLE desktop; positions are written in
    // content coordinates, so they are measured from the scrolled
    // origin. Mixing the two puts a tool window off-screen the moment
    // the desk has been scrolled sideways.
    var origin = desktopOrigin();
    if (!desktop.width) { return; }
    var wanted = size(node,
                      Math.min(1180, Math.max(desktop.width - 80, 320)),
                      Math.min(560, Math.max(desktop.height - 80, 200)));
    // Stepped down and across so a second and third one do not land
    // exactly on top of the first.
    // Beside the tiled ladders where there is room for it, so opening
    // Positions does not cover the market. Over them when there is not
    // — it is a window, and it can be dragged.
    var index = FLOATING_BY_DEFAULT.indexOf(id);
    var free = 0;
    Array.prototype.forEach.call(document.querySelectorAll('.window.ladder'),
      function (ladder) {
        if (ladder.classList.contains('floating')) { return; }
        var box = ladder.getBoundingClientRect();
        free = Math.max(free, box.right - origin.left + 8);
      });
    var left = Math.max(Math.min(free + index * 24,
                                 el('desktop').scrollLeft + desktop.width
                                 - wanted.w - 8), 8);
    var top = Math.min(30 + index * 24,
                       Math.max(desktop.height - wanted.h - 8, 0));
    var at = place(node, left, top);
    node.style.zIndex = nextZ();
    layout[id] = {left: at.left, top: at.top, z: topZ,
                  w: wanted.w, h: wanted.h};
    writeLayout();
  }

  function fairFloatByDefault(node, id) {
    /* A small window, landing clear of the ladders rather than over the
     * prices. It is dragged and resized like anything else once the
     * trader has put it somewhere. */
    var desktop = desktopBox();
    if (!desktop.width) { return; }
    // NEITHER dimension is set. The window is as wide as its widest row
    // and as tall as its figures — fix either one and the difference
    // comes back as empty grey, beside a ladder, where screen is not
    // spare. It is still dragged and resized like anything else; that
    // is when a size gets remembered.
    node.classList.add('sized');
    node.style.width = '';
    node.style.height = 'auto';
    var open = document.querySelectorAll('.window.fairwin').length;
    var width = node.getBoundingClientRect().width || 240;
    var left = Math.max(desktop.width - width - 12 - (open - 1) * 18, 8);
    var top = Math.min(30 + (open - 1) * 20, Math.max(desktop.height - 260, 0));
    var at = place(node, left, top);
    node.style.zIndex = nextZ();
    layout[id] = {left: at.left, top: at.top, z: topZ};
    writeLayout();
  }

  function applyLayout(node) {
    /* Put a window back where it was left. Called for every window on
     * every render, because panels are created lazily — the monitor
     * opened an hour later must still come back to its own corner. */
    var id = panelIdOf(node);
    var saved = layout[id];
    if (!saved) {
      if (FLOATING_BY_DEFAULT.indexOf(id) >= 0) { floatByDefault(node, id); }
      else if (node.classList.contains('fairwin')) {
        fairFloatByDefault(node, id);
      }
      return;
    }
    if (saved.w && saved.h) { size(node, saved.w, saved.h); }
    else if (saved.w) {
      // Width remembered, height left to the content — what the fair
      // window is given until the trader drags it themselves.
      node.classList.add('sized');
      node.style.width = Math.max(200, saved.w) + 'px';
      node.style.height = 'auto';
    }
    if (saved.left === undefined) { return; }   // resized, never moved
    var at = place(node, saved.left, saved.top);
    node.style.zIndex = saved.z || 10;
    // Clamped: a layout saved before the ceiling existed can carry a z
    // from the dialog's band, and reading it back would put us straight
    // over the modal again.
    topZ = Math.min(MAX_WINDOW_Z, Math.max(topZ, saved.z || 10));
    // Clamped on the way in as well as on the way out: a layout saved
    // on a big monitor must not hide a window on a laptop.
    if (at.left !== saved.left || at.top !== saved.top) {
      layout[id] = {left: at.left, top: at.top, z: saved.z || 10};
      writeLayout();
    }
  }

  function raise(node) {
    var id = panelIdOf(node);
    if (!layout[id] || layout[id].left === undefined) {
      return;                           // still in the row; nothing to raise
    }
    node.style.zIndex = nextZ();
    layout[id].z = topZ;
    writeLayout();
  }

  function tidyWindows() {
    /* Everything back to the row it started in. The way out of a mess,
     * and the way back from a window dragged somewhere useless. */
    layout = {};
    writeLayout();
    Array.prototype.forEach.call(document.querySelectorAll('.window'),
      function (node) {
        node.classList.remove('floating', 'sized');
        node.style.left = node.style.top = node.style.zIndex = '';
        node.style.width = node.style.height = '';
      });
  }

  function startDrag(e) {
    if (e.button !== 0) { return; }
    var node = e.target.closest('.window');
    if (!node) { return; }
    if (e.target.closest('.grip')) { return startResize(e, node); }
    var bar = e.target.closest('.titlebar');
    if (!bar) { return; }
    // The buttons and selects that live IN the title bar keep working.
    if (e.target.closest('button, select, input, a')) { return; }

    var box = node.getBoundingClientRect();
    var desktop = desktopOrigin();
    var grabX = e.clientX - box.left;
    var grabY = e.clientY - box.top;
    var startX = e.clientX;
    var startY = e.clientY;
    var moved = false;
    state.active = panelIdOf(node);

    function move(event) {
      if (!moved) {
        // A CLICK on a title bar must not rearrange the desk: the
        // window only leaves the row once the pointer has actually
        // travelled. Pinned first at exactly where it already is, so
        // it does not jump out from under the cursor.
        if (Math.abs(event.clientX - startX) < 4 &&
            Math.abs(event.clientY - startY) < 4) { return; }
        moved = true;
        node.classList.add('dragging');
        layout[panelIdOf(node)] = Object.assign(
          {}, layout[panelIdOf(node)],
          {left: box.left - desktop.left, top: box.top - desktop.top,
           z: Math.min(MAX_WINDOW_Z, topZ + 1)});
        place(node, box.left - desktop.left, box.top - desktop.top);
        raise(node);
      }
      // Re-read every move: a drag that reaches the edge scrolls the
      // desktop under the window, and an origin captured at grab time
      // would then be stale by exactly that scroll.
      var frame = desktopOrigin();
      var at = place(node, event.clientX - grabX - frame.left,
                     event.clientY - grabY - frame.top);
      layout[panelIdOf(node)].left = at.left;
      layout[panelIdOf(node)].top = at.top;
    }

    function drop() {
      node.classList.remove('dragging');
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', drop);
      if (moved) {
        writeLayout();
        render();
      }
    }

    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', drop);
    // The drag has the pointer now: the ladder underneath must not also
    // see this as a click.
    e.preventDefault();
  }

  function startResize(e, node) {
    /* Windows are freely expandable, in both directions, from the
     * corner. The ladder gets longer — more price rows on screen at
     * once, which is the whole point of a ladder — and the monitor and
     * the grid get wider without a horizontal scrollbar. */
    var box = node.getBoundingClientRect();
    var startX = e.clientX;
    var startY = e.clientY;
    var id = panelIdOf(node);
    node.classList.add('dragging');

    function move(event) {
      var to = size(node, box.width + (event.clientX - startX),
                    box.height + (event.clientY - startY));
      layout[id] = Object.assign({}, layout[id], {w: to.w, h: to.h});
    }

    function drop() {
      node.classList.remove('dragging');
      document.removeEventListener('pointermove', move);
      document.removeEventListener('pointerup', drop);
      writeLayout();
      render();
    }

    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', drop);
    e.preventDefault();
  }

  // -- the ladder ------------------------------------------------------

  function ladderNode(key) {
    var existing = document.querySelector('.ladder[data-pair="' + cssEscape(key) + '"]');
    if (existing) { return existing; }
    var node = el('ladder-template').content.firstElementChild.cloneNode(true);
    node.dataset.pair = key;
    wireLadder(node, key);
    el('desktop').appendChild(node);
    return node;
  }

  function cssEscape(value) { return String(value).replace(/"/g, '\\"'); }

  function fairNode(key) {
    /* One pair's fair value and exit, in a window of their own. */
    var existing = document.querySelector(
      '.fairwin[data-pair="' + cssEscape(key) + '"]');
    if (existing) { return existing; }
    var node = el('fair-template').content.firstElementChild.cloneNode(true);
    node.dataset.pair = key;
    node.querySelector('.close').addEventListener('click', function () {
      // Closing it IS turning it off — for this screen and in the
      // pair's settings. A window that comes back on the next poll
      // because the setting still says so is a window the trader
      // cannot get rid of.
      showFairWindow(key, false);
    });
    node.querySelectorAll('.fw-cog, .open-exits').forEach(function (cog) {
      cog.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        var ladder = document.querySelector(
          '.ladder[data-pair="' + cssEscape(key) + '"]');
        if (ladder) { openLadderSettings(ladder, key); }
        else { toast('open the ' + key + ' ladder to reach its settings'); }
      });
    });
    el('desktop').appendChild(node);
    return node;
  }

  function showFairWindow(key, on) {
    /* Whether this window is open is a decision about THIS SCREEN.
     *
     * The pair's setting is where it starts — tick the box and it
     * opens, here and on the next browser to load the page — but it
     * does not own the window. Owned by the snapshot, a poll arriving
     * before the save has landed, or with the engine down, or
     * restarted, takes the window away under the trader's hands a
     * third of a second after they opened it. Which is what it did.
     *
     * So the panel is opened and closed like every other panel, and
     * the setting is saved beside it.
     */
    var id = panelId('fair', key);
    if (on) { openPanel(id); } else { closePanel(id); }
    setPair(key, {algo_window: !!on});
    var row = (state.snapshot.pairs || {})[key];
    if (row) { row.algo_window = !!on; }           // before the next poll
    fetch('/api/pairs/' + encodeURIComponent(key), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({algo_window: !!on})
    }).catch(function () { /* the engine already has it */ });
  }

  function renderFairWindow(key, row) {
    var node = fairNode(key);
    var name = row.name || key;
    var title = node.querySelector('.fw-pair');
    // Named across the top: two of these open at once are otherwise two
    // identical tables of numbers with nothing to say which instrument
    // either belongs to.
    title.textContent = name;
    title.title = key + ' — leg A ' + (row.symbol_a || '?') + ', leg B '
      + (row.symbol_b || '?');
    // WHICH algo, first: the window shows one reading, not two.
    renderAlgo(node, row);
    renderFair(node, row);
  }

  function wireLadder(node, key) {
    node.querySelector('.close').addEventListener('click', function () {
      closePanel(panelId('ladder', key));
    });
    node.querySelector('.order-type').addEventListener('change', function (e) {
      setPair(key, {order_type: e.target.value});
    });
    node.querySelector('.tif').addEventListener('change', function (e) {
      setPair(key, {time_in_force: e.target.value});
    });
    node.querySelector('.increment').addEventListener('change', function (e) {
      setPair(key, {increment: parseFloat(e.target.value)});
    });
    // "Where do I change these?" — answered by the panel showing the
    // numbers, and answered with THIS ladder's own settings: the next
    // ladder is a different instrument with different costs.
    node.querySelectorAll('.open-exits, .ladder-cog').forEach(function (cog) {
      cog.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        openLadderSettings(node, key);
      });
    });
    node.querySelector('.ls-close').addEventListener('click', function () {
      node.querySelector('.ladder-settings').hidden = true;
    });
    node.querySelector('.ls-save').addEventListener('click', function () {
      saveLadderSettings(node, key);
    });
    // The pair type decides which legs have an expiry, so the form
    // follows it as it is changed.
    node.querySelector('.ladder-settings').addEventListener(
      'input', function (e) {
        // Which legs have an expiry follows the pair type, and it must
        // follow it as it is CHANGED — not only when the pane opens.
        if (e.target.classList.contains('ls-pair-type')) {
          var pane = node.querySelector('.ladder-settings');
          fairKindFields(pane, {pair_type: e.target.value});
        }
      });

    node.querySelector('.lock-scroll').addEventListener('change', function (e) {
      setLocked(key, e.target.checked);
    });
    node.querySelector('.recentre').addEventListener('click', function () {
      // Both halves: the ENGINE re-anchors the price window (the rows
      // themselves), and the browser scrolls to the mid. Doing only the
      // second would scroll to a market that had walked out of the
      // window the engine is still building.
      send('recentre_ladder', {pair: key});
      state.scrolledAt[key] = 0;
      state.busyAt[key] = 0;
      state.hovering[key] = false;
      state.centredAt[key] = 0;
      var market = (state.snapshot.pairs[key] || {}).market || {};
      if (market.spread !== undefined) { centreOnMid(node, key, market); }
    });
    node.querySelector('.filter').addEventListener('change', function (e) {
      state.filtered[key] = e.target.checked;
      render();
    });
    node.querySelector('.armed').addEventListener('input', function (e) {
      // Typed sizes are armed the moment they are typed: no Enter, no
      // second gesture. Blank goes back to the ladder's default rather
      // than arming zero, which would be a click that sends nothing.
      setArmed(key, 'buy', e.target.value);
      // Redraw THIS ladder as it is typed, so the button beside the box
      // carries the size before the next poll. The box being typed into
      // is never written back over — it has focus.
      if (state.snapshot.pairs[key]) {
        renderLadder(key, state.snapshot.pairs[key]);
      }
    });
    node.querySelector('.sell-qty').addEventListener('input', function (e) {
      setArmed(key, 'sell', e.target.value);
      if (state.snapshot.pairs[key]) {
        renderLadder(key, state.snapshot.pairs[key]);
      }
    });
    node.querySelector('.keypad').addEventListener('click', function (e) {
      var button = e.target.closest('.qty');
      if (!button) { return; }
      // The keypad arms BOTH sides: they are the same size in almost
      // every case, and the two boxes are there for the exception.
      var size = button.dataset.qty || '';
      setArmed(key, 'buy', size);
      setArmed(key, 'sell', size);
      render();
    });
    node.querySelector('.buy-touch').addEventListener('click', function () {
      atTouch(key, 'BUY');
    });
    node.querySelector('.sell-touch').addEventListener('click', function () {
      atTouch(key, 'SELL');
    });
    node.querySelector('.flatten').addEventListener('click', function () {
      flatten(key);
    });
    node.querySelector('.close-limit-go').addEventListener('click', function () {
      closeAtLimit(key, node.querySelector('.close-limit').value);
    });
    node.querySelector('.close-limit').addEventListener('keydown',
      function (e) {
        if (e.key === 'Enter') { closeAtLimit(key, e.target.value); }
      });
    node.querySelector('.refresh-feed').addEventListener('click', function () {
      // Re-subscribe both legs. The answer to "it is moving in MT5 and
      // stale here" that the trader can act on themselves.
      send('refresh_feed', {pair: key}, function (result) {
        var data = (result || {}).data || {};
        if (data.reason) { toast(data.reason, data.ok ? 'ok' : ''); }
      });
    });
    node.querySelector('.cxl-b').addEventListener('click', function () {
      send('cancel_where', {pair: key, side: 'BUY'});
    });
    node.querySelector('.cxl-s').addEventListener('click', function () {
      send('cancel_where', {pair: key, side: 'SELL'});
    });
    node.querySelector('.cxl-all').addEventListener('click', function () {
      send('cancel_where', {pair: key});
    });
    node.querySelector('.grid').addEventListener('scroll', function () {
      // A scroll we did not make is the trader looking somewhere:
      // leave the window where they put it (SCROLL_GRACE_MS).
      if (state.centring[key]) { return; }
      state.scrolledAt[key] = Date.now();
    });
    // Any of these means hands on: hold the ladder still (ladderIsBusy).
    var grid = node.querySelector('.grid');
    grid.addEventListener('pointerenter', function () {
      state.hovering[key] = true;
    });
    grid.addEventListener('pointerleave', function () {
      state.hovering[key] = false;
      state.busyAt[key] = Date.now();          // start the grace period
    });
    grid.addEventListener('pointerdown', function () {
      state.busyAt[key] = Date.now();
    });
    grid.addEventListener('pointermove', function () {
      state.busyAt[key] = Date.now();
    });
    node.querySelector('.grid tbody').addEventListener('click', function (e) {
      var cell = e.target.closest('td');
      if (!cell) { return; }
      var row = cell.closest('tr');
      var level = parseFloat(row.dataset.level);
      if (cell.classList.contains('ask')) {
        clickLevel(key, sideForColumn('ask'), level);
      } else if (cell.classList.contains('bid')) {
        clickLevel(key, sideForColumn('bid'), level);
      } else if (cell.classList.contains('work')) {
        // ADD one more at this level, on the side already resting
        // there. Clicking your own working order to DESTROY it is the
        // wrong default on a ladder: the ordinary thing a trader does
        // to a level they are working is want more of it, and pulling
        // has three other ways to happen (right-click here, the S / B
        // / CXL All buttons, and Cancel in the Working orders panel).
        if (!cell.dataset.side) { return; }
        clickLevel(key, cell.dataset.side, level);
      }
    });
    node.querySelector('.grid tbody').addEventListener(
      'contextmenu', function (e) {
        var cell = e.target.closest('td.work');
        if (!cell || !cell.dataset.orderId) { return; }
        e.preventDefault();
        send('cancel_order', {order_id: cell.dataset.orderId});
      });
  }

  function setArmed(key, side, value) {
    var size = parseFloat(value);
    var armed = state.armed[key] || (state.armed[key] = {});
    // Blank goes back to the ladder's default rather than arming zero,
    // which would be a click that sends nothing.
    armed[side] = (isFinite(size) && size > 0) ? size : null;
  }

  function armedFor(key, side) {
    var armed = state.armed[key] || {};
    return armed[String(side).toLowerCase()] || null;
  }

  function restingOn(quote) {
    /* WHERE the real pending is. In LIMIT mode one leg rests a pending
     * and the OTHER is crossed at market when it fills — so leg A
     * having no order at the broker is not a fault, it is the mode.
     * Said in words, because "leg a" on one panel was the only place
     * this appeared and it read as leg A having nothing. */
    if (!quote) { return ''; }
    if (!quote.ticket) {
      return ', not at the broker yet' +
        (quote.reason ? ' (' + quote.reason + ')' : '');
    }
    var leg = String(quote.leg || '').toUpperCase();
    return ', resting on leg ' + leg +
      (quote.symbol ? ' (' + quote.symbol + ')' : '') +
      ' at ' + fmt(quote.price, 5) + ' #' + quote.ticket +
      (quote.crosses_leg
        ? ' — leg ' + quote.crosses_leg + ' crosses at market when it fills'
        : '');
  }

  function sideForColumn(column) {
    /* Which SIDE a click in the bids or asks column sends.
     *
     * TT (default): the price-ladder convention every desk arrives
     * with — clicking BIDS joins the bid, which is a resting BUY, and
     * clicking ASKS joins the offer, which is a resting SELL.
     * TOUCH: the hit/lift reading — clicking ASKS lifts the offer.
     *
     * This is the ONLY place the mapping exists. It changes nothing
     * else: the level comes from the row that was clicked, and the
     * side reaches the engine already decided, so sizing, hedging and
     * execution are identical either way. The BUY and SELL buttons and
     * the B/S keys name their side outright and never come through
     * here.
     */
    // Absent means TOUCH, matching the engine's own default. The
    // fallback has to be the conservative one: a snapshot from before
    // this setting existed, or one that arrives without it, must send
    // exactly the side it always did rather than silently inverting
    // every click on the screen.
    var tt = state.snapshot.click_convention === 'TT';
    if (column === 'ask') { return tt ? 'SELL' : 'BUY'; }
    return tt ? 'BUY' : 'SELL';
  }

  function clickHint(column, level) {
    /* What this cell will actually DO, in the words of the convention
     * in force. A tooltip that says BUY while the click sends SELL is
     * worse than no tooltip at all, so it is built from the same
     * mapping the click uses rather than written out twice. */
    var side = sideForColumn(column);
    var legs = side === 'BUY' ? 'buy leg B, sell leg A'
                              : 'sell leg B, buy leg A';
    return 'Click: ' + side + ' the spread at ' + fmt(level, 4) +
      ' (' + legs + ')';
  }

  function clickLevel(key, side, level) {
    var pair = state.snapshot.pairs[key] || {};
    var armed = armedFor(key, side);
    var quantity = armed || pair.default_quantity;
    var payload = {pair: key, side: side, level: level};
    if (armed) { payload.quantity = armed; }

    // ONE CLICK IS ONE ORDER. A market click crosses both accounts
    // immediately — that is the product. The arming is what carries the
    // weight instead: the mode badge, the tinted columns, the cursor.
    // A desk that wants the extra gesture turns CONFIRM_MARKET_CLICKS on.
    if (pair.order_type === 'MARKET' && state.snapshot.confirm_market_clicks) {
      ask('Cross both legs now?',
          key + '\n' + side + ' ' + quantity + ' spread(s) at ' +
          fmt(level, 4) + '.\n\n' +
          'MARKET crosses both legs immediately. The clicked price is the ' +
          'slippage guard: a fill worse than it by more than the ' +
          'protection is refused.',
          'Send it', function () { fire(key, side, level, payload); });
      return;
    }
    fire(key, side, level, payload);
  }

  function fire(key, side, level, payload) {
    var pair = state.snapshot.pairs[key] || {};
    var ghost = {
      pair_key: key, side: side, level: level,
      quantity: payload.quantity || pair.default_quantity,
      market: pair.order_type === 'MARKET', at: Date.now()
    };
    state.pending.push(ghost);
    flash(key, level, side);
    render();
    send('click', payload, function (result) {
      ghost.done = true;
      if (result && result.ok === false) { ghost.failed = true; }
      render();
    });
  }

  function flash(key, level, side) {
    // The row lights up under the finger, before any round trip. It is
    // the difference between "did that register?" and knowing it did.
    var selector = '.ladder[data-pair="' + cssEscape(key) + '"] tr[data-level="'
      + level + '"]';
    var row = document.querySelector(selector);
    if (!row) { return; }
    row.classList.add(side === 'BUY' ? 'flash-buy' : 'flash-sell');
    window.setTimeout(function () {
      row.classList.remove('flash-buy');
      row.classList.remove('flash-sell');
    }, 350);
  }

  function prunePending() {
    var now = Date.now();
    state.pending = state.pending.filter(function (ghost) {
      // A ghost lives until the engine's own answer arrives, and never
      // more than a second: a stale ghost is a phantom order.
      if (ghost.failed) { return false; }
      if (ghost.done && now - ghost.at > 400) { return false; }
      return now - ghost.at < 1500;
    });
  }

  function pendingAt(key, level, increment) {
    var half = (increment || 0.0001) / 2;
    return state.pending.filter(function (ghost) {
      return ghost.pair_key === key && !ghost.failed &&
        Math.abs(ghost.level - level) < half;
    });
  }

  function setPair(key, fields) {
    send('set_pair', {pair: key, fields: fields});
  }

  //: The overnight rule in the trader's own words, for the read-only
  //: line on the Exit panel.
  var OVERNIGHT_WORDS = {
    ALLOW: 'hold', EXIT_IF_PROFIT: 'exit if profit', EXIT_ALWAYS: 'exit'
  };

  //: Every field in this ladder's settings: the class on the control,
  //: the name the engine and the config both know it by, and how to
  //: read it back. One list, because a form that saves a field it does
  //: not load — or loads one it cannot save — is a form that silently
  //: drops what was typed into it.
  //: Every field in this ladder's settings: the control, the name the
  //: engine and the config both know it by, how to read it, and — for
  //: the ones with a system-wide default — which setting that is.
  //:
  //: Three kinds, and the difference is the whole point of the form:
  //:
  //:   live    what the ladder is RUNNING. Never blank: a blank Mode
  //:           is not "no mode", it is a form that cannot tell you
  //:           what the ladder is doing — and one that would send
  //:           `order_type: null` to an engine that raises on it.
  //:   number  a number this ladder OWNS. There is no defaults page:
  //:           every ladder carries its own, so the field shows a
  //:           real value and saving writes it to this pair. A pair
  //:           that has never been configured starts from the
  //:           built-in value, shown plainly — nothing inherits, and
  //:           nothing is hidden behind a word.
  //:   text    a fact about this pair with no default at all — an
  //:           expiry, a swap. Blank is a real state here.
  var LADDER_FIELDS = [
    ['.ls-order-type', 'order_type', 'live'],
    ['.ls-exit-type', 'exit_type', 'live'],
    ['.ls-tif', 'time_in_force', 'live'],
    ['.ls-overnight', 'overnight', 'live'],
    ['.ls-increment', 'increment', 'live-number'],
    // NOT 'rows' on the snapshot — that is the ladder's PRICE ROWS,
    // an array of forty objects. A number input handed one shows
    // blank, which is how this field was empty on every ladder.
    ['.ls-rows', 'row_count', 'live-number'],
    ['.ls-qty', 'default_quantity', 'live-number'],
    // What ONE spread means in leg A lots. Blank is a real state and
    // the useful default: it means "the smallest size both legs can
    // actually clear", which is what the engine derives. Leg B is
    // never typed — it is the hedge of this one.
    // LIVE, never blank. A blank box now MEANS 1, so leaving it empty
    // while the engine runs 0.10 would read as "derived" and send 1 on
    // the next Apply — a ten-fold resize nobody typed.
    ['.ls-clip-a', 'clip_lots_a', 'live-number'],
    ['.ls-clip-b', 'clip_lots_b', 'live-number'],
    // Blank is a REAL state here and the normal one: it means the
    // instrument master's LotSize, which is right almost always — and
    // units on the wire are lots x LotSize, so a number typed here
    // wrongly is a hundredfold order that FILLS.
    ['.ls-contract-a', 'contract_size_a', 'blank-number'],
    ['.ls-contract-b', 'contract_size_b', 'blank-number'],
    // NRML or MIS. Not a display preference: the exchange nets per
    // (symbol, PRODUCT), and the broker squares MIS off near the close
    // without asking.
    ['.ls-product', 'product', 'live'],
    ['.ls-quoting', 'quoting_leg', 'live'],
    ['.ls-auto-route', 'auto_route', 'check'],
    // The Fair Spread window. One tick, and `algo` follows it on the
    // engine — there is one algo, and a dropdown to pick it plus a
    // tick to see it was two controls for one decision.
    ['.ls-algo-window', 'algo_window', 'check'],
    // Blank is a REAL state: it means the desk-wide threshold.
    ['.ls-stale', 'max_quote_age_sec', 'blank-number'],
    ['.ls-slip', 'slippage_allowance', 'number', 'SLIPPAGE_ALLOWANCE'],
    ['.ls-nights', 'break_even_nights', 'number', 'BREAK_EVEN_NIGHTS'],
    ['.ls-tp', 'tp_target_pct_of_margin', 'number', 'TP_TARGET_PCT_OF_MARGIN'],
    // THE TWO FIELDS THAT REPLACED FOUR SWAP BOXES.
    //
    // An Indian future pays no overnight financing: its carry is IN
    // the price, which is what the basis is. For a physically-settled
    // commodity — every MCX metal and energy contract — that carry is
    // money plus warehousing, so it is an interest rate and a storage
    // cost rather than a swap the broker quotes.
    //
    // Blank is a real state on both: no fair value, and the box shows
    // an em dash rather than a number built on nothing.
    ['.ls-carry-rate', 'carry_rate_pct', 'blank-number', 'CARRY_RATE_PCT'],
    ['.ls-storage', 'storage_per_unit_year', 'blank-number',
     'STORAGE_PER_UNIT_YEAR'],
    ['.ls-pair-type', 'pair_type', 'live']

  ];

  function openLadderSettings(node, key) {
    /* THIS ladder's settings, showing what is ACTUALLY IN FORCE.
     *
     * Three sources, because there are three kinds of field. What the
     * ladder is RUNNING comes from the snapshot — it is the only place
     * that knows, and a form that shows Mode blank while the title bar
     * says LIMIT is a form nobody can trust. A number with a
     * A cost or a target is this pair's OWN number: there is no
     * defaults page, so the box shows a real value — seeded from the
     * built-in one for a pair nobody has configured yet — and saving
     * writes it to this pair. And a fact about this pair alone — an
     * expiry, a swap — comes from the config, where blank is a real
     * state.
     */
    var pane = node.querySelector('.ladder-settings');
    if (!pane) { return; }
    pane.hidden = false;
    node.querySelector('.ls-pair').textContent = key;
    Promise.all([
      fetch('/api/pairs').then(function (r) { return r.json(); }),
      fetch('/api/settings').then(function (r) { return r.json(); })
    ]).then(function (answers) {
      var saved = ((answers[0] || {}).pairs || {})[key] || {};
      var settings = (answers[1] || {}).settings || {};
      var live = (state.snapshot.pairs || {})[key] || {};
      LADDER_FIELDS.forEach(function (entry) {
        var input = pane.querySelector(entry[0]);
        if (!input) { return; }
        var field = entry[1];
        var kind = entry[2];
        var own = saved[field];
        if (field === 'algo_window' && own === undefined) {
          own = saved.show_fair_window;              // the old name
        }
        input.dataset.touched = '';
        if (kind === 'check') {
          input.checked = !!(own === undefined || own === null
            ? live[entry[4] || field] : own);
          return;
        }
        if (kind === 'live' || kind === 'live-number') {
          // What the ladder is running, whatever the file does or does
          // not say. Never blank.
          var running = live[entry[4] || field];
          if (own === undefined || own === null || own === '') {
            input.value = running === undefined || running === null
              ? '' : running;
          } else {
            input.value = own;
          }
          if (field === 'quoting_leg' && !input.value) { input.value = ''; }
          return;
        }
        if (kind === 'number' || kind === 'blank-number') {
          // This ladder's own number. A pair that has never been
          // configured starts from the built-in value, shown plainly:
          // there is no defaults page, so what is in the box is what
          // this pair uses.
          var seed = settings[entry[3]];
          var value = (own === undefined || own === null || own === '')
            ? seed : own;
          input.value = (value === undefined || value === null)
            ? '' : value;
          input.title = kind === 'blank-number'
            ? 'this pair’s own. Leave it empty for no cross-check.'
            : 'this pair’s own. Every ladder carries its own numbers.';
          return;
        }
        input.value = (own === null || own === undefined) ? '' : own;
      });
      fairKindFields(pane, {pair_type: saved.pair_type
        || live.pair_type || 'SPOT_FUTURE'});
      clipNote(pane, live);
      measuredSlippageInto(pane);
    }).catch(function (e) {
      toast('could not read this pair: ' + e);
    });
  }

  function clipNote(pane, live) {
    /* What one spread IS, right now, in both legs and in money.
     *
     * Leg B is not a field: it is the hedge — leg A x contract A /
     * (beta x contract B), rounded down — and typing a number into it
     * independently is how a "hedged" pair ends up net long one leg.
     * So it is reported instead, beside the leg A box that decides it.
     */
    var note = pane.querySelector('.ls-clip-note');
    if (!note) { return; }
    var lotsA = live.clip_lots_a || 1;
    var lotsB = live.clip_lots_b || 1;
    var qty = live.default_quantity || 1;
    // PER QTY AND FOR THE QTY IN THE BOX, side by side. The first is
    // the figure the exit levels are priced per and does not move when
    // the keypad is touched; the second is the real money on the click
    // about to be made.
    // `spread_units` on the snapshot is ALREADY per one Qty — it is
    // clip_lots_b x contract B — so it is the per-unit figure, not the
    // one for the click.
    var perQty = live.spread_units || 0;
    // ...and what a lot IS, in the instrument's own units. The desk
    // thinks in ounces and barrels, not lots: "when a trader places 1,
    // in the backend it places 100 oz". The contract size comes from
    // MT5 and is never typed, so this is the one place the two
    // readings meet.
    function overridden(used, fromMt5, symbol) {
      if (!used || !fromMt5 || Math.abs(used - fromMt5) < 1e-9) { return ''; }
      return (symbol || '') + ' set to ' + fmt(used, 2) +
        ', MT5 says ' + fmt(fromMt5, 2);
    }
    function inUnits(lots, contract) {
      return contract ? ' = ' + fmt(lots * contract, 2) + ' units' : '';
    }
    note.textContent =
      'In force: 1 Qty = ' + fmt(lotsA, 2) + ' lots ' +
      (live.symbol_a || 'A') + inUnits(lotsA, live.contract_a) +
      ' / ' + fmt(lotsB, 2) + ' lots ' +
      (live.symbol_b || 'B') + inUnits(lotsB, live.contract_b) +
      (perQty ? ' — ' + money(perQty) + ' per 1.00 of spread' : '') +
      '. Qty ' + fmt(qty, 2) + ' sends ' + fmt(lotsA * qty, 2) + ' / ' +
      fmt(lotsB * qty, 2) + ' lots' +
      (perQty ? ', ' + money(perQty * qty) + ' per 1.00' : '') +
      // WHERE THE CONTRACT SIZE CAME FROM. Normally MT5's, and said so.
      // An override is stated as an override, with MT5's own number
      // beside it: every money figure on this ladder runs through it.
      (overridden(live.contract_a, live.contract_a_mt5, live.symbol_a) ||
       overridden(live.contract_b, live.contract_b_mt5, live.symbol_b)
        ? ' CONTRACT SIZE OVERRIDDEN — ' +
          [overridden(live.contract_a, live.contract_a_mt5, live.symbol_a),
           overridden(live.contract_b, live.contract_b_mt5, live.symbol_b)]
            .filter(Boolean).join('; ') + '.'
        : ' Contract sizes come from MT5 and are never typed in.');
  }

  function fairKindFields(pane, saved) {
    /* An expiry belongs to a CONTRACT, and a fair spread to a pair
     * whose legs are the same underlying. Spot vs a future: only leg B
     * expires. Two futures: both do. Two different instruments — WTI
     * against Brent — have no carry between them and no fair spread at
     * all, and asking for a date there is asking for something that
     * does not exist. */
    var type = (saved.pair_type || 'SPOT_FUTURE').toUpperCase();
    var shape = {
      SPOT_FUTURE: {a: false, b: true,
                    note: 'spot vs a future — only leg B expires'},
      FUTURE_FUTURE: {a: true, b: true,
                      note: 'two futures — both legs expire, and the '
                        + 'carry runs to the NEAR one'},
      RELATED: {a: false, b: false,
                note: 'two different instruments — no carry ties them '
                  + 'together, and there is no fair spread to quote'}
    }[type] || {a: true, b: true, note: ''};
    var note = pane.querySelector('.ls-pairtype');
    if (note) { note.textContent = shape.note; }
    [['.row-expiry-a', shape.a, 'Leg A is spot — spot does not expire'],
     ['.row-expiry-b', shape.b, 'this pair has no expiry']].forEach(
      function (entry) {
        var row = pane.querySelector(entry[0]);
        if (!row) { return; }
        var input = row.querySelector('input');
        row.classList.toggle('not-applicable', !entry[1]);
        input.disabled = !entry[1];
        input.placeholder = entry[1] ? 'from MT5' : entry[2];
      });
  }

  function measuredSlippageInto(pane) {
    /* The allowance is a BUDGET; this is what slippage actually cost.
     * Beside it, so the number is corrected from data. */
    fetch('/api/slippage').then(function (r) { return r.json(); })
      .then(function (body) {
        var stats = body && body.ok && body.overall && body.overall.round_trip;
        var row = pane.querySelector('.ls-slip');
        if (!row) { return; }
        row.title = (stats && stats.money_mean !== null &&
                     stats.money_mean !== undefined)
          ? row.title + ' MEASURED this session: ' +
            stats.money_mean.toFixed(2) + ' a round turn over ' +
            stats.measured + ' position(s).'
          : row.title;
      }).catch(function () { /* no engine: the row says nothing extra */ });
  }

  function saveLadderSettings(node, key) {
    /* What the form sends, and what it must never send.
     *
     * A LIVE field — Mode, Time in force, Overnight — is never sent as
     * null. `OrderType(null)` raises on the engine, so a blank Mode
     * used to make Apply fail silently: the save reported success and
     * the ladder went on running whatever it had.
     *
     * An INHERITED number that has not been touched is sent as null,
     * which means "keep using the system default" — not as the number
     * it happens to be showing, which would silently freeze today's
     * default into this pair for ever.
     */
    var pane = node.querySelector('.ladder-settings');
    var payload = {};
    var live = {};
    LADDER_FIELDS.forEach(function (entry) {
      var input = pane.querySelector(entry[0]);
      if (!input) { return; }
      var field = entry[1];
      var kind = entry[2];
      var raw = (input.value || '').trim();
      var value;
      if (kind === 'check') {
        value = input.checked;
      } else if (kind === 'live') {
        if (!raw && field !== 'quoting_leg') { return; }   // never a null
        value = raw || null;              // '' on quoting_leg = wider book
      } else if (kind === 'live-number') {
        if (!raw) { return; }
        value = parseFloat(raw);
        if (isNaN(value)) { return; }
      } else if (kind === 'number') {
        // Emptied, it goes back to the built-in value rather than to a
        // null the engine would read as "nothing set" — with no
        // defaults page there is nothing for a null to mean.
        value = raw === '' ? null : parseFloat(raw);
        if (value !== null && isNaN(value)) { return; }
      } else if (kind === 'blank-number') {
        value = raw === '' ? null : parseFloat(raw);
        if (value !== null && isNaN(value)) { return; }
      } else {
        value = raw === '' ? null : raw;
      }
      payload[field] = value;
      if (value !== null) { live[field] = value; }
    });
    fetch('/api/pairs/' + encodeURIComponent(key), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }).then(function (r) { return r.json(); }).then(function (answer) {
      if (!answer || !answer.ok) {
        toast('could not save: ' + ((answer && answer.error) || 'unknown'));
        return;
      }
      // Saved AND applied: the engine re-reads the file on its next
      // poll, and the live send changes the ladder in front of the
      // trader now rather than a poll later.
      setPair(key, live);
      var row = (state.snapshot.pairs || {})[key];
      if (row) {
        row.algo_window = !!payload.algo_window;
        row.algo = payload.algo_window ? 'FAIR_SPREAD' : 'NONE';
      }
      (answer.notes || []).forEach(function (note) { toast(note); });
      if (!(answer.notes || []).length) { toast('applied to ' + key, 'ok'); }
      pane.hidden = true;
      // One door for the window, so the pane and the window's own X
      // cannot disagree about whether it is open.
      var fairId = panelId('fair', key);
      var wanted = !!payload.algo_window;
      if (wanted !== (state.open.indexOf(fairId) >= 0)) {
        if (wanted) { openPanel(fairId); } else { closePanel(fairId); }
      } else {
        render();
      }
    }).catch(function (e) { toast('could not save: ' + e); });
  }

  function atTouch(key, side) {
    // Hit the touch without hunting for the row: the fastest
    // possible "I want in, now" on the ladder already in front of you.
    var row = state.snapshot.pairs[key];
    if (!row) { return; }
    // BUY lifts the long spread, SELL hits the short one — the price the
    // market is actually offering for that direction, never the mid.
    var level = side === 'BUY' ? row.long_spread : row.short_spread;
    if (level === null || level === undefined) {
      toast(key + ' has no price yet — nothing to hit');
      return;
    }
    clickLevel(key, side, level);
  }

  function closeAtLimit(key, value) {
    /* Close everything on this ladder AT A PRICE, instead of crossing
     * now. One working order per position, carrying that position's
     * ticket so it CLOSES — on a hedging account an opposite order
     * without it opens a second position. A target, and no stop. */
    var row = state.snapshot.pairs[key] || {};
    var level = parseFloat(value);
    if (!isFinite(level)) {
      toast('type the spread level to close at, beside the button');
      return;
    }
    if (!row.net_position) {
      toast((row.name || key) + ' is already flat — nothing to close');
      return;
    }
    ask('Rest a closing order at ' + fmt(level, 4) + '?',
        (row.name || key) + '\n' +
        (row.net_position > 0 ? '+' : '') + row.net_position +
        ' spreads open.\n\nOne working order per position, at that ' +
        'level, by ticket. It WAITS there — nothing crosses now, and ' +
        'there is no stop. CLOSE ALL is the one that crosses.',
        'Rest it', function () {
          send('close_at_limit', {pair: key, level: level},
               function (result) {
                 var data = (result || {}).data || {};
                 if (data.reason) { toast(data.reason, data.ok ? 'ok' : ''); }
                 else if (data.ok) {
                   toast('closing order(s) resting at ' + fmt(level, 4), 'ok');
                 }
               });
        });
  }

  function exitPair(key) {
    /* The ladder's DEFAULT way out — what F does.
     *
     * MARKET crosses now; LIMIT rests at the price on the rail. Both
     * buttons stay on the screen whichever is selected: this decides
     * which one is the default, never which one exists.
     */
    var node = document.querySelector(
      '.ladder[data-pair="' + cssEscape(key) + '"]');
    var row = state.snapshot.pairs[key] || {};
    if ((row.exit_type || 'MARKET') !== 'LIMIT') { return flatten(key); }
    var box = node && node.querySelector('.close-limit');
    return closeAtLimit(key, box ? box.value : '');
  }

  function flatten(key) {
    var row = state.snapshot.pairs[key] || {};
    if (!row.net_position) {
      toast((row.name || key) + ' is already flat');
      return;
    }
    // Flattening is irreversible and it is the button pressed in a
    // hurry, so it asks — but with one key, and only once.
    ask('Flatten ' + (row.name || key) + '?',
        (row.net_position > 0 ? '+' : '') + row.net_position +
        ' spreads, at market, by ticket. This cannot be undone.',
        'Flatten now', function () { send('flatten_pair', {pair: key}); });
  }

  function money_(value, currency) {
    /* Compact money for the rail: 15,764 rather than 15,764.37. The
     * rail is 120px wide and the cents are never the question there. */
    if (value === undefined || value === null || !isFinite(value)) {
      return DASH;
    }
    return Math.round(value).toLocaleString('en-IN') +
      (currency ? ' ' + currency : '');
  }

  function fitLadderFloor(node) {
    /* The ladder's floor, measured on THIS machine.
     *
     * A number typed into the stylesheet is a number measured against
     * one font on one box. Segoe UI on Windows sets taller than the
     * font this was designed against, so a rail that fits by four
     * pixels here goes behind a scrollbar there — which is exactly
     * what happened, twice.
     *
     * So it is measured: the rail's own children, the chrome above and
     * below, and nothing else. Written only when it CHANGES, because
     * this runs on every render.
     */
    var rail = node.querySelector('.rail');
    var footer = node.querySelector('.footer');
    if (!rail || !footer || !rail.children.length) { return; }
    // Measured from the first child's top to the last one's bottom, so
    // the gaps AND the margins are in it. Adding up offsetHeight
    // misses margins, and the four pixels that costs is the difference
    // between fitting and a scrollbar.
    var first = rail.firstElementChild.getBoundingClientRect();
    var last = rail.lastElementChild.getBoundingClientRect();
    var need = (last.bottom - first.top) + 8;     // + the rail's padding
    var chrome = 0;
    ['.titlebar', '.quotestrip'].forEach(function (selector) {
      var part = node.querySelector(selector);
      if (part) { chrome += part.offsetHeight; }
    });
    chrome += footer.scrollHeight + 2;
    // Never taller than the desktop it is on. `min-height` beats
    // `max-height` in CSS, so a floor measured larger than the screen
    // would push the footer off the bottom of a desktop that does not
    // scroll vertically — the books gone, and no way to get them back.
    var desktop = desktopBox();
    var floor = Math.ceil(need + chrome);
    if (desktop.height) { floor = Math.min(floor, Math.floor(desktop.height)); }
    if (!floor || node._floor === floor) { return; }
    node._floor = floor;
    node.style.minHeight = floor + 'px';
  }

  function renderLadder(key, row) {
    var node = ladderNode(key);
    var market = row.market || {};
    // The ENGINE holds the lock; the browser only shows it. On the
    // first draw of a pair — a reload, or a second screen — adopt what
    // the engine reports rather than starting unticked over a window
    // the engine is still holding still.
    if (state.locked[key] === undefined) {
      state.locked[key] = !!row.ladder_locked;
      var first = node.querySelector('.lock-scroll');
      if (first) { first.checked = state.locked[key]; }
    }
    node.classList.toggle('mode-market', row.order_type === 'MARKET');
    node.classList.toggle('inactive', state.active !== panelId('ladder', key));
    node.querySelector('.title').textContent = row.name || key;
    node.querySelector('.route').textContent =
      (row.account_a || '?') + ' → ' + (row.account_b || '?');
    // AutoRouting is armed on ONE ladder at a time and changes what a
    // FILL does, so it is stated where the mode is stated — and where
    // it is visible without opening anything. Ticking the box has to
    // change the screen, or it reads as having done nothing.
    // Which leg rests the real pending, and which one crosses. Read
    // from the ENGINE's own groups where there is one working, and
    // from the pair's setting before that.
    var note = node.querySelector('.quoting-note');
    if (note) {
      var live = (row.quotes || [])[0];
      var leg = (live && live.leg) ||
        (row.quoting_leg_effective
          ? row.quoting_leg_effective.toUpperCase() : '');
      var symbol = (live && live.symbol) ||
        (leg === 'A' ? row.symbol_a : leg === 'B' ? row.symbol_b : '');
      // WHY AN ORDER IS NOT AT THE BROKER, IN WORDS, WITHOUT HOVERING.
      //
      // An entry whose pending was never placed says why — but only in
      // the Work cell's tooltip, which nobody hovers when the thing
      // they are looking at is an order that appears to be working.
      // "W:1 (broker 0)" in 9px in the footer was the only other sign.
      // A trader reading the ladder must be able to see that their
      // order is in this process and NOWHERE ELSE.
      var held = null;
      (row.quotes || []).forEach(function (quote) {
        if (held || quote.intent === 'CLOSE' || quote.ticket) { return; }
        held = quote.reason || 'not at the broker yet';
      });
      note.textContent = held
        ? 'NOT AT THE BROKER: ' + held
        : (row.order_type === 'MARKET'
            ? 'MARKET: both legs cross at once'
            : (leg
                ? 'LIMIT: quotes leg ' + leg + (symbol ? ' · ' + symbol : '') +
                  ' · leg ' + (leg === 'A' ? 'B' : 'A') + ' crosses on fill'
                : 'LIMIT: quoting leg picked from the wider book'));
      note.classList.toggle('live', !!(live && live.ticket) && !held);
      note.classList.toggle('held', !!held);
      note.title = held
        ? 'This ladder has a working order that exists in THIS process '
          + 'and nowhere else — there is nothing of yours at either '
          + 'broker for it. It rests as soon as the reason above clears.'
        : 'LIMIT mode rests a real pending on ONE leg and crosses the '
          + 'other at market when it fills. Leg A showing no PENDING in '
          + 'MT5 is the mode working, not a fault: it takes a position '
          + 'the instant the quoting leg fills, and never an order '
          + 'before that.';
    }
    var badge = node.querySelector('.mode-badge');
    // The EFFECTIVE state, never the ladder's box alone: with the
    // master switch off the box is ticked and nothing arms, and a
    // badge reading AUTO there says a fill will rest a target when no
    // fill will.
    var autoOn = !!row.auto_route_on;
    var autoHeld = !!row.auto_route && !autoOn;
    badge.textContent = row.order_type + ' · ' + row.time_in_force
      + (autoOn ? ' · AUTO' : (autoHeld ? ' · AUTO OFF' : ''));
    badge.title = autoOn
      ? 'AutoRouting is ON for this ladder: a fill rests a working '
        + 'order to close at the take-profit. A target, and no stop.'
      : (autoHeld
          ? 'this ladder has AutoRouting ticked, but it is switched off '
            + 'for the whole system (AUTO_ROUTE_ENABLED) — a fill arms '
            + 'nothing'
          : 'the mode a click sends, and how long a working order lives');
    badge.classList.toggle('auto', autoOn);
    badge.classList.toggle('auto-held', autoHeld);

    var change = market.net_change;
    var netchg = node.querySelector('.netchg');
    netchg.textContent = change === null || change === undefined
      ? DASH : (change > 0 ? '+' : '') + fmt(change, 4);
    netchg.classList.toggle('up', change > 0);

    var session = market.session || {};
    node.querySelector('.stat-h').textContent = fmt(session.high, 4);
    node.querySelector('.stat-l').textContent = fmt(session.low, 4);
    node.querySelector('.stat-o').textContent = fmt(session.open, 4);
    // Per LEG, never added: one lot of spot and one of the future are
    // not two lots of anything.
    node.querySelector('.stat-v1').textContent = fmt(session.volume_a, 0);
    node.querySelector('.stat-v2').textContent = fmt(session.volume_b, 0);
    // Whose numbers these are — the terminals', or ours since this
    // process started. A borrowed range read as the exchange's is how a
    // spread gets judged against prices nobody traded.
    var ours = node.querySelector('.ours');
    ours.textContent = session.ours === false ? 'legs' : 'ours';
    ours.classList.toggle('legs', session.ours === false);

    setValue(node.querySelector('.order-type'), row.order_type);
    // WHICH way out is this ladder's default — said on the buttons
    // themselves, because F fires one of them and a trader must not
    // have to remember which. CLOSE ALL still crosses now either way:
    // the way out does not change meaning under the button pressed in
    // a hurry, it only stops being the DEFAULT one.
    var atLimit = (row.exit_type || 'MARKET') === 'LIMIT';
    var flat = node.querySelector('.flatten');
    var lmt = node.querySelector('.close-limit-go');
    if (flat && lmt) {
      flat.classList.toggle('primary-exit', !atLimit);
      lmt.classList.toggle('primary-exit', atLimit);
      flat.title = flat.title.replace(/ \(F\)$/, '') + (atLimit ? '' : ' (F)');
      lmt.title = lmt.title.replace(/ \(F\)$/, '') + (atLimit ? ' (F)' : '');
    }
    setValue(node.querySelector('.tif'), row.time_in_force);
    setValue(node.querySelector('.increment'), row.increment);

    // One box per SIDE, each beside the button that sends it. Never
    // blank — a box shows the size that click WILL send, which is the
    // armed one if there is one and the ladder's default otherwise —
    // and never written while it is being typed in, or the poll would
    // eat the size as it is entered.
    // THE KEYPAD OFFERS ONLY WHAT CAN BE TRADED.
    //
    // A ladder whose broker caps leg A at 10 lots was showing 50 and
    // 100 buttons, and pressing one got a refusal back — "Qty 100 x 1
    // wants 100 lots on leg A, over this broker's 10-lot maximum". The
    // arithmetic was right and the button should not have been there.
    var cap = (row.max_qty === null || row.max_qty === undefined)
      ? null : Number(row.max_qty);
    node.querySelectorAll('.keypad .qty').forEach(function (button) {
      var size = Number(button.dataset.qty);
      var beyond = cap !== null && button.dataset.qty && size > cap;
      button.disabled = !!beyond;
      button.title = beyond
        ? 'Qty ' + size + ' is over what this pair can trade — it tops '
          + 'out at ' + cap
        : '';
    });

    ['buy', 'sell'].forEach(function (side) {
      var box = node.querySelector(side === 'buy' ? '.armed' : '.sell-qty');
      var armed = armedFor(key, side);
      if (document.activeElement !== box) {
        box.value = armed ? String(armed)
          : (row.default_quantity === null ||
             row.default_quantity === undefined
              ? '' : String(row.default_quantity));
      }
      box.classList.toggle('on', !!armed);
      var size = armed || row.default_quantity;
      // OVER WHAT THE BROKER WILL TAKE. The refusal already says so
      // once the click is made; this says it before, on the box the
      // size was typed into.
      box.classList.toggle('over', cap !== null && size > cap);
      box.title = cap === null ? ''
        : 'this pair tops out at Qty ' + cap + ' — the smaller of what '
          + 'the two brokers will take';
      var sized = (size === null || size === undefined) ? '' : ' ' + size;
      var button = node.querySelector(
        side === 'buy' ? '.buy-touch' : '.sell-touch');
      button.textContent = (side === 'buy' ? 'BUY' : 'SELL') + sized;
    });
    Array.prototype.forEach.call(node.querySelectorAll('.keypad .qty'),
      function (button) {
        var size = parseFloat(button.dataset.qty);
        button.classList.toggle('on',
          armedFor(key, 'buy') === size && armedFor(key, 'sell') === size);
      });
    // Which accounts this ladder is routed across, and their logins.
    var accounts = state.snapshot.accounts || {};
    function routeText(account, symbol) {
      var info = accounts[account] || {};
      var login = info.login ? ' #' + info.login : '';
      // EQUITY, not balance: balance ignores what is open, and the
      // number a trader sizes the next spread against is the one that
      // already carries the running P&L. Unmeasured stays a dash —
      // a leg that could not be read must never read as zero money.
      var money = info.equity === undefined || info.equity === null
        ? DASH
        : money_(info.equity, info.currency);
      return (symbol || '?') + '<div class="hint">' + (account || '?') +
        login + '</div><div class="hint route-eq" title="Equity: balance ' +
        'plus the P&L of everything open on this account">' + money +
        '</div>';
    }
    node.querySelector('.route-a b').innerHTML =
      routeText(row.account_a, row.symbol_a);
    node.querySelector('.route-b b').innerHTML =
      routeText(row.account_b, row.symbol_b);
    // The three cancel buttons, with what they would pull ON them —
    // and disabled when that is nothing, so a button that cannot do
    // anything cannot be pressed and wondered about.
    var buys = row.working_buys || 0;
    var sells = row.working_sells || 0;
    node.querySelector('.count-b').textContent = buys || '';
    node.querySelector('.count-s').textContent = sells || '';
    node.querySelector('.count-all').textContent = (buys + sells) || '';
    node.querySelector('.cxl-b').disabled = !buys;
    node.querySelector('.cxl-s').disabled = !sells;
    node.querySelector('.cxl-all').disabled = !(buys + sells);

    renderRows(node, key, row);

    var counts = node.querySelector('.counts');
    counts.querySelector('.cnt-b').textContent = 'B:' + bought(row);
    counts.querySelector('.cnt-s').textContent = 'S:' + sold(row);
    var working = row.working_buys + row.working_sells;
    var resting = row.broker_pendings;
    // How many of ours SHOULD be at the broker. A resting close is
    // held here and fired by ticket — it can never have a pending —
    // so counting it against the broker's number made every reducing
    // click read as two orders that had failed to place, in red.
    var closes = row.resting_closes || 0;
    var expected = working - closes;
    var disagree = resting !== undefined && resting !== expected;
    counts.querySelector('.cnt-w').textContent =
      'W:' + working +
      (closes ? ' (' + closes + ' resting close' + (closes > 1 ? 's' : '')
                + ')' : '') +
      (disagree ? ' (broker ' + resting + ')' : '');
    counts.querySelector('.cnt-w').classList.toggle('mismatch', disagree);
    counts.querySelector('.cnt-w').title = closes
      ? closes + ' of these are resting CLOSES: the level is held here '
        + 'and both legs are closed by ticket when the market reaches '
        + 'it, so nothing of theirs is at the broker and nothing should be.'
      : 'Our working orders, and the broker\u2019s own count when they '
        + 'disagree.';

    var feed = node.querySelector('.feed');
    feed.textContent = market.feed_badge || DASH;
    feed.className = 'feed ' + badgeClass(market.feed_badge);
    node.querySelector('.pos').textContent = row.net_position
      ? (row.net_position > 0 ? '+' : '') + row.net_position + ' @ ' +
        fmt(row.avg_entry, 4)
      : 'flat';
    var pnl = node.querySelector('.pnl');
    pnl.textContent = row.open_pnl === null || row.open_pnl === undefined
      ? DASH : money(row.open_pnl);
    pnl.classList.toggle('up', row.open_pnl > 0);
    pnl.classList.toggle('down', row.open_pnl < 0);
    // HIDDEN ON REQUEST, not removed. The numbers are correct — Qty is
    // in SPREADS and 0.01 lots is what one spread IS, from
    // L_B = L_A x C_A / (beta x C_B) — but reading "Qty 1" and
    // "0.01 lots" side by side reads as a contradiction on the desk.
    // The derivation still lives in the element's tooltip. To put it
    // back, restore this line.
    node.querySelector('.clip').textContent = '';
    // node.querySelector('.clip').textContent =
    //   '1 spread = ' + fmt(row.clip_lots_a, 2) + ' A / ' +
    //   fmt(row.clip_lots_b, 2) + ' B, ' + money(row.spread_units) +
    //   ' per 1.00';
    node.querySelector('.errors').textContent = (row.errors || []).join(' ');
    renderLegBook(node, row, market);
    // After the books are in: the floor includes them, and they are the
    // part whose height depends on the symbols' names.
    fitLadderFloor(node);
    return node;
  }

  function depthText(size, isTouch, mark) {
    if (size === null || size === undefined) {
      return isTouch ? mark : '';
    }
    // Rounded to two, because a spread's implied size is a ratio of two
    // lot sizes and 3.3333333 is not a number anyone can act on.
    var text = size >= 10 ? String(Math.round(size)) : size.toFixed(2);
    return text.replace(/\.00$/, '');
  }

  function renderLegBook(node, row, market) {
    /* The two legs' books, at the BOTTOM of the window.
     *
     * Moved there rather than merely placed there: a page served from
     * a template an older process cached can have this panel somewhere
     * else entirely, and where it sits is not a detail — it is beside
     * the ladder's own prices, where it would be read as part of them.
     * One line here makes the position true of whatever markup the
     * browser was given.
     */
    var legs = node.querySelector('.legs');
    var footer = node.querySelector('.footer');
    if (!legs || !footer) { return; }
    if (legs.parentNode !== footer) { footer.appendChild(legs); }
    legs.innerHTML = legFeed(row, market);

  }

  function renderAlgo(node, row) {
    /* Which algo this ladder is running. FAIR SPREAD, or none.
     *
     * It MEASURES: it does not place, modify or cancel an order, and a
     * click on the ladder behaves identically either way.
     */
    var block = row.algo_block || {};
    var selected = block.algo || row.algo || 'NONE';
    var kind = node.querySelector('.fair-kind');
    if (kind) {
      // Which arithmetic this pair gets — spot against a future, a
      // calendar, or two instruments with no carry between them.
      kind.textContent = selected === 'FAIR_SPREAD'
        ? (block.kind_note || '') : '';
    }
  }

  function renderFair(node, row) {
    /* What the CARRY says this basis should be — both directions.
     *
     * Two columns, because buying the spread pays the offer and
     * selling it receives the bid, and those are charged different
     * swaps on different legs. One number here would be right half the
     * time, and the gap would be measured against a price nobody
     * fills at.
     *
     * Never an instruction. Nothing here places or withholds an order.
     */
    var fair = row.fair || {};
    var buy = node.querySelector('.fair-buy');
    if (!buy) { return; }          // a page from an older template
    var digits = digitsFor(row.increment);
    node.querySelector('.fair-buy').textContent = fmt(fair.fair_buy, digits);
    node.querySelector('.fair-sell').textContent = fmt(fair.fair_sell, digits);
    // Rich = the market is above what the carry justifies, which is the
    // side a trader sells. Said in colour as well as sign: the
    // direction of a basis is the thing everyone gets backwards once.
    // THE KEYPAD OFFERS ONLY WHAT CAN BE TRADED.
    //
    // A ladder whose broker caps leg A at 10 lots was showing 50 and
    // 100 buttons, and pressing one got a refusal back — "Qty 100 x 1
    // wants 100 lots on leg A, over this broker's 10-lot maximum". The
    // arithmetic was right and the button should not have been there.
    var cap = (row.max_qty === null || row.max_qty === undefined)
      ? null : Number(row.max_qty);
    node.querySelectorAll('.keypad .qty').forEach(function (button) {
      var size = Number(button.dataset.qty);
      var beyond = cap !== null && button.dataset.qty && size > cap;
      button.disabled = !!beyond;
      button.title = beyond
        ? 'Qty ' + size + ' is over what this pair can trade — it tops '
          + 'out at ' + cap
        : '';
    });

    ['buy', 'sell'].forEach(function (side) {
      var cell = node.querySelector('.gap-' + side);
      var gap = fair['gap_' + side];
      if (gap === null || gap === undefined) {
        cell.textContent = '—';
        cell.className = 'gap-' + side;
        cell.title = '';
        return;
      }
      cell.textContent = (gap > 0 ? '+' : '') + fmt(gap, digits);
      cell.className = 'gap-' + side + ' ' + (gap > 0 ? 'down' : 'up');
      cell.title = gap > 0
        ? 'the market is RICH to its own carry here'
        : 'the market is CHEAP to its own carry here';
    });
    // The rail is 100px wide: a sentence does not fit in it. The short
    // form is shown; the engine's full wording is the tooltip.
    var note = node.querySelector('.fair-note');
    var expiring = fair.days_to_expiry;
    note.textContent = fair.fair_buy === null || fair.fair_buy === undefined
      ? (fair.expects_expiry === false ? '' : 'set expiry + swap')
      : (expiring === null || expiring === undefined
          ? '' : expiring + 'd to expiry');
    note.title = fair.note || '';

    // A swap that disagrees with an annual rate — or a long leg showing
    // a credit — REPLACES the reading rather than printing beneath it.
    var warn = node.querySelector('.fair-warn');
    var fix = node.querySelector('.fair-fix');
    if (!warn) { return; }
    warn.hidden = !fair.warning;
    node.querySelector('.fair-warn-text').textContent = fair.warning || '';
    fix.hidden = !fair.fix;
    if (fair.fix) {
      // Named field, named value, ONE click — and still an explicit
      // action. A sign the engine flipped by itself is a sign nobody
      // would ever notice was wrong.
      fix.textContent = 'set ' + fair.fix.field.replace(/_/g, ' ') +
        ' = ' + fmt(fair.fix.value, 2);
      fix.onclick = function () { applyCarryFix(row.key, fair.fix); };
    }
    // The other two things that decide where a position ends, beside
    // the two figures that price it: read-only here, because both are
    // set in the ladder's own settings.
    var overnight = node.querySelector('.x-overnight');
    if (overnight) {
      // Read-only here: the overnight rule is EXIT logic and lives in
      // this ladder's settings, beside the other two things that decide
      // where a position ends.
      overnight.textContent = OVERNIGHT_WORDS[row.overnight] || row.overnight;
      overnight.title = 'what happens to an OPEN POSITION at the session '
        + 'cutoff — change it in this ladder\u2019s settings';
    }
    var armed = node.querySelector('.auto-route-state');
    if (armed) {
      // What is ACTUALLY resting, not what the switch says. A target
      // believed to be armed when it is not is the worse failure — and
      // it is the reason a re-arm after a restart is announced rather
      // than done quietly.
      var orders = row.auto_route_armed || [];
      armed.textContent = orders.length
        ? 'out ' + fmt(orders[0].level, digitsFor(row.increment))
        : (row.auto_route_on ? 'on' : 'off');
      armed.className = 'auto-route-state' + (orders.length ? ' armed' : '');
      armed.title = orders.length
        ? 'a working order is resting to close this position — a target, '
          + 'and no stop'
        : (row.auto_route_on
            ? 'on: the next fill arms a target at the take-profit'
            : (row.auto_route
                ? 'off: AutoRouting is switched off for the whole system'
                : 'off'));
    }
    renderExit(node, row);
  }

  function applyCarryFix(key, fix) {
    /* Correct one swap field, on the operator's click. */
    var body = {};
    body[fix.field] = fix.value;
    fetch('/api/pairs/' + encodeURIComponent(key), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); }).then(function (answer) {
      if (answer && answer.ok) {
        toast(fix.field.replace(/_/g, ' ') + ' set to ' + fmt(fix.value, 2),
          'ok');
      } else {
        toast('could not save: ' + ((answer && answer.error) || 'unknown'));
      }
    }).catch(function (e) { toast('could not save: ' + e); });
  }

  function renderExit(node, row) {
    /* Where a trade entered NOW gets out: break-even first, then
     * break-even plus the target.
     *
     * Two columns because a spread has two directions and their exits
     * move opposite ways: a long leaves on the bid ABOVE what it paid,
     * a short buys back BELOW what it sold. One number here would be
     * right half the time.
     *
     * A position that is already on gets its own line, anchored on the
     * price it was ENTERED at — a take-profit that moves with the
     * market is not a take-profit.
     */
    var exit = row.exit || {};
    if (!node.querySelector('.be-buy')) { return; }   // older template
    // The LADDER's own precision, not four decimals: the rail is 100px
    // wide and two seven-character numbers side by side collide into
    // one unreadable string. A price is shown here the way it is shown
    // in the price column.
    var digits = digitsFor(row.increment);
    // What the exit is BUILT from, in the order it is built: the round
    // turn the market charges, the commission the broker charges, the
    // profit the target asks for — then break-even and the target
    // price themselves.
    var width = node.querySelector('.x-width');
    width.textContent = fmt(exit.spread_width, digits);
    // One column, one basis: the spread figures are in spread points
    // and the money is in money, and `k` is what converts them. Both
    // units are on the row, because a bare dollar total cannot be
    // checked against a ladder quoted in spread.
    width.title = exit.spread_width_money === null ||
      exit.spread_width_money === undefined
      ? 'one round turn of both legs\u2019 bid-ask, measured live'
      : 'one round turn of both legs\u2019 bid-ask = ' +
        money(exit.spread_width_money) + ' at this size. Already inside ' +
        'the two prices, so it is not added again.';
    node.querySelector('.x-comm').textContent =
      exit.commission === null || exit.commission === undefined
        ? DASH : money(exit.commission);
    // Two terms that are ZERO by default and shown only when they are
    // in play: an allowance nobody set and a swap over no nights are
    // rows that say nothing, and the rail is 100px wide.
    showTerm(node, '.x-slip', exit.slippage_allowance,
      'a BUDGET for slippage, not a measurement — the realised figure ' +
      'is in the slippage report');
    showTerm(node, '.x-swap', exit.swap_money,
      exit.nights ? exit.nights + ' night(s) of swap, both legs, signed: ' +
        'a credit reduces what has to be recovered'
        : 'set BREAK_EVEN_NIGHTS to price a holding period');
    var target = node.querySelector('.x-target');
    target.textContent = exit.target_money === null ||
      exit.target_money === undefined ? DASH : money(exit.target_money);
    target.title = exit.target_pct
      ? exit.target_pct + '% of ' + money(exit.margin_per_spread) +
        ' margin per spread' +
        (exit.margin_source ? ' (from the ' + exit.margin_source + ')' : '')
      : 'set TP_TARGET_PCT_OF_MARGIN in Settings';
    // How far the target is, in spread, against what the pair actually
    // travels in a session. At small size a percentage of margin can be
    // far outside anything this spread moves — the number is honest,
    // but whether it is REACHABLE is the trader's call.
    var reach = node.querySelector('.x-reach');
    if (reach) {
      var tr = reach.parentNode;
      tr.hidden = exit.target_points === null ||
        exit.target_points === undefined;
      reach.textContent = fmt(exit.target_points, digits) +
        (exit.session_range ? ' / ' + fmt(exit.session_range, digits) : '');
      reach.className = 'x-reach' +
        (exit.target_reachable === false ? ' out-of-range' : '');
      reach.title = exit.target_reachable === false
        ? 'the target is FURTHER than this pair has travelled all ' +
          'session — honest arithmetic, but reaching it is another matter'
        : 'target distance in spread, against the session range';
    }
    // Break-even is quoted on the CLOSING side, which is the opposite
    // side to the one you entered on: a long's is a BID level, a
    // short's is an ASK level. Named, because unlabelled the number
    // reads as "the price now" rather than "the bid you need" — and
    // that is a fault that has already shown +$0.02 on a trade that
    // would have booked -$0.58.
    var beBuy = node.querySelector('.be-buy');
    var beSell = node.querySelector('.be-sell');
    beBuy.textContent = fmt(exit.break_even_buy, digits);
    beSell.textContent = fmt(exit.break_even_sell, digits);
    beBuy.title = 'bid \u2014 a long leaves on the bid, so this is the bid ' +
      'you need back';
    beSell.title = 'ask \u2014 a short buys back at the offer, so this is ' +
      'the ask you need';
    node.querySelector('.tp-buy').textContent = fmt(exit.tp_buy, digits);
    node.querySelector('.tp-sell').textContent = fmt(exit.tp_sell, digits);
    var short = '';
    if (exit.target_pct && exit.margin_per_spread) {
      short = exit.target_pct + '% of ' + money(exit.margin_per_spread);
    } else if (exit.break_even_buy !== null &&
               exit.break_even_buy !== undefined) {
      short = 'B/E only';
    }
    var open = (row.positions || [])[0];
    if (open && open.exit) {
      short = open.side[0] + ' out ' + fmt(open.exit.tp, 4);
    }
    var line = node.querySelector('.exit-note');
    line.textContent = short;
    line.title = (open && open.exit
      ? open.side + ' on: out at ' + fmt(open.exit.tp, 4) + ', flat at ' +
        fmt(open.exit.break_even, 4) + '. '
      : '') + (exit.note || '');
  }

  function showTerm(node, selector, value, why) {
    /* A break-even term that is zero by default: shown only when it is
     * actually in play, and hidden — not zeroed — when it is not. */
    var cell = node.querySelector(selector);
    if (!cell) { return; }
    var tr = cell.parentNode;
    var missing = value === null || value === undefined;
    tr.hidden = missing || value === 0;
    cell.textContent = missing ? DASH : money(value);
    cell.title = why;
    if (missing) { tr.hidden = false; }   // unmeasured is not zero: say so
  }

  function legFeed(row, market) {
    /* The two books, and the spread they make, as a small aligned
     * panel: bid, ask, and the WIDTH each leg is charging.
     *
     * Aligned in fixed columns because these numbers are read by
     * comparing them down the column — a leg whose width has doubled is
     * the leg about to cost money, and that is invisible in a ragged
     * row of figures. The spread's own width is the round turn, which
     * is what the whole ladder is about, so it sits under a rule.
     *
     * Staleness marks the AGE cell only. Painting the whole line red
     * made a quiet market look like a fault.
     */
    if (!market || market.leg_a_bid === undefined) {
      return '<span class="bad">no quote from either leg yet</span>';
    }
    function cell(value, klass, digits) {
      return '<td class="' + klass + '">' +
        fmt(value, digits === undefined ? 4 : digits) + '</td>';
    }
    function width(bid, ask) {
      if (bid === null || bid === undefined ||
          ask === null || ask === undefined) {
        return '<td class="c-width">' + DASH + '</td>';
      }
      return '<td class="c-width">' + fmt(ask - bid, 4) + '</td>';
    }
    function age(seconds) {
      if (seconds === null || seconds === undefined) {
        return '<td class="age c-age">' + DASH + '</td>';
      }
      return '<td class="age c-age' + (seconds > 5 ? ' bad' : '') + '">' +
        seconds.toFixed(1) + 's</td>';
    }
    function line(label, symbol, bid, ask, seconds, visible, stamp) {
      // The tooltip carries what a stale age actually means: this is
      // how long since the QUOTE CHANGED, not since we last asked.
      var note = (symbol || '?') +
        (seconds === null || seconds === undefined ? ''
          : ' — last change ' + seconds.toFixed(1) + 's ago') +
        (visible === undefined || visible === null ? ''
          : (visible ? '. In Market Watch: the terminal is subscribed, so '
                     + 'a frozen age here means it is receiving nothing — '
                     + 'look at this symbol in MT5'
                     : '. NOT in Market Watch — not subscribed. Press '
                     + 'Feed, and add it in the terminal')) +
        (stamp ? '. Broker stamp ' + stamp : '');
      return '<tr><th>' + label + '</th><td class="sym" title="' + note +
        '">' + (symbol || '?') + '</td>' +
        cell(bid, 'c-bid') + cell(ask, 'c-ask') +
        width(bid, ask) + age(seconds) + '</tr>';
    }
    var html = '<table class="legbook"><thead><tr>' +
      '<th></th><th class="sym">leg</th><th class="c-bid">Bid</th>' +
      '<th class="c-ask">Ask</th>' +
      // Each leg's own bid-ask, and on the last line the SPREAD's —
      // which is the two legs' summed, and is one round turn of both
      // books. That sum is the check that makes the panel readable at
      // a glance: 0.0460 + 0.0480 = 0.0940.
      '<th class="c-width" title="the bid-ask each leg is charging — and on the spread row, one round turn of both books">Spread</th>' +
      '<th class="c-age">Age</th></tr></thead><tbody>';
    html += line('A', row.symbol_a, market.leg_a_bid, market.leg_a_ask,
                 market.leg_a_quote_age_sec, market.leg_a_visible,
                 market.leg_a_tick_time);
    html += line('B', row.symbol_b, market.leg_b_bid, market.leg_b_ask,
                 market.leg_b_quote_age_sec, market.leg_b_visible,
                 market.leg_b_tick_time);
    html += '<tr class="spread"><th></th><td class="sym">spread</td>' +
      cell(market.short_spread, 'c-bid') +
      cell(market.long_spread, 'c-ask') +
      width(market.short_spread, market.long_spread) +
      '<td class="age c-age"></td></tr>';
    return html + '</tbody></table>';
  }

  function badgeClass(badge) {
    if (!badge) { return ''; }
    if (badge.indexOf('OK') === 0) { return 'ok'; }
    if (badge === 'warming up') { return 'warn'; }
    if (badge.indexOf('stale') === 0) { return 'warn'; }
    return 'bad';
  }

  function bought(row) {
    return (row.positions || []).filter(function (p) {
      return p.side === 'BUY';
    }).length;
  }

  function sold(row) {
    return (row.positions || []).filter(function (p) {
      return p.side === 'SELL';
    }).length;
  }

  function flashChanged(tr, side) {
    var cell = tr.querySelector('td.' + side);
    if (!cell) { return; }
    var now = cell.textContent;
    var was = tr['_' + side];
    tr['_' + side] = now;
    // Nothing to announce on the first draw, or when only some other
    // cell in the row changed.
    if (was === undefined || was === now) { return; }
    cell.classList.remove('ticked');
    void cell.offsetWidth;                    // restart the animation
    cell.classList.add('ticked');
  }

  function commitRows(body, rows) {
    /* Update the ladder IN PLACE, keyed by PRICE.
     *
     * The whole tbody used to be rebuilt three times a second with
     * `innerHTML =`. That threw away the very row the pointer was over
     * — losing :hover and the pressed state — and a click landing while
     * the rows were being replaced hit a detached element, or whatever
     * had just slid into that position. The element for a price is now
     * the SAME element from tick to tick, and only the cells whose
     * markup actually changed are written.
     *
     * The identity is the price, never the index: an index moves when
     * the window does, and an order must never be sent at whatever
     * happened to slide under the cursor.
     */
    var existing = {};
    Array.prototype.forEach.call(body.children, function (tr) {
      existing[tr.dataset.level] = tr;
    });
    var previous = null;
    rows.forEach(function (row) {
      var keyed = String(row.level);
      var tr = existing[keyed];
      if (tr) {
        delete existing[keyed];
        if (tr.className !== row.cls) { tr.className = row.cls; }
        // Held on the element, not in an attribute: this is a
        // comparison key, not something the page should carry.
        if (tr._cells !== row.cells) {
          tr.innerHTML = row.cells;
          tr._cells = row.cells;
          // Flash only the size that actually moved. On a ladder that
          // no longer crawls, this is the only thing left saying the
          // book is live — and it is a background colour, so nothing
          // reflows and the click target never shifts.
          flashChanged(tr, 'bid');
          flashChanged(tr, 'ask');
        }
      } else {
        tr = document.createElement('tr');
        tr.className = row.cls;
        tr.dataset.level = keyed;
        tr.innerHTML = row.cells;
        tr._cells = row.cells;
      }
      // Into place without disturbing rows already sitting correctly.
      var want = previous ? previous.nextSibling : body.firstChild;
      if (tr !== want) { body.insertBefore(tr, want); }
      previous = tr;
    });
    Object.keys(existing).forEach(function (level) {
      body.removeChild(existing[level]);
    });
  }

  function renderRows(node, key, row) {
    // '.grid tbody', not 'tbody': the rail has small tables of its own
    // now, and the first tbody in the window is not necessarily the
    // ladder's. Writing the rows into the wrong one destroys the rail.
    var body = node.querySelector('.grid tbody');
    if (state.snapshot.row_height_px) {
      // A bigger target is a faster and safer click. 17px is the
      // reference screen's; a large monitor wants more.
      node.style.setProperty('--row-h',
                             state.snapshot.row_height_px + 'px');
    }
    var rows = row.rows || [];
    var market = row.market || {};
    var lastPrint = row.last_print || {};
    // Which of our working orders are NOT actually resting at the
    // broker. A synthetic order joins the book the instant it is
    // clicked, but the real pending on the quoting leg is only placed
    // once the guards are clear (quoter._rest_or_repeg) — so while the
    // feed reads stale or desynced, the order exists here and NOWHERE
    // else. It looked exactly like one resting at the broker, and the
    // only hint was W:8 (broker 0) in small text in the footer.
    var heldOff = {};
    var quoteFor = {};
    (row.quotes || []).forEach(function (quote) {
      (quote.orders || []).forEach(function (id) { quoteFor[id] = quote; });
      if (quote.ticket) { return; }             // really is at the broker
      // A CLOSING order rests NOTHING at the broker, by design: MT5
      // ignores `position` on a pending, so a closing limit is an
      // ordinary one and opens a second position on a hedging account.
      // The level is held here and fired by ticket. Marking it "not at
      // the broker" flagged the mode working as a fault — every
      // resting close came up amber, permanently.
      if (quote.intent === 'CLOSE') { return; }
      (quote.orders || []).forEach(function (id) {
        heldOff[id] = quote.reason ||
          'not resting at the broker yet';
      });
    });

    var ordersByLevel = {};
    (row.orders || []).forEach(function (order) {
      var bucket = ordersByLevel[order.level.toFixed(6)] ||
        (ordersByLevel[order.level.toFixed(6)] = []);
      bucket.push(order);
    });

    var out = [];
    rows.forEach(function (line) {
      var level = line.level;
      var inBid = market.short_spread !== undefined &&
        level <= market.short_spread + 1e-12;
      var inAsk = market.long_spread !== undefined &&
        level >= market.long_spread - 1e-12;
      var orders = ordersByLevel[level.toFixed(6)] || [];
      if (state.filtered[key] && !orders.length && !line.is_best_bid &&
          !line.is_best_ask && !inBid && !inAsk) {
        return;
      }
      var work = orders.length ? orders[0] : null;
      var workQty = orders.reduce(function (sum, order) {
        return sum + (order.quantity - order.filled_quantity);
      }, 0);
      // Clicks that are on their way. Shown as their own thing, never
      // added into the working total: a ghost is not an order yet.
      var ghosts = pendingAt(key, level, row.increment);
      var ghostQty = ghosts.reduce(function (sum, ghost) {
        return sum + ghost.quantity;
      }, 0);
      var classes = [];
      if (inBid) { classes.push('in-bid'); }
      if (inAsk) { classes.push('in-ask'); }
      if (line.is_best_bid) { classes.push('market-line'); }
      // The heavy rule marks where the market is; on a LOCKED ladder
      // it marks the anchor instead, because the anchor is the one
      // price on a locked ladder that is guaranteed not to move. With
      // the rows frozen and the bands solid, a rule chasing the mid
      // was the only thing left hopping about.
      if (state.locked[key] ? line.is_anchor : line.is_mid) {
        classes.push('mid-line');
      }
      if (line.is_best_ask) { classes.push('best-ask'); }
      var isLast = lastPrint.level !== undefined &&
        Math.abs(lastPrint.level - level) < (row.increment || 1) / 2;

      var cells = '';
      var ghostSide = ghosts.length ? ghosts[0].side.toLowerCase() : '';
      // ANY order here not at the broker marks the cell: a level that
      // is half resting is still a level the trader must not read as
      // fully working.
      var heldReason = null;
      orders.forEach(function (order) {
        heldReason = heldReason || heldOff[order.order_id] || null;
      });
      var workQuote = work ? quoteFor[work.order_id] : null;
      var afterQty = (workQuote && workQuote.open_after) || 0;
      cells += '<td class="work' +
        (work ? ' ' + work.side.toLowerCase() : '') +
        (heldReason ? ' held' : '') +
        (ghosts.length ? ' pending ' + ghostSide : '') + '"' +
        (work ? ' data-order-id="' + work.order_id + '"' +
          ' data-side="' + work.side + '" title="' +
          escapeHtml(heldReason
            ? 'NOT at the broker: ' + heldReason +
              '. Click to add another here; right-click to pull one.'
            : orders.length + ' ' + work.side + ' order(s) here' +
              restingOn(workQuote) +
              '. Click to add another; right-click to pull one.') +
          '"' : '') + '>' +
        (workQty ? workQty : '') +
        // WHAT THIS CLICK STILL OWES. A reducing click bigger than the
        // position it covers holds the rest back until the close has
        // gone through — one instruction, in order. The held part was
        // in the engine and NOWHERE on the screen: a trader who
        // clicked 100 over a 93 saw '93' and no sign that 7 more were
        // coming, waiting, or quietly dropped.
        (afterQty ? '<span class="after" title="' +
          escapeHtml(afterQty + ' more to OPEN here, held back until this '
                     + 'close goes through. One click is one instruction, '
                     + 'in order: resting it now would put the new '
                     + 'position on before the old one came off.') +
          '">+' + afterQty + '</span>' : '') +
        (ghostQty ? '<span class="ghost" title="sent — waiting for the ' +
          'engine">' + ghostQty + '</span>' : '') + '</td>';
      // MT5 publishes no depth for a spread, so only the touch is real.
      // The size is the two ORDER BOOKS', in spreads: whatever the
      // worse of the two legs can fill at the prices this level
      // implies. Nothing where the brokers publish no depth — an
      // invented size is one a trader would click on.
      cells += '<td class="bid' + (line.is_best_bid ? ' has-qty' : '') +
        '" title="' + clickHint('bid', level) + '">' +
        depthText(line.bid_size, line.is_best_bid, '▲') + '</td>';
      cells += '<td class="price' + (isLast ? ' last-trade' : '') + '">' +
        fmt(level, digitsFor(row.increment)) + '</td>';
      cells += '<td class="ask' + (line.is_best_ask ? ' has-qty' : '') +
        '" title="' + clickHint('ask', level) + '">' +
        depthText(line.ask_size, line.is_best_ask, '▼') + '</td>';
      cells += '<td class="ltq' + (isLast ? ' print' : '') + '">' +
        (isLast ? fmt(lastPrint.quantity, 2) : '') + '</td>';
      out.push({level: level, cls: classes.join(' '), cells: cells});
    });
    commitRows(body, out);

    if (shouldRecentre(key, node, market)) { centreOnMid(node, key, market); }
  }

  //: How long the ladder leaves a hand-scrolled window alone before it
  //: re-centres. A ladder that snaps back while the trader is reading
  //: a level twenty rows away is a ladder they cannot use.
  var SCROLL_GRACE_MS = 4000;

  //: How long after the trader's last touch the ladder stays put. A
  //: window that re-centres between a mousedown and the mouseup is a
  //: window that moved the order.
  var BUSY_GRACE_MS = 1200;

  function setLocked(key, locked) {
    /* Lock, in one place, because it has to reach THREE things.
     *
     * The browser stops recentring its scroll; the engine stops
     * re-anchoring the price window; and the engine stops widening
     * that window to follow the touches. Only the first of those is
     * local, and a Lock that did only the first is what left the
     * ladder crawling with the box ticked.
     */
    locked = !!locked;
    state.locked[key] = locked;
    var node = document.querySelector('.ladder[data-pair="' +
                                      cssEscape(key) + '"]');
    var box = node && node.querySelector('.lock-scroll');
    if (box) { box.checked = locked; }
    send('lock_ladder', {pair: key, locked: locked});
  }

  function ladderIsBusy(key) {
    /* The trader is working this ladder RIGHT NOW: the pointer is over
     * it, a button is down on it, or they touched it a moment ago. Any
     * of those and it does not move under them — recentring while a
     * click is being aimed is how the wrong price gets sent. */
    if (state.hovering[key]) { return true; }
    return Date.now() - (state.busyAt[key] || 0) < BUSY_GRACE_MS;
  }

  function shouldRecentre(key, node, market) {
    if (state.locked[key]) { return false; }        // Lock means LOCKED
    if (ladderIsBusy(key)) { return false; }
    if (market.spread === undefined || market.spread === null) {
      return false;
    }
    var grid = node.querySelector('.grid');
    var now = Date.now();
    if (now - (state.scrolledAt[key] || 0) < SCROLL_GRACE_MS) {
      // The trader is working somewhere else on the ladder. Free
      // scrolling is the point: orders get placed away from the touch.
      return false;
    }
    if (!state.centredAt[key]) { return true; }     // first draw
    var row = midRow(node, market);
    if (row) {
      // Out of sight is always a reason: a market that has left the
      // window is a ladder showing prices nobody is trading.
      var top = row.offsetTop - grid.scrollTop;
      if (top < 0 || top > grid.clientHeight - row.offsetHeight) {
        return true;
      }
    }
    var every = state.snapshot.recentre_sec;
    if (every === undefined || every === null) { every = 5; }
    if (!every) { return false; }                   // 0 = only when lost
    return now - state.centredAt[key] >= every * 1000;
  }

  function midRow(node, market) {
    var marked = node.querySelector('tbody tr.mid-line');
    if (marked) { return marked; }
    /* The row the CURRENT MID sits on — the middle of the book, which
     * is where the ladder centres. Centring on a touch puts the other
     * side against an edge, and a side you cannot see is a side you
     * cannot trade. */
    var best = null;
    var closest = Infinity;
    Array.prototype.forEach.call(node.querySelectorAll('tbody tr'),
      function (row) {
        var away = Math.abs(parseFloat(row.dataset.level) - market.spread);
        if (away < closest) { closest = away; best = row; }
      });
    return best;
  }

  function centreOnMid(node, key, market) {
    var row = midRow(node, market);
    var grid = node.querySelector('.grid');
    if (!row || !grid.clientHeight) {
      // The window has not been laid out yet, so there is nothing to
      // centre IN. Ask again on the next frame rather than dropping it:
      // dropped, the ladder sits wherever the rows happened to land
      // until the re-centring timer next fires — seconds of a market
      // the trader is looking straight at, off centre.
      if (row && !state.centringAgain[key]) {
        state.centringAgain[key] = true;
        window.requestAnimationFrame(function () {
          state.centringAgain[key] = false;
          var live = (state.snapshot.pairs[key] || {}).market;
          if (live) { centreOnMid(node, key, live); }
        });
      }
      return;
    }
    state.centring[key] = true;                     // our scroll, not theirs
    grid.scrollTop = row.offsetTop -
      (grid.clientHeight - row.offsetHeight) / 2;
    state.centredAt[key] = Date.now();
    window.setTimeout(function () { state.centring[key] = false; }, 0);
  }


  function digitsFor(increment) {
    if (!increment) { return 4; }
    var digits = Math.max(0, Math.ceil(-Math.log10(increment)));
    return Math.min(6, digits);
  }

  function setValue(input, value) {
    if (document.activeElement === input) { return; }   // do not fight typing
    var text = value === null || value === undefined ? '' : String(value);
    if (input.value !== text) { input.value = text; }
  }

  // -- the Market Grid --------------------------------------------------

  function renderGrid() {
    var node = document.querySelector('.market-grid');
    if (!node) {
      node = document.createElement('section');
      node.className = 'window market-grid';
      node.innerHTML =
        '<div class="titlebar"><span class="swatch"></span>' +
        '<span class="title">Market Grid</span>' +
        '<span class="winbtns"><button class="winbtn close">&times;</button></span></div>' +
        '<div class="grid"><table><thead></thead><tbody></tbody></table></div>';
      node.querySelector('.close').addEventListener('click', function () {
        closePanel(panelId('grid'));
      });
      node.querySelector('tbody').addEventListener('click', onGridClick);
      node.querySelector('tbody').addEventListener('change', onGridChange);
      el('desktop').appendChild(node);
    }
    node.querySelector('thead').innerHTML =
      '<tr><th>Contract</th><th>Bid</th><th>Ask</th><th>Last</th>' +
      '<th>Chg</th><th>Incr</th><th>Qty</th><th>k $</th><th>Net</th>' +
      '<th>Work</th><th>Avg</th><th>Open P&amp;L</th><th>Mode</th>' +
      '<th>TIF</th><th>O/N</th><th>Feed</th><th>&beta;</th><th></th></tr>';

    var html = '';
    Object.keys(state.snapshot.pairs || {}).forEach(function (key) {
      var row = state.snapshot.pairs[key];
      var market = row.market || {};
      var broken = (row.errors || []).length > 0;
      html += '<tr class="' + (broken ? 'broken' : '') + '" data-pair="' +
        key + '">';
      html += '<td class="contract" title="Open this ladder">' +
        (row.name || key) + '</td>';
      // Bid = the SHORT spread (where you can sell it); Ask = the LONG.
      html += '<td class="bid" title="Click: SELL the spread here">' +
        fmt(row.short_spread, digitsFor(row.increment)) + '</td>';
      html += '<td class="ask" title="Click: BUY the spread here">' +
        fmt(row.long_spread, digitsFor(row.increment)) + '</td>';
      html += '<td>' + fmt((row.last_print || {}).level,
                           digitsFor(row.increment)) + '</td>';
      html += '<td>' + fmt(market.net_change, 4) + '</td>';
      html += '<td><input class="incr" type="number" step="0.0001" value="' +
        (row.increment === null ? '' : row.increment) + '" title="' +
        'Derived: max(tick B, beta x tick A) = ' +
        fmt(row.increment_derived, 6) + '"></td>';
      html += '<td><input class="qty" type="number" step="0.01" value="' +
        row.default_quantity + '"></td>';
      html += '<td>' + money(row.spread_units) + '</td>';
      html += '<td>' + (row.net_position || 0) + '</td>';
      html += '<td>' + row.working_buys + '/' + row.working_sells + '</td>';
      html += '<td>' + fmt(row.avg_entry, 4) + '</td>';
      html += '<td>' + (row.open_pnl === null || row.open_pnl === undefined
                        ? DASH : money(row.open_pnl)) + '</td>';
      html += '<td>' + select('mode', ['LIMIT', 'MARKET'], row.order_type) + '</td>';
      html += '<td>' + select('tif', ['DAY', 'GTC'], row.time_in_force) + '</td>';
      html += '<td>' + select('on', ['ALLOW', 'EXIT_IF_PROFIT', 'EXIT_ALWAYS'],
                              row.overnight) + '</td>';
      html += '<td>' + (market.feed_badge || DASH) + '</td>';
      html += '<td title="Stamped for ' + (row.hedge_ratio_for || '?') + '">' +
        fmt(row.hedge_ratio, 4) + '</td>';
      html += '<td class="reason">' + (row.errors || []).join(' ') + '</td>';
      html += '</tr>';
    });
    node.querySelector('tbody').innerHTML = html;
    node.classList.toggle('inactive', state.active !== panelId('grid'));
    return node;
  }

  function select(name, options, value) {
    var html = '<select class="' + name + '">';
    options.forEach(function (option) {
      html += '<option value="' + option + '"' +
        (option === value ? ' selected' : '') + '>' + option + '</option>';
    });
    return html + '</select>';
  }

  function onGridClick(e) {
    var cell = e.target.closest('td');
    if (!cell) { return; }
    var key = cell.closest('tr').dataset.pair;
    var row = state.snapshot.pairs[key] || {};
    if (cell.classList.contains('contract')) {
      openPanel(panelId('ladder', key));
    } else if (cell.classList.contains('bid')) {
      // Identical semantics to a ladder click, through the identical
      // code path and the identical mapping — a test asserts it. The
      // PRICE is still the one that cell displays; only the side moves
      // with the convention.
      clickLevel(key, sideForColumn('bid'), row.short_spread);
    } else if (cell.classList.contains('ask')) {
      clickLevel(key, sideForColumn('ask'), row.long_spread);
    }
  }

  function onGridChange(e) {
    var input = e.target;
    var key = input.closest('tr').dataset.pair;
    if (input.classList.contains('incr')) {
      setPair(key, {increment: parseFloat(input.value)});
    } else if (input.classList.contains('qty')) {
      setPair(key, {default_quantity: parseFloat(input.value)});
    } else if (input.classList.contains('mode')) {
      setPair(key, {order_type: input.value});
    } else if (input.classList.contains('tif')) {
      setPair(key, {time_in_force: input.value});
    } else if (input.classList.contains('on')) {
      setPair(key, {overnight: input.value});
    }
  }

  // -- the positions monitor ---------------------------------------------

  function renderMonitor() {
    var node = document.querySelector('.monitor');
    if (!node) {
      node = document.createElement('section');
      node.className = 'window monitor';
      node.innerHTML =
        '<div class="titlebar"><span class="swatch"></span>' +
        '<span class="title">Trading Monitor</span>' +
        '<span class="winbtns"><button class="winbtn close">&times;</button></span></div>' +
        '<div class="tabs">' +
        '<button data-tab="positions">Positions</button>' +
        '<button data-tab="orders">Working Orders</button>' +
        '<button data-tab="fills">Fills</button>' +
        '<button data-tab="slippage">Slippage</button>' +
        '<button data-tab="accounts">Accounts</button>' +
        '<button data-tab="reconcile">Reconciler</button></div>' +
        // `monitor-note`, not `note`: the panes have notes of their own,
        // and a bare `.note` selector reaches into them and overwrites
        // the first one it finds.
        '<div class="pane"></div><div class="note monitor-note"></div>';
      node.querySelector('.close').addEventListener('click', function () {
        closePanel(panelId('monitor'));
      });
      node.querySelector('.tabs').addEventListener('click', function (e) {
        var button = e.target.closest('button');
        if (!button) { return; }
        state.monitorTab = button.dataset.tab;
        render();
      });
      node.querySelector('.pane').addEventListener('click', onMonitorClick);
      node.querySelector('.pane').addEventListener('change', function (e) {
        if (e.target.classList.contains('ours-only')) {
          state.fillsFilter.ours = e.target.checked;
          loadFills(true);
        }
        if (e.target.classList.contains('all-sessions')) {
          state.slippageAll = e.target.checked;
          loadSlippage(true);
        }
      });
      el('desktop').appendChild(node);
    }
    Array.prototype.forEach.call(node.querySelectorAll('.tabs button'),
      function (button) {
        button.classList.toggle('on', button.dataset.tab === state.monitorTab);
      });
    var pane = node.querySelector('.pane');
    if (state.monitorTab === 'positions') { pane.innerHTML = positionsTable(); }
    else if (state.monitorTab === 'orders') { pane.innerHTML = ordersTable(); }
    else if (state.monitorTab === 'fills') {
      loadFills();
      pane.innerHTML = fillsTable();
    }
    else if (state.monitorTab === 'slippage') {
      loadSlippage();
      pane.innerHTML = slippageTable();
    }
    else if (state.monitorTab === 'accounts') {
      pane.innerHTML = accountsTable();
    }
    else { pane.innerHTML = reconcileTable(); }
    node.querySelector('.monitor-note').textContent =
      state.monitorTab === 'slippage'
      ? 'Every figure here was MEASURED against the price that was ' +
        'clicked. Positive is a cost, at both ends. A position whose ' +
        'fill could not be priced is counted as unmeasured, never as ' +
        'zero — averaging it in would flatter every column.'
      : 'Marked at the touches these would actually CLOSE at, less ' +
      'commission only — so a position shows a loss the instant it ' +
      'opens, equal to one round turn of both legs’ bid-ask. That is ' +
      'what closing it immediately would cost.';
    node.classList.toggle('inactive', state.active !== panelId('monitor'));
    return node;
  }

  function eachPosition(callback) {
    Object.keys(state.snapshot.pairs || {}).forEach(function (key) {
      (state.snapshot.pairs[key].positions || []).forEach(function (position) {
        callback(key, state.snapshot.pairs[key], position);
      });
    });
  }

  function positionsTable() {
    var html = '<table><thead><tr><th>Pair</th><th>Side</th><th>Net</th>' +
      '<th>Avg entry</th><th>Mark</th><th>Open P&amp;L</th><th>Mode</th>' +
      '<th>Slip</th><th>Click→on</th><th>Legs</th><th></th></tr></thead><tbody>';
    var total = 0;
    var any = false;
    eachPosition(function (key, row, position) {
      any = true;
      if (position.net_pnl !== null && position.net_pnl !== undefined) {
        total += position.net_pnl;
      }
      html += '<tr data-position="' + position.position_id + '">';
      html += '<td>' + (row.name || key) + '</td>';
      html += '<td>' + position.side + '</td>';
      html += '<td>' + position.quantity + '</td>';
      html += '<td>' + fmt(position.entry_spread, 4) + '</td>';
      html += '<td>' + fmt(position.closing_spread, 4) + '</td>';
      html += '<td class="' + (position.net_pnl > 0 ? 'up' : 'down') + '">' +
        money(position.net_pnl) + '</td>';
      html += '<td>' + position.order_type + '</td>';
      html += '<td>' + fmt(position.entry_slippage, 4) + '</td>';
      html += '<td>' + (position.click_to_on_ms === null ||
                        position.click_to_on_ms === undefined
                        ? DASH : Math.round(position.click_to_on_ms) + 'ms') +
        '</td>';
      html += '<td>' + legText(position.leg_a) + ' / ' +
        legText(position.leg_b) + '</td>';
      html += '<td><button class="btn close-position">Flatten</button></td>';
      html += '</tr>';
      html += '<tr class="detail"><td colspan="11">' +
        legDetail(position.leg_a, 'A') + ' &nbsp; ' +
        legDetail(position.leg_b, 'B') + '</td></tr>';
    });
    if (!any) { html += '<tr><td colspan="11">nothing open</td></tr>'; }
    html += '</tbody></table>';
    html += accountReconciliation(total);
    return html;
  }

  function legText(leg) {
    if (!leg) { return DASH; }
    return leg.side + ' ' + leg.volume + ' @ ' + fmt(leg.price, 2);
  }

  function legDetail(leg, label) {
    if (!leg) { return 'leg ' + label + ': ' + DASH; }
    return 'leg ' + label + ' ' + leg.symbol + ' on ' + leg.account +
      ' · tickets ' + (leg.position_tickets.join(', ') || DASH) +
      ' · ' + leg.volume + ' lots × ' +
      (leg.contract_size || DASH) + '/lot';
  }

  function accountReconciliation(ourTotal) {
    // Our total against MT5's OWN per-account profit. A difference is a
    // fault to be SHOWN, not smoothed.
    var accounts = state.snapshot.accounts || {};
    var theirs = 0;
    var known = false;
    Object.keys(accounts).forEach(function (name) {
      var info = accounts[name];
      if (info && typeof info.profit === 'number') {
        theirs += info.profit;
        known = true;
      }
    });
    if (!known) {
      return '<div class="note">MT5’s own profit could not be read, ' +
        'so there is nothing to reconcile against — unmeasured, not zero.</div>';
    }
    var difference = ourTotal - theirs;
    var bad = Math.abs(difference) > 0.01;
    return '<table><tbody><tr' + (bad ? ' class="mismatch"' : '') +
      '><td>our total</td><td>' + money(ourTotal) +
      '</td><td>MT5’s own</td><td>' + money(theirs) +
      '</td><td>difference</td><td>' + money(difference) +
      '</td></tr></tbody></table>';
  }

  function ordersTable() {
    var html = '<table><thead><tr><th>Pair</th><th>Side</th><th>Level</th>' +
      '<th>Qty</th><th>TIF</th><th>State</th><th>Pending</th>' +
      '<th>Peg price</th><th>Resting on</th><th>Re-pegs</th><th>Why</th>' +
      '<th></th>' +
      '</tr></thead><tbody>';
    var columns = 12;
    var any = false;
    Object.keys(state.snapshot.pairs || {}).forEach(function (key) {
      var row = state.snapshot.pairs[key];
      var quotes = {};
      (row.quotes || []).forEach(function (quote) {
        quote.orders.forEach(function (id) { quotes[id] = quote; });
      });
      (row.orders || []).forEach(function (order) {
        any = true;
        var quote = quotes[order.order_id] || {};
        html += '<tr data-order="' + order.order_id + '">';
        html += '<td>' + (row.name || key) + '</td>';
        html += '<td>' + order.side + '</td>';
        html += '<td>' + fmt(order.level, 4) + '</td>';
        // The size WORKING, and beside it what this click still owes:
        // a reducing click bigger than the position it covers holds
        // the rest back until the close has gone through, and that
        // part was on no panel at all.
        html += '<td>' + qty(order.quantity - order.filled_quantity) +
          (quote.open_after
            ? ' <span class="after" title="held back until this close '
              + 'goes through">+' + qty(quote.open_after) + '</span>'
            : '') + '</td>';
        html += '<td>' + order.time_in_force + '</td>';
        html += '<td>' + order.state + '</td>';
        html += '<td>' + (quote.ticket || DASH) + '</td>';
        html += '<td>' + fmt(quote.price, 2) + '</td>';
        // WHICH broker will show this order, and which leg crosses
        // when it fills. 'leg a' alone read as leg A having nothing.
        // WHAT THIS ROW IS, before which leg it sits on.
        //
        // One click can arm SEVERAL orders: a click bigger than the
        // oldest position walks down the stack, closing each ticket as
        // far as it reaches. Those rows are identical apart from their
        // size, so two of them at one level read as the same order
        // placed twice at the wrong quantity. They are not — they are
        // one click, split across the tickets it covers — and the row
        // now says which ticket it closes.
        html += '<td>' + (order.position_id
          ? '<span class="closing">closes ' +
            escapeHtml(order.position_id) + '</span><br>'
          : '') + (quote.leg
          ? 'leg ' + quote.leg +
            (quote.symbol ? ' · ' + escapeHtml(quote.symbol) : '') +
            (quote.crosses_leg
              ? '<div class="hint">' + quote.crosses_leg +
                ' crosses on fill</div>'
              : '')
          : DASH) + '</td>';
        html += '<td>' + (quote.repegs === undefined ? DASH : quote.repegs) +
          '</td>';
        html += '<td>' + (quote.reason || order.reason || '') + '</td>';
        html += '<td><button class="btn cancel-order">Cancel</button></td>';
        html += '</tr>';
        // AN ORDER THAT IS NOT AT THE BROKER, ACROSS THE WHOLE ROW.
        //
        // The reason was already here, in the eleventh column — which
        // is off the right edge on any normal window, and the panel
        // scrolls sideways to reach it. So an order that had never
        // been placed read exactly like one that was working, and the
        // one line explaining it was somewhere nobody scrolls to.
        //
        // Its own row, spanning the table, so no scrolling can hide it.
        var whyNot = order.state === 'WORKING' && !quote.ticket &&
          quote.intent !== 'CLOSE'
          ? (quote.reason || order.reason ||
             'not at the broker yet — no reason given')
          : null;
        if (whyNot) {
          html += '<tr class="not-placed"><td colspan="' + columns + '">' +
            'NOT AT THE BROKER: ' + escapeHtml(whyNot) + '</td></tr>';
        }
      });
    });
    // What stopped working recently, and why — in the broker's own
    // words. Without this a rejected order simply vanished.
    Object.keys(state.snapshot.pairs || {}).forEach(function (key) {
      var row = state.snapshot.pairs[key];
      (row.dead_orders || []).forEach(function (order) {
        any = true;
        html += '<tr class="dead">';
        html += '<td>' + (row.name || key) + '</td>';
        html += '<td>' + order.side + '</td>';
        html += '<td>' + fmt(order.level, 4) + '</td>';
        html += '<td>' + qty(order.quantity) + '</td>';
        html += '<td>' + order.time_in_force + '</td>';
        html += '<td class="mismatch">' + order.state + '</td>';
        html += '<td>' + (order.pending_ticket || DASH) + '</td>';
        html += '<td>' + DASH + '</td><td>' + DASH + '</td><td>' + DASH +
          '</td>';
        html += '<td class="reason">' + (order.reason || '') + '</td>';
        html += '<td></td></tr>';
      });
    });
    if (!any) { html += '<tr><td colspan="12">nothing working</td></tr>'; }
    html += '</tbody></table>';
    html += '<div class="note">GTC here means: until cancelled, or until ' +
      'this system stops. A synthetic order lives in the coordinator, and ' +
      'nothing watches the spread while it is down — so neither DAY ' +
      'nor GTC survives a restart. The real pending behind a LIMIT order ' +
      'DOES survive at the broker, which is why it is swept at shutdown ' +
      'and again at startup.</div>';
    return html;
  }

  function fillsTable() {
    var snapshot = state.snapshot;
    var html = '<table><thead><tr><th>What</th><th>Value</th></tr></thead>' +
      '<tbody>';
    html += timingRow('fill → hedge on (LIMIT)', snapshot.hedge_times_ms,
                      'against the 2.0s escalation deadline');
    html += timingRow('click → both legs on (MARKET)',
                      snapshot.click_to_on_ms, 'measured, not assumed');
    html += '</tbody></table>';

    var journal = state.fills;
    html += '<div class="journal-controls">' +
      '<label class="check"><input type="checkbox" class="ours-only"' +
      (state.fillsFilter.ours ? ' checked' : '') + '> ours only</label>' +
      '<a class="btn" href="/api/fills.csv" download>Export CSV</a>' +
      '<span class="hint">Read back from MT5\'s own deal history — so it ' +
      'carries the trader\'s terminal clicks too, marked as not ours.' +
      '</span></div>';

    if (journal === null) {
      return html + '<p class="note">loading the journal…</p>';
    }
    if (journal.error) {
      return html + '<p class="note mismatch">' + journal.error + '</p>';
    }

    var totals = journal.totals || {};
    html += '<table><tbody><tr><td>' + (totals.fills || 0) + ' fills</td>' +
      '<td>' + fmt(totals.volume, 2) + ' lots</td>' +
      '<td>commission ' + money(totals.commission) + '</td>' +
      '<td>swap ' + money(totals.swap) + '</td>' +
      '<td>the broker\'s own P&amp;L ' +
      '<b class="' + upDown(totals.profit) + '">' + money(totals.profit) +
      '</b></td></tr></tbody></table>';

    html += '<table><thead><tr><th>Broker time</th><th>Account</th>' +
      '<th>Symbol</th><th>Pair</th><th>Leg</th><th>Side</th><th>In/Out</th>' +
      '<th>Volume</th><th>Price</th><th>Comm</th><th>Swap</th>' +
      '<th>P&amp;L</th><th>Ticket</th><th>Deal</th><th>Ours</th>' +
      '<th>Comment</th></tr></thead><tbody>';
    (journal.fills || []).forEach(function (fill) {
      html += '<tr class="' + (fill.is_ours ? '' : 'theirs') + '">';
      html += '<td>' + brokerTime(fill) + '</td>';
      html += '<td>' + (fill.account || DASH) + '</td>';
      html += '<td>' + (fill.symbol || DASH) + '</td>';
      html += '<td>' + (fill.pair_key || DASH) + '</td>';
      html += '<td>' + (fill.leg || DASH) + '</td>';
      html += '<td>' + (fill.side || DASH) + '</td>';
      html += '<td>' + (fill.entry || DASH) + '</td>';
      html += '<td>' + fmt(fill.volume, 2) + '</td>';
      html += '<td>' + fmt(fill.price, 4) + '</td>';
      html += '<td>' + money(fill.commission) + '</td>';
      html += '<td>' + money(fill.swap) + '</td>';
      // The broker's own P&L on this deal: green made, red lost. An
      // opening deal books nothing, so 0.00 stays black — colouring it
      // green would make every entry look like a winner.
      html += moneyCell(fill.profit);
      html += '<td>' + (fill.position_ticket || DASH) + '</td>';
      html += '<td>' + (fill.deal_id || DASH) + '</td>';
      html += '<td>' + (fill.is_ours ? 'yes' : 'no') + '</td>';
      html += '<td>' + (fill.comment || '') + '</td>';
      html += '</tr>';
    });
    if (!(journal.fills || []).length) {
      html += '<tr><td colspan="16">no fills recorded yet</td></tr>';
    }
    return html + '</tbody></table>';
  }

  function upDown(value) {
    if (value === null || value === undefined || !isFinite(value) ||
        value === 0) {
      return '';
    }
    return value > 0 ? 'up' : 'down';
  }

  function moneyCell(value) {
    return '<td class="' + upDown(value) + '">' + money(value) + '</td>';
  }

  function brokerTime(fill) {
    // The BROKER's wall clock, which is what MT5's own History shows.
    // Rendering it in the browser's zone would put every row hours away
    // from the same trade in the terminal.
    if (!fill.broker_time_ms) { return DASH; }
    var stamp = new Date(fill.broker_time_ms);
    return stamp.toISOString().slice(11, 19) +
      (fill.server_offset_s === null || fill.server_offset_s === undefined
        ? '' : ' (broker)');
  }

  function loadFills(force) {
    var now = Date.now();
    if (!force && state.fills !== null && now - state.fillsAt < 5000) {
      return;
    }
    state.fillsAt = now;
    var query = state.fillsFilter.ours ? '?ours=1' : '';
    fetch('/api/fills' + query).then(function (r) { return r.json(); })
      .then(function (body) {
        state.fills = body.ok ? body : {error: body.error};
        render();
      })
      .catch(function (error) {
        state.fills = {error: 'the journal could not be read: ' +
                              error.message};
      });
  }

  // -- the slippage report -----------------------------------------------

  function slipPoints(value) {
    // Cost is red, improvement is green, unmeasured is a dash. The
    // colour is the whole point of the column: a table of black
    // numbers is one nobody reads twice.
    if (value === null || value === undefined) { return '<td>' + DASH + '</td>'; }
    return '<td class="' + (value > 0 ? 'down' : value < 0 ? 'up' : '') +
      '">' + fmt(value, 4) + '</td>';
  }

  function slipMoney(value) {
    if (value === null || value === undefined) { return '<td>' + DASH + '</td>'; }
    return '<td class="' + (value > 0 ? 'down' : value < 0 ? 'up' : '') +
      '">' + money(value) + '</td>';
  }

  function slipRow(label, stats) {
    if (!stats) { return ''; }
    var html = '<tr><td>' + label + '</td>';
    html += '<td>' + stats.measured + '</td>';
    // Unmeasured stands in its own column, in words, so it can never be
    // mistaken for a zero in the mean.
    html += '<td>' + (stats.unmeasured
      ? '<span class="hint">' + stats.unmeasured + ' unmeasured</span>'
      : '') + '</td>';
    html += slipPoints(stats.points_mean);
    html += slipPoints(stats.points_median);
    html += slipPoints(stats.points_worst);
    html += slipPoints(stats.points_best);
    html += slipMoney(stats.money_total);
    html += '<td>' + (stats.measured
      ? stats.paid + ' paid / ' + stats.earned + ' earned' : DASH) + '</td>';
    return html + '</tr>';
  }

  function slipHead(first) {
    return '<thead><tr><th>' + first + '</th><th>Measured</th><th></th>' +
      '<th>Mean</th><th>Median</th><th>Worst</th><th>Best</th>' +
      '<th>Cost</th><th>Split</th></tr></thead>';
  }

  function slippageTable() {
    var report = state.slippage;
    var html = '<div class="journal-controls">' +
      '<label class="check"><input type="checkbox" class="all-sessions"' +
      (state.slippageAll ? ' checked' : '') + '> every session</label>' +
      '<a class="btn" href="/api/slippage.csv' +
      (state.slippageAll ? '?session=all' : '') + '" download>Export CSV</a>' +
      '<span class="hint">Measured against the price that was clicked, ' +
      'on the fills the broker actually gave us.</span></div>';

    if (report === null) { return html + '<p class="note">loading…</p>'; }
    if (report.error) {
      return html + '<p class="note mismatch">' + report.error + '</p>';
    }

    var window_ = report.window || {};
    html += '<table><tbody><tr><td>' + (window_.label || DASH) + '</td>' +
      '<td>' + report.counts.positions + ' positions (' +
      report.counts.open + ' still open)</td>';
    var journal = report.journal;
    html += '<td>' + (journal
      ? journal.fills + ' of our fills at the broker over the same window'
      : 'the journal could not be counted') + '</td></tr></tbody></table>';
    if (window_.note) {
      html += '<div class="' + (window_.clock === 'broker' ? 'hint' : 'note') +
        '">' + window_.note + '</div>';
    }

    if (!report.counts.positions) {
      return html + '<p class="note">nothing was traded in this window. ' +
        'That is not a slippage of zero — there is nothing to measure ' +
        'yet.</p>';
    }

    html += '<table>' + slipHead('This session');
    html += '<tbody>';
    html += slipRow('Entries', report.overall.entry);
    html += slipRow('Exits', report.overall.exit);
    html += slipRow('Round turn', report.overall.round_trip);
    html += '</tbody></table>';

    // MARKET against LIMIT, side by side. This is the split the peg has
    // to justify itself against: a LIMIT that is not beating a market
    // click on the same ladder is costing time for nothing.
    html += '<table>' + slipHead('Entries by order type') + '<tbody>';
    Object.keys(report.by_order_type).forEach(function (type) {
      html += slipRow(type, report.by_order_type[type].entry);
    });
    html += '</tbody></table>';

    html += '<table>' + slipHead('Entries by ladder') + '<tbody>';
    Object.keys(report.by_pair).forEach(function (key) {
      html += slipRow(report.by_pair[key].name || key,
                      report.by_pair[key].entry);
    });
    html += '</tbody></table>';

    if ((report.worst || []).length) {
      html += '<table><thead><tr><th>Worst entries</th><th>Side</th>' +
        '<th>Qty</th><th>Type</th><th>Slip</th><th>Cost</th>' +
        '<th>Click→on</th><th>Opened</th></tr></thead><tbody>';
      report.worst.forEach(function (row) {
        html += '<tr><td>' + (row.pair_key || DASH) + '</td>';
        html += '<td>' + (row.side || DASH) + '</td>';
        html += '<td>' + row.quantity + '</td>';
        html += '<td>' + (row.order_type || DASH) + '</td>';
        html += slipPoints(row.entry_points);
        html += slipMoney(row.entry_money);
        html += '<td>' + (row.click_to_on_ms === null ||
                          row.click_to_on_ms === undefined
          ? DASH : Math.round(row.click_to_on_ms) + 'ms') + '</td>';
        html += '<td>' + localTime(row.opened_at) + '</td></tr>';
      });
      html += '</tbody></table>';
    }
    return html;
  }

  function localTime(seconds) {
    if (!seconds) { return DASH; }
    return new Date(seconds * 1000).toTimeString().slice(0, 8);
  }

  function loadSlippage(force) {
    var now = Date.now();
    if (!force && state.slippage !== null && now - state.slippageAt < 5000) {
      return;
    }
    state.slippageAt = now;
    fetch('/api/slippage' + (state.slippageAll ? '?session=all' : ''))
      .then(function (r) { return r.json(); })
      .then(function (body) {
        state.slippage = body.ok ? body : {error: body.error};
        render();
      })
      .catch(function (error) {
        state.slippage = {error: 'the report could not be read: ' +
                                 error.message};
      });
  }

  function timingRow(label, values, note) {
    if (!values || !values.length) {
      // `hint`, not `note`: the note style is a full-width banner, and
      // inside a table cell it paints a yellow band across the table.
      return '<tr><td>' + label + '</td><td>' + DASH +
        ' <span class="hint">nothing measured yet — not zero</span>' +
        '</td></tr>';
    }
    var sorted = values.slice().sort(function (a, b) { return a - b; });
    var worst = sorted[sorted.length - 1];
    var median = sorted[Math.floor(sorted.length / 2)];
    return '<tr><td>' + label + '</td><td>median ' + Math.round(median) +
      'ms, worst ' + Math.round(worst) + 'ms over ' + values.length +
      ' — ' + note + '</td></tr>';
  }

  function accountsTable() {
    // Margin is posted PER ACCOUNT with two brokers. There is no
    // combined figure worth showing: the pair can only be carried by
    // the WEAKER of the two, and a total would read comfortable while
    // one side sits at its stop-out.
    var health = state.snapshot.account_health || {};
    var rows = health.accounts || {};
    var names = Object.keys(rows);
    var html = '';

    if (health.weakest) {
      var weakest = rows[health.weakest] || {};
      html += '<div class="note' + (weakest.tight ? ' mismatch' : '') + '">' +
        'The weakest account governs: <b>' + health.weakest + '</b> at ' +
        fmt(health.weakest_level, 1) + '% margin level' +
        (weakest.tight
          ? ' — under the ' + fmt(health.warn_level, 0) + '% you set, and ' +
            'it is this account that stops the pair, not the pair\'s total.'
          : '.') + '</div>';
    }
    if ((health.unknown || []).length) {
      html += '<div class="note mismatch">' + health.unknown.join(', ') +
        ' could not be read — UNKNOWN, not flat and not funded.</div>';
    }

    html += '<table><thead><tr><th>Account</th><th>Login</th>' +
      '<th>Equity</th><th>Balance</th><th>Credit</th><th>Open P&amp;L</th>' +
      '<th>Margin used</th><th>Free</th><th>Level</th><th>Call / Stop</th>' +
      '<th>Leverage</th><th>Our legs</th><th>Our lots</th><th>Our units</th>' +
      '</tr></thead><tbody>';
    names.forEach(function (name) {
      var row = rows[name];
      html += '<tr class="' + (row.tight ? 'mismatch' : '') + '">';
      html += '<td>' + name + '</td>';
      html += '<td>' + (row.login || DASH) +
        (row.server ? '<div class="hint">' + row.server + '</div>' : '') +
        '</td>';
      // Equity first, then balance and credit beside it: a demo funded
      // with credit shows a balance of 0.00 against real equity.
      html += '<td>' + money(row.equity) + '</td>';
      html += '<td>' + money(row.balance) + '</td>';
      html += '<td>' + money(row.credit) + '</td>';
      html += '<td class="' + (row.profit > 0 ? 'up' : row.profit < 0
                               ? 'down' : '') + '">' + money(row.profit) +
        '</td>';
      html += '<td>' + money(row.margin) + '</td>';
      html += '<td>' + money(row.margin_free) + '</td>';
      html += '<td>' + (row.margin_level === null ||
                        row.margin_level === undefined
                        ? DASH : fmt(row.margin_level, 1) + '%') + '</td>';
      html += '<td>' + (row.so_call === null || row.so_call === undefined
                        ? DASH : fmt(row.so_call, 0) + '% / ' +
                          fmt(row.so_so, 0) + '%') + '</td>';
      html += '<td>' + (row.leverage ? row.leverage + 'x' : DASH) + '</td>';
      html += '<td>' + row.our_legs + '</td>';
      html += '<td>' + fmt(row.our_lots, 2) + '</td>';
      html += '<td>' + fmt(row.our_units, 2) + '</td>';
      html += '</tr>';
    });
    if (!names.length) {
      html += '<tr><td colspan="14">no accounts connected</td></tr>';
    }
    html += '</tbody></table>';
    html += '<div class="note">Equity is what the broker actually has ' +
      'of yours: balance plus credit plus open P&amp;L. Brokers often ' +
      'fund a demo with CREDIT, so a balance of 0.00 against real ' +
      'equity is normal rather than alarming. "Our lots" and "our ' +
      'units" are this system\'s own legs on that account — what is ' +
      'left is somebody else\'s, or the trader\'s own.</div>';
    return html;
  }

  function reconcileTable() {
    var reconciler = state.snapshot.reconciler || {};
    var html = '';
    var unclaimed = reconciler.unclaimed || [];
    if (unclaimed.length) {
      // The positions nothing will close automatically, with the two
      // things a person can do about them.
      html += '<div class="note mismatch">' + unclaimed.length +
        ' position(s) at the broker carry our magic but are not in our ' +
        'book. Nothing is closed automatically: adopt one into a pair, ' +
        'or close it by hand.</div>';
      html += '<table class="unclaimed"><thead><tr><th>Account</th>' +
        '<th>Ticket</th><th>Symbol</th><th>Side</th><th>Volume</th>' +
        '<th>Open</th><th></th></tr></thead><tbody>';
      unclaimed.forEach(function (row) {
        html += '<tr><td>' + row.account + '</td><td>' + row.ticket +
          '</td><td>' + row.symbol + '</td><td>' + row.side + '</td><td>' +
          fmt(row.volume, 2) + '</td><td>' + fmt(row.price_open, 4) +
          '</td><td><button class="btn close-unclaimed" data-account="' +
          row.account + '" data-ticket="' + row.ticket +
          '">Close it</button></td></tr>';
      });
      html += '</tbody></table>';
    }
    html += '<table><thead><tr><th>When</th><th>Symbol</th><th>Ticket</th>' +
      '<th>Volume</th><th>Contract</th><th>P&amp;L</th><th>Note</th>' +
      '</tr></thead><tbody>';
    var closes = reconciler.untracked_closes || [];
    closes.forEach(function (entry) {
      html += '<tr><td>' + new Date(entry.at * 1000).toLocaleTimeString() +
        '</td><td>' + entry.symbol + '</td><td>' + entry.ticket + '</td><td>' +
        entry.volume + '</td><td>' + entry.contract_size +
        (entry.contract_size_assumed ? ' (assumed)' : '') + '</td><td>' +
        money(entry.pnl) + '</td><td>' + (entry.note || '') + '</td></tr>';
    });
    if (!closes.length) {
      html += '<tr><td colspan="7">nothing untracked has been closed</td></tr>';
    }
    html += '</tbody></table>';
    if ((reconciler.escalated || []).length) {
      html += '<div class="note mismatch">CLOSE IT BY HAND: ' +
        reconciler.escalated.join(', ') + '</div>';
    }
    if ((reconciler.unknown_accounts || []).length) {
      html += '<div class="note">' + reconciler.unknown_accounts.join(', ') +
        ' could not be read this pass — UNKNOWN, not flat. No orphans ' +
        'or ghosts were inferred from them.</div>';
    }
    return html;
  }

  function onMonitorClick(e) {
    var button = e.target.closest('button');
    if (!button) { return; }
    var row = button.closest('tr');
    if (button.classList.contains('close-unclaimed')) {
      return ask('Close ' + button.dataset.account + ':' +
                 button.dataset.ticket + '?',
                 'This position is at the broker but not in our book. ' +
                 'Closing it is by ticket, at market, and cannot be ' +
                 'undone.', 'Close it', function () {
                   send('close_unclaimed',
                        {account: button.dataset.account,
                         ticket: button.dataset.ticket});
                 });
    }
    if (button.classList.contains('close-position')) {
      send('close_position', {position_id: row.dataset.position});
    } else if (button.classList.contains('cancel-order')) {
      send('cancel_order', {order_id: row.dataset.order});
    }
  }

  // -- the whole screen ---------------------------------------------------

  function linkState() {
    /* "Is it working?" answered in one line, from three facts: the
     * engine is publishing, both accounts answered, and every enabled
     * ladder has a two-sided quote. Anything less is not green — a
     * screen that says CONNECTED while one leg is dark is the failure
     * this whole banner exists to prevent.
     */
    var snapshot = state.snapshot;
    if (snapshot.engine !== 'up') {
      // STALLED and DOWN are different faults with different fixes: a
      // process that has stopped publishing while the page still reads
      // its last file, against no file at all. Both were called ENGINE
      // DOWN, and the first one is the one where every price on the
      // screen is a photograph.
      return {state: 'bad',
              text: snapshot.engine === 'stalled' ? 'ENGINE STALLED'
                : 'ENGINE DOWN',
              detail: snapshot.engine_note || 'the coordinator is not running'};
    }
    var health = (snapshot.account_health || {}).accounts || {};
    var names = Object.keys(health);
    var known = names.filter(function (name) { return health[name].known; });
    if (!names.length) {
      return {state: 'bad', text: 'NO ACCOUNTS',
              detail: 'no account is configured — open Exchanges'};
    }
    if (known.length < names.length) {
      var dark = names.filter(function (name) { return !health[name].known; });
      return {state: 'bad', text: known.length + '/' + names.length +
              ' ACCOUNTS',
              detail: dark.join(', ') + ' could not be read — check that ' +
                      'terminal is open and logged in'};
    }
    var pairs = snapshot.pairs || {};
    var enabled = Object.keys(pairs).filter(function (key) {
      return pairs[key].enabled !== false;
    });
    var quiet = enabled.filter(function (key) {
      var market = pairs[key].market;
      return !market || market.short_spread === null ||
        market.short_spread === undefined;
    });
    if (enabled.length && quiet.length) {
      return {state: 'warn', text: known.length + '/' + names.length +
              ' ACCOUNTS · NO QUOTE',
              detail: quiet.join(', ') + ': both terminals are attached, ' +
                      'but no two-sided quote has arrived — check the ' +
                      'symbols on the Exchanges page'};
    }
    var stale = enabled.filter(function (key) {
      return (pairs[key].market || {}).stale_reason;
    });
    if (stale.length) {
      // With the leg and the number in the badge itself: "stale" alone
      // sends the operator looking for a fault when the market may
      // simply be quiet.
      var worst = (pairs[stale[0]].market || {}).stale_reason || '';
      return {state: 'warn',
              text: 'QUOTES STALE · ' + (pairs[stale[0]].name || stale[0]),
              detail: worst + '. If this instrument is simply quiet, ' +
                      'raise MAX_QUOTE_AGE_SEC in Settings; if the ' +
                      'terminal has lost its feed, the price is frozen ' +
                      'in MT5 too.'};
    }
    return {state: 'ok',
            text: 'LIVE · ' + known.length + '/' + names.length +
                  ' accounts',
            detail: 'both terminals attached and every enabled ladder is ' +
                    'quoting'};
  }

  function renderLink() {
    var link = linkState();
    var badge = el('link-badge');
    badge.textContent = link.text;
    badge.className = 'link ' + link.state;
    badge.title = link.detail;
  }

  function reportDeadOrders() {
    /* A working order that STOPPED working, said out loud once.
     *
     * The broker can refuse the pending behind a synthetic — invalid
     * price, stops level, trading disabled — and the order then leaves
     * the book in the same instant the click was accepted. What the
     * trader saw was a green toast and an empty Work column, with the
     * refusal nowhere on the screen. This is that refusal, in the
     * broker's own words, on the screen, once.
     */
    var pairs = state.snapshot.pairs || {};
    Object.keys(pairs).forEach(function (key) {
      (pairs[key].dead_orders || []).forEach(function (order) {
        if (state.reported[order.order_id]) { return; }
        state.reported[order.order_id] = true;
        // A REFUSAL AND A WITHDRAWAL ARE NOT THE SAME NEWS. The
        // broker rejecting a pending is a fault and stays on the
        // screen until it is read. An order CANCELLED — by the
        // trader's own next click, by a sweep, by AutoRouting
        // standing down — is the system doing as it was told, and
        // dressing it as an alarm taught the desk to dismiss alarms.
        toast((pairs[key].name || key) + ': ' + order.side + ' ' +
              order.quantity + ' at ' + fmt(order.level, 4) + ' — ' +
              order.state.toLowerCase() + '. ' + order.reason,
              order.state === 'CANCELLED' ? 'ok' : undefined);
      });
    });
  }

  function renderStatusLine() {
    /* The two things that change on every poll whether or not the
     * coordinator published anything: whether it is alive, and how old
     * its last snapshot is. Cheap, and separate from the panels, so a
     * stalled engine does not cost a full redraw three times a second
     * — which is exactly when the screen must stay responsive. */
    var snapshot = state.snapshot;
    var banner = el('engine-banner');
    if (snapshot.engine && snapshot.engine !== 'up') {
      banner.textContent = snapshot.engine_note;
      banner.classList.remove('hidden');
    } else {
      banner.classList.add('hidden');
    }
    renderLink();
    var loop = snapshot.loop_interval_sec;
    el('loop-stat').textContent = loop
      ? 'loop ' + (loop * 1000).toFixed(0) + 'ms · snapshot ' +
        ((snapshot.status_age_sec || 0) * 1000).toFixed(0) + 'ms old'
      : DASH;
  }

  function render() {
    // Not while a window is being dragged. The whole screen is rebuilt
    // three times a second, and doing that under the pointer is what
    // made a window judder and lag behind the cursor. The drag is short
    // and drop() calls render() itself, so nothing stays stale.
    if (document.querySelector('.window.dragging')) { return; }
    var snapshot = state.snapshot;
    prunePending();
    renderStatusLine();

    renderNaked();
    renderUnclaimed();
    reportDeadOrders();
    renderUnresolved();

    var wanted = {};
    state.open.forEach(function (id) { wanted[id] = true; });
    Object.keys(snapshot.pairs || {}).forEach(function (key) {
      var id = panelId('ladder', key);
      if (wanted[id]) { renderLadder(key, snapshot.pairs[key]); }
      // The pair's setting SEEDS this window; the desk decides. Open
      // it because the setting says so, unless this screen has closed
      // it — and keep it open once it is, whatever a later snapshot
      // says, because a window that vanishes when the engine hiccups
      // is a window the trader cannot rely on.
      var fairId = panelId('fair', key);
      if (snapshot.pairs[key].algo_window && !state.closed[fairId]
          && state.open.indexOf(fairId) < 0) {
        state.open.push(fairId);
      }
      if (state.open.indexOf(fairId) >= 0) {
        wanted[fairId] = true;
        renderFairWindow(key, snapshot.pairs[key]);
      }
    });
    if (wanted[panelId('grid')]) { renderGrid(); }
    if (wanted[panelId('monitor')]) { renderMonitor(); }
    if (wanted[panelId('settings')] && window.MT5Settings) {
      window.MT5Settings.render();
    }

    // Remove the panels that are no longer open.
    Array.prototype.forEach.call(document.querySelectorAll('.window'),
      function (node) {
        if (!wanted[panelIdOf(node)]) { node.remove(); return; }
        // Where it was left, and the size it was left at, every render:
        // panels are created lazily, and one opened later must still
        // come back to its own corner.
        ensureGrip(node);
        applyLayout(node);
      });

    renderTabs();
    revealPanel();
  }

  function revealPanel() {
    /* Scroll a newly opened window into view — once, and never while
     * it is being dragged. A floating window is already where the
     * trader put it, so only the desktop's horizontal scroll moves. */
    var id = state.reveal;
    if (!id) { return; }
    state.reveal = null;
    var node = null;
    Array.prototype.forEach.call(document.querySelectorAll('.window'),
      function (candidate) {
        if (panelIdOf(candidate) === id) { node = candidate; }
      });
    if (!node) { return; }
    var desktop = el('desktop');
    var box = node.getBoundingClientRect();
    var frame = desktop.getBoundingClientRect();
    if (box.left < frame.left || box.right > frame.right) {
      desktop.scrollLeft += box.left - frame.left - 4;
    }
    node.classList.add('revealed');
    window.setTimeout(function () {
      node.classList.remove('revealed');
    }, 900);
  }

  function renderUnresolved() {
    /* ORDERS WE DO NOT KNOW THE OUTCOME OF.
     *
     * This banner replaced the two-legs-on-one-MT5-login one, which
     * cannot happen here: one Arrow session trades both legs and there
     * is no second login to collide with.
     *
     * What takes its place is worse, and it is specific to this venue.
     * mt5.order_send returns the fill — when the call returns, the
     * trade happened or it did not. Arrow returns an order id and the
     * fill is polled, so an order can be neither filled nor rejected
     * but simply still working when we stop waiting.
     *
     * It sits ABOVE the naked-leg banner because a naked leg is a
     * KNOWN exposure and this is an unknown one, and NOTHING clears it
     * automatically — only a person who has looked at the broker's own
     * order book. */
    var rows = state.snapshot.unresolved || [];
    var banner = el('same-login-banner');
    if (!rows.length) {
      banner.classList.add('hidden');
      return;
    }
    banner.innerHTML = rows.map(function (row) {
      return '<b>UNRESOLVED — ' + escapeHtml(row.symbol || '') +
        ' order ' + escapeHtml(String(row.ticket || '?')) +
        '</b> is neither filled nor rejected. Nothing has been unwound.' +
        (row.exposed && row.exposed.volume
          ? ' Leg ' + escapeHtml(row.exposed.leg) + ' IS ON for ' +
            qty(row.exposed.volume) + ' lots and is NAKED until this is ' +
            'settled.'
          : '') +
        ' <button class="banner-clear" data-ticket="' +
        escapeHtml(String(row.ticket || '')) + '">I have checked it</button>';
    }).join(' | ');
    banner.title = 'Look at this order in the broker\u2019s own order ' +
      'book before doing anything. Trading against an order that is ' +
      'still working does not cancel it \u2014 it opens the opposite ' +
      'position, which then fills against the leg you were cancelling.';
    banner.classList.remove('hidden');
  }
  function closeButton() {
    /* Every banner can be put away. This one is an FYI: it names a
     * thing worth knowing and does not stop the trader working. It
     * comes BACK if what it is about changes — a dismissal is for the
     * situation the trader read, not for the banner. */
    return '<button class="banner-close" title="Dismiss — it returns if ' +
      'this changes">&times;</button>';
  }

  function renderUnclaimed() {
    /* One line, and it can be put away.
     *
     * A position at the broker that our book cannot explain is exactly
     * the one an automatic close must never touch, so it has to be
     * SAID — but a wall of table across the top of the screen is not
     * how to say it. The line names the number; the table lives in
     * Positions -> Reconciler, where the buttons that act on it are.
     */
    var reconciler = state.snapshot.reconciler || {};
    var unclaimed = reconciler.unclaimed || [];
    var banner = el('unclaimed-banner');
    if (!unclaimed.length) {
      banner.classList.add('hidden');
      return;
    }
    var signature = unclaimed.map(function (row) {
      return row.account + ':' + row.ticket;
    }).sort().join(',');
    if (state.dismissed.unclaimed === signature) {
      banner.classList.add('hidden');
      return;
    }
    banner.dataset.signature = signature;
    banner.innerHTML = closeButton() +
      '<button class="banner-open">Review</button>' +
      '<b>' + unclaimed.length + ' position(s) at the broker are not in ' +
      'our book.</b> Nothing is closed automatically.';
    banner.title = unclaimed.map(function (row) {
      return row.account + ' #' + row.ticket + ' ' + row.side + ' ' +
        row.volume + ' ' + row.symbol;
    }).join('\n');
    banner.classList.remove('hidden');
  }

  function renderNaked() {
    var messages = [];
    Object.keys(state.snapshot.pairs || {}).forEach(function (key) {
      var row = state.snapshot.pairs[key];
      (row.positions || []).forEach(function (position) {
        var a = position.leg_a, b = position.leg_b;
        if (!a || !b || !a.volume || !b.volume) {
          messages.push(key + ': one leg is on and the other is not');
        }
      });
    });
    var banner = el('naked-banner');
    if (messages.length) {
      banner.textContent = 'NAKED LEG — ' + messages.join(' | ') +
        '. Hedge it or flatten it now.';
      banner.classList.remove('hidden');
    } else {
      banner.classList.add('hidden');
    }
  }

  function renderTabs() {
    var html = '';
    state.open.forEach(function (id) {
      var parts = id.split(':');
      var label = parts[0] === 'ladder'
        ? ((state.snapshot.pairs[parts.slice(1).join(':')] || {}).name ||
           parts.slice(1).join(':'))
        : parts[0] === 'grid' ? 'Market Grid'
          : parts[0] === 'settings' ? 'Exchanges' : 'Positions';
      var badge = '';
      if (parts[0] === 'ladder') {
        var row = state.snapshot.pairs[parts.slice(1).join(':')] || {};
        var working = (row.working_buys || 0) + (row.working_sells || 0);
        if (working) { badge = '<span class="badge">' + working + '</span>'; }
      }
      html += '<button class="tab' + (state.active === id ? ' on' : '') +
        '" data-panel="' + id + '">' + label + badge + '</button>';
    });
    el('tabs').innerHTML = html;
  }

  //: The keypad presets, in the order the reference screen has them.
  var QUANTITY_KEYS = {'1': 1, '2': 5, '3': 10, '4': 50, '5': 100};

  function onKey(e) {
    if (e.key === 'Escape') {
      closeModal();
      el('help-overlay').classList.add('hidden');
      return;
    }
    // Never steal a key from a field being typed into.
    var tag = (document.activeElement || {}).tagName;
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') { return; }
    if (e.metaKey || e.ctrlKey || e.altKey) { return; }
    // Turned off in the taskbar: B and S are ORDERS, and a desk that
    // does not want a keyboard near them can have none. Escape and ?
    // above still work — neither of them trades.
    if (state.keysOff) { return; }

    if (e.key === '?') {
      el('help-overlay').classList.toggle('hidden');
      return;
    }
    if (e.key === 'Tab') {
      e.preventDefault();
      return focusNextLadder();
    }

    var key = activeLadder();
    if (!key) { return; }
    var lower = e.key.toLowerCase();

    if (QUANTITY_KEYS[e.key] !== undefined) {
      setArmed(key, 'buy', QUANTITY_KEYS[e.key]);
      setArmed(key, 'sell', QUANTITY_KEYS[e.key]);
      return render();
    }
    if (e.key === '0') {
      setArmed(key, 'buy', '');
      setArmed(key, 'sell', '');
      return render();
    }
    if (lower === 'b') { return atTouch(key, 'BUY'); }
    if (lower === 's') { return atTouch(key, 'SELL'); }
    if (lower === 'f') { return exitPair(key); }
    if (lower === 'x') { return send('cancel_where', {pair: key}); }
    if (lower === 'l') {
      // Through the same path as the tick, so the keyboard and the
      // checkbox cannot end up disagreeing about a ladder that is
      // held: the key used to set the flag and leave the box unticked.
      setLocked(key, !state.locked[key]);
      return render();
    }
    if (lower === 'm') {
      var pair = state.snapshot.pairs[key] || {};
      return setPair(key, {order_type: pair.order_type === 'MARKET'
                           ? 'LIMIT' : 'MARKET'});
    }
  }

  function activeLadder() {
    if (!state.active || state.active.indexOf('ladder:') !== 0) { return null; }
    return state.active.slice('ladder:'.length);
  }

  function focusNextLadder() {
    var ladders = state.open.filter(function (id) {
      return id.indexOf('ladder:') === 0;
    });
    if (!ladders.length) { return; }
    var at = ladders.indexOf(state.active);
    state.active = ladders[(at + 1) % ladders.length];
    render();
  }

  // -- polling -------------------------------------------------------------

  function poll() {
    fetch('/api/status').then(function (r) { return r.json(); })
      .then(function (snapshot) {
        state.snapshot = snapshot;
        // A ghost whose order is now in the book has done its job.
        state.pending.forEach(function (ghost) {
          var pair = snapshot.pairs[ghost.pair_key] || {};
          var arrived = (pair.orders || []).some(function (order) {
            return order.side === ghost.side &&
              Math.abs(order.level - ghost.level) < 1e-9;
          }) || (pair.positions || []).some(function (position) {
            return position.opened_at * 1000 > ghost.at - 2000;
          });
          if (arrived) { ghost.done = true; }
        });
        var first = !state.open.length;
        // First load is LADDERS, and nothing else. The Market Grid and
        // the Positions window are tools: opened when wanted, from the
        // + button or the taskbar. Opening them here put two wide
        // windows over the ladders before the trader had asked for
        // either.
        // A ladder for every enabled pair — including one added while
        // this screen was open. A pair configured on the Exchanges page
        // and then nowhere to be seen is the whole setup looking
        // broken; the ladder appears beside the others the moment the
        // engine picks the pair up. A ladder the trader CLOSED stays
        // closed: that was a decision, and it is remembered.
        Object.keys(snapshot.pairs || {}).forEach(function (key) {
          var id = panelId('ladder', key);
          if (!snapshot.pairs[key].enabled) { return; }
          if (state.open.indexOf(id) >= 0 || state.closed[id]) { return; }
          state.open.unshift(id);
          if (!first) {
            state.reveal = id;
            toast('new ladder: ' + (snapshot.pairs[key].name || key), 'ok');
          }
        });
        if (first) { state.active = state.open[0]; }
        // Redraw the PANELS only when the coordinator has actually
        // published something new. `at` is its own publish clock, so a
        // stalled or restarting engine — the state this screen is in
        // exactly when the operator is trying to fix it — costs one
        // banner update per poll instead of a full redraw of every
        // ladder, the grid and the monitor.
        // A RESTING order that fills does so without a click, so the
        // sound has to come from the book changing rather than from a
        // command's answer.
        var positions = 0;
        Object.keys(snapshot.pairs || {}).forEach(function (key) {
          positions += (snapshot.pairs[key].positions || []).length;
        });
        if (state.positionCount !== null && positions > state.positionCount) {
          sound('filled');
        }
        state.positionCount = positions;
        var published = snapshot.at !== state.lastAt;
        state.lastAt = snapshot.at;
        if (published || state.pending.length) {
          render();
        } else {
          renderStatusLine();
        }
      })
      .catch(function (error) {
        el('engine-banner').textContent =
          'the web process is not answering: ' + error.message;
        el('engine-banner').classList.remove('hidden');
      });
  }

  function start() {
    el('add-panel').addEventListener('click', function (e) {
      e.stopPropagation();
      var menu = el('add-menu');
      if (!menu.classList.contains('hidden')) {
        return menu.classList.add('hidden');
      }
      var pairs = state.snapshot.pairs || {};
      var html = '<div class="menu-title">Ladders</div>';
      var keys = Object.keys(pairs);
      keys.forEach(function (key) {
        var pair = pairs[key];
        var open = state.open.indexOf(panelId('ladder', key)) >= 0;
        html += '<button data-panel="' + panelId('ladder', key) + '">' +
          (pair.name || key) + '<small>' + (pair.symbol_a || '?') + ' / ' +
          (pair.symbol_b || '?') + (open ? ' · open' : '') + '</small>' +
          '</button>';
      });
      if (!keys.length) {
        html += '<div class="menu-note">No pair is configured yet — add ' +
          'one on the Exchanges page. Each pair is its own ladder, and ' +
          'they trade side by side on this desktop.</div>';
      }
      html += '<div class="menu-title">Windows</div>' +
        '<button data-panel="' + panelId('grid') + '">Market Grid' +
        '<small>every ladder on one row each</small></button>' +
        '<button data-panel="' + panelId('monitor') + '">Trading Monitor' +
        '<small>positions, orders, fills, slippage, accounts</small>' +
        '</button>';
      menu.innerHTML = html;
      menu.classList.remove('hidden');
    });
    el('add-menu').addEventListener('click', function (e) {
      var button = e.target.closest('button');
      if (!button) { return; }
      el('add-menu').classList.add('hidden');
      openPanel(button.dataset.panel);
    });
    window.addEventListener('click', function () {
      el('add-menu').classList.add('hidden');
    });
    el('tabs').addEventListener('click', function (e) {
      var button = e.target.closest('.tab');
      if (!button) { return; }
      state.active = button.dataset.panel;
      // Clicking a tab means "show me that window" — including when it
      // is off the right-hand end of the desktop row.
      state.reveal = button.dataset.panel;
      render();
    });
    el('open-settings').addEventListener('click', function () {
      openPanel(panelId('settings'));
    });
    el('kill').addEventListener('click', function () {
      ask('Cancel everything, on every ladder?',
          'This pulls every working order across every pair. Tick nothing ' +
          'and it stops there; the second button also FLATTENS every open ' +
          'position at market, by ticket, which cannot be undone.',
          'Cancel all + flatten', function () {
            send('kill', {flatten: true});
          });
    });
    el('same-login-banner').addEventListener('click', function (e) {
      /* Clearing an unresolved order is a COMMAND, not a dismissal.
       * The engine cannot decide what happened to it and neither can
       * this page; a person looked, and that is the whole of the
       * information being recorded. */
      var button = e.target.closest('.banner-clear');
      if (!button) { return; }
      send('clear_unresolved', {ticket: button.dataset.ticket});
    });
    el('unclaimed-banner').addEventListener('click', function (e) {
      if (e.target.closest('.banner-open')) {
        // Where the table and the buttons that act on it live.
        state.monitorTab = 'reconcile';
        return openPanel(panelId('monitor'));
      }
      if (e.target.closest('.banner-close')) {
        state.dismissed.unclaimed = el('unclaimed-banner').dataset.signature;
        el('unclaimed-banner').classList.add('hidden');
        return;
      }
      var button = e.target.closest('.close-unclaimed');
      if (!button) { return; }
      ask('Close ' + button.dataset.account + ':' + button.dataset.ticket +
          '?',
          'This position is at the broker but not in our book. Closing it ' +
          'is by ticket, at market, and cannot be undone.',
          'Close it', function () {
            send('close_unclaimed', {account: button.dataset.account,
                                     ticket: button.dataset.ticket});
          });
    });
    try {
      state.keysOff =
        window.localStorage.getItem('mt5trader.keysOff') === '1';
    } catch (e) { state.keysOff = false; }
    function paintKeys() {
      var button = el('keys-toggle');
      button.textContent = 'Keys: ' + (state.keysOff ? 'off' : 'on');
      button.classList.toggle('off', state.keysOff);
    }
    paintKeys();
    try {
      state.soundOff =
        window.localStorage.getItem('mt5trader.soundOff') === '1';
    } catch (e) { state.soundOff = false; }
    function paintSound() {
      var button = el('sound-toggle');
      button.textContent = 'Sound: ' + (state.soundOff ? 'off' : 'on');
      button.classList.toggle('off', state.soundOff);
    }
    paintSound();
    el('sound-toggle').addEventListener('click', function () {
      state.soundOff = !state.soundOff;
      try {
        window.localStorage.setItem('mt5trader.soundOff',
                                    state.soundOff ? '1' : '0');
      } catch (e) { /* a preference that does not survive a reload */ }
      paintSound();
      if (!state.soundOff) { sound('placed'); }   // so it is heard once
    });
    el('keys-toggle').addEventListener('click', function () {
      state.keysOff = !state.keysOff;
      try {
        window.localStorage.setItem('mt5trader.keysOff',
                                    state.keysOff ? '1' : '0');
      } catch (e) { /* a preference that does not survive a reload */ }
      paintKeys();
      toast(state.keysOff
        ? 'keyboard shortcuts are OFF — the buttons and the ladder still work'
        : 'keyboard shortcuts are back on', 'ok');
    });
    el('help').addEventListener('click', function () {
      el('help-overlay').classList.toggle('hidden');
    });
    el('help-close').addEventListener('click', function () {
      el('help-overlay').classList.add('hidden');
    });
    el('modal-cancel').addEventListener('click', closeModal);
    el('modal-confirm').addEventListener('click', function () {
      var handler = el('modal')._onConfirm;
      closeModal();
      if (handler) { handler(); }
    });
    document.addEventListener('keydown', onKey);
    window.addEventListener('click', function (e) {
      var window_ = e.target.closest('.window');
      if (!window_) { return; }
      state.active = panelIdOf(window_);
      raise(window_);
    });
    // Delegated, on the desktop rather than on each window: panels come
    // and go, and a listener per window is a listener per window to
    // forget.
    el('desktop').addEventListener('pointerdown', startDrag);
    el('tidy').addEventListener('click', tidyWindows);
    poll();
    window.setInterval(poll, REFRESH_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }

  // The settings panel lives in its own file and borrows these: one
  // modal, one toast rack, one command path. A second copy of any of
  // them is a second set of house rules to keep.
  window.MT5Trader = {
    state: state, render: render, toast: toast, ask: ask, send: send,
    panelId: panelId, openPanel: openPanel, closePanel: closePanel,
    fmt: fmt, money: money, DASH: DASH,
    tidyWindows: tidyWindows, sound: sound,
    showFairWindow: showFairWindow,
    toastOutcome: toastOutcome
  };
})();
