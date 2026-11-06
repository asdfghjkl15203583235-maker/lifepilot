import asyncio
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from config import get_settings
from db.base import Base
from db.background_job_schema_migration import ensure_background_job_schedule_window_columns
from db.lifepilot_schema_migration import ensure_lifepilot_schema
from db.task_schema_migration import ensure_tasks_columns
from db.session import close_database_engine, get_engine, init_database_engine
from routers.auth import auth_router
from routers.background_jobs import background_jobs_router
from routers.tasks import tasks_router
from routers.calendar import calendar_router
from routers.chat import chat_router
from routers.conversations import conversations_router
from routers.voice import voice_router
from routers.create_entities import create_entities_router
from routers.dashboard import dashboard_router
from routers.health import health_router
from routers.life_preferences import preferences_router
from routers.life_services import life_services_router
from routers.meeting_invites import meeting_invites_router
from routers.memory import memory_router
from routers.notion import notion_router
from routers.notifications import notifications_router
from routers.pending_actions import pending_actions_router
from routers.mail163 import mail163_router
from routers.reminders import reminders_router
from routers.tencent_meeting import tencent_meeting_router
from services.embedding_service import init_embedding_client
from services.langgraph_checkpointer import (
    close_postgres_checkpointer,
    setup_postgres_checkpointer,
)
from services.redis_client import close_redis, init_redis

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _run_background_jobs_tick() -> None:
    """Poll for due scheduled jobs and deliver due reminders."""
    from db.session import _holder as db_holder

    if db_holder.session_factory is None:
        logger.warning("Background job tick skipped — database not available")
        return

    from config import get_settings as _get_settings
    from jobs.background_job_tick import run_due_background_jobs
    from jobs.reminder_tick import run_reminder_tick

    settings = _get_settings()
    async with db_holder.session_factory() as session:
        try:
            jobs = await run_due_background_jobs(session, settings)
            reminders = await run_reminder_tick(session, settings)
            await session.commit()
            if jobs or reminders:
                logger.info("Background tick processed %d jobs, delivered %d reminder(s)", jobs, reminders)
        except Exception:
            await session.rollback()
            logger.exception("Background job tick failed")


async def _run_daily_bio_job() -> None:
    """Wrapper executed by APScheduler — creates its own DB session."""
    from db.session import _holder as db_holder

    if db_holder.session_factory is None:
        logger.warning("Bio job skipped — database not available")
        return

    from config import get_settings as _get_settings
    from jobs.bio_summarization import run_bio_summarization

    settings = _get_settings()
    async with db_holder.session_factory() as session:
        try:
            await run_bio_summarization(session, settings)
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("Daily bio summarization job failed")


async def _run_inbox_listener() -> None:
    """每 5 分钟轮询邮箱：会议邀请走模板解析，其余邮件走 LLM 语义抽取。

    同一轮最后再跑一次"新邮件到达"提醒：这里不做语义判断，只把新到的邮件写成
    站内通知，前端轮询到就把右下角 Toast 弹出来。
    """
    from config import has_mail163_credentials
    from db.session import _holder as db_holder
    from services.agent_runner import build_mail163_config
    from services.inbox_listener import poll_mail163_inbox_for_events
    from services.mail163_client import Mail163Session
    from services.meeting_invite_scanner import scan_mail163_inbox_for_meeting_invites
    from services.new_mail_notifier import notify_new_inbox_mail
    from services.user_service import get_user_by_email

    if db_holder.session_factory is None:
        return
    settings = get_settings()
    if not has_mail163_credentials(settings):
        return
    config = build_mail163_config(settings)
    if config is None:
        return

    # 整轮扫描共用一条 IMAP 连接：为每封邮件重新登录会在几分钟内开出几十条连接，
    # 163 会直接重置新连接（WinError 10054）。
    mail_session = Mail163Session(config)

    async with db_holder.session_factory() as session:
        try:
            # 163 是系统级邮箱，只有邮箱主人才有对应的 user 行（登录时创建）。
            user = await get_user_by_email(session, settings.mail163_email or "")
            if user is None:
                return
            invites = await scan_mail163_inbox_for_meeting_invites(
                session,
                user_id=user.id,
                config=config,
                settings=settings,
                use_llm=True,
                mail_session=mail_session,
            )
            events = await poll_mail163_inbox_for_events(
                session,
                user_id=user.id,
                config=config,
                settings=settings,
                mail_session=mail_session,
            )
            # 已经由上面两条路径推过通知的邮件不再重复提醒（同一封邮件只弹一次）。
            handled_message_ids = {
                item.get("email_message_id")
                for item in [*invites.items, *events.items]
                if item.get("email_message_id")
            }
            new_mail = await notify_new_inbox_mail(
                session,
                user_id=user.id,
                config=config,
                skip_message_ids=handled_message_ids,
                mail_session=mail_session,
            )
            await session.commit()
            if (
                invites.scanned
                or invites.tasks_created
                or invites.errors
                or events.scanned
                or events.tasks_created
                or events.errors
                or new_mail.notified
                or new_mail.errors
            ):
                logger.info(
                    "Inbox listener — invites: %s; events: %s; new mail: %s",
                    invites.as_dict(),
                    events.as_dict(),
                    new_mail.as_dict(),
                )
        except Exception:
            await session.rollback()
            logger.exception("Inbox listener failed")
        finally:
            await asyncio.to_thread(mail_session.close)


