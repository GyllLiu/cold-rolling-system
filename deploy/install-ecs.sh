#!/usr/bin/env bash
# Run from an uploaded, uniquely named release directory on the target ECS.
set -euo pipefail
umask 022
release_dir="$(cd "$(dirname "$0")/.." && pwd)"
test "$(id -u)" = 0
case "$release_dir" in /opt/cold-rolling/releases/*) ;; *) echo 'Unexpected release directory'; exit 1 ;; esac
test -f "$release_dir/dist/server/index.js"
node -e 'const [a,b]=process.versions.node.split(".").map(Number); if(a<22 || (a===22 && b<13)) process.exit(1)'

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
apt-get update
apt-get install -y python3-venv nginx
id cold-rolling >/dev/null 2>&1 || useradd --system --home-dir /var/lib/cold-rolling --shell /usr/sbin/nologin cold-rolling
install -d -m 0750 -o cold-rolling -g cold-rolling /var/lib/cold-rolling/capture
install -d -m 0755 /var/lib/cold-rolling/acme/.well-known/acme-challenge
install -d -m 0750 /etc/cold-rolling
chown cold-rolling:cold-rolling /var/lib/cold-rolling
chown -R cold-rolling:cold-rolling "$release_dir"
runuser -u cold-rolling -- sh -c 'cd "$1" && npm ci --ignore-scripts --no-audit --no-fund --cache /var/lib/cold-rolling/npm-cache' sh "$release_dir"
runuser -u cold-rolling -- python3 -m venv "$release_dir/.venv"
runuser -u cold-rolling -- "$release_dir/.venv/bin/pip" install --index-url https://pypi.org/simple -r "$release_dir/backend/requirements-capture.txt"

if [ ! -f /etc/cold-rolling/capture.env ]; then
    python3 - <<'PY'
import os, secrets
path = '/etc/cold-rolling/capture.env'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    f.write('CAPTURE_DATA_DIR=/var/lib/cold-rolling/capture\n')
    f.write('CAPTURE_ACCESS_CODE=' + secrets.token_hex(6) + '\n')
PY
fi

cat > /etc/systemd/system/cold-rolling-web.service <<'UNIT'
[Unit]
Description=Cold rolling recommendation and photo page
After=network.target
[Service]
User=cold-rolling
Group=cold-rolling
WorkingDirectory=/opt/cold-rolling/current
Environment=NODE_ENV=production
Environment=PORT=3100
Environment=VINEXT_TRUSTED_HOSTS=8.221.114.18
ExecStart=/usr/bin/node /opt/cold-rolling/current/deploy/server.mjs
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
MemoryMax=768M
[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/cold-rolling-capture.service <<'UNIT'
[Unit]
Description=Cold rolling photo and pass record storage
After=network.target
[Service]
User=cold-rolling
Group=cold-rolling
WorkingDirectory=/opt/cold-rolling/current
EnvironmentFile=/etc/cold-rolling/capture.env
ExecStart=/opt/cold-rolling/current/.venv/bin/uvicorn backend.app.capture:app --host 127.0.0.1 --port 8101 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/cold-rolling/capture
MemoryMax=384M
UMask=0077
[Install]
WantedBy=multi-user.target
UNIT

ln -sfn "$release_dir" /opt/cold-rolling/current
systemctl daemon-reload
systemctl enable cold-rolling-web cold-rolling-capture
systemctl start cold-rolling-web cold-rolling-capture
if [ ! -f /etc/nginx/sites-available/cold-rolling ]; then
    install -m 0644 "$release_dir/deploy/nginx-http.conf" /etc/nginx/sites-available/cold-rolling
fi
ln -sfn /etc/nginx/sites-available/cold-rolling /etc/nginx/sites-enabled/cold-rolling
nginx -t
systemctl enable nginx
systemctl reload nginx
python3 -m venv /opt/cold-rolling/certbot
/opt/cold-rolling/certbot/bin/pip install --index-url https://pypi.org/simple 'certbot==5.8.0'
curl --retry 10 --retry-connrefused --retry-delay 1 -fsS http://127.0.0.1:8101/api/capture/health
curl -fsS http://127.0.0.1:3100/api/health
echo APP_INSTALL_COMPLETE
