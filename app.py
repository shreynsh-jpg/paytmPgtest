"""Checkout page + Paytm Payment Gateway (JS Checkout) backend.

Flow:
  1. Browser posts amount + customer details to /api/initiate.
  2. Server creates an order, signs the request with the merchant key and calls
     Paytm's Initiate Transaction API, then returns the txnToken to the browser.
  3. Browser opens Paytm JS Checkout with that token; the customer pays.
  4. Paytm POSTs the result to /payment/callback. The server verifies the
     checksum, re-checks the order with the Transaction Status API, and stores
     the final status.
  5. Customer lands on /payment/result/<order_id>.

Auto-debit (Paytm Subscriptions) works the same way through /api/subscribe and
/subscription/callback: the customer pays a ₹1 mandate charge, the pre-debit notice
goes out at once, and an hourly job debits the plan fee 24 hours later and each
renewal. See the "subscriptions" sections below.

With PAYTM_MID unset the app runs in MOCK mode so the page can be tested
without credentials.
"""

import calendar
import functools
import hashlib
import hmac
import json
import logging
import os
import secrets
import re
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, abort, g, jsonify, redirect, render_template, request, url_for
from paytmchecksum import PaytmChecksum

# ENV_FILE picks the settings file, e.g. ENV_FILE=.env.staging for Paytm test keys locally.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.getenv("ENV_FILE", ".env")))

PAYTM_MID = os.getenv("PAYTM_MID", "").strip()
PAYTM_MERCHANT_KEY = os.getenv("PAYTM_MERCHANT_KEY", "").strip()
PAYTM_ENV = os.getenv("PAYTM_ENV", "STAGING").strip().upper()
PAYTM_WEBSITE = os.getenv("PAYTM_WEBSITE", "WEBSTAGING").strip()
PAYTM_INDUSTRY_TYPE_ID = os.getenv("PAYTM_INDUSTRY_TYPE_ID", "Retail").strip()
PAYTM_CHANNEL_ID = os.getenv("PAYTM_CHANNEL_ID", "WEB").strip()
PAYTM_CLIENT_ID = os.getenv("PAYTM_CLIENT_ID", "C11").strip()
PAYTM_SUBSCRIPTION_PAYMENT_MODE = os.getenv("PAYTM_SUBSCRIPTION_PAYMENT_MODE", "UPI").strip().upper()
# Where Paytm sends the payment result. On Vercel this defaults to the project's production URL.
BASE_URL = (
    os.getenv("BASE_URL")
    or ("https://" + os.environ["VERCEL_PROJECT_PRODUCTION_URL"] if os.getenv("VERCEL_PROJECT_PRODUCTION_URL") else "")
    or "http://localhost:5000"
).rstrip("/")
MERCHANT_NAME = os.getenv("MERCHANT_NAME", "My Store")

# Paytm moved its gateway to paytmpayments.com; newer MIDs only work there.
PAYTM_HOST = os.getenv("PAYTM_HOST", "").strip().rstrip("/") or (
    "https://secure.paytmpayments.com" if PAYTM_ENV == "PRODUCTION" else "https://securestage.paytmpayments.com"
)
MOCK_MODE = not (PAYTM_MID and PAYTM_MERCHANT_KEY)
# Vercel Cron sends "Authorization: Bearer $CRON_SECRET" to /cron/sync.
CRON_SECRET = os.getenv("CRON_SECRET", "").strip()

# Fail fast on a live configuration that would lose payments or fake them.
if PAYTM_ENV == "PRODUCTION":
    if MOCK_MODE:
        raise RuntimeError("PAYTM_ENV=PRODUCTION needs PAYTM_MID and PAYTM_MERCHANT_KEY; mock mode is staging-only.")
    if not BASE_URL.startswith("https://"):
        raise RuntimeError(
            f"BASE_URL is {BASE_URL!r}, but in production Paytm must post results to your public https:// domain. "
            "Set BASE_URL (for a local test, expose the app with a tunnel such as ngrok and use its https URL)."
        )

MIN_AMOUNT = Decimal("1.00")
MAX_AMOUNT = Decimal("100000.00")

# Auto-debit plans. At signup the customer pays MANDATE_AMOUNT while approving the UPI AutoPay
# mandate. The plan amount is then debited FIRST_DEBIT_DAYS later (once the pre-debit notice is
# PRE_NOTIFY_HOURS old) and again every billing period, by the hourly billing job.
# Keep amounts at or below ₹15,000: above that RBI requires the customer to authorise each debit.
PLANS = {
    "monthly": {"name": "Monthly", "amount": "99.00", "unit": "MONTH", "per": "month"},
    "yearly": {"name": "Yearly", "amount": "999.00", "unit": "YEAR", "per": "year"},
}
MANDATE_AMOUNT = os.getenv("MANDATE_AMOUNT", "1.00")
FIRST_DEBIT_DAYS = int(os.getenv("FIRST_DEBIT_DAYS", "1"))
# NPCI requires the pre-debit notice at least 24 hours before a UPI AutoPay debit.
PRE_NOTIFY_HOURS = int(os.getenv("PRE_NOTIFY_HOURS", "24"))
SUBSCRIPTION_YEARS = int(os.getenv("SUBSCRIPTION_YEARS", "5"))
SUBSCRIPTION_GRACE_DAYS = "3"  # cards allow at most 3
IST = timezone(timedelta(hours=5, minutes=30))

