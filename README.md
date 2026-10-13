# Drojun

Drojun receives confirmed one-hour CCI zone-entry alerts from TradingView and manages one NIFTY option position at a time through Kite Connect.

## Strategy defaults

- CCI length: `50`
- Source: `HLC3`
- CALL: confirmed close crossing above `+100`
- PUT: confirmed close crossing below `-100`
- Minimum absolute delta: `0.69`
- Stop: `40` option points
- Target: `80` option points
- Quantity: `65`
- No new entries after `15:15` IST
- Same-day expiry is skipped

The CCI length, delta, stop, target, quantity, entry offset, and live-trading switch are editable on the password-protected control panel.

## Affordable deployment

Drojun runs on the existing DigitalOcean Droplet. The Droplet supplies the required static outbound IP, SQLite stores durable state, Caddy supplies HTTPS, systemd keeps the service running, and GitHub Actions deploys updates. Railway and Termius are not needed after the one-time installation.

1. Point a hostname to the Droplet's static IP. A dedicated subdomain is recommended; an `IP_ADDRESS.sslip.io` hostname can avoid buying a domain initially.
2. Clone the repository once on the Droplet and run `sudo ./deploy/bootstrap.sh bot.example.com`.
3. Edit `/etc/drojun.env`, replace every `CHANGE_ME`, then restart with `sudo systemctl restart drojun`.
4. Set the Kite redirect URL to `https://bot.example.com/kite/callback`.
5. In GitHub Actions secrets, add `DEPLOY_HOST`, `DEPLOY_USER`, and `DEPLOY_SSH_KEY`. The deploy user needs permission to run `/usr/local/sbin/drojun-deploy` with sudo.
6. Open the app root URL and complete Zerodha login each trading day.
7. Add `tradingview/Drojun_CCI.pine` to the one-hour NIFTY chart, set the same webhook secret, and create an alert using **Any alert() function call** with webhook URL `https://bot.example.com/webhook`.
8. Keep live trading disabled until the deployment and broker flow have been reviewed with a paper or minimal-risk run.

TradingView copies the script and inputs into an alert. After changing CCI length, delete and recreate the alert so the new value is active.

## Operations

- `GET /health` reports service, trading-switch, and daily Kite-login status without exposing secrets.
- The root control panel uses HTTP Basic authentication.
- Webhooks require the shared secret in the JSON body and are deduplicated by signal, bar close, and CCI length.
- Signals are ignored while another signal or position is active and expire after five minutes.
- Settings, signals, token state, and broker trigger IDs persist in SQLite on the Droplet.
- Entry orders that remain unfilled for 30 seconds are cancelled.
- Stop and target use Kite's native two-leg GTT OCO trigger, so the protection persists overnight and Kite cancels the unused leg.

Kite access tokens expire the next morning. The service remains online continuously, but Zerodha login must still be completed each trading day.