@asynccontextmanager
async def lifespan(application: FastAPI):
    global _scheduler  # noqa: PLW0603
    settings = get_settings()

    siliconflow_key = settings.siliconflow_api_key
    logger.info(
        "SiliconFlow STT: %s",
        f"API key loaded ({len(siliconflow_key)} chars)" if siliconflow_key else "SILICONFLOW_API_KEY missing — check backend/.env",
    )

    # ── Database ──────────────────────────────────────────────────────────
    init_database_engine(settings)
    if settings.database_url and settings.init_db_schema_on_startup:
        import models  # noqa: F401  # Load ORM classes so Base.metadata includes all tables.

        engine = get_engine()
        if engine is not None:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                await conn.run_sync(Base.metadata.create_all)
                await ensure_background_job_schedule_window_columns(conn)
                await ensure_tasks_columns(conn)
                await ensure_lifepilot_schema(conn)

    # ── Redis ─────────────────────────────────────────────────────────────
    await init_redis(settings)

    # ── LangGraph durable state ───────────────────────────────────────────
    await setup_postgres_checkpointer(settings)

    # ── VoyageAI embeddings ───────────────────────────────────────────────
    init_embedding_client(settings)

    # ── Background scheduler ──────────────────────────────────────────────
    if settings.database_url:
        _scheduler = AsyncIOScheduler()
        _scheduler.add_job(
            _run_daily_bio_job,
            trigger=CronTrigger(hour=3, minute=0),
            id="daily_bio_summarization",
            replace_existing=True,
        )
        _scheduler.add_job(
            _run_background_jobs_tick,
            trigger=IntervalTrigger(minutes=1),
            id="background_jobs_tick",
            replace_existing=True,
        )
        _scheduler.add_job(
            _run_inbox_listener,
            trigger=IntervalTrigger(minutes=5),
            id="meeting_invite_scan",
            replace_existing=True,
        )
        _scheduler.start()
        logger.info(
            "APScheduler started — daily bio at 03:00; background job poll every 1 min; "
            "inbox listener (IMAP) every 5 min"
        )

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
    await close_postgres_checkpointer()
    await close_redis()
    await close_database_engine()


def create_app() -> FastAPI:
    application = FastAPI(title="Chief of Staff API", version="0.1.0", lifespan=lifespan)

    settings = get_settings()
    cors_origins = [origin.strip() for origin in settings.cors_allowed_origins.split(",") if origin.strip()]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(health_router)
    application.include_router(auth_router)
    application.include_router(chat_router)
    application.include_router(voice_router)
    application.include_router(conversations_router)
    application.include_router(create_entities_router)
    application.include_router(mail163_router)
    application.include_router(calendar_router)
    application.include_router(notion_router)
    application.include_router(notifications_router)
    application.include_router(background_jobs_router)
    application.include_router(reminders_router)
    application.include_router(tasks_router)
    application.include_router(pending_actions_router)
    application.include_router(dashboard_router)
    application.include_router(life_services_router)
    application.include_router(preferences_router)
    application.include_router(memory_router)
    application.include_router(meeting_invites_router)
    application.include_router(tencent_meeting_router)
    return application


app = create_app()
