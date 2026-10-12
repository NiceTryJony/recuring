"""Картинки дашборда: аватарки участников (из Telegram Bot API) и фото задач —
сжатие, превью, загрузка, отдача с ETag."""

import asyncio
import io

import aiohttp
from aiohttp import web as aioweb
from PIL import Image

import db

from .config import AVATAR_CACHE_SECONDS, BOT_TOKEN, _acting_user_id, logger
from .security import _require_session, _verify_csrf

# ---------- аватарки профиля (Telegram Bot API) ----------
# Telegram не присылает фото профиля сам по себе — ни в Login Widget, ни в
# обычных апдейтах бота. Один раз при логине через виджет: getUserProfilePhotos
# (file_id) -> getFile (file_path, живёт ~1ч) -> скачиваем байты -> сжимаем до
# AVATAR_MAX_SIDE и кладём в БД как JPEG. Дальше показ аватарки — это просто
# SELECT из БД, без единого обращения к Telegram на каждый просмотр дашборда.
AVATAR_MAX_SIDE = 128
AVATAR_JPEG_QUALITY = 82

# ---------- фото задач ----------
TASK_PHOTO_MAX_SIDE = 1280
TASK_PHOTO_JPEG_QUALITY = 85
TASK_PHOTO_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # лимит на входящий файл ДО сжатия
# Превью. В ленте одно фото показывается во всю ширину карточки (на телефоне
# это ~340 CSS-пикселей), а в карусели — вообще в 118px. Полный кадр на 1280px
# весит в среднем 250-400 КБ: пять фото в одной задаче — это под два мегабайта
# трафика ради картинок, которые физически не могут показать такую детализацию.
# 560px по длинной стороне хватает и для крупного показа на retina-телефоне, а
# весит такое превью примерно в пять раз меньше. Полный кадр остаётся и
# отдаётся только по тапу — в лайтбоксе.
TASK_PHOTO_THUMB_MAX_SIDE = 560
TASK_PHOTO_THUMB_QUALITY = 76


