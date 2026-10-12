"""SwiftRun backend: Flask + SQLite.

Run:   python3 app.py            (dev)
       gunicorn -w 2 -b 0.0.0.0:8000 app:app     (production)

Everything is configured through environment variables or a .env file
(see .env.example). Only Flask is required from pip.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, g, jsonify, request, send_from_directory
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

HERE = Path(__file__).resolve().parent


# ── Config ──────────────────────────────────────────────────────────────────
def _load_dotenv(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(HERE / ".env")


def env(name, default=""):
    return os.environ.get(name, default)


def _persisted_secret(name, filename):
    """Use the env var if set, otherwise generate once and keep it in a file."""
    v = env(name)
    if v:
        return v
    p = HERE / filename
    if p.exists():
        return p.read_text().strip()
    v = secrets.token_urlsafe(48)
    p.write_text(v)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return v


DATABASE = env("DATABASE", str(HERE / "swiftrun.db"))
SECRET_KEY = _persisted_secret("SECRET_KEY", ".secret_key")
CALLBACK_SECRET = _persisted_secret("MPESA_CALLBACK_SECRET", ".mpesa_callback_secret")
TZ = ZoneInfo(env("TIMEZONE", "Africa/Nairobi"))
STATIC_DIR = Path(env("STATIC_DIR", str(HERE.parent))).resolve()
ALLOWED_ORIGINS = [o.strip() for o in env("ALLOWED_ORIGINS").split(",") if o.strip()]
TOKEN_MAX_AGE = 7 * 24 * 3600
LOCAL_ORIGIN = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$")  # local dev servers, e.g. VS Code Live Server
RATE_LIMIT_ON = env("RATE_LIMIT", "1") != "0"

# Pricing (KES). Distance fee = base for the errand type + per-km * distance when
# the client supplies distanceKm. Without a maps API the base fee is used.
BASE_FEE = {"delivery": 200, "grocery": 250, "pickup": 180, "errand": 220, "pharmacy": 200}
PER_KM = int(env("PER_KM_FEE", "30"))
GOODS_RATE = float(env("GOODS_FEE_RATE", "0.05"))
RIDER_SHARE = float(env("RIDER_SHARE", "0.8"))  # share of the distance fee paid to the rider

# M-Pesa (Daraja). MPESA_MODE: mock (default, no real money) | sandbox | production
MPESA_MODE = env("MPESA_MODE", "mock")
MPESA_MOCK_DELAY = float(env("MPESA_MOCK_DELAY", "4"))
# Unpaid M-Pesa orders are cancelled after this many minutes (0 = never). Keep the About-page FAQ in step.
ORDER_EXPIRY_MINUTES = int(env("ORDER_EXPIRY_MINUTES", "30"))
# Payment reconciliation: ask Safaricom about a payment once it has waited this long without a callback,
# then again at most every RECHECK seconds. After PAYMENT_WINDOW_MIN minutes it is closed as "expired".
RECONCILE_INTERVAL = int(env("RECONCILE_INTERVAL", "20"))
RECONCILE_MIN_AGE = int(env("RECONCILE_MIN_AGE", "45"))
RECONCILE_RECHECK = int(env("RECONCILE_RECHECK", "30"))
PAYMENT_WINDOW_MIN = int(env("PAYMENT_WINDOW_MIN", "60"))

# Website address, used to build password-reset links (the part before "#reset=...").
# e.g. https://big-dario.github.io/swiftrun   or   https://goto-errands.co.ke
FRONTEND_URL = env("FRONTEND_URL").rstrip("/")
SUPPORT_LINE = env("SUPPORT_LINE", "0751 696 230 / 0712 331 664")
RESET_TTL_SECONDS = 3600

# Notifications. SMS: Africa's Talking. Email: any SMTP server.
# With no provider configured a channel runs in LOG mode: messages are written to the server log only.
AT_USERNAME, AT_API_KEY, AT_SENDER_ID = env("AT_USERNAME"), env("AT_API_KEY"), env("AT_SENDER_ID")
AT_BASE = env("AT_BASE_URL") or ("https://api.sandbox.africastalking.com" if AT_USERNAME == "sandbox"
                                  else "https://api.africastalking.com")
SMS_PROVIDER = env("SMS_PROVIDER") or ("africastalking" if AT_API_KEY and AT_USERNAME else "log")
SMTP_HOST, SMTP_PORT = env("SMTP_HOST"), int(env("SMTP_PORT", "587"))
SMTP_USER, SMTP_PASSWORD = env("SMTP_USER"), env("SMTP_PASSWORD")
SMTP_SECURITY = env("SMTP_SECURITY", "starttls")  # starttls | ssl | none
SMTP_FROM = env("SMTP_FROM") or SMTP_USER or "no-reply@swiftrun.local"
EMAIL_PROVIDER = "smtp" if SMTP_HOST else "log"
NOTIFY_CUSTOMER_SMS = env("NOTIFY_CUSTOMER_SMS", "1") == "1"
NOTIFY_CUSTOMER_EMAIL = env("NOTIFY_CUSTOMER_EMAIL", "1") == "1"
NOTIFY_ADMIN_SMS = env("NOTIFY_ADMIN_SMS", "1") == "1"
NOTIFY_ADMIN_EMAIL = env("NOTIFY_ADMIN_EMAIL", "1") == "1"
NOTIFY_RIDER_SMS = env("NOTIFY_RIDER_SMS", "1") == "1"
MAX_SEND_ATTEMPTS = 4

CATALOG = json.loads((HERE / "services.json").read_text(encoding="utf-8"))
SERVICES = {s["id"]: s for s in CATALOG["services"]}
CATEGORIES = {c["id"]: c for c in CATALOG["categories"]}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
app.config["JSON_SORT_KEYS"] = False
if env("TRUST_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("swiftrun")
signer = URLSafeTimedSerializer(SECRET_KEY, salt="swiftrun-auth")

# ── Database ────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, phone TEXT,
  password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'customer',
  created_at TEXT NOT NULL, token_ver INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS riders(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL,
  rating REAL NOT NULL DEFAULT 5, trips INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active', vehicle TEXT NOT NULL DEFAULT 'Motorbike',
  plate TEXT, area TEXT, earnings INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders(
  num INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER REFERENCES users(id),
  service_id TEXT, category TEXT, service TEXT, service_icon TEXT,
  type TEXT NOT NULL, descr TEXT NOT NULL, pickup TEXT NOT NULL, dropoff TEXT NOT NULL,
  status TEXT NOT NULL, pay_status TEXT NOT NULL, pay_method TEXT,
  goods_value INTEGER NOT NULL DEFAULT 0, dist_fee INTEGER NOT NULL, goods_fee INTEGER NOT NULL,
  total INTEGER NOT NULL, notes TEXT, created_at TEXT NOT NULL,
  cust_name TEXT NOT NULL, cust_phone TEXT NOT NULL, cust_email TEXT,
  rider_id TEXT REFERENCES riders(id), mpesa_ref TEXT, pay_attempts INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_orders_phone ON orders(cust_phone);
CREATE INDEX IF NOT EXISTS ix_orders_email ON orders(cust_email);
CREATE INDEX IF NOT EXISTS ix_orders_user ON orders(user_id);
CREATE TABLE IF NOT EXISTS timeline(
  order_num INTEGER NOT NULL REFERENCES orders(num) ON DELETE CASCADE,
  idx INTEGER NOT NULL, label TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, at TEXT,
  PRIMARY KEY(order_num, idx));
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_num INTEGER NOT NULL REFERENCES orders(num),
  method TEXT NOT NULL, phone TEXT, amount INTEGER NOT NULL,
  status TEXT NOT NULL, ref TEXT, checkout_id TEXT UNIQUE, failure TEXT,
  created_at TEXT NOT NULL, completed_at TEXT,
  source TEXT, confirmed_by INTEGER, note TEXT, last_checked_at TEXT, check_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS password_resets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event TEXT NOT NULL, channel TEXT NOT NULL, audience TEXT NOT NULL DEFAULT 'customer',
  recipient TEXT NOT NULL, subject TEXT, body TEXT NOT NULL, order_num INTEGER,
  status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
  provider TEXT, error TEXT, next_try_at TEXT NOT NULL, created_at TEXT NOT NULL, sent_at TEXT);
CREATE INDEX IF NOT EXISTS ix_notif_status ON notifications(status, next_try_at);
"""

SEED_RIDERS = [
    ("R001", "Brian Otieno", "+254722111222", 4.8, 312, "active", "Motorbike", "KBZ 456X", "CBD / Westlands", 42000),
    ("R002", "Carol Mwangi", "+254733444555", 4.9, 589, "active", "Bicycle", None, "Kilimani / Karen", 28500),
    ("R003", "Eric Omondi", "+254744555666", 4.7, 201, "active", "Motorbike", None, "Eastlands", 31200),
    ("R004", "Faith Njoroge", "+254755666777", 4.6, 98, "offline", "Motorbike", None, "South C / Langata", 14800),
]


