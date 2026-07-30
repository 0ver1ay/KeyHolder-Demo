#!/usr/bin/env bash
# Запуск AdminApp из собранного PyInstaller-бандла (dist/AdminApp).
#
# Профиль БД из контейнера (deploy/docker-compose.yml):
#   cp deploy/config.deploy.cfg dist/AdminApp/config.cfg
# или задайте DATABASE_URL перед запуском, например:
#   export DATABASE_URL='postgresql+psycopg2://postgres:postgres@127.0.0.1:5433/postgres'
#
# Сборка бинарника: bash deploy/build_linux.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
APPDIR="${REPO_ROOT}/dist/AdminApp"
BINARY="${APPDIR}/AdminApp"

if [[ ! -x "${BINARY}" ]]; then
  echo "AdminApp binary not found: ${BINARY}" >&2
  echo "Run deploy/build_linux.sh first, then retry." >&2
  exit 1
fi

export KIVY_LOG_LEVEL=info
export KIVY_LOG_MODE=PYTHON
export KIVY_NO_FILELOG=1
# Fallback if config.cfg missing database section:
export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg2://postgres:postgres@127.0.0.1:5433/postgres}"

cd "${APPDIR}"
exec ./AdminApp
