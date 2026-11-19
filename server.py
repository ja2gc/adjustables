from flask import Flask, request, jsonify, g
from werkzeug.exceptions import BadRequest
from werkzeug.security import check_password_hash, generate_password_hash
import sqlite3, hashlib, hmac, json, base64, time, os, re, secrets, math

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
SECRET = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
DB_PATH = os.environ.get("DATABASE_PATH") or os.path.join(os.path.dirname(__file__), "adjustables.db")
ALLOWED_ORIGINS = {"https://ja2gc.github.io", "https://adjustables.onrender.com",
                   "http://localhost:5000", "http://127.0.0.1:5000"}


@app.before_request
def validate_json_body():
    if request.is_json and request.get_data() and not isinstance(request.get_json(), dict):
        raise BadRequest("Expected a JSON object")


@app.errorhandler(BadRequest)
def bad_request(error):
    return jsonify({"error": "Invalid request data"}), 400


def number(value, minimum=0, maximum=1000000, integer=False):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise BadRequest("Invalid number") from None
    if isinstance(value, bool) or not math.isfinite(result) or not minimum <= result <= maximum:
        raise BadRequest("Number out of range")
    if integer and result != int(result):
        raise BadRequest("Expected an integer")
    return int(result) if integer else result

@app.after_request
def add_cors(response):
    origin = request.headers.get("Origin")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.vary.add("Origin")
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, PATCH, OPTIONS"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

@app.route("/", defaults={"path": ""}, methods=["OPTIONS"])
@app.route("/<path:path>", methods=["OPTIONS"])
def options_handler(path=""):
    return jsonify({}), 200

