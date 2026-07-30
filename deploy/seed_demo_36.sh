#!/usr/bin/env bash
# Linux Mint: создать 1 бокс, 2 пользователей, 36 помещений, 36 ключей и раздать права.
#
# Использование:
#   bash deploy/seed_demo_36.sh
#
# Переменные окружения (необязательно):
#   BOX_NAME, USER1_LOGIN, USER1_PASSWORD, USER2_LOGIN, USER2_PASSWORD, ROOM_COUNT
#   DATABASE_URL  — строка подключения к PostgreSQL
#
# Перед запуском PostgreSQL должен быть доступен (см. deploy/docker-compose.yml).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

VENV="${REPO_ROOT}/.venv"
REQ_LINUX="${REPO_ROOT}/deploy/requirements-linux.txt"

if [[ ! -d "${VENV}" ]]; then
  echo "==> Creating .venv and installing dependencies (first run)..."
  python3 -m venv "${VENV}"
  # shellcheck source=/dev/null
  source "${VENV}/bin/activate"
  pip install --upgrade pip
  pip install -r requirements.txt
  if [[ -f "${REQ_LINUX}" ]]; then
    pip install -r "${REQ_LINUX}"
  fi
else
  # shellcheck source=/dev/null
  source "${VENV}/bin/activate"
fi

if [[ ! -f "${REPO_ROOT}/config.cfg" ]] || ! grep -q '^port=5433' "${REPO_ROOT}/config.cfg" 2>/dev/null; then
  echo "==> Applying deploy/config.deploy.cfg -> config.cfg"
  cp "${REPO_ROOT}/deploy/config.deploy.cfg" "${REPO_ROOT}/config.cfg"
fi

# Load DB settings from config.cfg if DATABASE_URL is not set
if [[ -z "${DATABASE_URL:-}" ]] && [[ -f "${REPO_ROOT}/config.cfg" ]]; then
  DB_HOST="$(grep -E '^host=' "${REPO_ROOT}/config.cfg" | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
  DB_PORT="$(grep -E '^port=' "${REPO_ROOT}/config.cfg" | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
  DB_USER="$(grep -E '^user=' "${REPO_ROOT}/config.cfg" | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
  DB_PASS="$(grep -E '^password=' "${REPO_ROOT}/config.cfg" | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
  DB_NAME="$(grep -E '^name=' "${REPO_ROOT}/config.cfg" | head -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
  DB_HOST="${DB_HOST:-127.0.0.1}"
  DB_PORT="${DB_PORT:-5433}"
  DB_USER="${DB_USER:-postgres}"
  DB_PASS="${DB_PASS:-postgres}"
  DB_NAME="${DB_NAME:-postgres}"
  export DATABASE_URL="postgresql+psycopg2://${DB_USER}:${DB_PASS}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
fi

export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg2://postgres:postgres@127.0.0.1:5433/postgres}"

echo "==> Checking PostgreSQL..."
if ! python - <<'PY'
from db.session import get_engine
from sqlalchemy import text
engine = get_engine()
with engine.connect() as conn:
    conn.execute(text("SELECT 1"))
print("PostgreSQL OK")
PY
then
  echo ""
  echo "PostgreSQL недоступен. Запустите БД:"
  echo "  docker compose -f deploy/docker-compose.yml up -d"
  exit 1
fi

echo "==> Seeding demo data (1 box, 2 users, 36 rooms/keys)..."
python scripts/seed_demo_36.py

echo ""
echo "Готово. Запустите AdminApp для проверки:"
echo "  bash deploy/run_dev_admin.sh"
