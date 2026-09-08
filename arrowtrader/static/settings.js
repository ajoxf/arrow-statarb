/* Exchanges: the Arrow session, the segments, the charges, the pairs.
 *
 * REWRITTEN FOR ARROW. What was here was MT5-Trader's panel, and it
 * rendered a TABLE OF TERMINALS: a terminal64.exe path, an MT5 login,
 * a server name and a runner endpoint per account, policed by three
 * clash refusals — two accounts on one port, one login, one terminal
 * folder. Every one of those is a way to end up trading one MT5
 * account while both screens report two.
 *
 * None of it can happen here. There is ONE Arrow session and both legs
 * hold it; there are no endpoints, no terminal paths and no second
 * login to collide with. What replaces the three clashes is a single
 * question with the same weight, asked once:
 *
 *     IS THIS ACCOUNT USED BY ANYTHING ELSE?
 *
 * On a netting exchange the trader's own position and this system's
 * are ONE number per contract, and nothing in the API separates them.
 * A dedicated account restores the guarantee MT5-Trader gets free from
 * its magic number. It is a DECLARATION, not a measurement, so it
 * defaults to off and every reconciler finding is shown beside it.
 *
 * Two house rules survive from the original and do most of the work:
 *   - A warning nobody can act on is not a fix. Where this can name
 *     the wrong field AND the right value it offers a one-click
 *     correction — but it stays a click. Nothing is corrected silently.
 *   - Structural fields need a restart and SAY so; nothing else does.
 *
 * And this panel must work with the ENGINE DOWN. The engine will not
 * start until the credentials and the contracts are right, and these
 * are the tools for getting them right — so everything here talks to
 * the web process's own short-lived Arrow session, never through the
 * engine.
 */

