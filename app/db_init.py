"""
app/db_init.py

DEV-only: criação automática de tabelas.
- Em produção, o ideal é Alembic (migrations).
- Em DEV, isso acelera: sobe DB vazio e o app cria o schema sozinho.
"""

import os
import logging

from .db import engine
from .models import Base

logger = logging.getLogger("agent")


def init_db_if_dev() -> None:
    """
    Cria tabelas automaticamente APENAS quando ENV=dev.

    Isso resolve erro:
    - psycopg2.errors.UndefinedTable: relation "leads" does not exist
    """
    env = os.getenv("ENV", "").strip().lower()
    if env != "dev":
        return

    try:
        Base.metadata.create_all(bind=engine)
        logger.info("DB_INIT: Base.metadata.create_all() executado (DEV)")
    except Exception as e:
        logger.error("DB_INIT_ERROR: %s", e)
