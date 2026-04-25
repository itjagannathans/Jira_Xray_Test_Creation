"""Admin blueprint: dashboard, user management, history views, Excel export."""
from __future__ import annotations

import io
from datetime import datetime
from functools import wraps

from flask import (Blueprint, abort, current_app, flash, redirect,
                   render_template, request, send_file, url_for)
from flask_login import current_user, login_required
from openpyxl import Workbook
from werkzeug.security import generate_password_hash

from . import db as dbm


admin_bp = Blueprint("admin", __name__)


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


@admin_bp.route("/")
@admin_required
def dashboard():
    db_path = current_app.config["DATABASE"]
    users = dbm.list_users(db_path)
    xray_count = len(dbm.list_xray_tests(db_path))
    export_count = len(dbm.list_exports(db_path))
    return render_template(
        "admin/dashboard.html",
        users=users,
        xray_count=xray_count,
        export_count=export_count,
    )


@admin_bp.route("/users")
@admin_required
def users():
    db_path = current_app.config["DATABASE"]
    return render_template("admin/users.html", users=dbm.list_users(db_path))


@admin_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_user(user_id: int):
    db_path = current_app.config["DATABASE"]
    row = dbm.get_user_by_id(db_path, user_id)
    if not row:
        abort(404)

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        jira_pat = (request.form.get("jira_pat") or "").strip() or None
        is_admin = bool(request.form.get("is_admin"))
        new_password = request.form.get("new_password") or ""

        if not username:
            flash("Username cannot be empty.", "error")
            return render_template("admin/edit_user.html", user=row)

        dbm.update_user(db_path, user_id, username=username,
                        jira_pat=jira_pat, is_admin=is_admin)
        if new_password:
            if len(new_password) < 8:
                flash("Password must be at least 8 characters.", "error")
                return render_template("admin/edit_user.html", user=row)
            with dbm.connect(db_path) as conn:
                conn.execute("UPDATE Tb_user SET password_hash = ? WHERE id = ?",
                             (generate_password_hash(new_password), user_id))
        flash("User updated.", "success")
        return redirect(url_for("admin.users"))

    return render_template("admin/edit_user.html", user=row)


@admin_bp.route("/users/<int:user_id>/toggle-active", methods=["POST"])
@admin_required
def toggle_active(user_id: int):
    db_path = current_app.config["DATABASE"]
    row = dbm.get_user_by_id(db_path, user_id)
    if not row:
        abort(404)
    if row["username"] == current_user.username:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("admin.users"))
    dbm.set_user_active(db_path, user_id, not bool(row["is_active"]))
    flash("User status updated.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id: int):
    db_path = current_app.config["DATABASE"]
    row = dbm.get_user_by_id(db_path, user_id)
    if not row:
        abort(404)
    if row["username"] == current_user.username:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin.users"))
    dbm.delete_user(db_path, user_id)
    flash("User deleted.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/xray-history")
@admin_required
def xray_history():
    db_path = current_app.config["DATABASE"]
    return render_template("admin/xray_history.html",
                           rows=dbm.list_xray_tests(db_path))


@admin_bp.route("/export-history")
@admin_required
def export_history():
    db_path = current_app.config["DATABASE"]
    return render_template("admin/export_history.html",
                           rows=dbm.list_exports(db_path))


@admin_bp.route("/export-activity")
@admin_required
def export_activity():
    db_path = current_app.config["DATABASE"]
    users = dbm.list_users(db_path)
    xrays = dbm.list_xray_tests(db_path)
    exports = dbm.list_exports(db_path)

    wb = Workbook()

    ws_users = wb.active
    ws_users.title = "Users"
    ws_users.append(["Id", "Username", "Is Admin", "Is Active", "Created Date"])
    for u in users:
        ws_users.append([u["id"], u["username"],
                         "Yes" if u["is_admin"] else "No",
                         "Yes" if u["is_active"] else "No",
                         u["created_date"]])

    ws_x = wb.create_sheet("Xray Testcases Created")
    ws_x.append(["SL_No", "Jira_id", "Test_id", "Test_Description",
                 "Test_Steps", "Test_expected", "Created_By", "Created_date"])
    for r in xrays:
        ws_x.append([r["SL_No"], r["Jira_id"], r["Test_id"],
                     r["Test_Description"], r["Test_Steps"], r["Test_expected"],
                     r["Created_By"], r["Created_date"]])

    ws_e = wb.create_sheet("Manual Test Exports")
    ws_e.append(["Sl_No", "Jira_id", "Test_name", "NoOfTest_Created",
                 "Created_By", "Created_date"])
    for r in exports:
        ws_e.append([r["Sl_No"], r["Jira_id"], r["Test_name"],
                     r["NoOfTest_Created"], r["Created_By"], r["Created_date"]])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"All_User_Activity_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(
        buf,
        as_attachment=True,
        download_name=fname,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
