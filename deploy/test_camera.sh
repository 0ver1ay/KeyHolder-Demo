#!/usr/bin/env bash
# Быстрая проверка камеры на Linux перед запуском UserApp.
# Не требует PostgreSQL и не запускает GUI.
#
# Usage:
#   bash deploy/test_camera.sh           # индекс из config.cfg или 0
#   bash deploy/test_camera.sh 1         # явный индекс
#   CAMERA_INDEX=1 bash deploy/test_camera.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_ROOT}/config.cfg"

failures=0

note_ok() { echo "  OK: $*"; }
note_warn() { echo "  WARN: $*"; }
note_fail() { echo "  FAIL: $*"; failures=$((failures + 1)); }

camera_index="${CAMERA_INDEX:-${1:-}}"

if [[ -z "${camera_index}" && -f "${CONFIG}" ]]; then
  camera_index="$(grep -E '^camera_index=' "${CONFIG}" 2>/dev/null | head -n1 | cut -d= -f2- | tr -d '[:space:]' || true)"
fi
camera_index="${camera_index:-0}"

echo "==> KeyHolder camera check (index=${camera_index})"
echo

echo "==> Video devices (/dev/video*)"
if compgen -G '/dev/video*' >/dev/null; then
  ls -l /dev/video* 2>/dev/null | sed 's/^/  /'
else
  note_fail "No /dev/video* devices found. Is the camera connected?"
fi
echo

echo "==> Group membership (need 'video' for device access)"
if id -nG "${USER}" 2>/dev/null | tr ' ' '\n' | grep -qx video; then
  note_ok "User '${USER}' is in group 'video'"
else
  note_fail "User '${USER}' is NOT in group 'video'"
  echo "       Fix: sudo usermod -aG video ${USER}  (then log out and back in)"
fi
echo

echo "==> v4l2-ctl (optional)"
if command -v v4l2-ctl >/dev/null 2>&1; then
  v4l2-ctl --list-devices 2>/dev/null | sed 's/^/  /' || note_warn "v4l2-ctl failed to list devices"
else
  note_warn "v4l2-ctl not installed (sudo apt install v4l-utils)"
fi
echo

pick_python() {
  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    echo "${REPO_ROOT}/.venv/bin/python"
    return 0
  fi
  if [[ -x "${REPO_ROOT}/.venv-build/bin/python" ]]; then
    echo "${REPO_ROOT}/.venv-build/bin/python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    echo "python3"
    return 0
  fi
  return 1
}

echo "==> OpenCV capture test (same backend as UserApp: cv2.VideoCapture)"
PY="$(pick_python || true)"
if [[ -z "${PY}" ]]; then
  note_fail "python3 not found"
else
  if ! "${PY}" -c "import cv2" 2>/dev/null; then
    note_fail "OpenCV (cv2) not available for ${PY}"
    echo "       Fix: cd ${REPO_ROOT} && python3 -m venv .venv && .venv/bin/pip install opencv-python"
  else
    echo "  Using: ${PY}"
    if "${PY}" - "${camera_index}" <<'PY'
import sys
import cv2

index = int(sys.argv[1])
cap = cv2.VideoCapture(index)
if not cap.isOpened():
    print(f"  FAIL: cv2.VideoCapture({index}) did not open")
    sys.exit(1)

ok, frame = cap.read()
cap.release()

if not ok or frame is None:
    print(f"  FAIL: opened index {index} but could not read a frame")
    sys.exit(1)

h, w = frame.shape[:2]
print(f"  OK: frame captured {w}x{h} from index {index}")
PY
    then
      :
    else
      failures=$((failures + 1))
      echo "       Try another index: bash deploy/test_camera.sh 1"
      echo "       Or set camera_index=N in ${CONFIG}"
    fi
  fi
fi
echo

if [[ "${failures}" -eq 0 ]]; then
  echo "=== Camera check passed ==="
  exit 0
fi

echo "=== Camera check failed (${failures} issue(s)) ==="
echo "UserApp will still run without photos if the camera fails at runtime."
exit 1
