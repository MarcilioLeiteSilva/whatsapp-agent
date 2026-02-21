"""
app/db.py

Responsabilidade ÚNICA deste módulo:
- configurar conexão com o banco (engine)
- expor SessionLocal (sessionmaker)

⚠️ Regra de ouro:
- db.py NÃO deve importar módulos do app (models, lead_logger, etc.)
  para evitar import circular.
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


# -----------------------------------------------------------------------------
# Database URL
# -----------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não definido no .env / env vars")

# -----------------------------------------------------------------------------
# Engine options
# -----------------------------------------------------------------------------
# SQLite precisa do check_same_thread=False quando roda em app async/web.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

# Pools só fazem sentido em Postgres/MySQL. Em SQLite, ignore pool_size/max_overflow.
engine_kwargs = {
    "pool_pre_ping": True,
}

if not DATABASE_URL.startswith("sqlite"):
    engine_kwargs.update(
        {
            "pool_size": int(os.getenv("DB_POOL_SIZE", "5")),
            "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "10")),
        }
    )

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    **engine_kwargs,
)

# -----------------------------------------------------------------------------
# Session factory
# -----------------------------------------------------------------------------
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

