"""
SQLite implementation of the repository contract.

CHOSEN BECAUSE IT ADDS NOTHING. sqlite3 is in the standard library, so the
store costs no new dependency, no server to run and no migration tool. It is
the right size for the FOS stage of one branch; it is not the right size for a
national deployment, which is exactly why every caller goes through
`Repository` and not through this file.

Concurrency: FastAPI serves requests from a thread pool, so connections are
per-thread (`check_same_thread=False` plus a lock would serialise every read).
WAL is enabled so readers do not block the writer.

Times are stored as ISO-8601 UTC strings. SQLite has no datetime type, and a
float epoch is unreadable when someone is looking at the file with a CLI at
two in the morning.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.store.models import (
    Applicant,
    Application,
    ApplicationStatus,
    Document,
    DocumentStatus,
    utcnow,
)
from app.store.repository import Repository, RepositoryError

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS applicants (
    applicant_id   TEXT PRIMARY KEY,
    full_name      TEXT,
    mobile         TEXT,
    email          TEXT,
    date_of_birth  TEXT,
    address        TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    case_id       TEXT PRIMARY KEY,
    applicant_id  TEXT NOT NULL,
    status        TEXT NOT NULL,
    product       TEXT,
    loan_amount   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (applicant_id) REFERENCES applicants (applicant_id)
);
CREATE INDEX IF NOT EXISTS idx_applications_applicant
    ON applications (applicant_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS documents (
    document_id         TEXT PRIMARY KEY,
    case_id             TEXT NOT NULL,
    applicant_id        TEXT NOT NULL,
    document_type       TEXT NOT NULL,
    status              TEXT NOT NULL,
    source_id           TEXT,
    verification_status TEXT,
    reason_codes        TEXT NOT NULL DEFAULT '[]',
    extracted_fields    TEXT NOT NULL DEFAULT '{}',
    uploaded_at         TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES applications (case_id)
);
CREATE INDEX IF NOT EXISTS idx_documents_case
    ON documents (case_id, uploaded_at);
"""


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime:
    if not value:
        return utcnow()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return utcnow()
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class SQLiteRepository(Repository):
    """The FOS store, in one file on disk."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialised = False

    # -- connection --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """One connection per thread, created on first use in that thread."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path), timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error as exc:
            raise RepositoryError(f"Could not open the case store: {exc}") from exc
        self._local.conn = conn
        return conn

    def initialise(self) -> None:
        with self._init_lock:
            if self._initialised:
                return
            try:
                conn = self._connect()
                conn.executescript(_SCHEMA)
                conn.commit()
            except sqlite3.Error as exc:
                raise RepositoryError(f"Could not create the schema: {exc}") from exc
            self._initialised = True
            logger.info("Case store ready at %s", self._path)

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

    def health(self) -> dict[str, Any]:
        try:
            self._connect().execute("SELECT 1").fetchone()
            return {"backend": "sqlite", "available": True, "path": str(self._path)}
        except Exception as exc:
            return {"backend": "sqlite", "available": False, "error": str(exc)[:200]}

    # -- applicants --------------------------------------------------------

    def get_applicant(self, applicant_id: str) -> Applicant | None:
        row = self._one("SELECT * FROM applicants WHERE applicant_id = ?",
                        (applicant_id,))
        return self._applicant(row) if row else None

    def save_applicant(self, applicant: Applicant) -> Applicant:
        applicant.updated_at = utcnow()
        self._write(
            """
            INSERT INTO applicants (applicant_id, full_name, mobile, email,
                                    date_of_birth, address, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(applicant_id) DO UPDATE SET
                full_name     = excluded.full_name,
                mobile        = excluded.mobile,
                email         = excluded.email,
                date_of_birth = excluded.date_of_birth,
                address       = excluded.address,
                updated_at    = excluded.updated_at
            """,
            (applicant.applicant_id, applicant.full_name, applicant.mobile,
             applicant.email, applicant.date_of_birth, applicant.address,
             _iso(applicant.created_at), _iso(applicant.updated_at)),
        )
        return applicant

    def list_applicants(self, limit: int = 50) -> list[Applicant]:
        rows = self._all(
            "SELECT * FROM applicants ORDER BY updated_at DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        )
        return [self._applicant(r) for r in rows]

    # -- applications ------------------------------------------------------

    def get_application(self, case_id: str) -> Application | None:
        row = self._one("SELECT * FROM applications WHERE case_id = ?", (case_id,))
        return self._application(row) if row else None

    def save_application(self, application: Application) -> Application:
        application.updated_at = utcnow()
        self._write(
            """
            INSERT INTO applications (case_id, applicant_id, status, product,
                                      loan_amount, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(case_id) DO UPDATE SET
                status      = excluded.status,
                product     = excluded.product,
                loan_amount = excluded.loan_amount,
                updated_at  = excluded.updated_at
            """,
            (application.case_id, application.applicant_id,
             application.status.value, application.product,
             application.loan_amount, _iso(application.created_at),
             _iso(application.updated_at)),
        )
        return application

    def list_applications(self, applicant_id: str) -> list[Application]:
        rows = self._all(
            "SELECT * FROM applications WHERE applicant_id = ? "
            "ORDER BY updated_at DESC",
            (applicant_id,),
        )
        return [self._application(r) for r in rows]

    # -- documents ---------------------------------------------------------

    def get_document(self, document_id: str) -> Document | None:
        row = self._one("SELECT * FROM documents WHERE document_id = ?",
                        (document_id,))
        return self._document(row) if row else None

    def save_document(self, document: Document) -> Document:
        document.updated_at = utcnow()
        self._write(
            """
            INSERT INTO documents (document_id, case_id, applicant_id,
                                   document_type, status, source_id,
                                   verification_status, reason_codes,
                                   extracted_fields, uploaded_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                document_type       = excluded.document_type,
                status              = excluded.status,
                source_id           = excluded.source_id,
                verification_status = excluded.verification_status,
                reason_codes        = excluded.reason_codes,
                extracted_fields    = excluded.extracted_fields,
                updated_at          = excluded.updated_at
            """,
            (document.document_id, document.case_id, document.applicant_id,
             document.document_type, document.status.value, document.source_id,
             document.verification_status, json.dumps(document.reason_codes),
             json.dumps(document.extracted_fields, default=str),
             _iso(document.uploaded_at), _iso(document.updated_at)),
        )
        return document

    def list_documents(self, case_id: str) -> list[Document]:
        rows = self._all(
            "SELECT * FROM documents WHERE case_id = ? ORDER BY uploaded_at",
            (case_id,),
        )
        return [self._document(r) for r in rows]

    # -- plumbing ----------------------------------------------------------

    def _one(self, sql: str, args: tuple) -> sqlite3.Row | None:
        self.initialise()
        try:
            return self._connect().execute(sql, args).fetchone()
        except sqlite3.Error as exc:
            raise RepositoryError(f"Case store read failed: {exc}") from exc

    def _all(self, sql: str, args: tuple) -> list[sqlite3.Row]:
        self.initialise()
        try:
            return list(self._connect().execute(sql, args).fetchall())
        except sqlite3.Error as exc:
            raise RepositoryError(f"Case store read failed: {exc}") from exc

    def _write(self, sql: str, args: tuple) -> None:
        self.initialise()
        conn = self._connect()
        try:
            conn.execute(sql, args)
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise RepositoryError(f"Case store write failed: {exc}") from exc

    # -- row -> model ------------------------------------------------------

    @staticmethod
    def _applicant(row: sqlite3.Row) -> Applicant:
        return Applicant(
            applicant_id=row["applicant_id"],
            full_name=row["full_name"],
            mobile=row["mobile"],
            email=row["email"],
            date_of_birth=row["date_of_birth"],
            address=row["address"],
            created_at=_parse(row["created_at"]),
            updated_at=_parse(row["updated_at"]),
        )

    @staticmethod
    def _application(row: sqlite3.Row) -> Application:
        try:
            status = ApplicationStatus(row["status"])
        except ValueError:
            status = ApplicationStatus.APPLICATION_CREATED
        return Application(
            case_id=row["case_id"],
            applicant_id=row["applicant_id"],
            status=status,
            product=row["product"],
            loan_amount=row["loan_amount"],
            created_at=_parse(row["created_at"]),
            updated_at=_parse(row["updated_at"]),
        )

    @staticmethod
    def _document(row: sqlite3.Row) -> Document:
        try:
            status = DocumentStatus(row["status"])
        except ValueError:
            status = DocumentStatus.UPLOADED

        def _load(raw: str, fallback):
            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                return fallback

        return Document(
            document_id=row["document_id"],
            case_id=row["case_id"],
            applicant_id=row["applicant_id"],
            document_type=row["document_type"],
            status=status,
            source_id=row["source_id"],
            verification_status=row["verification_status"],
            reason_codes=_load(row["reason_codes"], []),
            extracted_fields=_load(row["extracted_fields"], {}),
            uploaded_at=_parse(row["uploaded_at"]),
            updated_at=_parse(row["updated_at"]),
        )


__all__ = ["SQLiteRepository"]
