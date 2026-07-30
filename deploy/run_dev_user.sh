#!/usr/bin/env bash
# Запуск UserApp из исходников (без PyInstaller).

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

export KIVY_LOG_LEVEL=info
export KIVY_CLIPBOARD=dummy
export KIVY_WINDOW=sdl2
export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg2://postgres:postgres@127.0.0.1:5433/postgres}"

echo "==> Starting UserApp (python main_user.py)..."
exec python main_user.py
