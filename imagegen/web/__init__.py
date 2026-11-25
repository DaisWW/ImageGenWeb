from flask import Blueprint

web = Blueprint("web", __name__)


# 路由模块在导入时向共享 Blueprint 注册自身。
from . import (  # noqa: E402, F401
    admin,
    generation_api,
    generations,
    image_tools,
    library,
    pages,
    workspaces,
)

__all__ = ["web"]
