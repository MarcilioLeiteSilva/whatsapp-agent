from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import Column
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
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    foreign,
)

# =============================================================================
# Base declarativa (SQLAlchemy 2.0 style)
# =============================================================================
class Base(DeclarativeBase):
    """
    Base declarativa para os models do SQLAlchemy.

    Observação:
    - Este arquivo define apenas modelos e relações.
    - Conexão/engine/session ficam em app/db.py (separação de responsabilidades).
    """
    pass


# =============================================================================
# SaaS Multi-tenant: Clients e Agents
# =============================================================================
class Client(Base):
    """
    Cliente (tenant) do SaaS.
    Um client pode ter N agents (multi-agente / multi-instância).
    """
    __tablename__ = "clients"

    # IDs como Text permitem usar UUID/slug sem depender de sequências.
    id: Mapped[str] = mapped_column(Text, primary_key=True)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[str] = mapped_column(Text, nullable=False, default="basic")

    created_at: Mapped[object] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # Relationship: Client -> Agents (1:N)
    agents: Mapped[list["Agent"]] = relationship(
        "Agent",
        back_populates="client",
        cascade="all, delete-orphan",
    )


class Agent(Base):
    """
    Agente do WhatsApp dentro de um cliente (tenant).

    Conceito:
    - Um Agent geralmente corresponde a 1 "instance" na Evolution.
    - A "instance" precisa ser UNIQUE para rotear corretamente: instance -> agent.
    """
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(Text, primary_key=True)

    client_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)

    # Instance do Evolution (chave de roteamento no webhook)
    instance: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)

    # Campos opcionais (caso cada agente tenha base_url/api_key própria)
    evolution_base_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    api_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Status administrativo do agente/instância
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    last_seen_at: Mapped[Optional[object]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[object] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # Relationship: Agent -> Client (N:1)
    client: Mapped["Client"] = relationship("Client", back_populates="agents")

    # Relationship opcional: Agent -> Leads (1:N)
    #
    # ⚠️ IMPORTANTE:
    # - Como a coluna leads.agent_id atualmente NÃO tem ForeignKey("agents.id"),
    #   não podemos fazer um relationship "normal" aqui sem migration.
    # - Se você quiser esse relacionamento "de verdade", o ideal é adicionar FK via Alembic.
    #
    # Por enquanto, deixamos a relação apenas no Lead.agent (viewonly) para não quebrar.
    #
    # Se um dia você migrar e adicionar FK, você pode habilitar:
    # leads: Mapped[list["Lead"]] = relationship("Lead", back_populates="agent")
    # (e ajustar Lead.agent = relationship(... back_populates="leads") )


# =============================================================================
# Leads
# =============================================================================
class Lead(Base):
    """
    Lead capturado do WhatsApp.

    Observações de modelagem:
    - client_id e agent_id são os campos chave para multi-tenant/multi-agente.
    - agent_id é Text e hoje NÃO tem FK no banco (compat/legacy), então:
      - o relacionamento Lead.agent precisa usar foreign() no join
      - e é viewonly=True (não dependemos disso para o runtime funcionar)
    """
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Multi-tenant fields
    client_id: Mapped[str] = mapped_column(Text, nullable=False, default="default", index=True)

    # ⚠️ Sem FK por enquanto (para não exigir migration imediata).
    # Se/Quando você migrar, o ideal é:
    # agent_id = mapped_column(Text, ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True)
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

    # -------------------------------------------------------------------------
    # Relationship opcional: Lead -> Agent
    # -------------------------------------------------------------------------
    # Como leads.agent_id NÃO tem ForeignKey para agents.id, o SQLAlchemy não
    # consegue inferir qual lado é "foreign". Por isso usamos foreign(agent_id).
    #
    # viewonly=True:
    # - essa relação serve para leitura (debug/painel), mas não é usada para
    #   persistência principal do runtime.
    agent: Mapped[Optional["Agent"]] = relationship(
        "Agent",
        primaryjoin=foreign(agent_id) == Agent.id,
        viewonly=True,
    )

    rules_json = Column(JSONB, nullable=False, default=dict)
    rules_updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
