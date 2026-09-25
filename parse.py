"""Разбор PDF-файлов расписания колледжа ЛПК (Лангепас) в текстовый вид.

Структура PDF на сайте collegelan.ru:
  - каждая страница = один день недели ("День - Понедельник, 28.09.2026");
  - таблица: колонка № пары, затем на каждую группу пара колонок
    "Предмет, вид занятия, преподаватель" + "Ауд.";
  - в шапке перечислены номера групп (например: 23-29, 24-20, 24-21(2С)...).

Модуль извлекает занятия конкретной группы по дням недели.
Время начала пар берётся из стандартного графика колледжа (конфиг
LESSON_TIMES): сами PDF-файлы времени не содержат.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

import pdfplumber

# --------------------------------------------------------------------------- #
#  График начала/окончания пар (время местное). Правится без переписывания кода.
#  Формат: номер пары -> (начало, конец)  "HH:MM".
LESSON_TIMES: dict[int, tuple[str, str]] = {
    1: ("09:00", "09:45"),
    2: ("09:55", "10:40"),
    3: ("10:50", "11:35"),
    4: ("11:50", "12:35"),
    5: ("12:45", "13:30"),
    6: ("13:40", "14:25"),
    7: ("14:30", "15:15"),
    8: ("15:20", "16:05"),
    9: ("16:10", "16:55"),
}

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница",
               "суббота", "воскресенье"]

# «Понедельник» и его обрывки из-за переносов строк в PDF
_DAY_ALIASES: dict[str, int] = {}
for _i, _d in enumerate(WEEKDAYS_RU[:6]):
    _cap = _d.capitalize()
    for n in range(3, len(_cap) + 1):
        _DAY_ALIASES[_cap[:n].lower()] = _i


@dataclass
class Lesson:
    num: int                   # номер пары
    subject: str               # предмет / вид занятия
    teacher: str               # преподаватель
    room: str                  # аудитория
    start: dt.datetime | None = None   # время начала (из LESSON_TIMES)
    end: dt.datetime | None = None

    def time_str(self) -> str:
        if self.start and self.end:
            return f"{self.start:%H:%M}–{self.end:%H:%M}"
        return ""


@dataclass
class DaySchedule:
    weekday: int               # 0=понедельник ... 5=суббота
    date_label: str            # например «28.09.2026» с страницы PDF
    lessons: list[Lesson] = field(default_factory=list)


# --------------------------------------------------------------------------- #

def _norm(s: str | None) -> str:
    return re.sub(r"\s+", "", (s or "").replace("\n", "")).lower()


def extract_groups_from_header(header_cells: list[str]) -> dict[int, str]:
    """Возвращает {индекс колонки: название группы} из строки шапки таблицы."""
    groups: dict[int, str] = {}
    for idx, cell in enumerate(header_cells):
        c = (cell or "").strip()
        if not c or c == "№" or "предмет" in c.lower():
            continue
        # группа: начинается с цифры, без пробелов внутри, короткая
        compact = re.sub(r"\s+", "", c)
        if re.fullmatch(r"\d{1,2}-\d{1,2}([а-яa-zА-Я]\w*)?(\([0-9А-Яа-я]+\))?",
                        compact):
            groups[idx] = compact
    return groups


_TEACHER_RE = re.compile(
    r"[А-Я][а-яё]{1,25}\s+[А-Я]\.\s?[А-Я]?\.{0,2}\s?[А-Я]?\."
)


def parse_cell(text: str) -> tuple[str, str]:
    """Ячейка -> (предмет+вид, преподаватели).

    Обычный вид: «МДК.03.01 ОПНиГ\\n(лекция)\\nКрылова В. И.».
    Подгрупповой вид: «1.УстрЭкспСосудов (лаб)\\nНуриева С. Р.\\n
    2.МДК.03.01 ОПНиГ (лаб)\\nКрылова В. И.» — возвращаем обе подпары через «|».
    """
    raw_lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not raw_lines:
        return "", ""

    # Склейка перенесённых строк: строка без пробела на конце в PDF обычно
    # является продолжением предыдущей («Физ-ра» + «Спорт зал»).
    merged: list[str] = []
    for ln in raw_lines:
        prev_is_teacher = bool(merged and _TEACHER_RE.fullmatch(merged[-1]))
        starts_new = (
            re.match(r"^\d\s*[.:]\s*\D", ln)      # префикс подгруппы «1.»/«2.»
            or ln.startswith("(")                  # вид занятия «(лекция)»
            or _TEACHER_RE.fullmatch(ln)           # строка преподавателя
            or prev_is_teacher                     # всё после преподавателя — новый блок
        )
        if merged and not starts_new:
            merged[-1] += " " + ln
        else:
            merged.append(ln)

    # Разбивка на блоки по префиксам «1.», «2.» (подгрупповые занятия)
    blocks: list[list[str]] = []
    for ln in merged:
        if re.match(r"^\d\s*[.:]\s*\D", ln) and blocks:
            blocks.append([ln])
        elif blocks and _TEACHER_RE.fullmatch(ln):
            blocks[-1].append(ln)          # преподаватель относится к блоку
        else:
            blocks.append([ln])

    subjects: list[str] = []
    teachers: list[str] = []
    for b in blocks:
        body = list(b)
        if body and _TEACHER_RE.fullmatch(body[-1]):
            teachers.append(re.sub(r"\s+", " ", body[-1]).strip())
            body = body[:-1]
        subj = re.sub(r"\s+", " ", " ".join(body)).strip()
        if subj:
            subjects.append(subj)
    prefix = ""
    if any(re.match(r"^\d\s*[.:]", b[0]) for b in blocks if b):
        prefix = "(подгр. 1 / подгр. 2) "
    return prefix + " | ".join(subjects), "; ".join(teachers)


def parse_pdf(path, group_query: str) -> list[DaySchedule]:
    """Разбирает PDF и возвращает расписание дней для запрошенной группы."""
    gq = _norm(group_query)
    days: list[DaySchedule] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            head_line = text.splitlines()[0] if text else ""
            day = None
            for pref, wd in _DAY_ALIASES.items():
                if pref in head_line.lower():
                    day = wd
                    break
            date_m = re.search(r"\d{2}\.\d{2}\.\d{4}", head_line)
            tables = page.extract_tables()
            if day is None or not tables:
                continue
            ds = DaySchedule(weekday=day, date_label=date_m.group(0) if date_m else "")
            table = max(tables, key=len)
            groups = extract_groups_from_header(table[0])
            target_cols = [ci for ci, gname in groups.items()
                           if gq == _norm(gname) or gq in _norm(gname)]
            if not target_cols:
                continue
            col = target_cols[0]
            for row in table[2:]:
                if col >= len(row):
                    continue
                try:
                    num = int((row[0] or "").strip())
                except ValueError:
                    continue
                subject, teacher = parse_cell(row[col])
                if not subject:
                    continue
                room = (row[col + 1] if col + 1 < len(row) else "") or ""
                room = re.sub(r"\s*\n\s*", " ", room).strip()
                lesson = Lesson(num=num, subject=subject, teacher=teacher,
                                room=room)
                _fill_time(lesson, ds.weekday)
                ds.lessons.append(lesson)
            if ds.lessons:
                days.append(ds)
    return days


def _fill_time(lesson: Lesson, weekday: int) -> None:
    times = LESSON_TIMES.get(lesson.num)
    if not times:
        return
    today = dt.date.today()
    delta = (weekday - today.weekday()) % 7
    d = today + dt.timedelta(days=delta)
    h1, m1 = map(int, times[0].split(":"))
    h2, m2 = map(int, times[1].split(":"))
    lesson.start = dt.datetime.combine(d, dt.time(h1, m1))
    lesson.end = dt.datetime.combine(d, dt.time(h2, m2))


def next_weekday_date(weekday: int) -> dt.date:
    today = dt.date.today()
    return today + dt.timedelta(days=(weekday - today.weekday()) % 7)


def format_day(day: DaySchedule, *, show_dates: bool = True) -> str:
    wd = WEEKDAYS_RU[day.weekday]
    if show_dates:
        d = next_weekday_date(day.weekday)
        title = f"{wd.capitalize()} {d:%d.%m}"
    else:
        title = wd.capitalize()
    lines = [f"📅 {title}"]
    for ls in day.lessons:
        parts = [f"  {ls.num}. {ls.subject}"]
        if ls.time_str():
            parts.append(f"     🕐 {ls.time_str()}")
        tail = []
        if ls.room:
            tail.append(f"ауд. {ls.room}")
        if ls.teacher:
            tail.append(ls.teacher)
        if tail:
            parts.append("     " + " · ".join(tail))
        lines.extend(parts)
    return "\n".join(lines)


def format_schedule(days: list[DaySchedule], group: str) -> str:
    if not days:
        return ""
    out = [f"📚 Расписание группы {group} (актуальная неделя)\n"]
    for d in sorted(days, key=lambda x: x.weekday):
        out.append(format_day(d))
        out.append("")
    out.append("⏰ Начало пар — по общему графику колледжа.")
    return "\n".join(out).strip()


def find_group_in_pdf(path) -> list[str]:
    """Список всех групп, встречающихся в шапках PDF (для /groups)."""
    found: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for g in extract_groups_from_header(table[0]).values():
                    if g not in found:
                        found.append(g)
    return found
