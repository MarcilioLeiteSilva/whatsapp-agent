"""
app/models.py

Models SQLAlchemy (ORM clássico com Column) compatíveis com seu schema Postgres atual.

OBS:
- Sua tabela leads NÃO tem rules_json (isso fica em agents).
- rules_json e rules_updated_at precisam existir em agents no banco (JSONB + timestamptz).
"""

import sqlalchemy as sa
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB

from .db import Base
from sqlalchemy import Column, Text, TIMESTAMP
from sqlalchemy.sql import func

class Client(Base):
    __tablename__ = "clients"

    id = Column(sa.Text, primary_key=True)
    name = Column(sa.Text, nullable=False)
    plan = Column(sa.Text, nullable=True, server_default=sa.text("'basic'"))
    created_at = Column(sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now())

     # Portal login
    login_token = Column(Text, nullable=True)
    login_token_created_at = Column(TIMESTAMP(timezone=True), nullable=True)
    login_token_last_used_at = Column(TIMESTAMP(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<Client id={self.id!r} name={self.name!r} plan={self.plan!r}>"


class Agent(Base):
    __tablename__ = "agents"

    id = Column(sa.Text, primary_key=True)
    client_id = Column(sa.Text, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)

    name = Column(sa.Text, nullable=False)
    instance = Column(sa.Text, nullable=False, unique=True)

    evolution_base_url = Column(sa.Text, nullable=True)
    api_key = Column(sa.Text, nullable=True)

    status = Column(sa.Text, nullable=True, server_default=sa.text("'pending'"))
    last_seen_at = Column(sa.DateTime(timezone=True), nullable=True)
    created_at = Column(sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now())

    # ✅ Regras por agente (SaaS)
    rules_json = Column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    rules_updated_at = Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())

    def __repr__(self) -> str:
        return f"<Agent id={self.id!r} client_id={self.client_id!r} instance={self.instance!r} status={self.status!r}>"


class Lead(Base):
    __tablename__ = "leads"

    id = Column(sa.BigInteger, primary_key=True)

    client_id = Column(sa.Text, nullable=False)
    agent_id = Column(sa.Text, nullable=True)

    instance = Column(sa.Text, nullable=False)
    from_number = Column(sa.Text, nullable=False)

    nome = Column(sa.Text, nullable=True)
    telefone = Column(sa.Text, nullable=True)
    assunto = Column(sa.Text, nullable=True)

    status = Column(sa.Text, nullable=False, server_default=sa.text("'iniciado'"))
    origem = Column(sa.Text, nullable=False, server_default=sa.text("'primeiro_contato'"))
    intent_detected = Column(sa.Text, nullable=True)

    first_seen_at = Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    created_at = Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    updated_at = Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())

    lead_saved = Column(sa.Boolean, nullable=False, server_default=sa.text("false"))

    __table_args__ = (
        sa.Index("idx_leads_client_created", "client_id", sa.desc("created_at")),
    )

    def __repr__(self) -> str:
        return f"<Lead id={self.id} client_id={self.client_id!r} instance={self.instance!r} from={self.from_number!r}>"
