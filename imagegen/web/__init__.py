from flask import Blueprint

web = Blueprint("web", __name__)


# 路由模块在导入时向共享 Blueprint 注册自身。
from . import admin, generations, library, pages, workspaces  # noqa: E402, F401

__all__ = ["web"]
