"""
The entities the FOS stage works with.

DELIBERATELY SMALL. An applicant, an application and the documents attached to
it -- nothing else. Credit, risk, RCU and underwriting keep their own state
downstream, and putting placeholder columns here for them would invite someone
to fill those columns in from the wrong place.

Plain dataclasses rather than an ORM. The repository interface is what the
rest of the service depends on, so the storage technology stays swappable; a
model class carrying session state would nail it to one.

Every status value here is set from a DETERMINISTIC source: the document
pipeline's own verdict, or a transition the FOS explicitly asked for through
an authorised tool. Nothing in this module infers a status, and no language
model ever writes one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    """One clock, in UTC, so stored timestamps compare across machines."""
    return datetime.now(timezone.utc)


# ==========================================================================
# STATES
#
# Kept to the few the FOS stage actually distinguishes. A state nobody routes
# on is a state that drifts out of date silently.
# ==========================================================================

class ApplicationStatus(str, Enum):
    """Where an application sits in the FOS stage."""

    APPLICATION_CREATED = "APPLICATION_CREATED"
    DOCUMENT_COLLECTION = "DOCUMENT_COLLECTION"
    BASIC_DOCUMENT_VERIFICATION = "BASIC_DOCUMENT_VERIFICATION"
    READY_FOR_CPA = "READY_FOR_CPA"


class DocumentStatus(str, Enum):
    """
    What has happened to one document.

    MISSING is not stored -- it is the absence of a row, derived against the
    product's required list. Storing it would mean two places could disagree
    about whether a document exists.
    """

    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    VERIFIED = "VERIFIED"
    REVIEW = "REVIEW"
    REJECTED = "REJECTED"


#: Document statuses that satisfy a required-document slot.
SATISFYING_STATUSES = frozenset({DocumentStatus.VERIFIED})

#: Document statuses that need the FOS to do something.
ACTIONABLE_STATUSES = frozenset({DocumentStatus.REVIEW, DocumentStatus.REJECTED})


# ==========================================================================
# ENTITIES
# ==========================================================================

@dataclass
class Applicant:
    """A person applying. Basic FOS-captured information only."""

    applicant_id: str
    full_name: str | None = None
    mobile: str | None = None
    email: str | None = None
    date_of_birth: str | None = None
    address: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    #: Fields the readiness gate expects before a case leaves the FOS stage.
    #: Named here rather than in the gate so one edit changes both the
    #: completeness answer and the thing that explains it.
    REQUIRED_FIELDS = ("full_name", "mobile", "date_of_birth", "address")

    def missing_fields(self) -> list[str]:
        """Which required fields are still blank."""
        return [
            name for name in self.REQUIRED_FIELDS
            if not (getattr(self, name) or "").strip()
        ]

    def is_complete(self) -> bool:
        return not self.missing_fields()


@dataclass
class Application:
    """One loan application, belonging to one applicant."""

    case_id: str
    applicant_id: str
    status: ApplicationStatus = ApplicationStatus.APPLICATION_CREATED
    product: str | None = None
    loan_amount: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    REQUIRED_FIELDS = ("product",)

    def missing_fields(self) -> list[str]:
        return [
            name for name in self.REQUIRED_FIELDS
            if not (getattr(self, name) or "").strip()
        ]


@dataclass
class Document:
    """
    One document attached to a case.

    `verification_status` and `reason_codes` are COPIED from the document
    pipeline's verdict, never recomputed here. This store records what
    verification concluded; it does not participate in concluding it.
    """

    document_id: str
    case_id: str
    applicant_id: str
    document_type: str
    status: DocumentStatus = DocumentStatus.UPLOADED
    source_id: str | None = None
    verification_status: str | None = None
    reason_codes: list[str] = field(default_factory=list)
    extracted_fields: dict[str, Any] = field(default_factory=dict)
    uploaded_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def needs_attention(self) -> bool:
        return self.status in ACTIONABLE_STATUSES


#: Maps a document pipeline verdict onto a stored document status.
#:
#: The pipeline answers PASS / REVIEW / FAIL / SKIPPED about verification. A
#: SKIPPED check established nothing, so the document stays UPLOADED rather
#: than being recorded as though it had been looked at.
VERDICT_TO_STATUS = {
    "PASS": DocumentStatus.VERIFIED,
    "REVIEW": DocumentStatus.REVIEW,
    "FAIL": DocumentStatus.REJECTED,
    "SKIPPED": DocumentStatus.UPLOADED,
}


def status_for_verdict(verdict: str | None) -> DocumentStatus:
    """Translate a pipeline verification verdict into a stored status."""
    return VERDICT_TO_STATUS.get(
        str(verdict or "").strip().upper(), DocumentStatus.UPLOADED
    )


__all__ = [
    "ACTIONABLE_STATUSES",
    "Applicant",
    "Application",
    "ApplicationStatus",
    "Document",
    "DocumentStatus",
    "SATISFYING_STATUSES",
    "VERDICT_TO_STATUS",
    "status_for_verdict",
    "utcnow",
]
