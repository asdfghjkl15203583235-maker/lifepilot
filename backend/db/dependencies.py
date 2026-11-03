from fastapi import Depends, HTTPException, status

from config import Settings, get_settings
from db.session import has_session_factory


def verify_database_ready(settings: Settings = Depends(get_settings)) -> None:
    """Reject requests when Postgres is not configured or the engine failed to start."""
    if not settings.database_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DATABASE_URL 尚未配置",
        )
    if not has_session_factory():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="数据库会话工厂尚未初始化",
        )
