"""Производительность страницы задач дашборда на трёх классах устройств.

Не входит в обычный прогон (маркер perf исключён в pytest.ini): запускается
явно —

    python -m playwright install chromium        # один раз
    python -m pytest -m perf -s                  # замер + сверка с бюджетами
    PERF_BASELINE=1 python -m pytest -m perf -s  # записать базовую линию

Реальное aiohttp-приложение дашборда с заглушкой БД (tests/dashboard_stubs.py)
и «тяжёлым» набором данных; страница открывается в headless Chromium с
эмуляцией устройства:

* strong — десктоп / ноутбук с дискретной видеокартой;
* mid    — бюджетный ноутбук или телефон среднего класса;
* weak   — слабый телефон уровня Snapdragon 645.

Множитель замедления CPU в Chrome задаётся относительно машины, на которой идёт
тест, поэтому он не фиксирован, а калибруется: эталонный JS-бенчмарк гоняется
без замедления, и множитель подбирается так, чтобы на профиле бенчмарк шёл
примерно target_ms. Так «weak» означает примерно одно и то же и на мощном
десктопе, и на CI-раннере.

Эмуляция CPU честно воспроизводит JS, стили и layout, но не цену GPU-эффектов
вроде backdrop-filter. Поэтому для mid/weak дополнительно проверяется
структурно: в живом DOM нет элементов с backdrop-filter и нет анимаций filter.
"""

import gzip
import io
import json
import os
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("playwright.async_api")
from aiohttp import web as aioweb  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

import dashboard  # noqa: E402
from tests.dashboard_stubs import install_db_stubs  # noqa: E402

pytestmark = pytest.mark.perf

UTC = ZoneInfo("UTC")
TOKEN = "perf-token"
OWNER_ID = -100777
ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / ".pytest_cache" / "perf"
BASELINE_FILE = Path(__file__).with_name("baseline.json")
RECORD_BASELINE = os.environ.get("PERF_BASELINE") == "1"

# ---------- профили устройств ----------
# target_ms — сколько эталонный бенчмарк (_CPU_BENCH) должен идти на таком
# устройстве. Соотношение 1 : 4 : 6.5 — как у Lighthouse (mid-tier mobile = 4x
# от десктопа); SD645 (4×A75 @ 2.0 ГГц + 4×A55) ещё примерно в 1.5 раза
# медленнее среднего телефона. Сеть — пресеты CDP "4G" и "Slow 4G".
PROFILES = {
    "strong": {
        "target_ms": 60, "viewport": {"width": 1440, "height": 900}, "dpr": 1,
        "mobile": False, "device_memory": 16, "cores": 12, "network": None,
    },
    "mid": {
        "target_ms": 240, "viewport": {"width": 393, "height": 851}, "dpr": 2.75,
        "mobile": True, "device_memory": 4, "cores": 8,
        "network": {"latency": 40, "down_kbps": 9000, "up_kbps": 3000},
    },
    "weak": {
        "target_ms": 400, "viewport": {"width": 360, "height": 780}, "dpr": 2,
        "mobile": True, "device_memory": 4, "cores": 8,
        "network": {"latency": 150, "down_kbps": 1600, "up_kbps": 750},
    },
}

# ---------- бюджеты ----------
# Одно место, чтобы подкручивать. None — метрика для профиля не проверяется.
# Подобраны по замерам с аппаратным GPU (см. _GPU_ARGS): текущая версия
# укладывается с запасом ~20-30%, а версия до оптимизации
# (tests/perf/baseline.json) не проходит по DOM, по backdrop-filter и
# анимациям filter в облегчённом режиме, а на mid — ещё и по раскрытию карточки.
#
# Без GPU (renderer = SwiftShader: CI-раннер, сервер без видеокарты) браузер
# растеризует программно, и на viewport 1440x900 в режиме .fx это само по
# себе ограничивает FPS и раскрытие — тогда метрики из GPU_BOUND для strong
# не проверяются (это свойство стенда, а не страницы).
BUDGETS = {
    "server_ms":       {"strong": 25,   "mid": None, "weak": None},
    "html_gzip_kb":    {"strong": 11,   "mid": 11,   "weak": 11},
    "dom_nodes":       {"strong": 1200, "mid": 1200, "weak": 1200},
    "fcp_ms":          {"strong": 600,  "mid": 1100, "weak": 1900},
    "tbt_ms":          {"strong": 100,  "mid": 200,  "weak": 300},
    "expand_ms":       {"strong": 60,   "mid": 90,   "weak": 160},
    "scroll_fps_min":  {"strong": 45,   "mid": 48,   "weak": 40},
}
GPU_BOUND = {"expand_ms", "scroll_fps_min"}

# ---------- набор данных «тяжёлого» пользователя ----------
ACTIVE_TASKS = 40
DONE_TASKS = 120
AUTHORS = 10


