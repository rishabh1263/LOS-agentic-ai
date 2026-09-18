# Document requirements

The documents a case requires depend on the loan product. The list is
configuration, not code, and it can change without a release.

## Personal loan

Mandatory:

- **PAN** — satisfied by a PAN card
- **BANK_STATEMENT** — satisfied by a bank statement
- **ADDRESS_PROOF** — satisfied by a driving licence, a passport or a voter ID

Optional:

- **SALARY_SLIP**
- **PHOTO**

An optional document never blocks the handoff to CPA. It appears on the
checklist so a field officer can see what has been collected beyond the
minimum, but its absence is not a pending item.

## Home loan

Mandatory: PAN, BANK_STATEMENT, ADDRESS_PROOF.
Optional: ITR, SALARY_SLIP, EMPLOYMENT_PROOF.

## Slots and document types

A checklist entry is a **slot**, not a document type. A slot names a
requirement; one or more document types satisfy it. ADDRESS_PROOF is the clear
case: there is no document called an address proof, but a driving licence, a
passport and a voter ID each prove an address.

Uploading a document against a slot name is allowed. The service resolves the
slot to the document types it accepts and checks the uploaded file against
that set.

## Checklist statuses

| Status | Meaning |
|---|---|
| MISSING | Nothing has been uploaded for this slot |
| UPLOADED | A file is held but has not completed verification |
| PROCESSING | Verification is running |
| VERIFIED | A document passed verification and satisfies the slot |
| REVIEW | A document needs a person to look at it |
| REJECTED | A document failed verification and must be replaced |

Only VERIFIED satisfies a slot.

## Where this list comes from

The requirements above are **this service's configuration**, in
`app/config/applicant_agent.yaml`, and nothing else. They are not a copy of
any lender's published policy and they are not an industry standard.

Indian lenders commonly ask for more than this at the personal-loan stage —
Aadhaar, Form 16, ITR, employment proof and photographs all appear on public
lender checklists. **None of them is mandatory here**, because a document
becomes required only by being listed as a mandatory slot for a product in
that file.

The document taxonomy is deliberately wider than the checklist: this service
can classify and verify more types than any product currently requires, so a
product can begin requiring one by configuration change rather than a
release. FOS readiness depends on the configured checklist and on nothing
else.

If your organisation's policy differs, change the configuration. Do not read
this page as the policy itself.
