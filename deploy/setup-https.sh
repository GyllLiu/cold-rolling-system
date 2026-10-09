#!/usr/bin/env bash
set -euo pipefail
test "$(id -u)" = 0
certbot_bin=/opt/cold-rolling/certbot/bin/certbot
"$certbot_bin" certonly --non-interactive --agree-tos --register-unsafely-without-email \
    --cert-name cold-rolling-ip --preferred-profile shortlived --webroot \
    --webroot-path /var/lib/cold-rolling/acme --ip-address 8.221.114.18
install -m 0644 /opt/cold-rolling/current/deploy/nginx-https.conf /etc/nginx/sites-available/cold-rolling
nginx -t
systemctl reload nginx
cat > /etc/systemd/system/cold-rolling-cert-renew.service <<'UNIT'
[Unit]
Description=Renew cold rolling IP HTTPS certificate
After=network-online.target
[Service]
Type=oneshot
ExecStart=/opt/cold-rolling/certbot/bin/certbot renew --quiet
ExecStartPost=/usr/sbin/nginx -t
ExecStartPost=/bin/systemctl reload nginx
UNIT
cat > /etc/systemd/system/cold-rolling-cert-renew.timer <<'UNIT'
[Unit]
Description=Check cold rolling IP certificate hourly
[Timer]
OnCalendar=hourly
RandomizedDelaySec=900
Persistent=true
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now cold-rolling-cert-renew.timer
echo HTTPS_SETUP_COMPLETE
