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
  function tween(el, to, fmt, from) {
    var t0 = performance.now(), dur = 900, a = from || 0;
    (function step(t) {
      var p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3);
      el.textContent = fmt(Math.round(a + (to - a) * e));
      if (p < 1) requestAnimationFrame(step);
    })(t0);
  }
  // Числа сводки набегают при загрузке страницы.
  function statsIn() {
    all('.stat-num').forEach(function (el) {
      var to = parseInt(el.textContent, 10);
      if (to) tween(el, to, String);
    });
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
    // title заменяем своим тултипом (data-t → ::after), иначе подсказок две
    all('.col').forEach(function (c, i) {
      c.style.setProperty('--i', i);
      c.dataset.t = c.title; c.removeAttribute('title');
    });
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
  var CARD = '.task, .event, .chart-block, .day-picker, .new-task-form, .stat';
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

  // ---------- выполнение задачи без перезагрузки ----------
  // Перехватываем форму чекбокса: POST уходит fetch'ем (CSRF — в самой форме),
  // карточка отмечается, из кружка вылетают частицы, затем она схлопывается.
  // Любой сбой, повторяющаяся задача (после её выполнения список меняется
  // сложнее) или не-ОК от сервера — обычная отправка формы, как без скрипта.
  var COLORS = ['var(--accent)', 'var(--green)', 'var(--orange)'];
  function burst(btn) {
    var r = btn.getBoundingClientRect(), cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    for (var i = 0; i < 12; i++) {
      var p = d.createElement('span'), a = (i / 12) * Math.PI * 2 + Math.random() * 0.5, dist = 26 + Math.random() * 34;
      p.className = 'fx-p';
      p.style.cssText = 'left:' + cx + 'px;top:' + cy + 'px;background:' + COLORS[i % 3] +
        ';--dx:' + (Math.cos(a) * dist).toFixed(1) + 'px;--dy:' + (Math.sin(a) * dist).toFixed(1) + 'px';
      p.addEventListener('animationend', function () { this.remove(); });
      d.body.appendChild(p);
    }
  }
  function collapse(card) {
    var h = card.offsetHeight;
    card.style.overflow = 'hidden';
    var an = card.animate([
      {height: h + 'px', opacity: 1},
      {height: h + 'px', opacity: 0.6, offset: 0.25},
      {height: '0px', opacity: 0, marginBottom: '0px', paddingTop: '0px', paddingBottom: '0px', borderWidth: '0px'}
    ], {duration: 480, easing: 'cubic-bezier(0.22, 0.8, 0.2, 1)', fill: 'forwards', delay: 260});
    an.onfinish = function () { card.remove(); };
  }
  // Точные числа сводки берём с сервера (одна лёгкая GET) и плавно докручиваем.
  function refreshStats() {
    fetch(location.href, {credentials: 'same-origin'}).then(function (r) { return r.ok ? r.text() : ''; })
      .then(function (html) {
        if (!html) return;
        var doc = new DOMParser().parseFromString(html, 'text/html');
        ['today', 'overdue', 'streak'].forEach(function (k) {
          var now = d.querySelector('.stat-' + k + ' .stat-num'), next = doc.querySelector('.stat-' + k + ' .stat-num');
          if (!now || !next) return;
          var from = parseInt(now.textContent, 10) || 0, to = parseInt(next.textContent, 10) || 0;
          if (from !== to) tween(now, to, String, from);
        });
      }).catch(function () {});
  }
  function completion() {
    d.addEventListener('submit', function (e) {
      var f = e.target, card = f.parentNode, btn = f.querySelector && f.querySelector('.check-btn');
      if (!btn || !card.classList || !card.classList.contains('task') || card.dataset.busy) return;
      if (card.querySelector('.meta.rep') || !/\/(done|undone)$/.test(f.action)) return;
      e.preventDefault();
      card.dataset.busy = '1';
      var undo = /undone$/.test(f.action);
      fetch(f.action, {method: 'POST', body: new FormData(f), credentials: 'same-origin', redirect: 'manual'})
        .then(function (r) {
          if (r.type !== 'opaqueredirect' && !r.ok) throw new Error(r.status);
          card.classList.toggle('done', !undo);
          card.classList.add('fx-done');
          if (!undo) burst(btn);
          collapse(card);
          refreshStats();
        })
        .catch(function () { delete card.dataset.busy; f.submit(); });
    });
  }

  // ---------- клавиатура ----------
  // n — новая задача, j/k — по карточкам, x — выполнить, ? — подсказка,
  // ←/→ — листать фото в открытом лайтбоксе. В полях ввода не срабатывает.
  var HELP = {
    ru: ['n — новая задача', 'j / k — вниз / вверх по списку', 'x — выполнить / вернуть', '← / → — фото в просмотре', '? — эта подсказка'],
    en: ['n — new task', 'j / k — next / previous task', 'x — complete / restore', '← / → — photos in viewer', '? — this help'],
    pl: ['n — nowe zadanie', 'j / k — w dół / w górę listy', 'x — wykonaj / przywróć', '← / → — zdjęcia w podglądzie', '? — ta pomoc']
  };
  function toggleHelp() {
    var old = d.querySelector('.fx-help');
    if (old) { old.remove(); return; }
    var box = d.createElement('div');
    box.className = 'fx-help';
    box.setAttribute('role', 'dialog');
    box.innerHTML = '<ul>' + (HELP[root.lang] || HELP.en).map(function (l) { return '<li>' + l + '</li>'; }).join('') + '</ul>';
    box.addEventListener('click', function () { box.remove(); });
    d.body.appendChild(box);
  }
  function moveFocus(step) {
    var list = all('.task-summary'), cur = d.activeElement && d.activeElement.closest && d.activeElement.closest('.task-summary');
    var i = list.indexOf(cur), next = list[i < 0 ? (step > 0 ? 0 : list.length - 1) : i + step];
    if (next) { next.focus(); next.scrollIntoView({block: 'center', behavior: 'smooth'}); }
  }
  function lightboxStep(step) {
    var lb = d.getElementById('ph-lb'), img = lb && lb.querySelector('img');
    if (!img || !lb.classList.contains('open')) return false;
    var frames = all('.task-photo[data-full]'), i = -1;
    frames.forEach(function (f, k) { if (img.src === f.dataset.full || img.src === (f.querySelector('img') || {}).src) i = k; });
    var next = frames[i + step];
    if (next) img.src = next.dataset.full;
    return true;
  }
  function keyboard() {
    d.addEventListener('keydown', function (e) {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      var t = e.target;
      if (t && t.matches && t.matches('input, textarea, select, [contenteditable]')) return;
      if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
        if (lightboxStep(e.key === 'ArrowRight' ? 1 : -1)) e.preventDefault();
        return;
      }
      if (e.key === 'Escape') { var h = d.querySelector('.fx-help'); if (h) h.remove(); return; }
      if (e.key === '?') toggleHelp();
      else if (e.key === 'n') {
        var sheet = d.querySelector('.add-sheet');
        if (!sheet) return;
        e.preventDefault(); sheet.open = true;
        var f = sheet.querySelector('input[name=title]'); if (f) f.focus();
      }
      else if (e.key === 'j') moveFocus(1);
      else if (e.key === 'k') moveFocus(-1);
      else if (e.key === 'x') {
        var card = t && t.closest && t.closest('.task'), b = card && card.querySelector('.check-btn');
        if (b) b.click();
      }
    });
  }

  function init() { splitTitle(); reveal(); pointer(); statsIn(); completion(); keyboard(); }
  // Скрипт подгружается из <head> и может отработать до разбора <body>.
  if (d.readyState === 'loading') d.addEventListener('DOMContentLoaded', init);
  else init();
})();
