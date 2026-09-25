"""Настройки бота расписания ЛПК (Лангепас).

Все значения можно переопределить через переменные окружения —
см. файл .env.example
"""

import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# Токен бота MAX (получить у @MasterBot в мессенджере MAX)
MAX_BOT_TOKEN = _env("MAX_BOT_TOKEN")

# --- Источник расписания -------------------------------------------------
# Базовый URL сайта колледжа, где лежат файлы с расписанием.
# По умолчанию — ориентировочный адрес; замените на актуальный!
BASE_URL = _env("TIMETABLE_BASE_URL", "https://lppk.langeppas.ru").rstrip("/")

# Путь к странице(ам) с расписанием относительно BASE_URL
# (если страница находится по другому адресу, например /svedeniya-ob-obrazovanii/struktura/...).
TIMETABLE_PAGES = [
    p.strip()
    for p in _env("TIMETABLE_PAGES", "/raspisanie").split(",")
    if p.strip()
]

# Расширения файлов, которые считаем файлами расписания
FILE_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".rtf", ".odt", ".ods", ".png", ".jpg", ".jpeg",
)

# Ключевые слова для фильтрации ссылок на странице (если нужно сузить поиск).
# Пусто — берём все ссылки с подходящим расширением.
LINK_KEYWORDS = tuple(
    k.strip().lower()
    for k in _env("TIMETABLE_LINK_KEYWORDS", "расписан").split(",")
    if k.strip()
)

# --- Поведение кэша -------------------------------------------------------
# Как часто (в минутах) перескачивать список файлов с сайта
CACHE_TTL_MINUTES = int(_env("CACHE_TTL_MINUTES", "60") or 60)

# Каталог для скачанных файлов
DOWNLOAD_DIR = _env("DOWNLOAD_DIR", "cache_files")

# Запросы к сайту (секунды)
HTTP_TIMEOUT = int(_env("HTTP_TIMEOUT", "20") or 20)

# Пользовательский User-Agent для запросов к сайту
USER_AGENT = _env(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)
