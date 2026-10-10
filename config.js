/* SwiftRun - where is the backend?
   Local (python3 app.py):  leave as is, the page talks to the server it was loaded from.
   GitHub Pages:            replace the URL below with your deployed backend address (no trailing slash). */
(function () {
  var BACKEND_URL = 'https://github.com/Big-Dario';
  if (!window.SWIFTRUN_API && /github\.io$/.test(location.hostname)) window.SWIFTRUN_API = BACKEND_URL;
})();
