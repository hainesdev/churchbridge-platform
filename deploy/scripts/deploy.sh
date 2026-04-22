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

# Keep the shared Nginx proxy vhost config in sync.  The dhaines_nginx container
# mounts /var/www/dhaines.dev/nginx/conf.d from the host; this file must be there
# for Nginx to route churchbridge.dhaines.dev traffic.  Copying on every deploy
# ensures it survives a re-clone or reset of the dhaines.dev directory.
NGINX_CONF_DIR="/var/www/dhaines.dev/nginx/conf.d"
if [[ -d "${NGINX_CONF_DIR}" ]]; then
  cp "${REPO_ROOT}/deploy/nginx/churchbridge.dhaines.dev.conf" "${NGINX_CONF_DIR}/churchbridge.conf"
  echo "Nginx vhost config installed."
fi

# Reload the shared Nginx proxy so it re-resolves the recreated container IPs.
# Containers get new IPs on every `up` recreation; Nginx caches the old ones
# and returns 502 until its config is reloaded.
nginx_container=$(docker ps -q --filter name=dhaines_nginx --filter status=running | head -1)
if [[ -n "${nginx_container}" ]]; then
  echo "Reloading nginx (${nginx_container})..."
  docker exec "${nginx_container}" nginx -t 2>&1
  docker exec "${nginx_container}" nginx -s reload
else
  echo "Warning: dhaines_nginx container not found; upstream routing may be stale." >&2
fi
