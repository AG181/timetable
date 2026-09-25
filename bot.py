"""Бот мессенджера MAX: расписание пар колледжа ЛПК (Лангепас).

Расписание берётся с сайта collegelan.ru (PDF-файлы по группам),
бот скачивает их, разбирает и отвечает ТЕКСТОМ.

Команды:
  /start, /help      — приветствие и справка
  <номер группы>     — просто напиши группу (например 23-29) → текст-расписание
  /schedule <группа> — то же самое командой
  /groups            — список всех групп, найденных в PDF на сайте
  /notify <группа>   — включить уведомления за 5 минут до начала каждой пары
  /off               — отключить уведомления
  /today             — ссылка на страницу расписания на сайте
  /refresh           — принудительно обновить кэш с сайта

Запуск:
  MAX_BOT_TOKEN=<токен> python bot.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from maxio import Bot, Button, InlineKeyboard, MaxBot
from maxio.filters import Command
from maxio.types import Callback, Message

import config
from timetable import TimetableSource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("lppk-bot")

TZ = ZoneInfo(config.TIMEZONE)
SOURCE = TimetableSource()

HELP_TEXT = (
    "📚 Бот расписания ЛПК, г. Лангепас\n"
    "\n"
    "Просто напиши номер своей группы — пришлю расписание текстом.\n"
    "Например: 23-29 или 2421\n"
    "\n"
    "Команды:\n"
    "/schedule <группа> — расписание группы текстом\n"
    "/groups — список всех доступных групп\n"
    f"/notify <группа> — напоминания за {config.NOTIFY_BEFORE_MINUTES} мин "
    "до начала каждой пары\n"
    "/off — отключить напоминания\n"
    "/today — расписание на сайте\n"
    "/refresh — обновить данные с сайта\n"
    "/help — эта справка"
)


def now_local() -> dt.datetime:
    return dt.datetime.now(TZ).replace(tzinfo=None)  # «наивное» местное время


# ------------------------------------------------------------- подписки ----

class Subscriptions:
    """user_id -> группа; храним в JSON, чтобы переживать перезапуск бота."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._data: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            log.exception("Не удалось прочитать %s", self.path)

    def _save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            log.exception("Не удалось сохранить %s", self.path)

    def set(self, user_id: int, group: str) -> None:
        self._data[str(user_id)] = group
        self._save()

    def remove(self, user_id: int) -> bool:
        existed = str(user_id) in self._data
        self._data.pop(str(user_id), None)
        self._save()
        return existed

    def items(self) -> list[tuple[int, str]]:
        return [(int(k), v) for k, v in self._data.items()]


SUBS = Subscriptions(config.SUBSCRIPTIONS_FILE)

# Отправленные уведомления: (user_id, дата, номер пары) — чтобы не дублировать
_sent: set[tuple[int, str, int]] = set()


# ---------------------------------------------------------------- helpers ---

def recipient_of(msg: Message) -> dict:
    if msg.chat_id is not None:
        return {"chat_id": msg.chat_id}
    uid = msg.recipient.user_id or (msg.sender.user_id if msg.sender else None)
    return {"user_id": uid}


def kb_groups(groups: list[str]) -> InlineKeyboard:
    kb = InlineKeyboard()
    buttons = [Button.callback(g, f"g:{g}") for g in groups[:60]]
    for i in range(0, len(buttons), 4):
        kb.row(*buttons[i:i + 4])
    return kb


async def ensure_ready(force: bool = False) -> bool:
    try:
        await SOURCE.refresh(force=force)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Ошибка загрузки с сайта: %s", exc)
        return False


