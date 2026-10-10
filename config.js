/* SwiftRun - where is the backend?
   1. Deploy the backend (see README.md) and copy its address, e.g. https://swiftrun-api.onrender.com
   2. Paste it between the quotes below (no trailing slash, no /api).
   Running locally with python3 app.py? Leave this file alone; it only applies on github.io. */
(function () {
  var BACKEND_URL = 'https://goto-errands-co-ke-1.onrender.com/';
  if (!window.SWIFTRUN_API && /github\.io$/.test(location.hostname)) window.SWIFTRUN_API = BACKEND_URL;
})();
