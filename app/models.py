from __future__ import annotations

from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Text,
    TIMESTAMP,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ==========================================================
# Base
# ==========================================================
class Base(DeclarativeBase):
    pass


# ==========================================================
# SaaS Multi-tenant
# ==========================================================
class Client(Base):
    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[str] = mapped_column(Text, nullable=False, default="basic")
    created_at: Mapped[object] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    agents: Mapped[list["Agent"]] = relationship("Agent", back_populates="client")


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    client_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    # instance do Evolution (precisa ser UNIQUE)
    instance: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)

    evolution_base_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    last_seen_at: Mapped[Optional[object]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[object] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    client: Mapped["Client"] = relationship("Client", back_populates="agents")


# ==========================================================
# Leads
# ==========================================================
class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Multi-tenant fields
    client_id: Mapped[str] = mapped_column(Text, nullable=False, default="default", index=True)
    agent_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True, index=True)

    # Legacy/compat + tracking
    instance: Mapped[str] = mapped_column(Text, nullable=False)
    from_number: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    nome: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    telefone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    assunto: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(Text, nullable=False, default="iniciado")
    origem: Mapped[str] = mapped_column(Text, nullable=False, default="primeiro_contato")
    intent_detected: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    first_seen_at: Mapped[object] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_at: Mapped[object] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[object] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    lead_saved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Optional relationship (não obrigatório pro runtime funcionar)
    agent: Mapped[Optional["Agent"]] = relationship(
        "Agent",
        primaryjoin="Lead.agent_id == Agent.id",
        viewonly=True,
    )