async def reply_schedule(msg: Message, query: str) -> None:
    """Отвечает на сообщение текстовым расписанием по запросу группы."""
    query = (query or "").strip()
    if not query:
        await msg.answer("Напиши номер группы, например: /schedule 23-29")
        return
    if not await ensure_ready():
        await msg.answer(
            "⚠️ Не удалось загрузить расписание с сайта колледжа.\n"
            f"Попробуйте позже или откройте сайт вручную: "
            f"{config.BASE_URL}{config.TIMETABLE_PAGES[0]}"
        )
        return
    matches = SOURCE.match_groups(query)
    if not matches:
        sample = ", ".join(SOURCE.all_groups()[:20])
        await msg.answer(
            f"❌ Группа «{query}» не найдена в файлах расписания.\n"
            f"Доступные группы: {sample or 'нет'}…"
        )
        return
    for group in matches:
        text = SOURCE.schedule_text(group)
        if text:
            await msg.answer(text)
    if len(matches) > 1:
        await msg.answer(
            "Нашено несколько подходящих групп — уточните номер полностью."
        )


# --------------------------------------------------------------- handlers ---

BOT = MaxBot(config.MAX_BOT_TOKEN or "dummy-token-until-startup")


@BOT.message(Command("start", "help"))
async def cmd_start(msg: Message) -> None:
    await msg.answer(HELP_TEXT)


@BOT.message(Command("refresh"))
async def cmd_refresh(msg: Message) -> None:
    ok = await ensure_ready(force=True)
    if ok:
        await msg.answer(
            f"✅ Данные с сайта обновлены. Файлов: {len(SOURCE.files)}, "
            f"групп: {len(SOURCE.all_groups())}."
        )
    else:
        await msg.answer("⚠️ Обновить не удалось, попробуйте позже.")


@BOT.message(Command("today"))
async def cmd_today(msg: Message) -> None:
    url = config.BASE_URL + (config.TIMETABLE_PAGES[0]
                             if config.TIMETABLE_PAGES else "")
    today = now_local().strftime("%A")
    await msg.answer(f"🗓 Расписание на сайте: {url}\nСегодня: {today}")


@BOT.message(Command("groups"))
async def cmd_groups(msg: Message) -> None:
    if not await ensure_ready():
        await msg.answer("⚠️ Не удалось загрузить данные с сайта.")
        return
    groups = SOURCE.all_groups()
    if not groups:
        await msg.answer("На сайте пока не найдено ни одной группы.")
        return
    await msg.answer(
        f"Найдено групп: {len(groups)}. Нажми на свою 👇",
        keyboard=kb_groups(groups),
    )


@BOT.message(Command("schedule"))
async def cmd_schedule(msg: Message) -> None:
    parts = (msg.text or "").split(maxsplit=1)
    await reply_schedule(msg, parts[1] if len(parts) > 1 else "")


@BOT.message(Command("notify"))
async def cmd_notify(msg: Message) -> None:
    parts = (msg.text or "").split(maxsplit=1)
    query = (parts[1] if len(parts) > 1 else "").strip()
    if not query:
        await msg.answer("Напиши команду со своей группой, "
                         "например: /notify 23-29")
        return
    if not await ensure_ready():
        await msg.answer("⚠️ Не удалось загрузить данные с сайта.")
        return
    matches = SOURCE.match_groups(query)
    if not matches:
        await msg.answer(f"❌ Группа «{query}» не найдена. "
                         "Посмотри список через /groups.")
        return
    group = matches[0]
    uid = msg.sender.user_id if msg.sender else msg.recipient.user_id
    if uid is None:
        await msg.answer("Не могу определить ваш user_id 😕")
        return
    SUBS.set(uid, group)
    upcoming = SOURCE.upcoming_lessons(group, now_local(), limit=3)
    nxt = ""
    if upcoming:
        ls, start = upcoming[0]
        nxt = f"\nБлижайшая пара: {start:%d.%m %H:%M} — {ls.subject}"
    await msg.answer(
        f"🔔 Готово! Буду напоминать за {config.NOTIFY_BEFORE_MINUTES} мин "
        f"до начала каждой пары группы {group}.{nxt}\n"
        "Отключить: /off"
    )


@BOT.message(Command("off"))
async def cmd_off(msg: Message) -> None:
    uid = msg.sender.user_id if msg.sender else msg.recipient.user_id
    if uid is not None and SUBS.remove(uid):
        await msg.answer("🔕 Напоминания отключены.")
    else:
        await msg.answer("У вас не было включённых напоминаний.")