async def _fetch_and_compress_photo(tg_user_id: int) -> tuple[bytes, str] | None:
    if not BOT_TOKEN:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUserProfilePhotos",
                params={"user_id": tg_user_id, "limit": 1}, timeout=10,
            ) as resp:
                photos_data = await resp.json()
            if not photos_data.get("ok") or not photos_data["result"]["photos"]:
                return None
            file_id = photos_data["result"]["photos"][0][-1]["file_id"]  # самый крупный размер

            async with session.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                params={"file_id": file_id}, timeout=10,
            ) as resp:
                file_data = await resp.json()
            if not file_data.get("ok"):
                return None
            file_path = file_data["result"]["file_path"]

            async with session.get(
                f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}", timeout=10,
            ) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.read()
    except Exception:
        logger.exception("Не удалось скачать фото профиля для user_id=%s", tg_user_id)
        return None

    try:
        img = Image.open(io.BytesIO(raw))
        img = img.convert("RGB")
        img.thumbnail((AVATAR_MAX_SIDE, AVATAR_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=AVATAR_JPEG_QUALITY, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        logger.exception("Не удалось сжать фото профиля для user_id=%s", tg_user_id)
        return None


# ---------- фото задач ----------




def compress_task_photo(raw: bytes) -> tuple[bytes, str] | None:
    """Сжимает присланные байты (откуда угодно — с дашборда или из бота) до
    JPEG разумного размера. Возвращает None, если это не декодируется как
    изображение (битый файл, не тот content-type) — вызывающая сторона должна
    явно отклонить загрузку в этом случае, а не падать с 500."""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()  # форсируем декодирование сейчас, а не лениво при .save() —
                     # иначе битый файл всплывёт позже менее понятной ошибкой
        img = img.convert("RGB")
        img.thumbnail((TASK_PHOTO_MAX_SIDE, TASK_PHOTO_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=TASK_PHOTO_JPEG_QUALITY, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        logger.exception("Не удалось декодировать/сжать фото задачи")
        return None


def make_task_photo_thumb(data: bytes) -> bytes | None:
    """Делает превью из уже сжатого кадра (дешевле, чем из исходника). None —
    если кадр не декодируется; вызывающая сторона тогда просто живёт без
    превью и отдаёт полный файл, как раньше."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        img = img.convert("RGB")
        img.thumbnail((TASK_PHOTO_THUMB_MAX_SIDE, TASK_PHOTO_THUMB_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=TASK_PHOTO_THUMB_QUALITY, optimize=True)
        return buf.getvalue()
    except Exception:
        logger.exception("Не удалось сделать превью фото задачи")
        return None


async def handle_task_photo_upload(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/tasks/{task_id}/photo — загрузка фото с дашборда
    через multipart-форму. Та же сессия+CSRF защита, что и у остальных действий
    над задачей; лимит MAX_PHOTOS_PER_TASK проверяется здесь же (не в БД), так
    что при гонке двух одновременных загрузок возможен разовый перелимит на 1 —
    не считаем это проблемой для личного/семейного масштаба использования."""
    token = request.match_info["token"]
    try:
        task_id = int(request.match_info["task_id"])
    except ValueError:
        return aioweb.Response(status=404)

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, tg_user_id = owner
    acting_user_id = _acting_user_id(owner_id, owner_type, tg_user_id)

    task = await db.get_task(task_id)
    if not task or task["owner_id"] != owner_id or task["owner_type"] != owner_type:
        return aioweb.Response(status=404)

    cookie = request.cookies.get(f"session_{token}", "")
    reader = await request.multipart()
    got_file = False
    csrf_token = ""
    # bytearray, а не bytes: "+=" на bytes копирует весь накопленный буфер на
    # каждый чанк — на 10-мегабайтном файле это квадратичная работа.
    file_bytes = bytearray()
    # CSRF проверяется ПОСЛЕ цикла — порядок частей в multipart роли не играет;
    # читать байты до проверки безопасно, потому что сессия уже проверена выше,
    # а объём ограничен TASK_PHOTO_MAX_UPLOAD_BYTES.
    async for part in reader:
        if part.name == "csrf":
            csrf_token = (await part.read()).decode("utf-8", errors="ignore")
        elif part.name == "photo":
            got_file = True
            # Читаем с ограничением, чтобы не раздувать память на огромном файле —
            # читаем по чанку и обрываем, как только превысили лимит.
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                file_bytes += chunk
                if len(file_bytes) > TASK_PHOTO_MAX_UPLOAD_BYTES:
                    return aioweb.Response(status=413)

    if not _verify_csrf(csrf_token, cookie):
        return aioweb.Response(status=403)
    if not got_file or not file_bytes:
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})

    if await db.count_task_photos(task_id) >= db.MAX_PHOTOS_PER_TASK:
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})

    # Декодирование и пересжатие кадра до 1280px — сотни миллисекунд CPU;
    # в event loop это замораживало все остальные запросы процесса (включая бота).
    compressed = await asyncio.to_thread(compress_task_photo, bytes(file_bytes))
    if compressed is None:
        return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}?error=bad_photo"})
    data, mime = compressed
    # Превью считаем здесь же, а не при первом показе: сжатие уже произошло,
    # картинка в памяти, лишних миллисекунд это почти не стоит.
    thumb = await asyncio.to_thread(make_task_photo_thumb, data)
    await db.add_task_photo(task_id, data, mime, uploaded_by=acting_user_id, thumb=thumb)
    await db.log_history(task_id, acting_user_id, task["title"], "photo_added")

    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})


def _not_modified(request: aioweb.Request, etag: str) -> aioweb.Response | None:
    """304 вместо повторной отдачи тех же байтов. max-age у картинок — час, и
    раньше по его истечении телефон качал каждое фото целиком заново; теперь
    при неизменившемся ETag ответ — пустой 304."""
    if request.headers.get("If-None-Match") == etag:
        return aioweb.Response(status=304, headers={"ETag": etag})
    return None


def _image_headers(etag: str) -> dict:
    # private, а не public: доступ к картинке держится только на секретности
    # токена дашборда, и промежуточному прокси кешировать её и отдавать
    # кому-то ещё нельзя.
    return {"Cache-Control": f"private, max-age={AVATAR_CACHE_SECONDS}", "ETag": etag}


