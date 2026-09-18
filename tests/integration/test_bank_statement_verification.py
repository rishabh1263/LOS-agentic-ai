"""
Bank statements, bank-independently.

THE PROBLEM THIS SUITE PINS. Statements from several banks were coming back
as REVIEW with an EMPTY reason list. Two separate defects:

  1. The financial verification path published `status` and `checks` and no
     reason codes at all -- the key did not exist on that path -- so every
     non-pass was unexplained.

  2. Completeness could only be established by a PRINTED closing balance or
     a PRINTED "end of statement" marker. Both are things a particular bank
     chooses to include. A correctly-parsed Standard Chartered statement was
     sent to REVIEW for printing its pages differently from the banks the
     marker list was built from -- a parser limitation reported as a doubt
     about the customer's document.

THE PURPOSE HERE IS NOT TO MAKE EVERY SAMPLE PASS. It is that a parseable
statement passes, an inconclusive one reviews WITH A REASON, and a
conclusively broken one fails -- whichever bank issued it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.agents.los.flow import PROCESS, UploadedDocument, process_application

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "real_batch"

#: Different banks, different layouts. The point of the list is the variety.
STATEMENTS = [
    "bank_canara.pdf", "bank_hdfc_new.pdf", "bank_amit.pdf",
    "bank_kotak.pdf", "bank_std_chartered.pdf", "bank_generic.pdf",
    "sbi_new.pdf",
]

#: A genuine scan with no reconstructable grid. Kept separate: it is the one
#: sample this build legitimately cannot read.
SCANNED = "bank_sbi_scanned.pdf"


def run(name: str, expected: str | None = "BANK_STATEMENT") -> dict:
    """One statement through the FOS-mode pipeline."""
    path = SAMPLES / name
    if not path.exists():
        pytest.skip(f"sample not available: {name}")

    result = asyncio.run(process_application(
        [UploadedDocument(source_id=name, filename=name,
                          content=path.read_bytes(), expected_type=expected)],
        operation=PROCESS, applicant_id="APP-B", case_id="CASE-B",
        request_id="bank-test",
        cross_document_checks=False, summarise=False, financial_analysis=False,
    ))
    return (result.get("documents") or [{}])[0], result


# ==========================================================================
# BANK INDEPENDENCE
# ==========================================================================

@pytest.mark.parametrize("name", STATEMENTS)
def test_a_parseable_statement_passes_whatever_bank_issued_it(name):
    """
    Not "these seven banks are special" -- these seven have different column
    orders, date formats, page counts and header layouts, and a generic
    parser has to cope with all of them.
    """
    document, _ = run(name)

    assert document["type"] == "BANK_STATEMENT"
    assert document["verification"] == "PASS", (
        f"{name} did not pass: {document.get('reason_codes')} "
        f"{document.get('reasons')}"
    )


@pytest.mark.parametrize("name", STATEMENTS)
def test_a_passing_statement_carries_no_reason_codes(name):
    document, _ = run(name)
    assert not document.get("reason_codes")
    assert not document.get("reasons")


# ==========================================================================
# EVERY NON-PASS EXPLAINS ITSELF
# ==========================================================================

def test_an_unreadable_scan_reviews_with_a_reason():
    """
    THE EMPTY-REASON BUG. This used to be `REVIEW` with `reason_codes: []`.

    It is also not a failure: a scan this build cannot read is a limit of
    the service, and the document goes to a person rather than being
    refused.
    """
    document, _ = run(SCANNED)

    assert document["verification"] == "REVIEW"
    assert document["verification"] != "FAIL"
    assert document["reason_codes"], "a REVIEW with no reason code"
    assert document["reasons"], "a REVIEW with no explanation"
    assert "DOCUMENT_REQUIRES_OCR" in document["reason_codes"]


def test_a_review_reason_is_written_for_a_person():
    """Not a stack trace, not a parser internal."""
    document, _ = run(SCANNED)
    reason = document["reasons"][0]

    assert len(reason) > 30
    for internal in ("Traceback", ".py", "parser", "tensor", "None",
                     "Exception"):
        assert internal not in reason, f"{internal!r} leaked into a reason"


@pytest.mark.parametrize("name", STATEMENTS + [SCANNED])
def test_every_non_pass_has_a_reason_and_every_pass_does_not(name):
    """The rule, stated once over the whole corpus."""
    document, _ = run(name)

    if document["verification"] == "PASS":
        assert not document.get("reason_codes")
    else:
        assert document.get("reason_codes"), (
            f"{name} is {document['verification']} with no reason code"
        )


# ==========================================================================
# THE TWO NUMBERS
# ==========================================================================

@pytest.mark.parametrize("name", STATEMENTS + [SCANNED])
def test_score_and_confidence_are_present_and_in_range(name):
    document, _ = run(name)

    assert 0 <= document["verification_score"] <= 100
    assert 0 <= document["verification_confidence"] <= 100


def test_a_passing_statement_scores_high_on_both():
    document, _ = run("bank_hdfc_new.pdf")
    assert document["verification_score"] >= 80
    assert document["verification_confidence"] >= 80


def test_an_unreadable_statement_scores_low_confidence():
    """
    Nothing was established either way, and confidence says so. A low score
    with HIGH confidence would claim the document had been checked and found
    wanting, which is not what happened.
    """
    document, _ = run(SCANNED)
    assert document["verification_confidence"] <= 40


# ==========================================================================
# THE GATE, AND WHAT IT RELEASES
# ==========================================================================

@pytest.mark.parametrize("name", STATEMENTS)
def test_a_pass_releases_fields_and_says_so(name):
    document, _ = run(name)
    assert document["has_extracted_fields"] is True
    assert document.get("extraction")


def test_a_review_releases_nothing_and_says_so():
    document, _ = run(SCANNED)
    assert document["has_extracted_fields"] is False
    assert not document.get("extraction")


def test_a_wrong_asserted_type_still_fails_outright():
    """
    A HARD GATE. The weighted score does not get to overturn it -- a bank
    statement uploaded as a PAN is the wrong document however cleanly it
    parses.
    """
    document, _ = run("bank_hdfc_new.pdf", expected="PAN")

    assert document["verification"] == "FAIL"
    assert "DOCUMENT_TYPE_MISMATCH" in document["reason_codes"]
    assert document["has_extracted_fields"] is False


# ==========================================================================
# NO FINANCIAL ANALYSIS REACHES FOS
# ==========================================================================

@pytest.mark.parametrize("name", STATEMENTS)
def test_no_income_analysis_leaks_into_a_fos_response(name):
    """
    A statement is verified here as a DOCUMENT. Average monthly credit and
    net salary are the credit stage's output and must not appear.
    """
    import json

    _, result = run(name)
    blob = json.dumps(result, default=str).lower()

    for forbidden in ("average_monthly_credit", "monthly_net_salary",
                      "monthly_gross_salary", "declared_annual_income",
                      "risk_score", "creditworth", "affordability"):
        assert forbidden not in blob, f"{forbidden!r} leaked into FOS"


@pytest.mark.parametrize("name", STATEMENTS)
def test_no_kyc_runs_for_a_bank_statement_upload(name):
    _, result = run(name)
    assert result.get("kyc") is None


# ==========================================================================
# THE LOS ENDPOINT KEEPS ITS ANALYSIS
# ==========================================================================

def test_the_credit_stage_still_gets_the_income_signals():
    """
    The boundary is on the FOS caller, not on the capability. Removing the
    analysis rather than scoping it would have deleted what the credit stage
    runs on.
    """
    path = SAMPLES / "bank_hdfc_new.pdf"
    if not path.exists():
        pytest.skip("sample not available")

    result = asyncio.run(process_application(
        [UploadedDocument(source_id="bank.pdf", filename="bank.pdf",
                          content=path.read_bytes(),
                          expected_type="BANK_STATEMENT")],
        operation=PROCESS, applicant_id="APP-C", case_id="CASE-C",
        request_id="credit-test",
    ))

    import json

    blob = json.dumps(result, default=str)
    assert "average_monthly_credit" in blob, (
        "the credit stage lost its income signals"
    )
    assert "kyc" in result, "the LOS endpoint stopped running KYC"


# ==========================================================================
# TIMEOUT IS NOT EMPTINESS
# ==========================================================================

def test_a_time_budget_overrun_reviews_and_does_not_fail(monkeypatch):
    """
    PHASE 4, re-checked. A parse the clock cut short has established
    nothing. It must not be reported as a statement that failed to
    reconcile, and it must not be silently treated as an empty table.
    """
    monkeypatch.setenv("BANK_STATEMENT_TIME_BUDGET_MS", "1200")

    document, _ = run("bank_canara.pdf")

    assert document["verification"] != "FAIL", (
        "a parser timeout was reported as a failed document"
    )
    if document["verification"] == "REVIEW":
        assert document["reason_codes"]
        joined = " ".join(document["reasons"]).lower()
        assert "do not add up" not in joined, (
            "a timeout was described as an arithmetic failure"
        )
