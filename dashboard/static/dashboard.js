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
  var lb = document.getElementById('ph-lb');
  var lbImg = lb && lb.querySelector('img');
  function closeLightbox() {
    if (!lb) return;
    lb.classList.remove('open');
    lbImg.removeAttribute('src');
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
      lbImg.src = frame.dataset.full;
      lb.classList.add('open');
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
