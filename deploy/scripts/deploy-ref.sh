#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BRANCH="${BRANCH:-main}"
REF="${REF:-origin/${BRANCH}}"

cd "${REPO_ROOT}"

if [[ ! -f ".env.production" ]]; then
  echo ".env.production is required before deployment." >&2
  exit 1
fi

git fetch --prune origin "${BRANCH}"
git checkout "${BRANCH}" 2>/dev/null || git checkout -B "${BRANCH}" "origin/${BRANCH}"
git reset --hard "${REF}"

bash "${SCRIPT_DIR}/deploy.sh"
