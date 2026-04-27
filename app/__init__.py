"""Flask application factory."""
from __future__ import annotations

import os
import logging
from flask import Flask
from flask import got_request_exception
from flask_login import LoginManager

from .db import init_db, get_user_by_id, list_users
from .models import User

login_manager = LoginManager()
login_manager.login_view = "auth.login"


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SECRET_KEY"] = os.environ.get("APP_SECRET_KEY", "dev-secret-change-me")
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    app.config["DATABASE"] = os.environ.get(
        "APP_DATABASE",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app_data.db"),
    )

    # Persist runtime exceptions to a local file to simplify 500 diagnostics.
    log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "flask_errors.log")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.ERROR)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    ))
    app.logger.addHandler(file_handler)

    def _log_exception(sender, exception, **extra):
        sender.logger.exception("Unhandled exception", exc_info=exception)

    got_request_exception.connect(_log_exception, app)

    init_db(app.config["DATABASE"])

    @app.template_filter("dt")
    def _format_dt(value):
        if not value:
            return ""
        return str(value).replace("T", " ")

    @app.context_processor
    def _inject_name_map():
        try:
            users = list_users(app.config["DATABASE"])
            mapping = {u["username"]: (u["full_name"] or u["username"]) for u in users}
        except Exception:
            mapping = {}
        return {"name_map": mapping}

    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id: str):
        row = get_user_by_id(app.config["DATABASE"], int(user_id))
        if not row:
            return None
        return User.from_row(row)

    from .auth import auth_bp
    from .main import main_bp
    from .admin import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")

    return app
