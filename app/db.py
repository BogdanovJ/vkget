from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=1800,
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

def ensure_schema():
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    if "subscriptions" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("subscriptions")}
    if "title_is_custom" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE subscriptions "
                    "ADD COLUMN title_is_custom BOOLEAN NOT NULL DEFAULT 0"
                )
            )
        columns.add("title_is_custom")
    if "last_scan_result" not in columns:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE subscriptions ADD COLUMN last_scan_result TEXT NULL")
            )
    if "videos" in inspector.get_table_names():
        video_columns = {col["name"] for col in inspector.get_columns("videos")}
        if "queue_rank" not in video_columns:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE videos "
                        "ADD COLUMN queue_rank INTEGER NOT NULL DEFAULT 0"
                    )
                )
    # vpn_endpoints is left in place if it already exists. New installs use vpn_profiles.

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
