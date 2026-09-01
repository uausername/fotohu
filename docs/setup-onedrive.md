# OneDrive

## 1. Зарегистрировать приложение

1. <https://entra.microsoft.com> → **Applications** → **App registrations** →
   **New registration**.
2. **Supported account types:** «Accounts in any organizational directory and
   personal Microsoft accounts» — нужно, чтобы работали и личный OneDrive,
   и рабочий.
3. **Redirect URI:** тип **Web**, значение:

```
https://ваш-домен/oauth/callback
```

   Оно должно совпадать с `FOTOHU_PUBLIC_URL` + `/oauth/callback` символ в символ.

4. Скопируйте **Application (client) ID** в `.env` → `ONEDRIVE_CLIENT_ID`.

## 2. Разрешения

**API permissions** → **Microsoft Graph** → **Delegated permissions**:

- `Files.ReadWrite` — создавать и записывать файлы
- `offline_access` — **обязательно**, иначе refresh token не выдаётся и связь
  оборвётся через час
- `User.Read` — чтобы показать, какой аккаунт подключён

## 3. Секрет (опционально)

Для типа Web Microsoft обычно требует client secret:
**Certificates & secrets** → **New client secret** → в `.env`:

```
ONEDRIVE_CLIENT_SECRET=...
```

FotoHu использует PKCE в любом случае, так что при публичном клиенте секрет можно
не задавать.

## 4. Подключить

В боте: `/admin` → **☁️ Хранилище** → **＋ Подключить облако** → **Microsoft
OneDrive** → **🔗 Подключить аккаунт**. Ссылка одноразовая и живёт 10 минут.

Проверить: **🧪 Проверить связь** — покажет тип диска, владельца и свободное место.

## Как это работает

- Файлы до 4 МБ уходят одним `PUT`.
- Крупнее — через upload session кусками по 10 МБ (Graph требует кратности
  320 КиБ). Обрыв связи стоит одного куска, а не всей фотографии.
- Существующий файл никогда не перезаписывается: при совпадении имени добавляется
  ` (2)`, а полностью идентичный по хешу просто пропускается.

## Проверка целостности

OneDrive Personal возвращает `sha1Hash`/`sha256Hash` — сверка побайтная.
OneDrive for Business отдаёт только `quickXorHash`, который мы не считаем, поэтому
там сверяется размер, и бот честно пишет об этом в подтверждении. Подробнее —
[limitations.md](limitations.md).
