from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl


class HelpdeskEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=255)
    ticket_id: str = Field(min_length=1, max_length=255)
    customer_email: EmailStr
    message: str = Field(min_length=1, max_length=20_000)
    reply_url: HttpUrl


class WebhookAck(BaseModel):
    status: str
    event_id: str


class ArticleRef(BaseModel):
    slug: str
    title: str
    url: str


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_id: str
    ticket_id: str
    customer_email: str
    message: str
    reply_url: str
    status: str
    draft: str | None
    final_message: str | None
    articles: list[ArticleRef]
    llm_mode: str | None
    generation_attempts: int
    send_attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    approved_at: datetime | None
    sent_at: datetime | None


class ApproveRequest(BaseModel):
    message: str | None = Field(default=None, max_length=20_000)


class ActionResult(BaseModel):
    ok: bool
    status: str
