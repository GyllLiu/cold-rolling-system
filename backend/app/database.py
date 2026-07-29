from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"sqlite:///{(DATA_DIR / 'cold_rolling.db').as_posix()}",
)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def ensure_schema_compatibility(target_engine: Engine = engine) -> None:
    """Add local additive columns that older SQLite databases do not have."""
    inspector = inspect(target_engine)
    if "production_cards" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("production_cards")}
    definitions = {
        "transverse_plate_diff": "JSON NOT NULL DEFAULT '[]'",
        "longitudinal_plate_diff": "JSON NOT NULL DEFAULT '[]'",
        "surface_quality": "TEXT NOT NULL DEFAULT ''",
        "process_stage_records": "JSON NOT NULL DEFAULT '{}'",
    }
    with target_engine.begin() as connection:
        for name, definition in definitions.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE production_cards ADD COLUMN {name} {definition}"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
