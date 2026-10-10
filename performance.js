/* SwiftRun - performance and device tweaks (drop-in). Load after your main script and categories.js. */
(function () {
  'use strict';
  var root = document.documentElement;

  /* 1. Phone-friendly viewport: notch safe areas, keyboard that resizes the page */
  var vp = document.querySelector('meta[name="viewport"]');
  var want = 'width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content';
  if (vp && vp.content.indexOf('viewport-fit') < 0) vp.setAttribute('content', want);
  if (!document.querySelector('meta[name="theme-color"]')) {
    var tc = document.createElement('meta'); tc.name = 'theme-color'; tc.content = '#0D1117'; document.head.appendChild(tc);
  }

  /* 2. Lite mode for weak phones, data saver, slow networks (add ?lite=1 to force it) */
  var c = navigator.connection || {};
  var lite = /[?&]lite=1/.test(location.search) ||
    (navigator.deviceMemory && navigator.deviceMemory <= 2) ||
    (navigator.hardwareConcurrency && navigator.hardwareConcurrency <= 2) ||
    c.saveData === true || /(^|-)2g$/.test(c.effectiveType || '');
  if (lite) root.classList.add('lite');

  /* 3. Page changes jump to the top instantly (no long smooth scroll across a changed page) */
  if (typeof setPage === 'function') {
    var _sp = window.setPage;
    window.setPage = function () {
      var r = _sp.apply(this, arguments);
      root.style.scrollBehavior = 'auto';
      window.scrollTo(0, 0);
      requestAnimationFrame(function () { root.style.scrollBehavior = ''; });
      return r;
    };
  }

  /* 4. Lock the page behind open modals so scrolling stays inside them */
  var modals = ['orderModal', 'payModal'];
  function syncLock() {
    var open = modals.some(function (id) {
      var e = document.getElementById(id);
      return e && e.style.display && e.style.display !== 'none';
    });
    root.classList.toggle('no-scroll', open);
  }
  modals.forEach(function (id) {
    var e = document.getElementById(id);
    if (e && window.MutationObserver) new MutationObserver(syncLock).observe(e, { attributes: true, attributeFilter: ['style'] });
  });

  /* 5. Hide the WhatsApp button on phones while the chat is open */
  var wa = document.getElementById('waWindow');
  if (wa && window.MutationObserver) {
    new MutationObserver(function () { document.body.classList.toggle('wa-open', !wa.classList.contains('hidden')); })
      .observe(wa, { attributes: true, attributeFilter: ['class'] });
  }

  /* 6. Pause the floating hero cards when they are off screen */
  var stack = document.querySelector('.hero-card-stack');
  if (stack && 'IntersectionObserver' in window) {
    new IntersectionObserver(function (es) { stack.classList.toggle('paused', !es[0].isIntersecting); }).observe(stack);
  }

  /* 7. Menu: close on outside tap, and when the screen grows past the menu breakpoint */
  var links = document.getElementById('navLinks');
  if (links) {
    document.addEventListener('click', function (e) { if (!e.target.closest('.nav-inner')) links.classList.remove('open'); });
    if (window.matchMedia) {
      var mq = window.matchMedia('(min-width: 861px)');
      var close = function (m) { if (m.matches) links.classList.remove('open'); };
      if (mq.addEventListener) mq.addEventListener('change', close); else if (mq.addListener) mq.addListener(close);
    }
  }
})();
