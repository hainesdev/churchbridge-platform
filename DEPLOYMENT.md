# ChurchBridge AI Deployment

This project is deployed to `churchbridge.dhaines.dev` on the Ubuntu droplet at `167.71.84.35`.

## Production layout

- `churchbridge_web`: Next.js production server on port `3000`
- `churchbridge_api`: FastAPI server on port `8000`
- Shared reverse proxy: existing Dockerized Nginx stack at `/var/www/dhaines.dev`
- Shared Docker network: `dhainesdev_web`
- Persistent database volume: `churchbridge_data`

The public proxy serves the site on `https://churchbridge.dhaines.dev`, routes `/api/*` to the FastAPI container, and routes everything else to the Next.js container.

## Files in this repo

- `deploy/docker-compose.prod.yml`: production compose stack
- `deploy/api.Dockerfile`: backend image
- `deploy/web.Dockerfile`: frontend image
- `deploy/nginx/churchbridge.dhaines.dev.conf`: vhost config for the shared Nginx proxy — **installed automatically by `deploy.sh`** into `/var/www/dhaines.dev/nginx/conf.d/churchbridge.conf` on every deploy. Also committed to the `dhaines.dev` repo as a belt-and-suspenders backup.
- `deploy/.env.production.example`: production env template
- `deploy/scripts/deploy.sh`: build and restart the stack, install the nginx vhost config, and reload `dhaines_nginx`
- `deploy/scripts/sync-db.sh`: copies `data/churchbridge.db` into the Docker volume before deploy when present
- `deploy/scripts/deploy-ref.sh`: deploy a specific Git SHA/ref from `main`
- `deploy/scripts/update-if-needed.sh`: pull `main` and redeploy when changed
- `deploy/systemd/churchbridge-ai-autodeploy.service`: systemd oneshot unit
- `deploy/systemd/churchbridge-ai-autodeploy.timer`: systemd timer for polling GitHub
- `.github/workflows/deploy.yml`: GitHub Actions production deploy workflow

## Server bootstrap

1. Clone this repo to `/var/www/churchbridge-ai`.
2. Create `/var/www/churchbridge-ai/.env.production` from `deploy/.env.production.example`.
3. Copy the production SQLite file to `/var/www/churchbridge-ai/data/churchbridge.db`.
4. Add the `churchbridge` DNS record in DigitalOcean pointing to `167.71.84.35`.
5. Issue a certificate with the existing Certbot container and webroot volume: `docker compose -f /var/www/dhaines.dev/docker-compose.yml run --rm certbot certonly --webroot -w /var/www/certbot -d churchbridge.dhaines.dev`.
6. Run `deploy/scripts/deploy.sh` — this installs the nginx vhost config, reloads `dhaines_nginx`, and brings up the stack.
7. Install and enable the systemd timer for automatic updates.

> **Note:** Step 4 in the old bootstrap ("copy the nginx config manually") is now handled automatically by `deploy.sh`.

## GitHub Actions deployment

GitHub Actions can now deploy production on every push to `main` and via manual dispatch.

### Required GitHub secrets

- `DEPLOY_HOST`: droplet hostname or IP, for example `167.71.84.35`
- `DEPLOY_USER`: SSH user, for example `root`
- `DEPLOY_SSH_KEY`: private key that can SSH into the droplet
- `DEPLOY_PATH`: repo checkout on the droplet, for example `/var/www/churchbridge-ai`
- `DEPLOY_PORT`: optional SSH port, defaults to `22`
- `DEPLOY_HEALTHCHECK_URL`: optional public health URL, for example `https://churchbridge.dhaines.dev/health`

### Workflow behavior

- On push to `main`, GitHub Actions SSHes into the droplet and deploys the exact pushed SHA.
- Manual runs can deploy any SHA or ref through the workflow dispatch `ref` input.
- The workflow uses `deploy/scripts/deploy-ref.sh`, which fetches `main`, hard-resets to the requested ref, and runs the existing Docker deploy script.

### Recommended `gh` commands

```bash
gh workflow run deploy.yml --ref main
gh workflow run deploy.yml -f ref=<sha>
gh run list --workflow deploy.yml
gh run watch
gh run view --log
```

## Useful commands

```bash
# ChurchBridge containers
cd /var/www/churchbridge-ai
docker compose -f deploy/docker-compose.prod.yml ps
docker compose -f deploy/docker-compose.prod.yml logs -f api
docker compose -f deploy/docker-compose.prod.yml logs -f web
./deploy/scripts/deploy.sh

# Auto-deploy timer
systemctl status churchbridge-ai-autodeploy.timer
journalctl -u churchbridge-ai-autodeploy.service -n 100 --no-pager
./deploy/scripts/update-if-needed.sh

# Shared Nginx proxy (dhaines_nginx)
docker exec dhaines_nginx nginx -t              # test config before reload
docker exec dhaines_nginx nginx -s reload       # reload config + re-resolve upstream IPs
docker logs dhaines_nginx --tail 50             # nginx access/error logs

# SSL certificates (via dhainesdev_certbot_conf volume)
docker run --rm -v dhainesdev_certbot_conf:/etc/letsencrypt certbot/certbot:latest certificates
```

## Notes

- `NEXT_PUBLIC_API_URL` should stay on the same origin (`https://churchbridge.dhaines.dev`) so browser websocket connections use the public proxy instead of trying to reach port `8000` directly.
- The app persists SQLite data in the Docker volume `churchbridge_data`.
- The Bible corpus should also exist at `/var/www/churchbridge-ai/data/churchbridge.db` on the server. `deploy/scripts/sync-db.sh` seeds the Docker volume from that file before each deploy.
- The auto-deploy timer is pull-based. It checks `origin/main` every 5 minutes, hard-resets to that commit, and rebuilds only when the commit SHA changes.
- The GitHub Actions workflow is a better default control plane than the polling timer because it deploys the exact pushed SHA immediately and gives you logs in GitHub. Keep the timer only as a fallback if you still want pull-based recovery.
- **Nginx vhost config**: `deploy/nginx/churchbridge.dhaines.dev.conf` is the source of truth. `deploy.sh` copies it to `/var/www/dhaines.dev/nginx/conf.d/churchbridge.conf` on every deploy, so the file survives a re-clone or reset of the `dhaines.dev` directory. The same file is also committed to the `dhaines.dev` repo as a fallback.
- **Nginx reload**: `deploy.sh` reloads `dhaines_nginx` after every `docker compose up`. This is required because container recreation assigns new IP addresses; without a reload, Nginx routes to the old (dead) IPs and returns 502.
- **GitHub Actions health check timing**: The workflow fires the health check immediately after `docker compose up` returns. The API container has a Docker health check with `start_period: 20s` (so Compose waits for it), but the web container starts after the API and needs a few additional seconds. A transient 502 from the health check step does not mean the deploy failed — verify with `curl https://churchbridge.dhaines.dev/health` directly.
