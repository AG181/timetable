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

def sender_user_id(msg: Message) -> int | None:
    """user_id автора сообщения (для подписок на уведомления)."""
    if msg.sender is not None and msg.sender.user_id is not None:
        return msg.sender.user_id
    # в личных диалогах recipient == сам пользователь
    if msg.chat_id is None and msg.recipient.user_id is not None:
        return msg.recipient.user_id
    return None


def recipient_of(msg: Message) -> dict:
    """Аргументы для Bot.send_message, отвечающие в тот же чат/диалог."""
    if msg.chat_id is not None:
        return {"chat_id": msg.chat_id}
    return {"user_id": msg.recipient.user_id}


def kb_groups(groups: list[str]) -> InlineKeyboard:
    """Кнопки со списком групп; для каждой — расписание и подписка на напоминания."""
    kb = InlineKeyboard()
    buttons = []
    for g in groups[:60]:
        buttons.append(Button.callback(g, f"g:{g}"))
        buttons.append(Button.callback("🔔", f"n:{g}"))
    for i in range(0, len(buttons), 6):   # 3 группы в ряд (по 2 кнопки на группу)
        kb.row(*buttons[i:i + 6])
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
    uid = sender_user_id(msg)
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
    uid = sender_user_id(msg)
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
                             and u.callback.payload.split(":")[0] in ("g", "n")))
async def cb_group(cb: Callback) -> None:
    payload = cb.payload or ""
    kind, _, group = payload.partition(":")
    if kind == "n":  # кнопка «🔔 Включить напоминания» из /groups
        if cb.user is not None and cb.user.user_id is not None:
            SUBS.set(cb.user.user_id, group)
            await cb.answer(
                f"🔔 Напоминания за {config.NOTIFY_BEFORE_MINUTES} мин "
                f"до пар группы {group} включены. Отключить: /off"
            )
        else:
            await cb.answer("Не удалось определить пользователя 😕")
        return
    text = SOURCE.schedule_text(group)
    if not text:
        await cb.answer("Данные устарели — запросите /groups заново.")
        return
    if cb.message is not None:
        to = recipient_of(cb.message)
    elif cb.user is not None:
        to = {"user_id": cb.user.user_id}
    else:
        to = None
    await cb.answer(f"Расписание группы {group}")
    if to and (to.get("user_id") is not None or to.get("chat_id") is not None):
        await BOT.bot.send_message(text, **to)


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
    """Раз в NOTIFY_CHECK_INTERVAL_SECONDS проверяем: не пора ли предупредить.

    Важный нюанс: parse._fill_time проставляет lesson.start один раз при
    разборе PDF (на «ближайший» день недели от даты разбора). Поэтому здесь
    время начала каждой пары пересчитывается на актуальную дату, а не
    берётся из кэшированного объекта.
    """
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
            todays = {user_id: (SOURCE.get_days(group) or {}).get(now.weekday())
                      for user_id, group in subs}
            for user_id, group in subs:
                day = todays[user_id]
                if not day:
                    continue
                for lesson in day.lessons:
                    if lesson.start is None:
                        continue
                    # пересчитываем начало пары на сегодняшний день
                    start = dt.datetime.combine(now.date(),
                                                lesson.start.time())
                    mark = (user_id, now.date().isoformat(), lesson.num)
                    if mark in _sent:
                        continue
                    # окно: начало пары в пределах [now, now+5мин]
                    if now <= start <= cutoff:
                        try:
                            await send_reminder(bot, user_id, group,
                                                lesson, start)
                            _sent.add(mark)
                            log.info("Уведомление %s: пара %d в %s",
                                     user_id, lesson.num, start)
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
    """Один event loop: поллинг MAX + фоновый цикл уведомлений."""
    polling = asyncio.create_task(BOT.start_polling(timeout=30, limit=100))
    notifier = asyncio.create_task(notification_loop(BOT.bot))
    try:
        await polling
    finally:
        notifier.cancel()
        try:
            await notifier
        except asyncio.CancelledError:
            pass


def main() -> None:
    if not config.MAX_BOT_TOKEN:
        raise SystemExit(
            "Не задан токен бота. Установите переменную окружения MAX_BOT_TOKEN "
            "(получить токен можно у @MasterBot в мессенджере MAX)."
        )
    log.info("Старт бота. Сайт: %s%s", config.BASE_URL,
             config.TIMETABLE_PAGES[0] if config.TIMETABLE_PAGES else "")
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