@BOT.message()
async def any_text(msg: Message) -> None:
    """Любое текстовое сообщение (не команда) считаем названием группы."""
    text = (msg.text or "").strip()
    if not text or text.startswith("/"):
        return
    await reply_schedule(msg, text)


@BOT.callback(lambda u: bool(u.callback and u.callback.payload
                             and u.callback.payload.startswith("g:")))
async def cb_group(cb: Callback) -> None:
    group = (cb.payload or "").split(":", 1)[-1]
    text = SOURCE.schedule_text(group)
    if not text:
        await cb.answer("Данные устарели — запросите /groups заново.")
        return
    to = recipient_of(cb.message) if cb.message is not None else \
        ({"user_id": cb.user.user_id} if cb.user else None)
    await cb.answer("Отправляю расписание…")
    if to:
        await cb.bot.send_message(text, **to)


# ------------------------------------------------------------ notifications -

async def send_reminder(bot: Bot, user_id: int, group: str, lesson,
                        start: dt.datetime) -> None:
    minutes_left = max(
        0, round((start - now_local()).total_seconds() / 60)
    )
    text = (
        f"🔔 Через {minutes_left} мин — пара №{lesson.num} у группы {group}!\n"
        f"📖 {lesson.subject}\n"
        + (f"👤 {lesson.teacher}\n" if lesson.teacher else "")
        + (f"🚪 ауд. {lesson.room}\n" if lesson.room else "")
        + f"🕐 {start:%H:%M}–{lesson.end:%H:%M}"
    )
    await bot.send_message(text, user_id=user_id)


async def notification_loop(bot: Bot) -> None:
    """Раз в NOTIFY_CHECK_INTERVAL_SECONDS проверяем: не пора ли предупредить."""
    log.info("Цикл уведомлений запущен (tz=%s, за %d мин до пары)",
             config.TIMEZONE, config.NOTIFY_BEFORE_MINUTES)
    while True:
        try:
            await asyncio.sleep(config.NOTIFY_CHECK_INTERVAL_SECONDS)
            subs = SUBS.items()
            if not subs:
                continue
            if not await ensure_ready():
                continue
            now = now_local()
            cutoff = now + dt.timedelta(minutes=config.NOTIFY_BEFORE_MINUTES)
            for user_id, group in subs:
                days = SOURCE.get_days(group) or {}
                todays = days.get(now.weekday())
                if not todays:
                    continue
                for lesson in todays.lessons:
                    if lesson.start is None:
                        continue
                    mark = (user_id, now.date().isoformat(), lesson.num)
                    if mark in _sent:
                        continue
                    # окно: начало пары в пределах [now, now+5мин]
                    if now <= lesson.start <= cutoff:
                        try:
                            await send_reminder(bot, user_id, group,
                                                lesson, lesson.start)
                            _sent.add(mark)
                            log.info("Уведомление %s: пара %d в %s",
                                     user_id, lesson.num, lesson.start)
                        except Exception:  # noqa: BLE001
                            log.exception("Не удалось отправить уведомление")
            # чистим отметки за прошлые дни
            today = now.date().isoformat()
            _sent.intersection_update(
                {m for m in _sent if m[1] == today}
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в цикле уведомлений")


# ------------------------------------------------------------------- main ---

async def async_main() -> None:
    await BOT.start_polling(handle_signals=False)
    try:
        await asyncio.create_task(notification_loop(BOT.bot))
    finally:
        await BOT.close()


def main() -> None:
    if not config.MAX_BOT_TOKEN:
        raise SystemExit(
            "Не задан токен бота. Установите переменную окружения MAX_BOT_TOKEN "
            "(получить токен можно у @MasterBot в мессенджере MAX)."
        )
    log.info("Старт бота. Сайт: %s%s", config.BASE_URL,
             config.TIMETABLE_PAGES[0] if config.TIMETABLE_PAGES else "")
    try:
        BOT.run()          # внутри запускается asyncio loop
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
