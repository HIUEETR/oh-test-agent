"""导出 FastAPI 应用工厂，供 CLI、ASGI 服务器和测试复用。"""

from .app import create_app

__all__ = ["create_app"]
