"""SwiftRun backend: Flask + SQLite.

Run:   python3 app.py            (dev)
       gunicorn -w 2 -b 0.0.0.0:8000 app:app     (production)

Everything is configured through environment variables or a .env file
(see .env.example). Only Flask is required from pip.
"""
import base64
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
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
  created_at TEXT NOT NULL);
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
  created_at TEXT NOT NULL, completed_at TEXT);
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
def make_token(user_id):
    return signer.dumps({"uid": user_id})


def current_user():
    if hasattr(g, "_user"):
        return g._user
    g._user = None
    h = request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        try:
            uid = signer.loads(h[7:], max_age=TOKEN_MAX_AGE)["uid"]
            g._user = get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
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
    return jsonify(token=make_token(u["id"]), user=user_dict(u)), 201


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
    return jsonify(token=make_token(u["id"]), user=user_dict(u))


@app.get("/api/auth/me")
@require_auth
def me():
    return jsonify(user=user_dict(current_user()))


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
    return r


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
    do_cancel(db, o)
    db.commit()
    return jsonify(order=orders_out(db, [load_order(db, order_id)])[0])


def do_cancel(db, o):
    if o["status"] not in ("pending", "confirmed"):
        raise ApiError(f"A {o['status']} order can't be cancelled", 409)
    paid = o["pay_status"] == "paid"
    db.execute("UPDATE orders SET status='cancelled' WHERE num=?", (o["num"],))
    # Leave a trace on the timeline; refunds are handled manually by an admin
    db.execute("UPDATE timeline SET label=? WHERE order_num=? AND idx=4",
               ("Cancelled — refund due" if paid else "Cancelled", o["num"]))
    # Stop a pending STK prompt from confirming a cancelled order
    db.execute("UPDATE payments SET status='cancelled' WHERE order_num=? AND status='pending'", (o["num"],))


# ── M-Pesa (Daraja) ─────────────────────────────────────────────────────────
_mpesa_token = {"value": None, "exp": 0}


def _mpesa_base():
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


def complete_payment(db, pay, ref):
    """Mark a payment successful and confirm the order. Safe to call twice."""
    if pay["status"] == "completed":
        return
    num = pay["order_num"]
    now = now_iso()
    db.execute("UPDATE payments SET status='completed', ref=?, completed_at=? WHERE id=?", (ref, now, pay["id"]))
    o = db.execute("SELECT * FROM orders WHERE num=?", (num,)).fetchone()
    if o["status"] == "cancelled":
        log.warning("Payment %s received for cancelled order SR-%s: refund needed", ref, num)
        db.execute("UPDATE orders SET pay_status='paid', mpesa_ref=? WHERE num=?", (ref, num))
        return
    db.execute("UPDATE orders SET pay_status='paid', mpesa_ref=?, status=CASE WHEN status='pending' THEN 'confirmed' ELSE status END WHERE num=?",
               (ref, num))
    set_step(db, num, 1, "Payment confirmed via M-Pesa")
    if not o["rider_id"]:
        assign_rider(db, num)


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
    if not pay or pay["status"] in ("completed",):
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

    if action == "confirm":
        need("pending")
        db.execute("UPDATE orders SET status='confirmed' WHERE num=?", (num,))
    elif action == "assign":
        need("pending", "confirmed", "in-transit")
        if not assign_rider(db, num, d.get("riderId")):
            raise ApiError("No active rider available", 409)
    elif action == "dispatch":
        need("confirmed")
        if d.get("riderId") or not o["rider_id"]:
            if not assign_rider(db, num, d.get("riderId")):
                raise ApiError("No active rider available", 409)
        set_step(db, num, 3, "Rider picked up the errand")
        db.execute("UPDATE orders SET status='in-transit' WHERE num=?", (num,))
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
    elif action == "cancel":
        do_cancel(db, o)
    else:
        raise ApiError("Unknown action")
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
        "id": f"PAY-{r['id']:03d}", "orderId": f"SR-{r['order_num']}", "method": r["method"],
        "phone": r["phone"] or "—", "amount": r["amount"], "status": r["status"],
        "ref": r["ref"] or "—", "date": local_dt(r["completed_at"] or r["created_at"]),
    } for r in rows])


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

if __name__ == "__main__":
    app.run(host=env("HOST", "127.0.0.1"), port=int(env("PORT", "8000")), debug=env("DEBUG") == "1")
