from __future__ import annotations

from flask import jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import select

from ..errors import ServiceError
from ..extensions import db
from ..models import WalletLedger
from ..serializers import ledger_dict, user_dict
from ..services import AuthService
from ..version import __version__
from . import web
from .shared import (
    admin_required,
    json_body,
    services,
    workspace_payload,
)
from .shared import (
    channels as channel_registry,
)
from .shared import (
    chat_models as chat_model_registry,
)


@web.get("/health")
def health():
    try:
        db.session.execute(select(1))
        database = "ready"
    except Exception:
        database = "unavailable"
    return jsonify(
        ok=database == "ready",
        database=database,
        title=services().settings.site_title(),
        version=__version__,
    ), (200 if database == "ready" else 503)


@web.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("web.studio"))
    error = ""
    if request.method == "POST":
        user = services().auth.authenticate(
            request.form.get("username", ""),
            request.form.get("password", ""),
        )
        if user:
            login_user(user, remember=bool(request.form.get("remember")))
            session["password_version"] = user.password_version
            return redirect(url_for("web.studio"))
        error = "用户名或密码错误"
    return render_template("pages/login.html", error=error)


@web.post("/logout")
@login_required
def logout():
    logout_user()
    session.pop("password_version", None)
    return redirect(url_for("web.login"))


@web.post("/account/password")
@login_required
def change_password():
    data = json_body()
    auth: AuthService = services().auth
    if not auth.verify_password(current_user, str(data.get("current_password", ""))):
        raise ServiceError("当前密码错误", status_code=403)
    auth.set_password(current_user, str(data.get("new_password", "")))
    db.session.commit()
    session["password_version"] = current_user.password_version
    return jsonify(ok=True)


@web.get("/")
@login_required
def studio():
    workspace_service = services().workspaces
    workspaces = workspace_service.list(current_user.id)
    if not workspaces:
        workspaces = workspace_service.ensure_starter_workspaces(current_user.id)
    return render_template(
        "pages/studio.html",
        user=user_dict(current_user),
        workspaces=[workspace_payload(workspace) for workspace in workspaces],
        max_workspaces=workspace_service.MAX_WORKSPACES,
        channels=[
            channel.public_dict() for channel in channel_registry().list(include_disabled=False)
        ],
        chat_models=[model.public_dict() for model in chat_model_registry().list()],
    )


@web.get("/admin")
@admin_required
def admin_page():
    return render_template("pages/admin.html", user=user_dict(current_user))


@web.get("/api/me")
@login_required
def me():
    entries = list(
        db.session.scalars(
            select(WalletLedger)
            .where(WalletLedger.user_id == current_user.id)
            .order_by(WalletLedger.created_at.desc())
            .limit(50)
        )
    )
    return jsonify(
        user=user_dict(current_user),
        spending=services().billing.spending_summary(current_user.id).public_dict(),
        ledger=[ledger_dict(entry) for entry in entries],
    )


@web.get("/api/channels")
@login_required
def channels():
    registry = channel_registry()
    return jsonify(
        version=registry.version[:12],
        channels=[channel.public_dict() for channel in registry.list(include_disabled=False)],
    )


@web.get("/api/chat-models")
@login_required
def chat_models():
    registry = chat_model_registry()
    return jsonify(
        version=registry.version[:12],
        models=[model.public_dict() for model in registry.list()],
    )
