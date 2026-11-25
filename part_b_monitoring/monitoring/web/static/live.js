// Refreshes the live region every 2 s (NFR-PRF-04 asks for 5 s) and sounds the
// alarm tone while an Escalated alarm is open. No data is decided here.
(function () {
  var live = document.getElementById('live');
  if (!live) return;
  var tone = false, ctx = null;
  var btn = document.getElementById('tone-toggle');
  var status = document.getElementById('refresh-status');
  function showStatus(text, state) {
    if (!status) return;
    status.textContent = text;
    status.setAttribute('data-state', state);
  }
  if (btn) btn.addEventListener('click', function () {
    tone = !tone;
    btn.setAttribute('aria-pressed', tone ? 'true' : 'false');
    btn.textContent = tone ? 'Disable alarm tone' : 'Enable alarm tone';
    if (tone && !ctx) { try { ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) {} }
    beepIfNeeded();
  });
  function beep() {
    if (!ctx) return;
    var o = ctx.createOscillator(), g = ctx.createGain();
    o.frequency.value = 880; g.gain.value = 0.2;
    o.connect(g); g.connect(ctx.destination); o.start(); o.stop(ctx.currentTime + 0.4);
  }
  function escalated() {
    var f = document.getElementById('escalated-flag');
    return f && f.getAttribute('data-escalated') === 'yes';
  }
  function beepIfNeeded() { if (tone && escalated()) beep(); }
  function controlKey(element) {
    var fields = element.tagName === 'FORM' ? Array.from(element.querySelectorAll('input[type="hidden"]')).map(function (field) {
      return [field.name, field.value];
    }) : [];
    return JSON.stringify([element.tagName, element.id, element.getAttribute('action'),
      element.getAttribute('href'), element.getAttribute('aria-label'), element.getAttribute('aria-labelledby'), fields]);
  }
  function updateLive(html) {
    var next = document.createElement('div');
    next.innerHTML = html;
    var focused = live.contains(document.activeElement) ? document.activeElement : null;
    var kept = focused && (focused.closest('form') || focused);
    var region = focused && focused.closest('.table-scroll');
    var regionKey = region && controlKey(region);
    var scrolls = Array.from(live.querySelectorAll('.table-scroll')).map(function (table) {
      return { key: controlKey(table), left: table.scrollLeft, top: table.scrollTop };
    });
    if (kept) {
      var replacement = Array.from(next.querySelectorAll(kept.tagName)).find(function (element) {
        return controlKey(element) === controlKey(kept);
      });
      if (replacement) {
        if (kept.classList.contains('table-scroll')) kept.replaceChildren.apply(kept, Array.from(replacement.childNodes));
        replacement.replaceWith(kept);
      }
    }
    live.replaceChildren.apply(live, Array.from(next.childNodes));
    if (focused) {
      var target = live.contains(focused) ? focused : Array.from(live.querySelectorAll('.table-scroll')).find(function (table) {
        return controlKey(table) === regionKey;
      });
      if (target) target.focus({ preventScroll: true });
    }
    Array.from(live.querySelectorAll('.table-scroll')).forEach(function (table) {
      var position = scrolls.find(function (saved) { return saved.key === controlKey(table); });
      if (position) { table.scrollLeft = position.left; table.scrollTop = position.top; }
    });
  }
  function refresh() {
    fetch(live.getAttribute('data-url'), { credentials: 'same-origin' })
      .then(function (r) {
        var banner = document.getElementById('mode-banner');
        var mode = r.headers.get('X-Monitoring-Mode');
        var message = r.headers.get('X-Monitoring-Banner');
        if (banner && mode) {
          banner.hidden = !message;
          banner.className = 'banner banner-' + mode.toLowerCase();
          banner.textContent = message ? mode.toUpperCase() + ' MODE. ' + message : '';
        }
        // Fetch follows session-expiry redirects. Navigate the whole page rather
        // than inserting the login page into the live dashboard region.
        if (!r.redirected && r.status === 200) return r.text();
        window.location.reload();
      })
      .then(function (html) {
        if (html) {
          updateLive(html);
          showStatus('Live view \u00b7 updates every 2 seconds', 'live');
          beepIfNeeded();
        }
      })
      .catch(function () { showStatus('Live updates unavailable. Displayed data may be out of date; retrying automatically.', 'error'); });
  }
  setInterval(refresh, 2000);
  setInterval(beepIfNeeded, 4000);
})();