def open_db():
    db = sqlite3.connect(DATABASE, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    return db


def get_db():
    if "db" not in g:
        g.db = open_db()
    return g.db


@app.teardown_appcontext
def _close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = open_db()
    db.executescript(SCHEMA)
    # Databases created before password reset existed need the new column
    if "token_ver" not in [r["name"] for r in db.execute("PRAGMA table_info(users)")]:
        db.execute("ALTER TABLE users ADD COLUMN token_ver INTEGER NOT NULL DEFAULT 0")
    pay_cols = [r["name"] for r in db.execute("PRAGMA table_info(payments)")]
    for col, ddl in (("source", "TEXT"), ("confirmed_by", "INTEGER"), ("note", "TEXT"),
                     ("last_checked_at", "TEXT"), ("check_count", "INTEGER NOT NULL DEFAULT 0")):
        if col not in pay_cols:
            db.execute(f"ALTER TABLE payments ADD COLUMN {col} {ddl}")
    db.execute("UPDATE notifications SET status='queued' WHERE status='sending'")  # interrupted by a restart
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("DELETE FROM notifications WHERE created_at < ?", (cutoff,))
    db.execute("DELETE FROM password_resets WHERE created_at < ?", (cutoff,))
    # Order numbers start at SR-1024 (matches the demo data in the frontend)
    if not db.execute("SELECT 1 FROM sqlite_sequence WHERE name='orders'").fetchone():
        db.execute("INSERT INTO sqlite_sequence(name, seq) VALUES('orders', 1023)")
    if env("SEED_RIDERS", "1") == "1" and not db.execute("SELECT 1 FROM riders").fetchone():
        db.executemany("INSERT INTO riders VALUES(?,?,?,?,?,?,?,?,?,?)", SEED_RIDERS)
    admin_email, admin_pw = env("ADMIN_EMAIL").lower(), env("ADMIN_PASSWORD")
    if admin_email and admin_pw:
        row = db.execute("SELECT id FROM users WHERE email=?", (admin_email,)).fetchone()
        if row:
            db.execute("UPDATE users SET role='admin' WHERE id=?", (row["id"],))
            if env("ADMIN_FORCE_PASSWORD") == "1":  # emergency recovery: remove this variable afterwards
                db.execute("UPDATE users SET password_hash=?, token_ver=token_ver+1 WHERE id=?",
                           (generate_password_hash(admin_pw), row["id"]))
                log.warning("ADMIN_FORCE_PASSWORD is set: admin password reset from ADMIN_PASSWORD. Remove ADMIN_FORCE_PASSWORD now.")
        else:
            db.execute("INSERT INTO users(name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                       ("Admin", admin_email, generate_password_hash(admin_pw), "admin", now_iso()))
    elif not db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        log.warning("No admin user exists. Set ADMIN_EMAIL and ADMIN_PASSWORD to create one.")
    db.commit()
    db.close()


# ── Helpers ─────────────────────────────────────────────────────────────────
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_in(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_hm(iso):
    if not iso:
        return "—"
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(TZ).strftime("%H:%M")


def local_dt(iso):
    if not iso:
        return "—"
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(TZ).strftime("%Y-%m-%d %H:%M")


class ApiError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.msg, self.status = msg, status


@app.errorhandler(ApiError)
def _api_error(e):
    return jsonify(error=e.msg), e.status


@app.errorhandler(404)
def _404(_e):
    return jsonify(error="Not found"), 404


@app.errorhandler(405)
def _405(_e):
    return jsonify(error="Method not allowed"), 405


@app.errorhandler(413)
def _413(_e):
    return jsonify(error="Request too large"), 413


@app.errorhandler(Exception)
def _500(e):
    log.exception("Unhandled error: %s", e)
    return jsonify(error="Something went wrong on our side"), 500


@app.after_request
def _headers(resp):
    origin = request.headers.get("Origin")
    if origin and (origin in ALLOWED_ORIGINS or LOCAL_ORIGIN.match(origin)):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "600"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


_hits = {}


def rate_limit(bucket, limit, per=60):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **k):
            if RATE_LIMIT_ON:
                key, now = (bucket, request.remote_addr), time.time()
                recent = [t for t in _hits.get(key, []) if now - t < per]
                if len(recent) >= limit:
                    raise ApiError("Too many requests. Please wait a minute and try again.", 429)
                recent.append(now)
                _hits[key] = recent
                if len(_hits) > 20000:
                    for kk in [kk for kk, v in _hits.items() if not v or now - v[-1] > per]:
                        _hits.pop(kk, None)
            return f(*a, **k)
        return wrapper
    return deco


CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


def body():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def text(d, key, label, min_len=0, max_len=200, required=True):
    v = d.get(key)
    v = CTRL.sub("", v).strip() if isinstance(v, str) else ""
    if not v and not required:
        return ""
    if len(v) < max(min_len, 1 if required else 0):
        raise ApiError(f"{label} is required" if not v else f"{label} is too short")
    if len(v) > max_len:
        raise ApiError(f"{label} is too long (max {max_len} characters)")
    return v


def money(d, key, label, maximum=500000):
    v = d.get(key, 0)
    if v in (None, ""):
        return 0
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise ApiError(f"{label} must be a number")
    if n != n or n < 0 or n > maximum:
        raise ApiError(f"{label} must be between 0 and {maximum:,}")
    return int(round(n))


def normalize_phone(p):
    """Kenyan mobile -> '2547XXXXXXXX' / '2541XXXXXXXX', or None if invalid."""
    digits = re.sub(r"\D", "", str(p or ""))
    if len(digits) == 10 and digits[0] == "0":
        digits = "254" + digits[1:]
    elif len(digits) == 9 and digits[0] in "17":
        digits = "254" + digits
    return digits if re.fullmatch(r"254[17]\d{8}", digits) else None


def plus(phone):
    return "+" + phone if phone and not phone.startswith("+") else phone


# ── Auth ────────────────────────────────────────────────────────────────────
def make_token(user):
    # "ver" lets us sign out every existing session when the password changes
    return signer.dumps({"uid": user["id"], "ver": user["token_ver"]})


def current_user():
    if hasattr(g, "_user"):
        return g._user
    g._user = None
    h = request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        try:
            data = signer.loads(h[7:], max_age=TOKEN_MAX_AGE)
            u = get_db().execute("SELECT * FROM users WHERE id=?", (data["uid"],)).fetchone()
            if u and u["token_ver"] == data.get("ver", 0):
                g._user = u
        except (BadSignature, SignatureExpired, KeyError):
            pass
    return g._user


def require_auth(f):
    @wraps(f)
    def w(*a, **k):
        if not current_user():
            raise ApiError("Please log in", 401)
        return f(*a, **k)
    return w


def require_admin(f):
    @wraps(f)
    def w(*a, **k):
        u = current_user()
        if not u:
            raise ApiError("Please log in", 401)
        if u["role"] != "admin":
            raise ApiError("Admin access required", 403)
        return f(*a, **k)
    return w


def user_dict(u):
    return {"id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"]}


@app.post("/api/auth/register")
@rate_limit("register", 5)
def register():
    d = body()
    first = text(d, "first", "First name", 1, 40)
    last = text(d, "last", "Last name", 1, 40)
    email = text(d, "email", "Email", 3, 120).lower()
    pw = d.get("password")
    if not EMAIL.match(email):
        raise ApiError("Enter a valid email address")
    if not isinstance(pw, str) or len(pw) < 8:
        raise ApiError("Password must be at least 8 characters")
    if len(pw) > 200:
        raise ApiError("Password is too long")
    db = get_db()
    try:
        cur = db.execute("INSERT INTO users(name,email,password_hash,created_at) VALUES(?,?,?,?)",
                         (f"{first} {last}", email, generate_password_hash(pw), now_iso()))
    except sqlite3.IntegrityError:
        raise ApiError("An account with this email already exists", 409)
    db.commit()
    u = db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(token=make_token(u), user=user_dict(u)), 201


_DUMMY_HASH = generate_password_hash("not-a-real-password")


@app.post("/api/auth/login")
@rate_limit("login", 10)
def login():
    d = body()
    email = text(d, "email", "Email", 3, 120).lower()
    pw = d.get("password")
    u = get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    ok = check_password_hash(u["password_hash"] if u else _DUMMY_HASH, pw if isinstance(pw, str) else "")
    if not u or not ok:
        raise ApiError("Incorrect email or password", 401)
    return jsonify(token=make_token(u), user=user_dict(u))


@app.get("/api/auth/me")
@require_auth
def me():
    return jsonify(user=user_dict(current_user()))


def validate_new_password(pw, email=""):
    if not isinstance(pw, str) or len(pw) < 8:
        raise ApiError("Password must be at least 8 characters")
    if len(pw) > 200:
        raise ApiError("Password is too long")
    if email and pw.strip().lower() == email.lower():
        raise ApiError("Your password can't be your email address")


@app.post("/api/auth/password")
@require_auth
@rate_limit("chpw", 10)
def change_password():
    d, u, db = body(), current_user(), get_db()
    cur = d.get("current")
    # 403, not 401: a wrong current password must not look like an expired session
    if not isinstance(cur, str) or not check_password_hash(u["password_hash"], cur):
        raise ApiError("Your current password is incorrect", 403)
    new = d.get("password")
    validate_new_password(new, u["email"])
    if hmac.compare_digest(cur, new):
        raise ApiError("Choose a password different from your current one")
    ver = u["token_ver"] + 1
    db.execute("UPDATE users SET password_hash=?, token_ver=? WHERE id=?", (generate_password_hash(new), ver, u["id"]))
    db.execute("UPDATE password_resets SET used_at=? WHERE user_id=? AND used_at IS NULL", (now_iso(), u["id"]))
    notify_password_changed(db, u)
    db.commit()
    fresh = db.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
    # all other devices are signed out; this one gets a fresh token
    return jsonify(token=make_token(fresh), user=user_dict(fresh), message="Password changed. Other devices were signed out.")


@app.post("/api/auth/forgot")
@rate_limit("forgot", 5, 3600)
def forgot_password():
    email = str(body().get("email") or "").strip().lower()[:120]
    reply = jsonify(message="If an account exists for that email, a reset link is on its way. It expires in 1 hour.")
    if not EMAIL.match(email):
        return reply          # same answer for everything: never reveal which emails have accounts
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if u:
        recent = db.execute("SELECT COUNT(*) c FROM password_resets WHERE user_id=? AND created_at>?",
                            (u["id"], iso_in(-3600))).fetchone()["c"]
        if recent < 3:
            if not FRONTEND_URL:
                log.error("FRONTEND_URL is not set, so a reset link can't be built. Set it to your website address.")
            else:
                raw = secrets.token_urlsafe(32)
                db.execute("INSERT INTO password_resets(user_id,token_hash,expires_at,created_at) VALUES(?,?,?,?)",
                           (u["id"], hashlib.sha256(raw.encode()).hexdigest(), iso_in(RESET_TTL_SECONDS), now_iso()))
                first = u["name"].split()[0]
                queue_notification(
                    db, "password_reset", "email", u["email"], audience="customer",
                    subject="Reset your SwiftRun password",
                    body=(f"Hello {first},\n\nWe received a request to reset your SwiftRun password. "
                          f"Open this link to choose a new one (it works once and expires in 1 hour):\n\n"
                          f"{FRONTEND_URL}/#reset={raw}\n\n"
                          f"If you didn't ask for this, you can ignore this email; your password stays the same.\n\n"
                          f"SwiftRun | {SUPPORT_LINE}"))
            db.commit()
    return reply


@app.post("/api/auth/reset")
@rate_limit("reset", 10)
def reset_password():
    d, db = body(), get_db()
    tok, bad = d.get("token"), "This reset link is invalid or has expired. Request a new one."
    if not isinstance(tok, str) or not 20 <= len(tok) <= 120:
        raise ApiError(bad)
    row = db.execute("SELECT * FROM password_resets WHERE token_hash=?", (hashlib.sha256(tok.encode()).hexdigest(),)).fetchone()
    if not row or row["used_at"] or row["expires_at"] < now_iso():
        raise ApiError(bad)
    u = db.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
    validate_new_password(d.get("password"), u["email"])   # checked before the link is used up
    db.execute("UPDATE users SET password_hash=?, token_ver=token_ver+1 WHERE id=?",
               (generate_password_hash(d["password"]), u["id"]))
    db.execute("UPDATE password_resets SET used_at=? WHERE user_id=? AND used_at IS NULL", (now_iso(), u["id"]))
    notify_password_changed(db, u)
    db.commit()
    return jsonify(ok=True, message="Password updated. You can log in now.")


# ── Serialisers (shape matches what index.html already renders) ─────────────
def rider_public(r):
    if not r:
        return None
    vehicle = r["vehicle"] + (f" · {r['plate']}" if r["plate"] else "")
    return {"id": r["id"], "name": r["name"], "phone": r["phone"], "rating": r["rating"],
            "trips": r["trips"], "vehicle": vehicle}


def rider_admin(r):
    return {"id": r["id"], "name": r["name"], "phone": r["phone"], "rating": r["rating"],
            "trips": r["trips"], "status": r["status"], "vehicle": r["vehicle"], "plate": r["plate"] or "",
            "area": r["area"] or "", "earnings": f"KES {r['earnings']:,}"}


def orders_out(db, rows):
    if not rows:
        return []
    nums = [r["num"] for r in rows]
    ph = ",".join("?" * len(nums))
    tl = {}
    for t in db.execute(f"SELECT * FROM timeline WHERE order_num IN ({ph}) ORDER BY idx", nums):
        tl.setdefault(t["order_num"], []).append({"time": local_hm(t["at"]), "label": t["label"], "done": bool(t["done"])})
    rids = {r["rider_id"] for r in rows if r["rider_id"]}
    riders = {}
    if rids:
        rph = ",".join("?" * len(rids))
        riders = {r["id"]: r for r in db.execute(f"SELECT * FROM riders WHERE id IN ({rph})", list(rids))}
    out = []
    for r in rows:
        out.append({
            "id": f"SR-{r['num']}", "desc": r["descr"], "type": r["type"],
            "category": r["category"], "service": r["service"], "serviceIcon": r["service_icon"],
            "pickup": r["pickup"], "dropoff": r["dropoff"], "status": r["status"],
            "payStatus": r["pay_status"], "payMethod": r["pay_method"],
            "goodsValue": r["goods_value"], "distFee": r["dist_fee"], "goodsFee": r["goods_fee"], "total": r["total"],
            "notes": r["notes"] or "", "createdAt": r["created_at"],
            "customer": {"name": r["cust_name"], "phone": plus(r["cust_phone"]), "email": r["cust_email"] or "N/A"},
            "rider": rider_public(riders.get(r["rider_id"])), "mpesaRef": r["mpesa_ref"],
            "timeline": tl.get(r["num"], []),
        })
    return out


def parse_order_id(raw):
    m = re.fullmatch(r"(?:SR-?)?(\d{1,9})", str(raw).strip(), re.I)
    return int(m.group(1)) if m else None


def load_order(db, order_id):
    num = parse_order_id(order_id)
    row = db.execute("SELECT * FROM orders WHERE num=?", (num,)).fetchone() if num else None
    if not row:
        raise ApiError("Order not found", 404)
    return row


def is_owner(order, user):
    return bool(user) and (order["user_id"] == user["id"] or
                           (order["cust_email"] or "").lower() == user["email"])


def is_admin(user):
    return bool(user) and user["role"] == "admin"


# ── Timeline / rider helpers ────────────────────────────────────────────────
def set_step(db, num, idx, label=None, done=True):
    db.execute("UPDATE timeline SET done=?, at=?, label=COALESCE(?, label) WHERE order_num=? AND idx=?",
               (1 if done else 0, now_iso() if done else None, label, num, idx))


def assign_rider(db, num, rider_id=None):
    if rider_id:
        r = db.execute("SELECT * FROM riders WHERE id=?", (rider_id,)).fetchone()
        if not r:
            raise ApiError("Rider not found", 404)
        if r["status"] != "active":
            raise ApiError(f"{r['name']} is not active", 409)
    else:
        r = db.execute(
            """SELECT r.* FROM riders r WHERE r.status='active'
               ORDER BY (SELECT COUNT(*) FROM orders o WHERE o.rider_id=r.id AND o.status IN ('confirmed','in-transit')),
                        r.trips LIMIT 1""").fetchone()
    if not r:
        return None
    db.execute("UPDATE orders SET rider_id=? WHERE num=?", (r["id"], num))
    set_step(db, num, 2, f"Rider assigned — {r['name']}")
    notify_order(db, "assigned", num)
    return r


# ── Notifications: outbox + senders ─────────────────────────────────────────
# Events are written to the `notifications` table inside the same transaction as the order change,
# so nothing is lost if the server restarts. A background worker sends them and retries failures.
SENSITIVE_EVENTS = {"password_reset"}


def queue_notification(db, event, channel, recipient, body, subject=None, order_num=None, audience="customer"):
    if not recipient:
        return
    db.execute(
        "INSERT INTO notifications(event,channel,audience,recipient,subject,body,order_num,status,next_try_at,created_at)"
        " VALUES(?,?,?,?,?,?,?,'queued',?,?)",
        (event, channel, audience, recipient, subject, body, order_num, now_iso(), now_iso()))


def _csv(name):
    return [x.strip() for x in env(name).split(",") if x.strip()]


def admin_phones():
    return [p for p in (normalize_phone(x) for x in _csv("ADMIN_NOTIFY_PHONES")) if p]


def admin_emails():
    return _csv("ADMIN_NOTIFY_EMAILS") or ([env("ADMIN_EMAIL")] if env("ADMIN_EMAIL") else [])


def _short(text_, n):
    t = " ".join(str(text_ or "").split())
    return t if len(t) <= n else t[: n - 3].rstrip() + "..."


def notify_password_changed(db, u):
    queue_notification(
        db, "password_changed", "email", u["email"], audience="customer", subject="Your SwiftRun password was changed",
        body=(f"Hello {u['name'].split()[0]},\n\nThe password on your SwiftRun account was just changed and all other "
              f"devices were signed out.\n\nIf this was you, there is nothing to do. If it was not, reset your password "
              f"straight away and contact us: {SUPPORT_LINE}\n\nSwiftRun"))


def notify_order(db, event, num, by=None, ref=None, extra=None):
    """Queue every message an order event should produce (customer, admin, rider)."""
    o = db.execute("SELECT * FROM orders WHERE num=?", (num,)).fetchone()
    if not o:
        return
    rider = db.execute("SELECT * FROM riders WHERE id=?", (o["rider_id"],)).fetchone() if o["rider_id"] else None
    code, total = f"SR-{num}", f"KES {o['total']:,}"
    svc = o["service"] or o["type"].title()
    mpesa = o["pay_method"] == "mpesa"
    route = f"{_short(o['pickup'], 40)} -> {_short(o['dropoff'], 40)}"
    who = f"{o['cust_name']} {plus(o['cust_phone'])}"
    ref = ref or o["mpesa_ref"]
    refp = f" (M-Pesa ref {ref})" if ref else ""
    cust = admin = rider_msg = None
    headline = ""

    if event == "created":
        headline = "order received"
        within = f" within {ORDER_EXPIRY_MINUTES} minutes" if ORDER_EXPIRY_MINUTES > 0 else ""
        cust = (f"SwiftRun: Order {code} received ({total}). Complete the M-Pesa payment{within} to confirm it, or the order is cancelled." if mpesa else
                f"SwiftRun: Order {code} confirmed ({total}, cash on delivery). A rider will be assigned shortly.")
        admin = f"NEW ORDER {code}: {svc}, {total} ({'M-Pesa, awaiting payment' if mpesa else 'cash on delivery'}). {route}. {who}"
    elif event == "paid":
        headline = "payment received"
        cust = f"SwiftRun: Payment of {total} received for order {code}{refp}. Your order is confirmed."
        admin = None if by == "admin" else f"PAID {code}: {total} via M-Pesa{refp}. {route}. Ready to dispatch."
    elif event == "paid_cancelled":
        headline = "payment for a cancelled order"
        cust = f"SwiftRun: We received your payment of {total} for cancelled order {code}. We will contact you to refund it."
        admin = f"REFUND NEEDED {code}: M-Pesa payment of {total}{refp} arrived after the order was cancelled. {who}"
    elif event == "paid_duplicate":
        headline = "second payment received"
        cust = f"SwiftRun: We received a second payment of {total}{refp} for order {code}. We will refund it."
        admin = f"DUPLICATE PAYMENT {code}: a second M-Pesa payment of {total}{refp} came in. Refund it. {who}"
    elif event == "payment_review":
        headline = "payment needs review"
        admin = f"CHECK PAYMENT {code}: {extra}. {who}"
    elif event == "expired":
        headline = "order cancelled, payment not received"
        cust = (f"SwiftRun: Order {code} was cancelled because we did not receive your M-Pesa payment. "
                f"If you already paid, call or WhatsApp {SUPPORT_LINE} and we will sort it out.")
    elif event == "confirmed":
        headline = "order accepted"
        cust = f"SwiftRun: Order {code} has been accepted. We are lining up a rider."
    elif event == "dispatched":
        headline = "on its way"
        rtxt = f"Rider: {rider['name']} {plus(rider['phone'])}." if rider else "Your rider is on the way."
        cust = f"SwiftRun: Order {code} is on its way. {rtxt}"
    elif event == "completed":
        headline = "delivered"
        cust = f"SwiftRun: Order {code} has been delivered. Thank you for choosing SwiftRun!"
    elif event == "cancelled":
        headline = "order cancelled"
        paid = o["pay_status"] == "paid"
        cust = f"SwiftRun: Order {code} has been cancelled." + (f" We will contact you about your refund of {total}." if paid else "")
        if by == "customer":
            admin = f"CANCELLED by customer {code} ({total}){' - REFUND DUE' if paid else ''}. {who}"
    elif event == "assigned":
        if rider:
            pay = f"Collect {total} cash." if o["pay_status"] == "cash_on_delivery" else "Already paid."
            rider_msg = (f"SwiftRun job {code}: pickup {_short(o['pickup'], 50)}; drop-off {_short(o['dropoff'], 50)}. "
                         f"Customer {who}. {pay}")
    else:
        return

    details = (f"Order {code}\nService: {svc}\nPickup: {o['pickup']}\nDrop-off: {o['dropoff']}\n"
               f"Total: {total}\nPayment: {'M-Pesa' if mpesa else 'Cash on delivery'} ({o['pay_status'].replace('_', ' ')})")
    track = f"Track your order: {FRONTEND_URL}/\n\n" if FRONTEND_URL else ""

    if cust:
        if NOTIFY_CUSTOMER_SMS:
            queue_notification(db, event, "sms", plus(o["cust_phone"]), cust, order_num=num, audience="customer")
        if NOTIFY_CUSTOMER_EMAIL and o["cust_email"]:
            first = (o["cust_name"].split() or ["there"])[0]
            queue_notification(
                db, event, "email", o["cust_email"], audience="customer", order_num=num,
                subject=f"SwiftRun order {code}: {headline}",
                body=f"Hello {first},\n\n{cust.replace('SwiftRun: ', '', 1)}\n\n{details}\n\n{track}"
                     f"Questions? Call or WhatsApp {SUPPORT_LINE}.\n\nSwiftRun")
    if admin:
        if NOTIFY_ADMIN_SMS:
            for p in admin_phones():
                queue_notification(db, event, "sms", plus(p), admin, order_num=num, audience="admin")
        if NOTIFY_ADMIN_EMAIL:
            for e in admin_emails():
                queue_notification(db, event, "email", e, audience="admin", order_num=num,
                                   subject=f"[SwiftRun] {code}: {headline}",
                                   body=f"{admin}\n\n{details}\nCustomer: {who}\nEmail: {o['cust_email'] or 'N/A'}")
    if rider_msg and NOTIFY_RIDER_SMS:
        rp = normalize_phone(rider["phone"])
        if rp:
            queue_notification(db, event, "sms", plus(rp), rider_msg, order_num=num, audience="rider")


def _send_sms_at(to, message):
    form = {"username": AT_USERNAME, "to": to, "message": message}
    if AT_SENDER_ID:
        form["from"] = AT_SENDER_ID
    req = urllib.request.Request(
        AT_BASE + "/version1/messaging", data=urllib.parse.urlencode(form).encode(),
        headers={"apiKey": AT_API_KEY, "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode() or "{}")
    recips = (data.get("SMSMessageData") or {}).get("Recipients") or []
    if not recips or str(recips[0].get("status")) != "Success":
        raise RuntimeError("SMS rejected: " + (recips[0].get("status", "no recipients") if recips else "no recipients"))


def _send_smtp(to, subject, body):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SMTP_FROM, to, subject or "SwiftRun"
    msg.set_content(body)
    if SMTP_SECURITY == "ssl":
        srv_ = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20, context=ssl.create_default_context())
    else:
        srv_ = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
    try:
        if SMTP_SECURITY == "starttls":
            srv_.starttls(context=ssl.create_default_context())
        if SMTP_USER:
            srv_.login(SMTP_USER, SMTP_PASSWORD)
        srv_.send_message(msg)
    finally:
        try:
            srv_.quit()
        except Exception:
            pass


def _deliver(n):
    """Send one notification. Returns the provider used."""
    if n["channel"] == "sms":
        if SMS_PROVIDER == "africastalking":
            _send_sms_at(n["recipient"], n["body"])
            return "africastalking"
        log.info("[SMS - log mode, not sent] to %s: %s", n["recipient"], n["body"])
        return "log"
    if n["channel"] == "email":
        if EMAIL_PROVIDER == "smtp":
            _send_smtp(n["recipient"], n["subject"], n["body"])
            return "smtp"
        log.info("[EMAIL - log mode, not sent] to %s | %s\n%s", n["recipient"], n["subject"], n["body"])
        return "log"
    raise ValueError("unknown channel " + str(n["channel"]))


def process_outbox_once(limit=20):
    """Send queued notifications that are due. Returns how many were attempted."""
    db, done = open_db(), 0
    try:
        rows = db.execute("SELECT * FROM notifications WHERE status='queued' AND next_try_at<=? ORDER BY id LIMIT ?",
                          (now_iso(), limit)).fetchall()
        for n in rows:
            if db.execute("UPDATE notifications SET status='sending' WHERE id=? AND status='queued'", (n["id"],)).rowcount != 1:
                continue
            db.commit()
            done += 1
            try:
                provider = _deliver(n)
                body = "[removed after sending]" if n["event"] in SENSITIVE_EVENTS and provider != "log" else n["body"]
                db.execute("UPDATE notifications SET status='sent', provider=?, sent_at=?, attempts=attempts+1, error=NULL, body=? WHERE id=?",
                           (provider, now_iso(), body, n["id"]))
            except Exception as e:  # network error, provider rejection, bad credentials...
                attempts = n["attempts"] + 1
                final = attempts >= MAX_SEND_ATTEMPTS
                log.warning("notification %s (%s to %s) failed, attempt %s: %s", n["id"], n["channel"], n["recipient"], attempts, e)
                db.execute("UPDATE notifications SET status=?, attempts=?, error=?, next_try_at=? WHERE id=?",
                           ("failed" if final else "queued", attempts, str(e)[:300], iso_in(60 * attempts), n["id"]))
            db.commit()
    finally:
        db.close()
    return done


_worker_started = False


def start_worker():
    global _worker_started
    if _worker_started or env("NOTIFY_WORKER", "1") == "0":
        return
    _worker_started = True

    def loop():
        time.sleep(1)
        while True:
            try:
                n = process_outbox_once()
            except Exception:
                log.exception("outbox worker error")
                n = 0
            time.sleep(0.3 if n else 2)

    threading.Thread(target=loop, daemon=True, name="outbox").start()
    log.info("Notifications: SMS=%s, email=%s", SMS_PROVIDER, EMAIL_PROVIDER)
    if SMS_PROVIDER == "log" or EMAIL_PROVIDER == "log":
        log.warning("Some notification channels are in LOG mode: messages are only written to this log, nobody receives them. "
                    "Set AT_USERNAME/AT_API_KEY (SMS) and SMTP_HOST (email) to send for real.")


# ── Pricing ─────────────────────────────────────────────────────────────────
def compute_quote(type_, goods_value, distance_km=None):
    if type_ not in BASE_FEE:
        raise ApiError("Unknown errand type")
    fee = BASE_FEE[type_]
    if distance_km is not None:
        try:
            km = max(0.0, min(float(distance_km), 100.0))
        except (TypeError, ValueError):
            raise ApiError("distanceKm must be a number")
        fee += PER_KM * km
    dist_fee = int(round(fee / 10.0)) * 10
    goods_fee = int(round(goods_value * GOODS_RATE))
    return {"distFee": dist_fee, "goodsFee": goods_fee, "total": dist_fee + goods_fee}


def resolve_type(d):
    sid = d.get("service")
    if sid:
        svc = SERVICES.get(sid) if isinstance(sid, str) else None
        if not svc:
            raise ApiError("Unknown service")
        return svc["type"], svc
    t = d.get("type")
    if t not in BASE_FEE:
        raise ApiError("Choose a service")
    return t, None


@app.get("/api/services")
def services():
    return jsonify(CATALOG)


@app.post("/api/quote")
@rate_limit("quote", 60)
def quote():
    d = body()
    t, _ = resolve_type(d)
    return jsonify(compute_quote(t, money(d, "goodsValue", "Goods value"), d.get("distanceKm")))


# ── Orders ──────────────────────────────────────────────────────────────────
@app.post("/api/orders")
@rate_limit("order", 10)
def create_order():
    d = body()
    name = text(d, "name", "Name", 2, 80)
    phone = normalize_phone(d.get("phone"))
    if not phone:
        raise ApiError("Enter a valid Kenyan phone number, e.g. 0712 345 678")
    email = text(d, "email", "Email", 3, 120, required=False).lower()
    if email and not EMAIL.match(email):
        raise ApiError("Enter a valid email address")
    descr = text(d, "desc", "Errand description", 3, 300)
    pickup = text(d, "pickup", "Pickup location", 3, 200)
    dropoff = text(d, "dropoff", "Drop-off location", 3, 200)
    notes = text(d, "notes", "Notes", 0, 1000, required=False)
    goods = money(d, "goodsValue", "Goods value")
    method = d.get("payMethod")
    if method == "card":
        raise ApiError("Card payments are not available yet. Please pay with M-Pesa or cash.")
    if method not in ("mpesa", "cash"):
        raise ApiError("Choose a payment method")
    type_, svc = resolve_type(d)
    q = compute_quote(type_, goods, d.get("distanceKm"))
    cat = CATEGORIES[svc["cat"]]["name"] if svc else None

    db = get_db()
    user = current_user()
    cash = method == "cash"
    cur = db.execute(
        """INSERT INTO orders(user_id,service_id,category,service,service_icon,type,descr,pickup,dropoff,status,
             pay_status,pay_method,goods_value,dist_fee,goods_fee,total,notes,created_at,cust_name,cust_phone,cust_email)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user["id"] if user else None, svc["id"] if svc else None, cat, svc["name"] if svc else None,
         svc["icon"] if svc else None, type_, descr, pickup, dropoff,
         "confirmed" if cash else "pending", "cash_on_delivery" if cash else "unpaid", method,
         goods, q["distFee"], q["goodsFee"], q["total"], notes, now_iso(), name, phone, email or None))
    num = cur.lastrowid
    steps = ["Order placed", "Cash payment on delivery" if cash else "Awaiting M-Pesa payment",
             "Rider assignment", "Pickup", "Delivery"]
    for i, label in enumerate(steps):
        db.execute("INSERT INTO timeline(order_num,idx,label,done,at) VALUES(?,?,?,?,?)",
                   (num, i, label, 1 if i == 0 else 0, now_iso() if i == 0 else None))
    notify_order(db, "created", num)
    db.commit()
    row = db.execute("SELECT * FROM orders WHERE num=?", (num,)).fetchone()
    return jsonify(order=orders_out(db, [row])[0]), 201


@app.get("/api/orders")
@require_auth
def my_orders():
    u = current_user()
    rows = get_db().execute(
        "SELECT * FROM orders WHERE user_id=? OR cust_email=? ORDER BY num DESC LIMIT 200",
        (u["id"], u["email"])).fetchall()
    return jsonify(orders=orders_out(get_db(), rows))


@app.get("/api/orders/track")
@rate_limit("track", 30)
def track():
    q = (request.args.get("q") or "").strip()[:120]
    if not q:
        raise ApiError("Enter an order ID, phone number or email")
    db = get_db()
    if "@" in q:
        rows = db.execute("SELECT * FROM orders WHERE cust_email=? ORDER BY num DESC LIMIT 50", (q.lower(),)).fetchall()
    elif re.fullmatch(r"(?i)sr-?\d+", q) or (q.isdigit() and len(q) <= 6):
        rows = db.execute("SELECT * FROM orders WHERE num=?", (parse_order_id(q),)).fetchall()
    else:
        phone = normalize_phone(q)
        if not phone:
            raise ApiError("Enter an order ID (SR-1023), a Kenyan phone number or an email")
        rows = db.execute("SELECT * FROM orders WHERE cust_phone=? ORDER BY num DESC LIMIT 50", (phone,)).fetchall()
    return jsonify(orders=orders_out(db, rows))


@app.get("/api/orders/<order_id>")
def get_order(order_id):
    db = get_db()
    o = load_order(db, order_id)
    u = current_user()
    if not (is_admin(u) or is_owner(o, u)):
        raise ApiError("Order not found", 404)
    return jsonify(order=orders_out(db, [o])[0])


@app.post("/api/orders/<order_id>/cancel")
@rate_limit("cancel", 20)
def cancel_order(order_id):
    db = get_db()
    o = load_order(db, order_id)
    u = current_user()
    if not (is_admin(u) or is_owner(o, u)):
        phone = normalize_phone(body().get("phone"))
        if not phone or not hmac.compare_digest(phone, o["cust_phone"]):
            raise ApiError("Order not found", 404)
    do_cancel(db, o, by="admin" if is_admin(u) else "customer")
    db.commit()
    return jsonify(order=orders_out(db, [load_order(db, order_id)])[0])


def do_cancel(db, o, by="customer"):
    if o["status"] not in ("pending", "confirmed"):
        raise ApiError(f"A {o['status']} order can't be cancelled", 409)
    paid = o["pay_status"] == "paid"
    db.execute("UPDATE orders SET status='cancelled' WHERE num=?", (o["num"],))
    # Leave a trace on the timeline; refunds are handled manually by an admin
    db.execute("UPDATE timeline SET label=? WHERE order_num=? AND idx=4",
               ("Cancelled — refund due" if paid else "Cancelled", o["num"]))
    # Stop a pending STK prompt from confirming a cancelled order
    db.execute("UPDATE payments SET status='cancelled' WHERE order_num=? AND status='pending'", (o["num"],))
    notify_order(db, "cancelled", o["num"], by=by)


# ── M-Pesa (Daraja) ─────────────────────────────────────────────────────────
_mpesa_token = {"value": None, "exp": 0}


def _mpesa_base():
    if env("MPESA_BASE_URL"):      # override, mainly for tests
        return env("MPESA_BASE_URL").rstrip("/")
    return "https://api.safaricom.co.ke" if MPESA_MODE == "production" else "https://sandbox.safaricom.co.ke"


def _http_json(url, payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        log.error("Daraja HTTP %s: %s", e.code, e.read()[:300])
        raise ApiError("M-Pesa is unavailable right now. Please try again.", 502)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        log.error("Daraja error: %s", e)
        raise ApiError("M-Pesa is unavailable right now. Please try again.", 502)


def _http_raw(url, payload=None, headers=None):
    """Like _http_json but returns (status, body) so Daraja's error bodies can be read."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw or "{}")
        except ValueError:
            return e.code, {"raw": raw[:200]}


def _access_token():
    if _mpesa_token["value"] and time.time() < _mpesa_token["exp"] - 30:
        return _mpesa_token["value"]
    key, sec = env("MPESA_CONSUMER_KEY"), env("MPESA_CONSUMER_SECRET")
    if not key or not sec:
        raise ApiError("M-Pesa is not configured on the server", 503)
    basic = base64.b64encode(f"{key}:{sec}".encode()).decode()
    r = _http_json(_mpesa_base() + "/oauth/v1/generate?grant_type=client_credentials",
                   headers={"Authorization": "Basic " + basic})
    _mpesa_token.update(value=r.get("access_token"), exp=time.time() + int(r.get("expires_in", 3000)))
    if not _mpesa_token["value"]:
        raise ApiError("M-Pesa authentication failed", 502)
    return _mpesa_token["value"]


def stk_push(phone, amount, order_num):
    """Send the STK prompt. Returns the CheckoutRequestID."""
    if MPESA_MODE == "mock":
        return "MOCK-" + secrets.token_hex(8)
    shortcode, passkey, cb_base = env("MPESA_SHORTCODE"), env("MPESA_PASSKEY"), env("MPESA_CALLBACK_BASE").rstrip("/")
    if not (shortcode and passkey and cb_base.startswith("https://")):
        raise ApiError("M-Pesa is not configured on the server", 503)
    ts = datetime.now(TZ).strftime("%Y%m%d%H%M%S")
    payload = {
        "BusinessShortCode": shortcode,
        "Password": base64.b64encode((shortcode + passkey + ts).encode()).decode(),
        "Timestamp": ts, "TransactionType": env("MPESA_TXN_TYPE", "CustomerPayBillOnline"),
        "Amount": amount, "PartyA": phone, "PartyB": env("MPESA_PARTYB", shortcode), "PhoneNumber": phone,
        "CallBackURL": f"{cb_base}/api/mpesa/callback/{CALLBACK_SECRET}",
        "AccountReference": f"SR-{order_num}", "TransactionDesc": f"SwiftRun SR-{order_num}",
    }
    r = _http_json(_mpesa_base() + "/mpesa/stkpush/v1/processrequest", payload,
                   {"Authorization": "Bearer " + _access_token(), "Content-Type": "application/json"})
    if str(r.get("ResponseCode")) != "0" or not r.get("CheckoutRequestID"):
        log.error("STK push rejected: %s", r)
        raise ApiError("M-Pesa could not send the payment prompt. Check the phone number and try again.", 502)
    return r["CheckoutRequestID"]


class QueryUnavailable(Exception):
    """Safaricom couldn't tell us what happened to a payment right now."""


def stk_query(checkout_id):
    """Ask Safaricom what happened to an STK push. Returns (state, message), state in completed|failed|pending.
    Raises QueryUnavailable when no answer can be had. The answer has no receipt number or amount."""
    if MPESA_MODE == "mock":
        return "pending", "Mock mode: payments finish on their own"
    shortcode, passkey = env("MPESA_SHORTCODE"), env("MPESA_PASSKEY")
    if not (shortcode and passkey):
        raise QueryUnavailable("M-Pesa is not configured on the server")
    for attempt in (1, 2):
        ts = datetime.now(TZ).strftime("%Y%m%d%H%M%S")
        payload = {"BusinessShortCode": shortcode, "Timestamp": ts, "CheckoutRequestID": checkout_id,
                   "Password": base64.b64encode((shortcode + passkey + ts).encode()).decode()}
        try:
            code, r = _http_raw(_mpesa_base() + "/mpesa/stkpushquery/v1/query", payload,
                                {"Authorization": "Bearer " + _access_token(), "Content-Type": "application/json"})
        except ApiError as e:                      # token request failed
            raise QueryUnavailable(e.msg)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
            raise QueryUnavailable(str(e))
        if code == 401 and attempt == 1:           # cached token went stale
            _mpesa_token.update(value=None, exp=0)
            continue
        break
    if "being processed" in str(r.get("errorMessage") or "").lower():
        return "pending", "The customer has not finished the M-Pesa prompt yet"
    if code == 200 and str(r.get("ResponseCode")) == "0" and "ResultCode" in r:
        desc = str(r.get("ResultDesc") or "")[:200]
        if str(r["ResultCode"]) == "0":
            return "completed", desc
        return "failed", desc or f"Payment not completed (code {r['ResultCode']})"
    raise QueryUnavailable(f"Unexpected answer from Safaricom (HTTP {code})")


def complete_payment(db, pay, ref, source="callback", confirmed_by=None, note=None):
    """Mark a payment successful and confirm the order. Safe to call twice.
    source: callback | query (Safaricom status check, no receipt) | manual (an admin) | mock."""
    if pay["status"] == "completed":
        return
    num = pay["order_num"]
    already_paid = db.execute("SELECT 1 FROM payments WHERE order_num=? AND status='completed' AND id<>?",
                              (num, pay["id"])).fetchone()
    db.execute("UPDATE payments SET status='completed', ref=?, completed_at=?, source=?, confirmed_by=?, note=?, failure=NULL WHERE id=?",
               (ref, now_iso(), source, confirmed_by, note, pay["id"]))
    o = db.execute("SELECT * FROM orders WHERE num=?", (num,)).fetchone()
    if already_paid:                      # the customer paid twice
        log.warning("Second payment %s for order SR-%s: refund needed", ref, num)
        notify_order(db, "paid_duplicate", num, ref=ref)
        return
    if o["status"] == "cancelled":
        log.warning("Payment %s received for cancelled order SR-%s: refund needed", ref, num)
        db.execute("UPDATE orders SET pay_status='paid', mpesa_ref=? WHERE num=?", (ref, num))
        notify_order(db, "paid_cancelled", num)
        return
    db.execute("UPDATE orders SET pay_status='paid', mpesa_ref=?, status=CASE WHEN status='pending' THEN 'confirmed' ELSE status END WHERE num=?",
               (ref, num))
    set_step(db, num, 1, f"Payment confirmed by admin (M-Pesa ref {ref})" if source == "manual" else "Payment confirmed via M-Pesa")
    notify_order(db, "paid", num, by="admin" if source == "manual" else None)
    if not o["rider_id"]:
        assign_rider(db, num)


def reconcile_payment(db, pay):
    """Ask Safaricom about one payment and apply the answer. Returns (state, message). May raise QueryUnavailable."""
    state, msg = stk_query(pay["checkout_id"])
    db.execute("UPDATE payments SET last_checked_at=?, check_count=check_count+1 WHERE id=?", (now_iso(), pay["id"]))
    pay = db.execute("SELECT * FROM payments WHERE id=?", (pay["id"],)).fetchone()
    if state == "completed":
        complete_payment(db, pay, None, source="query")
    elif state == "failed" and pay["status"] in ("pending", "expired"):
        db.execute("UPDATE payments SET status='failed', failure=? WHERE id=?", (msg[:200], pay["id"]))
    return state, msg


def maybe_reconcile(db, pay):
    """Check a still-pending payment if it has waited long enough and wasn't checked a moment ago."""
    if (MPESA_MODE == "mock" or pay["status"] != "pending" or not pay["checkout_id"]
            or pay["created_at"] > iso_in(-RECONCILE_MIN_AGE)
            or (pay["last_checked_at"] and pay["last_checked_at"] > iso_in(-RECONCILE_RECHECK))):
        return None
    try:
        return reconcile_payment(db, pay)[0]
    except QueryUnavailable as e:
        log.warning("Couldn't check payment %s with Safaricom: %s", pay["id"], e)
        db.execute("UPDATE payments SET last_checked_at=? WHERE id=?", (now_iso(), pay["id"]))
        return None


def expire_order(db, o):
    """Cancel an M-Pesa order that was never paid."""
    num = o["num"]
    if db.execute("UPDATE orders SET status='cancelled' WHERE num=? AND status='pending' AND pay_status='unpaid'", (num,)).rowcount != 1:
        return False
    db.execute("UPDATE timeline SET label='Payment not received' WHERE order_num=? AND idx=1", (num,))
    db.execute("UPDATE timeline SET label='Cancelled — payment not received' WHERE order_num=? AND idx=4", (num,))
    db.execute("UPDATE payments SET status='expired', failure=COALESCE(failure,'Order expired') WHERE order_num=? AND status='pending'", (num,))
    notify_order(db, "expired", num)
    return True


def reconcile_once():
    """Housekeeping pass: settle payments whose callback never came, then cancel orders nobody paid for.
    Returns how many things it looked at."""
    db, work = open_db(), 0
    try:
        if MPESA_MODE != "mock":
            rows = db.execute(
                "SELECT * FROM payments WHERE status='pending' AND checkout_id IS NOT NULL AND created_at<=? "
                "AND (last_checked_at IS NULL OR last_checked_at<=?) ORDER BY id LIMIT 25",
                (iso_in(-RECONCILE_MIN_AGE), iso_in(-RECONCILE_RECHECK))).fetchall()
            for p in rows:
                work += 1
                try:
                    reconcile_payment(db, p)
                except QueryUnavailable as e:
                    log.warning("Couldn't check payment %s with Safaricom: %s", p["id"], e)
                    db.execute("UPDATE payments SET last_checked_at=? WHERE id=?", (now_iso(), p["id"]))
                db.commit()
            # nobody confirmed it within the window: close it (a late callback can still complete it)
            db.execute("UPDATE payments SET status='expired', failure='No confirmation received' WHERE status='pending' AND created_at<=?",
                       (iso_in(-PAYMENT_WINDOW_MIN * 60),))
            db.commit()
        if ORDER_EXPIRY_MINUTES > 0:
            stale = db.execute(
                "SELECT * FROM orders WHERE status='pending' AND pay_status='unpaid' AND pay_method='mpesa' AND created_at<=? ORDER BY num LIMIT 50",
                (iso_in(-ORDER_EXPIRY_MINUTES * 60),)).fetchall()
            give_up_after = iso_in(-(ORDER_EXPIRY_MINUTES + 120) * 60)
            for o in stale:
                work += 1
                verified = True
                if MPESA_MODE != "mock":    # never cancel an order that was actually paid
                    attempts = db.execute(
                        "SELECT * FROM payments WHERE order_num=? AND checkout_id IS NOT NULL AND status IN ('pending','expired','cancelled') "
                        "ORDER BY id DESC LIMIT 5", (o["num"],)).fetchall()
                    for p in attempts:
                        try:
                            if reconcile_payment(db, p)[0] == "completed":
                                break
                        except QueryUnavailable as e:
                            verified = False
                            log.warning("Can't verify payment for SR-%s before expiring it: %s", o["num"], e)
                    db.commit()
                fresh = db.execute("SELECT * FROM orders WHERE num=?", (o["num"],)).fetchone()
                if fresh["pay_status"] == "unpaid" and (verified or o["created_at"] <= give_up_after):
                    expire_order(db, fresh)
                    db.commit()
    finally:
        db.close()
    return work


_reconciler_started = False


def start_reconciler():
    global _reconciler_started
    if _reconciler_started or env("RECONCILE_WORKER", "1") == "0":
        return
    _reconciler_started = True

    def loop():
        time.sleep(5)
        while True:
            try:
                reconcile_once()
            except Exception:
                log.exception("payment reconciler error")
            time.sleep(RECONCILE_INTERVAL)

    threading.Thread(target=loop, daemon=True, name="reconciler").start()
    log.info("Payment reconciler running (orders expire after %s min unpaid, mode=%s)", ORDER_EXPIRY_MINUTES, MPESA_MODE)


def _mock_finish(checkout_id):
    def run():
        time.sleep(MPESA_MOCK_DELAY)
        db = open_db()
        try:
            p = db.execute("SELECT * FROM payments WHERE checkout_id=?", (checkout_id,)).fetchone()
            if p and p["status"] == "pending":
                complete_payment(db, p, "MOCK" + secrets.token_hex(4).upper())
                db.commit()
        except Exception:
            log.exception("mock payment failed")
        finally:
            db.close()
    threading.Thread(target=run, daemon=True).start()


@app.post("/api/orders/<order_id>/pay/mpesa")
@rate_limit("pay", 6)
def pay_mpesa(order_id):
    db = get_db()
    o = load_order(db, order_id)
    if o["pay_status"] == "paid":
        raise ApiError("This order is already paid", 409)
    if o["status"] in ("cancelled", "completed"):
        raise ApiError(f"A {o['status']} order can't be paid", 409)
    if o["pay_attempts"] >= 5:
        raise ApiError("Too many payment attempts for this order. Please contact support.", 429)
    phone = normalize_phone(body().get("phone") or o["cust_phone"])
    if not phone:
        raise ApiError("Enter a valid M-Pesa phone number")
    if MPESA_MODE != "mock":
        # The customer may have paid an earlier prompt whose confirmation never reached us: ask Safaricom first
        earlier = db.execute("SELECT * FROM payments WHERE order_num=? AND checkout_id IS NOT NULL AND status IN ('pending','expired','cancelled') "
                             "ORDER BY id DESC LIMIT 3", (o["num"],)).fetchall()
        for p in earlier:
            try:
                paid_already = reconcile_payment(db, p)[0] == "completed"
            except QueryUnavailable:
                continue
            if paid_already:
                db.commit()
                raise ApiError("Your earlier M-Pesa payment went through, so this order is already paid. Check My Orders.", 409)
        db.commit()
    db.execute("UPDATE payments SET status='cancelled' WHERE order_num=? AND status='pending'", (o["num"],))
    checkout_id = stk_push(phone, o["total"], o["num"])
    db.execute("INSERT INTO payments(order_num,method,phone,amount,status,checkout_id,created_at) VALUES(?,?,?,?,?,?,?)",
               (o["num"], "M-Pesa", plus(phone), o["total"], "pending", checkout_id, now_iso()))
    db.execute("UPDATE orders SET pay_method='mpesa', pay_attempts=pay_attempts+1 WHERE num=?", (o["num"],))
    db.commit()
    if MPESA_MODE == "mock":
        _mock_finish(checkout_id)
    return jsonify(state="pending", mode=MPESA_MODE, message=f"Payment prompt sent to {plus(phone)}"), 202


@app.get("/api/orders/<order_id>/payment")
def payment_state(order_id):
    db = get_db()
    o = load_order(db, order_id)
    p = db.execute("SELECT * FROM payments WHERE order_num=? ORDER BY id DESC LIMIT 1", (o["num"],)).fetchone()
    if p and p["status"] == "pending" and maybe_reconcile(db, p) is not None:
        db.commit()
        o = load_order(db, order_id)
        p = db.execute("SELECT * FROM payments WHERE id=?", (p["id"],)).fetchone()
    state = "none" if not p else p["status"]
    return jsonify(state=state, payStatus=o["pay_status"], status=o["status"], mpesaRef=o["mpesa_ref"],
                   message=(p["failure"] if p and p["failure"] else ""))


@app.post("/api/mpesa/callback/<secret>")
def mpesa_callback(secret):
    # Safaricom doesn't sign callbacks, so the URL itself carries a secret.
    if not hmac.compare_digest(secret, CALLBACK_SECRET):
        return jsonify(error="Not found"), 404
    cb = ((body().get("Body") or {}).get("stkCallback") or {})
    cid = cb.get("CheckoutRequestID")
    db = get_db()
    pay = db.execute("SELECT * FROM payments WHERE checkout_id=?", (cid,)).fetchone() if cid else None
    ok = {"ResultCode": 0, "ResultDesc": "Accepted"}
    if not pay:
        return jsonify(ok)
    if pay["status"] == "completed":
        # a status check already confirmed it; the callback now brings the M-Pesa receipt number
        meta0 = {i.get("Name"): i.get("Value") for i in (cb.get("CallbackMetadata") or {}).get("Item", [])}
        late_ref = str(meta0.get("MpesaReceiptNumber") or "")
        if str(cb.get("ResultCode")) == "0" and late_ref and not pay["ref"]:
            db.execute("UPDATE payments SET ref=? WHERE id=?", (late_ref, pay["id"]))
            db.execute("UPDATE orders SET mpesa_ref=? WHERE num=? AND mpesa_ref IS NULL", (late_ref, pay["order_num"]))
            db.commit()
        return jsonify(ok)
    if str(cb.get("ResultCode")) != "0":
        db.execute("UPDATE payments SET status=?, failure=? WHERE id=?",
                   ("failed", str(cb.get("ResultDesc", "Payment was not completed"))[:200], pay["id"]))
        db.commit()
        return jsonify(ok)
    meta = {i.get("Name"): i.get("Value") for i in (cb.get("CallbackMetadata") or {}).get("Item", [])}
    paid, ref = meta.get("Amount"), str(meta.get("MpesaReceiptNumber") or "")
    if not ref or paid is None or int(float(paid)) != pay["amount"]:
        log.error("M-Pesa callback mismatch for %s: paid=%s ref=%s expected=%s", cid, paid, ref, pay["amount"])
        db.execute("UPDATE payments SET status='failed', failure=?, ref=? WHERE id=?",
                   ("Amount mismatch, needs manual review", ref or None, pay["id"]))
        notify_order(db, "payment_review", pay["order_num"], ref=ref or None,
                     extra=f"M-Pesa reported KES {paid if paid is not None else '?'} but the order total is KES {pay['amount']:,}. Check the M-Pesa statement")
    else:
        complete_payment(db, pay, ref)
    db.commit()
    return jsonify(ok)


# ── Admin ───────────────────────────────────────────────────────────────────
@app.get("/api/admin/orders")
@require_admin
def admin_orders():
    db = get_db()
    return jsonify(orders=orders_out(db, db.execute("SELECT * FROM orders ORDER BY num DESC LIMIT 500").fetchall()))


@app.patch("/api/admin/orders/<order_id>")
@require_admin
def admin_update_order(order_id):
    d = body()
    action = d.get("action")
    db = get_db()
    o = load_order(db, order_id)
    num, st = o["num"], o["status"]

    def need(*allowed):
        if st not in allowed:
            raise ApiError(f"Can't {action} a {st} order", 409)

    def need_paid():
        if o["pay_method"] == "mpesa" and o["pay_status"] != "paid":
            raise ApiError('This M-Pesa order has not been paid yet. Use "Check payment" to ask Safaricom, '
                           'or "Mark paid" once you have seen the M-Pesa confirmation.', 409)

    if action == "confirm":
        need_paid()
        need("pending")
        db.execute("UPDATE orders SET status='confirmed' WHERE num=?", (num,))
        notify_order(db, "confirmed", num)
    elif action == "assign":
        need_paid()
        need("pending", "confirmed", "in-transit")
        if not assign_rider(db, num, d.get("riderId")):
            raise ApiError("No active rider available", 409)
    elif action == "dispatch":
        need_paid()
        need("confirmed")
        if d.get("riderId") or not o["rider_id"]:
            if not assign_rider(db, num, d.get("riderId")):
                raise ApiError("No active rider available", 409)
        set_step(db, num, 3, "Rider picked up the errand")
        db.execute("UPDATE orders SET status='in-transit' WHERE num=?", (num,))
        notify_order(db, "dispatched", num)
    elif action == "complete":
        need("in-transit")
        set_step(db, num, 3, None)
        set_step(db, num, 4, "Delivered successfully")
        db.execute("UPDATE orders SET status='completed' WHERE num=?", (num,))
        if o["pay_status"] == "cash_on_delivery":
            db.execute("UPDATE orders SET pay_status='paid' WHERE num=?", (num,))
            set_step(db, num, 1, "Cash collected on delivery")
            db.execute("INSERT INTO payments(order_num,method,phone,amount,status,ref,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?)",
                       (num, "Cash", plus(o["cust_phone"]), o["total"], "completed", f"CASH-{num}", now_iso(), now_iso()))
        if o["rider_id"]:
            db.execute("UPDATE riders SET trips=trips+1, earnings=earnings+? WHERE id=?",
                       (int(round(o["dist_fee"] * RIDER_SHARE)), o["rider_id"]))
        notify_order(db, "completed", num)
    elif action == "cancel":
        do_cancel(db, o, by="admin")
    else:
        raise ApiError("Unknown action")
    db.commit()
    return jsonify(order=orders_out(db, [load_order(db, order_id)])[0])


@app.post("/api/admin/orders/<order_id>/payment/check")
@require_admin
@rate_limit("paycheck", 30)
def admin_check_payment(order_id):
    """Ask Safaricom what happened to this order's M-Pesa prompt(s)."""
    db = get_db()
    o = load_order(db, order_id)
    if o["pay_status"] == "paid":
        return jsonify(state="completed", message="This order is already paid.", order=orders_out(db, [o])[0])
    if o["pay_method"] != "mpesa":
        raise ApiError("This is not an M-Pesa order", 409)
    attempts = db.execute(
        "SELECT * FROM payments WHERE order_num=? AND checkout_id IS NOT NULL "
        "AND (status IN ('pending','expired','cancelled') OR (status='failed' AND COALESCE(failure,'') NOT LIKE 'Amount mismatch%')) "
        "ORDER BY id DESC LIMIT 3", (o["num"],)).fetchall()
    if not attempts:
        return jsonify(state="none", message="No M-Pesa prompt was ever sent for this order, so there is nothing to check.",
                       order=orders_out(db, [o])[0])
    state, msg = "unknown", ""
    try:
        for i, p in enumerate(attempts):          # newest first; stop as soon as one is paid
            st, m = reconcile_payment(db, p)
            if i == 0 or st == "completed":
                state, msg = st, m
            if st == "completed":
                break
    except QueryUnavailable as e:
        state, msg = "unknown", f"Could not reach Safaricom: {e}"
    db.commit()
    text_ = {"completed": "Safaricom confirms the payment went through. The order is now paid.",
             "failed": f"Safaricom says the payment did not go through: {msg}",
             "pending": "Safaricom says the customer has not finished the M-Pesa prompt yet.",
             "unknown": msg}[state]
    return jsonify(state=state, message=text_, order=orders_out(db, [load_order(db, order_id)])[0])


@app.post("/api/admin/orders/<order_id>/mark-paid")
@require_admin
@rate_limit("markpaid", 30)
def admin_mark_paid(order_id):
    """Record an M-Pesa payment by hand, e.g. after checking the M-Pesa statement."""
    d, db, u = body(), get_db(), current_user()
    o = load_order(db, order_id)
    if o["pay_method"] != "mpesa":
        raise ApiError("Only M-Pesa orders can be marked paid. Cash is recorded when the order is completed.", 409)
    if o["pay_status"] == "paid":
        raise ApiError("This order is already paid", 409)
    if o["status"] in ("cancelled", "completed"):
        raise ApiError(f"A {o['status']} order can't be marked paid. Refund the customer, or ask them to place a new order.", 409)
    ref = str(d.get("ref") or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{8,15}", ref):
        raise ApiError("Enter the M-Pesa receipt code exactly as in the confirmation message (8 to 15 letters and numbers, e.g. RDE3XYZ12A)")
    if (db.execute("SELECT 1 FROM payments WHERE ref=? AND status='completed'", (ref,)).fetchone()
            or db.execute("SELECT 1 FROM orders WHERE mpesa_ref=?", (ref,)).fetchone()):
        raise ApiError("That M-Pesa code is already recorded on an order", 409)
    note = text(d, "note", "Note", 0, 200, required=False)
    pay = db.execute("SELECT * FROM payments WHERE order_num=? AND status IN ('pending','expired') ORDER BY id DESC LIMIT 1", (o["num"],)).fetchone()
    if not pay:
        cur = db.execute("INSERT INTO payments(order_num,method,phone,amount,status,created_at) VALUES(?,?,?,?,?,?)",
                         (o["num"], "M-Pesa", plus(o["cust_phone"]), o["total"], "pending", now_iso()))
        pay = db.execute("SELECT * FROM payments WHERE id=?", (cur.lastrowid,)).fetchone()
    complete_payment(db, pay, ref, source="manual", confirmed_by=u["id"], note=note or None)
    db.commit()
    return jsonify(order=orders_out(db, [load_order(db, order_id)])[0])


@app.get("/api/admin/riders")
@require_admin
def admin_riders():
    return jsonify(riders=[rider_admin(r) for r in get_db().execute("SELECT * FROM riders ORDER BY id")])


def _rider_fields(d, partial=False):
    out = {}
    if not partial or "name" in d:
        out["name"] = text(d, "name", "Name", 2, 80)
    if not partial or "phone" in d:
        p = normalize_phone(d.get("phone"))
        if not p:
            raise ApiError("Enter a valid Kenyan phone number")
        out["phone"] = plus(p)
    if "vehicle" in d or not partial:
        out["vehicle"] = text(d, "vehicle", "Vehicle", 0, 40, required=False) or "Motorbike"
    if "plate" in d:
        out["plate"] = text(d, "plate", "Plate", 0, 20, required=False) or None
    if "area" in d:
        out["area"] = text(d, "area", "Area", 0, 80, required=False) or None
    if "status" in d:
        if d["status"] not in ("active", "offline"):
            raise ApiError("Status must be active or offline")
        out["status"] = d["status"]
    return out


@app.post("/api/admin/riders")
@require_admin
def admin_add_rider():
    f = _rider_fields(body())
    db = get_db()
    n = (db.execute("SELECT MAX(CAST(SUBSTR(id,2) AS INTEGER)) m FROM riders").fetchone()["m"] or 0) + 1
    rid = f"R{n:03d}"
    db.execute("INSERT INTO riders(id,name,phone,vehicle,plate,area,status) VALUES(?,?,?,?,?,?,?)",
               (rid, f["name"], f["phone"], f["vehicle"], f.get("plate"), f.get("area"), f.get("status", "active")))
    db.commit()
    return jsonify(rider=rider_admin(db.execute("SELECT * FROM riders WHERE id=?", (rid,)).fetchone())), 201


@app.patch("/api/admin/riders/<rid>")
@require_admin
def admin_edit_rider(rid):
    db = get_db()
    if not db.execute("SELECT 1 FROM riders WHERE id=?", (rid,)).fetchone():
        raise ApiError("Rider not found", 404)
    f = _rider_fields(body(), partial=True)
    if f:
        sets = ",".join(f"{k}=?" for k in f)  # keys come from _rider_fields, never from the client
        db.execute(f"UPDATE riders SET {sets} WHERE id=?", [*f.values(), rid])
        db.commit()
    return jsonify(rider=rider_admin(db.execute("SELECT * FROM riders WHERE id=?", (rid,)).fetchone()))


@app.delete("/api/admin/riders/<rid>")
@require_admin
def admin_delete_rider(rid):
    db = get_db()
    if db.execute("SELECT 1 FROM orders WHERE rider_id=?", (rid,)).fetchone():
        raise ApiError("This rider has order history. Set them offline instead.", 409)
    db.execute("DELETE FROM riders WHERE id=?", (rid,))
    db.commit()
    return jsonify(ok=True)


@app.get("/api/admin/payments")
@require_admin
def admin_payments():
    rows = get_db().execute("SELECT * FROM payments ORDER BY id DESC LIMIT 500").fetchall()
    return jsonify(payments=[{
        "id": f"PAY-{r['id']:03d}", "orderId": f"SR-{r['order_num']}",
        "method": r["method"] + {"manual": " (manual)", "query": " (verified)"}.get(r["source"], ""),
        "phone": r["phone"] or "—", "amount": r["amount"], "status": r["status"],
        "ref": r["ref"] or "—", "date": local_dt(r["completed_at"] or r["created_at"]),
    } for r in rows])


@app.get("/api/admin/pulse")
@require_admin
def admin_pulse():
    """Cheap numbers the dashboard polls to spot new orders and payments."""
    r = get_db().execute(
        """SELECT COALESCE(MAX(num),0) latest,
                  COALESCE(SUM(status='pending'),0) pending,
                  COALESCE(SUM(pay_status='paid'),0) paid FROM orders""").fetchone()
    failed = get_db().execute("SELECT COUNT(*) c FROM notifications WHERE status='failed'").fetchone()["c"]
    return jsonify(latest=r["latest"], pending=r["pending"], paid=r["paid"], failedNotifications=failed)


@app.get("/api/admin/notifications")
@require_admin
def admin_notifications():
    rows = get_db().execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 200").fetchall()
    out = []
    for n in rows:
        sensitive = n["event"] in SENSITIVE_EVENTS
        out.append({
            "id": n["id"], "event": n["event"], "channel": n["channel"], "audience": n["audience"],
            "to": n["recipient"], "subject": n["subject"] or "",
            "preview": "[hidden]" if sensitive else _short(n["body"], 140),
            "order": f"SR-{n['order_num']}" if n["order_num"] else "", "status": n["status"],
            "provider": n["provider"] or "", "attempts": n["attempts"], "error": n["error"] or "",
            "createdAt": local_dt(n["created_at"]), "sentAt": local_dt(n["sent_at"]),
        })
    return jsonify(notifications=out, smsMode=SMS_PROVIDER, emailMode=EMAIL_PROVIDER)


@app.post("/api/admin/notifications/<int:nid>/retry")
@require_admin
def admin_retry_notification(nid):
    db = get_db()
    cur = db.execute("UPDATE notifications SET status='queued', attempts=0, error=NULL, next_try_at=? WHERE id=? AND status='failed'",
                     (now_iso(), nid))
    db.commit()
    if cur.rowcount != 1:
        raise ApiError("Only failed notifications can be retried", 409)
    return jsonify(ok=True)


# ── Misc + static frontend ──────────────────────────────────────────────────
@app.get("/api/health")
def health():
    get_db().execute("SELECT 1")
    return jsonify(ok=True, mpesaMode=MPESA_MODE)


STATIC_EXT = {".html", ".js", ".css", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".webp", ".webmanifest", ".woff2"}


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/<path:name>")
def static_files(name):
    # Only top-level front-end files; never the backend folder, .env or the database
    if "/" in name or name.startswith(".") or Path(name).suffix.lower() not in STATIC_EXT:
        return jsonify(error="Not found"), 404
    return send_from_directory(STATIC_DIR, name)


init_db()
start_worker()
start_reconciler()

if __name__ == "__main__":
    app.run(host=env("HOST", "127.0.0.1"), port=int(env("PORT", "8000")), debug=env("DEBUG") == "1")