def _jpeg(side: int) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (side, int(side * 0.75)), (150, 110, 70)).save(buf, format="JPEG", quality=76)
    return buf.getvalue()


def _dataset():
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    tasks, photos, subtasks = [], {}, {}
    photo_id = 1000
    sub_id = 5000
    repeats = ["none", "none", "daily", "weekly", "none", "monthly"]
    for i in range(ACTIVE_TASKS + DONE_TASKS):
        task_id = i + 1
        done = i >= ACTIVE_TASKS
        tasks.append({
            "id": task_id, "owner_id": OWNER_ID, "owner_type": "chat",
            "title": f"Задача номер {task_id}: купить, позвонить, проверить",
            "due_at": now + timedelta(hours=(i * 7) % 500 - 160),
            "repeat": repeats[i % len(repeats)], "remind": "on_time", "done": done,
            "tag": "дом" if i % 4 == 0 else None, "created_by": 9000 + i % AUTHORS,
            "description": ("Подробное описание задачи. " * 6) if i % 2 == 0 else None,
            "remind_until_done": False,
        })
        if i % 3 == 0:
            subtasks[task_id] = []
            for k in range(i % 6):
                sub_id += 1
                subtasks[task_id].append({"id": sub_id, "task_id": task_id, "title": f"Пункт {k + 1}",
                                          "done": k % 2 == 0, "position": k})
        if i < ACTIVE_TASKS and i % 3 == 1 and len(photos) < 15:
            photos[task_id] = []
            for _ in range(1 + i % 5):
                photo_id += 1
                photos[task_id].append({"id": photo_id, "task_id": task_id, "mime": "image/jpeg"})
    # Порядок как у настоящего ORDER BY done, due_at.
    tasks.sort(key=lambda t: (t["done"], t["due_at"]))
    users = {9000 + k: {"first_name": f"Участник{k}", "username": f"user{k}", "has_photo": k % 2 == 0}
             for k in range(AUTHORS)}
    today = date.today()
    history = [{"day": today - timedelta(days=d), "event": "done", "cnt": (d * 3) % 7} for d in range(14)]
    done_days = [today - timedelta(days=d) for d in range(5)]
    return tasks, photos, subtasks, users, history, done_days


# ---------- JS, исполняемый в странице ----------

_CPU_BENCH = """() => {
  const t0 = performance.now();
  let acc = 0;
  const arr = [];
  for (let i = 0; i < 300000; i++) { arr.push((i * 2654435761) % 1000003); }
  arr.sort((a, b) => a - b);
  for (let i = 0; i < arr.length; i++) { acc = (acc + arr[i] * 31) % 1000000007; }
  const s = JSON.stringify(arr.slice(0, 20000));
  acc += JSON.parse(s).length;
  return performance.now() - t0 + (acc < 0 ? 1 : 0);
}"""

# Ставится до любого скрипта страницы: long tasks для TBT и характеристики
# устройства, на которые смотрит детектор облегчённого режима.
_INIT_SCRIPT = """
window.__lt = [];
try {
  new PerformanceObserver(l => { for (const e of l.getEntries()) window.__lt.push([e.startTime, e.duration]); })
    .observe({type: 'longtask', buffered: true});
} catch (e) {}
Object.defineProperty(navigator, 'deviceMemory', {get: () => %(mem)s});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => %(cores)s});
"""

_LOAD_METRICS = """() => {
  const nav = performance.getEntriesByType('navigation')[0];
  const fcp = performance.getEntriesByName('first-contentful-paint')[0];
  const loadEnd = nav ? nav.loadEventEnd : performance.now();
  let tbt = 0;
  for (const [start, dur] of window.__lt) { if (start < loadEnd + 1000) tbt += Math.max(0, dur - 50); }
  return {
    fcp_ms: fcp ? fcp.startTime : null,
    dcl_ms: nav ? nav.domContentLoadedEventEnd : null,
    load_ms: loadEnd,
    tbt_ms: tbt,
    dom_nodes: document.querySelectorAll('*').length,
    backdrop_elements: Array.from(document.querySelectorAll('body *')).filter(el => {
      const cs = getComputedStyle(el);
      const v = cs.backdropFilter || cs.webkitBackdropFilter;
      return v && v !== 'none';
    }).length,
  };
}"""

