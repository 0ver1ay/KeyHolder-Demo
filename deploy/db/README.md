# PostgreSQL для KeyHolder (Docker)

Контейнер с PostgreSQL 16 для локального деплоя. Схему БД приложение создаёт само при первом запуске — миграции вручную не нужны.

## Подготовка

```bash
cp deploy/.env.example deploy/.env
```

Отредактируйте `deploy/.env`: в production обязательно смените `POSTGRES_PASSWORD`.

## Запуск

Из корня репозитория:

```bash
docker compose -f deploy/docker-compose.yml up -d
```

Порт по умолчанию: `127.0.0.1:5433` (задаётся через `DB_PORT` в `.env`).

## Проверка состояния

```bash
docker compose -f deploy/docker-compose.yml ps
```

Колонка `STATUS` должна показывать `healthy` после старта.

## Логи

```bash
docker compose -f deploy/docker-compose.yml logs -f postgres
```

## Подключение через psql

```bash
docker compose -f deploy/docker-compose.yml exec postgres \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

Или с хоста (если установлен клиент `psql`), используя значения из `deploy/.env`:

```bash
psql -h 127.0.0.1 -p 5433 -U postgres -d postgres
```

## Полный сброс данных

Удаляет контейнер и именованный том `pgdata` (все данные БД будут потеряны):

```bash
docker compose -f deploy/docker-compose.yml down -v
```

После сброса снова выполните `up -d`; при следующем запуске KeyHolder пересоздаст схему.
