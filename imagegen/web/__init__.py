from flask import Blueprint

web = Blueprint("web", __name__)


# Route modules register themselves on the shared blueprint at import time.
from . import admin, generations, pages, workspaces  # noqa: E402, F401

__all__ = ["web"]