# Postgres when DATABASE_URL is set (required on Vercel, whose filesystem is not persistent),
# otherwise a local SQLite file for development.
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.db")
if os.getenv("VERCEL") and not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Add a Postgres database (e.g. Neon) to the Vercel project.")

# Vercel only serves static assets from public/ (Flask's own static/ folder is not
# deployed there), so keep them in public/static: served at /static/* both locally
# by Flask and on Vercel by its CDN.
app = Flask(__name__, static_folder="public/static", static_url_path="/static")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app.logger.setLevel(logging.INFO)


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    # Send only our origin to other sites, never the path: /subscription/<token> is a secret link.
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if BASE_URL.startswith("https://"):
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if request.endpoint not in ("static", None):
        resp.headers.setdefault("Cache-Control", "no-store")  # pages show live payment state
    return resp


def same_origin(view):
    """Reject cross-site POSTs (CSRF) to endpoints the browser calls on our own pages.
    Paytm's callbacks and webhooks come from Paytm, so they rely on the checksum instead."""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if origin and urlparse(origin).netloc != request.host:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


# --- storage ---------------------------------------------------------------

def connect():
    if DATABASE_URL:
        import psycopg2
        import psycopg2.extras

        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def db_execute(sql, params=(), db=None):
    """Run `sql` (written with ? placeholders) on SQLite or Postgres and commit."""
    db = db or get_db()
    if DATABASE_URL:
        sql = sql.replace("?", "%s")
    cur = db.cursor()
    cur.execute(sql, params)
    db.commit()
    return cur


def init_db():
    db = connect()
    try:
        db_execute(
            """CREATE TABLE IF NOT EXISTS orders (
                order_id   TEXT PRIMARY KEY,
                amount     TEXT NOT NULL,
                name       TEXT,
                email      TEXT,
                phone      TEXT,
                status     TEXT NOT NULL DEFAULT 'CREATED',
                txn_id     TEXT,
                resp_msg   TEXT,
                raw        TEXT,
                created_at BIGINT NOT NULL,
                updated_at BIGINT NOT NULL
            )""",
            db=db,
        )
        db_execute(
            """CREATE TABLE IF NOT EXISTS subscriptions (
                order_id       TEXT PRIMARY KEY,
                subs_id        TEXT UNIQUE,
                manage_token   TEXT NOT NULL UNIQUE,
                plan           TEXT NOT NULL,
                amount         TEXT NOT NULL,
                frequency_unit TEXT NOT NULL,
                name           TEXT,
                email          TEXT,
                phone          TEXT,
                status         TEXT NOT NULL DEFAULT 'CREATED',
                sub_status     TEXT,
                pay_mode       TEXT,
                start_date     TEXT,
                expiry_date    TEXT,
                resp_msg       TEXT,
                raw            TEXT,
                next_due_date  TEXT,
                pre_notified_date TEXT,
                pre_notified_at BIGINT,
                created_at     BIGINT NOT NULL,
                updated_at     BIGINT NOT NULL
            )""",
            db=db,
        )
        if DATABASE_URL:
            db_execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS next_due_date TEXT", db=db)
            db_execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS pre_notified_date TEXT", db=db)
            db_execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS pre_notified_at BIGINT", db=db)
        else:
            columns = {row["name"] for row in db_execute("PRAGMA table_info(subscriptions)", db=db).fetchall()}
            if "next_due_date" not in columns:
                db_execute("ALTER TABLE subscriptions ADD COLUMN next_due_date TEXT", db=db)
            if "pre_notified_date" not in columns:
                db_execute("ALTER TABLE subscriptions ADD COLUMN pre_notified_date TEXT", db=db)
            if "pre_notified_at" not in columns:
                db_execute("ALTER TABLE subscriptions ADD COLUMN pre_notified_at BIGINT", db=db)
        db_execute(
            """CREATE TABLE IF NOT EXISTS subscription_payments (
                order_id   TEXT PRIMARY KEY,
                subs_id    TEXT NOT NULL,
                amount     TEXT,
                status     TEXT NOT NULL,
                txn_id     TEXT,
                resp_msg   TEXT,
                raw        TEXT,
                created_at BIGINT NOT NULL,
                updated_at BIGINT NOT NULL
            )""",
            db=db,
        )
        db_execute("CREATE INDEX IF NOT EXISTS subscription_payments_subs_id ON subscription_payments (subs_id)", db=db)
        db_execute("CREATE INDEX IF NOT EXISTS orders_status ON orders (status)", db=db)
    finally:
        db.close()


def get_order(order_id):
    return db_execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()


def create_order(order_id, amount, name, email, phone):
    now = int(time.time())
    db_execute(
        "INSERT INTO orders (order_id, amount, name, email, phone, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (order_id, amount, name, email, phone, now, now),
    )


def update_order(order_id, status, txn_id=None, resp_msg=None, raw=None):
    db_execute(
        "UPDATE orders SET status=?, txn_id=?, resp_msg=?, raw=?, updated_at=? WHERE order_id=?",
        (status, txn_id, resp_msg, json.dumps(raw) if raw else None, int(time.time()), order_id),
    )


def get_subscription(order_id=None, subs_id=None, manage_token=None):
    column, value = next((c, v) for c, v in
                         (("order_id", order_id), ("subs_id", subs_id), ("manage_token", manage_token)) if v)
    return db_execute(f"SELECT * FROM subscriptions WHERE {column}=?", (value,)).fetchone()


