/* SwiftRun - Service categories (drop-in)
   Setup: add one script tag for categories.js to index.html, just before the
   closing body tag and after your main script block. No other edits needed. */
(function () {
  'use strict';

  /* ── 1. CATEGORIES & SERVICES ─────────────────────────────── */
  var CATS = [
    { id:'delivery',  icon:'📦', name:'Delivery & Courier',   blurb:'Send parcels, documents and gifts across the city' },
    { id:'shopping',  icon:'🛒', name:'Shopping',             blurb:'Groceries, market produce and household items' },
    { id:'food',      icon:'🍔', name:'Food & Drinks',        blurb:'Takeaway, bakery and restaurant pickups' },
    { id:'health',    icon:'💊', name:'Pharmacy & Health',    blurb:'Prescriptions, medicine and lab results' },
    { id:'paperwork', icon:'📝', name:'Queues & Paperwork',   blurb:'We queue, pay and submit so you don’t have to' },
    { id:'pickups',   icon:'🔄', name:'Pickups & Returns',    blurb:'Collect packages and return online orders' },
    { id:'home',      icon:'🏠', name:'Home Services',        blurb:'Laundry, gas refills and repairs drop-off' },
    { id:'other',     icon:'✨', name:'Something Else',       blurb:'Describe any errand and we’ll sort it' }
  ];
  // type = existing SwiftRun errand type, so the rest of the app keeps working
  var SVC = [
    { id:'parcel',   cat:'delivery',  icon:'📦', name:'Parcel delivery',          type:'delivery', eta:'1–2 hrs', ex:'e.g. Deliver a parcel from CBD to Westlands' },
    { id:'docs',     cat:'delivery',  icon:'📄', name:'Document delivery',        type:'delivery', eta:'1–2 hrs', ex:'e.g. Signed contract to Upper Hill office' },
    { id:'gift',     cat:'delivery',  icon:'🎁', name:'Gifts & flowers',          type:'delivery', eta:'2–3 hrs', ex:'e.g. Deliver a bouquet to Karen by 5pm' },
    { id:'grocery',  cat:'shopping',  icon:'🛒', name:'Supermarket groceries',    type:'grocery',  eta:'1–2 hrs', ex:'e.g. Carrefour: 2kg unga, 1L milk, bread' },
    { id:'market',   cat:'shopping',  icon:'🥬', name:'Fresh market produce',     type:'grocery',  eta:'1–2 hrs', ex:'e.g. Gikomba market: tomatoes, onions, sukuma' },
    { id:'house',    cat:'shopping',  icon:'🧴', name:'Household & hardware',     type:'errand',   eta:'1–3 hrs', ex:'e.g. Buy a padlock and 2 light bulbs' },
    { id:'anything', cat:'shopping',  icon:'🛍️', name:'Buy anything for me',      type:'errand',   eta:'1–3 hrs', ex:'e.g. Buy a phone charger from Luthuli Avenue' },
    { id:'takeaway', cat:'food',      icon:'🍽️', name:'Restaurant takeaway',      type:'pickup',   eta:'45–90 min', ex:'e.g. Pick up my order at Java, Yaya Centre' },
    { id:'bakery',   cat:'food',      icon:'🎂', name:'Cake & bakery pickup',     type:'pickup',   eta:'1–2 hrs', ex:'e.g. Collect birthday cake, order #123' },
    { id:'rx',       cat:'health',    icon:'💊', name:'Prescription pickup',      type:'pharmacy', eta:'1–2 hrs', ex:'e.g. Collect prescription from Goodlife Pharmacy' },
    { id:'otc',      cat:'health',    icon:'🩹', name:'Over-the-counter medicine',type:'pharmacy', eta:'1–2 hrs', ex:'e.g. Buy paracetamol and cough syrup' },
    { id:'lab',      cat:'health',    icon:'🧪', name:'Lab results collection',   type:'pickup',   eta:'1–3 hrs', ex:'e.g. Collect results from Lancet, Kilimani' },
    { id:'queue',    cat:'paperwork', icon:'🕒', name:'Queue for me',             type:'errand',   eta:'2–5 hrs', ex:'e.g. Queue at Huduma Centre for ID replacement' },
    { id:'bills',    cat:'paperwork', icon:'🧾', name:'Pay bills',                type:'errand',   eta:'1–3 hrs', ex:'e.g. Pay KPLC tokens, meter no. ...' },
    { id:'submit',   cat:'paperwork', icon:'🏛️', name:'Submit or collect documents', type:'errand', eta:'2–5 hrs', ex:'e.g. Submit forms at the KRA office' },
    { id:'bank',     cat:'paperwork', icon:'🏦', name:'Bank run',                 type:'errand',   eta:'2–4 hrs', ex:'e.g. Deposit a cheque at Equity, Moi Avenue' },
    { id:'collect',  cat:'pickups',   icon:'📬', name:'Collect a package',        type:'pickup',   eta:'1–2 hrs', ex:'e.g. Collect parcel from G4S courier office' },
    { id:'returns',  cat:'pickups',   icon:'↩️', name:'Online order returns',     type:'pickup',   eta:'1–3 hrs', ex:'e.g. Return a jacket to the Jumia pickup point' },
    { id:'laundry',  cat:'home',      icon:'👕', name:'Laundry pickup & drop-off',type:'pickup',   eta:'2–4 hrs', ex:'e.g. Take clothes to the dry cleaner in Kilimani' },
    { id:'gas',      cat:'home',      icon:'🔥', name:'Gas (LPG) refill',         type:'errand',   eta:'1–2 hrs', ex:'e.g. Refill a 13kg cylinder and bring it home' },
    { id:'repairs',  cat:'home',      icon:'🧵', name:'Tailor & repairs drop-off',type:'pickup',   eta:'2–4 hrs', ex:'e.g. Drop shoes at the cobbler, collect Friday' },
    { id:'custom',   cat:'other',     icon:'✨', name:'Something else',           type:'errand',   eta:'varies',  ex:'Describe what you need and where' }
  ];
  var TYPE_TO_CAT = { delivery:'Delivery & Courier', grocery:'Shopping', pharmacy:'Pharmacy & Health', pickup:'Pickups & Returns', errand:'Something Else' };

  function catById(id){ return CATS.filter(function(c){ return c.id === id; })[0]; }
  function svcById(id){ return SVC.filter(function(s){ return s.id === id; })[0]; }
  function $(id){ return document.getElementById(id); }
  function orderCat(o){ return o && o.category ? o.category : (o && TYPE_TO_CAT[o.type]) || 'Something Else'; }

  var sel = null, activeCat = 'all', query = '', autoDesc = '';

  /* ── 2. STYLES ───────────────────────────────────────────── */
  var css =
  '.svc-search{margin-bottom:12px}' +
  '.svc-chips{display:flex;gap:8px;overflow-x:auto;padding-bottom:8px;margin-bottom:10px;scrollbar-width:thin}' +
  '.svc-chip{flex-shrink:0;background:var(--dark3);border:1.5px solid var(--border);color:var(--muted);border-radius:50px;padding:6px 14px;font-size:.8rem;font-weight:500;cursor:pointer;transition:.2s;white-space:nowrap;font-family:var(--font-body)}' +
  '.svc-chip:hover,.svc-chip.active{border-color:var(--green);color:var(--green);background:rgba(0,166,81,.08)}' +
  '.svc-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-bottom:12px}' +
  '.svc-card{background:var(--dark3);border:1.5px solid var(--border);border-radius:var(--rs);padding:12px;text-align:left;cursor:pointer;color:var(--text);transition:.2s;display:flex;flex-direction:column;gap:4px;font-family:var(--font-body)}' +
  '.svc-card:hover{border-color:var(--muted)}' +
  '.svc-card.selected{border-color:var(--green);background:rgba(0,166,81,.1)}' +
  '.svc-card .si{font-size:1.4rem;line-height:1}' +
  '.svc-card b{font-size:.84rem;font-weight:600;line-height:1.3}' +
  '.svc-card small{color:var(--muted);font-size:.72rem}' +
  '.svc-selected{font-size:.85rem;color:var(--green);min-height:1.3em;margin-bottom:4px}' +
  '.svc-empty{grid-column:1/-1;color:var(--muted);font-size:.85rem;padding:8px 0}' +
  '.svc-tag{display:inline-block;margin-top:4px;font-size:.68rem;color:var(--green);background:rgba(0,166,81,.12);border-radius:4px;padding:1px 7px}' +
  '.svc-home-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}' +
  '.svc-cat-card{background:var(--dark2);border:1px solid var(--border);border-radius:var(--r);padding:20px;cursor:pointer;transition:.2s;text-align:left;color:var(--text);font-family:var(--font-body);display:block;width:100%}' +
  '.svc-cat-card:hover{border-color:var(--green);transform:translateY(-2px)}' +
  '.svc-cat-card .ci{font-size:1.8rem;line-height:1;margin-bottom:10px;display:block}' +
  '.svc-cat-card h3{font-size:1rem;margin-bottom:6px}' +
  '.svc-cat-card p{font-size:.82rem;color:var(--muted);line-height:1.5;margin-bottom:8px}' +
  '.svc-cat-card span.n{font-size:.75rem;color:var(--green);font-weight:600}' +
  '.svc-chip:focus-visible,.svc-card:focus-visible,.svc-cat-card:focus-visible{outline:2px solid var(--green);outline-offset:2px}';
  var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);

  /* ── 3. REQUEST PAGE PICKER ──────────────────────────────── */
  var card = document.querySelector('.request-form-card');
  if (card) {
    var picker = document.createElement('div');
    picker.id = 'svcPicker';
    picker.innerHTML =
      '<div class="form-section-title">What do you need?</div>' +
      '<input type="search" class="field svc-search" id="svcSearch" placeholder="Search services, e.g. gas, laundry, pharmacy" aria-label="Search services">' +
      '<div class="svc-chips" id="svcChips" role="tablist" aria-label="Service categories"></div>' +
      '<div class="svc-grid" id="svcGrid"></div>' +
      '<div class="svc-selected" id="svcSelected" aria-live="polite"></div>' +
      '<div class="form-divider"></div>';
    card.insertBefore(picker, card.firstChild);

    // The old Errand Type dropdown is now set automatically from the service chosen
    var typeSel = $('rType');
    if (typeSel && typeSel.closest('.field-group')) typeSel.closest('.field-group').style.display = 'none';

    $('svcSearch').addEventListener('input', function (e) { query = e.target.value.trim().toLowerCase(); renderPicker(); });
    $('svcChips').addEventListener('click', function (e) {
      var b = e.target.closest('[data-cat]'); if (!b) return;
      activeCat = b.dataset.cat; renderPicker();
    });
    $('svcGrid').addEventListener('click', function (e) {
      var b = e.target.closest('[data-svc]'); if (b) chooseService(b.dataset.svc);
    });
    renderPicker();
  }

  function renderPicker() {
    var chips = '<button class="svc-chip' + (activeCat === 'all' ? ' active' : '') + '" data-cat="all" role="tab">All (' + SVC.length + ')</button>';
    CATS.forEach(function (c) {
      var n = SVC.filter(function (s) { return s.cat === c.id; }).length;
      chips += '<button class="svc-chip' + (activeCat === c.id ? ' active' : '') + '" data-cat="' + c.id + '" role="tab">' + c.icon + ' ' + c.name + '</button>';
    });
    $('svcChips').innerHTML = chips;

    var list = SVC.filter(function (s) {
      var inCat = activeCat === 'all' || s.cat === activeCat;
      var hay = (s.name + ' ' + catById(s.cat).name + ' ' + s.ex).toLowerCase();
      return inCat && (!query || hay.indexOf(query) > -1);
    });
    $('svcGrid').innerHTML = list.length ? list.map(function (s) {
      return '<button type="button" class="svc-card' + (sel && sel.id === s.id ? ' selected' : '') + '" data-svc="' + s.id + '" aria-pressed="' + (sel && sel.id === s.id) + '">' +
        '<span class="si">' + s.icon + '</span><b>' + s.name + '</b><small>Usually ' + s.eta + '</small></button>';
    }).join('') : '<div class="svc-empty">No matching service. Try “Something else” and describe what you need.</div>';

    $('svcSelected').textContent = sel ? '✓ ' + sel.icon + ' ' + sel.name + '  ·  ' + catById(sel.cat).name : '';
  }

  function chooseService(id) {
    sel = svcById(id); if (!sel) return;
    if ($('rType')) $('rType').value = sel.type;
    var d = $('rDesc');
    if (d) {
      if (!d.value.trim() || d.value === autoDesc) { d.value = sel.name; autoDesc = sel.name; }
      d.placeholder = sel.ex;
    }
    renderPicker();
    if (d) d.focus({ preventScroll: true });
  }

  function clearSelection() { sel = null; autoDesc = ''; query = ''; if ($('svcSearch')) $('svcSearch').value = ''; if ($('rDesc')) $('rDesc').placeholder = 'e.g. Grocery shopping at Carrefour'; renderPicker(); }

  /* ── 4. HOME PAGE: BROWSE BY CATEGORY ────────────────────── */
  var how = document.querySelector('.how-section');
  if (how) {
    var sec = document.createElement('section');
    sec.className = 'how-section';
    sec.innerHTML = '<div class="section-label">OUR SERVICES</div><h2>Find the service you need</h2><div class="svc-home-grid">' +
      CATS.map(function (c) {
        var n = SVC.filter(function (s) { return s.cat === c.id; }).length;
        return '<button type="button" class="svc-cat-card" data-homecat="' + c.id + '"><span class="ci">' + c.icon + '</span><h3>' + c.name + '</h3><p>' + c.blurb + '</p><span class="n">' + n + (n === 1 ? ' service' : ' services') + ' →</span></button>';
      }).join('') + '</div>';
    how.parentNode.insertBefore(sec, how);
    sec.addEventListener('click', function (e) {
      var b = e.target.closest('[data-homecat]'); if (!b) return;
      activeCat = b.dataset.homecat; query = ''; if ($('svcSearch')) $('svcSearch').value = '';
      if (typeof setPage === 'function') setPage('request');
      renderPicker();
      var p = $('svcPicker'); if (p) setTimeout(function () { p.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, 50);
    });
  }

  /* ── 5. HOOK INTO EXISTING FUNCTIONS (no changes to your code) ── */
  if (typeof calculateCost === 'function') {
    var _calc = window.calculateCost;
    window.calculateCost = function () {
      if (!sel) {
        showToast('Please choose a service first', 'error');
        var p = $('svcPicker'); if (p) p.scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
      }
      return _calc.apply(this, arguments);
    };
  }
  if (typeof submitOrder === 'function') {
    var _sub = window.submitOrder;
    window.submitOrder = function () {
      var chosen = sel, before = DB.orders.length;
      var r = _sub.apply(this, arguments);
      if (chosen && DB.orders.length > before) {
        var o = DB.orders[0];
        o.category = catById(chosen.cat).name; o.service = chosen.name; o.serviceIcon = chosen.icon;
      }
      return r;
    };
  }
  if (typeof resetForm === 'function') {
    var _reset = window.resetForm;
    window.resetForm = function () { var r = _reset.apply(this, arguments); clearSelection(); return r; };
  }
  if (typeof renderOrderCard === 'function') {
    var _roc = window.renderOrderCard;
    window.renderOrderCard = function (o) {
      var label = '<div class="ocd-label">Errand · ' + (o.serviceIcon ? o.serviceIcon + ' ' : '') + orderCat(o) + '</div>';
      return _roc(o).replace('<div class="ocd-label">Errand</div>', label);
    };
  }
  if (typeof admOrderRow === 'function') {
    var _adm = window.admOrderRow;
    window.admOrderRow = function (o) {
      var h = _adm(o), d = o.desc || o.description || '';
      return d ? h.replace('">' + d + '</td>', '">' + d + '<div class="svc-tag">' + orderCat(o) + '</div></td>') : h;
    };
  }
  if (typeof openOrderModal === 'function') {
    var _oom = window.openOrderModal;
    window.openOrderModal = function (id) {
      var r = _oom.apply(this, arguments);
      var o = DB.orders.filter(function (x) { return x.id === id; })[0];
      if (o) {
        var labels = document.querySelectorAll('#orderModalContent .od-field-label');
        for (var i = 0; i < labels.length; i++) {
          if (labels[i].textContent === 'Errand Type' && labels[i].nextElementSibling) {
            labels[i].textContent = 'Service';
            labels[i].nextElementSibling.style.textTransform = 'none';
            labels[i].nextElementSibling.textContent = (o.serviceIcon ? o.serviceIcon + ' ' : '') + (o.service ? o.service + ' · ' : '') + orderCat(o);
          }
        }
      }
      return r;
    };
  }
})();
