# app/models.py
from __future__ import annotations

from sqlalchemy import Column, Text, TIMESTAMP, ForeignKey, Integer, BigInteger, Boolean
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.schema import Identity
from .db import Base


class Client(Base):
    __tablename__ = "clients"
    id = Column(Text, primary_key=True)
    name = Column(Text, nullable=False)
    plan = Column(Text, default="basic")
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    # portal token
    login_token = Column(Text)
    login_token_created_at = Column(TIMESTAMP(timezone=True))
    login_token_last_used_at = Column(TIMESTAMP(timezone=True))

    # plan
    plan_id = Column(Text, ForeignKey("plans.id"), nullable=True)


class RuleTemplate(Base):
    __tablename__ = "rule_templates"

    id = Column(Text, primary_key=True)
    name = Column(Text, nullable=False)
    niche = Column(Text, nullable=True)
    kind = Column(Text, nullable=True)
    description = Column(Text, nullable=True)

    rules_json = Column(JSONB, nullable=False, server_default="{}")
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)


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

    # rules_json (JSONB)
    rules_json = Column(JSONB, nullable=True)
    rules_updated_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # Features
    features_override = Column(JSONB, nullable=True)
    features_override_updated_at = Column(TIMESTAMP(timezone=True), nullable=True)

    client = relationship("Client", lazy="joined")


class Lead(Base):
    __tablename__ = "leads"

    # ✅ BIGINT Identity gerado no Postgres
    id = Column(BigInteger, Identity(always=False), primary_key=True)

    client_id = Column(Text, ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    agent_id = Column(Text, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True)

    instance = Column(Text)
    from_number = Column(Text)
    nome = Column(Text)
    telefone = Column(Text)
    assunto = Column(Text)

    intent_detected = Column(Text)
    status = Column(Text)
    origem = Column(Text)

    first_seen_at = Column(TIMESTAMP(timezone=True), nullable=True)

    # ✅ use Boolean (recomendado)
    lead_saved = Column(Boolean, nullable=False, server_default="false")

    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class AgentCheck(Base):
    """
    Snapshot de monitoramento por agente.
    O monitor lê sempre o último check por agent_id.
    """
    __tablename__ = "agent_checks"

    id = Column(BigInteger, Identity(always=False), primary_key=True)
    agent_id = Column(Text, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)

    status = Column(Text, nullable=False, default="unknown")  # online|degraded|offline|unknown
    latency_ms = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)
    details = Column(JSONB, nullable=True)

    checked_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)

    agent = relationship("Agent", lazy="joined")


class Plan(Base):
    __tablename__ = "plans"
    id = Column(Text, primary_key=True)
    name = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)


class PlanFeature(Base):
    __tablename__ = "plan_features"
    plan_id = Column(Text, ForeignKey("plans.id", ondelete="CASCADE"), primary_key=True)
    key = Column(Text, primary_key=True)
    value_json = Column(JSONB, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