def create_subscription(order_id, plan_id, name, email, phone, first_debit_date, expiry_date):
    plan = PLANS[plan_id]
    now = int(time.time())
    db_execute(
        """INSERT INTO subscriptions (order_id, manage_token, plan, amount, frequency_unit, name, email, phone,
                                      start_date, expiry_date, next_due_date, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (order_id, secrets.token_urlsafe(24), plan_id, plan["amount"], plan["unit"], name, email, phone,
         first_debit_date.isoformat(), expiry_date.isoformat(), first_debit_date.isoformat(), now, now),
    )


def update_subscription(order_id, **fields):
    if "raw" in fields:
        fields["raw"] = json.dumps(fields["raw"]) if fields["raw"] else None
    fields["updated_at"] = int(time.time())
    columns = ", ".join(f"{k}=?" for k in fields)
    db_execute(f"UPDATE subscriptions SET {columns} WHERE order_id=?", (*fields.values(), order_id))


def save_subscription_payment(order_id, subs_id, amount, status, txn_id=None, resp_msg=None, raw=None):
    """Insert or update one confirmed or pending subscription renewal."""
    now = int(time.time())
    db_execute(
        """INSERT INTO subscription_payments (order_id, subs_id, amount, status, txn_id, resp_msg, raw,
                                              created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT (order_id) DO UPDATE SET status=excluded.status, txn_id=excluded.txn_id,
               resp_msg=excluded.resp_msg, raw=excluded.raw, updated_at=excluded.updated_at""",
        (order_id, subs_id, amount, status, txn_id, resp_msg, json.dumps(raw) if raw else None, now, now),
    )


def get_subscription_payment(order_id):
    return db_execute("SELECT * FROM subscription_payments WHERE order_id=?", (order_id,)).fetchone()


def list_subscription_payments(subs_id):
    return db_execute(
        "SELECT * FROM subscription_payments WHERE subs_id=? ORDER BY created_at DESC", (subs_id,)
    ).fetchall()


# --- Paytm API helpers -----------------------------------------------------

def paytm_post(path, body, head=None):
    """Sign `body` and POST it to a Paytm JSON API."""
    body_str = json.dumps(body, separators=(",", ":"))
    signature = PaytmChecksum.generateSignature(body_str, PAYTM_MERCHANT_KEY)
    head = {**(head or {}), "signature": signature}
    payload = '{"body":%s,"head":%s}' % (body_str, json.dumps(head, separators=(",", ":")))
    resp = requests.post(
        PAYTM_HOST + path,
        data=payload,
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def paytm_initiate(order_id, amount, cust_id):
    body = {
        "requestType": "Payment",
        "mid": PAYTM_MID,
        "websiteName": PAYTM_WEBSITE,
        "industryTypeId": PAYTM_INDUSTRY_TYPE_ID,
        "channelId": PAYTM_CHANNEL_ID,
        "orderId": order_id,
        "callbackUrl": BASE_URL + url_for("payment_callback"),
        "txnAmount": {"value": amount, "currency": "INR"},
        "userInfo": {"custId": cust_id},
    }
    return paytm_post(f"/theia/api/v1/initiateTransaction?mid={PAYTM_MID}&orderId={order_id}", body)


def checksum_ok(params, checksum):
    """Verify a CHECKSUMHASH Paytm posted to us; a malformed one counts as a mismatch."""
    try:
        return bool(checksum) and PaytmChecksum.verifySignature(params, PAYTM_MERCHANT_KEY, checksum)
    except ValueError:
        return False


def paytm_status(order_id):
    return paytm_post("/v3/order/status", {"mid": PAYTM_MID, "orderId": order_id})


def txn_state(status_body):
    """Map a Transaction Status API body to SUCCESS / FAILED / PENDING."""
    return {"TXN_SUCCESS": "SUCCESS", "TXN_FAILURE": "FAILED"}.get(
        status_body.get("resultInfo", {}).get("resultStatus"), "PENDING"
    )


# --- Paytm subscription (auto-debit) API helpers ----------------------------

def add_months(d, months):
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def add_period(d, unit):
    if unit == "WEEK":
        return d + timedelta(days=7)
    return add_months(d, {"MONTH": 1, "BI_MONTHLY": 2, "QUARTER": 3, "SEMI_ANNUALLY": 6, "YEAR": 12}[unit])


def paytm_create_subscription(order_id, plan_id, cust_id, name, email, phone, first_debit_date, expiry_date):
    """Create a fixed-amount UPI AutoPay mandate: MANDATE_AMOUNT is paid now, the plan amount on each debit."""
    plan = PLANS[plan_id]
    body = {
        "requestType": "NATIVE_SUBSCRIPTION",
        "mid": PAYTM_MID,
        "websiteName": PAYTM_WEBSITE,
        "orderId": order_id,
        "callbackUrl": BASE_URL + url_for("subscription_callback"),
        "txnAmount": {"value": MANDATE_AMOUNT, "currency": "INR"},
        "userInfo": {"custId": cust_id, "mobile": phone, "email": email, "firstName": name},
        "subscriptionAmountType": "FIX",
        # Without renewalAmount Paytm uses txnAmount (₹1) for every renewal.
        "renewalAmount": plan["amount"],
        "subscriptionFrequency": "1",
        "subscriptionFrequencyUnit": plan["unit"],
        "subscriptionPaymentMode": PAYTM_SUBSCRIPTION_PAYMENT_MODE,
        "subscriptionStartDate": first_debit_date.isoformat(),
        "subscriptionGraceDays": SUBSCRIPTION_GRACE_DAYS,
        "subscriptionExpiryDate": expiry_date.isoformat(),
        "subscriptionEnableRetry": "0",
    }
    path = "/theia/api/v1/subscription/create" if PAYTM_ENV == "PRODUCTION" else "/subscription/create"
    trace_id = uuid.uuid4().hex
    query = urlencode({"mid": PAYTM_MID, "orderId": order_id, "traceId": trace_id})
    return paytm_post(
        f"{path}?{query}",
        body,
        {"clientId": PAYTM_CLIENT_ID, "channelId": PAYTM_CHANNEL_ID},
    )


def paytm_subscription_status(subs_id=None, order_id=None, cust_id=None):
    body = {"mid": PAYTM_MID}
    if subs_id:
        body["subsId"] = subs_id
    if order_id:
        body["orderId"] = order_id
    if cust_id:
        body["custId"] = cust_id
    return paytm_post("/subscription/checkStatus", body, {"tokenType": "AES"})


def paytm_cancel_subscription(subs_id):
    body = {"mid": PAYTM_MID, "subscriptionId": subs_id, "subsId": subs_id}
    query = urlencode({"mid": PAYTM_MID, "subscriptionId": subs_id})
    return paytm_post(f"/subscription/cancel?{query}", body, {"tokenType": "AES"})


def paytm_subscription_pre_notify(sub, due_date):
    """Ask Paytm to send the required pre-debit notice before a scheduled renewal."""
    subs_id = sub["subs_id"]
    suffix = hashlib.sha256(subs_id.encode()).hexdigest()[:12].upper()
    order_id = f"PN{suffix}{due_date:%Y%m%d}"
    body = {
        "mid": PAYTM_MID,
        "subscriptionId": subs_id,
        "subsId": subs_id,
        "referenceId": subs_id,
        "orderId": order_id,
        "txnAmount": sub["amount"],
        "txnDate": due_date.strftime("%d-%m-%Y"),  # "Date on which the debit is intended to happen"
        "txnMessage": f"{PLANS[sub['plan']]['name']} subscription renewal",
        "merchantName": MERCHANT_NAME,
        "merchantLogoUrl": "",
        "subscriptionScheduledExecutionDate": due_date.isoformat(),
    }
    return paytm_post("/subscription/preNotify", body, {"tokenType": "AES"})


def paytm_subscription_renew(sub, due_date, order_id):
    body = {
        "mid": PAYTM_MID,
        "subscriptionId": sub["subs_id"],
        "orderId": order_id,
        "txnAmount": {"value": sub["amount"], "currency": "INR"},
    }
    query = urlencode({"mid": PAYTM_MID, "orderId": order_id})
    return paytm_post(f"/subscription/renew?{query}", body)


def subscription_order_id(subs_id, due_date):
    suffix = hashlib.sha256(subs_id.encode()).hexdigest()[:12].upper()
    return f"RN{suffix}{due_date:%Y%m%d}"


def paytm_result_ok(info):
    return info.get("status") == "SUCCESS" or info.get("resultStatus") in ("SUCCESS", "S")


def refresh_subscription(sub):
    """Pull the mandate's current state from Paytm into the subscriptions table."""
    body = paytm_subscription_status(sub["subs_id"]).get("body", {})
    info = body.get("resultInfo", {})
    if not paytm_result_ok(info):
        app.logger.warning("Subscription status failed for %s: %s", sub["subs_id"], info)
        return False
    update_subscription(
        sub["order_id"],
        status=body.get("status") or sub["status"],
        sub_status=body.get("subStatus"),
        pay_mode=body.get("payMode"),
        resp_msg=info.get("resultMsg"),
        raw=body,
    )
    return True


def confirm_order(order):
    """Settle a one-time order from the Transaction Status API, the only source we trust."""
    body = paytm_status(order["order_id"]).get("body", {})
    state = txn_state(body)
    if state == "SUCCESS" and parse_amount(body.get("txnAmount")) != Decimal(order["amount"]):
        state = "AMOUNT_MISMATCH"
        app.logger.error("Amount mismatch on order %s: paid %s", order["order_id"], body.get("txnAmount"))
    update_order(order["order_id"], state, body.get("txnId"), body.get("resultInfo", {}).get("resultMsg"), body)
    return state


def confirm_subscription_payment(order_id, subs_id, expected_amount):
    """Record a debit only after confirming it with the Transaction Status API."""
    body = paytm_status(order_id).get("body", {})
    state = txn_state(body)
    if state == "SUCCESS" and parse_amount(body.get("txnAmount")) != Decimal(expected_amount):
        state = "AMOUNT_MISMATCH"
    save_subscription_payment(order_id, subs_id, body.get("txnAmount") or expected_amount, state,
                              body.get("txnId"), body.get("resultInfo", {}).get("resultMsg"), body)
    return state


def settle_renewal(sub, due_date):
    """Re-check the renewal for `due_date`; once paid, move the schedule to the next period.
    Paytm's Renew API only accepts the request: the UPI debit settles minutes to hours later."""
    renewal_id = subscription_order_id(sub["subs_id"], due_date)
    payment = get_subscription_payment(renewal_id)
    if payment is None:
        return None
    state = payment["status"]
    if state == "PENDING":
        state = confirm_subscription_payment(renewal_id, sub["subs_id"], sub["amount"])
    if state == "SUCCESS" and sub["next_due_date"] == due_date.isoformat():
        next_due_date = add_period(due_date, sub["frequency_unit"])
        update_subscription(sub["order_id"], next_due_date=next_due_date.isoformat(),
                            pre_notified_date=None, pre_notified_at=None)
    return state


def on_mandate_update(sub):
    """After the mandate's status is refreshed: record the ₹1 mandate charge and, once the mandate
    is active, send the first pre-debit notice immediately so the first debit can follow in 24 hours."""
    sub = get_subscription(order_id=sub["order_id"])
    if get_subscription_payment(sub["order_id"]) is None:
        body = paytm_status(sub["order_id"]).get("body", {})
        if txn_state(body) == "SUCCESS":
            confirm_subscription_payment(sub["order_id"], sub["subs_id"], MANDATE_AMOUNT)
    outcome = process_subscription_billing(sub)
    if outcome["error"]:
        app.logger.error("Subscription billing issue for %s: %s", sub["subs_id"], outcome["error"])


def process_subscription_billing(sub):
    """Send a pre-debit notice and confirm one scheduled renewal without duplicate charges."""
    if sub["status"] != "ACTIVE" or sub["sub_status"] != "ACTIVE":
        return {"pre_notification": False, "renewal": False, "error": None}
    if not sub["next_due_date"]:
        return {
            "pre_notification": False,
            "renewal": False,
            "error": "Active mandate has no saved next debit date; set its schedule before billing.",
        }

    due_date = date.fromisoformat(sub["next_due_date"])
    today = datetime.now(IST).date()
    days_until_due = (due_date - today).days

    # A renewal was already requested for this due date: settle it instead of charging again.
    existing = settle_renewal(sub, due_date)
    if existing == "SUCCESS":
        return {"pre_notification": False, "renewal": True, "error": None}
    if existing == "PENDING":
        return {"pre_notification": False, "renewal": False, "error": None}  # still settling; check next run
    if existing is not None:
        return {"pre_notification": False, "renewal": False,
                "error": f"Renewal for {due_date.isoformat()} was {existing.lower()}; inspect the mandate."}

    if days_until_due < -int(SUBSCRIPTION_GRACE_DAYS):
        return {"pre_notification": False, "renewal": False,
                "error": f"Missed renewal date {due_date.isoformat()}; inspect the mandate before billing again."}

    if sub["pre_notified_date"] != due_date.isoformat():
        if days_until_due > 2:
            return {"pre_notification": False, "renewal": False, "error": None}
        response = paytm_subscription_pre_notify(sub, due_date).get("body", {})
        info = response.get("resultInfo", {})
        if not paytm_result_ok(info):
            return {
                "pre_notification": False,
                "renewal": False,
                "error": info.get("message") or info.get("resultMsg") or "Paytm rejected the pre-debit notification.",
            }
        update_subscription(sub["order_id"], pre_notified_date=due_date.isoformat(), pre_notified_at=int(time.time()))
        return {"pre_notification": True, "renewal": False, "error": None}

    # Debit only on/after the due date and once the customer has had the notice for PRE_NOTIFY_HOURS.
    notice_age = time.time() - (sub["pre_notified_at"] or 0)
    if days_until_due > 0 or notice_age < PRE_NOTIFY_HOURS * 3600:
        return {"pre_notification": False, "renewal": False, "error": None}

    renewal_id = subscription_order_id(sub["subs_id"], due_date)
    payment = get_subscription_payment(renewal_id)
    if payment is None:
        save_subscription_payment(renewal_id, sub["subs_id"], sub["amount"], "PENDING",
                                  resp_msg="Renewal request submitted")
        response = paytm_subscription_renew(sub, due_date, renewal_id).get("body", {})
        info = response.get("resultInfo", {})
        if not paytm_result_ok(info):
            save_subscription_payment(renewal_id, sub["subs_id"], sub["amount"], "FAILED",
                                      resp_msg=info.get("resultMsg") or "Paytm rejected the renewal.",
                                      raw=response)
            return {"pre_notification": False, "renewal": False,
                    "error": info.get("resultMsg") or "Paytm rejected the renewal."}

    state = settle_renewal(sub, due_date)
    if state == "SUCCESS":
        return {"pre_notification": False, "renewal": True, "error": None}
    if state == "PENDING":
        return {"pre_notification": False, "renewal": False, "error": None}  # settles on a later run or webhook
    return {"pre_notification": False, "renewal": False,
            "error": f"Renewal {renewal_id} was {(state or 'unknown').lower()}."}


# --- validation ------------------------------------------------------------

def parse_amount(value):
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        return None
    if not (MIN_AMOUNT <= amount <= MAX_AMOUNT):
        return None
    return amount


def parse_customer(data):
    """Return ((name, email, phone), None) or (None, error message)."""
    name = (data.get("name") or "").strip()[:100]
    email = (data.get("email") or "").strip()[:150]
    phone = re.sub(r"\D", "", data.get("phone") or "")
    if not name:
        return None, "Name is required."
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return None, "Enter a valid email address."
    if not re.fullmatch(r"\d{10}", phone):
        return None, "Enter a 10-digit mobile number."
    return (name, email, phone), None


# --- routes ----------------------------------------------------------------

@app.get("/")
def index():
    return render_template(
        "index.html",
        merchant_name=MERCHANT_NAME,
        mock_mode=MOCK_MODE,
        paytm_env=PAYTM_ENV,
        checkout_js_url=f"{PAYTM_HOST}/merchantpgpui/checkoutjs/merchants/{PAYTM_MID}.js",
        min_amount=MIN_AMOUNT,
        max_amount=MAX_AMOUNT,
    )


@app.post("/api/initiate")
@same_origin
def api_initiate():
    data = request.get_json(silent=True) or {}
    amount = parse_amount(data.get("amount"))
    if amount is None:
        return jsonify(error=f"Enter an amount between ₹{MIN_AMOUNT} and ₹{MAX_AMOUNT}."), 400
    customer, error = parse_customer(data)
    if error:
        return jsonify(error=error), 400
    name, email, phone = customer

    order_id = "ORD" + time.strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:6].upper()
    create_order(order_id, str(amount), name, email, phone)

    if MOCK_MODE:
        return jsonify(mock=True, orderId=order_id, redirect=url_for("mock_gateway", order_id=order_id))

    try:
        result = paytm_initiate(order_id, str(amount), cust_id="CUST_" + phone)
    except requests.RequestException as exc:
        app.logger.exception("Paytm initiateTransaction failed")
        update_order(order_id, "INIT_FAILED", resp_msg=str(exc))
        return jsonify(error="Could not reach Paytm. Please try again."), 502

    info = result.get("body", {}).get("resultInfo", {})
    token = result.get("body", {}).get("txnToken")
    if info.get("resultStatus") != "S" or not token:
        update_order(order_id, "INIT_FAILED", resp_msg=info.get("resultMsg"), raw=result)
        return jsonify(error=info.get("resultMsg") or "Paytm rejected the request."), 502

    return jsonify(mock=False, orderId=order_id, txnToken=token, amount=str(amount))


@app.post("/payment/callback")
def payment_callback():
    """Paytm POSTs the transaction result here (form-encoded)."""
    params = request.form.to_dict()
    order_id = params.get("ORDERID")
    checksum = params.pop("CHECKSUMHASH", "")

    if not order_id or MOCK_MODE:
        abort(400)
    if not checksum_ok(params, checksum):
        app.logger.warning("Checksum mismatch for order %s", order_id)
        abort(400, "Checksum mismatch")

    order = get_order(order_id)
    if order is None:
        abort(404)

    # Never trust the browser-relayed callback alone: confirm with Paytm server-to-server.
    try:
        confirm_order(order)
    except requests.RequestException:
        app.logger.exception("Paytm order status failed")
        update_order(order_id, "PENDING", params.get("TXNID"), "Awaiting confirmation from Paytm", params)

    return redirect(url_for("payment_result", order_id=order_id), code=303)


@app.get("/payment/result/<order_id>")
def payment_result(order_id):
    order = get_order(order_id)
    if order is None:
        abort(404)
    # "Refresh status" on a pending payment asks Paytm again.
    if order["status"] == "PENDING" and not MOCK_MODE:
        try:
            confirm_order(order)
            order = get_order(order_id)
        except requests.RequestException:
            app.logger.exception("Paytm order status failed for %s", order_id)
    return render_template("result.html", order=order, merchant_name=MERCHANT_NAME, mock_mode=MOCK_MODE)


# --- subscription (auto-debit) routes ---------------------------------------

@app.get("/subscribe")
def subscribe():
    return render_template(
        "subscribe.html",
        plans=PLANS,
        mandate_amount=MANDATE_AMOUNT,
        first_debit_days=FIRST_DEBIT_DAYS,
        merchant_name=MERCHANT_NAME,
        mock_mode=MOCK_MODE,
        paytm_env=PAYTM_ENV,
        checkout_js_url=f"{PAYTM_HOST}/merchantpgpui/checkoutjs/merchants/{PAYTM_MID}.js",
    )


@app.post("/api/subscribe")
@same_origin
def api_subscribe():
    data = request.get_json(silent=True) or {}
    plan_id = data.get("plan")
    if plan_id not in PLANS:
        return jsonify(error="Choose a plan."), 400
    customer, error = parse_customer(data)
    if error:
        return jsonify(error=error), 400
    name, email, phone = customer

    first_debit_date = datetime.now(IST).date() + timedelta(days=FIRST_DEBIT_DAYS)
    expiry_date = add_months(first_debit_date, 12 * SUBSCRIPTION_YEARS)
    order_id = "SUB" + time.strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:6].upper()
    create_subscription(order_id, plan_id, name, email, phone, first_debit_date, expiry_date)
    amount = MANDATE_AMOUNT  # what the customer pays in Paytm's window now

    if MOCK_MODE:
        return jsonify(mock=True, orderId=order_id, redirect=url_for("mock_subscription", order_id=order_id))

    try:
        result = paytm_create_subscription(
            order_id, plan_id, "CUST_" + phone, name, email, phone, first_debit_date, expiry_date
        )
    except requests.RequestException as exc:
        app.logger.exception("Paytm subscription/create failed")
        update_subscription(order_id, status="INIT_FAILED", resp_msg=str(exc))
        return jsonify(error="Could not reach Paytm. Please try again."), 502

    body = result.get("body", {})
    info = body.get("resultInfo", {})
    if info.get("resultStatus") != "S" or not body.get("txnToken"):
        update_subscription(order_id, status="INIT_FAILED", resp_msg=info.get("resultMsg"), raw=result)
        return jsonify(error=info.get("resultMsg") or "Paytm rejected the request."), 502

    update_subscription(order_id, subs_id=body.get("subscriptionId"), status="INIT")
    return jsonify(mock=False, orderId=order_id, txnToken=body["txnToken"], amount=amount)


