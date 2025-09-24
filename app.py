# app.py
import os, io, csv, uuid, logging
from datetime import datetime, timezone
from functools import wraps
from flask import (Flask, render_template, request, jsonify, session,
                   redirect, url_for, send_file, flash)
import firebase_admin
from firebase_admin import credentials, firestore

import config

# logging
logging.basicConfig(level=logging.INFO)

# Flask
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET", "supersecretkey")

# Firebase Admin
if not firebase_admin._apps:
    cred = credentials.Certificate("firebase-key.json")
    firebase_admin.initialize_app(cred)
db = firestore.client()

# machine identifier
def get_machine_code():
    return str(uuid.getnode())

# trial decorator
def trial_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "shop_id" not in session:
            return redirect(url_for("login_page"))
        shop_id = session["shop_id"]
        shop_doc = db.collection("shops").document(shop_id).get()
        if not shop_doc.exists:
            session.clear()
            return redirect(url_for("login_page"))
        shop = shop_doc.to_dict()
        expiry = shop.get("expiry_date")
        if expiry:
            now_utc = datetime.now(timezone.utc)
            # expiry is a Firestore timestamp (aware) — compare directly with aware now_utc
            if now_utc > expiry:
                return render_template("expired.html")
        return f(*args, **kwargs)
    return decorated

# ---------- AUTH ----------
@app.route("/login")
def login_page():
    return render_template("login.html")

@app.route("/api/login", methods=["POST"])
def api_login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    if not username or not password:
        return jsonify({"success": False, "message": "Username and password required"})

    # Query shops collection for the fixed shop name
    shops_query = db.collection("shops") \
        .where("shop_name", "==", config.SHOP_NAME) \
        .where("username", "==", username) \
        .where("password", "==", password).stream()

    shop_doc = None
    for s in shops_query:
        shop_doc = s
        break

    if not shop_doc:
        return jsonify({"success": False, "message": "Incorrect credentials"})

    shop = shop_doc.to_dict()

    # Machine binding (optional)
    machine_code = get_machine_code()
    stored_code = shop.get("machine_code")
    if stored_code and stored_code != machine_code:
        return jsonify({"success": False, "message": "This account is not authorized on this machine"})

    # expiry check
    expiry = shop.get("expiry_date")
    if expiry:
        now_utc = datetime.now(timezone.utc)
        if now_utc > expiry:
            return jsonify({"success": False, "message": "Subscription expired"})

    # success: set session
    session["shop_id"] = shop_doc.id
    session["shop_name"] = config.SHOP_NAME
    logging.info("Login success: shop_id=%s", shop_doc.id)
    return jsonify({"success": True})

@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out", "info")
    return redirect(url_for("login_page"))

# ---------- DASHBOARD ----------
@app.route("/")
@trial_required
def dashboard():
    # Simple counts
    pcount = len(list(db.collection(config.products_collection()).stream()))
    scount = len(list(db.collection(config.stocks_collection()).stream()))
    ecount = len(list(db.collection(config.employees_collection()).stream()))
    salescount = len(list(db.collection(config.sales_collection()).stream()))
    return render_template("dashboard.html", prod_count=pcount, stock_count=scount,
                           emp_count=ecount, sales_count=salescount)

# ---------- PRODUCTS ----------
@app.route("/products")
@trial_required
def products_page():
    return render_template("products.html")

@app.route("/api/products", methods=["GET", "POST"])
@trial_required
def api_products():
    col = db.collection(config.products_collection())
    if request.method == "GET":
        items = [{"id": d.id, **d.to_dict()} for d in col.stream()]
        return jsonify({"products": items})
    # POST add
    name = request.form.get("name", "").strip()
    price = float(request.form.get("price", 0))
    col.add({"name": name, "price": price, "timestamp": datetime.now(timezone.utc)})
    return jsonify({"success": True})

@app.route("/api/products/<id>", methods=["PUT", "DELETE"])
@trial_required
def api_product_item(id):
    col = db.collection(config.products_collection())
    doc = col.document(id)
    if request.method == "PUT":
        data = request.form.to_dict()
        update = {}
        if "name" in data: update["name"] = data["name"]
        if "price" in data: update["price"] = float(data["price"])
        doc.update(update)
        return jsonify({"success": True})
    else:
        doc.delete()
        return jsonify({"success": True})

