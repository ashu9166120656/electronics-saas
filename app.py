"""Electronics SaaS — PCB calculators with user accounts, rate limiting, and Razorpay Pro."""

import hashlib
import hmac
import os
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from math import log, sqrt

import razorpay
from flask import Flask, g, jsonify, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")

DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")
DAILY_LIMIT = 5
PRO_PRICE_PAISE = 19900  # ₹199
PRO_DURATION_DAYS = 30

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

# IPC-2221 constants
K_OUTER = 0.048
K_INNER = 0.024
COPPER_OZ_TO_MM = 0.0347
MM_TO_MILS = 39.37

E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]


# =============================================================================
# Database helpers
# =============================================================================

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            email           TEXT    UNIQUE NOT NULL,
            password        TEXT    NOT NULL,
            is_pro          INTEGER NOT NULL DEFAULT 0,
            pro_expiry      TEXT,
            daily_calc_count INTEGER NOT NULL DEFAULT 0,
            last_calc_date  TEXT,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS calculation_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            calculator      TEXT    NOT NULL,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS orders (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id             INTEGER NOT NULL,
            razorpay_order_id   TEXT    NOT NULL,
            razorpay_payment_id TEXT,
            amount              INTEGER NOT NULL,
            status              TEXT    NOT NULL DEFAULT 'pending',
            created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    # Migrate older databases that lack the new columns
    for col, col_def in [
        ("is_pro",           "INTEGER NOT NULL DEFAULT 0"),
        ("pro_expiry",       "TEXT"),
        ("daily_calc_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_calc_date",   "TEXT"),
    ]:
        try:
            db.execute(f"ALTER TABLE users ADD COLUMN {col} {col_def}")
        except sqlite3.OperationalError:
            pass  # column already exists
    db.commit()


def get_user(user_id):
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_email(email):
    db = get_db()
    return db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def create_user(email, password):
    db = get_db()
    db.execute(
        "INSERT INTO users (email, password) VALUES (?, ?)",
        (email, generate_password_hash(password)),
    )
    db.commit()


def log_calculation(user_id, calculator):
    db = get_db()
    db.execute(
        "INSERT INTO calculation_log (user_id, calculator) VALUES (?, ?)",
        (user_id, calculator),
    )
    db.commit()


def get_total_count(user_id):
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) AS cnt FROM calculation_log WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return row["cnt"]


def create_order_record(user_id, razorpay_order_id, amount):
    db = get_db()
    db.execute(
        "INSERT INTO orders (user_id, razorpay_order_id, amount) VALUES (?, ?, ?)",
        (user_id, razorpay_order_id, amount),
    )
    db.commit()


def update_order_success(razorpay_order_id, razorpay_payment_id):
    db = get_db()
    db.execute(
        "UPDATE orders SET status = 'completed', razorpay_payment_id = ? "
        "WHERE razorpay_order_id = ?",
        (razorpay_payment_id, razorpay_order_id),
    )
    db.commit()


def get_order_by_razorpay_id(razorpay_order_id):
    db = get_db()
    return db.execute(
        "SELECT * FROM orders WHERE razorpay_order_id = ?",
        (razorpay_order_id,),
    ).fetchone()


def mark_user_pro(user_id):
    db = get_db()
    pro_until = (datetime.utcnow() + timedelta(days=PRO_DURATION_DAYS)).isoformat()
    db.execute(
        "UPDATE users SET is_pro = 1, pro_expiry = ? WHERE id = ?",
        (pro_until, user_id),
    )
    db.commit()


def is_user_pro(user_row):
    """Check if a user row (dict / Row) has a valid Pro subscription."""
    if not user_row["is_pro"]:
        return False
    if not user_row["pro_expiry"]:
        return False
    try:
        expiry = datetime.fromisoformat(user_row["pro_expiry"])
    except (ValueError, TypeError):
        return False
    return expiry > datetime.utcnow()


def check_usage_limit(user_id):
    """
    Returns (allowed: bool, error_message: str | None, remaining: int).
    Resets daily_calc_count when a new day starts.
    """
    db = get_db()
    user = get_user(user_id)

    if is_user_pro(user):
        return True, None, float("inf")

    today_str = date.today().isoformat()

    if user["last_calc_date"] != today_str:
        db.execute(
            "UPDATE users SET daily_calc_count = 0, last_calc_date = ? WHERE id = ?",
            (today_str, user_id),
        )
        db.commit()
        user = get_user(user_id)  # re-read

    count = user["daily_calc_count"]
    if count >= DAILY_LIMIT:
        return (
            False,
            "Daily limit reached (5 calculations). Get Pro for unlimited access.",
            0,
        )

    return True, None, DAILY_LIMIT - count - 1


