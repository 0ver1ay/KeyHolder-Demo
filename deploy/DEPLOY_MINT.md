# Развёртывание KeyHolder на Linux Mint 22.2 «Zara»

> **Простая версия без терминов:** [КАК_УСТАНОВИТЬ.md](КАК_УСТАНОВИТЬ.md)

Пошаговая инструкция для чистой машины Mint 22.2 (база Ubuntu 24.04 noble, Python 3.12, x86_64).
Все команды выполняются **из корня репозитория** (каталог, где лежат `deploy/`, `main_admin.py` и т.д.).

---

## Требования

| Параметр | Значение |
|----------|----------|
| ОС | Linux Mint 22.2 «Zara», архитектура **x86_64** |
| Сеть | Доступ в интернет (apt, Docker-образ PostgreSQL, pip при сборке) |
| Графика | Активная графическая сессия (X11/Wayland); приложения — GUI на Kivy/KivyMD |
| Камера | Устройство `/dev/video0`; пользователь должен быть в группе `video` |
| Репозиторий | Клонированный или скопированный проект KeyHolder |

Приложения **AdminApp** и **UserApp** собираются нативно на Mint (PyInstaller). PostgreSQL
работает в Docker на `127.0.0.1`. Схема БД создаётся автоматически при первом запуске
приложения (`Base.metadata.create_all` и последующие `ALTER`).

---

## Шаг 1. Системные зависимости

Установите системные библиотеки (SDL2, OpenGL, libpq, Docker и т.д.):

```bash
sudo bash deploy/install_system_deps.sh
```

Скрипт идемпотентен — повторный запуск безопасен.

**После установки обязательно:**

1. **Выйти из сессии и войти снова** (или перезагрузить машину), чтобы вступила в силу
   группа `docker` (скрипт добавляет текущего пользователя в неё через `SUDO_USER`).
2. Добавить пользователя в группу **`video`** для доступа к камере:

```bash
sudo usermod -aG video $USER
```

После `usermod` для группы `video` тоже нужен повторный вход в сессию.

Проверка Docker (после re-login):

```bash
docker --version
docker compose version
```

---

## Шаг 2. База данных (PostgreSQL 16 в Docker)

Создайте файл окружения из шаблона:

```bash
cp deploy/.env.example deploy/.env
```

При необходимости отредактируйте `deploy/.env` (пароль `POSTGRES_PASSWORD`, порт `DB_PORT`).
По умолчанию: пользователь/пароль/БД — `postgres`/`postgres`/`postgres`, порт `5433`.

Запустите контейнер:

```bash
docker compose -f deploy/docker-compose.yml up -d
```

Проверьте статус — в колонке **STATUS** должно быть **healthy**:

```bash
docker compose -f deploy/docker-compose.yml ps
```

Контейнер слушает только localhost: `127.0.0.1:${DB_PORT:-5433}` → PostgreSQL внутри на 5432.
Данные хранятся в именованном томе `pgdata`.

---

## Шаг 3. Сборка приложений

Сборка создаёт виртуальное окружение `.venv-build`, устанавливает зависимости и PyInstaller,
собирает бандлы и копирует `views/` и `config.cfg` рядом с бинарниками:

```bash
bash deploy/build_linux.sh
```

Итоговые исполняемые файлы:

- `dist/AdminApp/AdminApp`
- `dist/UserApp/UserApp`

Рядом с каждым бинарником должны лежать каталог `views/` и файл `config.cfg` (скрипт копирует
их из корня репозитория; профиль БД для production настраивается на шаге 4).

---

## Шаг 4. Конфигурация профиля БД

Скопируйте шаблон развёртывания в оба бандла (подключается к `127.0.0.1:5433`):

```bash
cp deploy/config.deploy.cfg dist/AdminApp/config.cfg
cp deploy/config.deploy.cfg dist/UserApp/config.cfg
```

При необходимости отредактируйте `device_host`, `device_port`, `box_id` и другие секции
в скопированных `config.cfg`.

**Альтернатива** — переменная окружения (переопределяет `config.cfg`):

```bash
export DATABASE_URL='postgresql+psycopg2://postgres:postgres@127.0.0.1:5433/postgres'
```

Если меняете порт в `deploy/.env` (`DB_PORT`), обновите и `port=` в `config.cfg` (или URL),
чтобы значения совпадали.

---

## Шаг 5. Запуск

