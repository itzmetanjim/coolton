(function () {
  'use strict';

  const sidebarResizer = document.getElementById('sidebarResizer');
  const STORAGE_KEY = 'coolton.web.sidebarW';
  const MIN_WIDTH = 190;
  const MAX_WIDTH = 480;

  try {
    const saved = Number(localStorage.getItem(STORAGE_KEY));
    if (saved >= MIN_WIDTH && saved <= MAX_WIDTH) {
      document.documentElement.style.setProperty('--sidebar-w', `${saved}px`);
    }
  } catch (e) { /* storage unavailable */ }

  let dragging = false;
  const stop = () => {
    if (!dragging) return;
    dragging = false;
    sidebarResizer.classList.remove('resizing');
    document.body.classList.remove('resizing-sidebar');
    const width = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--sidebar-w'), 10);
    if (width >= MIN_WIDTH && width <= MAX_WIDTH) {
      try { localStorage.setItem(STORAGE_KEY, String(width)); } catch (e) { }
    }
  };

  sidebarResizer.addEventListener('pointerdown', (event) => {
    dragging = true;
    event.preventDefault();
    sidebarResizer.classList.add('resizing');
    document.body.classList.add('resizing-sidebar');
    sidebarResizer.setPointerCapture(event.pointerId);
  });
  sidebarResizer.addEventListener('pointermove', (event) => {
    if (!dragging) return;
    const width = Math.round(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, event.clientX)));
    document.documentElement.style.setProperty('--sidebar-w', `${width}px`);
  });
  sidebarResizer.addEventListener('pointerup', stop);
  sidebarResizer.addEventListener('pointercancel', stop);
})();
