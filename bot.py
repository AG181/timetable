"""Бот мессенджера MAX: расписание пар колледжа ЛПК (Лангепас).

Расписание берётся с сайта collegelan.ru. Основной интерфейс построен на
inline-кнопках: меню -> группа -> день. Текстовые команды оставлены только как
совместимый запасной вариант.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from maxio import Bot, Button, InlineKeyboard, MaxBot, Update
from maxio.filters import Command
from maxio.types import Callback, Message

import config
import parse
from timetable import TimetableSource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("lppk-bot")

TZ = ZoneInfo(config.TIMEZONE)
SOURCE = TimetableSource()

GROUP_PAGE_SIZE = 20
WEEKDAYS_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

MENU_TEXT = (
    "📚 Расписание ЛПК, г. Лангепас\n\n"
    "Выберите группу, а затем нужный день. Всё управляется кнопками 👇"
)

HELP_TEXT = (
    "ℹ️ Как пользоваться ботом\n\n"
    "1. Нажмите «Расписание».\n"
    "2. Выберите свою группу.\n"
    "3. Выберите день недели — бот отправит расписание только на этот день.\n"
    "4. При желании включите напоминания кнопкой 🔔.\n\n"
    "Кнопка «Сегодня» показывает расписание на текущий день."
)


def now_local() -> dt.datetime:
    return dt.datetime.now(TZ).replace(tzinfo=None)


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

    def get(self, user_id: int) -> str | None:
        return self._data.get(str(user_id))

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
_sent: set[tuple[int, str, int]] = set()


# ---------------------------------------------------------------- helpers ---

def sender_user_id(msg: Message) -> int | None:
    if msg.sender is not None and msg.sender.user_id is not None:
        return msg.sender.user_id
    if msg.chat_id is None and msg.recipient.user_id is not None:
        return msg.recipient.user_id
    return None


def recipient_of(msg: Message) -> dict:
    if msg.chat_id is not None:
        return {"chat_id": msg.chat_id}
    return {"user_id": msg.recipient.user_id}


def page_count(groups: list[str]) -> int:
    return max(1, (len(groups) + GROUP_PAGE_SIZE - 1) // GROUP_PAGE_SIZE)


def clamp_page(page: int, groups: list[str]) -> int:
    return max(0, min(page, page_count(groups) - 1))


def group_page(group: str) -> int:
    groups = SOURCE.all_groups()
    for page in range(page_count(groups)):
        start = page * GROUP_PAGE_SIZE
        if group in groups[start:start + GROUP_PAGE_SIZE]:
            return page
    return 0


def kb_main() -> InlineKeyboard:
    kb = InlineKeyboard()
    kb.row(
        Button.callback("📅 Расписание", "groups:0"),
        Button.callback("📅 Сегодня", "today:auto"),
    )
    kb.row(
        Button.callback("🔔 Уведомления", "sub"),
        Button.callback("🔄 Обновить", "refresh"),
    )
    kb.row(Button.callback("❓ Помощь", "help"))
    return kb


def kb_groups(groups: list[str], page: int = 0) -> InlineKeyboard:
    """Клавиатура групп с постраничным списком."""
    page = clamp_page(page, groups)
    pages = page_count(groups)
    start = page * GROUP_PAGE_SIZE
    visible = groups[start:start + GROUP_PAGE_SIZE]

    kb = InlineKeyboard()
    buttons = [Button.callback(group, f"g:{page}:{group}") for group in visible]
    for i in range(0, len(buttons), 4):
        kb.row(*buttons[i:i + 4])

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(Button.callback("⬅️", f"gp:{page - 1}"))
        nav.append(Button.callback(f"{page + 1}/{pages}", f"gp:{page}"))
        if page + 1 < pages:
            nav.append(Button.callback("➡️", f"gp:{page + 1}"))
        kb.row(*nav)

    kb.row(Button.callback("🏠 Меню", "menu"))
    return kb


def kb_group_choices(groups: list[str]) -> InlineKeyboard:
    kb = InlineKeyboard()
    buttons = [Button.callback(group, f"g:0:{group}") for group in groups[:20]]
    for i in range(0, len(buttons), 4):
        kb.row(*buttons[i:i + 4])
    kb.row(
        Button.callback("📋 Все группы", "groups:0"),
        Button.callback("🏠 Меню", "menu"),
    )
    return kb


def kb_days(group: str, page: int = 0) -> InlineKeyboard:
    """Кнопки дней для выбранной группы."""
    days = SOURCE.get_days(group) or {}
    kb = InlineKeyboard()
    kb.row(
        Button.callback("📅 Сегодня", f"today:{group}"),
        Button.callback("🔔 Напоминания", f"n:{group}"),
    )

    today = now_local().weekday()
    buttons = []
    for weekday in sorted(days):
        date = parse.next_weekday_date(weekday)
        marker = " •" if weekday == today else ""
        label = (
            f"{WEEKDAYS_SHORT[weekday]} {date:%d.%m}{marker} "
            f"({len(days[weekday].lessons)})"
        )
        buttons.append(Button.callback(label, f"d:{group}:{weekday}"))
    for i in range(0, len(buttons), 3):
        kb.row(*buttons[i:i + 3])

    kb.row(
        Button.callback("⬅️ Группы", f"groups:{page}"),
        Button.callback("🏠 Меню", "menu"),
    )
    return kb


def kb_day_actions(group: str, page: int = 0) -> InlineKeyboard:
    kb = InlineKeyboard()
    kb.row(
        Button.callback("⬅️ Выбор дня", f"g:{page}:{group}"),
        Button.callback("📅 Сегодня", f"today:{group}"),
        Button.callback("🔔 Напоминания", f"n:{group}"),
    )
    kb.row(
        Button.callback("⬅️ Группы", f"groups:{page}"),
        Button.callback("🏠 Меню", "menu"),
    )
    return kb


async def ensure_ready(
    force: bool = False, pending_message: Message | None = None
) -> bool:
    if pending_message is not None and not SOURCE.all_groups():
        await pending_message.answer(
            "⏳ Загружаю актуальное расписание с сайта колледжа…"
        )
    try:
        await SOURCE.refresh(force=force)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Ошибка загрузки с сайта: %s", exc)
        return False


def schedule_for_day(group: str, weekday: int) -> str:
    day = (SOURCE.get_days(group) or {}).get(weekday)
    weekday_name = parse.WEEKDAYS_RU[weekday].capitalize()
    if day is None:
        return f"🎉 В {weekday_name.lower()} у группы {group} пар нет."
    return (
        f"📚 Группа {group}\n\n"
        f"{parse.format_day(day)}\n\n"
        "⏰ Начало пар — по общему графику колледжа."
    )


async def send_group_days(
    bot: Bot, target: dict, group: str, page: int
) -> None:
    days = SOURCE.get_days(group) or {}
    if not days:
        await bot.send_message(
            f"⚠️ Для группы {group} не удалось получить расписание.",
            keyboard=kb_groups(SOURCE.all_groups(), page),
            **target,
        )
        return
    await bot.send_message(
        f"📚 Группа {group}\nВыберите день 👇",
        keyboard=kb_days(group, page),
        **target,
    )


async def reply_group(msg: Message, query: str) -> None:
    query = (query or "").strip()
    if not query:
        await msg.answer(
            "Выберите свою группу 👇",
            keyboard=kb_groups(SOURCE.all_groups()),
        )
        return
    if not await ensure_ready(pending_message=msg):
        await msg.answer("⚠️ Не удалось загрузить расписание с сайта.")
        return

    matches = SOURCE.match_groups(query)
    if not matches:
        await msg.answer(
            f"❌ Группа «{query}» не найдена.\nВыберите её из списка:",
            keyboard=kb_groups(SOURCE.all_groups()),
        )
        return
    if len(matches) > 1:
        await msg.answer(
            "Найдено несколько подходящих групп. Выберите нужную:",
            keyboard=kb_group_choices(matches),
        )
        return

    group = next((g for g in matches if SOURCE.get_days(g)), matches[0])
    await msg.answer(
        f"📚 Группа {group}\nВыберите день 👇",
        keyboard=kb_days(group),
    )


def callback_target(cb: Callback) -> dict | None:
    if cb.message is not None:
        return recipient_of(cb.message)
    if cb.user is not None and cb.user.user_id is not None:
        return {"user_id": cb.user.user_id}
    return None


async def send_to_callback(
    cb: Callback, text: str, keyboard: InlineKeyboard | None = None
) -> None:
    target = callback_target(cb)
    if target is None:
        return
    if keyboard is None:
        await BOT.bot.send_message(text, **target)
    else:
        await BOT.bot.send_message(text, keyboard=keyboard, **target)


# --------------------------------------------------------------- handlers ---

BOT = MaxBot(config.MAX_BOT_TOKEN or "dummy-token-until-startup")


@BOT.bot_started()
async def on_bot_started(update: Update, bot: Bot) -> None:
    await bot.send_message(MENU_TEXT, chat_id=update.chat_id, keyboard=kb_main())


@BOT.message(Command("start", "help"))
async def cmd_start(msg: Message) -> None:
    await msg.answer(MENU_TEXT, keyboard=kb_main())


@BOT.message(Command("refresh"))
async def cmd_refresh(msg: Message) -> None:
    await msg.answer("🔄 Обновляю расписание с сайта…")
    ok = await ensure_ready(force=True)
    if ok:
        await msg.answer(
            f"✅ Обновлено. Файлов: {len(SOURCE.files)}, "
            f"групп: {len(SOURCE.all_groups())}.",
            keyboard=kb_main(),
        )
    else:
        await msg.answer("⚠️ Обновить не удалось.", keyboard=kb_main())


@BOT.message(Command("today"))
async def cmd_today(msg: Message) -> None:
    uid = sender_user_id(msg)
    group = SUBS.get(uid) if uid is not None else None
    if not group:
        await msg.answer(
            "Сначала выберите группу, затем нажмите «Сегодня» 👇",
            keyboard=kb_groups(SOURCE.all_groups()),
        )
        return
    weekday = now_local().weekday()
    await msg.answer(
        schedule_for_day(group, weekday),
        keyboard=kb_day_actions(group, group_page(group)),
    )


@BOT.message(Command("groups"))
async def cmd_groups(msg: Message) -> None:
    if not await ensure_ready(pending_message=msg):
        await msg.answer("⚠️ Не удалось загрузить данные с сайта.")
        return
    groups = SOURCE.all_groups()
    await msg.answer(
        f"📋 Найдено групп: {len(groups)}. Выберите свою 👇",
        keyboard=kb_groups(groups),
    )


@BOT.message(Command("schedule"))
async def cmd_schedule(msg: Message) -> None:
    parts = (msg.text or "").split(maxsplit=1)
    await reply_group(msg, parts[1] if len(parts) > 1 else "")


@BOT.message(Command("notify"))
async def cmd_notify(msg: Message) -> None:
    parts = (msg.text or "").split(maxsplit=1)
    query = (parts[1] if len(parts) > 1 else "").strip()
    if not query:
        await msg.answer(
            "Выберите группу — напоминания включатся кнопкой 🔔",
            keyboard=kb_groups(SOURCE.all_groups()),
        )
        return
    if not await ensure_ready(pending_message=msg):
        await msg.answer("⚠️ Не удалось загрузить данные с сайта.")
        return
    matches = SOURCE.match_groups(query)
    if not matches:
        await msg.answer(
            "Группа не найдена. Выберите её из списка:",
            keyboard=kb_groups(SOURCE.all_groups()),
        )
        return
    group = next((g for g in matches if SOURCE.get_days(g)), matches[0])
    uid = sender_user_id(msg)
    if uid is None:
        await msg.answer("Не могу определить ваш user_id 😕")
        return
    SUBS.set(uid, group)
    await msg.answer(
        f"🔔 Напоминания для группы {group} включены.",
        keyboard=kb_days(group, group_page(group)),
    )


@BOT.message(Command("off"))
async def cmd_off(msg: Message) -> None:
    uid = sender_user_id(msg)
    if uid is not None and SUBS.remove(uid):
        await msg.answer("🔕 Напоминания отключены.", keyboard=kb_main())
    else:
        await msg.answer(
            "У вас не было включённых напоминаний.", keyboard=kb_main()
        )


def is_command_text(update: Update) -> bool:
    return (update.text or "").strip().startswith("/")


@BOT.message(is_command_text)
async def cmd_unknown(msg: Message) -> None:
    await msg.answer(
        "Такой команды нет. Используйте кнопки меню 👇",
        keyboard=kb_main(),
    )


@BOT.message()
async def any_text(msg: Message) -> None:
    text = (msg.text or "").strip()
    if not text or text.startswith("/"):
        return
    await reply_group(msg, text)


def is_ui_callback(update: Update) -> bool:
    payload = update.callback.payload if update.callback else ""
    return bool(payload and payload.split(":", 1)[0] in {
        "menu", "help", "refresh", "groups", "gp", "g", "d",
        "n", "off", "sub", "today",
    })


@BOT.callback(is_ui_callback)
async def cb_navigation(cb: Callback) -> None:
    payload = cb.payload or ""
    kind, _, rest = payload.partition(":")
    parts = rest.split(":", 2) if rest else []

    if kind in ("menu", "help"):
        text = MENU_TEXT if kind == "menu" else HELP_TEXT
        await cb.answer("Меню")
        await send_to_callback(cb, text, kb_main())
        return

    if kind == "refresh":
        await cb.answer("Обновляю…")
        await send_to_callback(cb, "🔄 Обновляю расписание с сайта…")
        ok = await ensure_ready(force=True)
        if ok:
            await send_to_callback(
                cb,
                f"✅ Обновлено. Файлов: {len(SOURCE.files)}, "
                f"групп: {len(SOURCE.all_groups())}.",
                kb_main(),
            )
        else:
            await send_to_callback(cb, "⚠️ Обновить не удалось.", kb_main())
        return

    if kind in ("groups", "gp"):
        try:
            page = int(parts[0]) if parts else 0
        except ValueError:
            page = 0
        if not SOURCE.all_groups():
            await send_to_callback(cb, "⏳ Загружаю расписание…")
            if not await ensure_ready():
                await send_to_callback(cb, "⚠️ Не удалось загрузить расписание.")
                return
        groups = SOURCE.all_groups()
        await cb.answer("Группы")
        await send_to_callback(
            cb,
            f"📋 Группы: {len(groups)}. Выберите свою 👇",
            kb_groups(groups, page),
        )
        return

    if kind == "g":
        # Поддерживается и новый payload g:<page>:<group>, и старый g:<group>.
        if len(parts) >= 2:
            try:
                page = int(parts[0])
            except ValueError:
                page = 0
            group = parts[1]
        else:
            page = 0
            group = parts[0] if parts else ""
        if not SOURCE.get_days(group):
            if not SOURCE.all_groups() and await ensure_ready():
                pass
            else:
                await cb.answer("Группа не найдена")
                await send_to_callback(
                    cb,
                    f"⚠️ Для группы {group} расписание не найдено.",
                    kb_groups(SOURCE.all_groups()),
                )
                return
        page = clamp_page(page, SOURCE.all_groups())
        await cb.answer(group)
        await send_group_days(BOT.bot, callback_target(cb) or {}, group, page)
        return

    if kind == "d":
        group = parts[0] if parts else ""
        try:
            weekday = int(parts[1]) if len(parts) > 1 else -1
        except ValueError:
            weekday = -1
        if not 0 <= weekday < len(WEEKDAYS_SHORT):
            await cb.answer("Некорректный день")
            return
        page = group_page(group)
        await cb.answer(parse.WEEKDAYS_RU[weekday].capitalize())
        await send_to_callback(
            cb,
            schedule_for_day(group, weekday),
            kb_day_actions(group, page),
        )
        return

    if kind == "n":
        group = parts[0] if parts else ""
        uid = cb.user.user_id if cb.user is not None else None
        if uid is None:
            await cb.answer("Не удалось определить пользователя")
            return
        if not SOURCE.get_days(group):
            matches = SOURCE.match_groups(group)
            group = matches[0] if matches else group
        SUBS.set(uid, group)
        await cb.answer("Напоминания включены")
        await send_to_callback(
            cb,
            f"🔔 Напоминания для группы {group} включены.",
            kb_days(group, group_page(group)),
        )
        return

    if kind == "off":
        uid = cb.user.user_id if cb.user is not None else None
        removed = uid is not None and SUBS.remove(uid)
        await cb.answer("Отключены" if removed else "Не были включены")
        await send_to_callback(
            cb,
            "🔕 Напоминания отключены." if removed else "Напоминания не были включены.",
            kb_main(),
        )
        return

    if kind == "sub":
        uid = cb.user.user_id if cb.user is not None else None
        group = SUBS.get(uid) if uid is not None else None
        if not group:
            await cb.answer("Выберите группу")
            await send_to_callback(
                cb,
                "Выберите группу и нажмите 🔔 «Напоминания».",
                kb_groups(SOURCE.all_groups()),
            )
            return
        kb = InlineKeyboard()
        kb.row(
            Button.callback("🔕 Отключить", "off"),
            Button.callback("📅 Расписание", f"g:{group_page(group)}:{group}"),
        )
        kb.row(Button.callback("🏠 Меню", "menu"))
        await cb.answer("Уведомления")
        await send_to_callback(
            cb,
            f"🔔 Уведомления включены для группы {group}.",
            kb,
        )
        return

    if kind == "today":
        group = parts[0] if parts else "auto"
        if group == "auto":
            uid = cb.user.user_id if cb.user is not None else None
            group = SUBS.get(uid) if uid is not None else None
        if not group:
            await cb.answer("Выберите группу")
            await send_to_callback(
                cb,
                "Сначала выберите группу, затем нажмите «Сегодня».",
                kb_groups(SOURCE.all_groups()),
            )
            return
        weekday = now_local().weekday()
        await cb.answer("Сегодня")
        await send_to_callback(
            cb,
            schedule_for_day(group, weekday),
            kb_day_actions(group, group_page(group)),
        )
        return


# ------------------------------------------------------------ notifications -

async def send_reminder(
    bot: Bot, user_id: int, group: str, lesson, start: dt.datetime
) -> None:
    minutes_left = max(0, round((start - now_local()).total_seconds() / 60))
    text = (
        f"🔔 Через {minutes_left} мин — пара №{lesson.num} у группы {group}!\n"
        f"📖 {lesson.subject}\n"
        + (f"👤 {lesson.teacher}\n" if lesson.teacher else "")
        + (f"🚪 ауд. {lesson.room}\n" if lesson.room else "")
        + f"🕐 {start:%H:%M}–{lesson.end:%H:%M}"
    )
    await bot.send_message(
        text,
        user_id=user_id,
        keyboard=kb_day_actions(group, group_page(group)),
    )


async def notification_loop(bot: Bot) -> None:
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
            todays = {
                user_id: (SOURCE.get_days(group) or {}).get(now.weekday())
                for user_id, group in subs
            }
            for user_id, group in subs:
                day = todays[user_id]
                if not day:
                    continue
                for lesson in day.lessons:
                    if lesson.start is None:
                        continue
                    start = dt.datetime.combine(
                        now.date(), lesson.start.time()
                    )
                    mark = (user_id, now.date().isoformat(), lesson.num)
                    if mark in _sent:
                        continue
                    if now <= start <= cutoff:
                        try:
                            await send_reminder(
                                bot, user_id, group, lesson, start
                            )
                            _sent.add(mark)
                            log.info(
                                "Уведомление %s: пара %d в %s",
                                user_id, lesson.num, start,
                            )
                        except Exception:  # noqa: BLE001
                            log.exception("Не удалось отправить уведомление")
            today = now.date().isoformat()
            _sent.intersection_update(
                {mark for mark in _sent if mark[1] == today}
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("Ошибка в цикле уведомлений")


# ------------------------------------------------------------------- main ---

async def preload_source() -> None:
    started = time.monotonic()
    log.info("Предварительная загрузка расписания…")
    if await ensure_ready():
        log.info(
            "Расписание готово: файлов=%d, групп=%d, за %.1f с",
            len(SOURCE.files), len(SOURCE.all_groups()),
            time.monotonic() - started,
        )


async def async_main() -> None:
    preloader = asyncio.create_task(preload_source())
    polling = asyncio.create_task(BOT.start_polling(timeout=30, limit=100))
    notifier = asyncio.create_task(notification_loop(BOT.bot))
    try:
        await polling
    finally:
        for task in (preloader, notifier):
            task.cancel()
        await asyncio.gather(preloader, notifier, return_exceptions=True)


def main() -> None:
    if not config.MAX_BOT_TOKEN:
        raise SystemExit(
            "Не задан токен бота. Установите MAX_BOT_TOKEN или "
            "MAX_BOT_TOKEN_FILE."
        )
    log.info(
        "Старт бота. Сайт: %s%s",
        config.BASE_URL,
        config.TIMETABLE_PAGES[0] if config.TIMETABLE_PAGES else "",
    )
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
