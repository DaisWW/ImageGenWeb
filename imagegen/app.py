from __future__ import annotations

import os
import secrets
from pathlib import Path

from flask import Flask, jsonify, request, session
from flask_login import current_user, logout_user
from flask_wtf.csrf import CSRFError

from .config import (
    ChannelRegistry,
    ChatModelRegistry,
    RuntimeConfigRepository,
    RuntimeConfigService,
    SecretCipher,
)
from .container import ApplicationServices
from .errors import ServiceError
from .extensions import csrf, db, login_manager
from .models import User
from .serializers import display_amount
from .services import (
    AuthService,
    BillingService,
    ConversationService,
    GenerationService,
    SystemSettingsService,
    UserService,
    WorkspaceService,
)
from .storage import ImageStorage
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
        MAX_CONTENT_LENGTH=45 * 1024 * 1024,
        CHANNEL_CONFIG_PATH=os.environ.get(
            "CHANNEL_CONFIG_PATH", str(BASE_DIR / "config" / "channels.yaml")
        ),
        CHAT_MODEL_CONFIG_PATH=os.environ.get(
            "CHAT_MODEL_CONFIG_PATH",
            str(BASE_DIR / "config" / "chat_models.yaml"),
        ),
        IMAGE_STORAGE_PATH=os.environ.get("IMAGE_STORAGE_PATH", str(data_dir / "files")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "").lower() in {"1", "true", "yes"},
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
        WTF_CSRF_TIME_LIMIT=None,
        AUTO_CREATE_DB=os.environ.get("AUTO_CREATE_DB", "true").strip().lower()
        not in {"0", "false", "no", "off"},
    )
    if config:
        app.config.update(config)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    login_manager.login_view = "web.login"
    login_manager.login_message = "请先登录"

    storage = ImageStorage(app.config["IMAGE_STORAGE_PATH"])
    auth = AuthService()
    billing = BillingService()
    users = UserService(auth)
    repository = RuntimeConfigRepository(SecretCipher(app.config["CONFIG_ENCRYPTION_KEY"]))

    with app.app_context():
        if app.config.get("AUTO_CREATE_DB", True):
            db.create_all()
        _bootstrap_admin(app, users)
        channels = ChannelRegistry(app.config["CHANNEL_CONFIG_PATH"], repository.load_channels)
        chat_models = ChatModelRegistry(
            app.config["CHAT_MODEL_CONFIG_PATH"], repository.load_chat_models
        )

    configuration = RuntimeConfigService(repository, channels, chat_models)
    services = ApplicationServices(
        auth=auth,
        billing=billing,
        users=users,
        workspaces=WorkspaceService(
            storage,
            billing,
            BASE_DIR / "static" / "assets" / "starter-ocean-sky-reference.png",
        ),
        generations=GenerationService(channels, billing),
        conversations=ConversationService(chat_models, storage),
        settings=SystemSettingsService(),
        configuration=configuration,
    )
    app.extensions["channel_registry"] = channels
    app.extensions["chat_model_registry"] = chat_models
    app.extensions["image_storage"] = storage
    app.extensions["runtime_config_repository"] = repository
    app.extensions["imagegen_services"] = services
    app.register_blueprint(web)
    _register_handlers(app)

    @app.before_request
    def enforce_password_version():
        if not current_user.is_authenticated:
            return None
        stored = session.get("password_version")
        if stored is None:
            session["password_version"] = current_user.password_version
        elif stored != current_user.password_version:
            logout_user()
            session.clear()
        return None

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
        user = db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None
    return user if user and user.is_active else None


def _register_handlers(app: Flask) -> None:
    @app.errorhandler(ServiceError)
    def service_error(error: ServiceError):
        if request.path.startswith("/api/") or request.path.startswith("/account/"):
            return jsonify(error=str(error), code=error.code), error.status_code
        return str(error), error.status_code

    @app.errorhandler(CSRFError)
    def csrf_error(error: CSRFError):
        if request.path.startswith("/api/") or request.path.startswith("/account/"):
            return jsonify(error="页面凭证已失效，请刷新后重试", code="csrf_error"), 400
        return "页面凭证已失效，请刷新后重试", 400

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error="上传内容超过 45 MiB 限制", code="payload_too_large"), 413


def _bootstrap_admin(app: Flask, users: UserService) -> None:
    if db.session.query(User.id).first() is not None:
        return
    username = os.environ.get("ADMIN_USERNAME", "").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not username or not password:
        app.logger.warning(
            "database is empty; set ADMIN_USERNAME and ADMIN_PASSWORD to create the first admin"
        )
        return
    users.create(
        username=username,
        password=password,
        display_name="系统管理员",
        role="admin",
        actor_user_id=None,
    )
    app.logger.info("initial administrator created: %s", username)


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
