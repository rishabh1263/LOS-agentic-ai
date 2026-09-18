"""
The public shape of POST /api/v1/los/process.

One place assembles the client-facing envelope, so what a client sees is
decided here rather than by whichever producer happened to fill a dict.

WHAT IS DELIBERATELY NOT EXPOSED: OCR tokens, per-candidate scores, the KYC
pair-comparison matrix, MCP envelopes, raw model output. Those are how the
answer was reached, not the answer, and a caller that starts parsing them
turns an internal detail into a contract nobody meant to sign.

WHAT IS EXPOSED IS DERIVED, NOT INVENTED. `conflicts` comes from the KYC
checks that actually failed -- KYC already computes name, date-of-birth,
address and PAN agreement across documents, and a conflict is simply one of
those saying no. `decision` is the deterministic status roll-up that already
governed the response. Neither is a new engine.
"""

from __future__ import annotations

import re
from typing import Any

# Severity of a failed KYC check, by the check it came from. A mismatch on
# identity is a different matter from one on address, and a reviewer sorting
# a queue needs to see which is which.
_CONFLICT_SEVERITY = {
    "PAN": "HIGH",
    "NAME": "HIGH",
    "DATE_OF_BIRTH": "HIGH",
    "DOB": "HIGH",
    "INCOME": "MEDIUM",
    "ADDRESS": "MEDIUM",
}

_TERMINAL_STATUSES = ("FAILED", "REJECTED", "REVIEW", "PARTIAL", "SUCCESS")

#: No document in the application cleared verification, so there is no
#: verified evidence for a decision to rest on.
NO_VERIFIED_DOCUMENTS = "NO_VERIFIED_DOCUMENTS"

#: The uploaded document was not the type the caller said it would be.
#: Distinct from an unreadable document: the caller can fix this one by
#: sending the right file, so it gets its own next action.
DOCUMENT_TYPE_MISMATCH = "DOCUMENT_TYPE_MISMATCH"



def verification_status(document: dict[str, Any]) -> str:
    """
    One document's verification verdict, whichever producer wrote it.

    Two producers, two spellings: the Document Agent writes
    verification.status, a specialist writes verification.decision and
    repeats it on its own payload. Resolved HERE and nowhere else, because
    reading only `status` once made every specialist document report
    SKIPPED -- and the extraction gate keys on this value, so it silently
    withheld their fields too.
    """
    verification = document.get("verification") or {}
    specialist = document.get("specialist") or {}

    return str(
        verification.get("status")
        or verification.get("decision")
        or specialist.get("decision")
        or "SKIPPED"
    )


def any_document_verified(documents: list[dict[str, Any]]) -> bool:
    """Did anything actually clear verification?"""
    return any(
        verification_status(document).upper() == "PASS" for document in documents
    )


