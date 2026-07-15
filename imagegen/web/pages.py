from __future__ import annotations

from flask import current_app, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import select

from ..errors import ServiceError
from ..extensions import db
from ..models import WalletLedger, WorkerState
from ..serializers import ledger_dict, user_dict
from ..services import AuthService
from ..version import __version__
from ..worker_health import worker_heartbeat_is_fresh
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


def _public_chat_models() -> list[dict]:
    registry = chat_model_registry()
    return [model.public_dict() for model in registry.list()]


@web.get("/health")
def health():
    database = "ready"
    try:
        db.session.execute(select(1))
    except Exception:
        db.session.rollback()
        database = "unavailable"
    storage_status = "ready"
    try:
        current_app.extensions["image_storage"].healthcheck()
    except Exception:
        storage_status = "unavailable"
    worker = "unavailable"
    title = ""
    if database == "ready":
        try:
            runtime = services().settings.runtime()
            title = services().settings.site_title()
            state = db.session.get(WorkerState, 1)
            if (
                state is not None
                and state.worker_id is not None
                and worker_heartbeat_is_fresh(
                    state.heartbeat_at,
                    runtime.worker_heartbeat_seconds,
                )
            ):
                worker = "ready"
        except Exception:
            db.session.rollback()
            database = "unavailable"
    ok = database == storage_status == worker == "ready"
    return jsonify(
        ok=ok,
        database=database,
        storage=storage_status,
        worker=worker,
        title=title,
        version=__version__,
    ), (200 if ok else 503)


@web.get("/health/live")
def health_live():
    try:
        db.session.execute(select(1))
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, database="unavailable", version=__version__), 503
    return jsonify(ok=True, database="ready", version=__version__)


@web.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("web.studio"))
    error = ""
    status_code = 200
    if request.method == "POST":
        try:
            user = services().auth.authenticate(
                request.form.get("username", ""),
                request.form.get("password", ""),
                client_id=request.remote_addr or "",
            )
        except ServiceError as exc:
            if exc.code != "login_rate_limited":
                raise
            user = None
            error = str(exc)
            status_code = exc.status_code
        if user:
            login_user(user, remember=bool(request.form.get("remember")))
            return redirect(url_for("web.studio"))
        if not error:
            error = "用户名或密码错误"
    return render_template("pages/login.html", error=error), status_code


@web.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("web.login"))


@web.post("/account/password")
@login_required
def change_password():
    data = json_body()
    auth: AuthService = services().auth
    user = current_user._get_current_object()
    if not auth.verify_password(user, str(data.get("current_password", ""))):
        raise ServiceError("当前密码错误", status_code=403)
    remember_cookie = current_app.config.get("REMEMBER_COOKIE_NAME", "remember_token")
    remember = request.cookies.get(remember_cookie) is not None
    auth.set_password(user, str(data.get("new_password", "")))
    db.session.commit()
    logout_user()
    login_user(user, remember=remember, fresh=True)
    return jsonify(ok=True)


@web.get("/")
@login_required
def studio():
    workspace_service = services().workspaces
    runtime = services().settings.runtime()
    workspaces = workspace_service.list(current_user.id)
    if not workspaces:
        workspaces = workspace_service.ensure_starter_workspaces(current_user.id)
    return render_template(
        "pages/studio.html",
        user=user_dict(current_user),
        workspaces=[workspace_payload(workspace) for workspace in workspaces],
        max_workspaces=runtime.max_workspaces_per_user,
        runtime_settings=runtime.client_dict(),
        history_retention_days=channel_registry().queue.history_retention_days,
        channels=[
            channel.public_dict() for channel in channel_registry().list(include_disabled=False)
        ],
        chat_models=_public_chat_models(),
    )


@web.get("/api/runtime-settings")
@login_required
def runtime_settings():
    config = services().settings.editable_config()
    return jsonify(
        revision=config["revision"],
        settings=services().settings.runtime().client_dict(),
        history_retention_days=channel_registry().queue.history_retention_days,
    )


@web.get("/admin")
@admin_required
def admin_page():
    return render_template("pages/admin.html", user=user_dict(current_user))


@web.get("/api/me")
@login_required
def me():
    payload = {
        "user": user_dict(current_user),
        "spending": services().billing.spending_summary(current_user.id).public_dict(),
    }
    if request.args.get("ledger") != "0":
        entries = list(
            db.session.scalars(
                select(WalletLedger)
                .where(WalletLedger.user_id == current_user.id)
                .order_by(WalletLedger.created_at.desc())
                .limit(50)
            )
        )
        payload["ledger"] = [ledger_dict(entry) for entry in entries]
    return jsonify(payload)


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
        models=_public_chat_models(),
    )
