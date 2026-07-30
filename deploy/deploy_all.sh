#!/usr/bin/env bash
# KeyHolder — interactive deployment orchestration for Linux Mint 22.2.
# Safe to re-run. Does NOT install system deps (sudo + re-login required).
#
# Prerequisites (manual):
#   sudo bash deploy/install_system_deps.sh
#   sudo usermod -aG video $USER
#   re-login for docker and video groups

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

COMPOSE_FILE="${REPO_ROOT}/deploy/docker-compose.yml"
ENV_FILE="${REPO_ROOT}/deploy/.env"
ENV_EXAMPLE="${REPO_ROOT}/deploy/.env.example"
CONFIG_DEPLOY="${REPO_ROOT}/deploy/config.deploy.cfg"

confirm() {
  local prompt="$1"
  local answer=""
  read -r -p "${prompt} [y/N]: " answer
  case "${answer}" in
    [yY]|[yY][eE][sS]) return 0 ;;
    *) echo "Skipped."; return 1 ;;
  esac
}

wait_for_healthy() {
  local max_attempts=30
  local attempt=1

  echo "==> Waiting for PostgreSQL healthcheck (up to ${max_attempts} attempts)..."
  while (( attempt <= max_attempts )); do
    if docker compose -f "${COMPOSE_FILE}" ps 2>/dev/null | grep -qiE 'healthy'; then
      echo "==> PostgreSQL container is healthy."
      docker compose -f "${COMPOSE_FILE}" ps
      return 0
    fi
    sleep 2
    ((attempt++))
  done

  echo "WARNING: Health check timed out. Inspect status manually:" >&2
  docker compose -f "${COMPOSE_FILE}" ps || true
  return 1
}

echo "=== KeyHolder deployment orchestrator ==="
echo "Repository: ${REPO_ROOT}"
echo
echo "REMINDER: Install system dependencies first (requires sudo and re-login):"
echo "  sudo bash deploy/install_system_deps.sh"
echo "  sudo usermod -aG video \$USER"
echo "Then log out and back in before continuing."
echo

# (a) Database
if confirm "Step 1/3: Start PostgreSQL (docker compose up -d)?"; then
  if [[ ! -f "${ENV_FILE}" ]]; then
    if [[ ! -f "${ENV_EXAMPLE}" ]]; then
      echo "ERROR: Missing ${ENV_EXAMPLE}" >&2
      exit 1
    fi
    echo "==> Creating deploy/.env from deploy/.env.example..."
    cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  else
    echo "==> Using existing deploy/.env"
  fi

  echo "==> Starting PostgreSQL container..."
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d

  wait_for_healthy || true
fi

echo

# (b) Build
if confirm "Step 2/3: Build AdminApp and UserApp (deploy/build_linux.sh)?"; then
  bash "${REPO_ROOT}/deploy/build_linux.sh"
fi

echo

# (c) Deploy config
if confirm "Step 3/3: Apply deploy/config.deploy.cfg to dist bundles?"; then
  if [[ ! -f "${CONFIG_DEPLOY}" ]]; then
    echo "ERROR: Missing ${CONFIG_DEPLOY}" >&2
    exit 1
  fi

  applied=0
  for app in AdminApp UserApp; do
    dist_dir="${REPO_ROOT}/dist/${app}"
    if [[ -d "${dist_dir}" ]]; then
      cp "${CONFIG_DEPLOY}" "${dist_dir}/config.cfg"
      echo "==> Applied config to dist/${app}/config.cfg"
      applied=1
    else
      echo "WARN: dist/${app} not found; run build step first." >&2
    fi
  done

  if [[ "${applied}" -eq 0 ]]; then
    echo "No dist directories updated." >&2
  fi
fi

echo
echo "=== Orchestration finished ==="
echo "Launch applications:"
echo "  bash deploy/run_admin.sh"
echo "  bash deploy/run_user.sh"
echo "Full guide: deploy/DEPLOY_MINT.md"