# Раскрытие карточки: от клика по summary до кадра, в котором раскрытое
# содержимое уже есть в DOM (если оно вставляется лениво — ждём вставки).
_EXPAND = """async (n) => {
  const raf = () => new Promise(r => requestAnimationFrame(r));
  const summaries = Array.from(document.querySelectorAll('.task-summary')).slice(0, n);
  const times = [];
  let filterAnimations = 0;
  for (const s of summaries) {
    s.scrollIntoView({block: 'center'});
    await raf(); await raf();
    const details = s.parentElement;
    const t0 = performance.now();
    s.click();
    for (let i = 0; i < 60 && !details.querySelector('.task-expanded'); i++) await raf();
    await raf(); await raf();
    times.push(performance.now() - t0);
    for (const a of document.getAnimations()) {
      const kfs = a.effect && a.effect.getKeyframes ? a.effect.getKeyframes() : [];
      if (kfs.some(k => 'filter' in k || 'backdropFilter' in k)) filterAnimations++;
    }
    s.click();  // свернуть обратно
    await raf();
  }
  times.sort((a, b) => a - b);
  return {expand_ms: times[Math.floor(times.length / 2)], filter_animations: filterAnimations};
}"""

_SCROLL = """async (durationMs) => {
  window.scrollTo(0, 0);
  await new Promise(r => requestAnimationFrame(r));
  const deltas = [];
  const max = document.documentElement.scrollHeight - innerHeight;
  let last = performance.now();
  const start = last;
  let dir = 1;
  await new Promise(resolve => {
    function step(now) {
      deltas.push(now - last); last = now;
      if (scrollY >= max - 5) dir = -1; else if (scrollY <= 0) dir = 1;
      window.scrollBy(0, 45 * dir);
      if (now - start < durationMs) requestAnimationFrame(step); else resolve();
    }
    requestAnimationFrame(step);
  });
  const total = deltas.reduce((a, b) => a + b, 0);
  return {
    scroll_fps: deltas.length / (total / 1000),
    scroll_jank_pct: 100 * deltas.filter(d => d > 33.4).length / deltas.length,
  };
}"""


# Строка рендерера WebGL — в отчёт, чтобы было видно, рисовал ли браузер на
# GPU или программно (SwiftShader): от этого зависят FPS и цена блюра.
_RENDERER = """() => {
  const gl = document.createElement('canvas').getContext('webgl');
  if (!gl) return 'no webgl';
  const ext = gl.getExtension('WEBGL_debug_renderer_info');
  return ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
}"""

# По умолчанию headless Chromium рисует программно (SwiftShader) — и тогда
# blur в режиме .fx на viewport 1440x900 стоит несоизмеримо больше, чем на
# любой реальной видеокарте. Просим аппаратный GPU; где его нет (CI-раннер),
# Chrome сам откатится на SwiftShader — это видно по полю renderer в отчёте.
_GPU_ARGS = ["--enable-gpu", "--ignore-gpu-blocklist"]
if sys.platform == "win32":
    _GPU_ARGS.append("--use-angle=d3d11")


# ---------- вспомогательное ----------

_host_bench_ms: float | None = None


async def _calibrate(browser) -> float:
    """Время эталонного бенчмарка на этой машине без замедления (мин. из 5)."""
    global _host_bench_ms
    if _host_bench_ms is None:
        page = await browser.new_page()
        await page.goto("about:blank")
        await page.evaluate(_CPU_BENCH)  # прогрев JIT
        _host_bench_ms = min([await page.evaluate(_CPU_BENCH) for _ in range(5)])
        await page.close()
    return _host_bench_ms


@pytest.fixture
def dataset():
    return _dataset()


@pytest.fixture
async def server(monkeypatch, dataset):
    tasks, photos, subtasks, users, history, done_days = dataset
    settings = {
        "owner_id": OWNER_ID, "owner_type": "chat", "language": "ru", "timezone": "Europe/Warsaw",
        "dashboard_password_hash": dashboard.hash_password("pw"), "dashboard_password_version": 1,
    }
    install_db_stubs(monkeypatch, TOKEN, settings, tasks, photos=photos, subtasks=subtasks, users=users,
                     thumb=_jpeg(560), avatar=_jpeg(64), history=history, done_days=done_days)
    app = aioweb.Application()
    dashboard.register_dashboard_routes(app)
    client = TestClient(TestServer(app, host="127.0.0.1"))
    await client.start_server()
    cookie = dashboard._sign_session(OWNER_ID, "chat", password_version=1)
    yield client, cookie
    await client.close()


async def _server_metrics(client, cookie) -> dict:
    """Время генерации HTML на сервере (медиана из 20) и вес страницы."""
    headers = {"Cookie": f"session_{TOKEN}={cookie}"}
    url = f"/dashboard/{TOKEN}"
    await (await client.get(url, headers=headers)).read()  # прогрев
    times = []
    body = b""
    for _ in range(20):
        t0 = time.perf_counter()
        resp = await client.get(url, headers=headers)
        body = await resp.read()
        times.append((time.perf_counter() - t0) * 1000)
        assert resp.status == 200
    return {
        "server_ms": statistics.median(times),
        "html_kb": len(body) / 1024,
        "html_gzip_kb": len(gzip.compress(body, 6)) / 1024,
    }


