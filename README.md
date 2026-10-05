# Paytm PG checkout page

A checkout page (Flask) wired for **Paytm JS Checkout**. Runs in **mock mode** until you add Paytm credentials.

## Run locally

```bash
cd /Users/shreyCode/paytmPgTest
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # first time only
cp .env.example .env                                                 # first time only
.venv/bin/python app.py
```

Open http://localhost:5000

To pick a settings file without renaming anything, set `ENV_FILE`:

```bash
ENV_FILE=.env.example .venv/bin/python app.py   # mock mode, no Paytm
ENV_FILE=.env.staging .venv/bin/python app.py   # Paytm test keys
```

A `.env` with `PAYTM_ENV=PRODUCTION` won't start on localhost: production needs an `https://` `BASE_URL`.

## Connect Paytm

1. In the Paytm Business dashboard, open **Developer Settings → API Keys** and copy the **Test** MID and Merchant Key.
2. Put them in `.env` (`PAYTM_MID`, `PAYTM_MERCHANT_KEY`), keep `PAYTM_ENV=STAGING`, `PAYTM_WEBSITE=WEBSTAGING`, `PAYTM_INDUSTRY_TYPE_ID=Retail`, and `PAYTM_CHANNEL_ID=WEB`.
3. Restart the app. The mock banner disappears, and "Pay" opens the real Paytm checkout (use Paytm's test cards/wallet credentials).

For live payments: switch to the **Production** keys, `PAYTM_ENV=PRODUCTION`, `PAYTM_WEBSITE=DEFAULT`, and set `BASE_URL` to your public `https://` domain.

## How it works

| Step | Where |
|---|---|
| Customer enters amount + details | `templates/index.html`, `static/checkout.js` |
| Server creates order, signs request, calls Paytm *Initiate Transaction* → `txnToken` | `POST /api/initiate` |
| Browser opens Paytm JS Checkout with the token | `static/checkout.js` |
| Paytm posts result → checksum verified → confirmed with *Transaction Status API* | `POST /payment/callback` |
| Customer sees the result | `GET /payment/result/<order_id>` |

The merchant key only lives on the server (`.env`). Orders are stored in `orders.db` (SQLite).

## Auto-debit subscriptions

`/subscribe` creates a Paytm Native Subscription mandate. The customer approves UPI Autopay at signup; no subscription debit is scheduled for that day. The first debit is scheduled seven calendar days later (configurable with `TRIAL_DAYS`), then at the selected plan's monthly or yearly interval.

| Step | Where |
|---|---|
| Customer picks a plan (defined in `PLANS` in `app.py`) | `templates/subscribe.html` |
| Server calls *Initiate Subscription* (`/subscription/create`, FIX amount, future first-debit date) → `txnToken` + `subscriptionId` | `POST /api/subscribe` |
| Customer approves the mandate in JS Checkout; this is not a subscription debit | `static/checkout.js` |
| Paytm posts back; the server confirms mandate state with *Fetch Subscription Status* | `POST /subscription/callback` |
| Customer can view payments and cancel (*Cancel Subscription* API) | `GET /subscription/<token>` |
| Hourly billing job sends Paytm's pre-debit notification, submits due debits, confirms each one, and refreshes mandate state | `GET /cron/sync` hourly from GitHub Actions (`.github/workflows/billing.yml`), daily from Vercel Cron as a backup, or `python -m flask --app app sync` |
| Webhooks can provide additional payment and mandate updates | `POST /webhooks/paytm` |

The recurring lifecycle is merchant-scheduled: Paytm requires a successful *Pre-Notify* before each *Renew* request. At signup the customer pays a ₹1 mandate charge (`MANDATE_AMOUNT`) while approving UPI AutoPay; the pre-debit notice for the plan fee is sent immediately, and the plan fee is debited once the notice is 24 hours old (`PRE_NOTIFY_HOURS`) on or after the due date (`FIRST_DEBIT_DAYS` after signup). Later cycles are notified 2 days ahead and debited on the due date. Paytm's Renew API only accepts the request; the debit is confirmed with *Transaction Status* on later runs or by the webhook, never submitted twice. Vercel's Hobby plan only allows a daily cron, so GitHub Actions calls `/cron/sync` hourly with the same `CRON_SECRET` (stored as a repository secret). Monitor the `errors` count and `Subscription billing issue` log lines.

### Before going live with subscriptions

1. Ask Paytm (Business dashboard → support, or your account manager) to **enable Native Subscriptions and UPI Autopay** on the MID. This app defaults to UPI and does not enable cards, wallets, or eNACH by itself.
2. Ask Paytm to set the **payment webhook** and the **subscription status webhook** to `https://<your-domain>/webhooks/paytm`. Without them the app won't hear about renewals or customer cancellations until the daily sync runs.
3. Set the same `CRON_SECRET` in Vercel and as a GitHub repository secret so both the daily Vercel cron and the hourly GitHub Actions job can call `/cron/sync`.
4. Keep plan amounts at or below ₹15,000. Above that, RBI requires the customer to authorise every debit (eNACH allows more).
5. The manage page is protected only by the secret link in its URL. In a real product, show subscriptions behind your own user login and email the customer a link.
6. On staging, verify mandate approval, the first debit date after the trial, pre-notification, renewal confirmation, and cancelling from both your page and the UPI app.

## Deploying to Vercel

Vercel runs `app.py` directly (it exports the Flask `app`). Its filesystem is not persistent, so orders go to Postgres there; the app refuses to start on Vercel without `DATABASE_URL`/`POSTGRES_URL`.

1. Push this folder to a GitHub repo (`.env`, `.venv` and `orders.db` are git-ignored).
2. In Vercel: **Add New → Project → Import** the repo. Framework preset: *Other* / auto-detected Flask.
3. **Storage → Create Database → Neon (Postgres)** and connect it to the project. This sets `DATABASE_URL` automatically.
4. **Settings → Environment Variables**, add:
   - `PAYTM_MID`, `PAYTM_MERCHANT_KEY`
   - `PAYTM_ENV` (`STAGING` or `PRODUCTION`), `PAYTM_WEBSITE` (`WEBSTAGING` or `DEFAULT`), `PAYTM_INDUSTRY_TYPE_ID` (for example `Retail`), and `PAYTM_CHANNEL_ID` (`WEB` for websites)
   - `PAYTM_CLIENT_ID` (confirm the production value with Paytm; `C11` is only a common default)
   - `PAYTM_SUBSCRIPTION_PAYMENT_MODE=UPI` and `TRIAL_DAYS=7`
   - `MERCHANT_NAME`
   - `BASE_URL` only if you use a custom domain. Otherwise it defaults to `https://<project>.vercel.app`.
5. Redeploy. The table is created automatically on first start.

### Going live (one-time payments)

1. Finish activation/KYC in the Paytm Business dashboard and register your website URL there.
2. Copy the **Production** MID and Merchant Key into the Vercel environment variables, with `PAYTM_ENV=PRODUCTION` and `PAYTM_WEBSITE=DEFAULT` (or the website name Paytm gives you).
3. Redeploy and make a small real payment to confirm.

## Production checklist

The app refuses to start with `PAYTM_ENV=PRODUCTION` unless real credentials are set and `BASE_URL` is `https://`. Mock mode only exists on staging.

**Vercel environment variables** (Settings → Environment Variables, *Production* scope):

| Variable | Value |
|---|---|
| `PAYTM_MID`, `PAYTM_MERCHANT_KEY` | Production keys from Paytm Business → Developer Settings → API Keys |
| `PAYTM_ENV` | `PRODUCTION` |
| `PAYTM_WEBSITE` | `DEFAULT` |
| `PAYTM_INDUSTRY_TYPE_ID` | as shown in the dashboard (for example `Retail109`) |
| `PAYTM_CHANNEL_ID` | `WEB` |
| `PAYTM_CLIENT_ID` | Confirm with Paytm; commonly `C11` |
| `PAYTM_SUBSCRIPTION_PAYMENT_MODE` | `UPI` (must be enabled on the MID) |
| `TRIAL_DAYS` | `7` |
| `BASE_URL` | your live `https://` domain, exactly as registered with Paytm |
| `MERCHANT_NAME` | your business name |
| `CRON_SECRET` | a long random string (`python3 -c "import secrets;print(secrets.token_urlsafe(32))"`); Vercel sends it as a Bearer token |
| `DATABASE_URL` | set automatically when you connect Neon Postgres |

**With Paytm:**
1. Account activated (KYC done) and your website domain approved in the dashboard.
2. Native Subscriptions and UPI Autopay enabled on the MID (only needed for `/subscribe`).
3. Payment webhook and subscription status webhook both set to `https://<your-domain>/webhooks/paytm`.

**Before announcing it:**
1. `GET https://<your-domain>/healthz` returns `{"ok": true, "env": "PRODUCTION", "mock": false}`.
2. Make a small one-time payment, then refund it from the Paytm dashboard.
3. Start a subscription on the cheapest plan, confirm no subscription debit is scheduled for signup, verify the first due date is seven days later, then cancel it.
4. In Vercel → Settings → Cron Jobs, run `/cron/sync` and verify pre-notification/renewal status and `"errors": 0`.

**What the app already does for you:**
- Every payment result is re-confirmed with Paytm server-to-server, and the paid amount is compared with the order. The browser and the callback are never trusted alone.
- Checksums on one-time callbacks and server-to-server webhooks are verified. Subscription redirects are confirmed against Paytm server-to-server because Paytm may omit their checksum.
- Cross-site POSTs to the payment and cancel endpoints are refused, and pages send security headers (HSTS, no framing, no-store, no path in referrers).
- Pending one-time and renewal payments are re-checked when Paytm's webhook arrives and by the daily sync.
- `.env*` files are excluded from git and from `vercel deploy`.

**Hosting somewhere other than Vercel:** run `gunicorn -w 2 -b 0.0.0.0:8000 app:app` behind an HTTPS reverse proxy, set `DATABASE_URL` to Postgres, and run `python -m flask --app app sync` daily from cron.

**Still your call before scaling up:** add customer accounts (the subscription page is protected only by its secret link), email receipts, rate limiting on `/api/*` (for example at Cloudflare or Vercel Firewall), and monitoring/alerts on `AMOUNT_MISMATCH` log errors.
