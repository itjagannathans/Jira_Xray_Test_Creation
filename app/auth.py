"""Authentication blueprint: login, signup, logout."""
from __future__ import annotations

import re
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from . import db as dbm
from .models import User


auth_bp = Blueprint("auth", __name__)

ALLOWED_DOMAIN = "@brightstarlottery.com"
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _validate_email(email: str) -> str | None:
    if not email:
        return "Username (email) is required."
    if not EMAIL_RE.match(email):
        return "Please enter a valid email address."
    if not email.lower().endswith(ALLOWED_DOMAIN):
        return f"Email must end with {ALLOWED_DOMAIN}."
    return None


def _validate_password(password: str) -> str | None:
    if not password or len(password) < 8:
        return "Password must be at least 8 characters."
    return None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.landing"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if not username or not password:
            flash("Please enter both username and password.", "error")
            return render_template("login.html")

        row = dbm.get_user_by_username(current_app.config["DATABASE"], username)
        if not row or not check_password_hash(row["password_hash"], password):
            flash("Invalid username or password.", "error")
            return render_template("login.html")

        if not row["is_active"]:
            flash("Your account is deactivated. Please contact the administrator.", "error")
            return render_template("login.html")

        login_user(User.from_row(row))
        if row["is_admin"]:
            return redirect(url_for("admin.dashboard"))
        return redirect(url_for("main.landing"))

    return render_template("login.html")


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("main.landing"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        jira_pat = (request.form.get("jira_pat") or "").strip() or None

        err = _validate_email(username) or _validate_password(password)
        if err:
            flash(err, "error")
            return render_template("signup.html", username=username)

        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("signup.html", username=username)

        if dbm.get_user_by_username(current_app.config["DATABASE"], username):
            flash("A user with that username already exists.", "error")
            return render_template("signup.html", username=username)

        dbm.create_user(
            current_app.config["DATABASE"],
            username=username,
            password_hash=generate_password_hash(password),
            jira_pat=jira_pat,
            is_admin=False,
        )
        flash("Account created. Please log in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("signup.html", username="")


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
