from pathlib import Path
from typing import Generator
from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine

from .config import settings


def _ensure_sqlite_dir(db_url: str) -> None:
    """Create parent directory for file-backed SQLite databases if missing."""
    if db_url.startswith("sqlite:///") and ":memory:" not in db_url:
        raw_path = db_url.replace("sqlite:///", "", 1)
        db_file = Path(raw_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_dir(settings.database_url)

connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False, "timeout": 15}

engine = create_engine(
    settings.database_url,
    echo=False,
    connect_args=connect_args,
)

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _configure_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=15000;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()


def init_db() -> None:
    """Create all SQLModel tables and ensure schema migrations on existing SQLite files."""
    from . import models  # noqa: F401 — ensure all SQLModel table classes are registered

    SQLModel.metadata.create_all(engine)

    if settings.database_url.startswith("sqlite"):
        with engine.begin() as conn:
            cols = [
                row[1]
                for row in conn.execute(text("PRAGMA table_info(ai_briefings);")).fetchall()
            ]
            if cols and "cards_json" not in cols:
                conn.execute(text("ALTER TABLE ai_briefings ADD COLUMN cards_json TEXT;"))

            chat_cols = [
                row[1]
                for row in conn.execute(text("PRAGMA table_info(chat_messages);")).fetchall()
            ]
            if chat_cols and "thread_id" not in chat_cols:
                conn.execute(text("ALTER TABLE chat_messages ADD COLUMN thread_id TEXT DEFAULT 'main';"))

            task_cols = [
                row[1]
                for row in conn.execute(text("PRAGMA table_info(tasks);")).fetchall()
            ]
            if task_cols:
                if "priority" not in task_cols:
                    conn.execute(text("ALTER TABLE tasks ADD COLUMN priority TEXT DEFAULT 'normal';"))
                if "estimated_minutes" not in task_cols:
                    conn.execute(text("ALTER TABLE tasks ADD COLUMN estimated_minutes INTEGER;"))
                if "scheduled_block" not in task_cols:
                    conn.execute(text("ALTER TABLE tasks ADD COLUMN scheduled_block TEXT;"))
                if "deferred_from_date" not in task_cols:
                    conn.execute(text("ALTER TABLE tasks ADD COLUMN deferred_from_date TEXT;"))
                if "sort_order" not in task_cols:
                    conn.execute(text("ALTER TABLE tasks ADD COLUMN sort_order INTEGER DEFAULT 0;"))

            notif_cols = [
                row[1]
                for row in conn.execute(text("PRAGMA table_info(notifications);")).fetchall()
            ]
            if notif_cols:
                if "ai_summary" not in notif_cols:
                    conn.execute(text("ALTER TABLE notifications ADD COLUMN ai_summary TEXT;"))
                if "is_important" not in notif_cols:
                    conn.execute(text("ALTER TABLE notifications ADD COLUMN is_important INTEGER DEFAULT 1;"))
                if "importance_score" not in notif_cols:
                    conn.execute(text("ALTER TABLE notifications ADD COLUMN importance_score REAL DEFAULT 0.75;"))
                if "importance_reason" not in notif_cols:
                    conn.execute(text("ALTER TABLE notifications ADD COLUMN importance_reason TEXT;"))
                if "user_importance_override" not in notif_cols:
                    conn.execute(text("ALTER TABLE notifications ADD COLUMN user_importance_override INTEGER;"))
                if "category" not in notif_cols:
                    conn.execute(text("ALTER TABLE notifications ADD COLUMN category TEXT DEFAULT 'General';"))
                if "ai_processed" not in notif_cols:
                    conn.execute(text("ALTER TABLE notifications ADD COLUMN ai_processed INTEGER DEFAULT 0;"))
                # ---- v10.2 tag-first Inbox columns
                v102_notif = {
                    "tags_csv": "TEXT DEFAULT ''",
                    "sender": "TEXT",
                    "body_text": "TEXT",
                    "body_fetched_at": "DATETIME",
                    "dedupe_key": "TEXT",
                    "dedupe_sources": "TEXT",
                    "merged_count": "INTEGER DEFAULT 1",
                    "thread_count": "INTEGER DEFAULT 0",
                    "has_attachment": "INTEGER DEFAULT 0",
                    "starred": "INTEGER DEFAULT 0",
                    "folder": "TEXT DEFAULT 'inbox'",
                    "tag_confidence": "REAL DEFAULT 0",
                    "tag_engine": "TEXT",
                    "tag_corrected_by_user": "INTEGER DEFAULT 0",
                }
                for col, ddl in v102_notif.items():
                    if col not in notif_cols:
                        conn.execute(text(f"ALTER TABLE notifications ADD COLUMN {col} {ddl};"))
                try:
                    conn.execute(
                        text("CREATE INDEX IF NOT EXISTS ix_notifications_dedupe_key ON notifications (dedupe_key);")
                    )
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_notifications_tags_csv ON notifications (tags_csv);"))
                except Exception:
                    pass

                # Seed legacy single-category rows into the new tag column so the
                # tag-first UI is never empty after the upgrade.
                try:
                    conn.execute(
                        text(
                            "UPDATE notifications SET tags_csv = COALESCE(NULLIF(category,''),'General') "
                            "WHERE tags_csv IS NULL OR tags_csv = '';"
                        )
                    )
                except Exception:
                    pass


            grade_cols = [
                row[1]
                for row in conn.execute(text("PRAGMA table_info(course_grades);")).fetchall()
            ]
            if grade_cols:
                if "scores_url" not in grade_cols:
                    conn.execute(text("ALTER TABLE course_grades ADD COLUMN scores_url TEXT;"))
                if "history_json" not in grade_cols:
                    conn.execute(text("ALTER TABLE course_grades ADD COLUMN history_json TEXT DEFAULT '[]';"))
                if "term_grades_json" not in grade_cols:
                    conn.execute(text("ALTER TABLE course_grades ADD COLUMN term_grades_json TEXT DEFAULT '{}';"))
                if "assignments_json" not in grade_cols:
                    conn.execute(text("ALTER TABLE course_grades ADD COLUMN assignments_json TEXT DEFAULT '[]';"))

            log_cols = [
                row[1]
                for row in conn.execute(text("PRAGMA table_info(agent_logs);")).fetchall()
            ]
            if log_cols and "revision" not in log_cols:
                conn.execute(text("ALTER TABLE agent_logs ADD COLUMN revision INTEGER DEFAULT 1;"))


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a scoped SQLModel database session."""
    with Session(engine) as session:
        yield session
