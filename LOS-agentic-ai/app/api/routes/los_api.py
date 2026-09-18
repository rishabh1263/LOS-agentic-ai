"""
LOS application API.

One call takes an applicant's documents end to end: each is classified,
verified and extracted by the Document Agent (which routes financial uploads
to the Financial Agent), the normalised results are cross-checked by KYC, and
one response comes back.

Every decision on that path is deterministic. A language model, when switched
on, writes the summary sentence and nothing else.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app.agents.los.flow import (
    PROCESS,
    PUBLIC_OPERATIONS,
    UploadedDocument,
    document_mode,
    process_application,
)
from app.agents.los.schemas import LosProcessResponse
from app.agents.document_agent.workflow import MAX_UPLOAD_BYTES
from app.store.ingest import persist_los_result

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/los", tags=["LOS"])

MAX_DOCUMENTS = 10


@router.post(
    "/process",
    summary="Process an applicant's documents end to end",
    description=(
        "Document Agent -> Financial Agent (where applicable) -> specialist "
        "capabilities -> KYC -> one response. Each page is rasterised, "
        "recognised and classified once. The canonical operation is "
        "PROCESS."
    ),
    # DOCUMENTED, NOT ENFORCED.
    #
    # `responses=` renders the contract in Swagger; `response_model=` would
    # additionally make FastAPI serialise through the model, and a key the
    # model had not been taught about would be silently dropped from a live
    # response. The shape is already decided in one place --
    # app/agents/los/response.py -- and this describes it rather than
    # competing with it. The 200 was previously documented as `{}`.
    responses={
        200: {
            "model": LosProcessResponse,
            "description": "The application, processed.",
        },
    },
)
async def process(
    files: list[UploadFile] = File(..., description="The applicant's documents."),
    operation: str = Form(
        default=PROCESS,
        description=(
            "PROCESS runs the whole application: classify, verify, extract "
            "behind the verification gate, route specialist evidence, "
            "cross-check with KYC, then decide. EXTRACT and VERIFY are the "
            "Document Agent's own modes, kept for existing callers -- VERIFY "
            "never releases extracted fields."
        ),
        json_schema_extra={"enum": list(PUBLIC_OPERATIONS)},
    ),
    applicant_id: str | None = Form(default=None),
    case_id: str | None = Form(
        default=None,
        description=(
            "The case this upload belongs to. Omit to start a new case for "
            "the applicant; supply one to add documents to an existing case. "
            "One applicant may have several cases, and they stay separate."
        ),
    ),
    expected_types: str | None = Form(
        default=None,
        description=(
            "Optional comma-separated expected type per file, positionally "
            "matched. Use AUTO to skip one."
        ),
    ),
):
    request_id = f"los_{uuid.uuid4().hex}"

    # Validated here so an unknown operation is refused at the boundary with
    # a message naming what IS accepted, rather than surfacing as a 500 from
    # the flow. `document_mode` is the single definition of what is valid.
    try:
        document_mode(operation)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    operation = (operation or PROCESS).strip().upper()

    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "request_id": request_id,
                "error": "NO_DOCUMENTS",
                "message": "At least one document is required.",
            },
        )

    if len(files) > MAX_DOCUMENTS:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "request_id": request_id,
                "error": "TOO_MANY_DOCUMENTS",
                "message": f"At most {MAX_DOCUMENTS} documents per application.",
            },
        )

    wanted = [t.strip() for t in (expected_types or "").split(",")] if expected_types else []

    uploads: list[UploadedDocument] = []
    for index, upload in enumerate(files):
        content = await upload.read()

        if not content:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "request_id": request_id,
                    "error": "EMPTY_FILE",
                    "message": f"Document {index + 1} is empty.",
                },
            )

        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail={
                    "request_id": request_id,
                    "error": "FILE_TOO_LARGE",
                    "message": (
                        f"Document {index + 1} exceeds the "
                        f"{MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit."
                    ),
                },
            )

        expected = wanted[index] if index < len(wanted) else None
        if expected in ("", "AUTO", "ANY"):
            expected = None

        uploads.append(UploadedDocument(
            source_id=upload.filename or f"document_{index + 1}",
            filename=upload.filename or f"document_{index + 1}",
            content=content,
            expected_type=expected,
        ))

    try:
        result = await process_application(
            uploads,
            operation=operation,
            applicant_id=applicant_id,
            case_id=case_id,
            request_id=request_id,
        )

        # Record what the pipeline concluded, so the FOS copilot can answer
        # questions about this case later.
        #
        # AFTER the result is complete and deliberately non-fatal: the caller
        # already has their answer, and a case store that is unavailable must
        # not turn a successful extraction into a 500. It copies verdicts; it
        # never changes one.
        persist_los_result(result)

        return result

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "request_id": request_id,
                "error": "INVALID_REQUEST",
                "message": str(exc),
            },
        ) from exc

    except Exception as exc:
        logger.exception("LOS application failed request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "request_id": request_id,
                "error": "LOS_PROCESSING_FAILED",
                "message": "Application processing failed unexpectedly.",
            },
        ) from exc
