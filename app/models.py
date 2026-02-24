# app/models.py
from __future__ import annotations

from sqlalchemy import (
    Column,
    Text,
    BigInteger,
    Integer,
    Boolean,
    TIMESTAMP,
    ForeignKey,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from .db import Base


class Client(Base):
    __tablename__ = "clients"

    id = Column(Text, primary_key=True)
    name = Column(Text, nullable=False)
    plan = Column(Text, default="basic")
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    # Portal login (cliente)
    login_token = Column(Text, nullable=True)
    login_token_created_at = Column(TIMESTAMP(timezone=True), nullable=True)
    login_token_last_used_at = Column(TIMESTAMP(timezone=True), nullable=True)


class Agent(Base):
    __tablename__ = "agents"

    id = Column(Text, primary_key=True)
    client_id = Column(Text, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)

    name = Column(Text, nullable=False)
    instance = Column(Text, nullable=False, unique=True)

    evolution_base_url = Column(Text, nullable=True)
    api_key = Column(Text, nullable=True)

    status = Column(Text, default="pending")  # active|pending|disabled
    last_seen_at = Column(TIMESTAMP(timezone=True), nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    # rules_json por agente
    rules_json = Column(JSONB, nullable=False, server_default="{}")
    rules_updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Lead(Base):
    __tablename__ = "leads"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    client_id = Column(Text, nullable=False)
    agent_id = Column(Text, nullable=True)

    instance = Column(Text, nullable=False)
    from_number = Column(Text, nullable=False)

    nome = Column(Text, nullable=True)
    telefone = Column(Text, nullable=True)
    assunto = Column(Text, nullable=True)

    status = Column(Text, nullable=True)
    origem = Column(Text, nullable=True)
    intent_detected = Column(Text, nullable=True)

    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class AgentCheck(Base):
    """
    Histórico de saúde do agente.
    Alimentado por:
      - POLL (scheduler) => mode='poll'
      - PUSH (futuro)    => mode='push'
    """
    __tablename__ = "agent_checks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    client_id = Column(Text, nullable=False)
    agent_id = Column(Text, nullable=False)
    instance = Column(Text, nullable=False)

    mode = Column(Text, nullable=False, default="poll")          # poll|push
    status = Column(Text, nullable=False, default="unknown")     # online|degraded|offline|unknown

    latency_ms = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)

    checked_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
