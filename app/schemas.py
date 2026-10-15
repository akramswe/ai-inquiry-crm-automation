from typing import Literal
from pydantic import BaseModel, EmailStr, Field, field_validator


class InquiryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    email: EmailStr
    company: str = Field(default="", max_length=160)
    subject: str = Field(default="", max_length=200)
    message: str = Field(min_length=5, max_length=10000)
    source: Literal["webform", "email"] = "webform"
    consent_to_follow_up: bool = False
    idempotency_key: str | None = Field(default=None, max_length=120)

    @field_validator("name", "message")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Must not be blank")
        return value.strip()


class Extraction(BaseModel):
    intent: Literal["pricing", "appointment", "support", "general"]
    priority: Literal["low", "medium", "high"]
    summary: str = Field(min_length=3, max_length=600)
    requirements: list[str] = Field(default_factory=list, max_length=10)


class WorkflowResult(BaseModel):
    inquiry_id: int
    status: str
    draft_id: int | None = None
    review_url: str = "http://localhost:8000/admin"
