# Shorts Auto Editor

Telegram-бот, который превращает обычное длинное видео в готовые YouTube Shorts:

```
видео ─► поиск динамики / нарезка ─► формат 9:16 ─► субтитры с подсветкой слов ─► фоновая музыка ─► Shorts в чат
```

Проект объединяет код и идеи ваших репозиториев в один конвейер, а интерфейс
(Telegram, SQLite, структура модулей, документация) построен по образцу
[Vinted_Telegramm_Bot](https://github.com/Liq0et-ev/Vinted_Telegramm_Bot).

## Откуда что взято

| Модуль | Исходный репозиторий | Что делает |
|--------|----------------------|------------|
| `shorts_factory/highlights/` | Dinamic_Control_YouTube_Videos | Поиск самых динамичных моментов: движение, оптический поток, громкость, спектральный поток; 3 алгоритма (heuristic / surprisal / attention) |
| `shorts_factory/slicer.py` | One_Minute_Videos | Последовательная нарезка на клипы случайной длины без пропусков и перекрытий |
| `shorts_factory/subtitles/` | Semi_Final_Video_Redactor, Youtube_AutoTitles_Creator, Second_Try_For_YouTube_videos | Whisper (EN/RU), слова группами по 3, подсветка текущего слова, 5 brand kit |
| `shorts_factory/audio_mix.py` | Background_Audio | Случайная цепочка треков под длину видео, микс с оригинальным звуком |
| `shorts_factory/bot.py`, `jobs.py`, `storage.py`, `main.py` | Vinted_Telegramm_Bot | Telegram, один asyncio-цикл, aiosqlite, `.env`, меню команд |

Что сделано заново: перевод в вертикальный формат 9:16 (размытый фон или обрезка по центру), единая очередь задач,
микс музыки на чистом ffmpeg (видеопоток не перекодируется), режим «хайлайт» режет и склеивает через ffmpeg,
а не через moviepy.

## Режимы

- **🔥 Хайлайт** — из длинного видео собирается один шорт ~60 с из самых динамичных моментов.
- **✂️ Нарезка** — всё видео режется на клипы 45–60 с (настраивается), каждый превращается в отдельный шорт (не более 10 за задачу).

## Быстрый старт

Нужны Python 3.10+ и FFmpeg в `PATH`.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # Linux/Mac: cp .env.example .env
```

1. Создайте бота у [@BotFather](https://t.me/BotFather) и впишите токен в `.env`.
2. Запустите `python main.py`, отправьте боту `/start` — он покажет ваш Telegram ID.
3. Впишите ID в `ALLOWED_USER_IDS` в `.env` и перезапустите бота.
4. Положите mp3/wav в `data/music` (или отправьте их боту файлом).

Без `ALLOWED_USER_IDS` бот никому не отвечает: рендер видео тяжёлый, открытый бот позволил бы любому
занять ваш компьютер.

## Как пользоваться

1. Отправьте видео файлом (до 20 МБ), либо ссылку (YouTube и др., через `yt-dlp`), либо положите файл в
   `data/inbox` и выберите его командой `/inbox`.
2. Бот покажет панель: режим, субтитры, музыка, стиль, формат 9:16, длина, алгоритм. Выбор запоминается как
   значения по умолчанию.
3. Нажмите «▶️ Запустить». Бот присылает статус с процентами, затем готовые шорты.

| Команда | Действие |
|---------|----------|
| `/start` | Справка (для чужих — их Telegram ID) |
| `/settings` | Настройки по умолчанию |
| `/inbox` | Файлы из папки inbox |
| `/jobs` | Последние 5 задач |
| `/music` | Музыкальная библиотека |
| `/cancel` | Отменить активные задачи |

## Без Telegram

```bash
python -m shorts_factory.cli video.mp4 --mode slice --kit neon_blue --no-music
python -m shorts_factory.cli video.mp4 --mode highlight --target 45 --variant attention
```

`python -m shorts_factory.cli --help` показывает все параметры.

## Ограничения

- Telegram позволяет боту скачивать файлы только до **20 МБ** и отправлять до **50 МБ**. Для больших файлов используйте
  `/inbox` или локальный Bot API сервер (`TELEGRAM_API_BASE_URL`, см. [DEPLOYMENT](docs/DEPLOYMENT.md)).
  Если готовый шорт больше лимита, бот сообщит путь к файлу на диске.
- Задачи выполняются по одной. На CPU субтитры к минутному клипу с моделью `medium` занимают порядка минуты и больше;
  для скорости поставьте `WHISPER_MODEL=small` или `base`.
- Язык субтитров определяется автоматически только между английским и русским.
- Скачивание по ссылкам зависит от `yt-dlp` и условий сервиса-источника. Используйте только видео, на которые у вас есть права.
- Отмена задачи срабатывает между этапами, а не посреди рендера субтитров.

## Тесты

```bash
pip install pytest pytest-asyncio
python -m pytest
```

90 тестов: нарезка, опции, SQLite, ffmpeg-слой, музыка, пайплайн на синтетических видео, логика панели, доступ,
очередь задач (успех, ошибка, отмена, большой файл). Тесты не обращаются к Telegram и не требуют токена.

Структура и устройство: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), запуск на сервере и в фоне:
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md), проблемы: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).
