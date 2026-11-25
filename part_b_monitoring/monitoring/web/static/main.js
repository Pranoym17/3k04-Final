// Presentation controls only. Permissions and decisions remain on the server.
(function () {
  'use strict';
  document.documentElement.classList.add('js');
  var toggle = document.querySelector('.menu-toggle');
  var nav = document.getElementById('main-nav');
  if (toggle && nav) {
    function closeMenu(returnFocus) {
      nav.classList.remove('is-open');
      toggle.setAttribute('aria-expanded', 'false');
      toggle.setAttribute('aria-label', 'Open navigation');
      if (returnFocus) toggle.focus();
    }
    toggle.addEventListener('click', function () {
      var open = toggle.getAttribute('aria-expanded') !== 'true';
      nav.classList.toggle('is-open', open);
      toggle.setAttribute('aria-expanded', String(open));
      toggle.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && toggle.getAttribute('aria-expanded') === 'true') closeMenu(true);
    });
    document.addEventListener('click', function (event) {
      if (!nav.contains(event.target) && !toggle.contains(event.target)) closeMenu(false);
    });
    nav.addEventListener('click', function (event) {
      if (event.target.closest('a')) closeMenu(false);
    });
    window.matchMedia('(min-width: 721px)').addEventListener('change', function () { closeMenu(false); });
  }
  var video = document.querySelector('.bg-video');
  var videoToggle = document.getElementById('video-toggle');
  if (video && videoToggle) {
    var reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    function syncVideo() {
      videoToggle.textContent = video.paused ? 'Play background' : 'Pause background';
    }
    function respectMotion() {
      if (reducedMotion.matches) video.pause();
      videoToggle.hidden = reducedMotion.matches;
    }
    video.addEventListener('pause', syncVideo);
    video.addEventListener('play', syncVideo);
    videoToggle.addEventListener('click', function () {
      if (video.paused) video.play().catch(syncVideo);
      else video.pause();
    });
    reducedMotion.addEventListener('change', respectMotion);
    respectMotion();
    syncVideo();
  }
})();