@app.route("/products/import", methods=["POST"])
@trial_required
def products_import():
    f = request.files.get("file")
    if not f:
        flash("No file uploaded", "danger")
        return redirect(url_for("products_page"))
    s = io.StringIO(f.stream.read().decode("utf-8"), newline=None)
    reader = csv.DictReader(s)
    col = db.collection(config.products_collection())
    for row in reader:
        name = row.get("name") or row.get("Name")
        price = float(row.get("price") or row.get("Price") or 0)
        if name:
            col.add({"name": name, "price": price, "timestamp": datetime.now(timezone.utc)})
    flash("Products imported", "success")
    return redirect(url_for("products_page"))

@app.route("/products/export")
@trial_required
def products_export():
    col = db.collection(config.products_collection())
    rows = [{"id": d.id, **d.to_dict()} for d in col.stream()]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id","name","price"])
    for r in rows:
        writer.writerow([r["id"], r.get("name",""), r.get("price",0)])
    output.seek(0)
    return send_file(io.BytesIO(output.getvalue().encode()), as_attachment=True,
                     download_name="products.csv", mimetype="text/csv")

# ---------- STOCKS ----------
@app.route("/stocks")
@trial_required
def stocks_page():
    return render_template("stocks.html")

@app.route("/api/stocks", methods=["GET", "POST"])
@trial_required
def api_stocks():
    col = db.collection(config.stocks_collection())
    if request.method == "GET":
        items = [{"id": d.id, **d.to_dict()} for d in col.stream()]
        return jsonify({"stocks": items})
    # POST add/update by name
    name = request.form.get("name", "").strip()
    quantity = int(request.form.get("quantity", 0))
    found = None
    for s in col.where("name", "==", name).stream():
        found = s
        break
    if found:
        col.document(found.id).update({"quantity": found.to_dict().get("quantity",0) + quantity})
    else:
        col.add({"name": name, "quantity": quantity, "timestamp": datetime.now(timezone.utc)})
    return jsonify({"success": True})

@app.route("/api/stocks/<id>", methods=["DELETE"])
@trial_required
def api_stock_delete(id):
    db.collection(config.stocks_collection()).document(id).delete()
    return jsonify({"success": True})

@app.route("/api/stocks/load/<id>", methods=["POST"])
@trial_required
def api_stock_load(id):
    qty = int(request.form.get("quantity", 0))
    db.collection(config.stocks_collection()).document(id).update({"quantity": firestore.Increment(qty)})
    return jsonify({"success": True})

@app.route("/api/stocks/unload/<id>", methods=["POST"])
@trial_required
def api_stock_unload(id):
    qty = int(request.form.get("quantity", 0))
    db.collection(config.stocks_collection()).document(id).update({"quantity": firestore.Increment(-qty)})
    return jsonify({"success": True})

@app.route("/stocks/import", methods=["POST"])
@trial_required
def stocks_import():
    f = request.files.get("file")
    if not f:
        flash("No file", "danger"); return redirect(url_for("stocks_page"))
    s = io.StringIO(f.stream.read().decode("utf-8"), newline=None)
    r = csv.DictReader(s)
    col = db.collection(config.stocks_collection())
    for row in r:
        name = row.get("name")
        qty = int(row.get("quantity") or 0)
        if name:
            col.add({"name": name, "quantity": qty, "timestamp": datetime.now(timezone.utc)})
    flash("Stocks imported", "success")
    return redirect(url_for("stocks_page"))

@app.route("/stocks/export")
@trial_required
def stocks_export():
    rows = [{"id": d.id, **d.to_dict()} for d in db.collection(config.stocks_collection()).stream()]
    out = io.StringIO(); w = csv.writer(out); w.writerow(["id","name","quantity"])
    for r in rows: w.writerow([r["id"], r.get("name",""), r.get("quantity",0)])
    out.seek(0)
    return send_file(io.BytesIO(out.getvalue().encode()), as_attachment=True, download_name="stocks.csv", mimetype="text/csv")

# ---------- EMPLOYEES ----------
@app.route("/employees")
@trial_required
def employees_page():
    return render_template("employees.html")