@app.post("/subscription/callback")
def subscription_callback():
    """Paytm POSTs here (via the browser) after the customer approves or abandons the mandate."""
    params = request.form.to_dict()
    order_id = params.get("ORDERID")
    posted_subs_id = params.get("SUBS_ID")
    checksum = params.pop("CHECKSUMHASH", "")

    if MOCK_MODE or (not order_id and not posted_subs_id):
        abort(400)
    if checksum and not checksum_ok(params, checksum):
        app.logger.warning("Subscription callback checksum mismatch; confirming with Paytm")

    sub = get_subscription(order_id=order_id) if order_id else None
    if sub is None and posted_subs_id:
        sub = get_subscription(subs_id=posted_subs_id)
    if sub is None:
        abort(404)

    # Redirect fields may be unsigned or omit a checksum; only server-side status is trusted.
    try:
        if sub["subs_id"]:
            refresh_subscription(sub)
        elif posted_subs_id:
            body = paytm_subscription_status(posted_subs_id).get("body", {})
            info = body.get("resultInfo", {})
            returned_subs_id = body.get("subsId")
            if paytm_result_ok(info) and (not returned_subs_id or returned_subs_id == posted_subs_id):
                update_subscription(
                    sub["order_id"],
                    subs_id=posted_subs_id,
                    status=body.get("status") or "INIT",
                    sub_status=body.get("subStatus"),
                    resp_msg=info.get("message") or info.get("resultMsg"),
                    raw=body,
                )
        else:
            body = paytm_subscription_status(
                order_id=sub["order_id"], cust_id="CUST_" + sub["phone"]
            ).get("body", {})
            info = body.get("resultInfo", {})
            if paytm_result_ok(info):
                update_subscription(
                    sub["order_id"],
                    status=body.get("status") or "INIT",
                    sub_status=body.get("subStatus"),
                    resp_msg=info.get("message") or info.get("resultMsg"),
                    raw=body,
                )
        if get_subscription(order_id=sub["order_id"])["subs_id"]:
            on_mandate_update(sub)
    except requests.RequestException:
        app.logger.exception("Paytm status check failed for subscription order %s", sub["order_id"])
        update_subscription(sub["order_id"], resp_msg="Awaiting confirmation from Paytm")

    sub = get_subscription(order_id=sub["order_id"])
    return redirect(url_for("manage_subscription", token=sub["manage_token"]), code=303)


