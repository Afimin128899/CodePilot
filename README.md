# CodePilot — AI coding workspace

Веб-редактор с coding-агентом, файловым деревом, редактором, поиском по проекту и предложениями изменений с ручным подтверждением. Поддерживает Anthropic Claude API и OpenAI-совместимый API с настройкой ключа, модели и Base URL в веб-интерфейсе.

## Возможности

- Агент умеет исследовать дерево проекта, читать файлы, искать текст и подготавливать правки нескольких файлов.
- Файлы не перезаписываются автоматически: правки нужно подтвердить в UI.
- Парольный вход, CSRF-токен для запросов, HttpOnly/SameSite session cookie и базовые security headers.
- Пути файлов ограничены рабочей папкой; доступ к .env, ключам и session-файлам блокируется.
- Терминал выключен по умолчанию. Allowlist команд — не полноценная OS-песочница.
- Ключ API вводится в окне «Модель / API» и не сохраняется приложением на диск.

## Запуск на Debian/Ubuntu VPS

```bash
cd ~/CodePilot
apt update
apt install -y git python3-venv python3-pip python3-dev build-essential
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
cp -n .env.example .env
nano .env
```

В .env задай уникальные значения APP_PASSWORD и APP_SECRET_KEY. Сгенерировать секретный ключ можно командой:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Скопируй результат в APP_SECRET_KEY и задай собственный APP_PASSWORD. Затем запусти:

```bash
python3 app.py
```

Приложение слушает только 127.0.0.1:8000. Для проверки с локального компьютера открой SSH-туннель:

```bash
ssh -L 8000:127.0.0.1:8000 root@IP_СЕРВЕРА
```

Затем открой http://localhost:8000 и войди с паролем из .env. Нажми «Модель / API», выбери провайдера, укажи доступную модель и вставь свой API-ключ. Base URL оставь пустым для официального API или задай адрес своего совместимого шлюза.

## Обновление существующей установки

```bash
cd ~/CodePilot
git pull origin main
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 app.py
```

Если приложение работает как systemd-сервис, перезапусти его после обновления. Если оно запущено в терминале, останови старый процесс Ctrl+C и запусти заново.

## Docker

```bash
cp .env.example .env
nano .env
docker compose up --build -d
```

Compose привязывает порт к localhost. Установи свои APP_PASSWORD и APP_SECRET_KEY. Для выполнения команд включай ENABLE_COMMANDS=1 только в изолированной среде.

## Безопасность и ограничения

Перед удалённым доступом настрой HTTPS через Nginx/Caddy или VPN и COOKIE_SECURE=1 (если HTTPS уже настроен). Парольная авторизация общая, не предназначена для нескольких пользователей и не заменяет полноценную систему управления пользователями.

Даже с allowlist команд процесс не полностью изолирован. Не открывай сервис напрямую в интернет без reverse proxy, HTTPS и дополнительных мер защиты. Для выполнения кода используй отдельный контейнер или пользователя с ограниченными правами, лимитами CPU/RAM и без доступа к секретам хоста. Не вводи API-ключ на чужом экземпляре CodePilot.
