#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root" >&2
  exit 1
fi

PUBLIC_HOSTNAME="${1:-}"
if [[ -z "$PUBLIC_HOSTNAME" ]]; then
  echo "Usage: sudo ./deploy/bootstrap.sh bot.example.com" >&2
  exit 1
fi

apt-get update
apt-get install -y python3-venv rsync curl openssl nginx certbot python3-certbot-nginx

id drojun >/dev/null 2>&1 || useradd --system --home /opt/drojun --shell /usr/sbin/nologin drojun
install -d -o drojun -g drojun /opt/drojun/app /var/lib/drojun
install -m 0755 deploy/drojun-deploy /usr/local/sbin/drojun-deploy
install -m 0644 deploy/drojun.service /etc/systemd/system/drojun.service

if [[ ! -f /etc/drojun.env ]]; then
  cat >/etc/drojun.env <<EOF
DATABASE_URL=sqlite:////var/lib/drojun/drojun.db
FLASK_SECRET_KEY=$(openssl rand -hex 32)
ADMIN_USERNAME=admin
ADMIN_PASSWORD=CHANGE_ME
WEBHOOK_SECRET=$(openssl rand -hex 32)
KITE_API_KEY=CHANGE_ME
KITE_API_SECRET=CHANGE_ME
KITE_USER_ID=CHANGE_ME
SIGNAL_MAX_AGE_SECONDS=300
GATEWAY_SHARED_SECRET=$(openssl rand -hex 32)
REQUIRE_GATEWAY_SIGNATURE=true
EOF
  chmod 0600 /etc/drojun.env
fi

cat >/etc/nginx/sites-available/drojun-executor <<EOF
server {
    listen 80;
    server_name $PUBLIC_HOSTNAME;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
ln -sfn /etc/nginx/sites-available/drojun-executor /etc/nginx/sites-enabled/drojun-executor

systemctl daemon-reload
systemctl enable drojun nginx
/usr/local/sbin/drojun-deploy "$(pwd)"
nginx -t
systemctl reload nginx
certbot --nginx --non-interactive --agree-tos --register-unsafely-without-email --redirect -d "$PUBLIC_HOSTNAME"
systemctl enable --now certbot.timer

echo "Edit /etc/drojun.env, replace every CHANGE_ME value, then run: systemctl restart drojun"
echo "Open https://$PUBLIC_HOSTNAME/ after the Railway gateway is configured."

