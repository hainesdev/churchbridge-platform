#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BRANCH="${BRANCH:-main}"

cd "${REPO_ROOT}"

git fetch origin "${BRANCH}"

LOCAL_SHA="$(git rev-parse HEAD)"
REMOTE_SHA="$(git rev-parse "origin/${BRANCH}")"

if [[ "${LOCAL_SHA}" == "${REMOTE_SHA}" ]]; then
  echo "No update required."
  exit 0
fi

git checkout "${BRANCH}"
git reset --hard "origin/${BRANCH}"
bash "${SCRIPT_DIR}/deploy.sh"
