"""Загрузка и парсинг расписания с сайта колледжа ЛПК (Лангепас).

Модуль ходит на страницу(ы) расписания, собирает ссылки на файлы
(pdf/doc/xls и т.п.), кэширует список и сами файлы на диске,
чтобы не качать их повторно при каждом запросе студента.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup

import config


@dataclass
class TimetableFile:
    """Один файл расписания, найденный на сайте."""

    title: str          # текст ссылки / название группы или файла
    url: str            # абсолютный URL файла
    path: Path = field(default=None)  # локальный путь (после скачивания)

    @property
    def key(self) -> str:
        """Устойчивый ключ файла (по URL)."""
        return hashlib.md5(self.url.encode("utf-8")).hexdigest()[:12]

    @property
    def filename(self) -> str:
        """Имя файла для отправки в мессенджер."""
        base = unquote(Path(urlparse(self.url).path).name) or "raspisanie"
        # очищаем «неудобные» символы
        return re.sub(r"[\\/:*?\"<>|]+", "_", base)


def _norm_title(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def _guess_group(title: str) -> str | None:
    """Пытаемся вычленить название группы из заголовка/имени файла.

    Типичные форматы: «Расписание гр. ИС-21», «3-ПД», «Группа 401» и т.п.
    """
    m = re.search(
        r"(?:гр(?:уппа|\.)?\s*[-:]?\s*)([А-Яа-яA-Za-z0-9\-_/]{2,15})",
        title,
        re.IGNORECASE,
    )
    if m and not m.group(1).lower().startswith(("уппа", "рупп")):
        return m.group(1)
    # самостоятельные обозначения вида ИС-21, 3-ПД, ПД-21-1, 401 (только с дефисом/буквой)
    m = re.search(r"\b([А-Я]{1,4}[-_]\d{1,2}(?:[-_]\d)?|\d[-_][А-Я]{1,4})\b", title)
    if m:
        return m.group(1)
    # просто номер группы цифрами: «401 группа» / «группа 401»
    m = re.search(r"\b(\d{3,4})\b(?:\s*(?:группа|гр\.?))?|\b(?:группа|гр\.?)\s*(\d{3,4})\b", title)
    if m:
        return m.group(1) or m.group(2)
    return None


TRANSLIT = str.maketrans({
    "a": "а", "b": "б", "c": "ц", "d": "д", "e": "е", "f": "ф", "g": "г",
    "h": "х", "i": "и", "j": "й", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "r": "р", "s": "с", "t": "т", "u": "у", "v": "в",
    "y": "ы", "z": "з",
})


def _norm_query(q: str) -> str:
    """Нормализует запрос: нижний регистр, дефисы вместо подчёркиваний/пробелов."""
    return re.sub(r"[_\s]+", "-", q.strip().lower())


class TimetableSource:
    """Клиент сайта с расписанием + локальный кэш."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": config.USER_AGENT})
        self._files: list[TimetableFile] = []
        self._loaded_at: float = 0.0
        self._lock = asyncio.Lock()
        Path(config.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

    # ---------- низкоуровневые сетевые операции (в отдельном потоке) ----
    def _fetch_page(self, page_path: str) -> str:
        url = urljoin(config.BASE_URL + "/", page_path.lstrip("/"))
        resp = self._session.get(url, timeout=config.HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.text

    def _download(self, file: TimetableFile) -> Path:
        dest = Path(config.DOWNLOAD_DIR) / f"{file.key}_{file.filename}"
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        resp = self._session.get(file.url, timeout=config.HTTP_TIMEOUT * 3)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        return dest

    # ---------- сбор ссылок ---------------------------------------------
    def _parse_page(self, html: str, page_url: str) -> list[TimetableFile]:
        soup = BeautifulSoup(html, "html.parser")
        found: dict[str, TimetableFile] = {}
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            abs_url = urljoin(page_url, href)
            lowered = abs_url.lower().split("?")[0]
            if not lowered.endswith(config.FILE_EXTENSIONS):
                continue
            title = _norm_title(a.get_text()) or _norm_title(a.get("title", ""))
            if not title:
                title = unquote(Path(urlparse(abs_url).path).name)
            # фильтр по ключевым словам (если задан и ссылка ведёт на HTML-страницу
            # с расписанием внутри — расширения уже отсекут лишнее)
            if config.LINK_KEYWORDS:
                haystack = (title + " " + abs_url).lower()
                # если в имени файла явно есть расширение расписания — пропускаем
                if not any(k in haystack for k in config.LINK_KEYWORDS):
                    continue
            found[abs_url] = TimetableFile(title=title, url=abs_url)
        return list(found.values())

    async def refresh(self, force: bool = False) -> list[TimetableFile]:
        """Обновляет список файлов с сайта (с учётом TTL-кэша)."""
        async with self._lock:
            fresh = (
                time.time() - self._loaded_at < config.CACHE_TTL_MINUTES * 60
            )
            if fresh and self._files and not force:
                return self._files

            files: dict[str, TimetableFile] = {}
            errors: list[str] = []
            for page in config.TIMETABLE_PAGES:
                page_url = urljoin(config.BASE_URL + "/", page.lstrip("/"))
                try:
                    html = await asyncio.to_thread(self._fetch_page, page)
                    for f in self._parse_page(html, page_url):
                        files[f.url] = f
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{page_url}: {exc}")
            if files:
                self._files = sorted(files.values(), key=lambda f: f.title.lower())
                self._loaded_at = time.time()
            elif errors and not self._files:
                raise RuntimeError("; ".join(errors))
            return self._files

    # ---------- публичные методы ----------------------------------------
    @staticmethod
    def group_of(file: TimetableFile) -> str | None:
        return _guess_group(file.title)

    def find_by_query(self, query: str) -> list[TimetableFile]:
        """Ищет файлы по названию группы или тексту запроса.

        Учитывает варианты написания: ИС-21 / ис21 / is-21 (транслитерация),
        дефис/подчёркивание/пробел как разделители.
        """
        q = _norm_query(query)
        if not q:
            return []
        q_tr = q.translate(TRANSLIT)  # «is-21» -> «ис-21»
        variants = {q, q_tr}
        result = []
        for f in self._files:
            candidates = {
                _norm_query(f.title),
                _norm_query(Path(f.filename).stem),
            }
            if any(v in c for v in variants for c in candidates):
                result.append(f)
        return result

    async def get_file(self, file: TimetableFile) -> Path:
        """Скачивает файл (или берёт из кэша) и возвращает локальный путь."""
        if file.path and Path(file.path).exists():
            return Path(file.path)
        path = await asyncio.to_thread(self._download, file)
        file.path = path
        return path

    @property
    def files(self) -> list[TimetableFile]:
        return list(self._files)

    @property
    def loaded_at(self) -> float:
        return self._loaded_at