def conflicts_from_kyc(kyc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """
    The disagreements KYC found between documents.

    Derived from the per-check results KYC already produces. A check that
    FAILED is a conflict; one that was SKIPPED is not -- absent evidence is
    not disagreement, and reporting it as one would fill the queue with
    documents nobody actually needs to look at.
    """
    if not kyc:
        return []

    conflicts: list[dict[str, Any]] = []

    for check in kyc.get("checks") or []:
        status = str(check.get("status") or "").upper()
        if status not in {"FAIL", "REVIEW"}:
            continue

        name = str(check.get("check") or check.get("name") or "").upper()
        conflicts.append(
            {
                "type": f"{name}_MISMATCH" if name else "MISMATCH",
                "severity": _CONFLICT_SEVERITY.get(name, "MEDIUM"),
                "status": status,
                "reason_codes": list(check.get("reason_codes") or []),
                "sources": list(check.get("source_ids") or []),
            }
        )

    return conflicts



#: Order of severity for the cross-document roll-up.
_CHECK_RANK = {"PASS": 0, "SKIPPED": 0, "REVIEW": 1, "FAIL": 2}


def cross_document_from(kyc: dict[str, Any] | None) -> dict[str, Any]:
    """
    Agreement BETWEEN documents, as one object.

    Replaces the older `conflicts` list, which reported only disagreements
    and so could not distinguish "every field agreed" from "nothing was
    comparable" -- both arrived as []. A reviewer needs that difference:
    the first is evidence, the second is the absence of it.

    `checks` is the five named checks KYC already performs, not the
    pair-comparison matrix behind them. That matrix stays internal; it grows
    with the square of the document count and says nothing a reviewer acts on.

    THE ROLL-UP IS CAPPED BY POLICY, THE CHECKS ARE NOT. A check that failed
    still reports FAIL with its reason codes, the documents involved and what
    each of them said. Whether that FAIL is allowed to carry the overall
    status down to FAIL -- rather than REVIEW -- is a business decision, and
    it is read from kyc_policies.yaml rather than made here. Documents
    disagreeing is a reason to involve a human, not a finding of fraud.
    """
    checks: list[dict[str, Any]] = []
    worst = "SKIPPED"
    compared = False

    for check in (kyc or {}).get("checks") or []:
        status = str(check.get("status") or "SKIPPED").upper()

        entry: dict[str, Any] = {
            "check": check.get("check"),
            "status": status,
            "reason_codes": list(check.get("reason_codes") or []),
        }

        sources = list(check.get("source_ids") or [])
        if sources:
            entry["sources"] = sources

        # Attached only where something disagreed. On a passing check the
        # values are already in documents[].extraction and repeating them
        # would double the size of a clean response for no reader.
        if status in {"FAIL", "REVIEW"}:
            values = {
                str(source): str(value)
                for source, value in (check.get("values") or {}).items()
            }
            if values:
                entry["details"] = values

        checks.append(entry)

        if status == "SKIPPED":
            continue

        compared = True

        # A non-blocking check contributes at most REVIEW, whatever it found.
        effective = status
        if status == "FAIL" and not check.get("blocking", False):
            effective = "REVIEW"

        if _CHECK_RANK.get(effective, 1) > _CHECK_RANK.get(worst, 0):
            worst = effective

    # Nothing comparable is not agreement. Reported as SKIPPED rather than
    # PASS, for the same reason a decision cannot pass on nothing verified.
    status = worst if compared else "SKIPPED"
    if compared and worst == "SKIPPED":
        status = "PASS"

    return {"status": status, "checks": checks}


def decision_from(
    status: str,
    kyc: dict[str, Any] | None,
    conflicts: list[dict[str, Any]],
    documents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    The deterministic outcome of this application, as computed.

    This is NOT a credit decision and not a policy engine: it restates the
    worst-wins roll-up the flow already produced, with the reasons that drove
    it, under a name a client can read. Nothing here weighs affordability or
    approves anything.

    One rule is enforced rather than restated: a PASS has to mean something
    was actually verified. With verification switched off every document
    reports SKIPPED and KYC has no sources, and both of those rank as
    harmless -- so the roll-up read SUCCESS and the decision read PASS for an
    application in which nothing had been checked. SKIPPED is not a pass
    anywhere else in this service and it is not one here.
    """
    reason_codes: list[str] = []

    if kyc:
        reason_codes.extend(str(code) for code in kyc.get("reason_codes") or [])

    for conflict in conflicts:
        reason_codes.extend(conflict.get("reason_codes") or [])

    mapped = {
        "SUCCESS": "PASS",
        "PARTIAL": "REVIEW",
        "REVIEW": "REVIEW",
        "REJECTED": "REJECT",
        "FAILED": "REJECT",
    }.get(str(status).upper(), "REVIEW")

    # A HIGH-severity disagreement between documents is never a pass, however
    # cleanly each document read on its own.
    if mapped == "PASS" and any(c["severity"] == "HIGH" for c in conflicts):
        mapped = "REVIEW"

    # Nothing verified is not a pass. Checked against the documents rather
    # than against the status, because the status is exactly what goes wrong
    # here: SKIPPED ranks alongside SUCCESS, so an application of entirely
    # unverified documents rolled up clean.
    if documents is not None and not any_document_verified(documents):
        reason_codes.append(NO_VERIFIED_DOCUMENTS)
        if mapped == "PASS":
            mapped = "REVIEW"

    return {
        "status": mapped,
        "reason_codes": sorted(set(reason_codes)),
        # Said plainly so nobody reads this as an underwriting outcome.
        "basis": "DETERMINISTIC_DOCUMENT_AND_KYC_RESULTS",
    }


def next_action_from(
    status: str,
    kyc: dict[str, Any] | None,
    conflicts: list[dict[str, Any]],
    documents: list[dict[str, Any]],
) -> str:
    """
    What should happen to this application next.

    A deterministic mapping over results that are already final -- not a
    recommendation engine, and nothing here weighs policy. It exists so a
    caller does not have to re-derive the obvious from four other fields.
    """
    # A wrong document and an unreadable one need different things from the
    # applicant: one has the document and sent the wrong file, the other
    # needs to re-photograph what they sent. Checked first, because a type
    # mismatch is also a verification FAIL.
    if any(
        DOCUMENT_TYPE_MISMATCH in (d.get("reason_codes") or [])
        for d in documents
    ):
        return "REQUEST_CORRECT_DOCUMENT"

    if any(d.get("verification") == "FAIL" for d in documents):
        return "REQUEST_VALID_DOCUMENT"

    if any(c.get("severity") == "HIGH" for c in conflicts):
        return "MANUAL_REVIEW"

    if any(d.get("verification") in {"REVIEW", "SKIPPED"} for d in documents):
        return "MANUAL_REVIEW"

    if str((kyc or {}).get("status", "")).upper() in {"FAIL", "REVIEW"}:
        return "MANUAL_REVIEW"

    if str(status).upper() in {"SUCCESS"}:
        return "CONTINUE"

    return "MANUAL_REVIEW"


def compact_document(document: dict[str, Any]) -> dict[str, Any]:
    """
    One document, in the mid-short public shape.

    Flat by design: a caller wants the type, whether it passed, and the
    fields. Verification is a WORD, not an object, because a status plus its
    reason codes is two keys where one usually does.

    Reason codes appear only when there are any, and extraction only when the
    gate released something -- an always-present empty object trains callers
    to ignore it.
    """
    verification = document.get("verification") or {}
    extraction = document.get("extraction") or {}
    specialist = document.get("specialist") or {}

    status = verification_status(document)

    compact: dict[str, Any] = {
        "source_id": document.get("source_id"),
        "type": (document.get("document") or {}).get("type") or "UNKNOWN",
        # The per-document roll-up. Kept alongside `verification` because the
        # two answer different questions: a document that could not be
        # processed at all reports FAILED here while its verification is
        # SKIPPED, which is otherwise indistinguishable from verification
        # having been switched off.
        "status": document.get("status"),
        "verification": status,
    }

    # NOT PRESENT, DELIBERATELY: `category` and the per-stage timings.
    #
    # `category` duplicated the capability name on a specialist document and
    # was derivable from `type` on every other, so it published an internal
    # routing decision and told a client nothing. The timings
    # (classification_ms, ocr_ms, specialist_ms, mcp_ms, orchestration_ms)
    # are diagnostics: they describe how this service is built, and a client
    # that started reading them would turn that into a contract. They remain
    # on the internal envelope and in the orchestration logs.

    # THE VERIFICATION GATE. Extraction is released only behind a PASS.
    #
    # Shared with the detailed shape rather than re-implemented, because that
    # is exactly how it got lost once: the compact path read the fields
    # straight off the document and a specialist REVIEW handed back its
    # extraction anyway. A gate written twice is a gate enforced once.
    released = _public_extraction(extraction, status)
    if released and released.get("fields"):
        compact["extraction"] = released["fields"]

    # A stage that was switched off says so on the document it would have
    # handled. Without this, "extraction is absent" is indistinguishable from
    # "extraction found nothing", and an operator who disabled it cannot tell
    # their own change took effect.
    from app.agents.los import config as los_config

    withheld: list[str] = []
    if status.upper() == "PASS" and not los_config.extraction_enabled():
        withheld.append("EXTRACTION_DISABLED")

    reasons = list(verification.get("reason_codes") or []) + withheld
    if reasons:
        compact["reason_codes"] = reasons

    # Present only for a specialist-handled document, and only what a caller
    # must know to READ the verdict: which capability judged it, what it
    # decided, why, and what it refused to claim. The capability's internal
    # checks, confidences and field scores stay internal.
    if specialist:
        # The BUSINESS verdict, not how it was reached. `capability`,
        # `input_mode` and `document_type` named the service, the calling
        # convention and the internal routing key -- three ways of saying
        # which code ran, which is this service's business and not the
        # client's.
        detail: dict[str, Any] = {
            "decision": specialist.get("decision"),
            "reason_codes": specialist.get("reason_codes") or [],
        }

        # `subtype` stays: a gift deed is not a sale deed, and a client
        # holding it as collateral needs to know which one it has.
        if specialist.get("subtype") is not None:
            detail["subtype"] = specialist["subtype"]

        if specialist.get("signature"):
            detail["signature"] = specialist["signature"]

        for claim in (
            "ownership_verified",
            "authenticity_verified",
            "business_existence_verified",
        ):
            if claim in specialist:
                detail[claim] = specialist[claim]

        compact["specialist"] = detail

    # The caller's asserted type, when classification was not allowed to run.
    # Carried as `hint` so it is never mistaken for a type this service found.
    hint = (document.get("document") or {}).get("hint")
    if hint:
        compact["hint"] = hint

    # What the caller said this file was, whenever they said anything. On a
    # DOCUMENT_TYPE_MISMATCH this is the half of the story `type` cannot
    # tell: the response has to show what was asked for AND what arrived.
    expected_type = document.get("expected_type")
    if expected_type:
        compact["expected_type"] = expected_type

    refs = _public_evidence_refs(document)
    if refs:
        compact["evidence_refs"] = refs

    errors = document.get("errors") or []
    if errors:
        compact["errors"] = errors

    return compact


def public_document(document: dict[str, Any]) -> dict[str, Any]:
    """
    One document, in the client-facing shape.

    Normalises the two internal shapes -- the Document Agent's and a
    specialist's -- onto one contract, so a caller does not have to know
    which path a file took to read its result.
    """
    verification = document.get("verification") or {}
    specialist = document.get("specialist") or {}
    extraction = document.get("extraction")

    # A specialist reports `decision`; the Document Agent reports `status`.
    # The client gets one word for one concept.
    status = verification_status(document)

    reason_codes = list(
        verification.get("reason_codes") or specialist.get("reason_codes") or []
    )

    public: dict[str, Any] = {
        "source_id": document.get("source_id"),
        # The per-document roll-up. Existing clients and tests read this, and
        # it answers "how did this one file end up" without them having to
        # combine verification and extraction themselves.
        "status": document.get("status"),
        "document": document.get("document") or {},
        "verification": {
            "status": status,
            "reason_codes": reason_codes,
        },
        "extraction": _public_extraction(extraction, status),
        "processing": _public_timings(document),
        "errors": document.get("errors") or [],
    }

    refs = _public_evidence_refs(document)
    if refs:
        public["evidence_refs"] = refs

    # A specialist's own verdict is worth surfacing, but compactly: the
    # verdict, why, and what it refused to claim -- not its internal checks.
    if specialist:
        public["specialist"] = {
            "capability": specialist.get("capability"),
            "decision": specialist.get("decision"),
            "reason_codes": specialist.get("reason_codes") or [],
        }

        # Which variant handled it: the proof slot, the signature input mode,
        # the deed subtype. A caller needs these to read the verdict -- a
        # signature REVIEW means something different standalone than embedded.
        for detail in ("document_type", "input_mode", "subtype", "template"):
            if specialist.get(detail) is not None:
                public["specialist"][detail] = specialist[detail]

        # The signature block answers the four questions a caller actually
        # asks, and is already compact.
        if specialist.get("signature"):
            public["specialist"]["signature"] = specialist["signature"]

        # What the capability refused to claim, carried through so nobody has
        # to infer it from absence.
        for claim in (
            "ownership_verified",
            "authenticity_verified",
            "business_existence_verified",
        ):
            if claim in specialist:
                public["specialist"][claim] = specialist[claim]

    return public



# A capability is handed a file staged inside the upload sandbox, so the
# locator it reports names that staged file -- "los_<uuid>.png#0,136,640,166".
# By the time the response is built the file has been unlinked, so the client
# receives a path that is useless to it, points at nothing, and leaks the
# sandbox naming scheme. The stable name for the same thing is the caller's
# own source_id, which is what they uploaded it as.
_INTERNAL_NAME_RE = re.compile(r"^los_[0-9a-f]{8,}\.[A-Za-z0-9]+$")

#: Anything that looks like a filesystem path rather than a logical reference.
_PATH_HINT_RE = re.compile(r"[\\/]|^[A-Za-z]:")


def _public_locator(locator: str, source_id: str) -> str:
    """
    One evidence locator, as a reference the caller can actually resolve.

    The fragment is what carries the meaning -- a page number, a pixel region
    -- and it is kept. Only the part that names a file is replaced, and only
    when that part is an internal artefact rather than something the caller
    would recognise.
    """
    text = str(locator or "").strip()
    if not text:
        return source_id

    head, separator, fragment = text.partition("#")

    # "page=1" with no file part: a bare fragment, so read it as one.
    if not separator and ("=" in head or "," in head):
        head, fragment = "", head

    if not head or _INTERNAL_NAME_RE.match(head) or _PATH_HINT_RE.search(head):
        head = source_id

    if not fragment:
        return head

    # A bare pixel region reads as noise without saying what it is.
    if re.fullmatch(r"\d+(?:,\d+){3}", fragment):
        fragment = f"region={fragment}"

    return f"{head}#{fragment}"


def _public_evidence_refs(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Evidence references, with no internal file naming in them."""
    source_id = str(document.get("source_id") or "")

    public: list[dict[str, Any]] = []
    for ref in document.get("evidence_refs") or []:
        if not isinstance(ref, dict):
            continue
        # Rebuilt rather than copied. `detail` carried the capability's own
        # wording -- "region via darkest_band" names an internal heuristic --
        # so a reference that is meant to be stable was shipping an
        # implementation note that could change with any tuning pass.
        entry_source = str(ref.get("source_id") or source_id)
        public.append({
            "source_id": entry_source,
            "locator": _public_locator(ref.get("locator", ""), entry_source),
        })

    return public


def _public_extraction(
    extraction: dict[str, Any] | None,
    verification_status: str,
) -> dict[str, Any] | None:
    """
    Extraction, released only behind the verification gate.

    THE ONE PLACE extraction reaches a client, so the gate is enforced here
    and nowhere else. It was briefly written twice -- once here and once in
    the compact shape -- and the second copy did not have the gate, so a
    specialist REVIEW handed its fields back anyway. One gate, one place.

    Two independent conditions, in order:

      verification must have PASSed. SKIPPED is not a pass: a check that did
      not run has established nothing, and reading it as permission would
      make switching verification off silently WIDEN what the API releases.

      extraction must be switched on. Off means the fields are withheld even
      from a document that passed, because the operator asked for that.
    """
    if str(verification_status).upper() != "PASS":
        return None

    from app.agents.los import config as los_config

    if not los_config.extraction_enabled():
        return None

    if not extraction:
        return None

    fields = extraction.get("fields") or {}
    return {
        "status": extraction.get("status") or ("SUCCESS" if fields else "PARTIAL"),
        "fields": fields,
    }


def _public_timings(document: dict[str, Any]) -> dict[str, float]:
    """
    Stage timings, flattened.

    The Document Agent nests some of these under `ocr` and `classification`
    objects that also carry confidences; only the durations belong in a
    client response.
    """
    processing = document.get("processing") or {}

    timings = {
        key: round(float(processing.get(key) or 0.0), 2)
        for key in (
            "classification_ms",
            "verification_ms",
            "extraction_ms",
            "total_ms",
        )
    }

    # Attribution for a specialist call: what MCP measured, and what routing
    # cost around it. Present only where a specialist ran, and never summed
    # with specialist_ms -- they are nested, not additive.
    for key in ("specialist_ms", "mcp_ms", "orchestration_ms"):
        if processing.get(key) is not None:
            timings[key] = round(float(processing[key]), 2)

    # A specialist reports one duration for the whole capability; without
    # this its timings would all read zero.
    specialist_ms = processing.get("specialist_ms")
    if specialist_ms and not timings["total_ms"]:
        timings["extraction_ms"] = round(float(specialist_ms), 2)
        timings["total_ms"] = round(float(specialist_ms), 2)

    # Always present, including as 0.0. A specialist that never OCR'd and a
    # document whose OCR time went unrecorded are different things, and a
    # missing key forces every caller to guard for it.
    timings["ocr_ms"] = round(float(processing.get("ocr_ms") or 0.0), 2)

    return timings


__all__ = [
    "conflicts_from_kyc", "cross_document_from", "decision_from",
    "public_document",
    "verification_status", "any_document_verified",
    "NO_VERIFIED_DOCUMENTS", "DOCUMENT_TYPE_MISMATCH",
]
