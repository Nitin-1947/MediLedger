import csv, io, math, os, re, sqlite3, uuid
from datetime import date
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, g, send_file, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from ocr import extract_text, parse_invoice
from gemini import extract_invoice as extract_with_gemini

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join("/tmp", "mediledger") if os.environ.get("VERCEL") else BASE
DB = os.path.join(DATA_DIR, "mediledger.sqlite3")
UPLOADS = os.path.join(DATA_DIR, "uploads")
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
os.makedirs(UPLOADS, exist_ok=True)
ALLOWED_UPLOADS = {".jpg", ".jpeg", ".jpe", ".png", ".webp", ".pdf"}

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(_=None):
    con = g.pop("db", None)
    if con: con.close()

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(open(os.path.join(BASE, "schema.sql"), encoding="utf8").read())
    columns = {row[1] for row in con.execute("PRAGMA table_info(users)")}
    for column in ("phone", "shop_name", "shop_address", "shop_gstin"):
        if column not in columns:
            con.execute("ALTER TABLE users ADD COLUMN %s TEXT DEFAULT ''" % column)
    con.commit(); con.close()

# Make `flask --app app run` work without a separate migration command.
init_db()

def login_required(fn):
    @wraps(fn)
    def wrapped(*a, **kw):
        if not session.get("user_id"):
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapped

@app.before_request
def load_user():
    g.user = db().execute("SELECT * FROM users WHERE id=?", (session.get("user_id"),)).fetchone() if session.get("user_id") else None