@app.route("/api/employees", methods=["GET","POST"])
@trial_required
def api_employees():
    col = db.collection(config.employees_collection())
    if request.method=="GET":
        return jsonify({"employees":[{"id": d.id, **d.to_dict()} for d in col.stream()]})
    data = {k: request.form.get(k) for k in ("name","phone","role")}
    data["timestamp"] = datetime.now(timezone.utc)
    col.add(data)
    return jsonify({"success": True})

@app.route("/api/employees/<id>", methods=["PUT","DELETE"])
@trial_required
def api_employee_item(id):
    col = db.collection(config.employees_collection())
    doc = col.document(id)
    if request.method=="PUT":
        doc.update(request.form.to_dict()); return jsonify({"success": True})
    else:
        doc.delete(); return jsonify({"success": True})

@app.route("/employees/import", methods=["POST"])
@trial_required
def employees_import():
    f = request.files.get("file")
    if not f: flash("No file", "danger"); return redirect(url_for("employees_page"))
    s = io.StringIO(f.stream.read().decode("utf-8"), newline=None); r = csv.DictReader(s)
    col = db.collection(config.employees_collection())
    for row in r:
        col.add({"name": row.get("name"), "phone": row.get("phone"), "role": row.get("role"), "timestamp": datetime.now(timezone.utc)})
    flash("Employees imported", "success"); return redirect(url_for("employees_page"))

@app.route("/employees/export")
@trial_required
def employees_export():
    rows = [{"id": d.id, **d.to_dict()} for d in db.collection(config.employees_collection()).stream()]
    out = io.StringIO(); w = csv.writer(out); w.writerow(["id","name","phone","role"])
    for r in rows: w.writerow([r["id"], r.get("name",""), r.get("phone",""), r.get("role","")])
    out.seek(0)
    return send_file(io.BytesIO(out.getvalue().encode()), as_attachment=True, download_name="employees.csv", mimetype="text/csv")

# ---------- ATTENDANCE ----------
@app.route("/attendance")
@trial_required
def attendance_page():
    return render_template("attendance.html")

@app.route("/api/attendance", methods=["GET","POST"])
@trial_required
def api_attendance():
    col = db.collection(config.attendance_collection())
    if request.method=="GET":
        return jsonify({"attendance":[{"id": d.id, **d.to_dict()} for d in col.stream()]})
    data = {"employee_id": request.form.get("employee_id"), "status": request.form.get("status", "in"), "timestamp": datetime.now(timezone.utc)}
    col.add(data)
    return jsonify({"success": True})

@app.route("/api/attendance/<id>", methods=["DELETE"])
@trial_required
def api_attendance_delete(id):
    db.collection(config.attendance_collection()).document(id).delete()
    return jsonify({"success": True})

@app.route("/attendance/import", methods=["POST"])
@trial_required
def attendance_import():
    f = request.files.get("file")
    if not f: flash("No file", "danger"); return redirect(url_for("attendance_page"))
    s = io.StringIO(f.stream.read().decode("utf-8"), newline=None); r = csv.DictReader(s)
    col = db.collection(config.attendance_collection())
    for row in r:
        col.add({"employee_id": row.get("employee_id"), "status": row.get("status"), "timestamp": datetime.now(timezone.utc)})
    flash("Attendance imported", "success"); return redirect(url_for("attendance_page"))

@app.route("/attendance/export")
@trial_required
def attendance_export():
    rows = [{"id": d.id, **d.to_dict()} for d in db.collection(config.attendance_collection()).stream()]
    out = io.StringIO(); w = csv.writer(out); w.writerow(["id","employee_id","status","timestamp"])
    for r in rows:
        ts = r.get("timestamp"); ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else ""
        w.writerow([r["id"], r.get("employee_id",""), r.get("status",""), ts_str])
    out.seek(0)
    return send_file(io.BytesIO(out.getvalue().encode()), as_attachment=True, download_name="attendance.csv", mimetype="text/csv")

# ---------- EXPENSES ----------
@app.route("/expenses")
@trial_required
def expenses_page():
    return render_template("expenses.html")

@app.route("/api/expenses", methods=["GET","POST"])
@trial_required
def api_expenses():
    col = db.collection(config.expenses_collection())
    if request.method == "GET":
        return jsonify({"expenses":[{"id": d.id, **d.to_dict()} for d in col.stream()]})
    data = {"title": request.form.get("title"), "amount": float(request.form.get("amount") or 0), "timestamp": datetime.now(timezone.utc), "note": request.form.get("note","")}
    col.add(data)
    return jsonify({"success": True})

