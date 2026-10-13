"""Статика дашборда (CSS/JS из dashboard/static/) и gzip-сжатие ответов.

Раньше весь CSS (десять с лишним килобайт на каждой странице, а на странице
задач — больше тридцати) инлайнился в HTML и качался заново при КАЖДОМ
открытии и каждом редиректе после действия. Теперь он отдаётся отдельным
файлом с хешем в имени и годовым immutable-кешем: телефон скачивает его один
раз за всё время, а HTML страницы задач становится в разы меньше. JS страницы
живёт там же и раздаётся тем же механизмом.

Сами стили и скрипты лежат обычными .css/.js-файлами (а не Python-строками с
задвоенными для .format() фигурными скобками) — их можно править и
проверять линтером как обычный фронтенд."""

import gzip
import hashlib
import re
from pathlib import Path

from aiohttp import web as aioweb

STATIC_DIR = Path(__file__).with_name("static")
_STATIC_CACHE_SECONDS = 365 * 24 * 3600
_CONTENT_TYPES = {".css": "text/css", ".js": "application/javascript", ".woff2": "font/woff2"}
# имя файла -> (тело, ETag, предсжатое gzip-тело или None, content-type)
_STATIC: dict[str, tuple[bytes, str, bytes | None, str]] = {}


def _minify_css(css: str) -> str:
    """Срезает комментарии и лишние пробелы. Комментарии в CSS-файлах —
    подробные и на русском, читателю исходника они нужны, телефону — нет."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"\s+", " ", css)
    css = re.sub(r"\s*([{};,])\s*", r"\1", css)
    css = re.sub(r"(?<=[{;:,])\s+", "", css)
    css = re.sub(r";}", "}", css)
    return css.strip()


def _minify_js(js: str) -> str:
    """Только построчные комментарии и отступы — без настоящего минификатора
    (в зависимостях его нет, а gzip всё равно съедает пробелы почти даром)."""
    lines = []
    for line in js.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("//"):
            lines.append(stripped)
    return "\n".join(lines)


def publish(filename: str) -> str:
    """Регистрирует static/<filename> и возвращает его URL. Хеш в имени делает
    кеш самоинвалидирующимся: правка файла меняет URL, а старый файл браузеру
    можно держать вечно."""
    path = STATIC_DIR / filename
    stem, ext = path.stem, path.suffix
    text = path.read_text(encoding="utf-8")
    body = (_minify_css(text) if ext == ".css" else _minify_js(text)).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()[:12]
    name = f"{stem}.{digest}{ext}"
    # Содержимое статики не меняется за всё время жизни процесса, поэтому gzip
    # считаем ровно один раз здесь, при импорте, с максимальным уровнем — а не
    # заново на каждый запрос, как это делал бы общий middleware.
    _STATIC[name] = (body, f'"{digest}"', gzip.compress(body, 9), _CONTENT_TYPES[ext])
    return f"/dashboard/static/{name}"


def publish_font(filename: str) -> None:
    """Шрифты .woff2 отдаются под своим именем (версия шрифта — в имени файла,
    на него ссылается @font-face в base.css) и без gzip: woff2 уже сжат."""
    body = (STATIC_DIR / filename).read_bytes()
    digest = hashlib.sha256(body).hexdigest()[:12]
    _STATIC[filename] = (body, f'"{digest}"', None, _CONTENT_TYPES[".woff2"])


for _font in sorted(STATIC_DIR.glob("*.woff2")):
    publish_font(_font.name)

BASE_CSS_URL = publish("base.css")
DASHBOARD_JS_URL = publish("dashboard.js")
# Эффекты для ПК: в HTML их нет, детектор в pages.py подгружает оба файла
# только когда ставит класс fx (на телефонах они не скачиваются вовсе).
FX_CSS_URL = publish("fx.css")
FX_JS_URL = publish("fx.js")


def stylesheets(page_css_file: str) -> str:
    """Две таблицы стилей на страницу: общая база (одна для всех страниц — её
    телефон скачивает единожды и переиспользует при переходах) и небольшой
    блок конкретной страницы."""
    return (f'<link rel="stylesheet" href="{BASE_CSS_URL}">\n'
            f'<link rel="stylesheet" href="{publish(page_css_file)}">')


async def handle_static(request: aioweb.Request) -> aioweb.Response:
    """GET /dashboard/static/{filename} — статика не приватна (ни одной крупицы
    данных владельца в ней нет), поэтому отдаётся с public-кешем и без токена.
    ETag — чтобы даже при принудительном обновлении страницы возвращался 304
    с пустым телом, а не файл целиком."""
    entry = _STATIC.get(request.match_info["filename"])
    if entry is None:
        return aioweb.Response(status=404)
    body, etag, gz_body, content_type = entry
    if request.headers.get("If-None-Match") == etag:
        return aioweb.Response(status=304, headers={"ETag": etag})
    headers = {"Cache-Control": f"public, max-age={_STATIC_CACHE_SECONDS}, immutable",
               "ETag": etag, "Vary": "Accept-Encoding"}
    if gz_body is not None and "gzip" in request.headers.get("Accept-Encoding", "").lower():
        headers["Content-Encoding"] = "gzip"
        body = gz_body
    return aioweb.Response(body=body, content_type=content_type, charset="utf-8", headers=headers)


# ---------- сжатие ----------
# aiohttp по умолчанию не сжимает ничего. HTML дашборда — это текст с огромной
# долей повторов (однотипные карточки, одинаковые формы), он жмётся в 8-10 раз:
# 48 КБ -> ~12 КБ на одной задаче и 116 КБ -> ~13 КБ на тридцати. На мобильной
# сети это самая крупная экономия из всех возможных.
_COMPRESS_MIN_BYTES = 700
_COMPRESSIBLE_TYPES = ("text/html", "text/css", "application/json", "text/plain", "application/javascript")


@aioweb.middleware
async def compression_middleware(request: aioweb.Request, handler):
    resp = await handler(request)
    try:
        if (isinstance(resp, aioweb.Response)
                and "Content-Encoding" not in resp.headers     # уже предсжато (статика)
                and resp.content_type in _COMPRESSIBLE_TYPES
                and (resp.content_length or 0) >= _COMPRESS_MIN_BYTES):
            # Кодировку выбираем явно. Сам enable_compression() без аргумента
            # перебирает ContentCoding в порядке объявления и выбрал бы
            # "deflate" даже тому клиенту, который просит gzip; gzip же
            # понимают абсолютно все и никакие прокси его не путают с
            # raw-deflate.
            accept = request.headers.get("Accept-Encoding", "").lower()
            if "gzip" in accept:
                resp.enable_compression(aioweb.ContentCoding.gzip)
            elif "deflate" in accept:
                resp.enable_compression(aioweb.ContentCoding.deflate)
            else:
                return resp
            vary = resp.headers.get("Vary")
            if not vary:
                resp.headers["Vary"] = "Accept-Encoding"
            elif "accept-encoding" not in vary.lower():
                resp.headers["Vary"] = f"{vary}, Accept-Encoding"
    except (AttributeError, TypeError):
        # Потоковые/нестандартные ответы просто не сжимаем — отдать ответ
        # важнее, чем сэкономить на нём байты.
        pass
    return resp
