# Запуск на сервере и в фоне

Бот работает, только пока запущен `main.py`.

## Требования

- Python 3.10+ и FFmpeg в `PATH` (`ffmpeg -version` и `ffprobe -version` должны работать).
- Для субтитров: Whisper `medium` занимает несколько ГБ ОЗУ. На слабой машине поставьте `WHISPER_MODEL=small` или `base`.
- Видеокарта NVIDIA с CUDA ускоряет Whisper многократно; без неё всё работает на процессоре.
- Свободное место: исходник + временные файлы + результат. Временные файлы лежат в `data/work` и чистятся после задачи.

Рендер нагружает процессор, поэтому комфортнее всего запускать бота на домашнем компьютере или на VPS с 4+ ГБ ОЗУ.

## Установка (Ubuntu)

```bash
apt update && apt install -y python3-venv python3-pip git ffmpeg
git clone <адрес вашего репозитория> Shorts_Auto_Editor
cd Shorts_Auto_Editor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
```

В `.env` укажите `TELEGRAM_BOT_TOKEN` и `ALLOWED_USER_IDS`. Проверочный запуск: `.venv/bin/python main.py`,
в Telegram отправьте `/start`.

## Автозапуск через systemd

Файл `/etc/systemd/system/shorts-bot.service`:

```ini
[Unit]
Description=Shorts Auto Editor Telegram Bot
After=network-online.target

[Service]
WorkingDirectory=/root/Shorts_Auto_Editor
ExecStart=/root/Shorts_Auto_Editor/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Если проект лежит не в `/root`, поправьте пути.

```bash
systemctl daemon-reload
systemctl enable --now shorts-bot
systemctl status shorts-bot      # состояние
journalctl -u shorts-bot -f      # логи
systemctl restart shorts-bot     # перезапуск после обновления
```

## Windows

Запустите из обычного терминала `.venv\Scripts\python.exe main.py`. Для автозапуска создайте задачу в
«Планировщике заданий» с триггером «При входе в систему» и этой же командой, рабочая папка — папка проекта.

## Большие файлы: локальный Bot API сервер

Облачный Bot API не отдаёт боту файлы больше 20 МБ и не принимает загрузки больше 50 МБ. Снять лимиты можно
своим [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) сервером (нужны `api_id` и `api_hash` с my.telegram.org):

```bash
telegram-bot-api --api-id=<ID> --api-hash=<HASH> --local --http-port=8081
```

Затем в `.env`: `TELEGRAM_API_BASE_URL=http://localhost:8081`, при необходимости увеличьте
`TELEGRAM_UPLOAD_LIMIT_MB` (до 2000). Перед переключением выйдите с облачного API методом `logOut`, иначе токен
будет привязан к старому серверу. Без этого просто используйте папку `data/inbox` и команду `/inbox`.

## Обновление

```bash
cd Shorts_Auto_Editor
git pull
.venv/bin/pip install -r requirements.txt
systemctl restart shorts-bot
```

## Перенос с другого компьютера

1. Остановите бота на старом месте: два экземпляра с одним токеном конфликтуют.
2. Скопируйте `data/shorts_factory.sqlite3` (настройки и история) и папку `data/music`.

## Безопасность

- Токен только в `.env`. Если он попал в чат, скриншот или лог, отзовите его в BotFather (`Revoke current token`) и впишите новый.
- `ALLOWED_USER_IDS` ограничивает круг пользователей. Не оставляйте его пустым на публичном боте.
- Не коммитьте `.env`, `*.sqlite3` и содержимое `data/` (они в `.gitignore`).
- Скачивайте по ссылкам и обрабатывайте только то, на что у вас есть права; для музыки проверяйте лицензию на использование на YouTube.
