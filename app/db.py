"""
app/db.py

Conexão SQLAlchemy com Postgres.
Exporta:
- engine
- SessionLocal
- Base  (IMPORTANTE: usado pelos models)
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não definido")

# Pool básico (bom pra container)
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# ✅ Base compartilhado (o que estava faltando)
Base = declarative_base()

