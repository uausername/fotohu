# Разворачивание на бесплатном VPS (Google Cloud e2-micro)

FotoHu — это один процесс, который должен работать постоянно: пока он выключен
дольше суток, очередь Telegram протухает (см.
[limitations.md](limitations.md#5-что-происходит-когда-бот-выключен)). Домашний
компьютер, который гасят на ночь, для этого не годится — нужен всегда включённый
сервер.

Этот документ — про **Google Cloud Free Tier**: одна виртуальная машина
`e2-micro`, бесплатно и бессрочно. Бот на ней помещается с запасом. Инструкция
подойдёт и для любого другого VPS на Debian/Ubuntu — отличаться будут только
шаги 1–2.

## Что даёт бесплатный уровень и где его границы

| Ресурс | Лимит Free Tier | Хватает ли FotoHu |
|---|---|---|
| 1 × `e2-micro` | 2 vCPU (burst), **1 ГБ RAM** | Да. В простое ~150 МБ, под нагрузкой ~250 МБ — файлы стримятся на диск, пиксели картинок не декодируются |
| Регион | только `us-west1`, `us-central1`, `us-east1` | Да. Чуть выше пинг к Telegram из Европы, для бота незаметно |
| Диск | 30 ГБ **standard** persistent disk (не SSD) | Да. База, `rclone.conf` и временные файлы — это десятки мегабайт |
| Исходящий трафик | **1 ГБ/мес** из Северной Америки | **Нет.** См. ниже |

**Главное ограничение — трафик.** Каждая фотография, улетающая в облако, — это
исходящий трафик с виртуальной машины: и в Google Drive, и в OneDrive, и через
rclone. Бесплатный 1 ГБ/мес закрывает лишь очень скромный поток. Сверх лимита —
примерно **$0.12 за ГБ**. Реалистично: 10 ГБ фотографий в месяц ≈ $1.1. Это уже
не строго бесплатно, но это цена чашки кофе, и карту к аккаунту привязывать
придётся в любом случае. Снизить этот трафик нельзя — сами фотографии обязаны
доехать до облака.

Если поток фотографий большой и постоянный — дешевле и спокойнее взять самый
маленький платный VPS (Hetzner CX22 ≈ €3.8/мес с 20 ТБ трафика), чем платить
Google за перерасход egress.

## Шаг 1. Создать виртуальную машину

1. Зайдите в [console.cloud.google.com](https://console.cloud.google.com),
   создайте проект, привяжите биллинг-аккаунт (нужна банковская карта; при
   верификации спишут и вернут ~$1).
2. Включите **Compute Engine API** (консоль предложит это при первом заходе в
   раздел «Compute Engine» → «VM instances»).
3. **Create instance** со следующими параметрами — иначе машина не попадёт под
   Free Tier:

   | Поле | Значение |
   |---|---|
   | Region | `us-west1`, `us-central1` **или** `us-east1` |
   | Machine type | `e2-micro` (серия E2, shared-core) |
   | Boot disk → тип | **Standard persistent disk** (не Balanced, не SSD) |
   | Boot disk → размер | 30 GB |
   | Boot disk → образ | Debian 12 (bookworm) |
   | Firewall | **для Telegram — ничего не отмечать.** Для Viber или кнопки OAuth — «Allow HTTP traffic» и «Allow HTTPS traffic» |

Telegram работает через long polling: серверу FotoHu не нужны входящие
подключения, поэтому по умолчанию не открывайте никаких портов. Публичный HTTPS
нужен только для Viber и для привязки OneDrive/Google Drive кнопкой в боте —
об этом в конце документа.

## Шаг 2. Подключиться

Проще всего — кнопка **SSH** напротив инстанса в консоли (открывает терминал в
браузере). Либо, если установлен `gcloud`:

```bash
gcloud compute ssh fotohu --zone us-west1-b
```

## Шаг 3. Добавить swap

На 1 ГБ RAM swap обязателен — он подстрахует сборку образа и редкие пики памяти.

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## Шаг 4. Установить Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
sudo systemctl enable docker          # чтобы бот поднимался после перезагрузки VM
newgrp docker                         # применить группу без перелогина
```

## Шаг 5. Развернуть FotoHu

```bash
git clone https://github.com/uausername/fotohu && cd fotohu
cp .env.example .env

# Ключ шифрования токенов — сгенерировать один раз, вписать в FOTOHU_SECRET_KEY.
# Формат Fernet — это ровно urlsafe-base64 от 32 случайных байт:
docker run --rm python:3.11-slim \
  python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"

nano .env
```

Минимум, что нужно заполнить в `.env`:

```
FOTOHU_SECRET_KEY=<вывод команды выше>
FOTOHU_BOOTSTRAP_TOKEN=<любая длинная строка, используется один раз>
TELEGRAM_BOT_TOKEN=<токен от @BotFather>

# На 1 ГБ RAM держите загрузку в один поток
FOTOHU_WORKER_CONCURRENCY=1

# Логи в файл — на сервере без консоли пригодятся
FOTOHU_LOG_FILE=/data/bot.log
```

Запуск — с производственным оверлеем
([docker-compose.prod.yml](../docker-compose.prod.yml): лимит памяти как
предохранитель и ротация логов Docker):

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Чтобы не повторять `-f ...` каждый раз, задайте переменную окружения один раз:

```bash
echo 'export COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml' >> ~/.bashrc
source ~/.bashrc
# дальше достаточно: docker compose up -d / logs -f / down
```

## Шаг 6. Подключить облако (без домена, через rclone)

Домен и белый IP для этого не нужны — rclone проводит авторизацию сам:

```bash
docker compose run --rm bot rclone --config /data/rclone.conf config
```

Провайдеры (Google Drive, OneDrive, Box, Dropbox, Яндекс.Диск, …) и тонкость с
OAuth-портом rclone внутри контейнера описаны в
[setup-rclone.md](setup-rclone.md). После этого в боте: `/admin` →
**☁️ Хранилище** → **＋ Подключить облако** → «Другое облако (через rclone)».

## Шаг 7. Стать администратором и проверить

В Telegram отправьте боту:

```
/start ВАШ_FOTOHU_BOOTSTRAP_TOKEN
```

Первый, кто это сделает, становится администратором, после чего токен
отключается. Проверка конфигурации, ничего не запуская:

```bash
docker compose run --rm bot python -m fotohu --check
```

## Переезд работающей установки

Если бот уже где-то работает (домашний компьютер, Windows-служба) и вы переносите
его сюда вместе с данными, шаги 5–7 заменяются на это. Переезжает три вещи: база,
`rclone.conf` и `FOTOHU_SECRET_KEY`.

**1. Остановите старый экземпляр — до всего остального.** Два бота с одним токеном
не уживаются: Telegram отдаёт long polling кому-то одному, апдейты начнут теряться
случайным образом. И выключите автозапуск, иначе старый вернётся после ближайшей
перезагрузки:

```powershell
# Windows
Stop-ScheduledTask -TaskName FotoHu
Disable-ScheduledTask -TaskName FotoHu   # нужен PowerShell «от администратора»
```

**2. Сцепите базу.** SQLite работает в режиме WAL: рядом с `fotohu.sqlite3` лежат
`-wal` и `-shm`, и часть данных живёт в них. Копировать один `.sqlite3` без
слияния — значит потерять последние записи. После остановки бота:

```bash
python -c "import sqlite3; c=sqlite3.connect('data/fotohu.sqlite3'); \
print(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()); \
print(c.execute('PRAGMA integrity_check').fetchone()[0])"
```

Должно напечатать `(0, 0, 0)` и `ok`, а файлы `-wal`/`-shm` — исчезнуть.

**3. Перенесите три файла** на VM (`fotohu.sqlite3`, `rclone.conf`, `.env`).
Через Cloud Shell: **⋮ → Upload** кладёт файл в Cloud Shell, оттуда на машину —
`gcloud compute scp <файл> fotohu:~/ --zone=<зона>`.

**4. Перепишите пути в `.env` под контейнер.** Абсолютные пути старой машины
работать не будут:

```
RCLONE_BINARY=rclone
RCLONE_CONFIG=/data/rclone.conf
FOTOHU_DATA_DIR=/data
FOTOHU_LOG_FILE=/data/bot.log
```

`FOTOHU_SECRET_KEY` **обязан остаться прежним** — им зашифрованы токены облаков в
базе, с новым ключом их придётся привязывать заново.

**5. Положите базу в том до первого запуска.** Иначе бот создаст пустую:

```bash
cd ~/fotohu
docker compose up --no-start          # соберёт образ и создаст том
docker run --rm -v fotohu_fotohu-data:/data -v ~/migrate:/src:ro alpine \
  sh -c 'cp /src/fotohu.sqlite3 /src/rclone.conf /data/ && chown -R 10001:10001 /data'
docker compose run --rm bot python -m fotohu --check
```

`--check` должен показать прежнее число участников и настроенное хранилище.
Тогда — `docker compose up -d`.

**6. Уберите архив с секретами** отовсюду, куда он попал: с VM, из Cloud Shell и
с исходной машины. В нём токен бота, OAuth-токены облака и ключ шифрования.

## Обслуживание

**Логи:**

```bash
docker compose logs -f bot          # поток
tail -f data/bot.log                 # если задан FOTOHU_LOG_FILE
```

**Обновление:**

```bash
cd ~/fotohu && git pull
docker compose up -d --build
```

**Резервная копия.** Всё состояние — в томе `fotohu-data` (база SQLite,
`rclone.conf`, временные файлы). Забрать важное:

```bash
docker compose cp bot:/data/fotohu.sqlite3 ./backup-fotohu.sqlite3
docker compose cp bot:/data/rclone.conf   ./backup-rclone.conf
```

Потеря `FOTOHU_SECRET_KEY` означает повторную привязку всех облачных аккаунтов —
храните `.env` отдельно от сервера.

**После перезагрузки машины** бот поднимается сам: у сервисов стоит
`restart: unless-stopped`, а `systemctl enable docker` (шаг 4) запускает сам
Docker. Google периодически проводит обслуживание хостов, но `e2-micro` при этом
переезжает на другой хост живьём, без остановки.

## Если нужен Viber или кнопка OAuth (публичный HTTPS)

1. В консоли GCP зарезервируйте **статический внешний IP** и привяжите к
   инстансу (бесплатен, пока привязан к запущенной VM). Без этого IP меняется
   при каждой остановке машины.
2. Заведите DNS-запись `A` с вашего домена на этот IP.
3. Откройте порты 80 и 443 (правила firewall при создании VM или позже во
   вкладке «Firewall»).
4. В `.env`:

   ```
   FOTOHU_PUBLIC_URL=https://photos.example.com
   FOTOHU_DOMAIN=photos.example.com
   ```

5. Запустите с профилем `tls` — Caddy сам получит сертификат:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile tls up -d
   ```

Дальше — [setup-viber.md](setup-viber.md) или
[setup-onedrive.md](setup-onedrive.md) / [setup-google-drive.md](setup-google-drive.md).