@app.get("/subscription/<token>")
def manage_subscription(token):
    """Customer's page for one subscription. The unguessable token in the URL is the only access
    control, so in a real product put this behind your own login instead."""
    sub = get_subscription(manage_token=token)
    if sub is None:
        abort(404)
    if request.args.get("refresh") and sub["subs_id"] and not MOCK_MODE:
        try:
            refresh_subscription(sub)
            sub = get_subscription(manage_token=token)
        except requests.RequestException:
            app.logger.exception("Paytm subscription status failed")
    return render_template(
        "subscription.html",
        sub=sub,
        plan=PLANS.get(sub["plan"], {}),
        payments=list_subscription_payments(sub["subs_id"]) if sub["subs_id"] else [],
        merchant_name=MERCHANT_NAME,
        mock_mode=MOCK_MODE,
    )


@app.post("/subscription/<token>/cancel")
@same_origin
def cancel_subscription(token):
    sub = get_subscription(manage_token=token)
    if sub is None:
        abort(404)
    if sub["subs_id"] and sub["status"] not in ("CLOSED", "EXPIRED", "REJECT"):
        if MOCK_MODE:
            update_subscription(sub["order_id"], status="CLOSED", sub_status="USER_CANCELLED")
        else:
            try:
                result = paytm_cancel_subscription(sub["subs_id"]).get("body", {}).get("resultInfo", {})
            except requests.RequestException:
                app.logger.exception("Paytm subscription cancel failed")
                result = {}
            if paytm_result_ok(result):
                try:
                    refresh_subscription(get_subscription(order_id=sub["order_id"]))
                except requests.RequestException:
                    app.logger.exception("Could not verify cancellation for subscription %s", sub["subs_id"])
                    update_subscription(sub["order_id"], resp_msg="Cancellation submitted; awaiting confirmation.")
            else:
                update_subscription(
                    sub["order_id"],
                    resp_msg=result.get("message") or result.get("resultMsg") or "Could not cancel. Try again.",
                )
    return redirect(url_for("manage_subscription", token=token), code=303)