def _report(profile: str, metrics: dict):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"{profile}.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    baseline = {}
    if BASELINE_FILE.exists():
        baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    if RECORD_BASELINE:
        baseline[profile] = metrics
        BASELINE_FILE.write_text(json.dumps(baseline, indent=2, sort_keys=True), encoding="utf-8")
        baseline = {}

    base = baseline.get(profile, {})
    lines = [f"\n=== dashboard perf: {profile} (CPU x{metrics['cpu_rate']:.1f}; {metrics.get('renderer', '?')}) ==="]
    for key, value in metrics.items():
        if not isinstance(value, (int, float)) or key == "cpu_rate":
            continue
        budget = BUDGETS.get("scroll_fps_min" if key == "scroll_fps" else key, {}).get(profile)
        line = f"  {key:<18} {value:>9.1f}"
        if budget is not None:
            line += f"   budget {budget}"
        if key in base and base[key]:
            line += f"   vs baseline {base[key]:.1f} ({(value - base[key]) / base[key] * 100:+.0f}%)"
        lines.append(line)
    print("\n".join(lines))


# ---------- сам тест ----------

@pytest.mark.parametrize("profile", list(PROFILES))
async def test_dashboard_page_performance(server, profile):
    client, cookie = server
    spec = PROFILES[profile]
    metrics = await _server_metrics(client, cookie)

    base_url = str(client.make_url("")).rstrip("/")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=_GPU_ARGS)
        try:
            host_ms = await _calibrate(browser)
            cpu_rate = max(1.0, min(20.0, spec["target_ms"] / host_ms))
            context = await browser.new_context(
                viewport=spec["viewport"], device_scale_factor=spec["dpr"],
                is_mobile=spec["mobile"], has_touch=spec["mobile"],
                reduced_motion="no-preference",
            )
            await context.add_cookies([{"name": f"session_{TOKEN}", "value": cookie, "url": base_url}])
            await context.add_init_script(_INIT_SCRIPT % {"mem": spec["device_memory"], "cores": spec["cores"]})
            page = await context.new_page()
            # Внешние шрифты и виджет Telegram — не наша страница и не детерминированы.
            await page.route("**/*", lambda route: route.abort()
                             if not route.request.url.startswith(base_url) else route.continue_())

            cdp = await context.new_cdp_session(page)
            await cdp.send("Emulation.setCPUThrottlingRate", {"rate": cpu_rate})
            if spec["network"]:
                net = spec["network"]
                await cdp.send("Network.enable")
                await cdp.send("Network.emulateNetworkConditions", {
                    "offline": False, "latency": net["latency"],
                    "downloadThroughput": net["down_kbps"] * 1000 / 8,
                    "uploadThroughput": net["up_kbps"] * 1000 / 8,
                })

            await page.goto(f"{base_url}/dashboard/{TOKEN}", wait_until="load")
            await page.wait_for_timeout(1000)
            metrics.update(await page.evaluate(_LOAD_METRICS))
            metrics.update(await page.evaluate(_EXPAND, 5))
            # Скролл — самая шумная метрика (фоновая нагрузка машины): медиана из трёх.
            runs = sorted([await page.evaluate(_SCROLL, 1500) for _ in range(3)], key=lambda r: r["scroll_fps"])
            metrics.update(runs[1])
            metrics["renderer"] = await page.evaluate(_RENDERER)
            metrics["cpu_rate"] = cpu_rate
            metrics["host_bench_ms"] = host_ms
            await context.close()
        finally:
            await browser.close()

    _report(profile, metrics)
    if RECORD_BASELINE:
        return  # базовая линия только записывается — старый код и не обязан укладываться

    failures = []
    for key, per_profile in BUDGETS.items():
        budget = per_profile.get(profile)
        metric = "scroll_fps" if key == "scroll_fps_min" else key
        if budget is None or metrics.get(metric) is None:
            continue
        if profile == "strong" and key in GPU_BOUND and "SwiftShader" in metrics.get("renderer", ""):
            print(f"  [skip] {key}: нет аппаратного GPU — бюджет strong не применяется")
            continue
        if key == "scroll_fps_min":
            if metrics["scroll_fps"] < budget:
                failures.append(f"scroll_fps {metrics['scroll_fps']:.1f} < {budget}")
        elif metrics[key] > budget:
            failures.append(f"{key} {metrics[key]:.1f} > {budget}")
    # GPU-тяжёлые эффекты эмуляция CPU не ловит — проверяем их отсутствие напрямую.
    if profile != "strong" and metrics["backdrop_elements"]:
        failures.append(f"{metrics['backdrop_elements']} элементов с backdrop-filter в облегчённом режиме")
    if metrics["filter_animations"]:
        failures.append(f"{metrics['filter_animations']} анимаций filter при раскрытии карточки")
    assert not failures, "; ".join(failures)
