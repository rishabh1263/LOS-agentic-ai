"""
The end-to-end LOS flow.

    uploads
      -> Document Agent (classify, verify, extract only when allowed)
      -> Financial Agent for financial documents
      -> normalised outputs
      -> KYC cross-check
      -> one response

Nothing here re-reads a document. The Document Agent already routes financial
uploads to the Financial Agent and returns normalised values, and KYC consumes
those values directly, so a page is rasterised once, recognised once and
classified once for the whole application.

Documents are independent of each other, so they are processed concurrently.
That parallelism is safe because each one offloads to the document executor and
hands recognition to the single serialised OCR worker -- RapidOCR is never
called from two threads at once. What actually overlaps is PDF rasterisation,
pypdf parsing and the financial parsers, which is where the wall-clock goes on
a multi-document application.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from app.agents.document_agent.workflow import process_document
from app.agents.los import config as los_config
from app.agents.los import response
from app.agents.kyc.agent import check_is_blocking, run_kyc
from app.agents.kyc.schemas import CheckStatus, KycRequest
from app.agents.los.mapping import to_kyc_source
from app.agents.los.summary import build_summary_async

logger = logging.getLogger(__name__)

# Worst-wins ordering across both vocabularies. The document statuses and the
# KYC statuses are different words for the same three outcomes, so they are
# ranked on one scale and the overall verdict is simply the worst.
_SEVERITY = {
    "SUCCESS": 0, "PASS": 0, "SKIPPED": 0,
    "PARTIAL": 1, "REVIEW": 1,
    "REJECTED": 2, "FAIL": 2,
    "FAILED": 3,
}

_PUBLIC = ("SUCCESS", "PARTIAL", "REVIEW", "REJECTED", "FAILED")


# Evidence that is NOT an identity document and must not go to the Document
# Agent's OCR pipeline. Each maps to a registered orchestration capability.
#
# Routing happens here so a client uploading a sale deed or a shop photo to
# /api/v1/los/process gets it assessed in the same call as everything else.
# Making them separate endpoints would mean the case is only complete if the
# client remembers to make three more requests.
_SPECIALIST_AGENTS: dict[str, str] = {
    "SALE_DEED": "sale_deed",
    "BUSINESS_PROOF_1": "business_evidence",
    "BUSINESS_PROOF_2": "business_evidence",
    "BANK_SIGNATURE": "signature_verification",
    "PAN_SIGNATURE": "signature_verification",
    "DRIVING_LICENSE_SIGNATURE": "signature_verification",
    "PASSPORT_SIGNATURE": "signature_verification",
    # A signature uploaded on its own, with no surrounding document.
    "SIGNATURE": "signature_verification",
    "STANDALONE_SIGNATURE": "signature_verification",
}

# Callers say "SIGNATURE"; the capability wants the specific input type.
_SIGNATURE_ALIASES = {"SIGNATURE": "STANDALONE_SIGNATURE"}


def specialist_for(expected_type: str | None) -> str | None:
    """Which capability owns this evidence type, if any."""
    return _SPECIALIST_AGENTS.get(str(expected_type or "").strip().upper())


def _rank(status: str | None) -> int:
    return _SEVERITY.get(str(status or "").upper(), 1)


def _public_status(rank: int) -> str:
    for name in _PUBLIC:
        if _SEVERITY[name] == rank:
            return name
    return "REVIEW"



# ---------------------------------------------------------------------------
# OPERATIONS
#
# PROCESS is the application-level verb and the canonical public operation:
# "run this applicant's documents through the whole flow". VERIFY and EXTRACT
# are the DOCUMENT AGENT's modes -- they decide whether that agent releases
# extracted fields -- and they reached the public API because this endpoint
# passed its `operation` straight through to it.
#
# They are kept accepted, because callers already send them, but they are now
# translated at this seam rather than being the vocabulary a client has to
# learn. Nothing else about the flow keys on the value: classification,
# specialist routing, KYC, cross-document checks, the decision and the
# summary all run identically for every operation. The ONLY thing it governs
# is whether the Document Agent hands back fields -- and that is still behind
# the verification gate either way.
# ---------------------------------------------------------------------------

#: The canonical public operation.
PROCESS = "PROCESS"

#: Everything /api/v1/los/process accepts, in the order a client should
#: prefer them.
PUBLIC_OPERATIONS = ("PROCESS", "EXTRACT", "VERIFY")

#: Public operation -> the Document Agent mode it runs as.
_DOCUMENT_MODE = {
    "PROCESS": "EXTRACT",
    "EXTRACT": "EXTRACT",
    "VERIFY": "VERIFY",
}


def document_mode(operation: str | None) -> str:
    """
    The Document Agent mode for a public operation.

    Raises ValueError, naming every accepted value, for anything else. The
    endpoint turns that into a 422 -- unknown operations are rejected, not
    quietly treated as the default.
    """
    value = (operation or PROCESS).strip().upper()

    mode = _DOCUMENT_MODE.get(value)
    if mode is None:
        raise ValueError(
            "operation must be one of: " + ", ".join(PUBLIC_OPERATIONS) + "."
        )

    return mode


class UploadedDocument:
    """One file offered to the flow."""

    __slots__ = ("source_id", "filename", "content", "expected_type")

    def __init__(
        self,
        source_id: str,
        filename: str,
        content: bytes,
        expected_type: str | None = None,
    ) -> None:
        self.source_id = source_id
        self.filename = filename
        self.content = content
        self.expected_type = expected_type





def _is_type_mismatch(result: dict[str, Any]) -> bool:
    """Did this document fail only because it was not the type asked for?"""
    verification = result.get("verification") or {}
    return response.DOCUMENT_TYPE_MISMATCH in (
        verification.get("reason_codes") or []
    )


def _reported_values(check: Any) -> dict[str, str]:
    """
    What each document said for the field this check compared.

    Read off the evidence KYC already recorded -- these are normalised
    extracted values, the same ones already published under
    documents[].extraction. No OCR text, no candidate list, no scores.
    """
    values: dict[str, str] = {}

    for item in getattr(check, "evidence", None) or []:
        if item.source_id and item.value is not None:
            values[str(item.source_id)] = str(item.value)

    return values


def _disagreeing_sources(check: Any) -> list[str]:
    """
    Which uploads a failed KYC check is about.

    The response layer reports a conflict's `sources` so a reviewer knows
    WHICH two documents disagree; without them a six-document application
    reports NAME_MISMATCH against nothing in particular. The ids are read off
    the comparison KYC already performed -- the pairs it marked as not
    agreeing -- so nothing is inferred here.

    A check that failed before it could compare anything (an unreadable value,
    say) has no comparisons; it falls back to the sources that carried the
    field, which is what the check was about.
    """
    ordered: list[str] = []

    for comparison in getattr(check, "comparisons", None) or []:
        if comparison.agreed:
            continue
        for source_id in (comparison.left_source_id, comparison.right_source_id):
            if source_id and source_id not in ordered:
                ordered.append(source_id)

    if not ordered:
        for item in getattr(check, "evidence", None) or []:
            if item.source_id and item.source_id not in ordered:
                ordered.append(item.source_id)

    return ordered


def _classification_disabled(
    document: UploadedDocument,
    request_id: str,
) -> dict[str, Any]:
    """
    A document nobody was allowed to identify.

    The caller's expected_type is echoed under `hint` rather than as `type`,
    because it is what the caller ASSERTED, not what this service found. A
    hint promoted to a type would be a classification result nobody produced.
    """
    hint = str(document.expected_type or "").strip().upper() or None

    return {
        "request_id": f"{request_id}:{document.source_id}",
        "source_id": document.source_id,
        "status": "SKIPPED",
        "document": {
            "type": "UNKNOWN",
            "category": None,
            "supported": False,
            "hint": hint,
        },
        "extraction": None,
        "verification": {
            "status": "SKIPPED",
            "reason_codes": ["CLASSIFICATION_DISABLED"],
        },
        "evidence_refs": [],
        "processing": {},
        "errors": [
            {
                "code": "CLASSIFICATION_DISABLED",
                "message": (
                    "Classification is disabled by configuration; this "
                    "document was not identified, and no capability ran."
                ),
            }
        ],
    }


def _specialist_disabled(
    document: UploadedDocument,
    agent_id: str,
    request_id: str,
) -> dict[str, Any]:
    """A specialist that is switched off says so on the document it owns."""
    expected = str(document.expected_type or "").strip().upper()

    return {
        "request_id": f"{request_id}:{document.source_id}",
        "source_id": document.source_id,
        "status": "SKIPPED",
        "document": {
            "type": expected or "UNKNOWN",
            "category": agent_id,
            "supported": True,
        },
        "extraction": None,
        "verification": {
            "status": "SKIPPED",
            "reason_codes": ["CAPABILITY_DISABLED"],
        },
        "evidence_refs": [],
        "processing": {},
        "errors": [
            {
                "code": "CAPABILITY_DISABLED",
                "message": (
                    f"{agent_id} is disabled by configuration; this document "
                    "was not examined."
                ),
            }
        ],
    }


async def _run_specialist(
    document: UploadedDocument,
    agent_id: str,
    request_id: str,
) -> dict[str, Any]:
    """
    Run one upload through a specialist capability, via the orchestrator.

    Deliberately routed through run_agent rather than calling the service
    directly: the capability then gets the same config, retry, circuit
    breaker and bulkhead treatment as every other agent, and there is exactly
    one execution path to audit rather than one for orchestrated calls and a
    quieter one for the LOS flow.

    The bytes are written inside the upload sandbox because every specialist
    takes a path and every one of them refuses a path outside it.
    """
    from app.agents.document_verification.service import upload_root
    from app.orchestration.graph import run_agent

    source_request_id = f"{request_id}:{document.source_id}"
    expected = str(document.expected_type or "").strip().upper()

    root = upload_root()
    root.mkdir(parents=True, exist_ok=True)
    staged = root / f"los_{uuid.uuid4().hex}{Path(document.filename).suffix.lower()}"

    started = time.perf_counter()
    try:
        staged.write_bytes(document.content)

        payload: dict[str, Any] = {
            "file_path": str(staged),
            "source_id": document.source_id,
        }
        if agent_id == "business_evidence":
            payload["slot"] = expected
        elif agent_id == "signature_verification":
            payload["document_type"] = _SIGNATURE_ALIASES.get(expected, expected)

        state = await run_agent(
            agent_id=agent_id, payload=payload, request_id=source_request_id
        )

        result = state.get("result") or {}
        error = state.get("error")

        if error and not result:
            raise RuntimeError(str(error))

        elapsed = round((time.perf_counter() - started) * 1000, 2)

        return {
            "request_id": source_request_id,
            "source_id": document.source_id,
            # The specialist's own verdict becomes the document status, so the
            # application-level roll-up ranks it on the same scale as the rest.
            "status": result.get("decision") or "REVIEW",
            "document": {
                "type": expected,
                "category": agent_id,
                "supported": True,
            },
            "extraction": {"fields": result.get("fields") or {}},
            "verification": {
                "decision": result.get("decision"),
                "checks": result.get("checks") or [],
                "reason_codes": result.get("reason_codes") or [],
            },
            "evidence_refs": result.get("evidence_refs") or [],
            "specialist": result,
            # Three nested measurements of one call, not three costs:
            #
            #   specialist_ms    the whole thing, as the flow sees it
            #   mcp_ms           inside that, what MCP measured
            #   orchestration_ms specialist_ms minus mcp_ms -- routing,
            #                    config resolution, retry and breaker
            #
            # Reported separately so a slow request can be attributed, and
            # never added together.
            "processing": {
                "specialist_ms": elapsed,
                "mcp_ms": round(float(result.get("mcp_ms") or 0.0), 2),
                "orchestration_ms": round(
                    max(0.0, elapsed - float(result.get("mcp_ms") or 0.0)), 2
                ),
            },
            "errors": [],
        }

    except Exception as exc:
        logger.exception(
            "Specialist %s failed request_id=%s", agent_id, source_request_id
        )
        return {
            "request_id": source_request_id,
            "source_id": document.source_id,
            "status": "FAILED",
            "document": {
                "type": expected or "UNKNOWN",
                "category": agent_id,
                "supported": True,
            },
            "extraction": None,
            "verification": None,
            "evidence_refs": [],
            "processing": {
                "specialist_ms": round((time.perf_counter() - started) * 1000, 2)
            },
            "errors": [
                {
                    "code": "SPECIALIST_FAILED",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            ],
        }
    finally:
        staged.unlink(missing_ok=True)


async def _process_one(
    document: UploadedDocument,
    operation: str,
    request_id: str,
) -> dict[str, Any]:
    """Run one upload through the Document Agent, never raising."""
    # Classification switched off.
    #
    # Nothing downstream may proceed as though the type were known. A
    # caller's expected_type is a HINT, not a classification result: routing
    # a document to a specialist on an unverified hint would let the hint
    # decide which capability judged it, and then report a verdict as though
    # the type had been established. So the document is reported SKIPPED with
    # the hint echoed, and no capability runs.
    if not los_config.classification_enabled():
        return _classification_disabled(document, request_id)

    agent_id = specialist_for(document.expected_type)
    if agent_id:
        if not los_config.specialist_enabled(agent_id):
            # Reported, not silently fallen through. Sending a shop photograph
            # to the Document Agent because its specialist is switched off
            # would classify it as an unreadable ID card, which looks like a
            # bad document rather than a disabled capability.
            return _specialist_disabled(document, agent_id, request_id)
        return _apply_verification_flag(
            await _run_specialist(document, agent_id, request_id)
        )

    try:
        result = await process_document(
            file_bytes=document.content,
            filename=document.filename,
            operation=operation,
            requested_class=document.expected_type,
            request_id=f"{request_id}:{document.source_id}",
            include_detail=False,
        )
    except ValueError as exc:
        # Rejected input (bad type, empty, oversized). One bad file must not
        # fail the whole application.
        result = {
            "request_id": f"{request_id}:{document.source_id}",
            "status": "FAILED",
            "document": {"type": "UNKNOWN", "category": None, "supported": False},
            "extraction": None,
            "verification": None,
            "kyc": None,
            "summary": "",
            "processing": {},
            "errors": [{"code": "INVALID_DOCUMENT", "message": str(exc)}],
        }
    except Exception as exc:
        logger.exception(
            "Document %s failed in the LOS flow request_id=%s",
            document.source_id, request_id,
        )
        result = {
            "request_id": f"{request_id}:{document.source_id}",
            "status": "FAILED",
            "document": {"type": "UNKNOWN", "category": None, "supported": False},
            "extraction": None,
            "verification": None,
            "kyc": None,
            "summary": "",
            "processing": {},
            "errors": [{
                "code": "DOCUMENT_PROCESSING_FAILED",
                "message": f"{type(exc).__name__}: {exc}",
            }],
        }

    result["source_id"] = document.source_id

    # What the CALLER asserted this file was. Carried so the response can show
    # the assertion beside the type actually found -- a mismatch is unreadable
    # without both halves.
    expected = str(document.expected_type or "").strip().upper()
    if expected and expected not in {"AUTO", "ANY"}:
        result["expected_type"] = expected

    # The per-document summary and kyc slot are redundant inside an
    # application-level response that carries both at the top.
    result.pop("summary", None)
    result.pop("kyc", None)
    return _apply_verification_flag(result)


def _apply_verification_flag(result: dict[str, Any]) -> dict[str, Any]:
    """
    Honour LOS_VERIFICATION_ENABLED on a finished document result.

    The flag was published in the LOS configuration snapshot and enforced
    nowhere: an operator who switched verification off still got a PASS and
    the extracted fields behind it, which is precisely what the gate exists
    to prevent. VERIFICATION_ENABLED -- the Verification Agent's own switch --
    did work; this one silently did not.

    Withheld AFTER the pipeline rather than before it. The verdict is
    discarded, not the classification: a caller still learns what the
    document is, which is the one thing verification being off does not put
    in doubt. Doing it earlier would mean threading the flag through the
    Document Agent, duplicating a switch that already lives with the agent
    that owns the decision.
    """
    if los_config.verification_enabled():
        return result

    # A document that could not be processed at all stays FAILED. Only the
    # verification verdict is being withheld here; downgrading an unreadable
    # file to SKIPPED would drop it out of the application roll-up entirely.
    if _rank(result.get("status")) <= _rank("SKIPPED"):
        result["status"] = "SKIPPED"

    result["extraction"] = None
    result["verification"] = {
        "status": "SKIPPED",
        "reason_codes": ["VERIFICATION_DISABLED"],
    }
    # A specialist's verdict is a verification verdict. Left in place it
    # would be read as one through the `specialist.decision` fallback, and
    # the document would report PASS with verification switched off.
    specialist = result.get("specialist")
    if specialist:
        specialist = dict(specialist)
        specialist["decision"] = "SKIPPED"
        specialist["reason_codes"] = ["VERIFICATION_DISABLED"]
        result["specialist"] = specialist

    return result


def _aggregate_timings(documents: list[dict[str, Any]]) -> dict[str, float]:
    """
    Roll per-document timings up.

    Stage costs are SUMMED because they are real work done, while the total is
    measured on the wall clock by the caller: documents run concurrently, so
    adding their totals would report more time than actually elapsed.
    """
    totals = {
        "ocr_ms": 0.0, "classification_ms": 0.0,
        "verification_ms": 0.0, "extraction_ms": 0.0,
    }
    for document in documents:
        processing = document.get("processing") or {}
        for key in totals:
            try:
                totals[key] += float(processing.get(key) or 0.0)
            except (TypeError, ValueError):
                continue
    return {key: round(value, 2) for key, value in totals.items()}


async def process_application(
    uploads: Iterable[UploadedDocument],
    *,
    operation: str = PROCESS,
    applicant_id: str | None = None,
    case_id: str | None = None,
    request_id: str,
    use_llm_summary: bool | None = None,
) -> dict[str, Any]:
    """Run every document, cross-check them, and return one response."""

    started = time.perf_counter()
    uploads = list(uploads)

    # PROCESS is the public verb; the Document Agent is handed its own mode.
    operation = document_mode(operation)

    # One applicant, many cases.
    #
    # An applicant_id identifies a PERSON and is reused across every
    # application they ever make; a case_id identifies ONE of those
    # applications. Conflating them would merge a rejected application from
    # last year with a fresh one, and a name mismatch between the two would
    # be reported as a conflict inside the new case.
    #
    # Omitting case_id starts a new case. Supplying one continues it. This
    # call is stateless -- everything in the response is scoped to the case
    # named in it -- so "continuing" means the client keeps sending the same
    # id, and nothing from another case can reach this one.
    case_id = (case_id or "").strip() or f"case_{uuid.uuid4().hex}"

    # Independent work, run together.
    documents = await asyncio.gather(
        *(_process_one(upload, operation, request_id) for upload in uploads)
    )

    # ---------------------------------------------------------------
    # KYC. Consumes only what the agents already released: a document
    # whose fields were withheld by the verification gate contributes
    # nothing, which is the gate doing its job.
    # ---------------------------------------------------------------
    kyc_started = time.perf_counter()

    sources = []
    for result in documents:
        source = to_kyc_source(result, result.get("source_id", "unknown"))
        if source is not None:
            sources.append(source)

    errors: list[dict[str, Any]] = []

    if not los_config.kyc_enabled():
        kyc_payload = {
            "status": CheckStatus.SKIPPED.value,
            "reason_codes": ["KYC_DISABLED"],
            "checks": [],
        }
        kyc_rank = _rank(CheckStatus.SKIPPED.value)
    elif sources:
        kyc_result = run_kyc(
            KycRequest(applicant_id=applicant_id, documents=sources),
            request_id=request_id,
        )
        kyc_payload = {
            "status": kyc_result.status.value,
            "reason_codes": [code.value for code in kyc_result.reason_codes],
            # Per-check results, so a disagreement between documents can be
            # reported as a conflict rather than only as a status. The full
            # pair-comparison matrix stays internal.
            "checks": [
                {
                    "check": check.check.value,
                    "status": check.status.value,
                    "reason_codes": [code.value for code in check.reason_codes],
                    "source_ids": _disagreeing_sources(check),
                    # What each document actually said. Without it a reviewer
                    # is told two documents disagree and has to open both to
                    # find out how.
                    "values": _reported_values(check),
                    # Whether this check failing may drive the verdict down
                    # to FAIL, or is capped at REVIEW. Read from policy, not
                    # decided here.
                    "blocking": check_is_blocking(check.check.value),
                }
                for check in kyc_result.checks
            ],
        }
        kyc_rank = _rank(kyc_result.status.value)
    else:
        kyc_payload = {
            "status": CheckStatus.SKIPPED.value,
            "reason_codes": ["INSUFFICIENT_SOURCES"],
        }
        kyc_rank = 0
        errors.append({
            "code": "KYC_NOT_RUN",
            "message": (
                "No document released extracted fields, so there was nothing "
                "to cross-check. VERIFY never releases fields, and EXTRACT "
                "releases them only after verification passes."
            ),
        })

    kyc_ms = (time.perf_counter() - kyc_started) * 1000

    # ---------------------------------------------------------------
    # Overall verdict: the worst of every document and the KYC result.
    # ---------------------------------------------------------------
    # A DOCUMENT TYPE MISMATCH IS A WRONG UPLOAD, NOT A CREDIT REJECTION.
    #
    # The document itself fails -- verification FAIL, no extraction -- but
    # the application is not rejected for it: the applicant sent the wrong
    # file and can send the right one. Capped at REVIEW for the roll-up in
    # the same way a non-blocking KYC disagreement is, so the case reaches a
    # human with REQUEST_CORRECT_DOCUMENT rather than being turned down.
    def _document_rank(result: dict[str, Any]) -> int:
        rank = _rank(result.get("status"))
        if _is_type_mismatch(result):
            return min(rank, _rank("REVIEW"))
        return rank

    worst = max(
        [_document_rank(result) for result in documents] + [kyc_rank],
        default=1,
    )

    # NOTHING VERIFIED IS NOT A SUCCESS.
    #
    # SKIPPED ranks alongside SUCCESS, which is right for one skipped stage
    # inside a healthy application and wrong for an application where every
    # document was skipped: with verification switched off the documents all
    # report SKIPPED, KYC has no sources and also reports SKIPPED, and the
    # roll-up read SUCCESS for a case in which nothing had been checked.
    #
    # A FLOOR, not an override -- an application that actually failed still
    # reports FAILED rather than being softened to REVIEW.
    nothing_verified = documents and not response.any_document_verified(documents)

    if nothing_verified:
        worst = max(worst, _rank("REVIEW"))
        errors.append({
            "code": response.NO_VERIFIED_DOCUMENTS,
            "message": (
                "No document cleared verification, so no verified evidence "
                "supports this application. Extraction was withheld for "
                "every document and KYC had nothing to cross-check."
            ),
        })

    status = _public_status(worst)

    # PARTIAL and REVIEW share a severity, and the roll-up renders that
    # severity as PARTIAL -- the right word for an application where some of
    # it worked. Where nothing was verified, none of it did, so the word is
    # REVIEW. Only this case is renamed; every other PARTIAL keeps its name.
    if nothing_verified and status == "PARTIAL":
        status = "REVIEW"

    for result in documents:
        for error in result.get("errors") or []:
            errors.append({**error, "source_id": result.get("source_id")})

    processing = _aggregate_timings(documents)
    processing["kyc_ms"] = round(kyc_ms, 2)
    processing["total_ms"] = round((time.perf_counter() - started) * 1000, 2)

    envelope: dict[str, Any] = {
        "request_id": request_id,
        "applicant_id": applicant_id,
        "case_id": case_id,
        "status": status,
        "documents": documents,
        "kyc": kyc_payload,
        "summary": "",
        "processing": processing,
        "errors": errors,
    }

    # Awaited, not called inline: with the model switched on this is a network
    # call, and the flow is already on the event loop.
    #
    # The summary is written LAST, from results that are already final, and it
    # cannot change any of them. A model failure costs the sentence and
    # nothing else -- build_summary_async falls back to the deterministic
    # summary rather than propagating.
    if use_llm_summary is None:
        use_llm_summary = los_config.llm_summary_enabled()

    summary_started = time.perf_counter()
    envelope["summary"], envelope["summary_source"] = await build_summary_async(
        envelope, use_llm=use_llm_summary
    )
    summary_ms = round((time.perf_counter() - summary_started) * 1000, 2)

    # Attributed separately: `summary_ms` is what the stage cost, `llm_ms` is
    # how much of that was the model. They differ when the model was not
    # consulted at all, and that difference is the whole point of the
    # reachability probe.
    processing["summary_ms"] = summary_ms
    processing["llm_ms"] = (
        summary_ms if envelope.get("summary_source") == "llm" else 0.0
    )
    processing["total_ms"] = round((time.perf_counter() - started) * 1000, 2)

    # The stage breakdown, to the log rather than to the client.
    #
    # It is deliberately absent from the response -- ocr_ms and friends
    # describe how this service is built, and a client reading them would
    # turn an implementation detail into a contract. But it has to go
    # SOMEWHERE, or nobody tuning the service can see which stage cost what,
    # and the only remaining answer is "add a timer and redeploy".
    #
    # One line per request, correlatable by request_id, and carrying nothing
    # but numbers: no field values, no filenames, no applicant data.
    logger.info(
        "los timings request_id=%s documents=%d source=%s %s",
        request_id,
        len(documents),
        envelope.get("summary_source"),
        " ".join(f"{key}={value}" for key, value in sorted(processing.items())),
    )

    return _public_envelope(envelope)


def _public_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """
    Reshape the internal result into the client-facing contract.

    Kept as a final pass rather than threaded through every producer: the
    internal dicts stay convenient for the code that builds them, and exactly
    one function decides what leaves the building.
    """
    kyc = envelope.get("kyc") or {}
    conflicts = (
        response.conflicts_from_kyc(kyc)
        if los_config.conflict_detection_enabled()
        else []
    )
    documents = [
        response.compact_document(document)
        for document in envelope.get("documents") or []
    ]

    decision = response.decision_from(
        envelope.get("status") or "", kyc, conflicts,
        envelope.get("documents") or [],
    )
    processing = envelope.get("processing") or {}

    public: dict[str, Any] = {
        "request_id": envelope.get("request_id"),
        "applicant_id": envelope.get("applicant_id"),
        "case_id": envelope.get("case_id"),
        "status": envelope.get("status"),
        "documents": documents,
        "kyc": {
            "status": kyc.get("status", "SKIPPED"),
            "reason_codes": list(kyc.get("reason_codes") or []),
        },
        # Cross-document agreement, as one object. The `conflicts` list it
        # replaces reported only disagreements, so "everything agreed" and
        # "nothing was comparable" both arrived as [].
        #
        # `conflicts` is still computed above -- decision and next_action are
        # derived from it -- it is simply no longer a separate public key
        # saying the same thing a second time.
        "cross_document": (
            response.cross_document_from(kyc)
            if los_config.conflict_detection_enabled()
            else {"status": "SKIPPED", "checks": []}
        ),
        # A word, not an object. The reasons behind it are already carried by
        # kyc.reason_codes and the cross-document checks, and repeating them
        # here made the same codes appear three times in one response.
        "decision": decision["status"],
        "next_action": response.next_action_from(
            envelope.get("status") or "", kyc, conflicts, documents
        ),
        "summary": envelope.get("summary", ""),
        "summary_source": envelope.get("summary_source"),
        "processing_ms": round(float(processing.get("total_ms") or 0.0), 2),
        "errors": envelope.get("errors") or [],
    }

    # NO STAGE-TIMING OBJECT.
    #
    # ocr_ms, classification_ms, specialist_ms, mcp_ms, orchestration_ms,
    # summary_ms and llm_ms describe how this service is built. They stay on
    # the internal envelope and in the orchestration logs, where the people
    # tuning it can read them, rather than becoming a contract a client
    # starts depending on. `processing_ms` -- how long the call took -- is
    # the one number a client has a use for, and it is above.

    return public


__all__ = [
    "process_application", "UploadedDocument", "document_mode",
    "PROCESS", "PUBLIC_OPERATIONS",
]
