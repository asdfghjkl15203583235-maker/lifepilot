import uuid

from pydantic import BaseModel, EmailStr, Field

from db.constants import MAX_USER_NAME_LENGTH


class UserCreateRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=MAX_USER_NAME_LENGTH)


class UserResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    email: str
    name: str
