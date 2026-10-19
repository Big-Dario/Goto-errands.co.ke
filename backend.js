/* SwiftRun - backend connector (drop-in). Load LAST, after categories.js and performance.js.
   Replaces the in-memory demo data with the real API: accounts, orders, M-Pesa, admin, riders.
   It finds the backend by itself: the server the page came from, or http://127.0.0.1:8000 when the page
   is opened from another local server (e.g. VS Code Live Server). To force an address, set
   window.SWIFTRUN_API = 'http://host:port' before this file. */
(function () {
  'use strict';
  var API = (window.SWIFTRUN_API || '').replace(/\/$/, '');
  var apiReady = null;
  function probe(base) {
    return fetch(base + '/api/health').then(function (r) { return r.ok ? r.json() : null; }).then(function (j) { return !!(j && j.ok); }, function () { return false; });
  }
  function findApi() {
    if (apiReady) return apiReady;
    if (/YOUR-BACKEND/.test(window.SWIFTRUN_API || ''))
      return Promise.reject(new Error('Backend address not set yet. Deploy the backend, then put its address in config.js (BACKEND_URL).'));
    if (window.SWIFTRUN_API) return (apiReady = Promise.resolve());
    if (location.protocol === 'file:')
      return (apiReady = Promise.reject(new Error('Open the site at http://127.0.0.1:8000 (run python3 app.py in the backend folder), not as a file.')));
    apiReady = probe('').then(function (ok) {
      if (ok) { API = ''; return; }
      if (/^(localhost|127\.0\.0\.1)$/.test(location.hostname)) {
        return probe('http://127.0.0.1:8000').then(function (ok2) {
          if (ok2) { API = 'http://127.0.0.1:8000'; return; }
          throw new Error('Backend not running. Start it with python3 app.py in the backend folder.');
        });
      }
      throw new Error("This page isn't served by the SwiftRun backend. Open the backend's address, or set window.SWIFTRUN_API.");
    });
    apiReady.catch(function () { apiReady = null; });   // retry next time
    return apiReady;
  }
  function $(id) { return document.getElementById(id); }

  /* ── storage (guarded: some browsers block it) ─────────────── */
  function sget(store, k) { try { return window[store].getItem(k); } catch (e) { return null; } }
  function sset(store, k, v) { try { if (v == null) window[store].removeItem(k); else window[store].setItem(k, v); } catch (e) {} }
  var token = sget('localStorage', 'sr_token');

  /* ── HTML safety: the existing UI builds pages with innerHTML, so escape
        everything that comes from the server (customer-typed text included) ── */
  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function clean(v) {
    if (typeof v === 'string') return v.replace(/[&<>"']/g, function (c) { return ESC[c]; });
    if (Array.isArray(v)) return v.map(clean);
    if (v && typeof v === 'object') { var o = {}; for (var k in v) o[k] = clean(v[k]); return o; }
    return v;
  }

  /* ── API helper ────────────────────────────────────────────── */
  function api(path, opts) {
    opts = opts || {};
    var headers = { 'Content-Type': 'application/json' };
    if (token) headers.Authorization = 'Bearer ' + token;
    return findApi().then(function () {
      return fetch(API + path, {
        method: opts.method || 'GET', headers: headers,
        body: opts.body ? JSON.stringify(opts.body) : undefined
      }).then(function (r) { return r; }, function () { throw new Error("Can't reach the server. Check your connection and try again."); });
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (r.ok) return j;
        if (r.status === 401 && token && path.indexOf('/api/auth/') !== 0) { dropSession(); j.error = 'Your session expired. Please log in again.'; }
        throw new Error(j.error || 'Request failed (' + r.status + ')');
      });
    });
  }
  function fail(e) { showToast(e.message || 'Something went wrong', 'error'); }
  function lock(id, on) { var b = $(id); if (b) { b.disabled = !!on; b.style.opacity = on ? '.6' : ''; } }

  /* ── session ───────────────────────────────────────────────── */
  function setSession(res) {
    token = res.token; sset('localStorage', 'sr_token', token);
    DB.currentUser = res.user; updateUserUI();
    if (isAdmin()) startPulse();
  }
  function dropSession() {
    token = null; sset('localStorage', 'sr_token', null); sset('sessionStorage', 'sr_lookup', null);
    DB.currentUser = null; DB.orders = []; updateUserUI(); stopPulse();
  }
  function isAdmin() { return !!(DB.currentUser && DB.currentUser.role === 'admin'); }

  /* ── data sync ─────────────────────────────────────────────── */
  function setOrders(list) { DB.orders = clean(list || []); }
  function upsert(o) {
    o = clean(o);
    var i = DB.orders.findIndex(function (x) { return x.id === o.id; });
    if (i >= 0) DB.orders[i] = o; else DB.orders.unshift(o);
  }
  function refreshOrders() {
    var p;
    if (token) p = api('/api/orders').then(function (r) { return r.orders; });
    else if (sget('sessionStorage', 'sr_lookup')) p = api('/api/orders/track?q=' + encodeURIComponent(sget('sessionStorage', 'sr_lookup'))).then(function (r) { return r.orders; });
    else p = Promise.resolve([]);
    return p.then(function (list) { setOrders(list); renderTrack(); updateAdminBadge(); }, function () { renderTrack(); });
  }
  function refreshAdmin() {
    return Promise.all([api('/api/admin/orders'), api('/api/admin/riders'), api('/api/admin/payments')]).then(function (r) {
      DB.orders = clean(r[0].orders); setRiders(r[1].riders); DB.payments = clean(r[2].payments);
      initAdmin();
    }, fail);
  }

  // Start clean: drop the demo data baked into index.html
  DB.orders = []; DB.riders = []; DB.payments = [];

  /* ── auth ──────────────────────────────────────────────────── */
  window.handleLogin = function () {
    var email = $('lEmail').value.trim(), pass = $('lPass').value;
    if (!email || !pass) { showToast('Please fill in all fields', 'error'); return; }
    lock('loginSubmitBtn', true);
    api('/api/auth/login', { method: 'POST', body: { email: email, password: pass } }).then(function (r) {
      setSession(r); $('lPass').value = '';
      showToast('Welcome back! 👋', 'success');
      replaceNext = true; setPage(isAdmin() ? 'admin' : 'home');
    }, fail).then(function () { lock('loginSubmitBtn', false); });
  };
  window.handleRegister = function () {
    var first = $('rFirst').value.trim(), last = $('rLast').value.trim(),
        email = $('rRegEmail').value.trim(), pass = $('rRegPass').value;
    if (!first || !last || !email || !pass) { showToast('Please fill in all fields', 'error'); return; }
    if (pass.length < 8) { showToast('Password must be at least 8 characters', 'error'); return; }
    lock('registerSubmitBtn', true);
    api('/api/auth/register', { method: 'POST', body: { first: first, last: last, email: email, password: pass } }).then(function (r) {
      setSession(r); $('rRegPass').value = '';
      showToast('Welcome, ' + first + '! Account created 🎉', 'success');
      replaceNext = true; setPage('home');
    }, fail).then(function () { lock('registerSubmitBtn', false); });
  };
  window.logout = function () { dropSession(); renderTrack(); showToast('Logged out', 'default'); };

  /* ── page addresses: #/about, #/track ... so Back works and links can be shared ── */
  var prevSetPage = window.setPage;
  var ROUTES = ['home', 'request', 'track', 'about', 'login', 'register', 'admin'];
  var HOME_TITLE = document.title;
  var PAGE_TITLES = { request: 'Request an Errand', track: 'Track Your Order', about: 'About Us',
                      login: 'Log in', register: 'Create an Account', admin: 'Admin Dashboard' };
  var currentPage = 'home', replaceNext = false;

  function routeFromHash() {
    var m = /^#\/([a-z]*)\/?$/.exec(location.hash);
    if (!m) return null;                       // not a page address (a #reset= link, for example)
    return m[1] === '' ? 'home' : (ROUTES.indexOf(m[1]) >= 0 ? m[1] : 'home');
  }
  function hashFor(page) { return page === 'home' ? '#/' : '#/' + page; }
  function writeHash(page, replace) {
    if (ROUTES.indexOf(page) < 0) return;
    if (routeFromHash() === page || (page === 'home' && (location.hash === '' || location.hash === '#'))) return;
    if (replace) {
      try { history.replaceState(null, '', location.pathname + location.search + hashFor(page)); } catch (e) { location.hash = hashFor(page); }
    } else location.hash = hashFor(page);      // adds a history entry, so Back returns to the previous page
  }
  // Shows a page (the admin page is guarded) and says which page was really shown
  function showPage(page) {
    var guarded = false;
    if (page === 'admin' && !isAdmin()) {
      showToast(token ? 'Admin access only' : 'Please log in with an admin account', 'error');
      page = token ? 'home' : 'login'; guarded = true;
    }
    prevSetPage.call(window, page);
    currentPage = page;
    document.title = PAGE_TITLES[page] ? PAGE_TITLES[page] + ' — SwiftRun' : HOME_TITLE;
    titleBase = document.title;
    if (page === 'track') refreshOrders();
    if (page === 'admin') refreshAdmin();
    return { page: page, guarded: guarded };
  }
  window.setPage = function (page) {
    var r = showPage(page);
    writeHash(r.page, r.guarded || replaceNext);   // a bounced or post-login move replaces the entry instead of adding one
    replaceNext = false;
  };
  window.addEventListener('hashchange', function () {    // Back, Forward, or a link someone pasted
    var want = routeFromHash();
    if (want === null && (location.hash === '' || location.hash === '#')) want = 'home';   // the plain address means Home
    if (want && want !== currentPage) {
      var shown = showPage(want);
      if (shown.page !== want) writeHash(shown.page, true);
    }
  });

  /* ── pricing: the server decides the price ─────────────────── */
  function chosenService() { var c = document.querySelector('.svc-card.selected'); return c ? c.dataset.svc : null; }
  var prevCalc = window.calculateCost;
  window.calculateCost = function () {
    var ready = ['rName', 'rPhone', 'rDesc', 'rPickup', 'rDropoff'].every(function (id) { return $(id).value.trim(); });
    var rnd = Math.random; Math.random = function () { return 0; };   // hide the demo's random fee
    try { prevCalc.apply(this, arguments); } finally { Math.random = rnd; }
    if (!ready || $('costPanel').style.display !== 'block') return;
    lock('calcBtn', true);
    api('/api/quote', { method: 'POST', body: { service: chosenService() || undefined, type: $('rType').value, goodsValue: parseFloat($('rValue').value) || 0 } })
      .then(function (q) {
        $('cDistance').textContent = 'KES ' + q.distFee.toLocaleString();
        $('cGoods').textContent = 'KES ' + q.goodsFee.toLocaleString();
        $('cTotal').textContent = 'KES ' + q.total.toLocaleString();
      }, function (e) { $('costPanel').style.display = 'none'; fail(e); })
      .then(function () { lock('calcBtn', false); });
  };

  /* ── place an order ────────────────────────────────────────── */
  window.submitOrder = function () {
    var method = DB.selectedPay;
    if ($('costPanel').style.display !== 'block') { showToast('Please calculate the cost first', 'error'); return; }
    if (method === 'card') { showToast('Card payments are not available yet. Please choose M-Pesa or cash.', 'error'); return; }
    var mp = $('mpesaPhone').value.trim();
    if (method === 'mpesa' && !mp) { showToast('Enter your M-Pesa phone number', 'error'); return; }
    lock('confirmPayBtn', true);
    api('/api/orders', { method: 'POST', body: {
      name: $('rName').value.trim(), phone: $('rPhone').value.trim(), email: $('rEmail').value.trim(),
      desc: $('rDesc').value.trim(), pickup: $('rPickup').value.trim(), dropoff: $('rDropoff').value.trim(),
      goodsValue: parseFloat($('rValue').value) || 0, notes: $('rNotes').value.trim(),
      type: $('rType').value, service: chosenService() || undefined, payMethod: method
    } }).then(function (r) {
      var o = r.order;
      upsert(o); updateAdminBadge();
      if (!token) sset('sessionStorage', 'sr_lookup', o.customer.phone);
      if (method === 'mpesa') showPayModal(o.id, mp, o.total);
      else {
        showToast('Order ' + o.id + ' confirmed! Rider will collect cash 🚴', 'success');
        resetForm(); setPage('track');
      }
    }, fail).then(function () { lock('confirmPayBtn', false); });
  };

  /* ── real M-Pesa payment ───────────────────────────────────── */
  var payRun = 0;
  var prevClosePay = window.closePayModal;
  window.closePayModal = function () { payRun++; return prevClosePay.apply(this, arguments); };

  window.showPayModal = function (orderId, phone, amount) {
    var run = ++payRun, box = $('payModalContent');
    function steps(n) {
      var names = ['STK Push sent to ' + phone, 'Waiting for customer PIN...', 'Processing payment...', 'Order confirmed'];
      return '<div class="pay-steps">' + names.map(function (s, i) {
        return '<div class="pay-step' + (i < n ? ' done' : '') + '"><div class="ps-dot"></div>' + s + '</div>';
      }).join('') + '</div>';
    }
    function waiting(n) {
      box.innerHTML = '<div class="pay-spinner"></div><div class="pay-modal-title">Waiting for M-Pesa PIN</div>' +
        '<div class="pay-modal-sub">A payment prompt has been sent to:</div><div class="pay-phone-big">' + clean(phone) + '</div>' + steps(n) +
        '<p style="font-size:.8rem;color:var(--muted)">Enter your M-Pesa PIN on your phone to pay <strong style="color:var(--text)">KES ' + amount.toLocaleString() + '</strong></p>';
    }
    function problem(title, msg, retry) {
      box.innerHTML = '<div class="pay-success-icon">⚠️</div><div class="pay-modal-title" style="color:var(--orange)">' + title + '</div>' +
        '<div class="pay-modal-sub">' + clean(msg) + '</div>' +
        (retry ? '<button class="btn-confirm-pay" id="payRetryBtn" style="margin-top:8px">Try again</button>' : '') +
        '<button class="btn-outline" id="payLaterBtn" style="margin-top:8px;width:100%">' + (retry ? 'Pay later' : 'View my orders') + '</button>';
      var rb = $('payRetryBtn'); if (rb) rb.addEventListener('click', function () { showPayModal(orderId, phone, amount); });
      $('payLaterBtn').addEventListener('click', function () { closePayModal(); resetForm(); setPage('track'); });
    }
    function success(ref) {
      box.innerHTML = '<div class="pay-success-icon">✅</div><div class="pay-modal-title" style="color:var(--green)">Payment Successful!</div>' +
        '<div class="pay-modal-sub">KES ' + amount.toLocaleString() + ' paid via M-Pesa.<br/>A rider has been assigned to your errand.</div>' +
        '<div class="pay-ref">Ref: ' + clean(ref || '—') + '</div>' +
        '<button class="btn-confirm-pay" id="paySuccessBtn" style="margin-top:8px">View My Orders →</button>';
      $('paySuccessBtn').addEventListener('click', function () { closePayModal(); resetForm(); setPage('track'); });
    }

    waiting(1);
    $('payModal').style.display = 'flex';
    api('/api/orders/' + encodeURIComponent(orderId) + '/pay/mpesa', { method: 'POST', body: { phone: phone } }).then(function () {
      if (run !== payRun) return;
      waiting(2);
      var started = Date.now();
      (function poll() {
        setTimeout(function () {
          if (run !== payRun) return;
          api('/api/orders/' + encodeURIComponent(orderId) + '/payment').then(function (s) {
            if (run !== payRun) return;
            if (s.state === 'completed') {
              waiting(3);
              api('/api/orders/track?q=' + encodeURIComponent(orderId)).then(function (r) { if (r.orders[0]) upsert(r.orders[0]); updateAdminBadge(); }).catch(function () {});
              setTimeout(function () { if (run === payRun) success(s.mpesaRef); }, 700);
            } else if (s.state === 'failed' || s.state === 'cancelled') {
              problem('Payment not completed', s.message || 'The M-Pesa request was cancelled or declined.', true);
            } else if (Date.now() - started > 120000) {
              problem('Still waiting', 'We have not received a confirmation yet. If you already paid, your order will update shortly. Check My Orders.', false);
            } else poll();
          }, function () { if (run === payRun) poll(); });
        }, 2500);
      })();
    }, function (e) { if (run === payRun) problem("Couldn't send the M-Pesa prompt", e.message, true); });
  };

  /* ── track page search ─────────────────────────────────────── */
  window.searchOrders = function () {
    var q = ($('trackQ').value || '').trim();
    if (!q) { refreshOrders(); return; }
    lock('searchBtn', true);
    api('/api/orders/track?q=' + encodeURIComponent(q)).then(function (r) {
      setOrders(r.orders);
      if (!token && r.orders.length) sset('sessionStorage', 'sr_lookup', q);
      if (!r.orders.length) {
        $('trackResults').innerHTML = '<div class="empty-state"><h3>No orders found</h3><p>Try a different phone number, email or order ID.</p></div>';
      } else renderTrack();
    }, fail).then(function () { lock('searchBtn', false); });
  };

  /* ── actions on the order modal and the admin table ────────── */
  var modalId = null, prevOpen = window.openOrderModal;
  window.openOrderModal = function (id) { modalId = id; return prevOpen.apply(this, arguments); };

  var ADMIN_MSG = {
    confirm: ['accepted ✓', 'success'], dispatch: ['dispatched 🚴', 'success'],
    complete: ['completed ✅', 'success'], cancel: ['cancelled', 'default']
  };
  document.addEventListener('click', function (e) {
    var cancel = e.target.closest && e.target.closest('#mdlCancelBtn');
    if (cancel) {
      e.stopPropagation(); e.preventDefault();
      var id = modalId, o = DB.orders.find(function (x) { return x.id === id; });
      if (!o) return;
      lock('mdlCancelBtn', true);
      api('/api/orders/' + encodeURIComponent(id) + '/cancel', { method: 'POST', body: { phone: o.customer.phone } }).then(function (r) {
        upsert(r.order); closeOrderModal(); renderTrack(); updateAdminBadge(); showToast('Order ' + id + ' cancelled');
      }, function (err) { lock('mdlCancelBtn', false); fail(err); });
      return;
    }
    var btn = e.target.closest && e.target.closest('.adm-action');
    if (btn) {
      e.stopPropagation(); e.preventDefault();
      var oid = btn.dataset.id, act = btn.dataset.action;
      btn.disabled = true;
      api('/api/admin/orders/' + encodeURIComponent(oid), { method: 'PATCH', body: { action: act } }).then(function (r) {
        upsert(r.order);
        showToast('Order ' + oid + ' ' + ADMIN_MSG[act][0], ADMIN_MSG[act][1]);
        updateAdminBadge(); renderAdmOverview(); renderAdmOrders();
        api('/api/admin/riders').then(function (x) { setRiders(x.riders); renderAdmRiders(); }).catch(function () {});
        api('/api/admin/payments').then(function (x) { DB.payments = clean(x.payments); renderAdmPayments(); }).catch(function () {});
      }, function (err) { btn.disabled = false; fail(err); });
    }
  }, true);


  /* ── riders: add / edit / activate / delete ────────────────── */
  var rawRiders = [];
  function setRiders(list) { rawRiders = list || []; DB.riders = clean(rawRiders); }
  var VEHICLES = ['Motorbike', 'Bicycle', 'Tuk-tuk', 'Car', 'Van'];

  window.renderAdmRiders = function () {
    var el = $('adm-riders'); if (!el) return;
    var active = DB.riders.filter(function (r) { return r.status === 'active'; }).length;
    var rows = DB.riders.map(function (r) {
      var on = r.status === 'active';
      return '<tr>' +
        '<td style="font-family:monospace;color:var(--muted);font-size:.8rem">' + r.id + '</td>' +
        '<td><strong>' + r.name + '</strong></td>' +
        '<td style="font-size:.82rem"><a href="tel:' + r.phone + '" style="color:var(--green)">' + r.phone + '</a></td>' +
        '<td style="font-size:.82rem">' + r.vehicle + (r.plate ? ' · ' + r.plate : '') + '</td>' +
        '<td style="font-size:.82rem;color:var(--muted)">' + (r.area || '—') + '</td>' +
        '<td style="color:var(--orange)">★ ' + r.rating + '</td>' +
        '<td>' + r.trips + '</td>' +
        '<td style="color:var(--green)">' + r.earnings + '</td>' +
        '<td><span style="font-size:.75rem;color:' + (on ? 'var(--green)' : 'var(--muted)') + '">' + r.status + '</span></td>' +
        '<td style="white-space:nowrap">' +
          '<button class="adm-btn adm-btn-blue rider-act" data-act="edit" data-id="' + r.id + '">Edit</button>' +
          '<button class="adm-btn ' + (on ? 'adm-btn-red' : 'adm-btn-green') + ' rider-act" data-act="toggle" data-id="' + r.id + '">' + (on ? 'Set offline' : 'Set active') + '</button>' +
          '<button class="adm-btn adm-btn-red rider-act" data-act="delete" data-id="' + r.id + '">Delete</button>' +
        '</td></tr>';
    }).join('') || '<tr><td colspan="10" style="text-align:center;color:var(--muted);padding:28px">No riders yet. Click "+ Add rider" to create one.</td></tr>';
    el.innerHTML =
      '<div class="adm-head"><div><h1>Riders</h1><div class="adm-head-sub">' + DB.riders.length + ' registered · ' + active + ' active</div></div>' +
      '<button class="btn-primary" id="addRiderBtn" style="padding:10px 18px">+ Add rider</button></div>' +
      '<div class="adm-table-wrap"><div class="adm-table-head"><h3>All Riders</h3></div>' +
      '<table><thead><tr><th>ID</th><th>Name</th><th>Phone</th><th>Vehicle</th><th>Area</th><th>Rating</th><th>Trips</th><th>Earnings</th><th>Status</th><th>Actions</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table></div>';
  };

  function riderModal(rider) {
    var edit = !!rider;
    rider = rider || { name: '', phone: '', vehicle: 'Motorbike', plate: '', area: '', status: 'active' };
    var vehicles = VEHICLES.indexOf(rider.vehicle) < 0 ? VEHICLES.concat([rider.vehicle]) : VEHICLES;
    var ov = document.createElement('div');
    ov.className = 'modal-overlay'; ov.id = 'riderModal';
    ov.innerHTML =
      '<div class="modal-box" style="max-width:520px"><button class="modal-close" id="rmClose" aria-label="Close">✕</button>' +
      '<div style="padding:28px">' +
      '<h2 style="font-size:1.25rem;margin-bottom:4px">' + (edit ? 'Edit rider ' + clean(rider.id) : 'Add rider') + '</h2>' +
      '<p style="font-size:.82rem;color:var(--muted);margin-bottom:18px">' + (edit ? 'Rating, trips and earnings update automatically from completed orders.' : 'New riders can be assigned to orders straight away when active.') + '</p>' +
      '<div class="field-group"><label>Full name *</label><input class="field" id="rmName" maxlength="80" value="' + clean(rider.name) + '" placeholder="e.g. John Kamau"/></div>' +
      '<div class="field-group"><label>Phone *</label><input class="field" id="rmPhone" type="tel" value="' + clean(rider.phone) + '" placeholder="0712 345 678"/></div>' +
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">' +
        '<div class="field-group"><label>Vehicle</label><select class="field" id="rmVehicle">' +
          vehicles.map(function (v) { return '<option' + (v === rider.vehicle ? ' selected' : '') + '>' + clean(v) + '</option>'; }).join('') + '</select></div>' +
        '<div class="field-group"><label>Plate (optional)</label><input class="field" id="rmPlate" maxlength="20" value="' + clean(rider.plate || '') + '" placeholder="KBZ 456X"/></div>' +
      '</div>' +
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">' +
        '<div class="field-group"><label>Area (optional)</label><input class="field" id="rmArea" maxlength="80" value="' + clean(rider.area || '') + '" placeholder="e.g. CBD / Westlands"/></div>' +
        '<div class="field-group"><label>Status</label><select class="field" id="rmStatus">' +
          '<option value="active"' + (rider.status === 'active' ? ' selected' : '') + '>Active</option>' +
          '<option value="offline"' + (rider.status === 'offline' ? ' selected' : '') + '>Offline</option></select></div>' +
      '</div>' +
      '<div id="rmError" style="color:var(--red);font-size:.82rem;min-height:1.2em;margin:4px 0 10px"></div>' +
      '<div style="display:flex;gap:10px;justify-content:flex-end">' +
        '<button class="btn-outline" id="rmCancel" style="padding:10px 18px">Cancel</button>' +
        '<button class="btn-primary" id="rmSave" style="padding:10px 22px">' + (edit ? 'Save changes' : 'Add rider') + '</button>' +
      '</div></div></div>';
    document.body.appendChild(ov);
    document.documentElement.classList.add('no-scroll');

    function close() {
      document.removeEventListener('keydown', onKey);
      document.documentElement.classList.remove('no-scroll');
      if (ov.parentNode) ov.parentNode.removeChild(ov);
    }
    function onKey(e) { if (e.key === 'Escape') close(); }
    document.addEventListener('keydown', onKey);
    ov.addEventListener('mousedown', function (e) { if (e.target === ov) close(); });
    $('rmClose').addEventListener('click', close);
    $('rmCancel').addEventListener('click', close);
    $('rmName').focus();

    $('rmSave').addEventListener('click', function () {
      var data = {
        name: $('rmName').value.trim(), phone: $('rmPhone').value.trim(),
        vehicle: $('rmVehicle').value, plate: $('rmPlate').value.trim(),
        area: $('rmArea').value.trim(), status: $('rmStatus').value
      };
      var err = $('rmError'); err.textContent = '';
      if (data.name.length < 2) { err.textContent = 'Enter the rider\'s full name.'; return; }
      if (!data.phone) { err.textContent = 'Enter the rider\'s phone number.'; return; }
      lock('rmSave', true);
      api(edit ? '/api/admin/riders/' + encodeURIComponent(rider.id) : '/api/admin/riders',
          { method: edit ? 'PATCH' : 'POST', body: data }).then(function (r) {
        var i = rawRiders.findIndex(function (x) { return x.id === r.rider.id; });
        if (i >= 0) rawRiders[i] = r.rider; else rawRiders.push(r.rider);
        setRiders(rawRiders); renderAdmRiders();
        showToast(edit ? 'Rider updated ✓' : 'Rider ' + r.rider.id + ' added ✓', 'success');
        close();
      }, function (e) { lock('rmSave', false); err.textContent = e.message; });
    });
  }

  document.addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('#addRiderBtn')) { riderModal(null); return; }
    var b = e.target.closest && e.target.closest('.rider-act'); if (!b) return;
    var id = b.dataset.id, act = b.dataset.act;
    var r = rawRiders.find(function (x) { return x.id === id; }); if (!r) return;
    if (act === 'edit') { riderModal(r); return; }
    b.disabled = true;
    if (act === 'toggle') {
      var next = r.status === 'active' ? 'offline' : 'active';
      api('/api/admin/riders/' + encodeURIComponent(id), { method: 'PATCH', body: { status: next } }).then(function (res) {
        rawRiders[rawRiders.indexOf(r)] = res.rider; setRiders(rawRiders); renderAdmRiders();
        showToast(r.name + ' is now ' + next, 'success');
      }, function (er) { b.disabled = false; fail(er); });
    } else if (act === 'delete') {
      if (!window.confirm('Delete rider ' + r.name + '? This cannot be undone.')) { b.disabled = false; return; }
      api('/api/admin/riders/' + encodeURIComponent(id), { method: 'DELETE' }).then(function () {
        setRiders(rawRiders.filter(function (x) { return x.id !== id; })); renderAdmRiders();
        showToast('Rider deleted');
      }, function (er) { b.disabled = false; fail(er); });
    }
  });


  /* ── dialogs (password screens) ────────────────────────────── */
  function dialog(o) {
    var hasPw = o.fields.some(function (f) { return f.type === 'password'; });
    var ov = document.createElement('div');
    ov.className = 'modal-overlay'; ov.id = 'srDialog';
    ov.innerHTML =
      '<div class="modal-box" style="max-width:440px"><button class="modal-close" aria-label="Close">✕</button><div style="padding:28px">' +
      '<h2 style="font-size:1.25rem;margin-bottom:6px">' + o.title + '</h2>' +
      '<p style="font-size:.84rem;color:var(--muted);margin-bottom:18px;line-height:1.6">' + o.intro + '</p>' +
      o.fields.map(function (f) {
        return '<div class="field-group"><label>' + f.label + '</label><input class="field" id="dlg_' + f.id + '" type="' + (f.type || 'text') +
               '" autocomplete="' + (f.auto || 'off') + '" placeholder="' + (f.ph || '') + '" maxlength="200"/></div>';
      }).join('') +
      (hasPw ? '<label style="display:flex;gap:8px;align-items:center;font-size:.8rem;color:var(--muted);margin-bottom:6px;cursor:pointer"><input type="checkbox" class="dlg-show"/> Show password</label>' : '') +
      '<div class="dlg-err" style="color:var(--red);font-size:.82rem;min-height:1.2em;margin:4px 0 10px"></div>' +
      '<div style="display:flex;gap:10px;justify-content:flex-end"><button class="btn-outline dlg-cancel" style="padding:10px 18px">Cancel</button>' +
      '<button class="btn-primary dlg-ok" style="padding:10px 22px">' + o.submit + '</button></div></div></div>';
    document.body.appendChild(ov);
    document.documentElement.classList.add('no-scroll');
    var err = ov.querySelector('.dlg-err'), ok = ov.querySelector('.dlg-ok');
    var ui = {
      error: function (m) { err.textContent = m || ''; },
      busy: function (on) { ok.disabled = !!on; ok.style.opacity = on ? '.6' : ''; },
      close: function () {
        document.removeEventListener('keydown', onKey);
        document.documentElement.classList.remove('no-scroll');
        if (ov.parentNode) ov.parentNode.removeChild(ov);
      }
    };
    function onKey(e) { if (e.key === 'Escape') ui.close(); }
    function submit() {
      var v = {}; o.fields.forEach(function (f) { v[f.id] = $('dlg_' + f.id).value; });
      ui.error(''); o.onSubmit(v, ui);
    }
    document.addEventListener('keydown', onKey);
    ov.addEventListener('mousedown', function (e) { if (e.target === ov) ui.close(); });
    ov.querySelector('.modal-close').addEventListener('click', ui.close);
    ov.querySelector('.dlg-cancel').addEventListener('click', ui.close);
    ok.addEventListener('click', submit);
    ov.addEventListener('keydown', function (e) { if (e.key === 'Enter' && e.target.tagName === 'INPUT' && e.target.type !== 'checkbox') submit(); });
    var sh = ov.querySelector('.dlg-show');
    if (sh) sh.addEventListener('change', function () {
      o.fields.forEach(function (f) { if (f.type === 'password') $('dlg_' + f.id).type = sh.checked ? 'text' : 'password'; });
    });
    var first = $('dlg_' + o.fields[0].id); if (first) { if (o.prefill) first.value = o.prefill; first.focus(); }
    return ui;
  }

  function checkNewPassword(v, ui) {
    if (!v.password || v.password.length < 8) { ui.error('Use at least 8 characters for the new password.'); return false; }
    if (v.password !== v.confirm) { ui.error('The two new passwords do not match.'); return false; }
    return true;
  }

  function openForgot() {
    dialog({
      title: 'Forgot your password?', intro: 'Enter the email you registered with. We will email you a link to choose a new password.',
      fields: [{ id: 'email', label: 'Email', type: 'email', auto: 'email', ph: 'you@email.com' }],
      submit: 'Send reset link', prefill: ($('lEmail') && $('lEmail').value.trim()) || '',
      onSubmit: function (v, ui) {
        var email = v.email.trim();
        if (!email) { ui.error('Enter your email address.'); return; }
        ui.busy(true);
        api('/api/auth/forgot', { method: 'POST', body: { email: email } }).then(function (r) {
          ui.close(); showToast(r.message, 'success');
        }, function (e) { ui.busy(false); ui.error(e.message); });
      }
    });
  }

  function openChangePassword() {
    if (!token) { showToast('Please log in first', 'error'); return; }
    dialog({
      title: 'Change password', intro: 'You will stay signed in here. Other devices will be signed out.',
      fields: [
        { id: 'current', label: 'Current password', type: 'password', auto: 'current-password' },
        { id: 'password', label: 'New password', type: 'password', auto: 'new-password', ph: 'At least 8 characters' },
        { id: 'confirm', label: 'Confirm new password', type: 'password', auto: 'new-password' }],
      submit: 'Change password',
      onSubmit: function (v, ui) {
        if (!v.current) { ui.error('Enter your current password.'); return; }
        if (!checkNewPassword(v, ui)) return;
        ui.busy(true);
        api('/api/auth/password', { method: 'POST', body: { current: v.current, password: v.password } }).then(function (r) {
          token = r.token; sset('localStorage', 'sr_token', token); DB.currentUser = r.user; updateUserUI();
          ui.close(); showToast(r.message, 'success');
        }, function (e) { ui.busy(false); ui.error(e.message); });
      }
    });
  }

  function openReset(tok) {
    dialog({
      title: 'Choose a new password', intro: 'Pick a password of at least 8 characters.',
      fields: [
        { id: 'password', label: 'New password', type: 'password', auto: 'new-password', ph: 'At least 8 characters' },
        { id: 'confirm', label: 'Confirm new password', type: 'password', auto: 'new-password' }],
      submit: 'Update password',
      onSubmit: function (v, ui) {
        if (!checkNewPassword(v, ui)) return;
        ui.busy(true);
        api('/api/auth/reset', { method: 'POST', body: { token: tok, password: v.password } }).then(function (r) {
          ui.close(); showToast(r.message, 'success'); setPage('login');
        }, function (e) { ui.busy(false); ui.error(e.message); });
      }
    });
  }

  // "Change password" in the account menu, only while logged in
  var prevUserUI = window.updateUserUI;
  window.updateUserUI = function () {
    var r = prevUserUI.apply(this, arguments);
    var a = $('ddChangePw'); if (a) a.style.display = DB.currentUser ? '' : 'none';
    return r;
  };

  /* ── admin: live alerts and the notifications log ──────────── */
  var audioCtx, titleBase = document.title, pulse = { latest: null, paid: null, pending: null, timer: null };
  function beep() {
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === 'suspended') audioCtx.resume();
      [880, 1175].forEach(function (f, i) {
        var t = audioCtx.currentTime + i * 0.18, o = audioCtx.createOscillator(), g = audioCtx.createGain();
        o.frequency.value = f; o.connect(g); g.connect(audioCtx.destination);
        g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.2, t + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.16);
        o.start(t); o.stop(t + 0.18);
      });
    } catch (e) {}
  }
  function alertAdmin(msg) {
    showToast('🔔 ' + msg, 'success'); beep();
    if (document.hidden) {
      document.title = '🔔 ' + msg;
      try { if (window.Notification && Notification.permission === 'granted') new Notification('SwiftRun', { body: msg }); } catch (e) {}
    }
  }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) document.title = titleBase; });
  function onAdminPage() { var p = $('page-admin'); return !!(p && p.classList.contains('active')); }

  function pollPulse() {
    if (!isAdmin()) { stopPulse(); return; }
    api('/api/admin/pulse').then(function (p) {
      var first = pulse.latest === null;
      var newOrder = !first && p.latest > pulse.latest, newPay = !first && p.paid > pulse.paid;
      var changed = newOrder || newPay || (!first && p.pending !== pulse.pending);
      pulse.latest = p.latest; pulse.paid = p.paid; pulse.pending = p.pending;
      var b = $('pendingBadge'); if (b) b.textContent = p.pending;
      var nb = $('notifBadge'); if (nb) { nb.textContent = p.failedNotifications; nb.style.display = p.failedNotifications ? '' : 'none'; }
      if (newOrder) alertAdmin('New order SR-' + p.latest); else if (newPay) alertAdmin('Payment received');
      if (changed && onAdminPage()) refreshAdmin();
    }, function () {});
  }
  function startPulse() {
    if (pulse.timer) return;
    pulse.latest = pulse.paid = pulse.pending = null;
    pulse.timer = setInterval(pollPulse, 20000); pollPulse();
  }
  function stopPulse() { if (pulse.timer) clearInterval(pulse.timer); pulse.timer = null; }

  function injectNotifTab() {
    var nav = document.querySelector('.admin-nav'), main = document.querySelector('.admin-main');
    if (!nav || !main || $('adm-nav-notifications')) return;
    var a = document.createElement('a');
    a.className = 'adm-link'; a.id = 'adm-nav-notifications'; a.dataset.admtab = 'notifications';
    a.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>Notifications <span class="adm-badge" id="notifBadge" style="display:none">0</span>';
    nav.appendChild(a);
    var d = document.createElement('div'); d.id = 'adm-notifications'; d.className = 'adm-tab'; main.appendChild(d);
    a.addEventListener('click', function () { showAdminTab('notifications'); loadNotifications(); });
  }

  function loadNotifications() {
    var el = $('adm-notifications'); if (!el) return;
    api('/api/admin/notifications').then(renderNotifications, fail);
  }
  function renderNotifications(r) {
    var el = $('adm-notifications'), list = clean(r.notifications), warn = '';
    if (r.smsMode === 'log' || r.emailMode === 'log') {
      warn = '<div style="background:rgba(240,165,0,.1);border:1px solid var(--orange);color:var(--orange);border-radius:var(--rs);padding:12px 14px;font-size:.84rem;margin-bottom:16px;line-height:1.6">' +
        (r.smsMode === 'log' ? 'SMS is in <b>log mode</b>: text messages are recorded here but <b>not sent</b>. ' : '') +
        (r.emailMode === 'log' ? 'Email is in <b>log mode</b>: emails are recorded here but <b>not sent</b>. ' : '') +
        'Set the SMS and email provider settings on the server to send them for real.</div>';
    }
    var colour = { sent: 'var(--green)', queued: 'var(--orange)', sending: 'var(--orange)', failed: 'var(--red)' };
    var rows = list.map(function (n) {
      return '<tr><td style="font-size:.78rem;color:var(--muted);white-space:nowrap">' + n.createdAt + '</td>' +
        '<td style="font-size:.82rem">' + n.to + '<div style="font-size:.7rem;color:var(--muted)">' + n.audience + ' · ' + n.channel + '</div></td>' +
        '<td style="font-size:.82rem">' + n.event.replace(/_/g, ' ') + (n.order ? '<div style="font-size:.7rem;color:var(--muted)">' + n.order + '</div>' : '') + '</td>' +
        '<td><span style="font-size:.78rem;font-weight:600;color:' + (colour[n.status] || 'var(--muted)') + '">' + n.status + '</span>' +
          '<div style="font-size:.7rem;color:var(--muted)">' + (n.provider || '') + (n.attempts ? ' · tries ' + n.attempts : '') + '</div></td>' +
        '<td style="font-size:.78rem;color:var(--muted);max-width:340px">' + (n.error ? '<span style="color:var(--red)">' + n.error + '</span>' : n.preview) + '</td>' +
        '<td>' + (n.status === 'failed' ? '<button class="adm-btn adm-btn-blue notif-retry" data-id="' + n.id + '">Retry</button>' : '') + '</td></tr>';
    }).join('') || '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:28px">No notifications yet. They appear here as orders come in.</td></tr>';
    el.innerHTML = '<div class="adm-head"><div><h1>Notifications</h1><div class="adm-head-sub">Every message sent to customers, riders and you (last 200)</div></div>' +
      '<button class="adm-btn adm-btn-blue" id="notifRefresh" style="padding:9px 16px">Refresh</button></div>' + warn +
      '<div class="adm-table-wrap"><table><thead><tr><th>Time</th><th>To</th><th>Event</th><th>Status</th><th>Message / error</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }
  document.addEventListener('click', function (e) {
    if (e.target.closest && e.target.closest('#notifRefresh')) { loadNotifications(); return; }
    var b = e.target.closest && e.target.closest('.notif-retry'); if (!b) return;
    b.disabled = true;
    api('/api/admin/notifications/' + encodeURIComponent(b.dataset.id) + '/retry', { method: 'POST' })
      .then(function () { showToast('Queued for another try', 'success'); setTimeout(loadNotifications, 2500); }, function (er) { b.disabled = false; fail(er); });
  });


  /* ── admin: unpaid M-Pesa orders (check payment / mark paid) ── */
  function isUnpaidMpesa(o) {
    return o && o.payMethod === 'mpesa' && o.payStatus !== 'paid' && (o.status === 'pending' || o.status === 'confirmed');
  }
  // Accept / Dispatch are blocked by the server for these orders, so offer the payment actions instead
  var prevAdmRow = window.admOrderRow;
  window.admOrderRow = function (o) {
    var h = prevAdmRow.apply(this, arguments);
    if (!isUnpaidMpesa(o)) return h;
    return h.replace(/<button class="adm-btn adm-btn-(?:green|blue) adm-action" data-action="(?:confirm|dispatch)"[^>]*>[^<]*<\/button>/,
      '<button class="adm-btn adm-btn-blue sr-check-pay" data-id="' + o.id + '" title="Ask Safaricom whether this order was paid">Check payment</button>' +
      '<button class="adm-btn adm-btn-green sr-mark-pay" data-id="' + o.id + '" title="Record an M-Pesa payment you have verified">Mark paid</button>');
  };

  function afterAdminChange() {
    updateAdminBadge(); renderAdmOverview(); renderAdmOrders();
    api('/api/admin/riders').then(function (x) { setRiders(x.riders); renderAdmRiders(); }).catch(function () {});
    api('/api/admin/payments').then(function (x) { DB.payments = clean(x.payments); renderAdmPayments(); }).catch(function () {});
  }

  function openMarkPaid(id) {
    var o = DB.orders.find(function (x) { return x.id === id; });
    dialog({
      title: 'Mark ' + id + ' as paid',
      intro: 'Only do this after you have seen the payment in your M-Pesa statement or the M-Pesa confirmation message' +
             (o ? ' (<b>KES ' + o.total.toLocaleString() + '</b> from ' + o.customer.phone + ')' : '') +
             '. The order will be confirmed and a rider assigned.',
      fields: [
        { id: 'ref', label: 'M-Pesa receipt code', ph: 'e.g. RDE3XYZ12A' },
        { id: 'note', label: 'Note (optional)', ph: 'e.g. checked on the statement' }],
      submit: 'Mark as paid',
      onSubmit: function (v, ui) {
        var ref = v.ref.trim();
        if (!ref) { ui.error('Enter the M-Pesa receipt code from the confirmation message.'); return; }
        ui.busy(true);
        api('/api/admin/orders/' + encodeURIComponent(id) + '/mark-paid', { method: 'POST', body: { ref: ref, note: v.note.trim() } }).then(function (r) {
          upsert(r.order); ui.close(); showToast('Order ' + id + ' marked as paid ✓', 'success'); afterAdminChange();
        }, function (e) { ui.busy(false); ui.error(e.message); });
      }
    });
  }

  document.addEventListener('click', function (e) {
    var chk = e.target.closest && e.target.closest('.sr-check-pay');
    if (chk) {
      e.stopPropagation(); e.preventDefault();
      var id = chk.dataset.id; chk.disabled = true; chk.textContent = 'Checking…';
      api('/api/admin/orders/' + encodeURIComponent(id) + '/payment/check', { method: 'POST' }).then(function (r) {
        upsert(r.order);
        showToast(r.message, r.state === 'completed' ? 'success' : r.state === 'failed' ? 'error' : 'default');
        afterAdminChange();
      }, function (er) { chk.disabled = false; chk.textContent = 'Check payment'; fail(er); });
      return;
    }
    var mk = e.target.closest && e.target.closest('.sr-mark-pay');
    if (mk) { e.stopPropagation(); e.preventDefault(); openMarkPaid(mk.dataset.id); }
  }, true);

  /* ── on load: restore the session, add the new screens ─────── */
  function checkResetLink() {
    var m = /^#reset=([\w-]{20,120})$/.exec(location.hash);
    if (!m) return;
    try { history.replaceState(null, '', location.pathname + location.search); } catch (e) {}   // keep the token out of the address bar
    openReset(m[1]);
  }
  document.addEventListener('DOMContentLoaded', function () {
    // "Forgot your password?" under the login button
    var btn = $('loginSubmitBtn');
    if (btn) {
      btn.insertAdjacentHTML('afterend', '<p class="auth-switch" style="margin-top:12px"><a id="forgotLink">Forgot your password?</a></p>');
      $('forgotLink').addEventListener('click', openForgot);
    }
    // "Change password" in the account menu
    var dd = $('userDropdown'), lo = $('ddLogout');
    if (dd && lo) {
      var a = document.createElement('a'); a.id = 'ddChangePw'; a.textContent = 'Change password'; a.style.display = 'none';
      dd.insertBefore(a, lo);
      a.addEventListener('click', function () { dd.classList.remove('open'); openChangePassword(); });
    }
    injectNotifTab();
    checkResetLink();
    // open the page named in the address, e.g. https://site/#/about
    var start = routeFromHash(), waitForSession = false;
    if (start && start !== 'home') {
      if (start === 'admin' && token) waitForSession = true;     // admin only once the saved login is confirmed
      else { var first = showPage(start); if (first.page !== start) writeHash(first.page, true); }
    }
    function openAdminLink() { var a = showPage('admin'); if (a.page !== 'admin') writeHash(a.page, true); }
    if (token) {
      api('/api/auth/me').then(function (r) {
        DB.currentUser = r.user; updateUserUI(); if (isAdmin()) startPulse();
        if (waitForSession) openAdminLink();
        return refreshOrders();
      }, function () { dropSession(); if (waitForSession) openAdminLink(); renderTrack(); });
    } else refreshOrders();
  });
  window.addEventListener('hashchange', checkResetLink);
})();
