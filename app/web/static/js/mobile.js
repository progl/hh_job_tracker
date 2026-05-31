// Мобильная навигация (гамбургер) и переключатель вида таблицы/карточек.

window.toggleMobileNav = () => {
  const m = document.getElementById('mobile-nav');
  const ic = document.getElementById('mnav-icon');
  if (!m) return;
  const open = m.classList.contains('hidden');
  m.classList.toggle('hidden', !open);
  if (ic) ic.textContent = open ? '✕' : '☰';
};

window.applyViewMode = (mode) => {
  const tbl = document.getElementById('vac-table');
  const icon = document.getElementById('view-mode-icon');
  const lbl = document.getElementById('view-mode-label');
  if (!tbl) return;
  if (mode === 'cards') {
    tbl.classList.add('view-mode-cards');
    if (icon) icon.textContent = '☰';
    if (lbl) lbl.textContent = 'карточки';
  } else {
    tbl.classList.remove('view-mode-cards');
    if (icon) icon.textContent = '▦';
    if (lbl) lbl.textContent = 'таблица';
  }
};

window.toggleViewMode = () => {
  const tbl = document.getElementById('vac-table');
  if (!tbl) return;
  const next = tbl.classList.contains('view-mode-cards') ? 'table' : 'cards';
  try { localStorage.setItem('viewMode', next); } catch(e){}
  window.applyViewMode(next);
};

(function() {
  // Инициализация вида: либо сохранённый выбор, либо автоматический по ширине.
  if (!document.getElementById('vac-table')) return;
  let saved = null;
  try { saved = localStorage.getItem('viewMode'); } catch(e){}
  if (!saved) {
    saved = window.matchMedia('(max-width: 767px)').matches ? 'cards' : 'table';
  }
  window.applyViewMode(saved);
})();
