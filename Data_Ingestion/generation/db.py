"""
Database Layer — Generation Module
====================================
Controls which database backend is used:

  LOCAL_DB=true   →  SQLite  (./local_storage/intellidraft.db)   ← default for dev
  LOCAL_DB=false  →  Any SQL DB via DATABASE_URL env var          ← production

DATABASE_URL examples:
  SQLite (default dev):  sqlite:///./local_storage/intellidraft.db
  PostgreSQL (GCP):      postgresql+psycopg2://user:pass@host/db
  Cloud SQL (GCP):       postgresql+pg8000://user:pass@/db?unix_sock=/cloudsql/proj:region:inst/.s.PGSQL.5432

Tables
------
  generation_jobs     — one per "generate document" request
  sections            — one per section within a job
  section_versions    — full Markdown content per version of each section
  section_comments    — user edit requests / approvals linked to a version
  templates           — reusable prompt templates (system + user-created)
"""

from __future__ import annotations
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text,
    create_engine, event,
)
from contextlib import contextmanager

from sqlalchemy.orm import DeclarativeBase, Session, relationship

# ─────────────────────────────────────────────────────────────────────────────
# Connection URL — local SQLite vs production DB
# ─────────────────────────────────────────────────────────────────────────────

LOCAL_DB        = os.environ.get("LOCAL_DB",        "true").lower()  == "true"
DATABRICKS_MODE = os.environ.get("DATABRICKS_MODE", "false").lower() == "true"

# DATABASE_URL is resolved below. It stays None in Databricks OAuth mode
# (no PAT / no explicit URL) — the engine is then built from connection parts
# using the App's service-principal OAuth credentials. See _make_engine().
DATABASE_URL: "str | None" = None

if LOCAL_DB:
    # ── DB path resolution ───────────────────────────────────────────────────
    # SQLite WAL-mode databases MUST NOT live inside cloud-synced folders
    # (OneDrive, Dropbox, etc.) — the sync client holds file handles that
    # prevent clean shutdown and can corrupt the WAL.
    #
    # Priority order:
    #   1. INTELLIDRAFT_DB_DIR env var — set this to override (e.g. C:\dev\intellidraft_db)
    #   2. If the default path is inside a OneDrive/Dropbox folder → redirect to
    #      %LOCALAPPDATA%\Intellidraft  (on Windows) or  ~/intellidraft_data  (Linux/Mac)
    #   3. Otherwise: Data_Ingestion/local_storage/ (the original default)
    _custom_db_dir = os.environ.get("INTELLIDRAFT_DB_DIR", "").strip()
    _default_dir   = Path(__file__).parent.parent / "local_storage"

    def _is_cloud_synced(p: Path) -> bool:
        s = str(p).lower()
        return any(x in s for x in ("onedrive", "dropbox", "google drive", "icloud"))

    if _custom_db_dir:
        _db_dir = Path(_custom_db_dir)
    elif _is_cloud_synced(_default_dir):
        # Redirect to a local-only path outside cloud sync
        if os.name == "nt":
            _db_dir = Path(os.environ.get("LOCALAPPDATA", "C:\\Users\\Public")) / "Intellidraft"
        else:
            _db_dir = Path.home() / "intellidraft_data"
        import warnings
        warnings.warn(
            f"\n  [DB] Default path is inside a cloud-synced folder — redirecting to:\n"
            f"       {_db_dir}\n"
            f"  Set INTELLIDRAFT_DB_DIR in .env to override.",
            stacklevel=2,
        )
    else:
        _db_dir = _default_dir

    _db_dir.mkdir(parents=True, exist_ok=True)
    DATABASE_URL = f"sqlite:///{(_db_dir / 'intellidraft.db').resolve()}"
else:
    # Production. Priority:
    #   1. Explicit DATABASE_URL  (any SQLAlchemy URL, incl. PAT-based databricks://)
    #   2. Databricks OAuth mode  → leave None; engine built from parts in _make_engine()
    DATABASE_URL = os.environ.get("DATABASE_URL") or None
    if DATABASE_URL is None and not DATABRICKS_MODE:
        raise KeyError(
            "DATABASE_URL is not set. Set it for production, or set "
            "DATABRICKS_MODE=true to use Databricks service-principal OAuth."
        )


# ─────────────────────────────────────────────────────────────────────────────
# SQLAlchemy engine — thread-safe SQLite config for background generation thread
# ─────────────────────────────────────────────────────────────────────────────

def _make_engine():
    if DATABASE_URL and DATABASE_URL.startswith("sqlite"):
        # QueuePool (SQLAlchemy default), NOT StaticPool: StaticPool shares ONE
        # sqlite3 connection across every thread, and concurrent requests then
        # interleave cursor state — sqlite3.InterfaceError ("bad parameter or
        # other API misuse") and corrupted rows under load. QueuePool checks a
        # connection out to exactly one thread at a time. check_same_thread stays
        # False because a pooled connection may be reused by a different thread
        # on the next checkout (never concurrently).
        # driver `timeout` = how long the sqlite3 driver itself waits on a busy
        # lock before raising (seconds). This is the most reliable busy knob;
        # the PRAGMA below reinforces it.
        engine = create_engine(
            DATABASE_URL,
            connect_args={"check_same_thread": False, "timeout": 30},
            pool_size=10,
            max_overflow=20,
            echo=False,
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            # WAL: readers never block the single writer.
            cur.execute("PRAGMA journal_mode=WAL")
            # busy_timeout: wait up to 30s for the writer instead of raising
            # "database is locked" — covers wave-parallel generation + polling.
            cur.execute("PRAGMA busy_timeout=30000")
            # synchronous=NORMAL is the recommended WAL pairing: it drops the
            # per-commit fsync, so a write transaction holds the lock for
            # microseconds instead of milliseconds — the single biggest lever
            # against write contention. Durable across app crashes; only a host
            # power-loss can lose the last transaction (regenerable here).
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
        return engine

    # Databricks OAuth mode: no explicit URL — build from parts using the App's
    # service-principal OAuth credentials (no PAT required).
    if DATABASE_URL is None:
        return _make_databricks_oauth_engine()

    # Production: explicit URL (PAT-based databricks:// or any SQLAlchemy URL).
    return create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)


