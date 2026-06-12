"""Authentication: password config, session secret, the ``login_required``
decorator, and the /login and /logout routes (as a Flask Blueprint).

``client`` applies ``SESSION_SECRET`` to the Flask app and registers ``bp``.
"""

import os
import secrets
from functools import wraps

from flask import (
    Blueprint, redirect, render_template_string, request, session, url_for
)

# Secret used to sign session cookies; applied to the app in client.py.
SESSION_SECRET = os.environ.get("MICROSCOPE_SESSION_SECRET") or secrets.token_hex(32)

# FIX #1: 하드코딩된 비밀번호 → 환경변수 우선, 없으면 기본값
MICROSCOPE_PASSWORD = os.environ.get("MICROSCOPE_PASSWORD", "password")

_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

bp = Blueprint("auth", __name__)


def _load_login_template():
    with open(os.path.join(_FRONTEND_DIR, "login.html"), encoding="utf-8") as f:
        return f.read()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("authenticated"):
            return view(*args, **kwargs)
        return redirect(url_for("auth.login"))
    return wrapped


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if MICROSCOPE_PASSWORD and secrets.compare_digest(pwd, MICROSCOPE_PASSWORD):
            session.clear()
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "Incorrect password" if MICROSCOPE_PASSWORD else "Password not configured"
    return render_template_string(_load_login_template(), error=error)


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
