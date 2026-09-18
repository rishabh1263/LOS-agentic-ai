"""
The published shape of POST /api/v1/los/process.

DOCUMENTATION ONLY. These models are attached to the route through
`responses=`, not `response_model=`, so FastAPI renders them in Swagger and
does NOT use them to serialise, filter or re-validate anything. That is
deliberate: the response is assembled by app/agents/los/response.py, which
decides what leaves the building, and a second model quietly dropping a key
it had not been taught about is exactly the failure this cannot afford.

WHAT IS NOT HERE IS THE POINT. Stage timings, capability names, routing
categories and the KYC pair-comparison matrix are all real, all useful, and
all internal: they describe how this service is built rather than what it
concluded, and a client that started reading them would turn an
implementation detail into a contract. They remain on the internal envelope
and in the logs.

Keep these in step with app/agents/los/response.py. The E2E suite asserts the
documented field set against a real response, so drift fails a test rather
than quietly misleading a reader.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EvidenceRef(BaseModel):
    """
    Where a finding came from, as a reference the caller can resolve.

    Anchored on the caller's own source_id -- never an internal or temporary
    file path, and never the capability's wording for how it looked.
    """

    source_id: str = Field(..., examples=["sig.png"])
    locator: str = Field(
        ...,
        description="source_id, optionally with #page=N or #region=x0,y0,x1,y1.",
        examples=["sig.png#region=0,136,640,166", "deed.pdf#page=1"],
    )


class SignatureFinding(BaseModel):
    """The four questions a caller actually asks about a signature."""

    present: bool
    presence: str = Field(..., examples=["PRESENT", "ABSENT"])
    quality: str = Field(..., examples=["SUFFICIENT", "POOR"])
    comparison: str = Field(..., examples=["MATCH", "NOT_COMPARABLE"])
    match_score: float | None = None
    synthetic_risk: str = Field(..., examples=["LOW"])
    manipulation_risk: str = Field(..., examples=["LOW"])


class SpecialistVerdict(BaseModel):
    """
    A specialist capability's verdict on one document.

    The business answer only: what was decided, why, and what it refused to
    claim. Which service ran, how it was called and what it routed on are
    this service's business.
    """

    model_config = ConfigDict(extra="allow")

    decision: str = Field(..., examples=["PASS", "REVIEW", "FAIL", "SKIPPED"])
    reason_codes: list[str] = Field(default_factory=list)

    subtype: str | None = Field(
        None,
        description=(
            "The kind of instrument, where that changes what the document "
            "is worth. A gift deed is not a sale deed."
        ),
        examples=["SALE_DEED", "GIFT_DEED"],
    )
    signature: SignatureFinding | None = None

    ownership_verified: bool | None = Field(
        None, description="Never true: no capability here establishes ownership."
    )
    authenticity_verified: bool | None = Field(
        None, description="Never true: authenticity is not established here."
    )
    business_existence_verified: bool | None = None


#: What the identity verification gate is and is not claiming.
AUTHENTICITY_DESCRIPTION = (
    "Present on identity documents. Always NOT_ESTABLISHED: this service has "
    "no issuer API, government lookup or issuer signature to check against, "
    "so a PASS means the document is structurally valid and internally "
    "consistent -- never that it was issued by the authority it names."
)


class DocumentError(BaseModel):
    code: str = Field(..., examples=["INVALID_DOCUMENT"])
    message: str
    source_id: str | None = None


class ProcessedDocument(BaseModel):
    """One uploaded document, as the client sees it."""

    source_id: str = Field(..., description="The filename as uploaded.")
    type: str = Field(
        ...,
        description="The type this service identified. UNKNOWN when it could not.",
        examples=["PAN", "DRIVING_LICENCE", "ITR", "BANK_STATEMENT"],
    )
    status: str = Field(
        ...,
        description="This document's roll-up.",
        examples=["SUCCESS", "REVIEW", "SKIPPED", "FAILED"],
    )
    verification: str = Field(
        ...,
        description=(
            "The verification verdict. SKIPPED is NOT a pass -- a check that "
            "did not run has established nothing, and extraction is withheld."
        ),
        examples=["PASS", "REVIEW", "FAIL", "SKIPPED"],
    )

    extraction: dict[str, Any] | None = Field(
        None,
        description=(
            "The extracted fields, released ONLY behind a verification PASS "
            "and only while extraction is enabled. Absent otherwise. The keys "
            "depend on the document type."
        ),
        examples=[{
            "pan_number": "ABCPV1234K",
            "name": "SUNIL KUMAR VERMA",
            "date_of_birth": "1988-04-12",
        }],
    )
    reason_codes: list[str] | None = Field(
        None, description="Why this verdict. Absent when there is nothing to say."
    )
    authenticity: str | None = Field(
        None,
        description=AUTHENTICITY_DESCRIPTION,
        examples=["NOT_ESTABLISHED"],
    )
    advisories: list[str] | None = Field(
        None,
        description=(
            "Findings a reviewer should see that are NOT grounds to refuse "
            "the document, and did not change the verdict. Deliberately "
            "separate from `reason_codes`, where a code reads as a problem "
            "with the document."
        ),
        examples=[["PAN_NAME_INITIAL_MISMATCH"]],
    )
    specialist: SpecialistVerdict | None = Field(
        None, description="Present only for a specialist-handled document."
    )
    expected_type: str | None = Field(
        None,
        description=(
            "The type the caller asserted for this file through "
            "expected_types, echoed back. Present whenever they asserted "
            "one; absent for AUTO. Read it beside `type`: where the two "
            "differ the document carries DOCUMENT_TYPE_MISMATCH, failed "
            "verification and released no fields."
        ),
        examples=["PAN"],
    )
    hint: str | None = Field(
        None,
        description=(
            "The type the CALLER asserted, echoed when classification was "
            "switched off. Never a type this service found."
        ),
    )
    evidence_refs: list[EvidenceRef] | None = None
    errors: list[DocumentError] | None = None


class KycFieldSource(BaseModel):
    """One document's contribution to a KYC field comparison."""

    source_id: str = Field(..., description="The filename as uploaded.")
    document_type: str = Field(..., examples=["PAN", "DRIVING_LICENCE"])
    value: Any = Field(
        None,
        description=(
            "What this document carried, as extraction released it. Already "
            "published under documents[].extraction -- never OCR text, "
            "tokens or boxes."
        ),
    )
    normalized_value: Any = Field(
        None,
        description=(
            "What was actually compared. Published beside `value` so a "
            "near-miss can be read correctly: the difference is either in "
            "the documents or in the normalisation, and only showing both "
            "says which."
        ),
    )


