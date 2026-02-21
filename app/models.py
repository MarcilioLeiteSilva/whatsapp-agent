from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import BigInteger, Text, Boolean, DateTime, func
from sqlalchemy import Column, Text, TIMESTAMP, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from .db import Base

class Client(Base):
    __tablename__ = "clients"

    id = Column(Text, primary_key=True)
    name = Column(Text, nullable=False)
    plan = Column(Text, default="basic")
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Text, primary_key=True)
    client_id = Column(Text, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    name = Column(Text, nullable=False)
    instance = Column(Text, nullable=False, unique=True)
    evolution_base_url = Column(Text)
    api_key = Column(Text)
    status = Column(Text, default="pending")
    last_seen_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    client = relationship("Client")

class Base(DeclarativeBase):
    pass

class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    client_id: Mapped[str] = mapped_column(Text, nullable=False, default="default")
    instance: Mapped[str] = mapped_column(Text, nullable=False)
    from_number: Mapped[str] = mapped_column(Text, nullable=False)

    nome: Mapped[str | None] = mapped_column(Text, nullable=True)
    telefone: Mapped[str | None] = mapped_column(Text, nullable=True)
    assunto: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(Text, nullable=False, default="iniciado")
    origem: Mapped[str] = mapped_column(Text, nullable=False, default="primeiro_contato")
    intent_detected: Mapped[str | None] = mapped_column(Text, nullable=True)

    first_seen_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[object] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    lead_saved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
