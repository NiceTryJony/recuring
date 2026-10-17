// JS страниц дашборда. Один кешируемый файл (immutable, хеш в имени) вместо
// инлайновых <script> и onclick-атрибутов, которые раньше повторялись в HTML
// каждой страницы и каждой карточки. Подключается с defer: к моменту запуска
// DOM уже разобран.
(function () {
  'use strict';

  // ---------- Telegram Mini App ----------
  // Вне Telegram initData пуст — блок не делает ничего. Внутри: флаг
  // html[data-tg] для стилей Mini App и тема Telegram как --tg-* переменные.
  var tg = window.Telegram && window.Telegram.WebApp;
  if (tg && tg.initData) {
    var root = document.documentElement;
    root.dataset.tg = '1';
    var applyTheme = function () {
      var p = tg.themeParams || {};
      Object.keys(p).forEach(function (k) {
        root.style.setProperty('--tg-' + k.replace(/_/g, '-'), p[k]);
      });
      root.dataset.tgScheme = tg.colorScheme || '';
    };
    applyTheme();
    tg.onEvent('themeChanged', applyTheme);
    tg.ready();
    tg.expand();

    // Нативная «Назад» на вторичных страницах (там есть .nav-back).
    var back = document.querySelector('.nav-back');
    if (back && tg.BackButton) {
      tg.BackButton.show();
      tg.BackButton.onClick(function () { location.href = back.href; });
    }
    // Экран «Общая задача»: основное действие — нативная кнопка внизу, а
    // HTML-кнопка прячется (html[data-tg-main], см. share.css).
    var shareForm = document.getElementById('share-form');
    var shareAdd = document.getElementById('share-add');
    if (shareForm && shareAdd && tg.MainButton) {
      root.dataset.tgMain = '1';
      tg.MainButton.setText(shareAdd.textContent);
      tg.MainButton.show();
      tg.MainButton.onClick(function () {
        if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
        if (shareForm.reportValidity()) { tg.MainButton.showProgress(); shareForm.submit(); }
      });
    }
  }

  // ---------- действия без перезагрузки ----------
  // Формы с data-ajax работают и без JS (обычный POST + редирект). С JS тот же
  // POST уходит fetch'ем с Accept: application/json, сервер отвечает готовыми
  // HTML-фрагментами (dashboard/fragments.py), а мы подменяем их на месте.
  // Интерфейс реагирует сразу (оптимистично) и откатывается, если сервер
  // отказал. Все анимации — только transform/opacity.
  var html = document.documentElement;
  var EASE_OUT = 'cubic-bezier(0.32, 0.72, 0, 1)';
  var canAnimate = !!html.animate &&
    !(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches);
  var rich = canAnimate && html.classList.contains('mid');   // FLIP-перестановка — не на слабых телефонах
  var errMeta = document.querySelector('meta[name=x-err]');
  var ERR_TEXT = errMeta ? errMeta.content : 'Error';

  // Настройки из шторки «Производительность» (localStorage; в приватном режиме может бросать).
  function pref(k, def) {
    try { var v = localStorage.getItem(k); return v === null ? def : v; } catch (e) { return def; }
  }
  function haptic(kind) {
    if (html.classList.contains('min') || pref('perf-haptic', '1') === '0') return;
    if (tg && tg.HapticFeedback) {
      if (kind === 'success') tg.HapticFeedback.notificationOccurred('success');
      else tg.HapticFeedback.impactOccurred('light');
    } else if (navigator.vibrate) {
      try { navigator.vibrate(kind === 'success' ? [10, 30, 10] : 10); } catch (e) { /* не поддерживается */ }
    }
  }

  // action: {label, run} — кнопка в тосте (например «Отменить»); ms — сколько показывать.
  function toast(text, action, ms) {
    var old = document.querySelector('.toast');
    if (old) old.remove();
    var el = document.createElement('div');
    el.className = 'toast';
    el.setAttribute('role', 'status');
    el.textContent = text;
    if (action) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'toast-btn';
      b.textContent = action.label;
      b.addEventListener('click', function () { el.remove(); action.run(); });
      el.appendChild(b);
    }
    document.body.appendChild(el);
    setTimeout(function () { el.classList.add('out'); setTimeout(function () { el.remove(); }, 300); }, ms || 3200);
  }

  function toNode(markup) {
    var t = document.createElement('template');
    t.innerHTML = markup.trim();
    return t.content.firstElementChild;
  }

  // Плавное перемещение соседей: запоминаем позиции видимых карточек, меняем
  // DOM, затем проигрываем разницу (FLIP) — карточки «съезжают» на новые места.
  function flip(mutate) {
    var first = [];
    if (rich) {
      var vh = window.innerHeight;
      Array.prototype.forEach.call(document.querySelectorAll('.list .task'), function (c) {
        var r = c.getBoundingClientRect();
        if (r.bottom > -200 && r.top < vh + 200) first.push([c, r.top]);
      });
    }
    mutate();
    first.forEach(function (p) {
      var c = p[0];
      if (!c.isConnected) return;
      var dy = p[1] - c.getBoundingClientRect().top;
      if (Math.abs(dy) > 1) c.animate([{transform: 'translateY(' + dy + 'px)'}, {transform: 'none'}], {duration: 420, easing: EASE_OUT});
    });
  }

  function enter(node) {
    if (canAnimate) node.animate([{opacity: 0, transform: 'translateY(-8px) scale(0.98)'}, {opacity: 1, transform: 'none'}],
      {duration: 360, easing: EASE_OUT});
  }

  function leave(node, done) {
    if (!canAnimate) { done(); return; }
    var a = node.animate([{opacity: 1, transform: 'none'}, {opacity: 0, transform: 'scale(0.96)'}],
      {duration: 180, easing: 'ease-out', fill: 'forwards'});
    a.onfinish = done;
  }

  function countTo(el, to) {
    var from = parseInt(el.textContent, 10) || 0;
    if (from === to) return;
    if (!canAnimate) { el.textContent = to; return; }
    var t0 = performance.now();
    (function step(t) {
      var p = Math.min(1, (t - t0) / 450), e = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(from + (to - from) * e);
      if (p < 1) requestAnimationFrame(step);
    })(t0);
  }

  function applyStats(s) {
    if (!s) return;
    ['today', 'overdue', 'streak'].forEach(function (k) {
      var el = document.querySelector('.stat-' + k + ' .stat-num');
      if (el && s[k] != null) countTo(el, s[k]);
    });
    var ring = document.querySelector('.stat-today');
    if (ring && s.progress != null) ring.style.setProperty('--p', s.progress);
  }

  // Куда вставить карточку: активные по возрастанию срока, выполненные — по убыванию.
  function insertSorted(box, card, tail) {
    var due = Number(card.dataset.due), done = card.classList.contains('done'), ref = null;
    var cards = box.querySelectorAll(':scope > .task');
    for (var i = 0; i < cards.length; i++) {
      var c = cards[i];
      if (c === card) continue;
      var cDone = c.classList.contains('done'), cDue = Number(c.dataset.due);
      if (done ? (cDone && cDue < due) : (cDone || cDue > due)) { ref = c; break; }
    }
    box.insertBefore(card, ref || tail);
  }

  function placeCard(card) {
    var list = document.querySelector('.list');
    if (!list) return false;
    var groups = list.querySelectorAll(':scope > .task-group');
    if (!groups.length) {
      insertSorted(list, card, list.querySelector(':scope > .load-more'));
      return true;
    }
    // Группы по дате: в секцию своего дня (нет такой — создаём на своём месте).
    // Группы по тегу так не умеем — карточка остаётся там, где была.
    var day = card.dataset.day;
    if (!day || !groups[0].dataset.day) return false;
    var sec = list.querySelector(':scope > .task-group[data-day="' + day + '"]');
    if (!sec) {
      var after = null;
      for (var i = 0; i < groups.length; i++) if (groups[i].dataset.day > day) { after = groups[i]; break; }
      var p = day.split('-');
      sec = document.createElement('section');
      sec.className = 'task-group';
      sec.dataset.day = day;
      sec.innerHTML = '<h2 class="group-title">' + p[2] + '.' + p[1] + ' <span class="group-count"></span></h2>';
      list.insertBefore(sec, after || list.querySelector(':scope > .load-more'));
    }
    insertSorted(sec, card, null);
    tidyGroups();
    return true;
  }

  // Счётчики в заголовках групп и пустые группы — после вставки/удаления карточки
  function tidyGroups() {
    var groups = document.querySelectorAll('.list > .task-group');
    for (var i = 0; i < groups.length; i++) {
      var g = groups[i], cnt = g.querySelector('.group-count');
      var all = g.querySelectorAll(':scope > .task').length, n = g.querySelectorAll(':scope > .task:not([hidden])').length;
      if (!all) { g.remove(); continue; }
      g.hidden = !n;                                 // остались только скрытые «удалённые» (ждут «Отменить»)
      if (cnt) cnt.textContent = n;
    }
  }

  function syncEmpty(p) {
    tidyGroups();
    var list = document.querySelector('.list');
    if (!list) return;
    var empty = list.querySelector(':scope > .empty');
    if (p.empty && !list.querySelector('.task')) {
      if (!empty) { var e = document.createElement('div'); e.className = 'empty'; e.textContent = p.empty; list.prepend(e); }
    } else if (empty) { empty.remove(); }
  }

  // Общая часть любого ответа: сводка, вкладки/теги, график.
  function applyCommon(p) {
    applyStats(p.stats);
    if (p.controls) {
      var cur = document.querySelector('.view-controls'), next = toNode(p.controls);
      if (cur && next && cur.outerHTML !== next.outerHTML) cur.replaceWith(next);
    }
    if (p.chart) {
      var chart = document.querySelector('.chart-block'), nc = toNode(p.chart);
      if (chart && nc) chart.replaceWith(nc);
    }
    if (p.more_done != null) {
      var lst = document.querySelector('.list'), curMore = lst && lst.querySelector(':scope > .load-more');
      if (!p.more_done) { if (curMore) curMore.remove(); }
      else if (curMore) curMore.replaceWith(toNode(p.more_done));
      else if (lst) lst.appendChild(toNode(p.more_done));
    }
    syncEmpty(p);
  }

  // forceMove: после оптимистичного переключения класс done уже стоит, по нему перемещение не определить.
  function applyTask(p, forceMove) {
    var cur = document.getElementById('task-' + p.task_id);
    if (!p.card) {
      if (cur) leave(cur, function () { flip(function () { cur.remove(); syncEmpty(p); }); });
      return;
    }
    var next = toNode(p.card);
    if (!next) return;
    if (!cur) {                                      // новая задача
      flip(function () {
        if (!placeCard(next)) (document.querySelector('.list') || document.body).prepend(next);
        syncEmpty({});
      });
      enter(next);
      return;
    }
    var moved = forceMove || cur.classList.contains('done') !== next.classList.contains('done') || cur.dataset.due !== next.dataset.due;
    flip(function () {
      cur.replaceWith(next);
      if (moved) placeCard(next);
    });
    if (moved) enter(next);
  }

  function showCreateError(form, msg) {
    var err = form.querySelector('.new-task-error');
    if (!err) {
      err = document.createElement('div');
      err.className = 'new-task-error';
      form.insertBefore(err, form.querySelector('input[name=csrf]'));
    }
    err.textContent = msg || ERR_TEXT;
  }

  function sendForm(form, submitter) {
    var kind = form.dataset.ajax;
    var card = form.closest('.task');
    var isToggle = kind === 'task' && /\/(done|undone)$/.test(form.action);
    var undo = isToggle && /undone$/.test(form.action);
    var row = kind === 'sub' ? form.closest('.subtask-row') : kind === 'photo' ? form.closest('.task-photo') : null;
    var subToggle = !!row && /\/toggle$/.test(form.action);
    var btn = submitter || form.querySelector('button[type=submit], button:not([type])');

    form.dataset.busy = '1';
    if (btn) btn.disabled = true;
    // оптимистичное состояние
    if (isToggle && card) card.classList.toggle('done', !undo);
    else if (subToggle) row.classList.toggle('done');
    else if (card && (kind === 'task' || kind === 'tpl')) card.classList.add('pending');
    else if (row) row.classList.add('pending');
    if (isToggle && !undo) haptic('success');

    function rollback(msg) {
      var gone = row || card;                  // отложенное удаление (deferDelete) прятало карточку/пункт
      if (gone && gone.hidden) {
        if (gone.getAnimations) gone.getAnimations().forEach(function (a) { a.cancel(); });
        gone.hidden = false;
        tidyGroups();
      }
      if (isToggle && card) card.classList.toggle('done', undo);
      else if (subToggle) row.classList.toggle('done');
      if (card) card.classList.remove('pending');
      if (row) row.classList.remove('pending');
      if (msg) toast(msg);
    }

    var fd = new FormData(form);
    fd.append('ctx', location.search);
    fetch(form.action, {method: 'POST', body: fd, credentials: 'same-origin', headers: {'Accept': 'application/json'}})
      .then(function (r) {
        if (r.status === 403) { location.reload(); return null; }
        return r.json().then(function (p) { return {ok: r.ok, status: r.status, p: p}; },
                             function () { return {ok: false, status: r.status, p: {}}; });
      })
      .then(function (res) {
        if (!res) return;
        if (!res.ok) {
          if (res.status === 422 && kind === 'create') showCreateError(form, res.p.error);
          else rollback((res.p && res.p.error) || ERR_TEXT);
          return;
        }
        var p = res.p;
        if (kind === 'task') {
          if (card) card.classList.remove('pending');
          applyTask(p, isToggle);
          if (isToggle && !undo && p.card) {
            var nb = document.querySelector('#task-' + p.task_id + ' .check-btn');
            if (nb) document.dispatchEvent(new CustomEvent('task:done', {detail: {btn: nb}}));
          }
          if (p.action === 'delete') haptic('light');
          if (p.toast) toast(p.toast);
          applyCommon(p);
        } else if (kind === 'sub') {
          var block = document.querySelector('#task-' + p.task_id + ' .subtasks-block');
          if (block) {
            var nbk = toNode(p.subtasks);
            block.replaceWith(nbk);
            if (form.classList.contains('subtask-add-form')) {
              var inp = nbk.querySelector('input[name=title]');
              if (inp) inp.focus({preventScroll: true});
            }
          }
        } else if (kind === 'tpl') {
          if (card) card.classList.remove('pending');
          if (p.removed && card) card.remove();
          if (p.toast) { toast(p.toast); haptic('success'); }
        } else if (kind === 'photo') {
          var pb = document.querySelector('#task-' + p.task_id + ' .photos-block');
          if (pb) pb.replaceWith(toNode(p.photos));
        } else if (kind === 'desc') {
          var dsc = document.querySelector('#task-' + p.task_id + ' .desc-block');
          if (dsc) dsc.replaceWith(toNode(p.description));
        } else if (kind === 'create') {
          form.reset();
          var err = form.querySelector('.new-task-error');
          if (err) err.remove();
          var sheetEl = document.querySelector('.add-sheet');
          if (sheetEl) sheetEl.open = false;
          applyTask(p);
          applyCommon(p);
          haptic('success');
        }
      })
      .catch(function () { rollback(ERR_TEXT); })
      .then(function () {
        delete form.dataset.busy;
        if (btn) btn.disabled = false;
      });
  }

  // Удаление с «Отменить» вместо confirm(): карточка (или пункт) прячется сразу,
  // а POST уходит через UNDO_MS. Одновременно ждёт только одно удаление —
  // следующее сначала отправляет предыдущее. Уход со страницы / сворачивание
  // Mini App отправляет ожидающее удаление сразу (fetch keepalive).
  var UNDO_MS = 5000, pendingDel = null;
  function commitDelete(viaKeepalive) {
    var d = pendingDel;
    if (!d) return;
    pendingDel = null;
    clearTimeout(d.timer);
    if (viaKeepalive) {
      var fd = new FormData(d.form);
      fd.append('ctx', location.search);
      fetch(d.form.action, {method: 'POST', body: fd, credentials: 'same-origin', keepalive: true,
                            headers: {'Accept': 'application/json'}}).catch(function () {});
    } else {
      sendForm(d.form, null);
    }
  }
  function deferDelete(form) {
    commitDelete(false);
    var el = form.closest('.task-photo') || form.closest('.subtask-row') || form.closest('.task');
    if (!el) { sendForm(form, null); return; }
    var d = {form: form, el: el};
    pendingDel = d;
    leave(el, function () {
      if (d.undone) return;
      flip(function () { el.hidden = true; tidyGroups(); });
    });
    haptic('light');
    toast(form.dataset.undo, {label: form.dataset.undoBtn, run: function () {
      if (pendingDel !== d) return;
      pendingDel = null;
      d.undone = true;
      clearTimeout(d.timer);
      if (el.getAnimations) el.getAnimations().forEach(function (a) { a.cancel(); });
      flip(function () { el.hidden = false; tidyGroups(); });
      enter(el);
    }}, UNDO_MS);
    d.timer = setTimeout(function () { if (pendingDel === d) commitDelete(false); }, UNDO_MS);
  }
  window.addEventListener('pagehide', function () { commitDelete(true); });
  document.addEventListener('visibilitychange', function () { if (document.hidden) commitDelete(true); });

  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (e.defaultPrevented || !f.dataset || !f.dataset.ajax || !window.fetch || !window.FormData) return;
    e.preventDefault();
    if (f.dataset.busy) return;
    if (f.dataset.undo) { if (!pendingDel || pendingDel.form !== f) deferDelete(f); return; }
    sendForm(f, e.submitter);
  });

  // Переключатель «Мои / группа»: явное подтверждение, затем индикатор
  // загрузки — полоса сверху, спиннер на чипе и «призрачные» карточки вместо
  // списка, пока сервер собирает чужую страницу (холодный старт Render — секунды).
  function showSwitchLoading(chip, nav) {
    chip.classList.add('loading');
    chip.setAttribute('aria-busy', 'true');
    var bar = document.createElement('div');
    bar.className = 'nav-progress';
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-label', nav.dataset.loading || '');
    document.body.appendChild(bar);
    requestAnimationFrame(function () { bar.classList.add('run'); });
    var list = document.querySelector('.list');
    if (list) {
      list.setAttribute('aria-busy', 'true');
      var sk = document.createElement('div');
      sk.className = 'list-skeleton';
      sk.innerHTML = '<div class="sk-card"></div><div class="sk-card"></div><div class="sk-card"></div>' +
                     '<p class="sk-label">' + (nav.dataset.loading || '') + '</p>';
      list.parentNode.insertBefore(sk, list);
    }
  }
  function confirmSwitch(chip, nav, go) {
    var back = document.createElement('div');
    back.className = 'confirm-back';
    back.innerHTML = '<div class="confirm-card" role="alertdialog" aria-modal="true">' +
      '<p class="confirm-text"></p><div class="confirm-actions">' +
      '<button type="button" class="confirm-cancel"></button><button type="button" class="confirm-go"></button></div></div>';
    back.querySelector('.confirm-text').textContent = chip.dataset.confirm;
    back.querySelector('.confirm-cancel').textContent = nav.dataset.cancel;
    back.querySelector('.confirm-go').textContent = nav.dataset.go;
    function close() { document.removeEventListener('keydown', onKey); back.remove(); chip.focus({preventScroll: true}); }
    function onKey(e) { if (e.key === 'Escape') close(); }
    back.addEventListener('click', function (e) {
      if (e.target === back || e.target.classList.contains('confirm-cancel')) close();
      else if (e.target.classList.contains('confirm-go')) { document.removeEventListener('keydown', onKey); back.remove(); go(); }
    });
    document.addEventListener('keydown', onKey);
    document.body.appendChild(back);
    back.querySelector('.confirm-go').focus({preventScroll: true});
    haptic('light');
  }
  document.addEventListener('click', function (e) {
    var chip = e.target.closest && e.target.closest('.owner-switch a[data-confirm]');
    if (!chip || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    if (chip.classList.contains('loading')) return;
    var nav = chip.closest('.owner-switch');
    confirmSwitch(chip, nav, function () {
      showSwitchLoading(chip, nav);
      commitDelete(true);   // ожидающее «Отменить» удаление — отправить до ухода
      location.href = chip.href;
    });
  });
  // Возврат «Назад» из bfcache: страница восстанавливается с индикатором — убираем
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    Array.prototype.forEach.call(document.querySelectorAll('.nav-progress, .list-skeleton'), function (n) { n.remove(); });
    Array.prototype.forEach.call(document.querySelectorAll('.owner-switch .loading'), function (c) {
      c.classList.remove('loading'); c.removeAttribute('aria-busy');
    });
    var l = document.querySelector('.list');
    if (l) l.removeAttribute('aria-busy');
  });

  // «Показать ещё выполненные» без перезагрузки: та же страница с большим
  // done_limit, из неё берём только список. Адрес обновляем — обновление
  // страницы покажет то же самое. Сбой — обычный переход по ссылке.
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('.list > .load-more');
    if (!a || !window.fetch || !window.DOMParser || e.metaKey || e.ctrlKey) return;
    e.preventDefault();
    if (a.getAttribute('aria-busy')) return;
    a.setAttribute('aria-busy', 'true');
    var href = a.href;
    fetch(href, {credentials: 'same-origin'})
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (markup) {
        var next = new DOMParser().parseFromString(markup, 'text/html').querySelector('.list');
        var cur = document.querySelector('.list');
        if (!next || !cur) throw new Error('no list');
        var known = {};
        Array.prototype.forEach.call(cur.querySelectorAll('.task'), function (c) { known[c.id] = 1; });
        // меняем содержимое, а не сам .list: на нём висят обработчики свайпа
        cur.replaceChildren.apply(cur, Array.prototype.slice.call(next.childNodes));
        Array.prototype.forEach.call(cur.querySelectorAll('.task'), function (c) { if (!known[c.id]) enter(c); });
        if (history.replaceState) history.replaceState(null, '', href);
      })
      .catch(function () { location.href = href; });
  });

  // ---------- график: подгружается, когда подъезжает к экрану ----------
  // Сервер отдаёт на его месте пустую рамку (.chart-lazy): два SQL-запроса
  // графика не задерживают список. Без JS страница ?nojs=1 рисует график сразу.
  var lazyChart = document.querySelector('.chart-lazy');
  if (lazyChart) {
    var loadChart = function () {
      fetch(lazyChart.dataset.src, {credentials: 'same-origin'})
        .then(function (r) { return r.ok ? r.text() : ''; })
        .then(function (markup) {
          var node = markup && toNode(markup);
          if (node && lazyChart.isConnected) { lazyChart.replaceWith(node); enter(node); }
          else lazyChart.removeAttribute('aria-busy');
        })
        .catch(function () { lazyChart.removeAttribute('aria-busy'); });
    };
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (entries, io) {
        if (entries[0].isIntersecting) { io.disconnect(); loadChart(); }
      }, {rootMargin: '300px'}).observe(lazyChart);
    } else {
      loadChart();
    }
  }

  // ---------- «Поделиться» ----------
  // Сервер отдаёт ссылку и (если получилось) id готового сообщения. Порядок:
  // нативный диалог выбора чата (Bot API 8.0+) -> окно «переслать» Telegram ->
  // системный share -> копирование ссылки.
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('.share-btn');
    if (!b) return;
    b.disabled = true;
    fetch(b.dataset.url, {
      method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: 'csrf=' + encodeURIComponent(b.dataset.csrf), credentials: 'same-origin'
    }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) {
        var inTg = tg && tg.initData;
        if (inTg && d.prepared_id && tg.shareMessage && tg.isVersionAtLeast && tg.isVersionAtLeast('8.0')) {
          tg.shareMessage(d.prepared_id);
        } else if (inTg && tg.openTelegramLink) {
          tg.openTelegramLink('https://t.me/share/url?url=' + encodeURIComponent(d.link) + '&text=' + encodeURIComponent(d.text));
        } else if (navigator.share) {
          navigator.share({text: d.text, url: d.link}).catch(function () {});
        } else if (navigator.clipboard) {
          return navigator.clipboard.writeText(d.link).then(function () {
            var old = b.lastChild.textContent;
            b.lastChild.textContent = b.dataset.copied;
            setTimeout(function () { b.lastChild.textContent = old; }, 1800);
          });
        }
      })
      .catch(function () {})
      .then(function () { b.disabled = false; });
  });

  // ---------- шторка «Новая задача» ----------
  // Клик по затемнению (это ::before самого <details>, цель клика — он сам) и Esc закрывают.
  var sheet = document.querySelector('.add-sheet');
  if (sheet) {
    sheet.addEventListener('click', function (e) { if (e.target === sheet) sheet.open = false; });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') sheet.open = false; });
  }

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

  // ---------- часовой пояс устройства ≠ пояс бота ----------
  // Неверный пояс = напоминания не в то время, а заметить это сложно. Сравниваем
  // пояс браузера с сохранённым и предлагаем исправить (на странице задач —
  // баннер, в настройках — кнопка «Взять с устройства»).
  var deviceTz = '';
  try { deviceTz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) {}
  var tzHint = document.querySelector('.tz-hint');
  if (tzHint && deviceTz && deviceTz !== tzHint.dataset.tz) {
    var tzKey = 'tzHintOff:' + deviceTz, tzOff = false;
    try { tzOff = localStorage.getItem(tzKey) === '1'; } catch (e) {}
    if (!tzOff) {
      tzHint.querySelector('span').textContent =
        tzHint.dataset.text.replace('{tz}', deviceTz).replace('{saved}', tzHint.dataset.tz);
      tzHint.querySelector('a').href = tzHint.dataset.href + encodeURIComponent(deviceTz);
      tzHint.hidden = false;
      tzHint.querySelector('button').addEventListener('click', function () {
        tzHint.hidden = true;
        try { localStorage.setItem(tzKey, '1'); } catch (e) {}
      });
    }
  }
  var tzBtn = document.querySelector('.tz-detect'), tzInput = document.querySelector('input[name="timezone"]');
  if (tzBtn && tzInput && deviceTz && deviceTz !== tzInput.value) {
    tzBtn.hidden = false;
    tzBtn.textContent += ' (' + deviceTz + ')';
    tzBtn.addEventListener('click', function () { tzInput.value = deviceTz; tzBtn.hidden = true; });
  }

  // ---------- шторка «Производительность» ----------
  // Уровень (auto / full / lite / min) читает детектор из <head> — он решает, какие
  // классы повесить на <html>, поэтому смена уровня перезагружает страницу.
  (function () {
    var openBtn = document.getElementById('perf-open'), back = document.getElementById('perf-back');
    if (!openBtn || !back) return;
    var lv = back.querySelector('.perf-lv'), level = pref('perf', 'auto'), lastFocus = null;
    function set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* приватный режим */ } }
    function paint() {
      Array.prototype.forEach.call(lv.children, function (b) { b.setAttribute('aria-pressed', String(b.dataset.l === level)); });
      Array.prototype.forEach.call(back.querySelectorAll('.perf-sw'), function (b) {
        b.setAttribute('aria-checked', String(pref('perf-' + b.dataset.k, '1') !== '0'));
      });
    }
    function show() {
      lastFocus = document.activeElement; paint();
      back.classList.add('open'); back.setAttribute('aria-hidden', 'false');
      var on = lv.querySelector('[aria-pressed=true]'); if (on) on.focus({preventScroll: true});
    }
    function hide() {
      back.classList.remove('open'); back.setAttribute('aria-hidden', 'true');
      if (lastFocus && lastFocus.focus) lastFocus.focus({preventScroll: true});
    }
    openBtn.addEventListener('click', show);
    back.addEventListener('click', function (e) { if (e.target === back) hide(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && back.classList.contains('open')) hide(); });
    lv.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b || b.dataset.l === level) return;
      set('perf', b.dataset.l); location.reload();
    });
    back.addEventListener('click', function (e) {
      var b = e.target.closest('.perf-sw'); if (!b) return;
      set('perf-' + b.dataset.k, b.getAttribute('aria-checked') === 'true' ? '0' : '1'); paint();
    });
  })();

  // ---------- свайп вправо по карточке = выполнить (touch/pen) ----------
  // Мышь не трогаем (выделение текста). Двигается только transform одной
  // карточки, по порогу срабатывает обычная кнопка-галочка — то есть тот же
  // fetch, оптимистичный UI и откат, что и при клике.
  (function () {
    var list = document.querySelector('.list');
    if (!list || !window.PointerEvent) return;
    var TH = 90, drag = null, swiped = false;
    list.addEventListener('pointerdown', function (e) {
      if (e.pointerType === 'mouse' || pref('perf-swipe', '1') === '0') return;
      var card = e.target.closest('.task');
      if (!card || card.classList.contains('pending') || e.target.closest('button, a, input, textarea, select, .task-expanded')) return;
      drag = {card: card, x: e.clientX, y: e.clientY, dx: 0, on: false, id: e.pointerId, f: 0, ready: false};
    });
    list.addEventListener('pointermove', function (e) {
      if (!drag || e.pointerId !== drag.id) return;
      var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (!drag.on) {
        if (dx > 10 && dx > Math.abs(dy) * 1.5) {
          drag.on = true; drag.card.classList.remove('snap'); drag.card.classList.add('drag');
          try { drag.card.setPointerCapture(e.pointerId); } catch (_) { /* карточку уже убрали */ }
        } else { if (Math.abs(dy) > 10 || dx < -10) drag = null; return; }
      }
      drag.dx = Math.max(0, dx);
      if (!drag.f) drag.f = requestAnimationFrame(function () {
        if (!drag) return;
        drag.f = 0;
        drag.card.style.transform = 'translate3d(' + drag.dx + 'px,0,0)';
        var r = drag.dx >= TH;
        if (r !== drag.ready) { drag.ready = r; drag.card.classList.toggle('swipe-ready', r); if (r) haptic('light'); }
      });
    });
    function end() {
      if (!drag) return;
      var d = drag; drag = null;
      cancelAnimationFrame(d.f);
      if (!d.on) return;
      swiped = true; setTimeout(function () { swiped = false; }, 350);
      d.card.classList.remove('drag', 'swipe-ready');
      d.card.classList.add('snap'); d.card.style.transform = '';
      setTimeout(function () { d.card.classList.remove('snap'); }, 250);
      if (d.dx >= TH) { var b = d.card.querySelector('.check-btn'); if (b) b.click(); }
    }
    list.addEventListener('pointerup', end); list.addEventListener('pointercancel', end);
    // после жеста браузер шлёт click на карточку — он не должен раскрыть её
    list.addEventListener('click', function (e) { if (swiped && e.isTrusted) { e.preventDefault(); e.stopPropagation(); } }, true);
  })();
})();
