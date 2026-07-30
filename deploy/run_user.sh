#!/usr/bin/env bash
# Запуск UserApp из собранного PyInstaller-бандла (dist/UserApp).
#
# Профиль БД из контейнера (deploy/docker-compose.yml):
#   cp deploy/config.deploy.cfg dist/UserApp/config.cfg
# или задайте DATABASE_URL перед запуском, например:
#   export DATABASE_URL='postgresql+psycopg2://postgres:postgres@127.0.0.1:5433/postgres'
#
# Сборка бинарника: bash deploy/build_linux.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
APPDIR="${REPO_ROOT}/dist/UserApp"
BINARY="${APPDIR}/UserApp"

if [[ ! -x "${BINARY}" ]]; then
  echo "UserApp binary not found: ${BINARY}" >&2
  echo "Run deploy/build_linux.sh first, then retry." >&2
  exit 1
fi

export KIVY_LOG_LEVEL=info
export KIVY_LOG_MODE=PYTHON
export KIVY_NO_FILELOG=1
export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg2://postgres:postgres@127.0.0.1:5433/postgres}"

cd "${APPDIR}"
exec ./UserApp
