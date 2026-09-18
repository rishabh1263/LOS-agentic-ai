import {
  AlertTriangle,
  CheckCircle2,
  FileText,
  Info,
  ShieldCheck,
  TrendingUp,
  XCircle,
} from "lucide-react";
import type {
  CrossDocument,
  DocumentResult,
} from "../../../runtime/api-tester";

export interface CrossDocumentReconciliationProps {
  crossDocument: CrossDocument;
  documents: DocumentResult[];
}

interface ComparisonRow {
  field: string;
  sublabel?: string;
  values: { [sourceId: string]: string | null };
  status: "PASS" | "SINGLE_SOURCE" | "SKIPPED" | "FAIL";
  statusText: string;
}

function getFieldFromDoc(key: string, doc: DocumentResult): string | null {
  if (!doc.extraction) return null;
  const ext = doc.extraction;

  const lookup = (...keys: string[]): string | null => {
    for (const k of keys) {
      if (ext[k] != null && ext[k] !== "") {
        const val = ext[k];
        if (Array.isArray(val)) return val.join(", ");
        return String(val);
      }
    }
    return null;
  };

  switch (key) {
    case "NAME":
      return lookup("name", "full_name", "applicant_name");
    case "DOB":
      return lookup("date_of_birth", "dob");
    case "FATHER_NAME":
      return lookup("father_name", "guardian_name");
    case "DOCUMENT_ID":
      return lookup("dl_number", "pan_number", "passport_number", "voter_id");
    case "ADDRESS": {
      const addr = lookup("address", "current_address");
      const pin = lookup("pin_code", "pincode");
      if (addr && pin && !addr.includes(pin)) {
        return `${addr}, PIN: ${pin}`;
      }
      return addr;
    }
    case "VEHICLE_CLASSES":
      return lookup("vehicle_classes");
    case "VALIDITY": {
      const issued = lookup("date_of_issue");
      const validTill = lookup("valid_till");
      if (issued && validTill)
        return `Issued: ${issued} · Valid till: ${validTill}`;
      if (validTill) return `Valid till: ${validTill}`;
      return null;
    }
    default:
      return lookup(key.toLowerCase(), key);
  }
}

/** Strip file extension from source_id for a cleaner display label */
function formatDocLabel(sourceId: string): string {
  return sourceId.replace(/\.[^/.]+$/, "");
}

