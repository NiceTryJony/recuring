// Эффекты для ПК. Подключается детектором из pages.py только когда на <html>
// стоит класс fx (мышь с hover, широкий экран, >= 8 ГБ, >= 6 потоков, нет
// reduced-motion) — на телефонах файл не скачивается вовсе. Скрытое начальное
// состояние (.rv) ставится только здесь: не загрузился скрипт — всё на месте.
(function () {
  'use strict';
  var root = document.documentElement;
  if (!root.classList.contains('fx')) return;
  var d = document;

  function all(sel, scope) { return Array.prototype.slice.call((scope || d).querySelectorAll(sel)); }

  // ---------- большой заголовок: режем на слова ----------
  // Исходный текст остаётся читателю экрана в aria-label, слова — декорация.
  function splitTitle() {
    var h = d.querySelector('.large-title');
    if (!h || h.classList.contains('sp')) return;
    var words = h.textContent.trim().split(/\s+/);
    h.setAttribute('aria-label', words.join(' '));
    h.textContent = '';
    words.forEach(function (w, i) {
      var s = d.createElement('span');
      s.className = 'w'; s.textContent = w;
      s.setAttribute('aria-hidden', 'true');
      s.style.setProperty('--i', i);
      if (i) h.appendChild(d.createTextNode(' '));
      h.appendChild(s);
    });
    h.classList.add('sp');
  }

  // ---------- числа «набегают» ----------
  function tween(el, to, fmt) {
    var t0 = performance.now(), dur = 900;
    (function step(t) {
      var p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3);
      el.textContent = fmt(Math.round(to * e));
      if (p < 1) requestAnimationFrame(step);
    })(t0);
  }
  function countUp(scope) {
    all('.num', scope).forEach(function (el) {
      var to = parseInt(el.textContent, 10);
      if (to) tween(el, to, String);
    });
    var st = scope.querySelector('.streak');
    var m = st && /\d+/.exec(st.textContent);
    if (m) {
      var txt = st.textContent;
      tween(st, parseInt(m[0], 10), function (n) { return txt.replace(/\d+/, n); });
    }
  }

  // ---------- появление при прокрутке ----------
  // Один наблюдатель на страницу. Элементы, показанные одной пачкой, получают
  // нарастающую задержку --d (до 8 шагов) — список «выстраивается».
  function reveal() {
    if (!('IntersectionObserver' in window)) return;
    all('.col').forEach(function (c, i) { c.style.setProperty('--i', i); });
    var io = new IntersectionObserver(function (entries) {
      var n = 0;
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        var el = e.target;
        io.unobserve(el);
        el.style.setProperty('--d', Math.min(n++, 8));
        el.classList.add('in');
        if (el.classList.contains('chart-block')) countUp(el);
      });
    }, {threshold: 0.05});
    all('.task, .event, .chart-block, .day-picker, .new-task-form, .tg-widget-banner, .load-more')
      .forEach(function (el) { el.classList.add('rv'); io.observe(el); });
  }

  // ---------- указатель: подсветка карточки, наклон фото, «магнит» кнопки ----------
  // Один делегированный pointermove на документ, работа раз в кадр и только для
  // элемента под курсором.
  var CARD = '.task, .event, .chart-block, .day-picker, .new-task-form';
  var raf = 0, last = null, prevPhoto = null, prevBtn = null;

  function clearVars(el, names) {
    if (el) names.forEach(function (n) { el.style.removeProperty(n); });
  }
  function frame() {
    raf = 0;
    var e = last, t = e.target;
    if (!t || !t.closest) return;

    var card = t.closest(CARD);
    if (card) {
      var r = card.getBoundingClientRect();
      card.style.setProperty('--mx', (e.clientX - r.left) + 'px');
      card.style.setProperty('--my', (e.clientY - r.top) + 'px');
    }

    var ph = t.closest('.task-photo');
    if (ph !== prevPhoto) { clearVars(prevPhoto, ['--rx', '--ry']); prevPhoto = ph; }
    if (ph) {
      var pr = ph.getBoundingClientRect();
      ph.style.setProperty('--ry', (((e.clientX - pr.left) / pr.width - 0.5) * 8).toFixed(2) + 'deg');
      ph.style.setProperty('--rx', ((0.5 - (e.clientY - pr.top) / pr.height) * 8).toFixed(2) + 'deg');
    }

    var b = t.closest('.btn-filled');
    if (b !== prevBtn) { clearVars(prevBtn, ['--tx', '--ty']); prevBtn = b; }
    if (b) {
      var br = b.getBoundingClientRect();
      var clamp = function (v) { return Math.max(-6, Math.min(6, v)); };
      b.style.setProperty('--tx', clamp((e.clientX - br.left - br.width / 2) / (br.width / 2) * 6).toFixed(1) + 'px');
      b.style.setProperty('--ty', clamp((e.clientY - br.top - br.height / 2) / (br.height / 2) * 6).toFixed(1) + 'px');
    }
  }
  function pointer() {
    d.addEventListener('pointermove', function (e) {
      if (e.pointerType !== 'mouse') return;
      last = e;
      if (!raf) raf = requestAnimationFrame(frame);
    }, {passive: true});
    // Мышь ушла из окна — возвращаем фото и кнопку на место.
    root.addEventListener('mouseleave', function () {
      clearVars(prevPhoto, ['--rx', '--ry']); clearVars(prevBtn, ['--tx', '--ty']);
      prevPhoto = prevBtn = null;
    });
  }

  function init() { splitTitle(); reveal(); pointer(); }
  // Скрипт подгружается из <head> и может отработать до разбора <body>.
  if (d.readyState === 'loading') d.addEventListener('DOMContentLoaded', init);
  else init();
})();