async def handle_task_photo_get(request: aioweb.Request) -> aioweb.Response:
    """GET /dashboard/{token}/tasks/{task_id}/photo/{photo_id} — отдаёт байты.
    ?size=thumb отдаёт превью (его и просит лента), без параметра — полный
    кадр (его просит лайтбокс по тапу).

    Как и у аватарок, доступ не завязан на сессию (обычный <img src>), но
    привязан к валидному токену дашборда + проверке, что фото реально
    принадлежит задаче этого владельца (иначе можно перебирать чужие photo_id)."""
    token = request.match_info["token"]
    settings = await db.get_owner_by_dashboard_token(token)
    if not settings:
        return aioweb.Response(status=404)
    try:
        task_id = int(request.match_info["task_id"])
        photo_id = int(request.match_info["photo_id"])
    except ValueError:
        return aioweb.Response(status=404)

    task = await db.get_task(task_id)
    if not task or task["owner_id"] != settings["owner_id"] or task["owner_type"] != settings["owner_type"]:
        return aioweb.Response(status=404)

    want_thumb = request.query.get("size") == "thumb"
    etag = f'"{photo_id}-{"t" if want_thumb else "f"}"'
    cached = _not_modified(request, etag)
    if cached is not None:
        return cached

    if want_thumb:
        # Отдельный запрос без колонки data: превью весит десятки килобайт, а
        # полный кадр — сотни, и тащить его из БД, чтобы отдать превью, — это
        # ровно та же лишняя работа, только на стороне сервера.
        row = await db.get_task_photo_thumb(photo_id)
        if row is None or row[1] != task_id:
            return aioweb.Response(status=404)
        thumb = row[0]
        if thumb is None:
            # Фото загружено до появления превью (или пришло из бота) — делаем
            # превью сейчас и сохраняем, чтобы следующий показ обошёлся без
            # пересжатия. Только в этом случае и нужен полный кадр.
            full = await db.get_task_photo(photo_id)
            if not full or full["task_id"] != task_id:
                return aioweb.Response(status=404)
            thumb = await asyncio.to_thread(make_task_photo_thumb, full["data"])
            if thumb:
                await db.set_task_photo_thumb(photo_id, thumb)
            else:
                # Превью не сделалось (битый кадр) — отдаём как есть, лишь бы
                # картинка показалась.
                return aioweb.Response(body=full["data"], content_type=full["mime"],
                                       headers=_image_headers(etag))
        return aioweb.Response(body=thumb, content_type="image/jpeg", headers=_image_headers(etag))

    photo = await db.get_task_photo(photo_id)
    if not photo or photo["task_id"] != task_id:
        return aioweb.Response(status=404)

    return aioweb.Response(body=photo["data"], content_type=photo["mime"], headers=_image_headers(etag))


async def handle_task_photo_delete(request: aioweb.Request) -> aioweb.Response:
    """POST /dashboard/{token}/tasks/{task_id}/photo/{photo_id}/delete."""
    token = request.match_info["token"]
    try:
        task_id = int(request.match_info["task_id"])
        photo_id = int(request.match_info["photo_id"])
    except ValueError:
        return aioweb.Response(status=404)

    owner = await _require_session(request, token)
    if owner is None:
        return aioweb.Response(status=403)
    owner_id, owner_type, tg_user_id = owner
    acting_user_id = _acting_user_id(owner_id, owner_type, tg_user_id)

    task = await db.get_task(task_id)
    if not task or task["owner_id"] != owner_id or task["owner_type"] != owner_type:
        return aioweb.Response(status=404)

    cookie = request.cookies.get(f"session_{token}", "")
    data = await request.post()
    if not _verify_csrf(data.get("csrf", ""), cookie):
        return aioweb.Response(status=403)

    await db.delete_task_photo(photo_id, task_id)
    await db.log_history(task_id, acting_user_id, task["title"], "photo_removed")

    return aioweb.Response(status=302, headers={"Location": f"/dashboard/{token}"})


async def handle_avatar(request: aioweb.Request) -> aioweb.Response:
    """GET /dashboard/{token}/avatar/{user_id} — отдаёт уже сжатую картинку из БД.
    Доступ не завязан на сессию (как и сами изображения в <img>), но токен
    дашборда должен быть валиден, иначе id участников чата можно перебирать."""
    token = request.match_info["token"]
    settings = await db.get_owner_by_dashboard_token(token)
    if not settings:
        return aioweb.Response(status=404)
    try:
        user_id = int(request.match_info["user_id"])
    except ValueError:
        return aioweb.Response(status=404)

    etag = f'"av{user_id}"'
    cached = _not_modified(request, etag)
    if cached is not None:
        return cached

    photo = await db.get_telegram_user_photo(user_id)
    if not photo:
        return aioweb.Response(status=404)
    data, mime = photo
    return aioweb.Response(body=data, content_type=mime, headers=_image_headers(etag))
