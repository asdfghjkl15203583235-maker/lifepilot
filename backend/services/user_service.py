import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.user import User
from schemas.user import UserCreateRequest


async def create_user(session: AsyncSession, body: UserCreateRequest) -> User:
    user = User(email=body.email.lower(), name=body.name.strip())
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email.lower()))
    return result.scalar_one_or_none()


async def get_or_create_user_by_email(session: AsyncSession, email: str) -> User:
    user = await get_user_by_email(session, email)
    if user is not None:
        return user

    name = email.split("@")[0]
    user = User(email=email.lower(), name=name)
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user
