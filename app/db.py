import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select
from .models import Agent
from .db import SessionLocal


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não definido no .env / env vars")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_agent_by_instance(instance: str):
    if not instance:
        return None

    with SessionLocal() as db:
        return db.execute(
            select(Agent).where(Agent.instance == instance)
        ).scalar_one_or_none()
