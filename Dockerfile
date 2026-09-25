FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Yekaterinburg

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser

COPY --chown=appuser:appuser bot.py config.py parse.py timetable.py ./
RUN mkdir -p /app/data/cache_files \
    && printf '{}\n' > /app/data/subscriptions.json \
    && chown -R appuser:appuser /app/data

USER appuser

CMD ["python", "bot.py"]