def _make_databricks_oauth_engine():
    """
    Build a SQLAlchemy engine for a Databricks SQL Warehouse using OAuth
    machine-to-machine (M2M) auth — no Personal Access Token needed.

    Inside a Databricks App the service principal's OAuth credentials are
    injected automatically as DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET.
    The app's service principal must be granted:
      - CAN USE on the SQL Warehouse
      - USE CATALOG / USE SCHEMA / CREATE / MODIFY / SELECT on catalog+schema
    """
    from urllib.parse import quote
    from databricks.sdk.core import Config, oauth_service_principal

    host      = os.environ["DATABRICKS_SERVER_HOSTNAME"]
    http_path = os.environ["DATABRICKS_HTTP_PATH"]
    catalog   = os.environ.get("DATABRICKS_CATALOG", "adani_ael_ailabs_catalog_dev")
    schema    = os.environ.get("DATABRICKS_SCHEMA",  "document-generator")

    def credentials_provider():
        cfg = Config(
            host          = f"https://{host}",
            client_id     = os.environ["DATABRICKS_CLIENT_ID"],
            client_secret = os.environ["DATABRICKS_CLIENT_SECRET"],
        )
        return oauth_service_principal(cfg)

    # databricks://:@host  → empty user:pass tells the dialect to use the
    # credentials_provider passed via connect_args instead of a PAT.
    url = (
        f"databricks://:@{host}"
        f"?http_path={quote(http_path, safe='/')}"
        f"&catalog={quote(catalog)}"
        f"&schema={quote(schema)}"
    )
    return create_engine(
        url,
        connect_args={"credentials_provider": credentials_provider},
        echo=False,
        pool_pre_ping=True,
    )


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = _make_engine()
        Base.metadata.create_all(_engine)   # idempotent — creates missing tables only
        _migrate_sqlite_columns(_engine)    # add new nullable columns to existing tables
    return _engine


def _migrate_sqlite_columns(engine) -> None:
    """
    SQLite-safe column migration.
    SQLAlchemy's create_all() creates missing *tables* but never adds columns to
    existing ones.  This function fills that gap by inspecting each table with
    PRAGMA table_info and issuing ALTER TABLE … ADD COLUMN for any column that
    is present in the ORM model but absent from the live table.

    Only runs on SQLite (dev).  On PostgreSQL / Cloud SQL, use proper Alembic
    migrations — create_all() won't be the engine path in production.
    """
    if not engine.url.drivername.startswith("sqlite"):
        return

    # Map: table_name → {column_name → SQLAlchemy column object}
    table_columns: dict[str, dict] = {}
    for mapper in Base.registry.mappers:
        tbl = mapper.local_table
        table_columns[tbl.name] = {c.name: c for c in tbl.columns}

    # Identifier allow-list. Table/column names come from ORM metadata (trusted),
    # but they are interpolated into DDL below — validate them against a strict
    # SQL-identifier pattern so no unexpected name can ever reach the statement
    # (defence-in-depth against SQL injection / CWE-89).
    import re as _re
    _IDENT = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    with engine.connect() as conn:
        for table_name, columns in table_columns.items():
            if not _IDENT.match(table_name):
                continue
            rows = conn.execute(
                __import__("sqlalchemy").text(f"PRAGMA table_info({table_name})")
            ).fetchall()
            existing = {r[1] for r in rows}  # column names already in the live table

            for col_name, col_obj in columns.items():
                if col_name in existing or not _IDENT.match(col_name):
                    continue
                # Build a minimal DDL type string SQLite understands
                col_type = col_obj.type.compile(dialect=engine.dialect)
                nullable  = "" if col_obj.nullable else " NOT NULL"
                default   = ""
                if col_obj.default is not None and col_obj.default.is_scalar:
                    default = f" DEFAULT {col_obj.default.arg!r}"
                sql = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}{default}{nullable}"
                conn.execute(__import__("sqlalchemy").text(sql))
                conn.commit()
                import logging as _logging
                _logging.getLogger(__name__).info(
                    "[db] Auto-migrated: %s.%s (%s)", table_name, col_name, col_type
                )


def _is_sqlite_locked(exc: BaseException) -> bool:
    """True for a transient SQLite 'database is locked' / 'busy' error."""
    from sqlalchemy.exc import OperationalError
    if not isinstance(exc, OperationalError):
        return False
    msg = str(getattr(exc, "orig", exc)).lower()
    return "database is locked" in msg or "database is busy" in msg


