#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
COMPOSE_FILE="deploy/docker-compose.prod.yml"
API_WAIT_TIMEOUT="${API_WAIT_TIMEOUT:-180}"
WEB_WAIT_TIMEOUT="${WEB_WAIT_TIMEOUT:-240}"

cd "${REPO_ROOT}"

if [[ ! -f ".env.production" ]]; then
  echo ".env.production is required before deployment." >&2
  exit 1
fi

set_env_var() {
  local key="$1"
  local value="$2"
  local env_file="${3:-.env.production}"

  if grep -qE "^${key}=" "${env_file}"; then
    local current
    current="$(grep -E "^${key}=" "${env_file}" | head -1 | cut -d= -f2-)"
    if [[ "${current}" == "${value}" ]]; then
      return 0
    fi
    python3 - "${env_file}" "${key}" "${value}" <<'PY'
from pathlib import Path
import sys

env_path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = env_path.read_text().splitlines()
updated = []
replaced = False
prefix = f"{key}="
for line in lines:
    if line.startswith(prefix):
        updated.append(f"{prefix}{value}")
        replaced = True
    else:
        updated.append(line)
if not replaced:
    updated.append(f"{prefix}{value}")
env_path.write_text("\n".join(updated) + "\n")
PY
    echo "Updated ${key} in ${env_file}."
    return 0
  fi

  printf '%s=%s\n' "${key}" "${value}" >> "${env_file}"
  echo "Added ${key} to ${env_file}."
}

ensure_google_speech_env() {
  local env_file=".env.production"
  local host_credentials_path="/var/www/churchbridge-ai/secrets/google-speech-service-account.json"
  local container_credentials_path="/app/secrets/google-speech-service-account.json"

  set_env_var "GOOGLE_CLOUD_PROJECT" "active-alchemy-491315-c4" "${env_file}"
  set_env_var "GOOGLE_CLOUD_LOCATION" "us" "${env_file}"
  set_env_var "GOOGLE_SPEECH_MODEL" "chirp_3" "${env_file}"
  set_env_var "GOOGLE_SPEECH_LANGUAGE" "es-US" "${env_file}"
  set_env_var "GOOGLE_SPEECH_RECOGNIZER" "_" "${env_file}"
  set_env_var "GOOGLE_APPLICATION_CREDENTIALS" "${container_credentials_path}" "${env_file}"

  if [[ ! -f "${host_credentials_path}" ]]; then
    echo "Warning: ${host_credentials_path} does not exist on the host. Google Speech will still fail until the service account key is placed there." >&2
  fi
}

ensure_google_speech_env

compose() {
  docker compose -f "${COMPOSE_FILE}" "$@"
}

print_service_logs() {
  local service="$1"
  echo "----- ${service} logs -----" >&2
  compose logs --tail=200 "${service}" >&2 || true
}

wait_for_service() {
  local service="$1"
  local timeout_s="$2"
  local deadline=$((SECONDS + timeout_s))
  local container_id=""
  local status=""

  while (( SECONDS < deadline )); do
    container_id="$(compose ps -q "${service}" 2>/dev/null | head -1)"
    if [[ -n "${container_id}" ]]; then
      status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}" 2>/dev/null || true)"
      case "${status}" in
        healthy|running)
          echo "${service} is ${status}."
          return 0
          ;;
        unhealthy|exited|dead)
          echo "${service} entered terminal status: ${status}" >&2
          print_service_logs "${service}"
          return 1
          ;;
      esac
    fi

    sleep 5
  done

  echo "Timed out waiting for ${service} to become ready." >&2
  compose ps >&2 || true
  print_service_logs "${service}"
  return 1
}

bash "${SCRIPT_DIR}/sync-db.sh"

# Legacy cleanup: older production revisions pinned fixed container names
# (`churchbridge_api`, `churchbridge_web`). Those names can conflict with
# compose-managed recreation and leave the API offline behind nginx.
for legacy_name in churchbridge_api churchbridge_web; do
  legacy_id="$(docker ps -aq --filter "name=^/${legacy_name}$" | head -1)"
  if [[ -n "${legacy_id}" ]]; then
    echo "Removing legacy container ${legacy_name} (${legacy_id})..."
    docker rm -f "${legacy_id}"
  fi
done

echo "Stopping current compose stack to avoid recreate-time container name conflicts..."
compose down --remove-orphans

compose up -d --build api
wait_for_service api "${API_WAIT_TIMEOUT}"

compose up -d web
wait_for_service web "${WEB_WAIT_TIMEOUT}"

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