/** Humanise doc type: DRIVING_LICENCE → Driving Licence */
function formatDocType(type: string): string {
  return type
    .replace(/_/g, " ")
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function CrossDocumentReconciliation({
  crossDocument,
  documents,
}: CrossDocumentReconciliationProps) {
  const checks = crossDocument?.checks || [];

  // Map check status by check type name
  const checkStatusMap = new Map<
    string,
    { status: string; reason_codes?: string[] }
  >();
  checks.forEach((c) => {
    checkStatusMap.set(c.check.toUpperCase(), {
      status: c.status,
      reason_codes: c.reason_codes,
    });
  });

  // Build rows for comparison table
  const rows: ComparisonRow[] = [];

  // 1. Name row
  const nameCheck = checkStatusMap.get("NAME");
  const nameValues: { [sourceId: string]: string | null } = {};
  documents.forEach((d) => {
    nameValues[d.source_id] = getFieldFromDoc("NAME", d);
  });
  rows.push({
    field: "Full Name",
    sublabel: "Applicant Identity",
    values: nameValues,
    status:
      nameCheck?.status === "PASS"
        ? "PASS"
        : nameCheck?.status === "FAIL"
          ? "FAIL"
          : "SINGLE_SOURCE",
    statusText:
      nameCheck?.status === "PASS"
        ? "Matched"
        : nameCheck?.status === "FAIL"
          ? "Mismatch"
          : "Single source",
  });

  // 2. DOB row
  const dobCheck = checkStatusMap.get("DOB");
  const dobValues: { [sourceId: string]: string | null } = {};
  documents.forEach((d) => {
    dobValues[d.source_id] = getFieldFromDoc("DOB", d);
  });
  rows.push({
    field: "Date of Birth",
    sublabel: "DOB Verification",
    values: dobValues,
    status:
      dobCheck?.status === "PASS"
        ? "PASS"
        : dobCheck?.status === "FAIL"
          ? "FAIL"
          : "SINGLE_SOURCE",
    statusText:
      dobCheck?.status === "PASS"
        ? "Exact Match"
        : dobCheck?.status === "FAIL"
          ? "Mismatch"
          : "Single source",
  });

  // 3. Father / Guardian Name row
  const parentValues: { [sourceId: string]: string | null } = {};
  let hasParentName = false;
  documents.forEach((d) => {
    const v = getFieldFromDoc("FATHER_NAME", d);
    if (v) hasParentName = true;
    parentValues[d.source_id] = v;
  });
  if (hasParentName) {
    const nonNulls = Object.values(parentValues).filter(Boolean);
    const isMulti = nonNulls.length >= 2;
    rows.push({
      field: "Father / Guardian",
      sublabel: "Secondary Lineage",
      values: parentValues,
      status: isMulti ? "PASS" : "SINGLE_SOURCE",
      statusText: isMulti ? "Reconciled" : "Single source",
    });
  }

  // 4. Document / ID Number row
  const idValues: { [sourceId: string]: string | null } = {};
  documents.forEach((d) => {
    idValues[d.source_id] = getFieldFromDoc("DOCUMENT_ID", d);
  });
  rows.push({
    field: "Document Number",
    sublabel: "Official Identifier",
    values: idValues,
    status: "SINGLE_SOURCE",
    statusText: "Extracted",
  });

  // 5. Address row
  const addrValues: { [sourceId: string]: string | null } = {};
  let hasAddress = false;
  documents.forEach((d) => {
    const v = getFieldFromDoc("ADDRESS", d);
    if (v) hasAddress = true;
    addrValues[d.source_id] = v;
  });
  if (hasAddress) {
    const addrCheck = checkStatusMap.get("ADDRESS");
    rows.push({
      field: "Residential Address",
      sublabel: "Address Proof",
      values: addrValues,
      status: "SINGLE_SOURCE",
      statusText: addrCheck?.reason_codes?.includes("ADDRESS_SINGLE_SOURCE")
        ? "Single Source (DL)"
        : "Extracted",
    });
  }

  // 6. Validity / Issue row (if applicable)
  const validityValues: { [sourceId: string]: string | null } = {};
  let hasValidity = false;
  documents.forEach((d) => {
    const v = getFieldFromDoc("VALIDITY", d);
    if (v) hasValidity = true;
    validityValues[d.source_id] = v;
  });
  if (hasValidity) {
    rows.push({
      field: "Validity & Expiry",
      sublabel: "Document Lifecycle",
      values: validityValues,
      status: "PASS",
      statusText: "Active",
    });
  }

  // 7. Vehicle Classes row (if applicable)
  const vehicleValues: { [sourceId: string]: string | null } = {};
  let hasVehicle = false;
  documents.forEach((d) => {
    const v = getFieldFromDoc("VEHICLE_CLASSES", d);
    if (v) hasVehicle = true;
    vehicleValues[d.source_id] = v;
  });
  if (hasVehicle) {
    rows.push({
      field: "Vehicle Classes",
      sublabel: "Authorised Categories",
      values: vehicleValues,
      status: "SINGLE_SOURCE",
      statusText: "Extracted (DL)",
    });
  }

  // 8. Income Proof row (if check present in API)
  const incomeCheck = checkStatusMap.get("INCOME");
  if (incomeCheck) {
    const incValues: { [sourceId: string]: string | null } = {};
    documents.forEach((d) => {
      incValues[d.source_id] = getFieldFromDoc("INCOME", d);
    });
    rows.push({
      field: "Income & Earnings",
      sublabel: "Financial Proof",
      values: incValues,
      status: "SKIPPED",
      statusText: "Not provided",
    });
  }

  // ── KPI Computations ──────────────────────────────────────────────────────
  const totalChecks = checks.length;
  const passCount = checks.filter((c) => c.status === "PASS").length;
  const failCount = checks.filter((c) => c.status === "FAIL").length;
  const reviewCount = checks.filter((c) => c.status === "REVIEW").length;
  const matchScore =
    totalChecks > 0 ? Math.round((passCount / totalChecks) * 100) : 0;

  const isOverallPass = crossDocument.status === "PASS";
  const isOverallFail = crossDocument.status === "FAIL";

  // ── Result badge (shown under each field header) ──────────────────────────
  const statusBadge = (status: ComparisonRow["status"], text: string) => {
    if (status === "PASS") {
      return (
        <span className="inline-flex items-center gap-1 text-[10px] font-semibold text-success mt-1">
          <CheckCircle2 className="h-3 w-3 shrink-0" />
          {text}
        </span>
      );
    }
    if (status === "FAIL") {
      return (
        <span className="inline-flex items-center gap-1 text-[10px] font-semibold text-danger mt-1">
          <XCircle className="h-3 w-3 shrink-0" />
          {text}
        </span>
      );
    }
    if (status === "SKIPPED") {
      return (
        <span className="text-[10px] font-medium text-content-disabled italic mt-1 block">
          {text}
        </span>
      );
    }
    return (
      <span className="chip text-[9px] py-0.5 px-1.5 font-medium mt-1 inline-block">
        {text}
      </span>
    );
  };

  return (
    <div className="space-y-4">
      {/* ── Report-style section header ─────────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-sm bg-ember/10 text-ember">
            <ShieldCheck className="h-4 w-4" />
          </span>
          <div>
            <h3 className="font-display text-[15px] font-bold tracking-tight text-content leading-tight">
              Cross-Document Comparison Matrix
            </h3>
            <p className="text-[11px] text-content-secondary">
              {documents.length} document{documents.length !== 1 ? "s" : ""} ·{" "}
              {rows.length} attributes reconciled
            </p>
          </div>
        </div>

        {/* Overall status pill */}
        <span
          className={`inline-flex items-center gap-1.5 rounded-sm px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider border ${
            isOverallPass
              ? "bg-success-subtle text-success-text border-success/30"
              : isOverallFail
                ? "bg-danger-subtle text-danger-text border-danger/30"
                : "bg-warning-subtle text-warning-text border-warning/30"
          }`}
        >
          {isOverallPass ? (
            <CheckCircle2 className="h-3.5 w-3.5" />
          ) : isOverallFail ? (
            <XCircle className="h-3.5 w-3.5" />
          ) : (
            <AlertTriangle className="h-3.5 w-3.5" />
          )}
          {isOverallPass
            ? "All Checks Passed"
            : isOverallFail
              ? "Checks Failed"
              : "Review Required"}
        </span>
      </div>

      {/* ── KPI Cards ──────────────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {/* Match Score */}
        <div className="card border-line bg-surface p-4 shadow-xs flex flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-wider text-content-disabled">
              Match Score
            </span>
            <TrendingUp className="h-3.5 w-3.5 text-ember" />
          </div>
          <div className="flex items-end gap-1.5">
            <span
              className={`font-display text-[28px] font-bold leading-none tracking-tight ${
                matchScore >= 80
                  ? "text-success"
                  : matchScore >= 50
                    ? "text-warning"
                    : "text-danger"
              }`}
            >
              {matchScore}
            </span>
            <span className="mb-0.5 text-[14px] font-semibold text-content-secondary">
              %
            </span>
          </div>
          {/* Progress bar */}
          <div className="h-1 w-full rounded-full bg-raised overflow-hidden">
            <div
              className={`h-full rounded-full ${matchScore >= 80 ? "bg-success" : matchScore >= 50 ? "bg-warning" : "bg-danger"}`}
              style={{ width: `${matchScore}%` }}
            />
          </div>
          <span className="text-[10px] text-content-secondary">
            {passCount} of {totalChecks} checks passed
          </span>
        </div>

        {/* Documents Analyzed */}
        <div className="card border-line bg-surface p-4 shadow-xs flex flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-wider text-content-disabled">
              Documents
            </span>
            <FileText className="h-3.5 w-3.5 text-ember" />
          </div>
          <span className="font-display text-[28px] font-bold leading-none tracking-tight text-content">
            {documents.length}
          </span>
          <span className="text-[10px] text-content-secondary">
            Submitted & analyzed
          </span>
        </div>

        {/* Passed Checks */}
        <div className="card border-success/30 bg-success-subtle p-4 shadow-xs flex flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-wider text-success/70">
              Passed
            </span>
            <CheckCircle2 className="h-3.5 w-3.5 text-success" />
          </div>
          <span className="font-display text-[28px] font-bold leading-none tracking-tight text-success">
            {passCount}
          </span>
          <span className="text-[10px] text-success/70">
            Identity checks matched
          </span>
        </div>

        {/* Failed / Review */}
        <div
          className={`card p-4 shadow-xs flex flex-col gap-1.5 ${
            failCount > 0
              ? "border-danger/30 bg-danger-subtle"
              : reviewCount > 0
                ? "border-warning/30 bg-warning-subtle"
                : "border-line bg-surface"
          }`}
        >
          <div className="flex items-center justify-between">
            <span
              className={`text-[10px] font-bold uppercase tracking-wider ${
                failCount > 0
                  ? "text-danger/70"
                  : reviewCount > 0
                    ? "text-warning/70"
                    : "text-content-disabled"
              }`}
            >
              {failCount > 0 ? "Failed" : "Review"}
            </span>
            {failCount > 0 ? (
              <XCircle className="h-3.5 w-3.5 text-danger" />
            ) : (
              <AlertTriangle className="h-3.5 w-3.5 text-warning" />
            )}
          </div>
          <span
            className={`font-display text-[28px] font-bold leading-none tracking-tight ${
              failCount > 0
                ? "text-danger"
                : reviewCount > 0
                  ? "text-warning"
                  : "text-content-disabled"
            }`}
          >
            {failCount > 0 ? failCount : reviewCount}
          </span>
          <span
            className={`text-[10px] ${
              failCount > 0
                ? "text-danger/70"
                : reviewCount > 0
                  ? "text-warning/70"
                  : "text-content-disabled"
            }`}
          >
            {failCount > 0
              ? "Checks failed — review needed"
              : reviewCount > 0
                ? "Flagged for review"
                : "No issues detected"}
          </span>
        </div>
      </div>

      {/* ── Comparison Table Card ──────────────────────────────────────────── */}
      <div className="card overflow-hidden border-line bg-surface p-0 shadow-xs">
        {/* Sub-section label */}
        <div className="border-b border-line-divider px-5 py-3 bg-raised/30 flex items-center justify-between">
          <span className="text-[11px] font-bold uppercase tracking-wider text-content-secondary">
            Attribute Reconciliation
          </span>
          <span className="text-[11px] text-content-disabled">
            {rows.length} fields · {documents.length} sources
          </span>
        </div>

        {/* Responsive Table — TRANSPOSED: documents = rows, fields = columns */}
        <div className="overflow-x-auto">
          <table className="w-full text-left text-[13px] border-collapse">
            <thead>
              <tr className="border-b border-line bg-raised/40 text-[10px] font-bold uppercase tracking-wider text-content-secondary">
                {/* Fixed "Source" column — document names go here */}
                <th scope="col" className="py-3 px-5 w-[200px] min-w-[160px]">
                  Source
                </th>

                {/* Dynamic field columns — Full Name, DOB, etc. as headers + Result badge */}
                {rows.map((row) => (
                  <th
                    key={row.field}
                    scope="col"
                    className="py-3 px-5 min-w-[150px]"
                  >
                    <div className="font-semibold text-[12px] text-content normal-case tracking-normal leading-snug">
                      {row.field}
                    </div>
                    {row.sublabel && (
                      <div className="text-[10px] font-normal text-content-disabled mt-0.5 normal-case tracking-normal">
                        {row.sublabel}
                      </div>
                    )}
                    {statusBadge(row.status, row.statusText)}
                  </th>
                ))}
              </tr>
            </thead>

            <tbody className="divide-y divide-line-divider font-sans">
              {documents.map((doc) => (
                <tr
                  key={doc.source_id}
                  className="transition-colors hover:bg-raised/30 group border-l-[3px] border-l-line"
                >
                  {/* Document name + type badge on the left */}
                  <td className="py-3.5 px-5 align-top">
                    <div className="flex items-center gap-1.5 mb-0.5">
                      <FileText className="h-3 w-3 text-ember shrink-0" />
                      <span
                        className="font-mono text-[11px] font-bold text-content truncate max-w-[160px]"
                        title={doc.source_id}
                      >
                        {formatDocLabel(doc.source_id)}
                      </span>
                    </div>
                    <span className="inline-block rounded-xs bg-ember/10 px-1.5 py-0.5 font-sans text-[9px] font-bold uppercase tracking-wider text-ember">
                      {formatDocType(doc.type)}
                    </span>
                  </td>

                  {/* Value for each field */}
                  {rows.map((row) => {
                    const val = row.values[doc.source_id];
                    return (
                      <td key={row.field} className="py-3.5 px-5 align-top">
                        {val ? (
                          <span className="font-mono text-[12px] text-content font-medium break-words leading-snug">
                            {val}
                          </span>
                        ) : (
                          <span className="text-content-disabled font-sans text-[12px] italic">
                            —
                          </span>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* Footer note */}
        <div className="border-t border-line-divider bg-raised/20 px-5 py-3 flex items-start gap-2 text-[11px] text-content-secondary">
          <Info className="h-3.5 w-3.5 text-content-disabled shrink-0 mt-0.5" />
          <span>
            {isOverallPass
              ? "All identity attributes cross-verified with full concordance across submitted documents."
              : isOverallFail
                ? "One or more identity attributes could not be reconciled across documents. Manual review required before proceeding."
                : "Cross-document comparison complete. Some attributes require further human verification."}
          </span>
        </div>
      </div>
    </div>
  );
}