def increment_daily_count(user_id):
    db = get_db()
    today_str = date.today().isoformat()
    db.execute(
        "UPDATE users SET daily_calc_count = daily_calc_count + 1, "
        "last_calc_date = ? WHERE id = ?",
        (today_str, user_id),
    )
    db.commit()


# =============================================================================
# Flask-Login setup
# =============================================================================

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message_category = "info"
login_manager.init_app(app)


class User(UserMixin):
    def __init__(self, row):
        self.id = row["id"]
        self.email = row["email"]
        self.is_pro = bool(row["is_pro"])
        self.pro_expiry = row["pro_expiry"]
        self.created_at = row["created_at"]


@login_manager.user_loader
def load_user(user_id):
    row = get_user(user_id)
    return User(row) if row else None


# =============================================================================
# Context processor
# =============================================================================

@app.context_processor
def inject_globals():
    return {
        "current_user": current_user,
        "razorpay_key_id": RAZORPAY_KEY_ID,
    }


# =============================================================================
# Helper — rate-limit helper for calculator routes
# =============================================================================

def calc_rate_limit():
    """Shared rate-limit check for POST handlers. Returns (allowed, error, remaining)."""
    if not current_user.is_authenticated:
        return None, None, None  # caller must handle redirect

    allowed, msg, remaining = check_usage_limit(current_user.id)
    if not allowed:
        return False, msg, 0
    return True, None, remaining


# =============================================================================
# PCB Trace Width
# =============================================================================

def calc_trace_width(current_a, temp_rise_c, copper_oz, layer):
    k = K_OUTER if layer == "outer" else K_INNER
    if current_a <= 0 or temp_rise_c <= 0 or copper_oz <= 0:
        return None
    area_sq_mils = (current_a / (k * (temp_rise_c ** 0.44))) ** (1 / 0.725)
    thickness_mils = copper_oz * COPPER_OZ_TO_MM * MM_TO_MILS
    width_mils = area_sq_mils / thickness_mils
    width_mm = width_mils / MM_TO_MILS
    return {
        "width_mm": round(width_mm, 4),
        "width_mils": round(width_mils, 2),
        "area_sq_mils": round(area_sq_mils, 2),
        "k": k,
    }


@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    error = None
    form_data = {}
    limit_warning = None

    if request.method == "POST":
        if not current_user.is_authenticated:
            return redirect(url_for("login", next=request.path))

        allowed, msg, remaining = calc_rate_limit()
        if allowed is False:
            error = msg
        elif allowed is None:
            return redirect(url_for("login", next=request.path))
        else:
            try:
                current = float(request.form.get("current", 0))
                temp_rise = float(request.form.get("temp_rise", 0))
                copper = float(request.form.get("copper", 1))
                layer = request.form.get("layer", "outer")

                form_data = {
                    "current": request.form.get("current", "1"),
                    "temp_rise": request.form.get("temp_rise", "10"),
                    "copper": request.form.get("copper", "1"),
                    "layer": layer,
                }

                result = calc_trace_width(current, temp_rise, copper, layer)
                if result is None:
                    error = "All input values must be positive numbers greater than zero."
                else:
                    increment_daily_count(current_user.id)
                    log_calculation(current_user.id, "trace_width")
                    limit_warning = remaining

            except (ValueError, TypeError):
                error = "Please enter valid numeric values for all fields."

    return render_template(
        "index.html",
        result=result, error=error, form_data=form_data,
        limit_warning=limit_warning, daily_limit=DAILY_LIMIT,
    )


# =============================================================================
# Microstrip Impedance
# =============================================================================

