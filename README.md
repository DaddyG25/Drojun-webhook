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

## Managed deployment

The `.do/app.yaml` specification runs the app on DigitalOcean App Platform with PostgreSQL and dedicated egress IPs. It removes the Droplet, Termius, SFTP, and Railway-to-VPS forwarding path.

After creating the app:

1. Add `FLASK_SECRET_KEY`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `WEBHOOK_SECRET`, `KITE_API_KEY`, `KITE_API_SECRET`, and `KITE_USER_ID` as encrypted run-time variables.
2. Copy both dedicated egress IPs from the app Networking page and whitelist them in Kite Connect.
3. Set the Kite redirect URL to `https://YOUR_APP_DOMAIN/kite/callback`.
4. Open the app root URL and complete Zerodha login each trading day.
5. Add `tradingview/Drojun_CCI.pine` to the one-hour NIFTY chart, set the same webhook secret, and create an alert using **Any alert() function call** with webhook URL `https://YOUR_APP_DOMAIN/webhook`.
6. Keep live trading disabled until the deployment and broker flow have been reviewed with a paper or minimal-risk run.

TradingView copies the script and inputs into an alert. After changing CCI length, delete and recreate the alert so the new value is active.

## Operations

- `GET /health` reports service, trading-switch, and daily Kite-login status without exposing secrets.
- The root control panel uses HTTP Basic authentication.
- Webhooks require the shared secret in the JSON body and are deduplicated by signal, bar close, and CCI length.
- Signals are ignored while another signal or position is active and expire after five minutes.
- Settings, signals, token state, and order IDs persist in PostgreSQL.
- Entry orders that remain unfilled for 30 seconds are cancelled.
- Stop and target orders are tracked by their broker order IDs; when one fills, the sibling is cancelled.

Kite access tokens expire the next morning. The service remains online continuously, but Zerodha login must still be completed each trading day.
