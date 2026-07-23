from __future__ import annotations

import os
import secrets
from pathlib import Path

from flask import Flask, jsonify, request
from flask_login import current_user
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import (
    ChannelRegistry,
    ChatModelRegistry,
    RuntimeConfigRepository,
    RuntimeConfigService,
    SecretCipher,
)
from .container import ApplicationServices
from .errors import ServiceError
from .extensions import compress, csrf, db, login_manager
from .integrations.matting import LucidaMattingClient
from .models import GenerationQueueState, User, WorkerState
from .serializers import display_amount
from .services import (
    AuthService,
    BillingService,
    ConversationService,
    GenerationService,
    ImageLibraryService,
    RuntimeLogService,
    SystemSettingsService,
    UserService,
    WorkspaceService,
)
from .storage import ImageStorage, InvalidImageError
from .version import __version__
from .web import web

BASE_DIR = Path(__file__).resolve().parent.parent


def create_app(config: dict | None = None) -> Flask:
    data_dir = Path(os.environ.get("IMAGEGEN_DATA_DIR", BASE_DIR / "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    secret_key = _persistent_secret(data_dir)
    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "templates"),
        static_folder=str(BASE_DIR / "static"),
    )
    app.jinja_env.filters["money"] = display_amount
    app.config.from_mapping(
        SECRET_KEY=secret_key,
        CONFIG_ENCRYPTION_KEY=os.environ.get("CONFIG_ENCRYPTION_KEY", "").strip() or secret_key,
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL", f"sqlite:///{(data_dir / 'imagegen.db').as_posix()}"
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        COMPRESS_STREAMS=True,
        MAX_CONTENT_LENGTH=45 * 1024 * 1024,
        CHANNEL_CONFIG_PATH=os.environ.get(
            "CHANNEL_CONFIG_PATH", str(BASE_DIR / "config" / "channels.yaml")
        ),
        CHAT_MODEL_CONFIG_PATH=os.environ.get(
            "CHAT_MODEL_CONFIG_PATH",
            str(BASE_DIR / "config" / "chat_models.yaml"),
        ),
        IMAGE_STORAGE_PATH=os.environ.get("IMAGE_STORAGE_PATH", str(data_dir / "files")),
        LUCIDA_MATTING_URL=os.environ.get("LUCIDA_MATTING_URL", "").strip(),
        LUCIDA_MATTING_MODEL=os.environ.get("LUCIDA_MATTING_MODEL", "lucida").strip() or "lucida",
        LUCIDA_MATTING_TIMEOUT_SECONDS=_env_float(
            "LUCIDA_MATTING_TIMEOUT_SECONDS",
            default=120.0,
            minimum=1.0,
            maximum=1800.0,
        ),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower() in {"1", "true", "yes"},
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower() in {"1", "true", "yes"},
        WTF_CSRF_TIME_LIMIT=None,
        AUTO_CREATE_DB=os.environ.get("AUTO_CREATE_DB", "true").strip().lower()
        not in {"0", "false", "no", "off"},
        TRUST_PROXY_HEADERS=os.environ.get("TRUST_PROXY_HEADERS", "").strip().lower()
        in {"1", "true", "yes"},
    )
    if config:
        app.config.update(config)
    if app.config["TRUST_PROXY_HEADERS"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    db.init_app(app)
    compress.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    login_manager.login_view = "web.login"
    login_manager.login_message = "请先登录"

    storage = ImageStorage(app.config["IMAGE_STORAGE_PATH"])
    auth = AuthService()
    billing = BillingService()
    users = UserService(auth)
    settings = SystemSettingsService()
    runtime_logs = RuntimeLogService()
    workspaces = WorkspaceService(
        storage,
        billing,
        BASE_DIR / "static" / "assets" / "starter-ocean-sky-reference.png",
        settings,
    )
    repository = RuntimeConfigRepository(SecretCipher(app.config["CONFIG_ENCRYPTION_KEY"]))

    with app.app_context():
        if app.config.get("AUTO_CREATE_DB", True):
            db.create_all()
            _bootstrap_internal_state()
        _bootstrap_admin(app, users, workspaces)
        channels = ChannelRegistry(
            app.config["CHANNEL_CONFIG_PATH"],
            repository.load_channels,
            repository.channel_revision,
        )
        chat_models = ChatModelRegistry(
            app.config["CHAT_MODEL_CONFIG_PATH"],
            repository.load_chat_models,
            repository.chat_revision,
        )

    configuration = RuntimeConfigService(repository, channels, chat_models)
    services = ApplicationServices(
        auth=auth,
        billing=billing,
        users=users,
        workspaces=workspaces,
        image_library=ImageLibraryService(storage),
        generations=GenerationService(channels, billing, settings),
        conversations=ConversationService(
            chat_models,
            storage,
            settings,
            runtime_logs,
        ),
        runtime_logs=runtime_logs,
        settings=settings,
        configuration=configuration,
    )
    app.extensions["channel_registry"] = channels
    app.extensions["chat_model_registry"] = chat_models
    app.extensions["image_storage"] = storage
    app.extensions["imagegen_services"] = services
    app.extensions["lucida_matting_client"] = LucidaMattingClient(
        base_url=str(app.config.get("LUCIDA_MATTING_URL", "") or ""),
        model=str(app.config.get("LUCIDA_MATTING_MODEL", "lucida") or "lucida"),
        timeout_seconds=float(app.config.get("LUCIDA_MATTING_TIMEOUT_SECONDS", 120.0) or 120.0),
    )
    app.register_blueprint(web)
    _register_handlers(app)

    @app.after_request
    def secure_response(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; "
            "style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
        return response

    @app.context_processor
    def inject_branding():
        return {
            "site_title": services.settings.site_title(),
            "app_version": __version__,
            "account_spending": (
                services.billing.spending_summary(current_user.id).public_dict()
                if current_user.is_authenticated
                else None
            ),
        }

    return app


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    try:
        identifier, password_version = str(user_id).split(":", 1)
        user = db.session.get(User, int(identifier))
        expected_password_version = int(password_version)
    except (TypeError, ValueError):
        return None
    if user is None or not user.is_active or user.password_version != expected_password_version:
        return None
    return user


def _register_handlers(app: Flask) -> None:
    @app.errorhandler(ServiceError)
    def service_error(error: ServiceError):
        if request.path.startswith("/api/") or request.path.startswith("/account/"):
            payload = {"error": str(error), "code": error.code}
            if error.error_id:
                payload["error_id"] = error.error_id
            return jsonify(payload), error.status_code
        return str(error), error.status_code

    @app.errorhandler(CSRFError)
    def csrf_error(_error: CSRFError):
        if request.path.startswith("/api/") or request.path.startswith("/account/"):
            return jsonify(error="页面凭证已失效，请刷新后重试", code="csrf_error"), 400
        return "页面凭证已失效，请刷新后重试", 400

    @app.errorhandler(InvalidImageError)
    def invalid_image(error: InvalidImageError):
        if request.path.startswith("/api/"):
            return jsonify(error=str(error), code="invalid_image"), 400
        return str(error), 400

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error="上传内容超过 45 MiB 限制", code="payload_too_large"), 413

    @app.errorhandler(Exception)
    def unexpected_error(error: Exception):
        if isinstance(error, HTTPException):
            return error
        db.session.rollback()
        user_id = current_user.id if current_user.is_authenticated else None
        user_label = (
            current_user.display_name or current_user.username
            if current_user.is_authenticated
            else ""
        )
        entry = app.extensions["imagegen_services"].runtime_logs.commit_best_effort(
            category="web",
            event="web.unhandled_exception",
            status="error",
            message="Web 请求发生未处理异常",
            source="web",
            user_id=user_id,
            user_label=user_label,
            error_code="internal_error",
            details={
                "exception_type": error.__class__.__name__,
                "method": request.method,
                "path": request.path,
                "endpoint": request.endpoint or "",
            },
        )
        app.logger.exception("未处理的 Web 异常：%s", request.path)
        error_id = entry.id if entry is not None else ""
        message = "服务器内部错误"
        if request.path.startswith("/api/") or request.path.startswith("/account/"):
            payload = {"error": message, "code": "internal_error"}
            if error_id:
                payload["error_id"] = error_id
            return jsonify(payload), 500
        return f"{message}{f'（错误 ID：{error_id}）' if error_id else ''}", 500


def _bootstrap_admin(app: Flask, users: UserService, workspaces: WorkspaceService) -> None:
    if db.session.query(User.id).first() is not None:
        return
    username = os.environ.get("ADMIN_USERNAME", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not username or not password:
        app.logger.warning("数据库为空；请设置 ADMIN_USERNAME 和 ADMIN_PASSWORD 以创建首个管理员")
        return
    try:
        user = users.create(
            username=username,
            password=password,
            display_name="系统管理员",
            role="admin",
            actor_user_id=None,
            commit=False,
        )
        workspaces.ensure_starter_workspaces(user.id)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    app.logger.info("已创建初始管理员：%s", username)


def _bootstrap_internal_state() -> None:
    if db.session.get(GenerationQueueState, 1) is None:
        db.session.add(GenerationQueueState(id=1))
    if db.session.get(WorkerState, 1) is None:
        db.session.add(WorkerState(id=1))
    db.session.commit()


def _persistent_secret(data_dir: Path) -> str:
    configured = os.environ.get("SECRET_KEY", "").strip()
    if configured:
        return configured
    path = data_dir / ".secret_key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_hex(32)
    path.write_text(value, encoding="utf-8")
    return value


def _env_float(
    name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))