@app.route("/impedance", methods=["GET", "POST"])
def impedance():
    result = None
    error = None
    form_data = {}
    limit_warning = None

    if request.method == "POST":
        if not current_user.is_authenticated:
            return redirect(url_for("login", next=request.path))

        allowed, msg, remaining = calc_rate_limit()
        if allowed is False:
            error = msg
        elif allowed is None:
            return redirect(url_for("login", next=request.path))
        else:
            try:
                trace_w = float(request.form.get("trace_w", 0))
                dielectric_h = float(request.form.get("dielectric_h", 0))
                er = float(request.form.get("er", 0))
                copper_oz = float(request.form.get("copper_oz", 1))

                form_data = {
                    "trace_w": request.form.get("trace_w", "0.5"),
                    "dielectric_h": request.form.get("dielectric_h", "0.2"),
                    "er": request.form.get("er", "4.5"),
                    "copper_oz": request.form.get("copper_oz", "1"),
                }

                if trace_w <= 0 or dielectric_h <= 0 or er <= 0 or copper_oz <= 0:
                    error = "All input values must be positive numbers greater than zero."
                else:
                    t_mm = copper_oz * COPPER_OZ_TO_MM
                    z0 = (87 / sqrt(er + 1.41)) * log(5.98 * dielectric_h / (0.8 * trace_w + t_mm))
                    result = {
                        "z0": round(z0, 1),
                        "trace_w": trace_w,
                        "dielectric_h": dielectric_h,
                        "er": er,
                        "t_mm": round(t_mm, 4),
                    }
                    increment_daily_count(current_user.id)
                    log_calculation(current_user.id, "impedance")
                    limit_warning = remaining

            except (ValueError, TypeError):
                error = "Please enter valid numeric values for all fields."

    return render_template(
        "impedance.html",
        result=result, error=error, form_data=form_data,
        limit_warning=limit_warning, daily_limit=DAILY_LIMIT,
    )


# =============================================================================
# Buck Converter
# =============================================================================

def nearest_std(value):
    if value <= 0:
        return 1.0
    exp = 0
    v = value
    while v >= 10:
        v /= 10
        exp += 1
    while v < 1:
        v *= 10
        exp -= 1
    nearest = min(E12, key=lambda x: abs(x - v))
    return nearest * (10 ** exp)


@app.route("/buck", methods=["GET", "POST"])
def buck():
    result = None
    error = None
    form_data = {}
    limit_warning = None

    if request.method == "POST":
        if not current_user.is_authenticated:
            return redirect(url_for("login", next=request.path))

        allowed, msg, remaining = calc_rate_limit()
        if allowed is False:
            error = msg
        elif allowed is None:
            return redirect(url_for("login", next=request.path))
        else:
            try:
                vin = float(request.form.get("vin", 0))
                vout = float(request.form.get("vout", 0))
                iout = float(request.form.get("iout", 0))
                freq_khz = float(request.form.get("freq_khz", 0))

                form_data = {
                    "vin": request.form.get("vin", "12"),
                    "vout": request.form.get("vout", "5"),
                    "iout": request.form.get("iout", "2"),
                    "freq_khz": request.form.get("freq_khz", "500"),
                }

                if vin <= 0 or vout <= 0 or iout <= 0 or freq_khz <= 0:
                    error = "All input values must be positive numbers greater than zero."
                elif vout >= vin:
                    error = "Vout must be less than Vin for a buck converter."
                else:
                    freq_hz = freq_khz * 1000
                    D = vout / vin
                    I_ripple = 0.3 * iout
                    L_h = (vin - vout) * D / (freq_hz * I_ripple)
                    L_uh = L_h * 1e6
                    V_ripple = 0.01 * vout
                    C_f = I_ripple / (8 * freq_hz * V_ripple)
                    C_uf = C_f * 1e6
                    I_peak = iout + I_ripple / 2

                    result = {
                        "D": round(D * 100, 1),
                        "V_ripple_mv": round(V_ripple * 1000, 1),
                        "I_ripple_a": round(I_ripple, 3),
                        "I_peak_a": round(I_peak, 3),
                        "L_uh": round(L_uh, 2),
                        "L_std": round(nearest_std(L_uh), 2),
                        "C_uf": round(C_uf, 2),
                        "C_std": round(nearest_std(C_uf), 2),
                    }
                    increment_daily_count(current_user.id)
                    log_calculation(current_user.id, "buck")
                    limit_warning = remaining

            except (ValueError, TypeError):
                error = "Please enter valid numeric values for all fields."

    return render_template(
        "buck.html",
        result=result, error=error, form_data=form_data,
        limit_warning=limit_warning, daily_limit=DAILY_LIMIT,
    )