Запуск AdminApp:

```bash
bash deploy/run_admin.sh
```

Запуск UserApp:

```bash
bash deploy/run_user.sh
```

Скрипты проверяют наличие бинарника, выставляют `KIVY_LOG_LEVEL=info` и запускают приложение
из соответствующего каталога `dist/<App>/`.

---

## Чек-лист приёмки

- [ ] `docker compose -f deploy/docker-compose.yml ps` — контейнер PostgreSQL в статусе **healthy**
- [ ] `bash deploy/run_admin.sh` — AdminApp стартует без ошибок импортов/Kivy
- [ ] `bash deploy/run_user.sh` — UserApp стартует без ошибок импортов/Kivy
- [ ] В логах при старте есть строка подключения к БД, содержащая **`[DB] Connected`**
      (полный текст: `[DB] Connected successfully to: ...`)
- [ ] Данные сохраняются после рестарта контейнера:

```bash
docker compose -f deploy/docker-compose.yml restart
```

После рестарта приложения снова подключаются к той же БД.

---

## Траблшутинг

### Ошибки libGL / SDL2 при запуске GUI

Установите или переустановите системные пакеты:

```bash
sudo bash deploy/install_system_deps.sh
```

Проверка наличия библиотек:

```bash
ldconfig -p | grep -E 'libGL|libSDL2'
```

### Камера недоступна

- Убедитесь, что устройство существует: `ls -l /dev/video0`
- Пользователь в группе `video`: `groups` (должна быть `video`)
- При необходимости: `sudo usermod -aG video $USER` и повторный вход в сессию
- В `config.cfg` проверьте `camera_index=0` в секции `[device]`

### GUI не открывается («cannot open display»)

Приложению нужна активная графическая сессия. Запускайте из терминала внутри рабочего стола,
не из чистой SSH-сессии без X11. При удалённом доступе настройте `DISPLAY` (например `:0`)
и разрешения X11.

### Не удаётся подключиться к БД

- Контейнер запущен и healthy: `docker compose -f deploy/docker-compose.yml ps`
- Порт в `deploy/.env` (`DB_PORT`) совпадает с `port=` в `dist/*/config.cfg`
- Учётные данные в `.env` совпадают с `config.cfg` или `DATABASE_URL`
- Проверка готовности PostgreSQL: `docker compose -f deploy/docker-compose.yml exec postgres pg_isready -U postgres`

### Смена порта БД

1. Задайте `DB_PORT` в `deploy/.env`
2. Пересоздайте контейнер: `docker compose -f deploy/docker-compose.yml up -d`
3. Обновите `port=` в `dist/AdminApp/config.cfg` и `dist/UserApp/config.cfg`
   (или `DATABASE_URL`)

### Полная очистка данных БД

Удаляет контейнер и том с данными:

```bash
docker compose -f deploy/docker-compose.yml down -v
```

При следующем `up -d` БД будет пустой; схема создастся заново при запуске приложения.

### Бинарник не найден

```bash
bash deploy/build_linux.sh
```

---

## Опционально: автозапуск через systemd

В каталоге `deploy/systemd/` лежат шаблоны:

- `deploy/systemd/keyholder-admin.service`
- `deploy/systemd/keyholder-user.service`

Перед установкой замените плейсхолдеры **`__USER__`** (имя пользователя Linux) и
**`__REPO_ROOT__`** (абсолютный путь к корню репозитория). При необходимости раскомментируйте
`Environment=DISPLAY=:0`.

Пример для AdminApp:

```bash
# Отредактируйте файл: __USER__ → ваш пользователь, __REPO_ROOT__ → /home/user/KeyHolder
sudo cp deploy/systemd/keyholder-admin.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now keyholder-admin.service
```

Аналогично для UserApp с `keyholder-user.service`.

> **Важно:** GUI-приложениям нужна графическая сессия и корректный `DISPLAY` у пользователя
> юнита. Автозапуск на headless-сервере без дисплея не поддерживается «из коробки».

---

## Быстрый оркестратор

После шага 1 (системные зависимости + re-login) можно использовать интерактивный скрипт:

```bash
bash deploy/deploy_all.sh
```

Он по шагам (с подтверждением y/N) поднимает БД, собирает приложения и копирует
`config.deploy.cfg`. Установку `install_system_deps.sh` **не** выполняет — только напоминает
о необходимости сделать это вручную заранее.