@contextmanager
def get_session():
    """
    Context-manager that yields a SQLAlchemy Session.

    Usage:
        with get_session() as s:
            s.add(obj)
            s.commit()

    Guarantees:
      - Always closes the session on exit (returns connection to pool).
      - Rolls back automatically on any unhandled exception, so DB locks
        are never left hanging — critical for a shared DB in production.
      - Callers must still call s.commit() explicitly; nothing is auto-committed.

    NOTE on locking: this does NOT retry. Wrap write functions that can contend
    under SQLite (wave-parallel generation, concurrent job creation) with the
    @retry_on_locked decorator, which re-runs the whole function on a transient
    'database is locked' error.
    """
    session = Session(get_engine())
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def retry_on_locked(retries: int = 6, base_delay: float = 0.15):
    """
    Decorator: re-run a write function from the top when its transaction hits a
    transient SQLite 'database is locked' error (busy_timeout exhausted under
    heavy concurrent writes). Exponential backoff + jitter.

    Only safe on functions whose body is a self-contained DB transaction with
    no external side effects before the commit (they re-run wholesale). On
    non-SQLite backends the lock class never occurs, so this is a no-op.

        @retry_on_locked()
        def start_job(...):
            with get_session() as s: ...
    """
    import functools
    import logging as _lg
    import random
    import time as _time

    _log = _lg.getLogger(__name__)

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            attempt = 0
            while True:
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    if _is_sqlite_locked(exc) and attempt < retries:
                        delay = base_delay * (2 ** attempt) + random.SystemRandom().uniform(0, base_delay)
                        _log.warning("[db] %s locked — retry %d/%d in %.2fs",
                                     fn.__name__, attempt + 1, retries, delay)
                        _time.sleep(delay)
                        attempt += 1
                        continue
                    raise
        return wrapper
    return decorator


# ─────────────────────────────────────────────────────────────────────────────
# ORM Models
# ─────────────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class GenerationJob(Base):
    """One per user-triggered document generation request."""
    __tablename__ = "generation_jobs"

    job_id             = Column(String(36),  primary_key=True)
    document_id        = Column(String(36),  nullable=False, index=True)
    # Owning project — enables MULTIPLE documents (BRD, NFA, NIT, …) per project.
    # Latest job per (project_id, document_type) is that document's current state.
    # Project.job_id remains as a legacy "most recent job" alias.
    project_id         = Column(String(36),  nullable=True, index=True)
    status             = Column(String(20),  nullable=False, default="pending")
    # pending | in_progress | completed | failed
    document_type      = Column(String(100), nullable=False)
    output_format      = Column(String(20),  nullable=False, default="Word (.docx)")
    template_id        = Column(String(36),  nullable=True)
    language           = Column(String(50),  nullable=False, default="English")
    # User inputs snapshot (JSON) — stored so re-generation works without the original request
    user_inputs_json   = Column(Text,        nullable=True)
    error              = Column(Text,        nullable=True)
    total_sections     = Column(Integer,     default=0)
    completed_sections = Column(Integer,     default=0)
    # Review lifecycle of the generated document (independent of generation status):
    # draft | under_review | revision_requested | approved | rejected
    review_status      = Column(String(30),  nullable=True, default="draft")
    created_at         = Column(DateTime,    default=datetime.utcnow)
    completed_at       = Column(DateTime,    nullable=True)

    sections = relationship(
        "Section", back_populates="job",
        cascade="all, delete-orphan",
        order_by="Section.order_index",
    )

    def to_dict(self, include_sections: bool = True) -> dict:
        d = {
            "job_id":             self.job_id,
            "document_id":        self.document_id,
            "project_id":         self.project_id,
            "status":             self.status,
            "document_type":      self.document_type,
            "output_format":      self.output_format,
            "template_id":        self.template_id,
            "language":           self.language,
            "error":              self.error,
            "total_sections":     self.total_sections,
            "completed_sections": self.completed_sections,
            "review_status":      self.review_status or "draft",
            "created_at":         self.created_at.isoformat() if self.created_at else None,
            "completed_at":       self.completed_at.isoformat() if self.completed_at else None,
        }
        if include_sections:
            d["sections"] = [s.to_dict() for s in self.sections]
        return d


