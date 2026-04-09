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
- `deploy/nginx/churchbridge.dhaines.dev.conf`: vhost config to copy into the shared proxy
- `deploy/.env.production.example`: production env template
- `deploy/scripts/deploy.sh`: build and restart the stack
- `deploy/scripts/update-if-needed.sh`: pull `main` and redeploy when changed
- `deploy/systemd/churchbridge-ai-autodeploy.service`: systemd oneshot unit
- `deploy/systemd/churchbridge-ai-autodeploy.timer`: systemd timer for polling GitHub

## Server bootstrap

1. Clone this repo to `/var/www/churchbridge-ai`.
2. Create `/var/www/churchbridge-ai/.env.production` from `deploy/.env.production.example`.
3. Copy `deploy/nginx/churchbridge.dhaines.dev.conf` into `/var/www/dhaines.dev/nginx/conf.d/`.
4. Add the `churchbridge` DNS record in DigitalOcean pointing to `167.71.84.35`.
5. Reload the shared Nginx container after the HTTP config is present.
6. Issue a certificate with the existing Certbot container and webroot volume.
7. Run `deploy/scripts/deploy.sh`.
8. Install and enable the systemd timer for automatic updates.

## Useful commands

```bash
cd /var/www/churchbridge-ai
docker compose -f deploy/docker-compose.prod.yml ps
docker compose -f deploy/docker-compose.prod.yml logs -f api
docker compose -f deploy/docker-compose.prod.yml logs -f web
./deploy/scripts/deploy.sh
./deploy/scripts/update-if-needed.sh
systemctl status churchbridge-ai-autodeploy.timer
journalctl -u churchbridge-ai-autodeploy.service -n 100 --no-pager
```

## Notes

- `NEXT_PUBLIC_API_URL` should stay on the same origin (`https://churchbridge.dhaines.dev`) so browser websocket connections use the public proxy instead of trying to reach port `8000` directly.
- The app persists SQLite data in the Docker volume `churchbridge_data`.
- The auto-deploy timer is pull-based. It checks `origin/main` every 5 minutes, hard-resets to that commit, and rebuilds only when the commit SHA changes.
