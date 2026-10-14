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

The CCI length, delta, stop, target, quantity, and execution mode are editable on the password-protected control panel. Execution can be disabled, paper, live, or paper journal plus live.

## Affordable deployment

Railway remains the public HTTPS gateway. The existing DigitalOcean Droplet is the private trading executor and supplies the required static outbound IP. Requests between them are signed with a shared secret. SQLite stores durable state, systemd keeps the executor running, Nginx provides HTTPS, and GitHub Actions deploys updates. Termius is not needed after the one-time installation.

1. Point a hostname to the Droplet's static IP. An `IP_ADDRESS.sslip.io` hostname can avoid buying a domain initially.
2. Clone the repository once on the Droplet and run `sudo ./deploy/bootstrap.sh IP_ADDRESS.sslip.io`.
3. Edit `/etc/drojun.env`, replace every `CHANGE_ME`, and copy its generated `GATEWAY_SHARED_SECRET`. Restart with `sudo systemctl restart drojun`.
4. On Railway, set `APP_ROLE=gateway`, `EXECUTOR_BASE_URL=https://IP_ADDRESS.sslip.io`, and the same `GATEWAY_SHARED_SECRET`.
5. Create a free **Personal** Kite app and point its redirect URL at `https://YOUR_RAILWAY_DOMAIN/kite/callback`. Drojun does not need the paid historical data or live quote subscription.
6. In GitHub Actions secrets, add `DEPLOY_HOST`, `DEPLOY_USER`, and `DEPLOY_SSH_KEY`. The deploy user needs permission to run `/usr/local/sbin/drojun-deploy` with sudo.
7. Open the Railway URL and complete Zerodha login each trading day.
8. Add `tradingview/Drojun_CCI.pine` to the one-hour NIFTY chart, set the same webhook secret, and create an alert using **Any alert() function call** with webhook URL `https://YOUR_RAILWAY_DOMAIN/webhook`.
9. Keep live trading disabled until the gateway, executor, and broker flow have been reviewed with a controlled run.

TradingView copies the script and inputs into an alert. After changing CCI length, delete and recreate the alert so the new value is active.

## Operations

- `GET /health` reports service, execution-mode, and daily Kite-login status without exposing secrets.
- `GET /broker/status` provides an authenticated view of nonzero positions, open orders, and active GTT triggers for preflight checks.
- `GET /journal.csv` downloads the authenticated paper-trade journal.
- The root control panel uses HTTP Basic authentication.
- Kite app credentials can be replaced from the control panel and are saved with service-only permissions on the Droplet, so routine changes do not require Termius.
- Webhooks require the shared secret in the JSON body and are deduplicated by signal, bar close, and CCI length.
- Railway signs every forwarded request; the executor rejects unsigned direct requests.
- Signals are ignored while another signal or position is active and expire after five minutes.
- Settings, signals, token state, and broker trigger IDs persist in SQLite on the Droplet.
- Contract delta is estimated from the TradingView spot price with `ESTIMATED_IV` (default `0.18`), avoiding the paid live quote API. The minimum delta remains editable on the control panel.
- Paper mode never submits broker orders. It records the selected contract, estimated entry premium, stop, target, and quantity in the journal. Hourly TradingView bar updates mark open paper trades using the same Black-Scholes estimate and record stop, target, expiry, and estimated P&L outcomes. Combined paper-and-live mode keeps the same journal while also using the real broker order path.
- Entry orders are placed at market and the actual fill price is used for the stop and target.
- Stop and target use Kite's native two-leg GTT OCO trigger, so the protection persists overnight and Kite cancels the unused leg.

Kite access tokens expire the next morning. The service remains online continuously, but Zerodha login must still be completed each trading day.

