"""Бот мессенджера MAX: расписание пар колледжа ЛПК (Лангепас).

Файлы с расписанием берутся с сайта колледжа; бот скачивает их и
отправляет студенту в чат.

Команды:
  /start, /help      — приветствие и справка
  /groups            — список доступных файлов/групп (кнопками)
  /schedule <группа> — прислать расписание группы
                       (или просто написать название группы текстом)
  /today             — ссылка на страницу расписания на сайте
  /refresh           — принудительно обновить кэш с сайта

Запуск:
  MAX_BOT_TOKEN=<токен> python bot.py
"""

from __future__ import annotations

import logging

from maxio import Bot, Button, InlineKeyboard, MaxBot, media
from maxio.filters import Command
from maxio.types import Callback, Message

import config
from timetable import TimetableFile, TimetableSource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("lppk-bot")

SOURCE = TimetableSource()

HELP_TEXT = (
    "📚 Бот расписания ЛПК, г. Лангепас\n"
    "\n"
    "Просто напиши название группы — пришлю файл с расписанием.\n"
    "\n"
    "Команды:\n"
    "/groups — список групп и файлов\n"
    "/schedule <группа> — расписание группы\n"
    "/today — ссылка на расписание на сайте\n"
    "/refresh — обновить кэш с сайта\n"
    "/help — эта справка"
)


# ---------------------------------------------------------------- helpers ---

def payload_for(file: TimetableFile) -> str:
    """Короткий уникальный payload кнопки для файла."""
    return f"tt:{file.key}"


def find_by_payload(payload: str) -> TimetableFile | None:
    key = (payload or "").split(":", 1)[-1]
    for f in SOURCE.files:
        if f.key == key:
            return f
    return None


def kb_groups(files: list[TimetableFile]) -> InlineKeyboard:
    """Инлайн-клавиатура со списком файлов (по две кнопки в ряду)."""
    kb = InlineKeyboard()
    buttons = [
        Button.callback(
            f.title if len(f.title) <= 32 else f.title[:29] + "...",
            payload_for(f),
        )
        for f in files[:40]  # ограничение на размер клавиатуры
    ]
    for i in range(0, len(buttons), 2):
        kb.row(*buttons[i:i + 2])
    return kb


def recipient_of(msg: Message) -> dict:
    """Чат или пользователь, куда отвечать на сообщение."""
    if msg.chat_id is not None:
        return {"chat_id": msg.chat_id}
    uid = msg.recipient.user_id or (msg.sender.user_id if msg.sender else None)
    return {"user_id": uid}


async def send_timetable_file(bot: Bot, to: dict, file: TimetableFile) -> None:
    """Скачивает файл с сайта (или берёт из кэша) и отправляет в чат."""
    try:
        path = await SOURCE.get_file(file)
        token = await bot.upload(path, "file", filename=file.filename)
        await bot.send_message(
            f"📄 {file.title}\nИсточник: {file.url}",
            attachments=[media.file(token)],
            **to,
        )
    except Exception:  # noqa: BLE001
        log.exception("Не удалось отправить файл %s", file.url)
        await bot.send_message(
            f"⚠️ Не получилось прикрепить файл «{file.title}».\n"
            f"Откройте его напрямую: {file.url}",
            **to,
        )


async def ensure_files(msg: Message, force: bool = False) -> list[TimetableFile] | None:
    try:
        return await SOURCE.refresh(force=force)
    except Exception as exc:  # noqa: BLE001
        log.error("Ошибка загрузки с сайта: %s", exc)
        await msg.answer(
            "⚠️ Не удалось загрузить расписание с сайта колледжа. "
            "Попробуйте позже или откройте сайт вручную."
        )
        return None


async def handle_group_query(msg: Message, bot: Bot, query: str) -> None:
    files = await ensure_files(msg)
    if files is None:
        return
    query = (query or "").strip()
    if not query:
        await msg.answer("Напишите название группы, например: /schedule ИС-21")
        return
    found = SOURCE.find_by_query(query)
    if not found:
        sample = "; ".join(f.title for f in files[:15])
        await msg.answer(
            f"❌ По запросу «{query}» ничего не найдено.\n"
            f"Доступно: {sample or 'нет файлов'}…"
        )
        return
    if len(found) == 1:
        await send_timetable_file(bot, recipient_of(msg), found[0])
    else:
        await msg.answer(
            f"По запросу «{query}» найдено несколько файлов. Выберите:",
            keyboard=kb_groups(found),
        )


# --------------------------------------------------------------- handlers ---

BOT = MaxBot(config.MAX_BOT_TOKEN or "dummy-token-until-startup")


@BOT.message(Command("start", "help"))
async def cmd_start(msg: Message) -> None:
    await msg.answer(HELP_TEXT)


@BOT.message(Command("refresh"))
async def cmd_refresh(msg: Message) -> None:
    files = await ensure_files(msg, force=True)
    if files is not None:
        await msg.answer(f"✅ Кэш обновлён, найдено файлов: {len(files)}.")


@BOT.message(Command("today"))
async def cmd_today(msg: Message) -> None:
    page = config.TIMETABLE_PAGES[0] if config.TIMETABLE_PAGES else ""
    url = config.BASE_URL + page
    await msg.answer(f"🗓 Актуальное расписание на сайте: {url}")


@BOT.message(Command("groups"))
async def cmd_groups(msg: Message) -> None:
    files = await ensure_files(msg)
    if files is None:
        return
    if not files:
        await msg.answer("На сайте не найдено ни одного файла с расписанием.")
        return
    await msg.answer("Выберите группу / файл расписания 👇", keyboard=kb_groups(files))


@BOT.message(Command("schedule"))
async def cmd_schedule(msg: Message, bot: Bot) -> None:
    parts = (msg.text or "").split(maxsplit=1)
    await handle_group_query(msg, bot, parts[1] if len(parts) > 1 else "")


@BOT.message()
async def any_text(msg: Message, bot: Bot) -> None:
    """Любое текстовое сообщение (не команда) считаем запросом названия группы."""
    text = (msg.text or "").strip()
    if not text or text.startswith("/"):
        return
    await handle_group_query(msg, bot, text)


# ------------------------------------------------------ callback buttons ----

def is_timetable_callback(update) -> bool:
    p = update.callback.payload if update.callback else None
    return bool(p and p.startswith("tt:"))


@BOT.callback(is_timetable_callback)
async def cb_timetable(cb: Callback, bot: Bot) -> None:
    file = find_by_payload(cb.payload or "")
    if file is None:
        await cb.answer("Файл недоступен, запросите /groups заново.")
        return
    await cb.answer("Отправляю расписание…")
    to: dict | None = None
    if cb.message is not None:
        to = recipient_of(cb.message)
    elif cb.user is not None:
        to = {"user_id": cb.user.user_id}
    if to is None or to.get("chat_id") is None and to.get("user_id") is None:
        await cb.answer("Не знаю, куда отправить файл 😕")
        return
    await send_timetable_file(bot, to, file)


# ------------------------------------------------------------------- main ---

def main() -> None:
    if not config.MAX_BOT_TOKEN:
        raise SystemExit(
            "Не задан токен бота. Установите переменную окружения MAX_BOT_TOKEN "
            "(получить токен можно у @MasterBot в мессенджере MAX)."
        )
    BOT.bot.token = config.MAX_BOT_TOKEN
    log.info(
        "Старт бота. Сайт: %s, страницы: %s",
        config.BASE_URL, config.TIMETABLE_PAGES,
    )
    BOT.run()


if __name__ == "__main__":
    main()