@app.post("/webhooks/paytm")
def paytm_webhook():
    """Server-to-server notifications from Paytm: each renewal debit, and mandate status changes
    (activated, paused, resumed, cancelled by the customer in their UPI/bank app, ...).
    Also one-time payments whose customer closed the browser before returning to us.
    Ask Paytm support to point both the payment and subscription-status webhooks at this URL."""
    params = request.form.to_dict()
    checksum = params.pop("CHECKSUMHASH", "")
    if MOCK_MODE or not checksum_ok(params, checksum):
        abort(400)

    subs_id = params.get("SUBS_ID")
    status = params.get("STATUS", "")
    order_id = params.get("ORDERID")
    if not subs_id and order_id:
        renewal = get_subscription_payment(order_id)  # renewal notices may omit SUBS_ID
        subs_id = renewal["subs_id"] if renewal else None
    sub = get_subscription(subs_id=subs_id) if subs_id else None
    if sub is None:
        order = get_order(order_id) if order_id and not subs_id else None
        if order is None:
            # Not one of ours; acknowledge so Paytm stops retrying.
            app.logger.info("Ignoring Paytm webhook for unknown order %s / subscription %s", order_id, subs_id)
            return "OK"
        try:
            confirm_order(order)  # one-time payment whose browser callback never arrived
        except requests.RequestException:
            app.logger.exception("Paytm confirmation failed for webhook on order %s", order_id)
            return "Retry later", 503
        return "OK"

    try:
        if (status.startswith("TXN_") or status == "PENDING") and order_id:
            if order_id == sub["order_id"]:
                confirm_subscription_payment(order_id, subs_id, MANDATE_AMOUNT)  # the ₹1 mandate charge
            elif sub["next_due_date"] and order_id == subscription_order_id(subs_id, date.fromisoformat(sub["next_due_date"])):
                settle_renewal(sub, date.fromisoformat(sub["next_due_date"]))
            else:
                confirm_subscription_payment(order_id, subs_id, sub["amount"])
        if refresh_subscription(sub):
            on_mandate_update(sub)
    except requests.RequestException:
        app.logger.exception("Paytm confirmation failed for webhook on %s", subs_id)
        return "Retry later", 503  # a non-2xx makes Paytm resend the webhook
    return "OK"


