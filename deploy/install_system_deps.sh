#!/usr/bin/env bash
# KeyHolder — system dependency bootstrap for Linux Mint 22.2 (Ubuntu 24.04 noble).
# Installs native Kivy/KivyMD runtime libs, PyInstaller build toolchain, and Docker.
# Idempotent: safe to re-run.

set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "This script must be run as root (e.g. sudo bash deploy/install_system_deps.sh)." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "==> Updating package lists..."
apt-get update

echo "==> Installing build/runtime/system libraries..."
apt-get install -y \
  build-essential \
  python3 \
  python3-venv \
  python3-dev \
  python3-pip \
  git \
  patchelf \
  libsdl2-dev \
  libsdl2-image-dev \
  libsdl2-mixer-dev \
  libsdl2-ttf-dev \
  libglib2.0-0 \
  libsm6 \
  libxext6 \
  libxrender1 \
  libgomp1 \
  libpq-dev \
  v4l-utils \
  xclip \
  xsel

echo "==> Installing OpenGL libraries..."
if ! apt-get install -y libgl1; then
  echo "libgl1 unavailable; trying mesa-utils libgl1-mesa-dri..." >&2
  apt-get install -y mesa-utils libgl1-mesa-dri
fi

echo "==> Installing Docker Engine..."
# docker.io is in Mint/Ubuntu repos. Compose is a separate package on some systems.
apt-get install -y docker.io

echo "==> Installing Docker Compose (plugin)..."
compose_ok=0
if docker compose version >/dev/null 2>&1; then
  compose_ok=1
  echo "docker compose already available."
elif apt-get install -y docker-compose-v2; then
  compose_ok=1
elif apt-get install -y docker-compose-plugin; then
  compose_ok=1
fi

if [[ "${compose_ok}" -eq 0 ]] && ! docker compose version >/dev/null 2>&1; then
  echo "WARNING: 'docker compose' not found after apt install." >&2
  echo "Try manually: sudo apt install docker-compose-plugin" >&2
  echo "Or see deploy/КАК_УСТАНОВИТЬ.md section «Если install_system_deps.sh падает»." >&2
fi

if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  systemctl enable --now docker || true
else
  echo "systemd not available; skipping 'systemctl enable --now docker'." >&2
fi

invoke_user="${SUDO_USER:-${USER:-root}}"
if [[ "${invoke_user}" != "root" ]]; then
  if id -nG "${invoke_user}" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
    echo "User '${invoke_user}' is already in the 'docker' group."
  else
    usermod -aG docker "${invoke_user}"
    echo "Added '${invoke_user}' to the 'docker' group."
  fi
  echo "NOTE: Log out and back in (or reboot) for docker group membership to take effect."
else
  echo "Running as root without SUDO_USER; skipped adding a user to the 'docker' group." >&2
fi

echo "==> Installing optional PyInstaller compression helper (best-effort)..."
apt-get install -y upx-ucl || echo "upx-ucl not available; continuing without UPX compression."

echo
echo "=== Verification ==="
docker --version || echo "WARN: docker command missing" >&2
docker compose version || echo "WARN: docker compose missing (see doc)" >&2
python3 --version
ldconfig -p | grep -E 'libGL|libSDL2' || true
echo "=== Done ==="