(function () {
  'use strict';

  var UI = window.ArrowTrader;
  var DASH = UI.DASH;

  var local = {
    account: null,        // app id, user id, dedicated, which secrets are set
    connection: null,     // the last Connect answer
    segments: null,       // which segments this account can actually reach
    charges: null,        // the Indian stack, per segment, + configured
    pairs: {},
    editing: null,        // the pair key being edited, '' for a new one
    draft: {},            // the pair form's current values
    derived: null,        // what the MASTER says about the draft's two legs
    picker: {},           // leg -> {segment, underlying, contracts}
    settings: null,
    busy: {}
  };

  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function get(path) {
    return fetch(path).then(function (r) {
      return r.json().then(function (body) { return {ok: r.ok, body: body}; });
    }).catch(function (error) {
      return {ok: false, body: {error: String(error)}};
    });
  }

  function post(path, payload, method) {
    return fetch(path, {
      method: method || 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload || {})
    }).then(function (r) {
      return r.json().then(function (body) { return {ok: r.ok, body: body}; });
    }).catch(function (error) {
      return {ok: false, body: {error: String(error)}};
    });
  }

  // -- the panel ----------------------------------------------------------

  function node() {
    var panel = document.querySelector('.window.settings');
    if (panel) { return panel; }
    panel = document.createElement('section');
    panel.className = 'window settings';
    panel.innerHTML =
      '<div class="titlebar"><span class="swatch"></span>' +
      '<span class="title">Exchanges &mdash; Arrow session, segments and pairs</span>' +
      '<span class="winbtns"><button class="winbtn close" title="Close">&times;</button>' +
      '</span></div>' +
      '<div class="settings-body">' +
      '<section class="ready"></section>' +
      '<section class="session"></section>' +
      '<section class="segments"></section>' +
      '<section class="charges"></section>' +
      '<section class="trading"></section>' +
      '<section class="pairs"></section>' +
      '</div>' +
      '<div class="note">Credentials and the instrument master are read at ' +
      'STARTUP, so a change to them restarts the engine. Charges, the ' +
      'dedicated declaration and everything on a ladder apply at once.</div>';
    panel.querySelector('.close').addEventListener('click', function () {
      UI.closePanel(UI.panelId('settings'));
    });
    panel.addEventListener('click', onClick);
    panel.addEventListener('change', onChange);
    // `change` fires on BLUR. The operator types a contract name, looks
    // at the dropdown beside it, and nothing has happened yet — which
    // is exactly what "instruments not loading" looked like. Search as
    // they type.
    panel.addEventListener('input', onInput);
    document.getElementById('desktop').appendChild(panel);
    refresh();
    return panel;
  }

  function refresh() {
    Promise.all([get('/api/account'), get('/api/pairs'), get('/api/settings'),
                 get('/api/segments'), get('/api/charges')])
      .then(function (results) {
        local.account = results[0].body;
        local.pairs = results[1].body || {};
        local.settings = results[2].body;
        local.segments = results[3].body;
        local.charges = results[4].body;
        render();
      });
  }

  function render(force) {
    var panel = document.querySelector('.window.settings');
    if (!panel) { return; }
    // Never redraw a section the operator is typing into.
    var focused = document.activeElement;
    var editing = focused && panel.contains(focused) &&
      /INPUT|SELECT|TEXTAREA/.test(focused.tagName);
    if (editing && !force) { return; }
    redraw(panel.querySelector('.ready'), readyHtml());
    redraw(panel.querySelector('.session'), sessionHtml());
    redraw(panel.querySelector('.segments'), segmentsHtml());
    redraw(panel.querySelector('.charges'), chargesHtml());
    redraw(panel.querySelector('.trading'), tradingHtml());
    redraw(panel.querySelector('.pairs'), pairsHtml());
  }

  function redraw(target, html) {
    if (target && target.innerHTML !== html) { target.innerHTML = html; }
  }

  // -- one line: is it working --------------------------------------------

  function readyHtml() {
    var account = local.account || {};
    var connection = local.connection;
    var missing = account.missing || [];
    var state, message;

    if (missing.length) {
      // A form not yet filled in, which is prose. The BROKER'S REFUSAL is
      // a different state ('not-ready'): it is quoted verbatim, and it is
      // shown in the monospace the broker's own words are formatted in.
      state = 'no-creds';
      message = 'these credentials are not set: <b>' +
        missing.map(esc).join('</b>, <b>') + '</b> &mdash; enter them below. ' +
        'They go to <code>.env</code>, never to config.json.';
    } else if (!connection) {
      state = 'unknown';
      message = 'credentials are set. Press <b>Connect</b> to log in and ' +
        'load the instrument master.';
    } else if (!connection.ok) {
      state = 'not-ready';
      // THE BROKER'S OWN WORDS. Never "check the log".
      message = esc(connection.error || 'Arrow refused the connection');
    } else {
      state = 'ready';
      var sources = connection.master_sources || {};
      var routes = Object.keys(sources);
      message = 'Arrow session live &middot; instrument master <b>' +
        Number(connection.master_rows || 0).toLocaleString('en-IN') +
        '</b> contracts across ' +
        (connection.segments || []).map(esc).join(', ') +
        // Which routes it came from. `/all` is not always the whole
        // master, and "no MCX contracts" means one thing if /mcx
        // answered with none and another if it was never reachable.
        (routes.length > 1 ? ' <span class="dim">(' + routes.map(
          function (route) {
            return esc(route) + ' ' +
              Number(sources[route]).toLocaleString('en-IN');
          }).join(' + ') + ')</span>' : '');
    }
    return '<div class="ready-line ' + state + '">' +
      '<b>' + (state === 'ready' ? 'CONNECTED'
        : state === 'unknown' ? 'NOT CONNECTED'
        : state === 'no-creds' ? 'NOT SET UP' : 'NOT READY') + '</b> ' +
      message + '</div>';
  }

  // -- the session ---------------------------------------------------------

  function sessionHtml() {
    var account = local.account || {};
    var secrets = account.secrets || {};
    var html = '<h3>The Arrow session <small>one login trades both legs ' +
      '&mdash; there is no second terminal, port or account to configure' +
      '</small></h3>';

    html += '<div class="session-box">';
    html += '<div class="field-row">';
    html += field('App ID', 'f-app-id', account.app_id, 'text',
                  'from your Arrow app registration');
    html += field('User ID', 'f-user-id', account.user_id, 'text', '');
    html += secretField('Password', 'f-password', secrets.ARROW_PASSWORD,
                        'ARROW_PASSWORD');
    html += '</div>';

    html += '<div class="field-row">';
    html += secretField('API secret', 'f-api-secret',
                        secrets.ARROW_API_SECRET, 'ARROW_API_SECRET');
    // The mistake everybody makes once.
    html += '<label class="sfield wide"><span>TOTP <b class="warn">seed' +
      '</b></span><input class="f-totp" type="password" placeholder="' +
      (secrets.ARROW_TOTP_SECRET ? 'set — type to replace' : 'not set') +
      '"><div class="hint warn-hint"><b>The base32 seed from your ' +
      'authenticator setup &mdash; not the 6-digit code.</b> ' +
      'ARROW_TOTP_SECRET in <code>.env</code>.</div></label>';
    html += '</div>';

    // THE DECLARATION EVERY RECONCILER FINDING DEPENDS ON.
    html += '<label class="dedicated' + (account.dedicated ? ' on' : '') + '">' +
      '<input type="checkbox" class="f-dedicated"' +
      (account.dedicated ? ' checked' : '') + '>' +
      '<span><b>This Arrow account is used by nothing else.</b>' +
      '<div class="hint">Leave it off unless it is true. On a netting ' +
      'exchange your own position and this system’s are ONE number per ' +
      'contract, and nothing in the API separates them. Off, the reconciler ' +
      'never calls a difference an orphan and never offers to close one ' +
      '&mdash; it says it cannot tell.</div></span></label>';

    html += '<div class="session-actions">' +
      '<button class="btn primary connect">Connect</button>' +
      '<button class="btn save-session">Save</button>' +
      '<span class="hint">SEBI requires the host’s IP to be registered ' +
      'with Arrow. Nothing works from an unregistered box, and the failure ' +
      'reads like a password problem.</span></div>';
    html += '</div>';
    return html;
  }

  function field(label, cls, value, type, hint) {
    return '<label class="sfield"><span>' + esc(label) + '</span>' +
      '<input class="' + cls + '" type="' + (type || 'text') +
      '" value="' + esc(value) + '">' +
      (hint ? '<div class="hint">' + hint + '</div>' : '') + '</label>';
  }

  function secretField(label, cls, isSet, key) {
    return '<label class="sfield"><span>' + esc(label) + '</span>' +
      '<input class="' + cls + '" type="password" placeholder="' +
      (isSet ? 'set — type to replace' : 'not set') + '">' +
      '<div class="hint">' + esc(key) + ' in <code>.env</code>' +
      (isSet ? ' &middot; <span class="ok-ink">set</span>' : '') +
      '</div></label>';
  }

  // -- segments: what this account can actually reach ----------------------

  function segmentsHtml() {
    var found = local.segments;
    var html = '<h3>Segments <small>a segment needs BOTH: contracts in the ' +
      'master, and a value in the SDK’s Exchange enum</small></h3>';
    if (!found || !found.ok) {
      return html + '<p class="hint">' +
        esc((found && found.error) || 'connect to read the segments') +
        '</p>';
    }
    html += '<table class="grid-form segments-table"><thead><tr>' +
      '<th>Segment</th><th>Master</th><th>SDK</th><th>Contracts</th>' +
      '<th>If it is not ready, the fix</th></tr></thead><tbody>';
    Object.keys(found.segments || {}).forEach(function (key) {
      var row = found.segments[key];
      html += '<tr class="' + (row.ready ? 'ok' : 'warn') + '">';
      html += '<td><b>' + esc(row.label) + '</b> <span class="mono dim">' +
        esc(row.exch_seg) + ' &rarr; ' + esc(row.exchange) + '</span></td>';
      html += '<td class="' + (row.in_master ? 'ok-ink' : 'bad-ink') + '">' +
        (row.in_master ? 'yes' : 'no rows') + '</td>';
      // UNMEASURED IS NOT A FAILURE.
      html += '<td class="' + (row.in_sdk === false ? 'bad-ink'
        : row.in_sdk === true ? 'ok-ink' : 'dim') + '">' +
        (row.in_sdk === false ? 'missing'
          : row.in_sdk === true ? esc(row.exchange) : 'unknown') + '</td>';
      html += '<td class="mono">' +
        (row.contracts === undefined || row.contracts === null ? DASH
          : Number(row.contracts).toLocaleString('en-IN')) + '</td>';
      html += '<td>' + esc(row.ready ? 'ready' : row.note) + '</td>';
      html += '</tr>';
    });
    return html + '</tbody></table>';
  }

  // -- charges --------------------------------------------------------------

  var CHARGE_FIELDS = [
    ['brokerage_per_order', 'Brokerage /order', ''],
    ['brokerage_per_lot', 'Brokerage /lot', ''],
    ['exchange_txn_pct', 'Exchange txn %', 'of turnover'],
    ['sebi_pct', 'SEBI %', 'of turnover'],
    ['stamp_duty_pct_buy', 'Stamp duty %', 'BUY side only'],
    ['gst_pct', 'GST %', 'on brokerage + exchange'],
    ['ctt_pct_sell', 'CTT %', 'SELL side only']
  ];

  function chargesHtml() {
    var charges = local.charges;
    var html = '<h3>Charges <small>from your Arrow contract note, per ' +
      'segment</small></h3>';
    if (!charges) { return html + '<p class="hint">loading&hellip;</p>'; }
    html += '<div class="charges-grid">';
    Object.keys(charges).forEach(function (key) {
      var row = charges[key];
      html += '<div class="charge-block" data-segment="' + esc(key) + '">';
      html += '<div class="charge-head"><b>' + esc(row.label) + '</b>';
      if (!row.configured) {
        html += '<span class="warn-pill">not configured &mdash; every cost ' +
          'on that ladder reads &#8377;0.00, which is an unfilled form and ' +
          'not a free trade</span>';
      }
      html += '</div><div class="charge-fields">';
      CHARGE_FIELDS.forEach(function (entry) {
        html += '<label class="sfield tight"><span>' + esc(entry[1]) +
          (entry[2] ? '<small>' + esc(entry[2]) + '</small>' : '') + '</span>' +
          '<input class="c-' + entry[0] + ' mono" type="number" step="0.0001" ' +
          'value="' + esc(row.rates[entry[0]]) + '"></label>';
      });
      html += '</div></div>';
    });
    html += '</div>';
    html += '<div class="hint">Stamp duty is charged to the BUYER and CTT to ' +
      'the SELLER, so a spread pays a different stack on each leg &mdash; ' +
      'and swaps them coming out. There is no swap: an Indian future pays no ' +
      'overnight financing, and its carry is in the price.</div>';
    html += '<div class="session-actions"><button class="btn save-charges">' +
      'Save charges</button></div>';
    return html;
  }

  // -- the desk-wide tunables ------------------------------------------------

  var TRADING_FIELDS = [
    ['MARKET_PROTECTION_TICKS', 'Slippage protection (ticks)', 'number',
     'A market click is market-WITH-protection: a fill worse than the ' +
     'clicked spread by more than this many increments is refused. ' +
     '<b>Arrow has no deviation parameter, so this is the ONLY slippage ' +
     'guard in the system.</b> 0 turns it off.'],
    ['CONFIRM_MARKET_CLICKS', 'Ask before crossing', 'bool',
     'OFF (default): one click is one order. The arming carries the weight ' +
     'instead — the mode badge, the tinted columns, the cursor.'],
    ['CLICK_AWAY_RESTS', 'A click away from the touch rests', 'bool',
     'A buy under the offer cannot cross at any price, so it becomes a ' +
     'working order there. Off refuses the click instead.'],
    ['MAX_QUOTE_AGE_SEC', 'Stale quote limit (seconds)', 'number',
     'A pair is only as good as its worse leg. A far-month MCX contract ' +
     'trades a few times a minute while the near month ticks constantly, so ' +
     'raise it per ladder rather than here. 0 turns the guard off — it ' +
     'can withhold an order, never a close.'],
    ['REPEG_DEAD_BAND_TICKS', 'Re-peg dead band (ticks)', 'number',
     'LIMIT mode only. Every re-peg loses queue position, so a tight band ' +
     'means never being at the front of a queue — which defeats quoting.'],
    ['TENDER_WARN_DAYS', 'Tender warning (days)', 'number',
     'MCX settles PHYSICALLY. A position carried into the tender period can ' +
     'be assigned for delivery, which involves a warehouse.'],
    ['REFUSE_OPEN_IN_TENDER', 'Refuse to OPEN inside tender', 'bool',
     'Only ever an open. A guard never prevents a close.'],
    ['AUTO_ROUTE_ENABLED', 'AutoRouting master switch', 'bool',
     'OFF, no ladder arms a target however its own box is ticked. One place ' +
     'to stand every automatic order down before a session.'],
    ['ROW_HEIGHT_PX', 'Ladder row height (px)', 'number',
     '17 is the reference screen’s. A bigger target is a faster, safer ' +
     'click on a large monitor.'],
    ['COMMAND_POLL_SEC', 'Click drain (seconds)', 'number',
     'How often the engine picks clicks up, on its own thread. This is the ' +
     'click-to-order latency you feel; the price poll is separate.']
  ];

  function tradingHtml() {
    var settings = local.settings;
    if (!settings) { return '<h3>Trading</h3><p class="hint">loading&hellip;</p>'; }
    var values = settings.settings || {};
    var html = '<h3>Trading <small>what a click does, and how fast</small></h3>' +
      '<div class="trading-fields">';
    TRADING_FIELDS.forEach(function (entry) {
      var name = entry[0], value = values[name];
      html += '<label class="sfield"><span>' + esc(entry[1]) + '</span>';
      if (entry[2] === 'bool') {
        html += '<input class="s-' + name + '" type="checkbox"' +
          (value ? ' checked' : '') + '>';
      } else {
        html += '<input class="s-' + name + ' mono" type="number" step="any" ' +
          'value="' + esc(value) + '">';
      }
      html += '<div class="hint">' + entry[3] + '</div></label>';
    });
    html += '</div><div class="session-actions">' +
      '<button class="btn save-trading">Apply</button>' +
      '<span class="hint">These reach the running engine at once &mdash; no ' +
      'restart.</span></div>';
    return html;
  }

  // -- pairs -----------------------------------------------------------------

  function pairsHtml() {
    var html = '<h3>Pairs <small>each ladder is two contracts; the spread is ' +
      'B &minus; &beta; &times; A</small></h3>';
    html += '<table class="grid-form pairs-table"><thead><tr>' +
      '<th>Ladder</th><th>Leg A</th><th>Leg B</th><th>&beta;</th>' +
      '<th>Lots A/B</th><th>Incr</th><th>Product</th><th>Type</th>' +
      '<th></th></tr></thead><tbody>';
    var keys = Object.keys(local.pairs);
    if (!keys.length) {
      html += '<tr><td colspan="9" class="hint">no pairs yet &mdash; add one ' +
        'below</td></tr>';
    }
    keys.forEach(function (key) {
      var pair = local.pairs[key] || {};
      var legA = pair.leg_a || {}, legB = pair.leg_b || {};
      html += '<tr data-pair="' + esc(key) + '">';
      html += '<td><b>' + esc(pair.name || key) + '</b></td>';
      html += '<td class="mono">' + esc(legA.symbol) + '<div class="dim">' +
        esc(legA.segment) + '</div></td>';
      html += '<td class="mono">' + esc(legB.symbol) + '<div class="dim">' +
        esc(legB.segment) + '</div></td>';
      html += '<td class="mono">' + esc(pair.hedge_ratio) + '</td>';
      html += '<td class="mono">' + esc(pair.clip_lots_a) + ' / ' +
        esc(pair.clip_lots_b) + '</td>';
      html += '<td class="mono">' + (pair.increment === null ||
        pair.increment === undefined ? 'derived' : esc(pair.increment)) + '</td>';
      html += '<td>' + esc(pair.product || 'NRML') + '</td>';
      html += '<td>' + esc(pair.pair_type || '') + '</td>';
      html += '<td><button class="btn roll-pair" data-pair="' + esc(key) +
        '" title="MCX contracts expire every month or two; a calendar has to ' +
        'be re-pointed">Roll</button> ' +
        '<button class="btn drop-pair" data-pair="' + esc(key) +
        '">Delete</button></td>';
      html += '</tr>';
    });
    html += '</tbody></table>';

    if (local.editing === null) {
      html += '<div class="session-actions"><button class="btn primary ' +
        'new-pair">New pair</button></div>';
    } else {
      html += newPairHtml();
    }
    return html;
  }

  function newPairHtml() {
    var draft = local.draft || {};
    var html = '<div class="pair-form"><div class="pair-form-head"><b>New pair' +
      '</b></div><div class="pair-legs">';
    ['a', 'b'].forEach(function (leg) {
      html += legPickerHtml(leg, draft);
    });
    html += '</div>';

    html += '<div class="session-actions">' +
      '<button class="btn primary derive-pair">Read both legs from the ' +
      'instrument master</button>' +
      '<span class="hint">Nothing below is applied until you Save.</span>' +
      '</div>';

    if (local.derived) { html += derivedHtml(local.derived); }

    html += '<div class="field-row">';
    html += '<label class="sfield"><span>Lots A per Qty</span>' +
      '<input class="d-lots-a mono" type="number" step="1" value="' +
      esc(draft.clip_lots_a || 1) + '"></label>';
    html += '<label class="sfield"><span>Lots B per Qty</span>' +
      '<input class="d-lots-b mono" type="number" step="1" value="' +
      esc(draft.clip_lots_b || 1) + '"><div class="hint">Both typed. Nothing ' +
      'derives leg B — GOLD vs GOLDM is 1 and 10.</div></label>';
    html += '<label class="sfield"><span>Pair type</span>' +
      '<select class="d-pair-type">' +
      option('FUTURE_FUTURE', 'Calendar (same underlying)', draft.pair_type) +
      option('SPOT_FUTURE', 'Spot vs future', draft.pair_type) +
      option('RELATED', 'Related (no fair spread)', draft.pair_type) +
      '</select></label>';
    html += '<label class="sfield"><span>Product</span>' +
      '<select class="d-product">' +
      option('NRML', 'NRML (carry)', draft.product) +
      option('MIS', 'MIS (intraday)', draft.product) +
      '</select><div class="hint">MIS is squared off by the broker near the ' +
      'close, without asking. A spread half-squared-off is an outright.</div>' +
      '</label>';
    html += '</div>';

    html += '<div class="session-actions">' +
      '<button class="btn primary save-pair">Save pair</button>' +
      '<button class="btn cancel-pair">Cancel</button>' +
      '<span class="hint">The key is the two trading symbols. Two GOLD ' +
      'calendars a month apart are different ladders with different ' +
      'positions.</span></div>';
    return html + '</div>';
  }

  function legPickerHtml(leg, draft) {
    var picker = local.picker[leg] || {};
    var html = '<div class="pair-leg" data-leg="' + leg + '">' +
      '<div class="leg-head leg-' + leg + '">Leg ' + leg.toUpperCase() +
      '</div>';

    html += '<label class="sfield"><span>Segment</span>' +
      '<select class="p-segment">' + segmentOptions(picker.segment) +
      '</select></label>';
    html += '<label class="sfield"><span>Search</span>' +
      '<input class="p-search" placeholder="GOLD, SILVER, CRUDEOIL…" ' +
      'value="' + esc(picker.query) + '"></label>';
    html += '<label class="sfield"><span>Contract <i>oldest expiry first</i>' +
      '</span><select class="p-symbol mono">' +
      contractOptions(picker, draft['symbol_' + leg]) +
      '</select></label>';

    var spec = (local.derived && local.derived.legs &&
                local.derived.legs[leg]) || null;
    if (spec && spec.found) {
      html += '<table class="spec"><tbody>' +
        specRow('Lot size', spec.lot_size, 'units/lot') +
        specRow('Tick size', spec.tick_size, '') +
        specRow('Expiry', spec.expiry, spec.days_to_expiry === null ||
                spec.days_to_expiry === undefined ? ''
                : '(' + spec.days_to_expiry + 'd)') +
        specRow('Freeze qty', spec.freeze_qty, 'units') +
        specRow('Token', spec.token, '') +
        '</tbody></table>';
      (spec.problems || []).forEach(function (problem) {
        html += '<div class="spec-problem">' + esc(problem) + '</div>';
      });
    } else if (spec) {
      html += '<div class="spec-problem">' + esc(spec.error) + '</div>';
    } else {
      html += '<div class="hint">Lot size, tick size, expiry, freeze quantity ' +
        'and token are READ from the master — never typed. Lot size ' +
        'varies per expiry.</div>';
    }
    return html + '</div>';
  }

  function specRow(label, value, unit) {
    var shown = (value === null || value === undefined || value === '')
      ? '<span class="dim">' + DASH + '</span>'
      : '<b>' + esc(value) + '</b>' + (unit ? ' ' + esc(unit) : '');
    return '<tr><td>' + esc(label) + '</td><td class="mono">' + shown +
      '</td></tr>';
  }

  function derivedHtml(derived) {
    var html = '<table class="grid-form derived-table"><thead><tr>' +
      '<th>Derived</th><th>Value</th><th>From</th></tr></thead><tbody>';
    var rows = [
      ['Increment', derived.increment, derived.increment_note],
      ['&beta; (hedge ratio)', derived.hedge_ratio,
       'stamped for ' + esc(derived.hedge_ratio_for || '')],
      ['Units on the wire', derived.units_note, derived.lot_note]
    ];
    rows.forEach(function (row) {
      if (row[1] === undefined) { return; }
      html += '<tr><td>' + row[0] + '</td><td class="mono"><b>' +
        (row[1] === null ? DASH : esc(row[1])) + '</b></td><td class="dim">' +
        esc(row[2] || '') + '</td></tr>';
    });
    html += '</tbody></table>';
    (derived.problems || []).forEach(function (problem) {
      if (problem) {
        html += '<div class="spec-problem">' + esc(problem) + '</div>';
      }
    });
    return html;
  }

  function segmentOptions(chosen) {
    var found = (local.segments && local.segments.segments) || {};
    var html = '';
    Object.keys(found).forEach(function (key) {
      if (!found[key].ready) { return; }
      html += option(key, found[key].label, chosen);
    });
    return html || option('mcx_fo', 'MCX futures', chosen);
  }

  function contractOptions(picker, chosen) {
    var contracts = picker.contracts;
    if (!contracts || !contracts.length) {
      // FOUR DIFFERENT STATES, and they had one sentence between them.
      // "search to list contracts" was shown when nothing had been
      // typed, while a search was in flight, when the master held no
      // match, AND when the search failed because there is no Arrow
      // session — so a broken connection looked exactly like an empty
      // search box.
      return '<option value="">' + esc(
        picker.error ? picker.error
          : picker.busy ? 'searching\u2026'
          : !picker.query ? 'type a name above to list contracts'
          : 'no contract in the master matches "' + picker.query + '"'
      ) + '</option>';
    }
    return contracts.map(function (row) {
      return option(row.trading_symbol,
                    row.trading_symbol + '  ' + (row.expiry || ''), chosen);
    }).join('');
  }

  function option(value, label, chosen) {
    return '<option value="' + esc(value) + '"' +
      (String(chosen) === String(value) ? ' selected' : '') + '>' +
      esc(label) + '</option>';
  }

  // -- events -----------------------------------------------------------------

  function onClick(event) {
    var target = event.target;
    if (target.closest('.connect')) { return doConnect(); }
    if (target.closest('.save-session')) { return saveSession(); }
    if (target.closest('.save-charges')) { return saveCharges(); }
    if (target.closest('.save-trading')) { return saveTrading(); }
    if (target.closest('.new-pair')) {
      local.editing = '';
      local.draft = {clip_lots_a: 1, clip_lots_b: 1,
                     pair_type: 'FUTURE_FUTURE', product: 'NRML'};
      local.derived = null;
      return render(true);
    }
    if (target.closest('.cancel-pair')) {
      local.editing = null;
      local.derived = null;
      return render(true);
    }
    if (target.closest('.derive-pair')) { return derivePair(); }
    if (target.closest('.save-pair')) { return savePair(); }
    var drop = target.closest('.drop-pair');
    if (drop) { return dropPair(drop.dataset.pair); }
    var roll = target.closest('.roll-pair');
    if (roll) { return rollPair(roll.dataset.pair); }
  }

  function onChange(event) {
    var target = event.target;
    if (target.classList.contains('p-segment')) {
      return searchLeg(target.closest('.pair-leg').dataset.leg);
    }
    if (target.classList.contains('p-symbol')) {
      var leg = target.closest('.pair-leg').dataset.leg;
      local.draft['symbol_' + leg] = target.value;
    }
  }

  //: The search runs as the operator types, so it is debounced: one
  //: request per pause, not one per keystroke. Each of those requests
  //: can open an Arrow session, and hammering a broker's login is how
  //: an account gets rate-limited.
  var SEARCH_DEBOUNCE_MS = 250;
  var searchTimers = {};

  function onInput(event) {
    var target = event.target;
    if (!target.classList.contains('p-search')) { return; }
    var leg = target.closest('.pair-leg').dataset.leg;
    clearTimeout(searchTimers[leg]);
    searchTimers[leg] = setTimeout(function () {
      searchLeg(leg);
    }, SEARCH_DEBOUNCE_MS);
  }

  function panelValue(selector) {
    var found = document.querySelector('.window.settings ' + selector);
    if (!found) { return undefined; }
    return found.type === 'checkbox' ? found.checked : found.value;
  }

  function searchLeg(leg) {
    var box = document.querySelector('.pair-leg[data-leg="' + leg + '"]');
    if (!box) { return; }
    var segment = box.querySelector('.p-segment').value;
    var query = box.querySelector('.p-search').value;
    var mine = (local.picker[leg] = {
      segment: segment, query: query, contracts: [], busy: !!query,
      error: null, at: Date.now()
    });
    if (!query) { return paintContracts(leg); }
    paintContracts(leg);
    get('/api/find?q=' + encodeURIComponent(query) + '&segment=' +
        encodeURIComponent(segment)).then(function (result) {
      // A slower earlier search must not overwrite a later one — the
      // operator would be looking at the contracts for a prefix they
      // have already finished typing past.
      if (local.picker[leg] !== mine) { return; }
      var body = result.body || {};
      mine.busy = false;
      mine.contracts = body.symbols || [];
      // The BROKER'S OWN WORDS, in the dropdown where the contracts
      // would have been — not only in a toast that clears itself.
      mine.error = body.ok ? null : (body.error || 'the search failed');
      if (mine.error) { UI.toast(mine.error); }
      paintContracts(leg);
    });
  }

  //: Repaint ONE dropdown, in place.
  //:
  //: `render(true)` rewrites the whole Pairs section, which contains
  //: the search box the operator is typing into: the input is
  //: replaced, the caret is lost, and the next keystroke goes nowhere.
  //: The search cannot use it.
  function paintContracts(leg) {
    var box = document.querySelector('.pair-leg[data-leg="' + leg + '"]');
    if (!box) { return; }
    var select = box.querySelector('.p-symbol');
    if (!select) { return; }
    var html = contractOptions(local.picker[leg] || {},
                               local.draft['symbol_' + leg]);
    if (select.innerHTML !== html) { select.innerHTML = html; }
  }

  function doConnect() {
    UI.toast('connecting to Arrow…', 'ok');
    post('/api/connect').then(function (result) {
      local.connection = result.body;
      // The broker's own words, on the panel, not in a log.
      UI.toast(result.body.ok ? 'Arrow session live' : result.body.error,
               result.body.ok ? 'ok' : undefined);
      refresh();
    });
  }

  function saveSession() {
    var payload = {
      app_id: panelValue('.f-app-id'),
      user_id: panelValue('.f-user-id'),
      dedicated: panelValue('.f-dedicated')
    };
    // A secret is only sent when it was TYPED. An empty box means
    // "leave what is in .env alone", never "clear it".
    ['password:.f-password', 'api_secret:.f-api-secret',
     'totp_secret:.f-totp'].forEach(function (entry) {
      var parts = entry.split(':');
      var typed = panelValue(parts[1]);
      if (typed) { payload[parts[0]] = typed; }
    });
    post('/api/account', payload).then(function (result) {
      if (!result.body.ok) { return UI.toast(result.body.error); }
      UI.toast('saved — the engine reads the session at startup, so it ' +
               'restarts to pick this up', 'ok');
      refresh();
    });
  }

  function saveCharges() {
    var table = {};
    document.querySelectorAll('.window.settings .charge-block')
      .forEach(function (block) {
        var rates = {};
        CHARGE_FIELDS.forEach(function (entry) {
          var input = block.querySelector('.c-' + entry[0]);
          if (input) { rates[entry[0]] = parseFloat(input.value) || 0; }
        });
        table[block.dataset.segment] = rates;
      });
    post('/api/settings', {fields: {CHARGES: table}}).then(function (result) {
      UI.toast(result.body.ok ? 'charges saved' : result.body.error,
               result.body.ok ? 'ok' : undefined);
      refresh();
    });
  }

  function saveTrading() {
    var fields = {};
    TRADING_FIELDS.forEach(function (entry) {
      var value = panelValue('.s-' + entry[0]);
      if (value === undefined) { return; }
      fields[entry[0]] = entry[2] === 'bool' ? !!value : parseFloat(value);
    });
    post('/api/settings', {fields: fields}).then(function (result) {
      if (!result.body.ok) { return UI.toast(result.body.error); }
      var cold = result.body.restart_required || [];
      UI.toast(cold.length ? 'saved — ' + cold.join(', ') +
               ' need a restart' : 'applied to the running engine', 'ok');
      refresh();
    });
  }

  function derivePair() {
    var payload = {
      symbol_a: local.draft.symbol_a || panelValue('.pair-leg[data-leg="a"] .p-symbol'),
      symbol_b: local.draft.symbol_b || panelValue('.pair-leg[data-leg="b"] .p-symbol'),
      hedge_ratio: 1.0
    };
    local.draft.symbol_a = payload.symbol_a;
    local.draft.symbol_b = payload.symbol_b;
    post('/api/pair/derive', payload).then(function (result) {
      local.derived = result.body;
      if (!result.body.ok) { UI.toast(result.body.error); }
      render(true);
    });
  }

  function savePair() {
    var draft = local.draft;
    var legA = document.querySelector('.pair-leg[data-leg="a"]');
    var legB = document.querySelector('.pair-leg[data-leg="b"]');
    var symbolA = draft.symbol_a || (legA && legA.querySelector('.p-symbol').value);
    var symbolB = draft.symbol_b || (legB && legB.querySelector('.p-symbol').value);
    if (!symbolA || !symbolB) {
      return UI.toast('pick a contract for both legs first');
    }
    var key = symbolA + '|' + symbolB;
    var payload = {
      name: symbolA + ' / ' + symbolB,
      leg_a: {account: 'arrow', symbol: symbolA,
              segment: legA.querySelector('.p-segment').value},
      leg_b: {account: 'arrow', symbol: symbolB,
              segment: legB.querySelector('.p-segment').value},
      hedge_ratio: (local.derived && local.derived.hedge_ratio) || 1.0,
      hedge_ratio_for: key,
      increment: (local.derived && local.derived.increment) || null,
      clip_lots_a: parseFloat(panelValue('.d-lots-a')) || 1,
      clip_lots_b: parseFloat(panelValue('.d-lots-b')) || 1,
      pair_type: panelValue('.d-pair-type'),
      product: panelValue('.d-product'),
      enabled: true
    };
    post('/api/pairs/' + encodeURIComponent(key), payload)
      .then(function (result) {
        if (!result.body.ok) { return UI.toast(result.body.error); }
        UI.toast('saved ' + key + ' — contracts and β are read at ' +
                 'startup, so the engine restarts to pick them up', 'ok');
        local.editing = null;
        local.derived = null;
        refresh();
      });
  }

  function dropPair(key) {
    UI.ask('Delete this ladder?', key + ' — the ladder goes and its ' +
           'configuration with it. Any POSITION it holds stays at the ' +
           'exchange and the reconciler will report it as unexplained.',
           'Delete', function () {
      post('/api/pairs/' + encodeURIComponent(key), {}, 'DELETE')
        .then(function (result) {
          UI.toast(result.body.ok ? 'deleted ' + key : result.body.error,
                   result.body.ok ? 'ok' : undefined);
          refresh();
        });
    });
  }

  function rollPair(key) {
    var pair = local.pairs[key] || {};
    var symbol = (pair.leg_a || {}).symbol;
    get('/api/roll/' + encodeURIComponent(symbol)).then(function (result) {
      var next = (result.body || {}).next || [];
      if (!next.length) {
        return UI.toast('the master lists no later contract on that ' +
                        'underlying');
      }
      UI.toast('next on ' + symbol + ': ' +
               next.map(function (row) { return row.trading_symbol; })
                 .join(', ') + ' — add a new pair on them', 'ok');
    });
  }

  window.ArrowSettings = {render: function () { node(); render(true); },
                          refresh: refresh, state: local};
})();