# DB
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db: db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        email       TEXT NOT NULL UNIQUE,
        password    TEXT NOT NULL,
        role        TEXT NOT NULL DEFAULT 'customer',
        phone       TEXT DEFAULT '',
        address     TEXT DEFAULT '',
        created_at  TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS products (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL,
        category    TEXT NOT NULL,
        price       REAL NOT NULL,
        stock       INTEGER NOT NULL DEFAULT 100,
        rating      REAL DEFAULT 4.5,
        reviews     INTEGER DEFAULT 0,
        badge       TEXT DEFAULT '',
        description TEXT DEFAULT '',
        img_url     TEXT DEFAULT '',
        active      INTEGER DEFAULT 1,
        created_at  TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS orders (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        order_ref   TEXT NOT NULL UNIQUE,
        user_id     INTEGER NOT NULL,
        user_name   TEXT NOT NULL,
        user_email  TEXT NOT NULL,
        items       TEXT NOT NULL,
        subtotal    REAL NOT NULL,
        shipping    REAL NOT NULL DEFAULT 0,
        total       REAL NOT NULL,
        payment     TEXT NOT NULL DEFAULT 'cod',
        shipping_method TEXT DEFAULT 'standard',
        address     TEXT DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'Processing',
        created_at  TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS cart (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        product_id  TEXT NOT NULL,
        product_name TEXT NOT NULL,
        price       REAL NOT NULL,
        qty         INTEGER NOT NULL DEFAULT 1,
        variant     TEXT DEFAULT '',
        custom      INTEGER DEFAULT 0,
        UNIQUE(user_id, product_id),
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    CREATE TABLE IF NOT EXISTS reviews (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id  INTEGER NOT NULL,
        user_id     INTEGER NOT NULL,
        user_name   TEXT NOT NULL,
        rating      INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
        comment     TEXT NOT NULL,
        created_at  TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS votes (
        user_id     INTEGER PRIMARY KEY,
        voted_for   TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );
    """)
    db.commit()

    # Seed admin user
    admin_pw = _hash_password("admin123")
    try:
        db.execute("INSERT INTO users (name,email,password,role) VALUES (?,?,?,?)",
                   ("Admin","admin@adjustables.com", admin_pw, "admin"))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()

    # Seed demo customer
    cust_pw = _hash_password("demo123")
    try:
        db.execute("INSERT INTO users (name,email,password,role) VALUES (?,?,?,?)",
                   ("Demo User","demo@adjustables.com", cust_pw, "customer"))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()

    # Seed products if empty
    count = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    if count == 0:
        products = [
            ("ProStand X1","standing-desk",599,100,4.8,142,"Best Seller",
             "Our flagship standing desk with smooth dual-motor lift system. Whisper-quiet and built to last.",
             "https://images.unsplash.com/photo-1593642632559-0c6d3fc62b89?w=600&q=80"),
            ("ProStand Elite","standing-desk",799,80,4.9,89,"New",
             "Premium standing desk with programmable height presets, integrated cable management, and a stunning solid-wood top.",
             "https://images.unsplash.com/photo-1518455027359-f3f8164ba6bd?w=600&q=80"),
            ("CompactDesk 48","standing-desk",449,120,4.6,203,"",
             "Perfect for smaller spaces without sacrificing quality. The 48\" top fits most home offices beautifully.",
             "https://images.unsplash.com/photo-1524758631624-e2822e304c36?w=600&q=80"),
            ("ErgoChair Pro","desk-chair",399,60,4.7,316,"Top Rated",
             "Fully adjustable ergonomic chair with lumbar support, breathable mesh back, and 4D armrests.",
             "https://images.unsplash.com/photo-1580480055273-228ff5388ef8?w=600&q=80"),
            ("ErgoChair Lite","desk-chair",249,90,4.4,178,"",
             "Essential ergonomic comfort at an accessible price point. Great for home offices and hybrid setups.",
             "https://images.unsplash.com/photo-1586023492125-27b2c045efd7?w=600&q=80"),
            ("Monitor Arm Pro","accessory",149,200,4.8,421,"",
             "Premium full-motion monitor arm supporting up to 32\" displays. Clean desk, happy neck.",
             "https://images.unsplash.com/photo-1527443224154-c4a3942d3acf?w=600&q=80"),
            ("Cable Management Kit","accessory",49,300,4.5,89,"",
             "Complete cable management solution including tray, clips, and sleeves. Say goodbye to desk spaghetti.",
             "https://images.unsplash.com/photo-1601524909162-ae8725290836?w=600&q=80"),
            ("Desk Pad XL","accessory",39,250,4.6,267,"",
             "100% vegan leather desk pad. Protects your surface and looks incredible doing it.",
             "https://images.unsplash.com/photo-1555041469-a586c61ea9bc?w=600&q=80"),
            ("Home Office Bundle","bundle",899,40,4.9,54,"Save $148",
             "Everything you need for the perfect home office: ProStand X1 + ErgoChair Lite + Desk Pad.",
             "https://images.unsplash.com/photo-1497366216548-37526070297c?w=600&q=80"),
            ("Pro Bundle","bundle",1149,25,4.9,38,"Save $248",
             "Our ultimate productivity package: ProStand Elite + ErgoChair Pro + Monitor Arm + Cable Kit.",
             "https://images.unsplash.com/photo-1497366754035-f200968a6e72?w=600&q=80"),
        ]
        db.executemany(
            "INSERT INTO products (name,category,price,stock,rating,reviews,badge,description,img_url) VALUES (?,?,?,?,?,?,?,?,?)",
            products
        )
        db.commit()
    db.close()

def _b64url_encode(data):
    if isinstance(data, str): data = data.encode()
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64url_decode(s):
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)

def create_token(payload: dict, expires_in=86400*7) -> str:
    header = _b64url_encode(json.dumps({"alg":"HS256","typ":"JWT"}))
    payload = {**payload, "exp": int(time.time()) + expires_in}
    body = _b64url_encode(json.dumps(payload))
    sig = _b64url_encode(hmac.new(SECRET.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"

def verify_token(token: str) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) != 3: return None
        header, body, sig = parts
        if json.loads(_b64url_decode(header)) != {"alg": "HS256", "typ": "JWT"}:
            return None
        expected = _b64url_encode(hmac.new(SECRET.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected): return None
        payload = json.loads(_b64url_decode(body))
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), int): return None
        if not isinstance(payload.get("exp"), (int, float)) or not math.isfinite(payload["exp"]) or payload["exp"] <= time.time(): return None
        return payload
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None

def _hash_password(pw: str) -> str:
    return generate_password_hash(pw)


def _check_password(stored, password):
    if not isinstance(password, str) or len(password) > 1024:
        return False
    if re.fullmatch(r"[0-9a-f]{64}", stored):
        # Upgrade existing accounts on login without changing their passwords.
        legacy = hashlib.pbkdf2_hmac("sha256", password.encode(), b"adjustables_salt_2026", 200000).hex()
        return hmac.compare_digest(stored, legacy)
    return check_password_hash(stored, password)

def get_current_user(required=True, admin_only=False):
    auth = request.headers.get("Authorization","")
    if not auth.startswith("Bearer "):
        if required: return None, (jsonify({"error":"Unauthorized"}), 401)
        return None, None
    payload = verify_token(auth[7:])
    if not payload:
        if required: return None, (jsonify({"error":"Invalid or expired token"}), 401)
        return None, None
    row = get_db().execute("SELECT id,name,email,role FROM users WHERE id=?", (payload["id"],)).fetchone()
    if not row:
        return None, (jsonify({"error":"Unauthorized"}), 401) if required else None
    user = dict(row)
    if admin_only and user["role"] != "admin":
        return None, (jsonify({"error":"Admin access required"}), 403)
    return user, None

#  validate
def validate_email(email):
    return re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email or '')
def validate_password(pw):
    return isinstance(pw, str) and 8 <= len(pw) <= 1024
def sanitize(s, max_len=500):
    return str(s or '')[:max_len].strip()


def cart_product(db, pid, data):
    if not pid.startswith("custom-"):
        product = db.execute("SELECT name,price FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not product:
            raise BadRequest("Product unavailable")
        return product["name"], product["price"]

    config = data.get("config")
    if not isinstance(config, dict):
        raise BadRequest("Missing desk configuration")
    materials = {"Wood": 0, "Metal": 50, "Glass": 100, "Walnut": 150}
    heights = {"Standard": 0, "Extended": 100}
    sizes = {'Compact (48\")': 0, 'Compact (48″)': 0, 'Standard (60″)': 80, 'Large (72″)': 160, "L-Shape": 250}
    accessories = {"Cable Management Tray": 49, "Monitor Arm": 149, "Under-Desk Drawer": 89,
                   "Desk Pad": 39, "Keyboard Tray": 79}
    material, height, size = (config.get(k) for k in ("material", "height", "size"))
    selected = config.get("accessories", [])
    if not all(isinstance(v, str) for v in (material, height, size)):
        raise BadRequest("Invalid desk configuration")
    if material not in materials or height not in heights or size not in sizes:
        raise BadRequest("Invalid desk configuration")
    if not isinstance(selected, list) or not all(isinstance(a, str) and a in accessories for a in selected):
        raise BadRequest("Invalid accessories")
    if len(selected) != len(set(selected)):
        raise BadRequest("Duplicate accessories")
    price = 599 + materials[material] + heights[height] + sizes[size] + sum(accessories[a] for a in selected)
    return f"Custom Desk ({material}, {size})", price

 
#  auth routes
@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json() or {}
    name    = sanitize(data.get("name"), 100)
    email   = sanitize(data.get("email"), 200).lower()
    pw      = data.get("password","")
    role    = "customer"  # always customer on self-register

    if not name:
        return jsonify({"error":"Name is required"}), 400
    if not validate_email(email):
        return jsonify({"error":"Invalid email address"}), 400
    if not validate_password(pw):
        return jsonify({"error":"Password must be at least 8 characters"}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if existing:
        return jsonify({"error":"Email already registered"}), 409

    hashed = _hash_password(pw)
    cur = db.execute(
        "INSERT INTO users (name,email,password,role) VALUES (?,?,?,?)",
        (name, email, hashed, role)
    )
    db.commit()
    uid = cur.lastrowid

    token = create_token({"id":uid, "name":name, "email":email, "role":role})
    return jsonify({"token":token, "user":{"id":uid,"name":name,"email":email,"role":role}}), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    data  = request.get_json() or {}
    email = sanitize(data.get("email","")).lower()
    pw    = data.get("password","")

    if not validate_email(email):
        return jsonify({"error":"Invalid email"}), 400
    if not isinstance(pw, str) or not pw or len(pw) > 1024:
        return jsonify({"error":"Password required"}), 400

    db   = get_db()
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not _check_password(user["password"], pw):
        return jsonify({"error":"Invalid email or password"}), 401

    if re.fullmatch(r"[0-9a-f]{64}", user["password"]):
        db.execute("UPDATE users SET password=? WHERE id=?", (_hash_password(pw), user["id"]))
        db.commit()

    token = create_token({"id":user["id"],"name":user["name"],"email":user["email"],"role":user["role"]})
    return jsonify({"token":token, "user":{"id":user["id"],"name":user["name"],"email":user["email"],"role":user["role"]}})


@app.route("/api/auth/me", methods=["GET"])
def me():
    user, err = get_current_user()
    if err: return err
    db   = get_db()
    row  = db.execute("SELECT id,name,email,role,phone,address,created_at FROM users WHERE id=?", (user["id"],)).fetchone()
    if not row: return jsonify({"error":"User not found"}), 404
    return jsonify(dict(row))


@app.route("/api/auth/update", methods=["PUT"])
def update_profile():
    user, err = get_current_user()
    if err: return err
    data = request.get_json() or {}
    name    = sanitize(data.get("name",""), 100)
    phone   = sanitize(data.get("phone",""), 30)
    address = sanitize(data.get("address",""), 300)
    if not name:
        return jsonify({"error":"Name required"}), 400
    db = get_db()
    db.execute("UPDATE users SET name=?,phone=?,address=? WHERE id=?",
               (name, phone, address, user["id"]))
    db.commit()
    return jsonify({"message":"Profile updated"})


@app.route("/api/auth/change-password", methods=["PUT"])
def change_password():
    user, err = get_current_user()
    if err: return err
    data    = request.get_json() or {}
    old_pw  = data.get("oldPassword","")
    new_pw  = data.get("newPassword","")

    if not validate_password(new_pw):
        return jsonify({"error":"New password must be at least 8 characters"}), 400

    db  = get_db()
    row = db.execute("SELECT password FROM users WHERE id=?", (user["id"],)).fetchone()
    if not row or not _check_password(row["password"], old_pw):
        return jsonify({"error":"Current password is incorrect"}), 401

    db.execute("UPDATE users SET password=? WHERE id=?", (_hash_password(new_pw), user["id"]))
    db.commit()
    return jsonify({"message":"Password changed"})

 
#  product
@app.route("/api/products", methods=["GET"])
def get_products():
    db  = get_db()
    cat = request.args.get("category","")
    q   = request.args.get("q","")
    sql = "SELECT * FROM products WHERE active=1"
    params = []
    if cat:
        sql += " AND category=?"; params.append(cat)
    if q:
        sql += " AND (name LIKE ? OR description LIKE ?)"; params += [f"%{q}%", f"%{q}%"]
    rows = db.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/products/<int:pid>", methods=["GET"])
def get_product(pid):
    db  = get_db()
    row = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not row: return jsonify({"error":"Product not found"}), 404
    product = dict(row)
    reviews = db.execute(
        "SELECT r.*,u.name as user_name FROM reviews r JOIN users u ON r.user_id=u.id WHERE r.product_id=? ORDER BY r.created_at DESC",
        (pid,)
    ).fetchall()
    product["reviews_list"] = [dict(r) for r in reviews]
    return jsonify(product)


@app.route("/api/products/<int:pid>/review", methods=["POST"])
def add_review(pid):
    user, err = get_current_user()
    if err: return err
    data    = request.get_json() or {}
    rating  = number(data.get("rating", 0), 1, 5, integer=True)
    comment = sanitize(data.get("comment",""), 1000)
    if not (1 <= rating <= 5):
        return jsonify({"error":"Rating must be 1–5"}), 400
    if not comment:
        return jsonify({"error":"Review comment required"}), 400
    db = get_db()
    db.execute(
        "INSERT INTO reviews (product_id,user_id,user_name,rating,comment) VALUES (?,?,?,?,?)",
        (pid, user["id"], user["name"], rating, comment)
    )
    # Update avg rating
    avg = db.execute("SELECT AVG(rating),COUNT(*) FROM reviews WHERE product_id=?", (pid,)).fetchone()
    db.execute("UPDATE products SET rating=?,reviews=? WHERE id=?",
               (round(avg[0],1), avg[1], pid))
    db.commit()
    return jsonify({"message":"Review added"}), 201

 
#  car t
@app.route("/api/cart", methods=["GET"])
def get_cart():
    user, err = get_current_user()
    if err: return err
    db   = get_db()
    rows = db.execute("SELECT * FROM cart WHERE user_id=?", (user["id"],)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/cart", methods=["POST"])
def add_to_cart():
    user, err = get_current_user()
    if err: return err
    data = request.get_json() or {}
    pid  = sanitize(data.get("product_id",""), 50)
    name = sanitize(data.get("product_name",""), 200)
    price= number(data.get("price", 0), 0.01)
    qty  = number(data.get("qty", 1), 1, 1000, integer=True)
    variant = sanitize(data.get("variant",""), 100)
    custom  = int(pid.startswith("custom-"))

    if not pid or not name or price <= 0:
        return jsonify({"error":"Invalid cart item"}), 400

    db = get_db()
    name, price = cart_product(db, pid, data)
    existing = db.execute(
        "SELECT id,qty FROM cart WHERE user_id=? AND product_id=?",
        (user["id"], pid)
    ).fetchone()
    if existing:
        quantity = number(existing["qty"] + qty, 1, 1000, integer=True)
        db.execute("UPDATE cart SET qty=? WHERE id=?", (quantity, existing["id"]))
    else:
        db.execute(
            "INSERT INTO cart (user_id,product_id,product_name,price,qty,variant,custom) VALUES (?,?,?,?,?,?,?)",
            (user["id"], pid, name, price, qty, variant, custom)
        )
    db.commit()
    return jsonify({"message":"Added to cart"}), 201


@app.route("/api/cart/<int:cart_id>", methods=["PATCH"])
def update_cart_item(cart_id):
    user, err = get_current_user()
    if err: return err
    data = request.get_json() or {}
    qty  = number(data.get("qty", 1), 0, 1000, integer=True)
    db   = get_db()
    item = db.execute("SELECT * FROM cart WHERE id=? AND user_id=?", (cart_id, user["id"])).fetchone()
    if not item: return jsonify({"error":"Cart item not found"}), 404
    if qty <= 0:
        db.execute("DELETE FROM cart WHERE id=?", (cart_id,))
    else:
        db.execute("UPDATE cart SET qty=? WHERE id=?", (qty, cart_id))
    db.commit()
    return jsonify({"message":"Updated"})


@app.route("/api/cart/<int:cart_id>", methods=["DELETE"])
def remove_cart_item(cart_id):
    user, err = get_current_user()
    if err: return err
    db = get_db()
    db.execute("DELETE FROM cart WHERE id=? AND user_id=?", (cart_id, user["id"]))
    db.commit()
    return jsonify({"message":"Removed"})


@app.route("/api/cart/clear", methods=["DELETE"])
def clear_cart():
    user, err = get_current_user()
    if err: return err
    get_db().execute("DELETE FROM cart WHERE user_id=?", (user["id"],))
    get_db().commit()
    return jsonify({"message":"Cart cleared"})

#  orders
@app.route("/api/orders", methods=["POST"])
def place_order():
    user, err = get_current_user()
    if err: return err
    data    = request.get_json() or {}
    items   = data.get("items", [])
    address = sanitize(data.get("address",""), 500)
    payment = sanitize(data.get("payment","cod"), 50)
    ship_m  = sanitize(data.get("shipping_method","standard"), 50)
    if ship_m not in {"standard", "express", "pickup"} or payment not in {"cod", "gcash", "maya", "card"}:
        raise BadRequest("Invalid checkout option")
    shipping = 29 if ship_m == "express" else 0

    if not isinstance(items, list) or not items or len(items) > 100:
        return jsonify({"error":"No items in order"}), 400
    if not address:
        return jsonify({"error":"Delivery address required"}), 400

    db = get_db()
    verified_items = []
    for item in items:
        if not isinstance(item, dict):
            raise BadRequest("Invalid order item")
        pid = sanitize(item.get("product_id"), 50)
        if pid.startswith("custom-"):
            saved = db.execute("SELECT product_name,price FROM cart WHERE user_id=? AND product_id=? AND custom=1",
                               (user["id"], pid)).fetchone()
            if not saved:
                raise BadRequest("Custom item is not in your cart")
            name, price = saved["product_name"], saved["price"]
        else:
            name, price = cart_product(db, pid, {})
        qty = number(item.get("qty", 1), 1, 1000, integer=True)
        verified_items.append({"product_id": pid, "product_name": name, "price": price, "qty": qty})
    items = verified_items
    subtotal = round(sum(i["price"] * i["qty"] for i in items), 2)
    total = round(subtotal + shipping, 2)
    ref = f"AT-{time.time_ns()}"
    db.execute(
        "INSERT INTO orders (order_ref,user_id,user_name,user_email,items,subtotal,shipping,total,payment,shipping_method,address) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (ref, user["id"], user["name"], user["email"],
         json.dumps(items), subtotal, shipping, total, payment, ship_m, address)
    )
    # clear cart
    db.execute("DELETE FROM cart WHERE user_id=?", (user["id"],))
    db.commit()
    return jsonify({"message":"Order placed", "order_ref": ref, "total": total}), 201


@app.route("/api/orders/my", methods=["GET"])
def my_orders():
    user, err = get_current_user()
    if err: return err
    db   = get_db()
    rows = db.execute(
        "SELECT * FROM orders WHERE user_id=? ORDER BY created_at DESC",
        (user["id"],)
    ).fetchall()
    result = []
    for r in rows:
        o = dict(r)
        o["items"] = json.loads(o["items"])
        result.append(o)
    return jsonify(result)

 
#  vote
@app.route("/api/votes", methods=["GET"])
def get_votes():
    db    = get_db()
    rows  = db.execute("SELECT voted_for, COUNT(*) as cnt FROM votes GROUP BY voted_for").fetchall()
    counts = {r["voted_for"]: r["cnt"] for r in rows}
    user, _ = get_current_user(required=False)
    my_vote = None
    if user:
        v = db.execute("SELECT voted_for FROM votes WHERE user_id=?", (user["id"],)).fetchone()
        if v: my_vote = v["voted_for"]
    return jsonify({"counts": counts, "myVote": my_vote})


@app.route("/api/votes", methods=["POST"])
def cast_vote():
    user, err = get_current_user()
    if err: return err
    data   = request.get_json() or {}
    choice = sanitize(data.get("choice",""), 50)
    valid  = ["cup","headphone","planter","charger"]
    if choice not in valid:
        return jsonify({"error":"Invalid choice"}), 400
    db = get_db()
    existing = db.execute("SELECT * FROM votes WHERE user_id=?", (user["id"],)).fetchone()
    if existing:
        db.execute("UPDATE votes SET voted_for=? WHERE user_id=?", (choice, user["id"]))
    else:
        db.execute("INSERT INTO votes (user_id,voted_for) VALUES (?,?)", (user["id"], choice))
    db.commit()
    return jsonify({"message":"Vote recorded", "choice": choice})

#  admin routes
@app.route("/api/admin/stats", methods=["GET"])
def admin_stats():
    user, err = get_current_user(admin_only=True)
    if err: return err
    db = get_db()
    total_orders   = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    total_revenue  = db.execute("SELECT COALESCE(SUM(total),0) FROM orders").fetchone()[0]
    total_customers= db.execute("SELECT COUNT(*) FROM users WHERE role='customer'").fetchone()[0]
    total_products = db.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0]
    recent_orders  = db.execute(
        "SELECT id,order_ref,user_name,total,status,created_at FROM orders ORDER BY created_at DESC LIMIT 10"
    ).fetchall()
    return jsonify({
        "totalOrders": total_orders,
        "totalRevenue": round(total_revenue, 2),
        "totalCustomers": total_customers,
        "totalProducts": total_products,
        "recentOrders": [dict(r) for r in recent_orders]
    })


@app.route("/api/admin/orders", methods=["GET"])
def admin_orders():
    user, err = get_current_user(admin_only=True)
    if err: return err
    db   = get_db()
    rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    result = []
    for r in rows:
        o = dict(r); o["items"] = json.loads(o["items"]); result.append(o)
    return jsonify(result)


@app.route("/api/admin/orders/<int:oid>/status", methods=["PATCH"])
def update_order_status(oid):
    user, err = get_current_user(admin_only=True)
    if err: return err
    data   = request.get_json() or {}
    status = sanitize(data.get("status",""), 50)
    valid  = ["Processing","Shipped","Delivered","Cancelled"]
    if status not in valid:
        return jsonify({"error":"Invalid status"}), 400
    db = get_db()
    db.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
    db.commit()
    return jsonify({"message":"Status updated"})


@app.route("/api/admin/users", methods=["GET"])
def admin_users():
    user, err = get_current_user(admin_only=True)
    if err: return err
    db   = get_db()
    rows = db.execute("SELECT id,name,email,role,phone,created_at FROM users ORDER BY created_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/admin/users/<int:uid>/role", methods=["PATCH"])
def update_user_role(uid):
    user, err = get_current_user(admin_only=True)
    if err: return err
    data = request.get_json() or {}
    role = data.get("role","")
    if role not in ["admin","customer"]:
        return jsonify({"error":"Invalid role"}), 400
    db = get_db()
    db.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    db.commit()
    return jsonify({"message":"Role updated"})


@app.route("/api/admin/products", methods=["POST"])
def admin_add_product():
    user, err = get_current_user(admin_only=True)
    if err: return err
    data  = request.get_json() or {}
    name  = sanitize(data.get("name",""), 200)
    cat   = sanitize(data.get("category",""), 100)
    price = number(data.get("price", 0), 0.01)
    stock = number(data.get("stock", 0), 0, integer=True)
    desc  = sanitize(data.get("description",""), 2000)
    img   = sanitize(data.get("img_url",""), 500)
    badge = sanitize(data.get("badge",""), 50)

    if not name or not cat or price <= 0:
        return jsonify({"error":"Name, category and price required"}), 400

    db  = get_db()
    cur = db.execute(
        "INSERT INTO products (name,category,price,stock,description,img_url,badge) VALUES (?,?,?,?,?,?,?)",
        (name, cat, price, stock, desc, img, badge)
    )
    db.commit()
    return jsonify({"message":"Product added", "id": cur.lastrowid}), 201


@app.route("/api/admin/products/<int:pid>", methods=["PUT"])
def admin_update_product(pid):
    user, err = get_current_user(admin_only=True)
    if err: return err
    data  = request.get_json() or {}
    name  = sanitize(data.get("name",""), 200)
    price = number(data.get("price", 0), 0.01)
    stock = number(data.get("stock", 0), 0, integer=True)
    badge = sanitize(data.get("badge",""), 50)
    desc  = sanitize(data.get("description",""), 2000)

    if not name or price <= 0:
        return jsonify({"error":"Name and price required"}), 400

    db = get_db()
    db.execute("UPDATE products SET name=?,price=?,stock=?,badge=?,description=? WHERE id=?",
               (name, price, stock, badge, desc, pid))
    db.commit()
    return jsonify({"message":"Product updated"})


@app.route("/api/admin/products/<int:pid>", methods=["DELETE"])
def admin_delete_product(pid):
    user, err = get_current_user(admin_only=True)
    if err: return err
    db = get_db()
    db.execute("UPDATE products SET active=0 WHERE id=?", (pid,))
    db.commit()
    return jsonify({"message":"Product deactivated"})

@app.route("/api/health")
def health():
    return jsonify({"status":"ok","version":"1.0","db": os.path.exists(DB_PATH)})

# BOOT 
if __name__ == "__main__":
    init_db()
    from waitress import serve

    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
