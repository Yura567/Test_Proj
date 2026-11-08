from datetime import datetime

from sqlalchemy import BigInteger, Computed, DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TicketStatus:
    NEW = "new"
    GENERATING = "generating"
    DRAFT_READY = "draft_ready"
    GENERATION_FAILED = "generation_failed"
    APPROVED = "approved"
    SENDING = "sending"
    SENT = "sent"
    SEND_FAILED = "send_failed"
    REJECTED = "rejected"

    ALL = (
        NEW,
        GENERATING,
        DRAFT_READY,
        GENERATION_FAILED,
        APPROVED,
        SENDING,
        SENT,
        SEND_FAILED,
        REJECTED,
    )


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(255), unique=True)
    ticket_id: Mapped[str] = mapped_column(String(255), index=True)
    customer_email: Mapped[str] = mapped_column(String(320))
    message: Mapped[str] = mapped_column(Text)
    reply_url: Mapped[str] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(32), server_default=TicketStatus.NEW)
    draft: Mapped[str | None] = mapped_column(Text)
    final_message: Mapped[str | None] = mapped_column(Text)
    articles: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    llm_mode: Mapped[str | None] = mapped_column(String(16))

    generation_attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    send_attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_tickets_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_tickets_created_at", "created_at"),
    )


class KbArticle(Base):
    __tablename__ = "kb_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    keywords: Mapped[str] = mapped_column(Text, server_default="")
    body: Mapped[str] = mapped_column(Text)
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('simple'::regconfig, coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('simple'::regconfig, coalesce(keywords, '')), 'A') || "
            "setweight(to_tsvector('simple'::regconfig, coalesce(body, '')), 'B')",
            persisted=True,
        ),
    )

    __table_args__ = (Index("ix_kb_articles_search", "search_vector", postgresql_using="gin"),)


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("clock_timestamp()"), index=True
    )
