"""Настройки бота расписания ЛПК (Лангепас) для мессенджера MAX.

Все значения можно переопределить через переменные окружения.
"""

import os


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# Токен бота MAX (получить у @MasterBot в мессенджере MAX)
MAX_BOT_TOKEN = _env("MAX_BOT_TOKEN")

# Часовой пояс колледжа (для уведомлений за 5 минут до пары)
TIMEZONE = _env("TIMEZONE", "Asia/Yekaterinburg")

# --- Источник расписания -------------------------------------------------
# Страница сайта колледжа, где лежат PDF-файлы с расписанием по группам.
BASE_URL = _env("TIMETABLE_BASE_URL", "https://collegelan.ru").rstrip("/")

TIMETABLE_PAGES = [
    p.strip()
    for p in _env(
        "TIMETABLE_PAGES", "/studentam/raspisanie-zanyatiy.php"
    ).split(",")
    if p.strip()
]

# Расширения файлов, которые считаем файлами расписания
FILE_EXTENSIONS = (".pdf",)

# Ключевые слова для фильтрации ссылок (пусто — берём все pdf со страницы)
LINK_KEYWORDS = tuple(
    k.strip().lower()
    for k in _env("TIMETABLE_LINK_KEYWORDS", "").split(",")
    if k.strip()
)

# --- Поведение кэша -------------------------------------------------------
# Как часто (в минутах) перескачивать список файлов и разбирать PDF
CACHE_TTL_MINUTES = int(_env("CACHE_TTL_MINUTES", "60") or 60)

# Каталог для скачанных файлов
DOWNLOAD_DIR = _env("DOWNLOAD_DIR", "cache_files")

# Каталог сохранённых подписок на уведомления
SUBSCRIPTIONS_FILE = _env("SUBSCRIPTIONS_FILE", "subscriptions.json")

# Запросы к сайту (секунды)
HTTP_TIMEOUT = int(_env("HTTP_TIMEOUT", "20") or 20)

USER_AGENT = _env(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)

# --- Уведомления ------------------------------------------------------------
# За сколько минут до начала пары предупреждать студента
NOTIFY_BEFORE_MINUTES = int(_env("NOTIFY_BEFORE_MINUTES", "5") or 5)

# Интервал проверки наступления пар (секунды)
NOTIFY_CHECK_INTERVAL_SECONDS = int(
    _env("NOTIFY_CHECK_INTERVAL_SECONDS", "30") or 30
)