class KycFieldResult(BaseModel):
    """
    One comparable identity field, resolved across the uploaded documents.

    The operational view. The pairwise comparison matrix behind it stays
    internal: it grows with the square of the document count and nobody acts
    on it.
    """

    field: str = Field(
        ...,
        examples=["NAME", "DATE_OF_BIRTH", "PAN_NUMBER", "FATHER_NAME",
                  "ADDRESS"],
    )
    status: str = Field(
        ...,
        description=(
            "PASS the values agree. PARTIAL they overlap materially but are "
            "not identical. REVIEW the comparison was inconclusive. FAIL they "
            "plainly differ. SKIPPED nothing was compared -- which is NOT a "
            "failure and never becomes one."
        ),
        examples=["PASS", "PARTIAL", "REVIEW", "FAIL", "SKIPPED"],
    )
    match_score: int = Field(
        0, ge=0, le=100,
        description=(
            "How closely the available values match EACH OTHER. 0 on a "
            "SKIPPED field means nothing was compared, not that nothing "
            "matched."
        ),
    )
    confidence: int = Field(
        0, ge=0, le=100,
        description=(
            "How far this result can be relied on -- a DIFFERENT question "
            "from match_score, and never a copy of it. Built from the "
            "comparison method used, how many documents were compared, how "
            "complete the field was across them, the extraction quality "
            "reported upstream, and how near the score sits to a decision "
            "threshold. "
            "Two poorly-read fields that agree exactly score 100 for match "
            "and well under it for confidence. Two cleanly-read fields that "
            "plainly differ score 0 for match and HIGH for confidence -- the "
            "disagreement is real and a reviewer should act on it."
        ),
    )
    reason_code: str = Field(
        "", examples=["EXACT_MATCH", "PARTIAL_ADDRESS_MATCH", "NAME_MISMATCH"]
    )
    reason: str = Field(
        "",
        description="Deterministic. Never model-generated.",
        examples=["Name matches across PAN and Driving Licence."],
    )
    sources: list[KycFieldSource] = Field(default_factory=list)