@app.route("/api/expenses/<id>", methods=["PUT","DELETE"])
@trial_required
def api_expense_item(id):
    col = db.collection(config.expenses_collection())
    if request.method=="PUT":
        col.document(id).update(request.form.to_dict()); return jsonify({"success": True})
    else:
        col.document(id).delete(); return jsonify({"success": True})

@app.route("/expenses/import", methods=["POST"])
@trial_required
def expenses_import():
    f = request.files.get("file")
    if not f: flash("No file", "danger"); return redirect(url_for("expenses_page"))
    s = io.StringIO(f.stream.read().decode("utf-8"), newline=None); r = csv.DictReader(s)
    col = db.collection(config.expenses_collection())
    for row in r:
        col.add({"title": row.get("title"), "amount": float(row.get("amount") or 0), "note": row.get("note",""), "timestamp": datetime.now(timezone.utc)})
    flash("Expenses imported", "success"); return redirect(url_for("expenses_page"))

@app.route("/expenses/export")
@trial_required
def expenses_export():
    rows = [{"id": d.id, **d.to_dict()} for d in db.collection(config.expenses_collection()).stream()]
    out = io.StringIO(); w = csv.writer(out); w.writerow(["id","title","amount","note","timestamp"])
    for r in rows:
        ts = r.get("timestamp"); ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else ""
        w.writerow([r["id"], r.get("title",""), r.get("amount",0), r.get("note",""), ts_str])
    out.seek(0)
    return send_file(io.BytesIO(out.getvalue().encode()), as_attachment=True, download_name="expenses.csv", mimetype="text/csv")

# ---------- VENDORS ----------
@app.route("/vendors")
@trial_required
def vendors_page():
    return render_template("vendors.html")

@app.route("/api/vendors", methods=["GET","POST"])
@trial_required
def api_vendors():
    col = db.collection(config.vendors_collection())
    if request.method == "GET":
        return jsonify({"vendors":[{"id": d.id, **d.to_dict()} for d in col.stream()]})
    data = {"name": request.form.get("name"), "phone": request.form.get("phone"), "address": request.form.get("address",""), "timestamp": datetime.now(timezone.utc)}
    col.add(data)
    return jsonify({"success": True})

@app.route("/api/vendors/<id>", methods=["PUT","DELETE"])
@trial_required
def api_vendor_item(id):
    col = db.collection(config.vendors_collection())
    if request.method=="PUT":
        col.document(id).update(request.form.to_dict()); return jsonify({"success": True})
    else:
        col.document(id).delete(); return jsonify({"success": True})

@app.route("/vendors/import", methods=["POST"])
@trial_required
def vendors_import():
    f = request.files.get("file")
    if not f: flash("No file", "danger"); return redirect(url_for("vendors_page"))
    s = io.StringIO(f.stream.read().decode("utf-8"), newline=None); r = csv.DictReader(s)
    col = db.collection(config.vendors_collection())
    for row in r:
        col.add({"name": row.get("name"), "phone": row.get("phone"), "address": row.get("address",""), "timestamp": datetime.now(timezone.utc)})
    flash("Vendors imported", "success"); return redirect(url_for("vendors_page"))

@app.route("/vendors/export")
@trial_required
def vendors_export():
    rows = [{"id": d.id, **d.to_dict()} for d in db.collection(config.vendors_collection()).stream()]
    out = io.StringIO(); w = csv.writer(out); w.writerow(["id","name","phone","address"])
    for r in rows: w.writerow([r["id"], r.get("name",""), r.get("phone",""), r.get("address","")])
    out.seek(0)
    return send_file(io.BytesIO(out.getvalue().encode()), as_attachment=True, download_name="vendors.csv", mimetype="text/csv")

# ---------- SALES ----------
@app.route("/sales")
@trial_required
def sales_page():
    return render_template("sales.html")

