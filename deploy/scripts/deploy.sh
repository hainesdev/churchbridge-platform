#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

cd "${REPO_ROOT}"

if [[ ! -f ".env.production" ]]; then
  echo ".env.production is required before deployment." >&2
  exit 1
fi

bash "${SCRIPT_DIR}/sync-db.sh"

docker compose -f deploy/docker-compose.prod.yml up -d --build --remove-orphans

# Reload the shared Nginx proxy so it re-resolves the recreated container IPs.
# Containers get new IPs on every `up` recreation; Nginx caches the old ones
# and returns 502 until its config is reloaded.
nginx_container=$(docker ps -q --filter name=nginx --filter status=running | head -1)
if [[ -n "${nginx_container}" ]]; then
  echo "Reloading nginx (${nginx_container})..."
  docker exec "${nginx_container}" nginx -s reload
else
  echo "Warning: no running nginx container found; upstream routing may be stale." >&2
fi