class KycSummary(BaseModel):
    """
    The KYC verdict.

    Establishes that the documents describe the same person consistently. It
    does NOT establish that any of them is genuine, and it is NOT a credit,
    risk or fraud score.
    """

    status: str = Field(..., examples=["PASS", "REVIEW", "FAIL", "SKIPPED"])
    reason_codes: list[str] = Field(default_factory=list)

    overall_score: int = Field(
        0, ge=0, le=100,
        description=(
            "Weighted mean of the field match scores, over the fields that "
            "were actually compared. Weights are in kyc_policies.yaml. A "
            "skipped field is excluded and the rest renormalised, so an "
            "absent field neither helps nor hurts. NOT a risk score."
        ),
    )
    overall_confidence: int = Field(
        0, ge=0, le=100,
        description="Weighted mean of the field confidences, same basis.",
    )
    fields: list[KycFieldResult] = Field(
        default_factory=list,
        description="One row per comparable identity field.",
    )


class CrossDocumentCheck(BaseModel):
    """One named agreement check across the uploaded documents."""

    check: str = Field(..., examples=["NAME", "DOB", "ADDRESS", "PAN", "INCOME"])
    status: str = Field(
        ...,
        description="SKIPPED means there was nothing to compare, not agreement.",
        examples=["PASS", "REVIEW", "FAIL", "SKIPPED"],
    )
    reason_codes: list[str] = Field(default_factory=list)
    sources: list[str] | None = Field(
        None,
        description=(
            "The documents this check compared -- or, where it failed, the "
            "ones that disagree."
        ),
        examples=[["pan.jpg", "itr.pdf"]],
    )
    details: dict[str, str] | None = Field(
        None,
        description=(
            "What each document said for the compared field, by source_id. "
            "Present only where the check did not pass, so a reviewer can "
            "see the disagreement without reopening every file. These are "
            "the same normalised values already published under "
            "documents[].extraction -- never OCR text or candidates."
        ),
        examples=[{"pan.jpg": "MUKESH KUMAR", "dl.jpg": "RISHABH AJIT SINGH"}],
    )


class CrossDocument(BaseModel):
    """
    Agreement between documents.

    An object rather than a list of disagreements, because a bare list could
    not distinguish "every field agreed" from "nothing was comparable" --
    both arrived empty, and they mean opposite things to a reviewer.
    """

    status: str = Field(
        ...,
        description=(
            "Worst of the checks that actually ran, capped by policy: a "
            "failed check that is not marked blocking in kyc_policies.yaml "
            "contributes at most REVIEW. Documents disagreeing routes the "
            "case to a human; it is not treated as fraud. SKIPPED if nothing "
            "was comparable."
        ),
        examples=["PASS", "REVIEW", "FAIL", "SKIPPED"],
    )
    checks: list[CrossDocumentCheck] = Field(default_factory=list)


class LosProcessResponse(BaseModel):
    """One applicant's documents, processed end to end."""

    request_id: str = Field(..., examples=["los_8604ee9d1f2b4c0a9e7d3f1a2b3c4d5e"])
    applicant_id: str | None = None
    case_id: str = Field(
        ...,
        description="Generated when none is supplied. One applicant may hold several.",
    )
    status: str = Field(
        ...,
        description="Worst-wins across every document and the KYC result.",
        examples=["SUCCESS", "PARTIAL", "REVIEW", "REJECTED", "FAILED"],
    )
    documents: list[ProcessedDocument]
    kyc: KycSummary
    cross_document: CrossDocument
    decision: str = Field(
        ...,
        description=(
            "The deterministic outcome. NOT a credit decision and not an "
            "underwriting verdict: it restates the roll-up under a name a "
            "client can read. PASS requires that something was actually "
            "verified."
        ),
        examples=["PASS", "REVIEW", "REJECT"],
    )
    next_action: str = Field(
        ..., examples=["CONTINUE", "MANUAL_REVIEW", "REQUEST_VALID_DOCUMENT"]
    )
    summary: str = Field(..., description="One sentence. Never decides anything.")
    summary_source: str = Field(
        ...,
        description=(
            "Who wrote the summary sentence. Published because a client "
            "showing it to a human needs to know whether a model produced "
            "it; every decision above was already final when it was written."
        ),
        examples=["deterministic", "llm"],
    )
    processing_ms: float = Field(
        ..., description="How long the call took. Stage timings stay internal."
    )
    errors: list[DocumentError] = Field(default_factory=list)


__all__ = [
    "LosProcessResponse", "ProcessedDocument", "SpecialistVerdict",
    "SignatureFinding", "EvidenceRef", "KycSummary", "KycFieldResult",
    "KycFieldSource", "CrossDocument",
    "CrossDocumentCheck", "DocumentError",
]