@app.route("/api/add_sale", methods=["POST"])
@trial_required
def api_add_sale():
    product_id = request.form.get("product")
    qty = int(request.form.get("quantity", 1))
    payment_mode = request.form.get("payment_mode", "Cash")
    prod_doc = db.collection(config.products_collection()).document(product_id).get()
    if not prod_doc.exists:
        return jsonify({"success": False, "message": "Product not found"})
    prod = prod_doc.to_dict()
    sale = {"product_id": product_id, "item": prod.get("name"), "quantity": qty, "price": prod.get("price",0), "total": qty * prod.get("price",0), "payment_mode": payment_mode, "timestamp": datetime.now(timezone.utc)}
    db.collection(config.sales_collection()).add(sale)
    # return last 5
    rows = []
    for s in db.collection(config.sales_collection()).order_by("timestamp", direction=firestore.Query.DESCENDING).limit(5).stream():
        d = s.to_dict(); d["id"] = s.id; d["timestamp"] = d["timestamp"].strftime("%Y-%m-%d %H:%M:%S"); rows.append(d)
    return jsonify({"success": True, "last_sales": rows})

@app.route("/api/sales", methods=["GET"])
@trial_required
def api_sales_list():
    rows = []
    for s in db.collection(config.sales_collection()).order_by("timestamp", direction=firestore.Query.DESCENDING).stream():
        d = s.to_dict(); d["id"] = s.id; d["timestamp"] = d["timestamp"].strftime("%Y-%m-%d %H:%M:%S"); rows.append(d)
    return jsonify({"sales": rows})

# ---------- REPORTS ----------
@app.route("/reports")
@trial_required
def reports_page():
    prods = [{"id": d.id, "name": d.to_dict().get("name")} for d in db.collection(config.products_collection()).stream()]
    return render_template("reports.html", products=prods)

@app.route("/api/reports", methods=["POST"])
@trial_required
def api_reports():
    data = request.get_json() or {}
    start = data.get("start_date")
    end = data.get("end_date")
    product_id = data.get("product_id")
    query = db.collection(config.sales_collection())
    # apply date filters (convert to timezone-aware)
    if start:
        try:
            sdt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
            query = query.where("timestamp", ">=", sdt)
        except:
            pass
    if end:
        try:
            edt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
            query = query.where("timestamp", "<=", edt)
        except:
            pass
    if product_id:
        prod_doc = db.collection(config.products_collection()).document(product_id).get()
        if prod_doc.exists:
            pname = prod_doc.to_dict().get("name")
            query = query.where("item", "==", pname)
    rows = []
    for s in query.order_by("timestamp", direction=firestore.Query.DESCENDING).stream():
        d = s.to_dict()
        d["timestamp"] = d["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
        rows.append(d)
    # consolidated by date
    consolidated_by_date = {}
    consolidated_by_product = {}
    for r in rows:
        date_key = r["timestamp"][:10]
        consolidated_by_date.setdefault(date_key, {"qty":0, "amount":0})
        consolidated_by_date[date_key]["qty"] += int(r.get("quantity",0))
        consolidated_by_date[date_key]["amount"] += float(r.get("total",0))
        prod_key = r.get("item")
        consolidated_by_product.setdefault(prod_key, {})
        pmode = r.get("payment_mode","Cash")
        consolidated_by_product[prod_key].setdefault(pmode, {"qty":0, "amount":0})
        consolidated_by_product[prod_key][pmode]["qty"] += int(r.get("quantity",0))
        consolidated_by_product[prod_key][pmode]["amount"] += float(r.get("total",0))
    return jsonify({"rows": rows, "by_date": consolidated_by_date, "by_product_payment": consolidated_by_product})

# ---------- Download CSV generic helper ----------
def rows_to_csv_bytes(rows, header):
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(header)
    for r in rows: w.writerow([r.get(k,"") for k in header])
    out.seek(0)
    return io.BytesIO(out.getvalue().encode())

@app.route("/download/sales")
@trial_required
def download_sales():
    rows = []
    for s in db.collection(config.sales_collection()).order_by("timestamp").stream():
        d = s.to_dict(); d["timestamp"] = d["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
        rows.append(d)
    header = ["item","quantity","price","total","payment_mode","timestamp"]
    buf = rows_to_csv_bytes(rows, header)
    return send_file(buf, as_attachment=True, download_name="sales.csv", mimetype="text/csv")

# ---------- run ----------
if __name__ == "__main__":
    app.run(debug=True)