class Section(Base):
    """One row per section within a generation job."""
    __tablename__ = "sections"

    section_id      = Column(String(36),  primary_key=True)
    job_id          = Column(String(36),  ForeignKey("generation_jobs.job_id"), nullable=False, index=True)
    section_key     = Column(String(100), nullable=False)   # "executive_summary"
    section_title   = Column(String(200), nullable=False)   # "Executive Summary"
    order_index     = Column(Integer,     nullable=False)
    status          = Column(String(20),  nullable=False, default="pending")
    # pending | generating | completed | failed
    current_version = Column(Integer,     default=0)        # latest version_number
    error           = Column(Text,        nullable=True)
    # Cache for preview: MD5 of {section_id}:{current_version}
    # Invalidated only when current_version changes; avoids 50ms MD5 recalc per preview request
    version_hash    = Column(String(16),  nullable=True)
    created_at      = Column(DateTime,    default=datetime.utcnow)
    updated_at      = Column(DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow)

    job      = relationship("GenerationJob", back_populates="sections")
    versions = relationship(
        "SectionVersion", back_populates="section",
        cascade="all, delete-orphan",
        order_by="SectionVersion.version_number",
    )
    comments = relationship(
        "SectionComment", back_populates="section",
        cascade="all, delete-orphan",
        order_by="SectionComment.created_at",
    )

    def latest_version(self) -> Optional["SectionVersion"]:
        if not self.versions:
            return None
        return max(self.versions, key=lambda v: v.version_number)

    def to_dict(self, include_versions: bool = True, include_comments: bool = True) -> dict:
        d = {
            "section_id":      self.section_id,
            "job_id":          self.job_id,
            "section_key":     self.section_key,
            "section_title":   self.section_title,
            "order_index":     self.order_index,
            "status":          self.status,
            "current_version": self.current_version,
            "error":           self.error,
            "created_at":      self.created_at.isoformat() if self.created_at else None,
            "updated_at":      self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_versions:
            d["versions"] = [v.to_dict() for v in self.versions]
        if include_comments:
            d["comments"] = [c.to_dict() for c in self.comments]
        return d


class SectionVersion(Base):
    """Full Markdown content for one version of a section."""
    __tablename__ = "section_versions"

    version_id          = Column(String(36),  primary_key=True)
    section_id          = Column(String(36),  ForeignKey("sections.section_id"), nullable=False, index=True)
    version_number      = Column(Integer,     nullable=False)   # 1, 2, 3 …
    content             = Column(Text,        nullable=False)   # Markdown
    word_count          = Column(Integer,     default=0)
    generation_prompt   = Column(Text,        nullable=True)    # full prompt sent to LLM
    generation_model    = Column(String(100), nullable=True)    # e.g. "gemini-2.5-flash"
    is_accepted         = Column(Boolean,     default=False)    # user explicitly approved
    trigger_comment_id  = Column(String(36),  nullable=True)    # comment that triggered regen
    # Who/what created this version: ai_generation | ai_regeneration | manual_edit | review_comment
    trigger_type        = Column(String(50),  nullable=True,    default="ai_generation")
    # Email of the person who made a manual edit (attribution for reviewer-edit highlighting)
    edited_by           = Column(String(255), nullable=True)
    created_at          = Column(DateTime,    default=datetime.utcnow)

    section = relationship("Section", back_populates="versions")

    def to_dict(self) -> dict:
        return {
            "version_id":         self.version_id,
            "section_id":         self.section_id,
            "version_number":     self.version_number,
            "content":            self.content,
            "word_count":         self.word_count,
            "generation_model":   self.generation_model,
            "is_accepted":        self.is_accepted,
            "trigger_type":       self.trigger_type,
            "trigger_comment_id": self.trigger_comment_id,
            "edited_by":          self.edited_by,
            "created_at":         self.created_at.isoformat() if self.created_at else None,
        }


class DocumentSnapshot(Base):
    """
    Point-in-time checkpoint of all accepted/current section versions.

    Created when the user clicks "Save Version" or when the review agent
    completes a review pass.  Can be restored to roll back the document
    to a previous state.

    section_refs stores a JSON array:
      [{"section_id": "…", "version_id": "…", "version_number": N, "section_title": "…"}, …]
    """
    __tablename__ = "document_snapshots"

    snapshot_id   = Column(String(36),  primary_key=True)
    job_id        = Column(String(36),  ForeignKey("generation_jobs.job_id"), nullable=False, index=True)
    created_at    = Column(DateTime,    default=datetime.utcnow)
    label         = Column(String(200), nullable=True)
    # manual | review_agent | auto | manual_html (a whole-document edited-HTML save)
    trigger_type  = Column(String(50),  nullable=False, default="manual")
    section_refs  = Column(Text,        nullable=False, default="[]")
    # Who created this version (display name from X-User-Name). For manual_html
    # edits this is the person who edited the preview.
    author        = Column(String(255), nullable=True)
    # Raw edited HTML for a manual_html snapshot — stored verbatim so restoring a
    # version loads exactly what the user saved. NULL for section-ref snapshots.
    html_content  = Column(Text,        nullable=True)

    def get_section_refs(self) -> list:
        try:
            return json.loads(self.section_refs or "[]")
        except Exception:
            return []

    def to_dict(self, include_html: bool = False) -> dict:
        d = {
            "snapshot_id":  self.snapshot_id,
            "job_id":       self.job_id,
            "created_at":   self.created_at.isoformat() if self.created_at else None,
            "label":        self.label,
            "trigger_type": self.trigger_type,
            "author":       self.author,
            "has_html":     bool(self.html_content),
            "section_refs": self.get_section_refs(),
        }
        # html_content can be large — only include it on explicit single-get.
        if include_html:
            d["html_content"] = self.html_content
        return d


class SectionComment(Base):
    """User feedback / edit request on a specific section version."""
    __tablename__ = "section_comments"

    comment_id    = Column(String(36),  primary_key=True)
    section_id    = Column(String(36),  ForeignKey("sections.section_id"), nullable=False, index=True)
    version_number = Column(Integer,   nullable=False)    # which version this targets
    comment_text  = Column(Text,        nullable=False)
    comment_type  = Column(String(30),  default="edit_request")
    # edit_request | approval | rejection | note
    status        = Column(String(20),  default="pending")
    # pending | addressed | dismissed
    created_at    = Column(DateTime,    default=datetime.utcnow)

    section = relationship("Section", back_populates="comments")

    def to_dict(self) -> dict:
        return {
            "comment_id":     self.comment_id,
            "section_id":     self.section_id,
            "version_number": self.version_number,
            "comment_text":   self.comment_text,
            "comment_type":   self.comment_type,
            "status":         self.status,
            "created_at":     self.created_at.isoformat() if self.created_at else None,
        }


class Template(Base):
    """Reusable prompt templates — system-shipped + user-created."""
    __tablename__ = "templates"

    template_id       = Column(String(36),   primary_key=True)
    name              = Column(String(200),  nullable=False)
    document_type     = Column(String(100),  nullable=False, index=True)
    description       = Column(Text,         nullable=True)
    sections_config   = Column(Text,         nullable=False)   # JSON array of section defs
    system_instructions = Column(Text,       nullable=True)
    is_system         = Column(Boolean,      default=False)    # shipped vs user-created
    created_at        = Column(DateTime,     default=datetime.utcnow)
    updated_at        = Column(DateTime,     default=datetime.utcnow, onupdate=datetime.utcnow)

    def sections_list(self) -> list[dict]:
        return json.loads(self.sections_config or "[]")

    def to_dict(self) -> dict:
        return {
            "template_id":        self.template_id,
            "name":               self.name,
            "document_type":      self.document_type,
            "description":        self.description,
            "sections":           self.sections_list(),
            "system_instructions": self.system_instructions,
            "is_system":          self.is_system,
            "created_at":         self.created_at.isoformat() if self.created_at else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Project — stores Create New Project wizard data (Steps 1 & 2)
# ─────────────────────────────────────────────────────────────────────────────

class Project(Base):
    """
    One row per saved project.
    Ingested fields = everything the user fills in via the Create Project form.
    Stored in DB — frontend fetches via GET /api/projects/{id}/data, never
    reads back from the POST response body.
    """
    __tablename__ = "projects"

    # ── Identity ──────────────────────────────────────────────────────────────
    project_id    = Column(String(36),  primary_key=True)
    project_code  = Column(String(50),  nullable=True,  index=True)
    project_name  = Column(String(300), nullable=True,  index=True)
    business_unit = Column(String(100), nullable=True)

    # ── Core content ──────────────────────────────────────────────────────────
    business_priority    = Column(String(50),  nullable=True)
    problem_statement    = Column(Text,        nullable=True)
    project_objective    = Column(Text,        nullable=True)
    as_is_processes      = Column(Text,        nullable=True)
    proposed_solution    = Column(Text,        nullable=True)
    technical_landscape  = Column(Text,        nullable=True)

    # ── Optional fields ───────────────────────────────────────────────────────
    constraints           = Column(Text,       nullable=True)
    risks                 = Column(Text,       nullable=True)
    estimated_cost_crores = Column(String(50), nullable=True)   # stored as string e.g. "12.5"

    # ── Figma create-form fields (5-step wizard, msg#293/#313) ────────────────
    # Step 2 — Project Summary
    pain_points            = Column(Text,       nullable=True)
    opportunities          = Column(Text,       nullable=True)
    business_justification = Column(Text,       nullable=True)
    deadline               = Column(String(20), nullable=True)   # ISO: YYYY-MM-DD
    # Step 3 — Project Details
    integration_requirement = Column(Text,      nullable=True)
    assumptions             = Column(Text,      nullable=True)
    # Step 4 — Optional Information
    approval_matrix            = Column(Text,   nullable=True)
    future_roadmap             = Column(Text,   nullable=True)
    scalability_considerations = Column(Text,   nullable=True)
    innovation_objectives      = Column(Text,   nullable=True)
    sustainability_esg         = Column(Text,   nullable=True)
    # Step 1 — Core Details
    project_type               = Column(String(20), nullable=True)   # internal | external

    # ── Structured fields (serialised as JSON strings) ────────────────────────
    stakeholders_json  = Column(Text, nullable=True)   # [{"name":"...", "designation":"..."}]
    start_date         = Column(String(20), nullable=True)   # ISO: YYYY-MM-DD
    end_date           = Column(String(20), nullable=True)

    # ── Generation settings ───────────────────────────────────────────────────
    document_type            = Column(String(100), nullable=True, default="BRD")
    output_format            = Column(String(50),  nullable=True, default="Word (.docx)")
    additional_instructions  = Column(Text,        nullable=True)

    # ── Source documents (list of parsed document IDs) ────────────────────────
    document_ids_json = Column(Text, nullable=True)   # ["uuid1", "uuid2"]
    template_id       = Column(String(36), nullable=True)

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    status     = Column(String(30), nullable=False, default="draft", index=True)
    # draft | ready | generating | completed
    job_id     = Column(String(36), nullable=True)   # linked GenerationJob.job_id
    created_at = Column(DateTime,   default=datetime.utcnow,  index=True)
    updated_at = Column(DateTime,   default=datetime.utcnow,  onupdate=datetime.utcnow)

    # ── Relationship to AI-derived fields ─────────────────────────────────────
    derived = relationship(
        "DerivedData",
        back_populates="project",
        uselist=False,
        cascade="all, delete-orphan",
    )

    # ── Helpers ───────────────────────────────────────────────────────────────
    @property
    def stakeholders(self) -> list:
        try:
            return json.loads(self.stakeholders_json or "[]")
        except Exception:
            return []

    @property
    def document_ids(self) -> list:
        try:
            return json.loads(self.document_ids_json or "[]")
        except Exception:
            return []

    def to_ingested_dict(self) -> dict:
        """All ingested (user-entered) fields — used by GET /api/projects/{id}/data."""
        return {
            "project_id":             self.project_id,
            "project_code":           self.project_code           or "",
            "project_name":           self.project_name           or "",
            "business_unit":          self.business_unit          or "",
            "business_priority":      self.business_priority      or "",
            "problem_statement":      self.problem_statement      or "",
            "project_objective":      self.project_objective      or "",
            "stakeholders":           self.stakeholders,
            "start_date":             self.start_date             or "",
            "end_date":               self.end_date               or "",
            "as_is_processes":        self.as_is_processes        or "",
            "proposed_solution":      self.proposed_solution      or "",
            "constraints":            self.constraints            or "",
            "risks":                  self.risks                  or "",
            "technical_landscape":    self.technical_landscape    or "",
            "estimated_cost_crores":  self.estimated_cost_crores  or "",
            "pain_points":            self.pain_points            or "",
            "opportunities":          self.opportunities          or "",
            "business_justification": self.business_justification or "",
            "deadline":               self.deadline               or "",
            "integration_requirement": self.integration_requirement or "",
            "assumptions":            self.assumptions            or "",
            "approval_matrix":        self.approval_matrix        or "",
            "future_roadmap":         self.future_roadmap         or "",
            "scalability_considerations": self.scalability_considerations or "",
            "innovation_objectives":  self.innovation_objectives  or "",
            "sustainability_esg":     self.sustainability_esg     or "",
            "project_type":           self.project_type           or "",
            "document_type":          self.document_type          or "BRD",
            "output_format":          self.output_format          or "Word (.docx)",
            "additional_instructions": self.additional_instructions or "",
            "document_ids":           self.document_ids,
            "template_id":            self.template_id            or "",
        }

    def to_summary_dict(self) -> dict:
        """Lightweight summary for list views (dashboard project table)."""
        return {
            "project_id":    self.project_id,
            "project_name":  self.project_name  or "",
            "project_code":  self.project_code  or "",
            "business_unit": self.business_unit or "",
            "document_type": self.document_type or "BRD",
            "status":        self.status,
            "job_id":        self.job_id,
            "created_at":    self.created_at.isoformat() if self.created_at else None,
            "updated_at":    self.updated_at.isoformat() if self.updated_at else None,
        }

    def to_full_dict(self) -> dict:
        """Full project dict including lifecycle meta (used by GET /api/projects/{id})."""
        d = self.to_ingested_dict()
        d.update({
            "status":     self.status,
            "job_id":     self.job_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        })
        return d


# ─────────────────────────────────────────────────────────────────────────────
# DerivedData — AI-generated extended fields (one-to-one with Project)
# ─────────────────────────────────────────────────────────────────────────────

class DerivedData(Base):
    """
    AI-generated project fields populated by POST /api/projects/{id}/derive-fields.
    Also manually editable by the user via PUT /api/projects/{id}/data/derived.
    One row per project (upsert on generation run).
    """
    __tablename__ = "derived_data"

    project_id                  = Column(String(36), ForeignKey("projects.project_id"), primary_key=True)
    current_challenges          = Column(Text, nullable=True)
    to_be_process               = Column(Text, nullable=True)
    success_criteria            = Column(Text, nullable=True)
    business_requirements       = Column(Text, nullable=True)
    functional_requirements     = Column(Text, nullable=True)
    non_functional_requirements = Column(Text, nullable=True)
    industry_benchmarks         = Column(Text, nullable=True)
    workflow                    = Column(Text, nullable=True)
    analytics_requirements      = Column(Text, nullable=True)
    systems_involved            = Column(Text, nullable=True)
    data_sources                = Column(Text, nullable=True)
    constraints_dependencies    = Column(Text, nullable=True)
    generated_at                = Column(DateTime, nullable=True)   # set when AI populates
    updated_at                  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", back_populates="derived")

    def to_dict(self) -> dict:
        return {
            "project_id":                  self.project_id,
            "current_challenges":          self.current_challenges          or "",
            "to_be_process":               self.to_be_process               or "",
            "success_criteria":            self.success_criteria            or "",
            "business_requirements":       self.business_requirements       or "",
            "functional_requirements":     self.functional_requirements     or "",
            "non_functional_requirements": self.non_functional_requirements or "",
            "industry_benchmarks":         self.industry_benchmarks         or "",
            "workflow":                    self.workflow                    or "",
            "analytics_requirements":      self.analytics_requirements      or "",
            "systems_involved":            self.systems_involved            or "",
            "data_sources":                self.data_sources                or "",
            "constraints_dependencies":    self.constraints_dependencies    or "",
            "generated_at":  self.generated_at.isoformat() if self.generated_at else None,
            "updated_at":    self.updated_at.isoformat()   if self.updated_at   else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# ChatSession — persists conversation state for the Document Chat Studio
# ─────────────────────────────────────────────────────────────────────────────

class ChatSession(Base):
    """
    One row per chat conversation in the Document Chat Studio.
    Tracks which project + job the conversation is about, and stores
    the full message history as a JSON array.

    Phases:
      context    → user is setting up, generation not started
      generating → generation job is running (polling active)
      review     → generation complete, user reviewing / modifying sections
    """
    __tablename__ = "chat_sessions"

    session_id    = Column(String(36),  primary_key=True)
    project_id    = Column(String(36),  nullable=True, index=True)
    job_id        = Column(String(36),  nullable=True)
    document_type = Column(String(100), nullable=True)
    phase         = Column(String(20),  nullable=False, default="context")
    messages_json = Column(Text,        nullable=False, default="[]")
    pending_json  = Column(Text,        nullable=True)   # pending op awaiting user confirmation
    created_at    = Column(DateTime,    default=datetime.utcnow)
    updated_at    = Column(DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow)

    def get_messages(self) -> list:
        try:
            return json.loads(self.messages_json or "[]")
        except Exception:
            return []

    def add_message(self, role: str, content: str, data: dict = None) -> None:
        msgs = self.get_messages()
        msg  = {"role": role, "content": content, "ts": datetime.utcnow().isoformat()}
        if data:
            msg["data"] = data
        msgs.append(msg)
        self.messages_json = json.dumps(msgs)
        self.updated_at    = datetime.utcnow()

    def set_pending(self, data: dict) -> None:
        self.pending_json = json.dumps(data)

    def get_pending(self):
        if not self.pending_json:
            return None
        try:
            return json.loads(self.pending_json)
        except Exception:
            return None

    def clear_pending(self) -> None:
        self.pending_json = None

    def to_dict(self) -> dict:
        return {
            "session_id":    self.session_id,
            "project_id":    self.project_id,
            "job_id":        self.job_id,
            "document_type": self.document_type,
            "phase":         self.phase,
            "messages":      self.get_messages(),
            "created_at":    self.created_at.isoformat() if self.created_at else None,
            "updated_at":    self.updated_at.isoformat() if self.updated_at else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# REVIEW MODULE — users, personas, review requests / assignments / comments
# (implements the Figma "Review" flow: share for review, reviewer statuses,
#  threaded comments, AI persona reviews, AI summaries for the author)
# ─────────────────────────────────────────────────────────────────────────────

class User(Base):
    """
    Application user (feeds the Admin Panel + reviewer identity).
    Auth is Entra ID SSO on the frontend; the backend receives identity via
    X-User-Email / X-User-Name headers until full token validation is wired.
    """
    __tablename__ = "users"

    user_id    = Column(String(36),  primary_key=True)
    email      = Column(String(255), nullable=False, unique=True, index=True)
    name       = Column(String(200), nullable=False)
    role       = Column(String(50),  nullable=False, default="Contributor")
    # Admin | Project Manager | Contributor | Viewer
    is_active  = Column(Boolean,     default=True)
    created_at = Column(DateTime,    default=datetime.utcnow)

    def to_dict(self) -> dict:
        initials = "".join(w[0].upper() for w in (self.name or "?").split()[:2])
        return {
            "user_id":  self.user_id,
            "email":    self.email,
            "name":     self.name,
            "role":     self.role,
            "avatar":   initials,
            "is_active": bool(self.is_active),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Persona(Base):
    """
    AI reviewer profile — guides ai_persona_review() and summarize_for_author().
    System personas are seeded; users can add their own (owner_email set).
    """
    __tablename__ = "personas"

    persona_id  = Column(String(36),  primary_key=True)
    name        = Column(String(120), nullable=False)
    description = Column(Text,        nullable=True)
    is_system   = Column(Boolean,     default=False)
    owner_email = Column(String(255), nullable=True, index=True)  # null = global
    created_at  = Column(DateTime,    default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "persona_id":  self.persona_id,
            "name":        self.name,
            "description": self.description or "",
            "is_system":   bool(self.is_system),
            "owner_email": self.owner_email,
        }


class ReviewRequest(Base):
    """One 'share for review' action on a generated document (job)."""
    __tablename__ = "review_requests"

    review_id     = Column(String(36),  primary_key=True)
    job_id        = Column(String(36),  ForeignKey("generation_jobs.job_id"), nullable=False, index=True)
    project_id    = Column(String(36),  nullable=True, index=True)
    document_type = Column(String(100), nullable=True)
    requested_by_email = Column(String(255), nullable=False, index=True)
    requested_by_name  = Column(String(200), nullable=True)
    message       = Column(Text,        nullable=True)   # optional note to reviewers
    status        = Column(String(30),  nullable=False, default="open")
    # open | completed | cancelled
    created_at    = Column(DateTime,    default=datetime.utcnow)
    updated_at    = Column(DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow)

    assignments = relationship(
        "ReviewAssignment", back_populates="review",
        cascade="all, delete-orphan",
    )
    comments = relationship(
        "ReviewComment", back_populates="review",
        cascade="all, delete-orphan",
        order_by="ReviewComment.created_at",
    )

    def to_dict(self, include_children: bool = True) -> dict:
        d = {
            "review_id":     self.review_id,
            "job_id":        self.job_id,
            "project_id":    self.project_id,
            "document_type": self.document_type,
            "requested_by":  {"email": self.requested_by_email, "name": self.requested_by_name},
            "message":       self.message,
            "status":        self.status,
            "created_at":    self.created_at.isoformat() if self.created_at else None,
        }
        if include_children:
            d["reviewers"]      = [a.to_dict() for a in self.assignments]
            d["comments_count"] = len(self.comments)
        return d


class ReviewAssignment(Base):
    """One reviewer on a review request, with their per-reviewer status."""
    __tablename__ = "review_assignments"

    assignment_id  = Column(String(36),  primary_key=True)
    review_id      = Column(String(36),  ForeignKey("review_requests.review_id"), nullable=False, index=True)
    reviewer_email = Column(String(255), nullable=False, index=True)
    reviewer_name  = Column(String(200), nullable=True)
    reviewer_role  = Column(String(100), nullable=True)   # e.g. "Technical Lead"
    status         = Column(String(30),  nullable=False, default="shared")
    # shared | reviewing | accepted | rejected | revision_requested
    notified_at        = Column(DateTime, default=datetime.utcnow)
    last_renotified_at = Column(DateTime, nullable=True)
    responded_at       = Column(DateTime, nullable=True)

    review = relationship("ReviewRequest", back_populates="assignments")

    def to_dict(self) -> dict:
        return {
            "assignment_id": self.assignment_id,
            "review_id":     self.review_id,
            "email":         self.reviewer_email,
            "name":          self.reviewer_name,
            "role":          self.reviewer_role,
            "status":        self.status,
            "notified_at":   self.notified_at.isoformat() if self.notified_at else None,
            "last_renotified_at": self.last_renotified_at.isoformat() if self.last_renotified_at else None,
            "responded_at":  self.responded_at.isoformat() if self.responded_at else None,
        }


class ReviewComment(Base):
    """
    A comment inside a review — human or AI-generated (kept), optionally
    anchored to a section and threaded via parent_id.
    """
    __tablename__ = "review_comments"

    comment_id   = Column(String(36),  primary_key=True)
    review_id    = Column(String(36),  ForeignKey("review_requests.review_id"), nullable=False, index=True)
    section_id   = Column(String(36),  nullable=True, index=True)   # anchor (Section.section_id)
    section_title = Column(String(200), nullable=True)              # denormalised for display
    parent_id    = Column(String(36),  nullable=True, index=True)   # reply thread
    author_email = Column(String(255), nullable=False, index=True)
    author_name  = Column(String(200), nullable=True)
    source       = Column(String(10),  nullable=False, default="user")   # user | ai
    persona      = Column(String(120), nullable=True)   # persona used when source=ai
    text         = Column(Text,        nullable=False)
    status       = Column(String(20),  nullable=False, default="open")   # open | resolved
    applied_section_comment_id = Column(String(36), nullable=True)
    # set when the author applies this comment to a section via the generation flow
    created_at   = Column(DateTime,    default=datetime.utcnow)
    updated_at   = Column(DateTime,    default=datetime.utcnow, onupdate=datetime.utcnow)

    review = relationship("ReviewRequest", back_populates="comments")

    def to_dict(self) -> dict:
        return {
            "comment_id":   self.comment_id,
            "review_id":    self.review_id,
            "section_id":   self.section_id,
            "section_title": self.section_title,
            "parent_id":    self.parent_id,
            "author":       {"email": self.author_email, "name": self.author_name},
            "source":       self.source,
            "persona":      self.persona,
            "text":         self.text,
            "status":       self.status,
            "applied_section_comment_id": self.applied_section_comment_id,
            "created_at":   self.created_at.isoformat() if self.created_at else None,
            "updated_at":   self.updated_at.isoformat() if self.updated_at else None,
        }


class ReviewSummary(Base):
    """Cached AI persona-wise summary of reviewer feedback (author's carousel)."""
    __tablename__ = "review_summaries"

    summary_id   = Column(String(36),  primary_key=True)
    review_id    = Column(String(36),  ForeignKey("review_requests.review_id"), nullable=False, index=True)
    persona      = Column(String(120), nullable=False)
    summary_text = Column(Text,        nullable=False)
    model        = Column(String(100), nullable=True)
    # Fingerprint of the comment set the summary was generated from — lets
    # summarize_for_author() reuse the cache when nothing changed (staleness check).
    comments_fingerprint = Column(String(64), nullable=True)
    created_at   = Column(DateTime,    default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "summary_id": self.summary_id,
            "review_id":  self.review_id,
            "persona":    self.persona,
            "summary":    self.summary_text,
            "model":      self.model,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Notification(Base):
    """
    In-app notification (no email infra — bell icon in the UI).
    Keyed by recipient email; emitted by the review flow:
      review_shared | review_renotified | review_responded |
      comment_added | comments_kept | comment_applied
    """
    __tablename__ = "notifications"

    notification_id = Column(String(36),  primary_key=True)
    recipient_email = Column(String(255), nullable=False, index=True)
    actor_email     = Column(String(255), nullable=True)
    actor_name      = Column(String(200), nullable=True)
    type            = Column(String(40),  nullable=False)
    title           = Column(String(255), nullable=False)
    body            = Column(Text,        nullable=True)
    review_id       = Column(String(36),  nullable=True, index=True)
    job_id          = Column(String(36),  nullable=True)
    project_id      = Column(String(36),  nullable=True)
    is_read         = Column(Boolean,     default=False)
    created_at      = Column(DateTime,    default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "notification_id": self.notification_id,
            "type":       self.type,
            "title":      self.title,
            "body":       self.body or "",
            "actor":      {"email": self.actor_email, "name": self.actor_name},
            "review_id":  self.review_id,
            "job_id":     self.job_id,
            "project_id": self.project_id,
            "is_read":    bool(self.is_read),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# Default AI reviewer personas (from the Figma design) — seeded on first use.
DEFAULT_PERSONAS: list[dict] = [
    {"name": "Project Manager",    "description": "Focus on project execution, timelines, milestones, and deliverables"},
    {"name": "Technical Reviewer", "description": "Focus on technical accuracy, architecture, and feasibility"},
    {"name": "Business Analyst",   "description": "Focus on business requirements, process clarity, and ROI"},
    {"name": "Compliance Officer", "description": "Focus on regulatory compliance, policy adherence, and risk"},
    {"name": "Financial Auditor",  "description": "Focus on budget, cost estimates, and financial soundness"},
]
