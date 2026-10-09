import os
from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, Float, Integer, String, Text, UniqueConstraint, create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./needradar.db")

Base = declarative_base()


class RawSignal(Base):
    __tablename__ = "raw_signals"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_source_source_id"),)

    id = Column(Integer, primary_key=True)
    source = Column(String(32), nullable=False, index=True)
    source_id = Column(String(128), nullable=False)
    signal_type = Column(String(32), nullable=False)
    title = Column(Text)
    description = Column(Text)
    amount = Column(Float)
    naics = Column(String(16))
    state = Column(String(8))
    posted_at = Column(DateTime)
    fetched_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    url = Column(Text)
    rating = Column(Integer)
    raw_json = Column(Text)


def get_engine(url=None):
    return create_engine(url or DATABASE_URL)


def init_db(engine=None):
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    _migrate(engine)
    return engine


def _migrate(engine):
    from sqlalchemy import inspect, text
    cols = {c["name"] for c in inspect(engine).get_columns("raw_signals")}
    if "rating" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE raw_signals ADD COLUMN rating INTEGER"))


def get_session(engine=None):
    engine = engine or get_engine()
    init_db(engine)
    return sessionmaker(bind=engine)()


def upsert_signals(session, records):
    """Insert records (list of dicts), skipping (source, source_id) duplicates.

    Returns (inserted, skipped).
    """
    inserted = skipped = 0
    for rec in records:
        exists = (
            session.query(RawSignal.id)
            .filter_by(source=rec["source"], source_id=rec["source_id"])
            .first()
        )
        if exists:
            skipped += 1
            continue
        session.add(RawSignal(**rec))
        inserted += 1
        if inserted % 500 == 0:
            session.commit()
    session.commit()
    return inserted, skipped
