#!/usr/bin/env bash
# KeyHolder — build AdminApp and UserApp Linux onedir bundles with PyInstaller.
# Run on Linux Mint 22.2 from the repository root (script cd's there automatically).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

echo "==> Repository root: ${REPO_ROOT}"

required_files=(
  requirements.txt
  main_admin.py
  main_user.py
  config.cfg
)
missing=0
for f in "${required_files[@]}"; do
  if [[ ! -f "${REPO_ROOT}/${f}" ]]; then
    echo "ERROR: Missing ${REPO_ROOT}/${f}" >&2
    missing=1
  fi
done
if [[ ! -d "${REPO_ROOT}/views" ]]; then
  echo "ERROR: Missing ${REPO_ROOT}/views/" >&2
  missing=1
fi
if [[ "${missing}" -ne 0 ]]; then
  echo >&2
  echo "This script needs the FULL KeyHolder repository, not only deploy/." >&2
  echo "On Mint, cd to the project root (where requirements.txt lives), then run:" >&2
  echo "  bash deploy/build_linux.sh" >&2
  exit 1
fi

echo "==> Recreating build virtual environment..."
rm -rf .venv-build
python3 -m venv .venv-build
# shellcheck source=/dev/null
source .venv-build/bin/activate

echo "==> Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt -r deploy/requirements-linux.txt "pyinstaller==6.11.1"

# PyInstaller + Kivy/KivyMD: avoid isolated-subprocess crashes (exit -4 / SIGILL).
export PYINSTALLER_NO_ISOLATED=1
# Older CPUs: numpy/OpenBLAS wheels may use unsupported SIMD instructions.
export OPENBLAS_CORETYPE=NEHALEM
export NPY_DISABLE_CPU_FEATURES="${NPY_DISABLE_CPU_FEATURES:-AVX2,FMA3,AVX512F,AVX512_SKX}"
# Quiet build logs (TRACE floods the terminal for minutes — looks like a hang).
unset PYTHONVERBOSE
export KIVY_LOG_LEVEL=error

echo "==> Building AdminApp (5–15 min, do NOT press Ctrl+C)..."
pyinstaller --log-level WARN --noconfirm --clean deploy/pyinstaller/AdminApp.linux.spec

echo "==> Building UserApp (5–15 min)..."
pyinstaller --log-level WARN --noconfirm --clean deploy/pyinstaller/UserApp.linux.spec

echo "==> Copying runtime data next to binaries..."
cp -r views config.cfg dist/AdminApp/
cp -r views config.cfg dist/UserApp/

ADMIN_BIN="$(cd dist/AdminApp && pwd)/AdminApp"
USER_BIN="$(cd dist/UserApp && pwd)/UserApp"

echo
echo "=== Build complete ==="
echo "AdminApp: ${ADMIN_BIN}"
echo "UserApp:  ${USER_BIN}"
echo
echo "Reminder: install system dependencies first:"
echo "  sudo bash deploy/install_system_deps.sh"