@app.route("/")
def index():
    return redirect(url_for("dashboard" if g.user else "login"))

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name, email, password = request.form.get("name","").strip(), request.form.get("email","").strip().lower(), request.form.get("password","")
        if not name or not email or len(password) < 8:
            flash("Name, email and a password of at least 8 characters are required.", "danger")
        else:
            try:
                cur = db().execute("INSERT INTO users(name,email,password_hash) VALUES(?,?,?)", (name,email,generate_password_hash(password)))
                db().commit(); session.clear(); session["user_id"] = cur.lastrowid
                flash("Welcome to MediLedger!", "success"); return redirect(url_for("dashboard"))
            except sqlite3.IntegrityError: flash("That email is already registered.", "danger")
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = db().execute("SELECT * FROM users WHERE email=?", (request.form.get("email","").strip().lower(),)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password","")):
            session.clear(); session["user_id"] = user["id"]
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Invalid email or password.", "danger")
    return render_template("login.html")

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirmation = request.form.get("confirmation", "")
        user = db().execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if not user:
            flash("No account was found for that email address.", "danger")
        elif len(password) < 8:
            flash("Your new password must be at least 8 characters.", "danger")
        elif password != confirmation:
            flash("The passwords do not match.", "danger")
        else:
            db().execute("UPDATE users SET password_hash=? WHERE id=?",
                         (generate_password_hash(password), user["id"]))
            db().commit()
            flash("Password changed successfully. You can now log in.", "success")
            return redirect(url_for("login"))
    return render_template("forgot_password.html")

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        shop_name = request.form.get("shop_name", "").strip()
        shop_address = request.form.get("shop_address", "").strip()
        shop_gstin = request.form.get("shop_gstin", "").strip().upper()
        if not name:
            flash("Your name is required.", "danger")
        else:
            db().execute(
                "UPDATE users SET name=?,phone=?,shop_name=?,shop_address=?,shop_gstin=? WHERE id=?",
                (name, phone, shop_name, shop_address, shop_gstin, g.user["id"]),
            )
            db().commit()
            flash("Profile updated successfully.", "success")
            return redirect(url_for("profile"))
    return render_template("profile.html", user=g.user)

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

def bill_query(where="", args=()):
    return db().execute("SELECT b.*, COALESCE(SUM(i.quantity*i.unit_price),0) AS item_total FROM bills b LEFT JOIN bill_items i ON i.bill_id=b.id WHERE b.user_id=? "+where+" GROUP BY b.id ORDER BY b.invoice_date DESC,b.id DESC", (g.user["id"],)+tuple(args)).fetchall()

@app.route("/dashboard")
@login_required
def dashboard():
    con=db(); uid=g.user["id"]
    stats=con.execute("SELECT COUNT(*) bills, COALESCE(SUM(total),0) spend, COUNT(DISTINCT supplier) suppliers FROM bills WHERE user_id=?", (uid,)).fetchone()
    recent=con.execute("SELECT * FROM bills WHERE user_id=? ORDER BY created_at DESC LIMIT 6",(uid,)).fetchall()
    monthly=con.execute("SELECT substr(invoice_date,1,7) month, SUM(total) total FROM bills WHERE user_id=? GROUP BY month ORDER BY month DESC LIMIT 6",(uid,)).fetchall()
    return render_template("dashboard.html", stats=stats, recent=recent, monthly=monthly)

@app.route("/bills")
@login_required
def bills():
    q=request.args.get("q","").strip(); category=request.args.get("category","")
    clauses=[]; args=[]
    if q: clauses.append("(b.invoice_number LIKE ? OR b.supplier LIKE ? OR EXISTS (SELECT 1 FROM bill_items x WHERE x.bill_id=b.id AND x.product_name LIKE ?))"); args += [f"%{q}%"]*3
    if category: clauses.append("EXISTS (SELECT 1 FROM bill_items x WHERE x.bill_id=b.id AND x.category=?)"); args.append(category)
    rows=bill_query((" AND "+" AND ".join(clauses)) if clauses else "", args)
    return render_template("bills.html", bills=rows, q=q, category=category)

@app.get("/scan")
@login_required
def scan():
    return render_template("scan.html",
                           gemini_enabled=bool(os.environ.get("GEMINI_API_KEY", "").strip()),
                           gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"))

@app.post("/scan")
@login_required
def scan_upload():
    uploaded = request.files.get("bill_file")
    if not uploaded or not uploaded.filename:
        flash("Choose an image or PDF to scan.", "danger")
        return redirect(url_for("scan"))
    original = secure_filename(uploaded.filename)
    ext = os.path.splitext(original)[1].lower()
    if not original or ext not in ALLOWED_UPLOADS:
        flash("Only JPG, JPEG, PNG, WEBP and PDF files are supported.", "danger")
        return redirect(url_for("scan"))
    filename = "%s_%s%s" % (g.user["id"], uuid.uuid4().hex, ext)
    path = os.path.join(UPLOADS, filename)
    uploaded.save(path)
    extracted, gemini_warning = extract_with_gemini(path)
    if extracted:
        flash("Gemini Vision extracted the bill. Review every field before saving.", "success")
    else:
        text, warning = extract_text(path)
        extracted = parse_invoice(text)
        if gemini_warning:
            flash(gemini_warning, "warning")
        if warning:
            flash(warning, "warning")
        elif not text.strip():
            flash("No text was detected. Please verify the fields manually.", "warning")
        else:
            flash("Bill scanned. Review the extracted details before saving.", "success")
    return render_template("bill_form.html", bill=None, items=extracted["items"] or
                           [{"product_name": "", "category": "medicine", "quantity": 1, "unit_price": ""}],
                           extracted=extracted, uploaded_file=filename)

@app.get("/bills/<int:bill_id>")
@login_required
def bill_detail(bill_id):
    con = db()
    bill = con.execute("SELECT * FROM bills WHERE id=? AND user_id=?", (bill_id, g.user["id"])).fetchone()
    if not bill:
        return ("Not found", 404)
    items = con.execute("SELECT * FROM bill_items WHERE bill_id=? ORDER BY id", (bill_id,)).fetchall()
    return render_template("bill_detail.html", bill=bill, items=items)

@app.get("/bills/<int:bill_id>/file")
@login_required
def bill_file(bill_id):
    bill = db().execute("SELECT file_name FROM bills WHERE id=? AND user_id=?", (bill_id, g.user["id"])).fetchone()
    if not bill or not bill["file_name"]:
        return ("File not found", 404)
    path = os.path.join(UPLOADS, os.path.basename(bill["file_name"]))
    if not os.path.isfile(path):
        return ("File not found", 404)
    return send_file(path, as_attachment=True, download_name=os.path.basename(path).split("_", 1)[-1])

def parse_items(form):
    names=form.getlist("product_name"); cats=form.getlist("category"); qtys=form.getlist("quantity"); prices=form.getlist("unit_price")
    out=[]
    for i,n in enumerate(names):
        if n.strip():
            try: q=float(qtys[i] or 0); p=float(prices[i] or 0)
            except (ValueError, IndexError): raise ValueError("Quantities and prices must be numbers.")
            category = cats[i] if i < len(cats) else "medicine"
            if category not in ("medicine", "cosmetic", "other"):
                raise ValueError("Choose a valid product category.")
            if not math.isfinite(q) or not math.isfinite(p) or q <= 0 or p < 0: raise ValueError("Quantity must be positive and price cannot be negative.")
            out.append((n.strip(), category, q, p))
    if not out: raise ValueError("Add at least one product.")
    return out

@app.route("/bills/new", methods=["GET","POST"])
@login_required
def new_bill():
    if request.method=="POST":
        try:
            supplier=request.form.get("supplier","").strip(); inv=request.form.get("invoice_number","").strip(); dt=request.form.get("invoice_date") or str(date.today())
            total=float(request.form.get("total") or 0); gst=float(request.form.get("gst") or 0); items=parse_items(request.form)
            try: date.fromisoformat(dt)
            except ValueError: raise ValueError("Enter a valid invoice date.")
            if not math.isfinite(total) or not math.isfinite(gst) or not supplier or not inv or total < 0 or gst < 0: raise ValueError("Supplier, invoice number and a valid total are required.")
            if db().execute("SELECT id FROM bills WHERE user_id=? AND supplier=? AND invoice_number=?",(g.user["id"],supplier,inv)).fetchone(): raise ValueError("Duplicate invoice detected for this supplier.")
            filename=request.form.get("uploaded_file") or None; f=request.files.get("bill_file")
            if f and f.filename:
                original=secure_filename(f.filename); ext=os.path.splitext(original)[1].lower()
                if not original or ext not in ALLOWED_UPLOADS: raise ValueError("Only JPG, JPEG, PNG, WEBP and PDF files are supported.")
                filename=f"{g.user['id']}_{uuid.uuid4().hex}{ext}"; f.save(os.path.join(UPLOADS,filename))
            if filename and (not filename.startswith(f"{g.user['id']}_") or not os.path.isfile(os.path.join(UPLOADS, os.path.basename(filename)))):
                raise ValueError("The uploaded bill could not be found. Please upload it again.")
            cur=db().execute("INSERT INTO bills(user_id,supplier,invoice_number,invoice_date,gst,total,notes,file_name) VALUES(?,?,?,?,?,?,?,?)",(g.user["id"],supplier,inv,dt,gst,total,request.form.get("notes","").strip(),filename))
            for n,c,q,p in items: db().execute("INSERT INTO bill_items(bill_id,product_name,category,quantity,unit_price) VALUES(?,?,?,?,?)",(cur.lastrowid,n,c,q,p))
            db().commit(); flash("Bill saved successfully.","success"); return redirect(url_for("bills"))
        except ValueError as e: flash(str(e),"danger")
    return render_template("bill_form.html", bill=None, items=[{"product_name":"","category":"medicine","quantity":1,"unit_price":""}],
                           extracted={}, uploaded_file=None)

@app.route("/bills/<int:bill_id>/edit", methods=["GET","POST"])
@login_required
def edit_bill(bill_id):
    con=db(); bill=con.execute("SELECT * FROM bills WHERE id=? AND user_id=?",(bill_id,g.user["id"])).fetchone()
    if not bill: return ("Not found",404)
    if request.method=="POST":
        try:
            items=parse_items(request.form); supplier=request.form.get("supplier","").strip(); inv=request.form.get("invoice_number","").strip()
            total=float(request.form.get("total") or 0); gst=float(request.form.get("gst") or 0)
            try: date.fromisoformat(request.form.get("invoice_date", ""))
            except ValueError: raise ValueError("Enter a valid invoice date.")
            if not supplier or not inv or not math.isfinite(total) or not math.isfinite(gst) or total < 0 or gst < 0:
                raise ValueError("Supplier, invoice number and a valid total are required.")
            dup=con.execute("SELECT id FROM bills WHERE user_id=? AND supplier=? AND invoice_number=? AND id<>?",(g.user["id"],supplier,inv,bill_id)).fetchone()
            if dup: raise ValueError("Duplicate invoice detected.")
            con.execute("UPDATE bills SET supplier=?,invoice_number=?,invoice_date=?,gst=?,total=?,notes=? WHERE id=?",(supplier,inv,request.form.get("invoice_date"),gst,total,request.form.get("notes",""),bill_id))
            con.execute("DELETE FROM bill_items WHERE bill_id=?",(bill_id,))
            for n,c,q,p in items: con.execute("INSERT INTO bill_items(bill_id,product_name,category,quantity,unit_price) VALUES(?,?,?,?,?)",(bill_id,n,c,q,p))
            con.commit(); flash("Bill updated.","success"); return redirect(url_for("bills"))
        except ValueError as e: flash(str(e),"danger")
    return render_template("bill_form.html",bill=bill,items=con.execute("SELECT * FROM bill_items WHERE bill_id=?",(bill_id,)).fetchall(),
                           extracted={}, uploaded_file=None)

@app.post("/bills/<int:bill_id>/delete")
@login_required
def delete_bill(bill_id):
    db().execute("DELETE FROM bills WHERE id=? AND user_id=?",(bill_id,g.user["id"])); db().commit(); flash("Bill deleted.","success"); return redirect(url_for("bills"))

@app.route("/analytics")
@login_required
def analytics():
    con=db(); uid=g.user["id"]
    suppliers=con.execute("SELECT supplier,COUNT(*) bills,SUM(total) total FROM bills WHERE user_id=? GROUP BY supplier ORDER BY total DESC",(uid,)).fetchall()
    products=con.execute("SELECT product_name,category,SUM(quantity) quantity,SUM(quantity*unit_price) spend FROM bill_items i JOIN bills b ON b.id=i.bill_id WHERE b.user_id=? GROUP BY product_name,category ORDER BY spend DESC",(uid,)).fetchall()
    return render_template("analytics.html",suppliers=suppliers,products=products)

@app.get("/export.csv")
@login_required
def export_csv():
    rows=db().execute("SELECT b.invoice_number,b.invoice_date,b.supplier,i.product_name,i.category,i.quantity,i.unit_price,b.gst,b.total FROM bills b LEFT JOIN bill_items i ON i.bill_id=b.id WHERE b.user_id=? ORDER BY b.invoice_date DESC",(g.user["id"],)).fetchall()
    out=io.StringIO(); w=csv.writer(out); w.writerow(["Invoice","Date","Supplier","Product","Category","Quantity","Unit price","GST","Bill total"]); w.writerows(rows)
    return send_file(io.BytesIO(out.getvalue().encode()), mimetype="text/csv", as_attachment=True, download_name="mediledger-bills.csv")

@app.context_processor
def globals(): return {"today": date.today().isoformat()}

if __name__=="__main__":
    init_db(); app.run(debug=False, use_reloader=False)
