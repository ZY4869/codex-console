"""
Web UI 应用模块。
"""

__all__ = ["app", "create_app"]


def __getattr__(name):
    if name in {"app", "create_app"}:
        from .app import app, create_app

        return {"app": app, "create_app": create_app}[name]
    raise AttributeError(name)