def run_sync():
    """Re-check Paytm state, send pre-debit notices, and settle scheduled renewals."""
    orders = db_execute("SELECT * FROM orders WHERE status='PENDING'").fetchall()
    subs = db_execute(
        "SELECT * FROM subscriptions WHERE subs_id IS NOT NULL AND status NOT IN ('CLOSED','EXPIRED','REJECT')"
    ).fetchall()
    errors = 0
    pre_notifications = 0
    renewals = 0
    for order in orders:
        try:
            confirm_order(order)
        except requests.RequestException:
            errors += 1
            app.logger.exception("Sync failed for order %s", order["order_id"])
    for sub in subs:
        try:
            if not refresh_subscription(sub):
                errors += 1
                continue
            sub = get_subscription(order_id=sub["order_id"])
            outcome = process_subscription_billing(sub)
            pre_notifications += int(outcome["pre_notification"])
            renewals += int(outcome["renewal"])
            if outcome["error"]:
                errors += 1
                app.logger.error("Subscription billing issue for %s: %s", sub["subs_id"], outcome["error"])
        except requests.RequestException:
            errors += 1
            app.logger.exception("Sync failed for subscription %s", sub["subs_id"])
    return {
        "orders": len(orders),
        "subscriptions": len(subs),
        "pre_notifications": pre_notifications,
        "renewals": renewals,
        "errors": errors,
    }


