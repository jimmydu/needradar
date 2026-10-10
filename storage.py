import os
from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
    create_engine,
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


class Extraction(Base):
    __tablename__ = "extractions"

    signal_id = Column(Integer, ForeignKey("raw_signals.id"), primary_key=True)
    p_level = Column(Integer)          # 1-5, None = 规则与 LLM 均未判定
    p_method = Column(String(8))       # rule / llm / none
    cost_hint = Column(Text)
    audience = Column(Text)
    scenario = Column(Text)
    pain_point = Column(Text)
    urgency = Column(String(8))
    current_solution = Column(Text)
    alternatives = Column(Text)
    supply_gap = Column(Text)
    evidence_json = Column(Text)       # {field: quote}，LLM 输出的原文引用
    model = Column(String(64))
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    llm_raw = Column(Text)
    extracted_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class Cluster(Base):
    __tablename__ = "clusters"

    id = Column(Integer, primary_key=True)
    run_date = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD，同日重跑覆盖
    name = Column(Text)
    summary = Column(Text)
    keywords_json = Column(Text)     # [kw, ...]
    member_ids = Column(Text)        # JSON [raw_signals.id, ...]
    sources_json = Column(Text)      # {source: count}
    p_dist_json = Column(Text)       # {"3": n, "4": n, "5": n}
    n_signals = Column(Integer)
    p_max = Column(Integer)
    median_amount = Column(Float)
    wps_p = Column(Float)
    wps_density = Column(Float)
    wps_diversity = Column(Float)
    wps_amount = Column(Float)
    wps_trend = Column(Float)
    wps_geo = Column(Float)
    wps = Column(Float)
    confidence = Column(Float)
    accessibility = Column(Float)
    final_score = Column(Float)
    software_fit = Column(Float)
    fit_method = Column(String(16))
    entry_point = Column(Text)
    barrier = Column(Float)
    barrier_note = Column(Text)
    window_start = Column(DateTime)
    window_end = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


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
    ccols = {c["name"] for c in inspect(engine).get_columns("clusters")}
    for col, ddl in (("software_fit", "ALTER TABLE clusters ADD COLUMN software_fit FLOAT"),
                     ("fit_method", "ALTER TABLE clusters ADD COLUMN fit_method VARCHAR(16)"),
                     ("entry_point", "ALTER TABLE clusters ADD COLUMN entry_point TEXT"),
                     ("barrier", "ALTER TABLE clusters ADD COLUMN barrier FLOAT"),
                     ("barrier_note", "ALTER TABLE clusters ADD COLUMN barrier_note TEXT")):
        if col not in ccols:
            with engine.begin() as conn:
                conn.execute(text(ddl))


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