# =============================================================================
# Auth routes
# =============================================================================

@app.route("/signup", methods=["GET", "POST"])
def signup():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")

        if not email or not password:
            error = "Email and password are required."
        elif "@" not in email or "." not in email:
            error = "Please enter a valid email address."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif password != confirm:
            error = "Passwords do not match."
        elif get_user_by_email(email):
            error = "An account with that email already exists."
        else:
            init_db()
            create_user(email, password)
            user_row = get_user_by_email(email)
            login_user(User(user_row))
            return redirect(url_for("profile"))

    return render_template("signup.html", error=error, daily_limit=DAILY_LIMIT)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user_row = get_user_by_email(email)
        if user_row and check_password_hash(user_row["password"], password):
            login_user(User(user_row))
            next_page = request.args.get("next")
            if next_page and not next_page.startswith("/"):
                next_page = None
            return redirect(next_page or url_for("profile"))
        else:
            error = "Invalid email or password."

    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))


@app.route("/profile")
@login_required
def profile():
    user_row = get_user(current_user.id)
    pro = is_user_pro(user_row)
    today_str = date.today().isoformat()

    if user_row["last_calc_date"] == today_str:
        today_count = user_row["daily_calc_count"]
    else:
        today_count = 0

    total = get_total_count(current_user.id)

    if pro:
        remaining = float("inf")
    else:
        remaining = max(0, DAILY_LIMIT - today_count)

    return render_template(
        "profile.html",
        today=today_count,
        total=total,
        daily_limit=DAILY_LIMIT,
        remaining=remaining,
        is_pro=pro,
        pro_expiry=user_row["pro_expiry"] if user_row["pro_expiry"] else None,
    )


# =============================================================================
# Pricing page (public)
# =============================================================================

@app.route("/pricing")
def pricing():
    return render_template(
        "pricing.html",
        pro_price=PRO_PRICE_PAISE // 100,
        daily_limit=DAILY_LIMIT,
    )


# =============================================================================
# Razorpay — Create Order
# =============================================================================

@app.route("/create-order", methods=["POST"])
@login_required
def create_order():
    client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    order_data = {
        "amount": PRO_PRICE_PAISE,
        "currency": "INR",
        "receipt": f"pro_{current_user.id}_{int(datetime.utcnow().timestamp())}",
        "notes": {"user_id": str(current_user.id)},
    }
    rz_order = client.order.create(data=order_data)

    create_order_record(current_user.id, rz_order["id"], PRO_PRICE_PAISE)

    return jsonify({
        "order_id": rz_order["id"],
        "amount": PRO_PRICE_PAISE,
        "currency": "INR",
    })


# =============================================================================
# Razorpay — Payment Success (frontend callback)
# =============================================================================

@app.route("/payment-success", methods=["POST"])
@login_required
def payment_success():
    order_id = request.form.get("razorpay_order_id", "")
    payment_id = request.form.get("razorpay_payment_id", "")
    signature = request.form.get("razorpay_signature", "")

    # Verify signature
    payload = f"{order_id}|{payment_id}"
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        return jsonify({"error": "Signature verification failed"}), 400

    order = get_order_by_razorpay_id(order_id)
    if order is None:
        return jsonify({"error": "Order not found"}), 404

    if order["status"] == "completed":
        # Already processed — still mark pro (idempotent)
        pass

    mark_user_pro(current_user.id)
    update_order_success(order_id, payment_id)

    return redirect(url_for("profile"))


# =============================================================================
# Razorpay — Webhook
# =============================================================================

@app.route("/webhook/razorpay", methods=["POST"])
def razorpay_webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")

    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        return jsonify({"error": "Invalid signature"}), 400

    event = request.get_json()
    if event.get("event") != "payment.captured":
        return jsonify({"status": "ignored"}), 200

    payment_entity = event["payload"]["payment"]["entity"]
    order_id = payment_entity.get("order_id")
    payment_id = payment_entity.get("id")
    notes = payment_entity.get("notes", {})

    user_id = notes.get("user_id")
    if user_id:
        init_db()
        mark_user_pro(int(user_id))

    if order_id:
        existing = get_order_by_razorpay_id(order_id)
        if existing and existing["status"] != "completed":
            update_order_success(order_id, payment_id)

    return jsonify({"status": "ok"}), 200


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