@app.cli.command("sync")
def sync_command():
    """python -m flask --app app sync"""
    print(run_sync())


@app.get("/cron/sync")
def cron_sync():
    """Called daily by Vercel Cron (see vercel.json)."""
    expected = f"Bearer {CRON_SECRET}"
    if MOCK_MODE or not CRON_SECRET or not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
        abort(401)
    return jsonify(run_sync())


@app.get("/healthz")
def healthz():
    db_execute("SELECT 1")
    return jsonify(ok=True, env=PAYTM_ENV, mock=MOCK_MODE)


@app.get("/api/health")
def health():
    """Non-secret configuration summary, to check what the deployment is actually using."""
    return jsonify(
        mode="MOCK" if MOCK_MODE else PAYTM_ENV,
        mid_set=bool(PAYTM_MID),
        merchant_key_set=bool(PAYTM_MERCHANT_KEY),
        paytm_host=PAYTM_HOST,
        website=PAYTM_WEBSITE,
        channel_id=PAYTM_CHANNEL_ID,
        callback_url=BASE_URL + url_for("payment_callback"),
        database="postgres" if DATABASE_URL else "sqlite",
    )


# --- mock gateway (only when no credentials are configured) -----------------

@app.route("/payment/mock/<order_id>", methods=["GET", "POST"])
def mock_gateway(order_id):
    if not MOCK_MODE:
        abort(404)
    order = get_order(order_id)
    if order is None:
        abort(404)
    if request.method == "POST":
        outcome = request.form.get("outcome")
        if outcome == "success":
            update_order(order_id, "SUCCESS", "MOCK" + uuid.uuid4().hex[:10].upper(), "Mock payment successful")
        else:
            update_order(order_id, "FAILED", None, "Mock payment failed")
        return redirect(url_for("payment_result", order_id=order_id), code=303)
    return render_template("mock.html", order=order, merchant_name=MERCHANT_NAME, mock_mode=True)


@app.route("/subscription/mock/<order_id>", methods=["GET", "POST"])
def mock_subscription(order_id):
    if not MOCK_MODE:
        abort(404)
    sub = get_subscription(order_id=order_id)
    if sub is None:
        abort(404)
    if request.method == "POST":
        if request.form.get("outcome") == "success":
            subs_id = "MOCKSUB" + uuid.uuid4().hex[:8].upper()
            update_subscription(order_id, subs_id=subs_id, status="ACTIVE", sub_status="ACTIVE", pay_mode="UPI",
                                resp_msg="Mock mandate approved")
            save_subscription_payment(order_id, subs_id, MANDATE_AMOUNT, "SUCCESS",
                                      "MOCK" + uuid.uuid4().hex[:10].upper(), "Mock mandate charge")
        else:
            update_subscription(order_id, status="REJECT", resp_msg="Mock mandate declined")
        return redirect(url_for("manage_subscription", token=sub["manage_token"]), code=303)
    return render_template("mock.html", order=sub, merchant_name=MERCHANT_NAME, mock_mode=True)


init_db()

if __name__ == "__main__":
    print(f" * Paytm mode: {'MOCK (no credentials)' if MOCK_MODE else PAYTM_ENV}")
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", 5000)), debug=os.getenv("FLASK_DEBUG") == "1")
