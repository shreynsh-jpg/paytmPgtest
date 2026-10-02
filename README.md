# Paytm PG checkout page

A checkout page (Flask) wired for **Paytm JS Checkout**. Runs in **mock mode** until you add Paytm credentials.

## Run locally

```bash
cd /Users/shreyCode/webpage
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # first time only
cp .env.example .env                                                 # first time only
.venv/bin/python app.py
```

Open http://localhost:5000

## Connect Paytm

1. In the Paytm Business dashboard, open **Developer Settings → API Keys** and copy the **Test** MID and Merchant Key.
2. Put them in `.env` (`PAYTM_MID`, `PAYTM_MERCHANT_KEY`), keep `PAYTM_ENV=STAGING`, `PAYTM_WEBSITE=WEBSTAGING`.
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

## Deploying to Vercel

Vercel runs `app.py` directly (it exports the Flask `app`). Its filesystem is not persistent, so orders go to Postgres there; the app refuses to start on Vercel without `DATABASE_URL`/`POSTGRES_URL`.

1. Push this folder to a GitHub repo (`.env`, `.venv` and `orders.db` are git-ignored).
2. In Vercel: **Add New → Project → Import** the repo. Framework preset: *Other* / auto-detected Flask.
3. **Storage → Create Database → Neon (Postgres)** and connect it to the project. This sets `DATABASE_URL` automatically.
4. **Settings → Environment Variables**, add:
   - `PAYTM_MID`, `PAYTM_MERCHANT_KEY`
   - `PAYTM_ENV` (`STAGING` or `PRODUCTION`), `PAYTM_WEBSITE` (`WEBSTAGING` or `DEFAULT`)
   - `MERCHANT_NAME`
   - `BASE_URL` only if you use a custom domain. Otherwise it defaults to `https://<project>.vercel.app`.
5. Redeploy. The table is created automatically on first start.

### Going live

1. Finish activation/KYC in the Paytm Business dashboard and register your website URL there.
2. Copy the **Production** MID and Merchant Key into the Vercel environment variables, with `PAYTM_ENV=PRODUCTION` and `PAYTM_WEBSITE=DEFAULT` (or the website name Paytm gives you).
3. Redeploy and make a small real payment to confirm.
