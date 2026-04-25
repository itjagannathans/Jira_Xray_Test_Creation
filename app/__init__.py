"""Flask application factory."""
from __future__ import annotations

import os
from flask import Flask
from flask_login import LoginManager

from .db import init_db, get_user_by_id
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

    init_db(app.config["DATABASE"])

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
