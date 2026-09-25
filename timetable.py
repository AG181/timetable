"""Источник расписания: сайт collegelan.ru + разбор PDF в текст.

Модуль:
  1. качает страницу «Расписание занятий» и собирает ссылки на PDF-файлы;
  2. скачивает каждый PDF (с кэшированием на диске);
  3. разбирает PDF модулем parse и строит индекс: группа -> расписание по дням;
  4. выдаёт готовый текст расписания по запросу студента.

PDF-файлы на сайте разложены по файлам групп, например «23-29-24-25.pdf»
содержит расписание групп 23-29, 24-20, 24-21(2С), 24-23(П), 24-25.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import config
import parse


@dataclass
class TimetableFile:
    """Один PDF с расписанием, найденный на сайте."""

    title: str          # имя файла без расширения
    url: str            # абсолютный URL файла
    path: Path | None = field(default=None)   # локальный путь после скачивания
    groups: list[str] = field(default_factory=list)  # группы внутри файла

    @property
    def key(self) -> str:
        """Устойчивый ключ файла (по URL)."""
        return hashlib.md5(self.url.encode("utf-8")).hexdigest()[:12]

    @property
    def filename(self) -> str:
        """Имя файла на диске."""
        base = unquote(Path(urlparse(self.url).path).name) or "raspisanie.pdf"
        return re.sub(r"[\\/:*?\"<>| ]+", "_", base)


def _is_schedule_pdf(href: str, title: str) -> bool:
    """Отсекаем служебные файлы («Кураторы групп...», «График консультаций...»)."""
    hay = (href + " " + title).lower()
    for junk in ("куратор", "консультаци", "график"):
        if junk in hay:
            return False
    return True


class TimetableSource:
    """Клиент сайта + кэш разобранных расписаний по группам."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": config.USER_AGENT})
        self._files: list[TimetableFile] = []
        # canonical group name -> {weekday: DaySchedule}
        self._index: dict[str, dict[int, parse.DaySchedule]] = {}
        self._loaded_at: float = 0.0
        self._lock = asyncio.Lock()
        Path(config.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

    # ---------- сеть ------------------------------------------------------
    def _fetch_page(self, page_path: str) -> tuple[str, str]:
        url = urljoin(config.BASE_URL + "/", page_path.lstrip("/"))
        resp = self._session.get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.text, url

    def _download(self, file: TimetableFile) -> Path:
        dest = Path(config.DOWNLOAD_DIR) / f"{file.key}_{file.filename}"
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        last_err: Exception | None = None
        for attempt in range(3):  # сайт иногда отдаёт пустой ответ — повторяем
            try:
                resp = self._session.get(
                    file.url, timeout=config.HTTP_TIMEOUT * 3
                )
                resp.raise_for_status()
                if not resp.content.startswith(b"%PDF"):
                    raise ValueError(f"файл не является PDF: {file.url}")
                tmp = dest.with_suffix(".part")
                tmp.write_bytes(resp.content)
                tmp.replace(dest)
                return dest
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(1 + attempt)
        raise RuntimeError(f"не скачался {file.url}: {last_err}")

    @staticmethod
    def _parse_page(html: str, page_url: str) -> list[TimetableFile]:
        soup = BeautifulSoup(html, "html.parser")
        found: dict[str, TimetableFile] = {}
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            abs_url = urljoin(page_url, href)
            lowered = abs_url.lower().split("?")[0]
            if not lowered.endswith(config.FILE_EXTENSIONS):
                continue
            title = (a.get_text(strip=True)
                     or a.get("title", "")
                     or unquote(Path(urlparse(abs_url).path).stem))
            if config.LINK_KEYWORDS and not any(
                k in (title + " " + abs_url).lower()
                for k in config.LINK_KEYWORDS
            ):
                continue
            if not _is_schedule_pdf(abs_url, title):
                continue
            found[abs_url] = TimetableFile(title=title, url=abs_url)
        return list(found.values())

    # ---------- индекс групп ----------------------------------------------
    @staticmethod
    def _canon(group: str) -> str:
        return re.sub(r"\s+", "", group).upper()

    def _build_index(self) -> None:
        """Разбирает все скачанные PDF и собирает карту группа -> дни."""
        index: dict[str, dict[int, parse.DaySchedule]] = {}
        for f in self._files:
            if not f.path or not Path(f.path).exists():
                continue
            try:
                groups = parse.find_group_in_pdf(f.path)
            except Exception:  # noqa: BLE001 — битый/не тот pdf
                continue
            f.groups = groups
            for g in groups:
                days = parse.parse_pdf(f.path, g)
                if days:
                    index[self._canon(g)] = {d.weekday: d for d in days}
        self._index = index

    # ---------- публичное API ---------------------------------------------
    async def refresh(self, force: bool = False) -> list[TimetableFile]:
        """Обновляет список файлов с сайта и пересобирает индекс групп."""
        async with self._lock:
            fresh = time.time() - self._loaded_at < config.CACHE_TTL_MINUTES * 60
            if fresh and self._index and not force:
                return self._files

            files: dict[str, TimetableFile] = {}
            errors: list[str] = []
            for page in config.TIMETABLE_PAGES:
                page_url = urljoin(config.BASE_URL + "/", page.lstrip("/"))
                try:
                    html, _ = await asyncio.to_thread(self._fetch_page, page)
                    for f in self._parse_page(html, page_url):
                        files[f.url] = f
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{page_url}: {exc}")

            new_files = sorted(files.values(), key=lambda f: f.title.lower())
            changed = {f.url for f in new_files} != {f.url for f in self._files}
            need_reindex = force or changed or not self._index

            for f in new_files:
                old = next((o for o in self._files if o.url == f.url), None)
                if old and old.path and Path(old.path).exists():
                    f.path = old.path
                else:
                    try:
                        f.path = await asyncio.to_thread(self._download, f)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{f.url}: {exc}")

            if new_files:
                self._files = new_files
                self._loaded_at = time.time()
            if need_reindex and self._files:
                await asyncio.to_thread(self._build_index)
            if not self._files and errors:
                raise RuntimeError("; ".join(errors))
            return self._files

    @property
    def files(self) -> list[TimetableFile]:
        return list(self._files)

    def all_groups(self) -> list[str]:
        return sorted(self._index.keys())

    def get_days(self, group: str) -> dict[int, parse.DaySchedule] | None:
        """Дни недели для точного названия группы."""
        return self._index.get(self._canon(group))

    def match_groups(self, query: str) -> list[str]:
        """Подходящие под запрос канонические названия групп.

        «2329», «23-29», «23_29», «23 29» -> 23-29; «24-21» -> 24-21(2С).
        """
        q = re.sub(r"[\s_\-/.]+", "", query.strip()).upper()
        if not q:
            return []
        exact, partial = [], []
        for g in self.all_groups():
            gc = re.sub(r"[\s_\-/.()А-Я]", "", g)  # «24-21(2С)» -> «2421»
            if gc == q or g == self._canon(query):
                exact.append(g)
            elif q in gc or gc.startswith(q):
                partial.append(g)
        return exact or partial

    def schedule_text(self, group: str) -> str:
        days = self.get_days(group)
        if not days:
            return ""
        ordered = [days[w] for w in sorted(days)]
        return parse.format_schedule(ordered, group)

    def upcoming_lessons(
        self, group: str, now, limit: int = 3
    ) -> list[tuple[parse.Lesson, object]]:
        """Ближайшие занятия группы (с реальными датами) от момента `now`.

        Возвращает [(Lesson, datetime начала)], Lesson.start уже проставлен
        парсером на ближайшие даты относительно сегодня.
        """
        days = self.get_days(group)
        if not days:
            return []
        result = []
        for w in sorted(days):
            for ls in days[w].lessons:
                if ls.start is None:
                    continue
                start = ls.start
                # парсер ставит дату «ближайший этот день недели»;
                # если пара уже прошла сегодня — берём следующую неделю
                while start <= now:
                    start = start + dt.timedelta(days=7)
                result.append((ls, start))
        result.sort(key=lambda t: t[1])
        return result[:limit]
