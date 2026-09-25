# Запуск в Docker

Бот использует long polling и автоматически перезапускается после сбоя или
перезагрузки WSL/Docker.

Токен не хранится в `compose.yaml`. По умолчанию он читается из файла
`/home/ubuntu0/.config/timetable-bot/max_bot_token.txt`.

```bash
cd /mnt/c/Users/amirk/OneDrive/Документы/timetable-main
docker compose -f compose.yaml up -d --build
```

Просмотр состояния и журналов:

```bash
docker compose -f compose.yaml ps
docker compose -f compose.yaml logs -f --tail=100
```

Остановка:

```bash
docker compose -f compose.yaml down
```
