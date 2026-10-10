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
  }
  function dropSession() {
    token = null; sset('localStorage', 'sr_token', null); sset('sessionStorage', 'sr_lookup', null);
    DB.currentUser = null; DB.orders = []; updateUserUI();
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
      setPage(isAdmin() ? 'admin' : 'home');
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
      setPage('home');
    }, fail).then(function () { lock('registerSubmitBtn', false); });
  };
  window.logout = function () { dropSession(); renderTrack(); showToast('Logged out', 'default'); };

  /* ── page changes: load fresh data, guard the admin page ───── */
  var prevSetPage = window.setPage;
  window.setPage = function (page) {
    if (page === 'admin' && !isAdmin()) {
      showToast(token ? 'Admin access only' : 'Please log in with an admin account', 'error');
      return prevSetPage.call(this, token ? 'home' : 'login');
    }
    var r = prevSetPage.apply(this, arguments);
    if (page === 'track') refreshOrders();
    if (page === 'admin') refreshAdmin();
    return r;
  };

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

  /* ── on load: restore the session ──────────────────────────── */
  document.addEventListener('DOMContentLoaded', function () {
    if (token) {
      api('/api/auth/me').then(function (r) { DB.currentUser = r.user; updateUserUI(); return refreshOrders(); }, function () { dropSession(); renderTrack(); });
    } else refreshOrders();
  });
})();
