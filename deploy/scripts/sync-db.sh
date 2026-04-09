#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SOURCE_DB="${REPO_ROOT}/data/churchbridge.db"
VOLUME_DIR="/var/lib/docker/volumes/deploy_churchbridge_data/_data"
TARGET_DB="${VOLUME_DIR}/churchbridge.db"

if [[ ! -f "${SOURCE_DB}" ]]; then
  echo "No source DB at ${SOURCE_DB}; skipping DB sync."
  exit 0
fi

mkdir -p "${VOLUME_DIR}"

if [[ ! -f "${TARGET_DB}" ]] || [[ "${SOURCE_DB}" -nt "${TARGET_DB}" ]] || [[ $(stat -c%s "${TARGET_DB}" 2>/dev/null || echo 0) -lt 1048576 ]]; then
  cp "${SOURCE_DB}" "${TARGET_DB}"
  echo "Synced ${SOURCE_DB} -> ${TARGET_DB}"
else
  echo "DB sync not needed."
fi
