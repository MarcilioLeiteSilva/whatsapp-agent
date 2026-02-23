"""
app/models.py

Models SQLAlchemy (ORM clássico com Column) compatíveis com seu schema Postgres atual.

Tabelas no banco (confirmadas por você):
- clients (id TEXT PK, name TEXT NOT NULL, plan TEXT default 'basic', created_at timestamptz default now())
- agents  (id TEXT PK, client_id TEXT FK, name TEXT, instance TEXT UNIQUE, evolution_base_url TEXT, api_key TEXT,
          status TEXT default 'pending', last_seen_at timestamptz, created_at timestamptz default now()
          + (SaaS rules) rules_json JSONB default '{}' e rules_updated_at timestamptz default now() — se você migrou)
- leads   (id BIGINT PK, client_id TEXT NOT NULL, agent_id TEXT nullable, instance TEXT NOT NULL, from_number TEXT NOT NULL,
          nome/telefone/assunto TEXT, status TEXT default 'iniciado', origem TEXT default 'primeiro_contato',
          intent_detected TEXT, first_seen_at/created_at/updated_at timestamptz default now(), lead_saved boolean default false)

IMPORTANTE:
- Se sua tabela `agents` ainda não tem rules_json/rules_updated_at, rode a migração SQL:
    ALTER TABLE agents ADD COLUMN IF NOT EXISTS rules_json jsonb NOT NULL DEFAULT '{}'::jsonb;
    ALTER TABLE agents ADD COLUMN IF NOT EXISTS rules_updated_at timestamptz NOT NULL DEFAULT now();

"""

import sqlalchemy as sa
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB

from .db import Base


class Client(Base):
    __tablename__ = "clients"

    id = Column(sa.Text, primary_key=True)  # TEXT PK (sem default no banco)
    name = Column(sa.Text, nullable=False)
    plan = Column(sa.Text, nullable=True, server_default=sa.text("'basic'"))
    created_at = Column(sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now())

    def __repr__(self) -> str:
        return f"<Client id={self.id!r} name={self.name!r} plan={self.plan!r}>"


class Agent(Base):
    __tablename__ = "agents"

    id = Column(sa.Text, primary_key=True)  # TEXT PK (sem default no banco)
    client_id = Column(sa.Text, sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)

    name = Column(sa.Text, nullable=False)
    instance = Column(sa.Text, nullable=False, unique=True)

    evolution_base_url = Column(sa.Text, nullable=True)
    api_key = Column(sa.Text, nullable=True)

    status = Column(sa.Text, nullable=True, server_default=sa.text("'pending'"))
    last_seen_at = Column(sa.DateTime(timezone=True), nullable=True)
    created_at = Column(sa.DateTime(timezone=True), nullable=True, server_default=sa.func.now())

    # ✅ Regras por agente (SaaS) — JSONB no Postgres
    # Se a coluna existir no banco, isso passa a persistir corretamente.
    rules_json = Column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    rules_updated_at = Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())

    def __repr__(self) -> str:
        return f"<Agent id={self.id!r} client_id={self.client_id!r} instance={self.instance!r} status={self.status!r}>"


class Lead(Base):
    __tablename__ = "leads"

    id = Column(sa.BigInteger, primary_key=True)  # BIGINT PK (sequence no banco)

    client_id = Column(sa.Text, nullable=False)
    agent_id = Column(sa.Text, nullable=True)  # seu schema atual é TEXT nullable

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

    # Índice já existe no DB: idx_leads_client_created (client_id, created_at desc)
    # Não precisa duplicar aqui — mas não atrapalha se você estiver usando migrations.
    __table_args__ = (
        sa.Index("idx_leads_client_created", "client_id", sa.desc("created_at")),
    )

    def __repr__(self) -> str:
        return f"<Lead id={self.id} client_id={self.client_id!r} instance={self.instance!r} from={self.from_number!r}>"
