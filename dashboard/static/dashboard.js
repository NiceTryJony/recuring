// JS страниц дашборда. Один кешируемый файл (immutable, хеш в имени) вместо
// инлайновых <script> и onclick-атрибутов, которые раньше повторялись в HTML
// каждой страницы и каждой карточки. Подключается с defer: к моменту запуска
// DOM уже разобран.
(function () {
  'use strict';

  // ---------- ленивое раскрытие карточек ----------
  // Содержимое раскрытой карточки (описание, формы, подзадачи, фото) сервер
  // кладёт в <template class="task-x">: браузер разбирает его в инертный
  // фрагмент — без стилей, layout, отрисовки и без регистрации форм. Живым
  // DOM оно становится только при первом раскрытии конкретной карточки.
  // На длинном списке это в разы меньше узлов, которые слабому телефону
  // приходится стилизовать и держать в памяти.
  function hydrate(details) {
    var tpl = details.querySelector(':scope > template.task-x');
    if (tpl) details.replaceChild(tpl.content.cloneNode(true), tpl);
  }

  // Вставляем синхронно — до того, как <details> откроется: на pointerdown
  // (палец ещё на экране, до click обычно десятки миллисекунд — успеваем
  // заранее) и на click по summary (клавиатура тоже даёт click). Событие
  // toggle приходит уже ПОСЛЕ открытия, отдельной задачей — вставка в нём
  // стоила бы лишнего кадра с пустой раскрытой карточкой и второго
  // пересчёта стилей; оно остаётся только запасным путём.
  function hydrateFromSummary(e) {
    var s = e.target.closest && e.target.closest('.task-summary');
    if (s && s.parentElement) hydrate(s.parentElement);
  }
  document.addEventListener('pointerdown', hydrateFromSummary, {capture: true, passive: true});
  document.addEventListener('click', hydrateFromSummary, true);

  // toggle не всплывает — ловим на фазе перехвата.
  document.addEventListener('toggle', function (e) {
    var d = e.target;
    if (d.open && d.classList && d.classList.contains('task-details')) hydrate(d);
  }, true);

  // После действий внутри карточки (описание, подзадачи) сервер редиректит на
  // #task-N — раскрываем эту карточку, чтобы человек вернулся туда, где был.
  function openFromHash() {
    var m = /^#task-(\d+)$/.exec(location.hash);
    if (!m) return;
    var card = document.getElementById('task-' + m[1]);
    var details = card && card.querySelector('.task-details');
    if (details) { hydrate(details); details.open = true; }
  }
  openFromHash();
  window.addEventListener('hashchange', openFromHash);

  // ---------- фото: лайтбокс и стрелки карусели ----------
  // Один оверлей на всю страницу и один делегированный обработчик вместо
  // inline-onclick на каждом кадре. Полный кадр (без ?size=thumb)
  // запрашивается только здесь, по тапу.
  //
  // На слабых устройствах — просто кроссфейд (CSS). На .mid (см. детектор в
  // pages.py) — как «Фото» в iOS: кадр вырастает из миниатюры и возвращается
  // в неё же, а свайп вниз тащит его 1:1 за пальцем и закрывает с той же
  // скоростью, с которой палец его отпустил.
  var lb = document.getElementById('ph-lb');
  var lbImg = lb && lb.querySelector('img');
  var fluid = document.documentElement.classList.contains('mid') && !!(lb && lb.animate);
  var EASE = 'cubic-bezier(0.32, 0.72, 0, 1)';
  var origin = null;      // миниатюра, из которой открыли
  var closing = false;

  // Прямоугольник миниатюры -> transform, который ставит кадр на её место.
  function toRect(r) {
    var b = lbImg.getBoundingClientRect();   // кадр без transform
    if (!r || !r.width || !b.width) return null;
    var s = r.width / b.width;
    return 'translate(' + (r.left + r.width / 2 - b.left - b.width / 2) + 'px,' +
           (r.top + r.height / 2 - b.top - b.height / 2) + 'px) scale(' + s + ')';
  }
  function scrim(alpha) { lb.style.backgroundColor = 'rgba(0,0,0,' + (0.94 * alpha) + ')'; }

  function openLightbox(frame) {
    origin = frame;
    closing = false;
    if (!fluid) {
      lbImg.src = frame.dataset.full;
      lb.classList.add('open');
      return;
    }
    // Сначала уже закешированное превью (размер известен сразу — есть от чего
    // считать путь из миниатюры), полный кадр подменяет его, когда приедет.
    var thumb = frame.querySelector('img');
    lbImg.src = thumb.currentSrc || thumb.src;
    lb.classList.add('open');
    var full = new Image();
    full.onload = function () { if (origin === frame && !closing) lbImg.src = full.src; };
    full.src = frame.dataset.full;
    var from = toRect(thumb.getBoundingClientRect());
    if (from) lbImg.animate([{transform: from}, {transform: 'none'}], {duration: 420, easing: EASE});
    lb.animate([{backgroundColor: 'rgba(0,0,0,0)'}, {}], {duration: 300, easing: 'ease-out'});
  }

  function finishClose() {
    lb.classList.remove('open');
    lb.style.backgroundColor = '';
    lbImg.style.transform = '';
    lbImg.removeAttribute('src');
    origin = null;
    closing = false;
  }

  // fromTransform — текущее (видимое) положение кадра: закрытие начинается
  // оттуда, где кадр сейчас, а не с «логической» позиции.
  function closeLightbox(fromTransform, fromAlpha) {
    if (!lb || !lb.classList.contains('open') || closing) return;
    if (!fluid) { finishClose(); return; }
    closing = true;
    var thumb = origin && origin.querySelector('img');
    lbImg.style.transform = '';
    var to = thumb && toRect(thumb.getBoundingClientRect());
    var start = fromTransform || 'none';
    // Миниатюра не видна (карточку свернули) — уезжаем вниз, в сторону жеста.
    var frames = to ? [{transform: start}, {transform: to}]
                    : [{transform: start, opacity: 1}, {transform: 'translateY(60vh)', opacity: 0}];
    var a = lbImg.animate(frames, {duration: 340, easing: EASE, fill: 'forwards'});
    lb.animate([{backgroundColor: 'rgba(0,0,0,' + (0.94 * (fromAlpha == null ? 1 : fromAlpha)) + ')'},
                {backgroundColor: 'rgba(0,0,0,0)'}], {duration: 300, easing: 'ease-out', fill: 'forwards'});
    a.onfinish = function () {
      lb.getAnimations().forEach(function (x) { x.cancel(); });
      a.cancel();
      finishClose();
    };
  }

  // ---------- свайп вниз ----------
  if (fluid) {
    var drag = null, dy = 0, raf = 0, dragged = false;

    // Сопротивление у границы: чем дальше за край, тем меньше кадр следует за пальцем.
    function rubberband(x, dim) { return (x * dim * 0.55) / (dim + 0.55 * Math.abs(x)); }
    // Куда «докатится» кадр с такой скоростью — функция Apple из Designing Fluid Interfaces.
    function project(v) { return (v / 1000) * 0.998 / (1 - 0.998); }
    function apply(y) {
      dy = y;
      var p = Math.max(0, Math.min(1, y / (innerHeight * 0.6)));
      lbImg.style.transform = 'translateY(' + y + 'px) scale(' + (1 - p * 0.2) + ')';
      scrim(1 - p);
    }
    // Критически затухающая пружина (damping 1.0, response 0.35 c) с начальной
    // скоростью пальца: x(t) = (x0 + (v0 + w·x0)·t)·e^(-w·t).
    function springBack(v0) {
      var x0 = dy, w = 2 * Math.PI / 0.35, t0 = performance.now();
      cancelAnimationFrame(raf);
      (function step(now) {
        var t = (now - t0) / 1000, x = (x0 + (v0 + w * x0) * t) * Math.exp(-w * t);
        if (t > 1.5 || (Math.abs(x) < 0.5 && t > 0.05)) { apply(0); lbImg.style.transform = ''; lb.style.backgroundColor = ''; return; }
        apply(x);
        raf = requestAnimationFrame(step);
      })(t0);
    }

    lb.addEventListener('pointerdown', function (e) {
      if (closing || e.target.closest('.photo-lightbox-close') || (e.pointerType === 'mouse' && e.button !== 0)) return;
      cancelAnimationFrame(raf);   // схватили на лету — продолжаем с текущего места
      lbImg.getAnimations().forEach(function (x) { x.finish(); });
      lb.setPointerCapture(e.pointerId);
      drag = {id: e.pointerId, y0: e.clientY - dy, startY: e.clientY, on: false, hist: [{y: e.clientY, t: e.timeStamp}]};
      dragged = false;
    });
    lb.addEventListener('pointermove', function (e) {
      if (!drag || e.pointerId !== drag.id) return;
      if (!drag.on) {
        if (Math.abs(e.clientY - drag.startY) < 10) return;   // порог: это ещё тап, а не свайп
        drag.on = dragged = true;
        drag.y0 = e.clientY - dy;                           // без рывка на величину порога
      }
      var y = e.clientY - drag.y0;
      apply(y < 0 ? rubberband(y, innerHeight) : y);
      drag.hist.push({y: e.clientY, t: e.timeStamp});
      if (drag.hist.length > 6) drag.hist.shift();
    });
    function release(e) {
      if (!drag || e.pointerId !== drag.id) return;
      var h = drag.hist, a = h[0], b = h[h.length - 1];
      var v = b.t - a.t > 0 && e.timeStamp - b.t < 100 ? (b.y - a.y) / (b.t - a.t) * 1000 : 0;
      var wasOn = drag.on;
      drag = null;
      if (!wasOn) return;
      // Решение по месту, куда кадр докатился бы, а не по месту отпускания.
      if (e.type !== 'pointercancel' && dy + project(v) > innerHeight * 0.22) {
        closeLightbox(lbImg.style.transform, 1 - Math.max(0, Math.min(1, dy / (innerHeight * 0.6))));
        dy = 0;
      } else {
        springBack(v);
      }
    }
    lb.addEventListener('pointerup', release);
    lb.addEventListener('pointercancel', release);
    // Клик в конце свайпа — не «тап по фону».
    lb.addEventListener('click', function (e) { if (dragged) { dragged = false; e.stopPropagation(); } }, true);
  }

  document.addEventListener('click', function (e) {
    var t = e.target;
    var nav = t.closest('.photo-nav');
    if (nav) {
      // scrollBy на фиксированный шаг (один кадр) — явное, предсказуемое
      // перемещение по клику, никакого авто-таймера/зацикливания.
      var track = document.getElementById(nav.dataset.track);
      if (track) track.scrollBy({left: Number(nav.dataset.step), behavior: 'smooth'});
      return;
    }
    if (!lb) return;
    var frame = t.closest('.task-photo');
    // Клик по крестику удаления живёт внутри своей формы — лайтбокс не трогаем.
    if (frame && !t.closest('form')) {
      openLightbox(frame);
    } else if (t === lb || t.closest('.photo-lightbox-close')) {
      closeLightbox();
    }
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeLightbox();
  });

  // ---------- Telegram Login Widget ----------
  // Виджет зовёт глобальную onTelegramAuth(user). Данные от Telegram шлём на
  // наш же бэкенд, который сам перепроверяет подпись — самому виджету на
  // фронтенде доверять нельзя, это просто источник данных.
  window.onTelegramAuth = function (user) {
    var banner = document.querySelector('.tg-widget-banner');
    if (!banner) return;
    user._csrf = banner.dataset.csrf;
    fetch('/dashboard/' + banner.dataset.token + '/telegram-auth', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(user)
    }).then(function (r) { if (r.ok) location.reload(); });
  };
})();
