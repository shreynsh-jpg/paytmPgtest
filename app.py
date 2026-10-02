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

With PAYTM_MID unset the app runs in MOCK mode so the page can be tested
without credentials.
"""

import json
import os
import re
import sqlite3
import time
import uuid
from decimal import Decimal, InvalidOperation

import requests
from dotenv import load_dotenv
from flask import Flask, abort, g, jsonify, redirect, render_template, request, url_for
from paytmchecksum import PaytmChecksum

load_dotenv()

PAYTM_MID = os.getenv("PAYTM_MID", "").strip()
PAYTM_MERCHANT_KEY = os.getenv("PAYTM_MERCHANT_KEY", "").strip()
PAYTM_ENV = os.getenv("PAYTM_ENV", "STAGING").strip().upper()
PAYTM_WEBSITE = os.getenv("PAYTM_WEBSITE", "WEBSTAGING").strip()
PAYTM_INDUSTRY_TYPE_ID = os.getenv("PAYTM_INDUSTRY_TYPE_ID", "Retail").strip()
PAYTM_CHANNEL_ID = os.getenv("PAYTM_CHANNEL_ID", "WEB").strip()
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

MIN_AMOUNT = Decimal("1.00")
MAX_AMOUNT = Decimal("100000.00")

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


# --- Paytm API helpers -----------------------------------------------------

def paytm_post(path, body):
    """Sign `body` and POST it to a Paytm v1/v3 JSON API."""
    body_str = json.dumps(body, separators=(",", ":"))
    signature = PaytmChecksum.generateSignature(body_str, PAYTM_MERCHANT_KEY)
    payload = '{"body":%s,"head":{"signature":"%s"}}' % (body_str, signature)
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


def paytm_status(order_id):
    return paytm_post("/v3/order/status", {"mid": PAYTM_MID, "orderId": order_id})


# --- validation ------------------------------------------------------------

def parse_amount(value):
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        return None
    if not (MIN_AMOUNT <= amount <= MAX_AMOUNT):
        return None
    return amount


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
def api_initiate():
    data = request.get_json(silent=True) or {}
    amount = parse_amount(data.get("amount"))
    name = (data.get("name") or "").strip()[:100]
    email = (data.get("email") or "").strip()[:150]
    phone = re.sub(r"\D", "", data.get("phone") or "")

    if amount is None:
        return jsonify(error=f"Enter an amount between ₹{MIN_AMOUNT} and ₹{MAX_AMOUNT}."), 400
    if not name:
        return jsonify(error="Name is required."), 400
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify(error="Enter a valid email address."), 400
    if not re.fullmatch(r"\d{10}", phone):
        return jsonify(error="Enter a 10-digit mobile number."), 400

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
    if not PaytmChecksum.verifySignature(params, PAYTM_MERCHANT_KEY, checksum):
        app.logger.warning("Checksum mismatch for order %s", order_id)
        abort(400, "Checksum mismatch")

    order = get_order(order_id)
    if order is None:
        abort(404)

    # Never trust the browser-relayed callback alone: confirm with Paytm server-to-server.
    try:
        status = paytm_status(order_id)
        body = status.get("body", {})
        result = body.get("resultInfo", {})
        paid_amount = parse_amount(body.get("txnAmount"))
        state = {"TXN_SUCCESS": "SUCCESS", "TXN_FAILURE": "FAILED", "PENDING": "PENDING"}.get(
            result.get("resultStatus"), "PENDING"
        )
        if state == "SUCCESS" and paid_amount != Decimal(order["amount"]):
            state = "AMOUNT_MISMATCH"
        update_order(order_id, state, body.get("txnId"), result.get("resultMsg"), status)
    except requests.RequestException:
        app.logger.exception("Paytm order status failed")
        update_order(order_id, "PENDING", params.get("TXNID"), "Awaiting confirmation from Paytm", params)

    return redirect(url_for("payment_result", order_id=order_id), code=303)


@app.get("/payment/result/<order_id>")
def payment_result(order_id):
    order = get_order(order_id)
    if order is None:
        abort(404)
    return render_template("result.html", order=order, merchant_name=MERCHANT_NAME, mock_mode=MOCK_MODE)


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


init_db()

if __name__ == "__main__":
    print(f" * Paytm mode: {'MOCK (no credentials)' if MOCK_MODE else PAYTM_ENV}")
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", 5000)), debug=os.getenv("FLASK_DEBUG") == "1")
