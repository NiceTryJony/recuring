"""Состояния FSM (aiogram StatesGroup)."""

from aiogram.fsm.state import State, StatesGroup


class SaveTemplate(StatesGroup):
    title = State()
    time_of_day = State()
    day_offset = State()

class AddTask(StatesGroup):
    title = State()
    date = State()
    repeat = State()
    remind = State()
    nag = State()
    tag = State()

class EditTask(StatesGroup):
    value = State()

class AddSubtasks(StatesGroup):
    collecting = State()

class AddTaskPhoto(StatesGroup):
    collecting = State()

class SetTimezone(StatesGroup):
    value = State()

class SetDashboardPassword(StatesGroup):
    value = State()

class FindTask(StatesGroup):
    query = State()

class SetQuiet(StatesGroup):
    value = State()
