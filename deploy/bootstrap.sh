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
apt-get install -y python3-venv rsync curl gnupg openssl debian-keyring debian-archive-keyring apt-transport-https

if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  apt-get update
  apt-get install -y caddy
fi

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

cat >/etc/caddy/Caddyfile <<EOF
$PUBLIC_HOSTNAME {
  encode zstd gzip
  reverse_proxy 127.0.0.1:8000
}
EOF

systemctl daemon-reload
systemctl enable drojun caddy
/usr/local/sbin/drojun-deploy "$(pwd)"
systemctl reload caddy

echo "Edit /etc/drojun.env, replace every CHANGE_ME value, then run: systemctl restart drojun"
echo "Open https://$PUBLIC_HOSTNAME/ after DNS points to this Droplet."
