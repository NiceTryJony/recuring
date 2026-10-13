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

  // ---------- указатель ----------
  // Один pointermove на документ, один requestAnimationFrame на кадр и только
  // transform/opacity: подсветка сетки (.fx-hot), пятна света карточек (.fx-sp b),
  // наклон карточек и «магнит» кнопки «+» не вызывают ни пересчёта стилей всей
  // страницы, ни перерисовки. Размеры карточек берутся из кэша (сбрасывается при
  // прокрутке и ресайзе), а не читаются на каждое движение. Пятна света создаются
  // лениво — при первом наведении, так что карточки вне курсора ничего не стоят.
  var CARD = '.task, .event, .chart-block, .day-picker, .new-task-form, .stat';
  var TILT = '.task, .stat';
  var raf = 0, px = -999, py = -999, tgt = null, rects = new WeakMap(), fabRect = null;
  var tiltEl = null, prevPhoto = null, prevBtn = null;
  var hot = d.createElement('div'), hotIn = d.createElement('i');
  hot.className = 'fx-hot'; hot.setAttribute('aria-hidden', 'true'); hot.appendChild(hotIn);
  var fab = null, fabGlow = null;

  function clearVars(el, names) {
    if (el) names.forEach(function (n) { el.style.removeProperty(n); });
  }
  function rectOf(el) {
    var r = rects.get(el);
    if (!r) { r = el.getBoundingClientRect(); rects.set(el, r); }
    return r;
  }
  function mod(a, m) { return ((a % m) + m) % m; }
  function untilt(el) { if (el) { el.style.transform = ''; el.classList.remove('tilting'); } }
  function ensureSpots(card) {
    if (card.dataset.sp) return;
    card.dataset.sp = '1';
    card.insertAdjacentHTML('afterbegin', '<i class="fx-sp"><b></b></i><i class="fx-sp fx-ring"><b></b></i>');
  }
  function clampTo(v, m) { return Math.max(-m, Math.min(m, v)); }

  function frame() {
    raf = 0;
    var t = tgt;
    // сетка: слой двигается целиком, линии внутри сдвигаются обратно на остаток от шага 56px
    if (px > -900) {
      var ax = px - 260, ay = py - 260;
      hot.style.transform = 'translate3d(' + ax + 'px,' + ay + 'px,0)';
      hotIn.style.transform = 'translate3d(' + (-mod(ax, 56)) + 'px,' + (-mod(ay, 56)) + 'px,0)';
      hot.classList.add('on');
    } else hot.classList.remove('on');

    var card = t && t.closest ? t.closest(CARD) : null;
    if (card) {
      ensureSpots(card);
      var q = rectOf(card), tr = 'translate3d(' + (px - q.left).toFixed(1) + 'px,' + (py - q.top).toFixed(1) + 'px,0)';
      var b1 = card.querySelector(':scope > .fx-sp > b'), b2 = card.querySelector(':scope > .fx-sp.fx-ring > b');
      if (b1) b1.style.transform = tr;
      if (b2) b2.style.transform = tr;
    }
    // наклон
    var tilt = t && t.closest ? t.closest(TILT) : null;
    if (tiltEl && tiltEl !== tilt) untilt(tiltEl);
    tiltEl = tilt;
    if (tilt && !tilt.matches('.task.pending')) {
      var r = rectOf(tilt), k = tilt.classList.contains('task') ? 3 : 7;
      tilt.classList.add('tilting');
      tilt.style.transform = 'perspective(900px) rotateX(' + (-((py - r.top) / r.height - 0.5) * k).toFixed(2) +
        'deg) rotateY(' + (((px - r.left) / r.width - 0.5) * k).toFixed(2) + 'deg) translateY(-2px)';
    }
    // кнопка «+»
    if (fab) {
      var fr = fabRect || (fabRect = fab.getBoundingClientRect());
      var cx = fr.left + fr.width / 2, cy = fr.top + fr.height / 2, dx = px - cx, dy = py - cy, dist = Math.hypot(dx, dy), R = 150;
      if (dist < R) {
        var kk = 1 - dist / R;
        fab.style.transform = 'translate3d(' + clampTo(dx * 0.44 * kk, 10).toFixed(1) + 'px,' + clampTo(dy * 0.44 * kk, 10).toFixed(1) + 'px,0)';
        fabGlow.style.transform = 'translate3d(' + (cx + dx * 0.6).toFixed(1) + 'px,' + (cy + dy * 0.6).toFixed(1) + 'px,0)';
        fabGlow.style.opacity = kk.toFixed(2);
      } else { fab.style.transform = ''; fabGlow.style.opacity = 0; }
    }

    var ph = t && t.closest ? t.closest('.task-photo') : null;
    if (ph !== prevPhoto) { clearVars(prevPhoto, ['--rx', '--ry']); prevPhoto = ph; }
    if (ph) {
      var pr = ph.getBoundingClientRect();
      ph.style.setProperty('--ry', (((px - pr.left) / pr.width - 0.5) * 8).toFixed(2) + 'deg');
      ph.style.setProperty('--rx', ((0.5 - (py - pr.top) / pr.height) * 8).toFixed(2) + 'deg');
    }
    var b = t && t.closest ? t.closest('.btn-filled') : null;
    if (b !== prevBtn) { clearVars(prevBtn, ['--tx', '--ty']); prevBtn = b; }
    if (b) {
      var br = b.getBoundingClientRect();
      b.style.setProperty('--tx', clampTo((px - br.left - br.width / 2) / (br.width / 2) * 6, 6).toFixed(1) + 'px');
      b.style.setProperty('--ty', clampTo((py - br.top - br.height / 2) / (br.height / 2) * 6, 6).toFixed(1) + 'px');
    }
  }
  function schedule() { if (!raf) raf = requestAnimationFrame(frame); }
  function pointer() {
    d.body.appendChild(hot);
    var summary = d.querySelector('.add-sheet > summary');
    if (summary) {
      fab = summary;
      fabGlow = d.createElement('div');
      fabGlow.className = 'fx-fabglow'; fabGlow.setAttribute('aria-hidden', 'true');
      d.body.appendChild(fabGlow);
    }
    d.addEventListener('pointermove', function (e) {
      if (e.pointerType !== 'mouse') return;
      px = e.clientX; py = e.clientY; tgt = e.target;
      schedule();
    }, {passive: true});
    // Мышь ушла из окна — всё возвращается на место.
    root.addEventListener('mouseleave', function () {
      px = py = -999; tgt = null;
      untilt(tiltEl); tiltEl = null;
      clearVars(prevPhoto, ['--rx', '--ry']); clearVars(prevBtn, ['--tx', '--ty']);
      prevPhoto = prevBtn = null;
      schedule();
    });
    addEventListener('scroll', function () { rects = new WeakMap(); }, {passive: true});
    addEventListener('resize', function () { rects = new WeakMap(); fabRect = null; }, {passive: true});
  }

  // ---------- выполнение задачи: эффект частиц ----------
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
  // Волна света по карточке и всплеск частиц при выполнении; когда закрыта последняя
  // задача дня (прогресс «сегодня» дошёл до 100) — один раз конфетти. Само действие
  // (fetch, схлопывание карточки, сводка) живёт в dashboard.js для всех устройств и
  // сообщает событием task:done; сводка обновляется чуть позже события — читаем её с задержкой.
  function wave(card) {
    var w = d.createElement('i');
    w.className = 'fx-wave'; w.setAttribute('aria-hidden', 'true'); w.innerHTML = '<b></b>';
    card.appendChild(w);
    setTimeout(function () { w.remove(); }, 1000);
  }
  function progress() {
    var st = d.querySelector('.stat-today');
    return st ? parseFloat(st.style.getPropertyValue('--p')) || 0 : 0;
  }
  var prevProgress = 0;
  function confetti(x, y) {
    var cv = d.createElement('canvas'), dpr = Math.min(2, window.devicePixelRatio || 1), W = innerWidth, H = innerHeight;
    cv.width = W * dpr; cv.height = H * dpr;
    cv.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;z-index:2500;pointer-events:none';
    d.body.appendChild(cv);
    var g = cv.getContext('2d'), cs = getComputedStyle(root), ps = [], i;
    g.scale(dpr, dpr);
    var cols = ['--accent', '--orange', '--red', '--label'].map(function (n) { return cs.getPropertyValue(n).trim() || '#888'; });
    for (i = 0; i < 90; i++) {
      var a = -Math.PI / 2 + (Math.random() - 0.5) * 2.2, v = 7 + Math.random() * 9;
      ps.push({x: x, y: y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, w: 5 + Math.random() * 5, h: 3 + Math.random() * 4,
               r: Math.random() * 6, vr: (Math.random() - 0.5) * 0.4, c: cols[i % cols.length]});
    }
    var f = 0;
    (function tick() {
      f++; g.clearRect(0, 0, W, H);
      ps.forEach(function (p) {
        p.vx *= 0.985; p.vy = p.vy * 0.985 + 0.36; p.x += p.vx; p.y += p.vy; p.r += p.vr;
        g.save(); g.globalAlpha = Math.max(0, 1 - f / 120); g.translate(p.x, p.y); g.rotate(p.r);
        g.fillStyle = p.c; g.fillRect(-p.w / 2, -p.h / 2, p.w, p.h); g.restore();
      });
      if (f < 130 && !d.hidden) requestAnimationFrame(tick); else cv.remove();
    })();
  }
  function completion() {
    prevProgress = progress();
    d.addEventListener('task:done', function (e) {
      var btn = e.detail && e.detail.btn;
      if (!btn) return;
      burst(btn);
      var card = btn.closest('.task');
      if (card) wave(card);
      setTimeout(function () {
        var p = progress();
        if (p >= 100 && prevProgress < 100) {
          var r = btn.getBoundingClientRect();
          confetti(r.left + r.width / 2, r.top + r.height / 2);
        }
        prevProgress = p;
      }, 250);
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

  // ---------- палитра команд: Ctrl/Cmd + K ----------
  // Ничего не запрашивает у сервера: пункты берутся из страницы — карточки задач,
  // вкладки видов и ссылки навигации. «Создать» открывает обычную форму новой
  // задачи с введённым названием (срок всё равно выбирает пользователь).
  var PAL_TXT = {
    ru: {ph: 'Название задачи, поиск или раздел', acts: 'Действия', tasks: 'Задачи', nav: 'Разделы', create: 'Создать задачу', none: 'Ничего не найдено', go: 'Перейти'},
    en: {ph: 'Task title, search or section', acts: 'Actions', tasks: 'Tasks', nav: 'Sections', create: 'Create task', none: 'Nothing found', go: 'Go'},
    pl: {ph: 'Tytuł zadania, szukaj lub sekcja', acts: 'Akcje', tasks: 'Zadania', nav: 'Sekcje', create: 'Utwórz zadanie', none: 'Nic nie znaleziono', go: 'Przejdź'}
  };
  var pal = null, palIn = null, palList = null, palItems = [], palSel = 0;
  function palText() { return PAL_TXT[root.lang] || PAL_TXT.en; }
  function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;'); }
  function palCollect(q) {
    var T = palText(), ql = q.trim().toLowerCase(), L = [];
    if (ql) L.push({g: T.acts, t: T.create + ': «' + q.trim() + '»', h: 'Enter', run: function () {
      var sheet = d.querySelector('.add-sheet');
      if (!sheet) return;
      sheet.open = true;
      var f = sheet.querySelector('input[name=title]');
      if (f) { f.value = q.trim(); f.focus(); }
    }});
    var seen = {};
    all('.navbar-right a, .view-tab, .tabbar .tab').forEach(function (a) {
      var t = a.textContent.trim();
      if (!t || seen[a.href] || (ql && t.toLowerCase().indexOf(ql) < 0)) return;
      seen[a.href] = 1;
      L.push({g: T.nav, t: t, h: T.go, run: function () { location.href = a.href; }});
    });
    if (ql) all('.task').forEach(function (card) {
      var te = card.querySelector('.title');
      if (!te || te.textContent.toLowerCase().indexOf(ql) < 0) return;
      L.push({g: T.tasks, t: te.textContent.trim(), h: '', run: function () {
        var s = card.querySelector('.task-summary');
        card.scrollIntoView({block: 'center', behavior: 'smooth'});
        if (s) s.focus({preventScroll: true});
      }});
    });
    return L;
  }
  function palRender() {
    palItems = palCollect(palIn.value);
    if (palSel >= palItems.length) palSel = 0;
    var h = '', last = '';
    palItems.forEach(function (it, i) {
      if (it.g !== last) { h += '<div class="fx-pal-h">' + esc(it.g) + '</div>'; last = it.g; }
      h += '<button class="fx-pal-i' + (i === palSel ? ' on' : '') + '" type="button" data-i="' + i + '"><span>' + esc(it.t) + '</span><small>' + esc(it.h) + '</small></button>';
    });
    palList.innerHTML = h || '<div class="fx-pal-empty">' + palText().none + '</div>';
    var on = palList.querySelector('.on');
    if (on) on.scrollIntoView({block: 'nearest'});
  }
  function palOpen() {
    if (!pal) {
      pal = d.createElement('div');
      pal.className = 'fx-pal-back';
      pal.innerHTML = '<div class="fx-pal" role="dialog" aria-modal="true"><input type="text" autocomplete="off" spellcheck="false"><div class="fx-pal-list"></div></div>';
      palIn = pal.querySelector('input'); palList = pal.querySelector('.fx-pal-list');
      palIn.placeholder = palText().ph; palIn.setAttribute('aria-label', palText().ph);
      palIn.addEventListener('input', function () { palSel = 0; palRender(); });
      palList.addEventListener('mousemove', function (e) {
        var b = e.target.closest('.fx-pal-i');
        if (b && +b.dataset.i !== palSel) {
          palSel = +b.dataset.i;
          all('.fx-pal-i', palList).forEach(function (x) { x.classList.toggle('on', +x.dataset.i === palSel); });
        }
      });
      palList.addEventListener('click', function (e) { var b = e.target.closest('.fx-pal-i'); if (b) palRun(+b.dataset.i); });
      pal.addEventListener('mousedown', function (e) { if (e.target === pal) palClose(); });
      d.body.appendChild(pal);
    }
    palIn.value = ''; palSel = 0; palRender();
    pal.classList.add('open');
    setTimeout(function () { palIn.focus(); }, 30);
  }
  function palClose() { if (pal) { pal.classList.remove('open'); palIn.blur(); } }
  function palRun(i) { var it = palItems[i]; if (!it) return; palClose(); setTimeout(it.run, 120); }
  function palette() {
    d.addEventListener('keydown', function (e) {
      var open = pal && pal.classList.contains('open');
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); if (open) palClose(); else palOpen(); return; }
      if (!open) return;
      if (e.key === 'Escape') palClose();
      else if (e.key === 'ArrowDown') { e.preventDefault(); palSel = (palSel + 1) % Math.max(1, palItems.length); palRender(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); palSel = (palSel - 1 + palItems.length) % Math.max(1, palItems.length); palRender(); }
      else if (e.key === 'Enter') { e.preventDefault(); palRun(palSel); }
    }, true);
  }

  function init() { splitTitle(); reveal(); pointer(); statsIn(); completion(); keyboard(); palette(); }
  // Скрипт подгружается из <head> и может отработать до разбора <body>.
  if (d.readyState === 'loading') d.addEventListener('DOMContentLoaded', init);
  else init();
})();
