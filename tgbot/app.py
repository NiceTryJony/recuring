"""Общие объекты бота: логгер, Bot, Dispatcher и планировщик."""

import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .constants import API_TOKEN, DEFAULT_TZ

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bot")

bot = Bot(token=API_TOKEN)

dp = Dispatcher(storage=MemoryStorage())

# misfire_grace_time по умолчанию = 1 сек: джоба, опоздавшая на секунду (например, после
# "засыпания" free-инстанса), молча пропускается. Даём 5 минут запаса.
scheduler = AsyncIOScheduler(timezone=DEFAULT_TZ, job_defaults={"coalesce": True, "misfire_grace_time": 300})
