# FotoHu

Семейный бот-архиватор фотографий: снимки прилетают в **Telegram** или **Viber**,
уезжают в **OneDrive**, **Google Drive** или любое другое облако — **без потери
качества** — и удаляются из переписки.

```
Telegram ─┐                                 ┌─ OneDrive       (Microsoft Graph)
          ├─→  FotoHu  ──→ проверка хеша ──┼─ Google Drive   (Drive v3)
Viber ────┘       │                         ├─ Box / Dropbox / pCloud / … (rclone)
                  ↓                         └─ Локальная папка / NAS
          удаление из чата
      (только после проверки копии)
```

## Что умеет

- **Оригинальное качество.** Файлы, присланные документом, сохраняются побайтно,
  вместе с EXIF. Контрольная сумма сверяется с той, что вернуло облако, — это не
  обещание, а проверка.
- **Очистка чата.** Снимок удаляется из переписки после успешной загрузки —
  и только после неё.
- **Семейный доступ.** Приглашения по кодам. Каждому своя папка, всем одна общая,
  или общая для выбранной группы — настраивается глобально и лично для человека.
- **Админка прямо в боте.** Кнопочное меню в Telegram, текстовые команды в Viber.
  Веб-интерфейс не нужен.
- **Любое облако.** Родные адаптеры для OneDrive и Google Drive, плюс rclone для
  ещё ~70 провайдеров.

## Быстрый старт

```bash
git clone https://github.com/uausername/fotohu && cd fotohu
cp .env.example .env

# Ключ шифрования токенов — сгенерировать один раз
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# вписать его в FOTOHU_SECRET_KEY, а токен бота от @BotFather — в TELEGRAM_BOT_TOKEN

docker compose up -d
```

Дальше — в Telegram:

1. `/start ВАШ_BOOTSTRAP_TOKEN` → вы администратор.
2. `/admin` → **☁️ Хранилище** → подключить облако.
3. `/admin` → **👨‍👩‍👧 Семья** → создать приглашение и отправить родственнику.
4. Присылайте фотографии **файлом**. Как — `/howto`.

Без Docker: `pip install -e .` и `python -m fotohu`.
Проверить настройку, ничего не запуская: `python -m fotohu --check`.

## Документация

| | |
|---|---|
| [docs/limitations.md](docs/limitations.md) | **Читать первым.** Что мессенджеры не дают сделать и почему |
| [docs/setup-telegram.md](docs/setup-telegram.md) | Бот, вебхук, свой Bot API сервер для файлов > 20 МБ |
| [docs/setup-viber.md](docs/setup-viber.md) | Бот, обязательный HTTPS-вебхук |
| [docs/setup-onedrive.md](docs/setup-onedrive.md) | Регистрация приложения в Microsoft Entra |
| [docs/setup-google-drive.md](docs/setup-google-drive.md) | OAuth-клиент в Google Cloud Console |
| [docs/setup-rclone.md](docs/setup-rclone.md) | Box, Dropbox, pCloud, Яндекс.Диск, WebDAV, S3 |
| [docs/admin-guide.md](docs/admin-guide.md) | Панель администратора, папки, группы, очистка |

## Три ограничения, о которых честно

Их создаёт не бот, а сами мессенджеры — подробности в
[docs/limitations.md](docs/limitations.md):

1. **Telegram не удаляет сообщения старше 48 часов.** «Удалять через неделю»
   технически невозможно; по умолчанию бот удаляет через час после загрузки.
2. **Viber вообще не умеет удалять сообщения ботом** — в его API нет такого метода.
3. **Telegram отдаёт ботам файлы до 20 МБ.** Свой Bot API сервер (профиль
   `local-api`) поднимает предел до 2 ГБ.

## Разработка

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

## Лицензия

MIT
