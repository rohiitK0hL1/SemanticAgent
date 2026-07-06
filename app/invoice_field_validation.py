"""
Oracle Fusion AP invoice field validation for app18_mod.
"""
from __future__ import annotations

import copy
import re
from datetime import date, datetime
from typing import Any

ORACLE_MANDATORY_HEADER_FIELDS = [
    "InvoiceNumber",
    "InvoiceCurrency",
    "InvoiceAmount",
    "InvoiceDate",
    "BusinessUnit",
    "Supplier",
]

ORACLE_MANDATORY_LINE_FIELDS = [
    "LineNumber",
    "LineAmount",
]

KNOWN_PLACEHOLDER_INVOICE_NUMBERS = {"INV-PROMPT-01", "INV-DEMO-001", "INV-TEST-001"}

# Filled at approve in app18 (not expected in source JSON).
APPROVE_TIME_AUTOFILL_FIELDS = [
    "SupplierSite",
    "PaymentTerms",
    "Distributions",
    "Tax",
]

# Internal helper fields extracted from OCR; stripped before Oracle API create.
INTERNAL_INVOICE_FIELDS = ["DueDate", "PaymentDueDate", "TermsDate"]

_OCR_DUE_DATE_LABELS = frozenset({
    "duedate", "paymentduedate", "paybydate", "termsdate",
})

_OCR_PAYMENT_TERM_LABELS = frozenset({
    "paymentterm", "paymentterms", "terms",
})

_IMMEDIATE_ALIASES = frozenset({
    "immediate", "due on receipt", "due upon receipt", "payable on receipt",
    "payment due on receipt", "cod", "cash on delivery",
})

_TERMS_DAY_PATTERNS = [
    re.compile(r"within\s+(\d+)\s+days?", re.I),
    re.compile(r"due\s+in\s+(\d+)\s+days?", re.I),
    re.compile(r"(\d+)\s+days?\s+(?:net|payment|terms)", re.I),
    re.compile(r"net\s+(\d+)", re.I),
    re.compile(r"(\d+)\s+days?", re.I),
]

_NET_TERM_RE = re.compile(r"^net\s*(\d+)$", re.I)

ORACLE_MANDATORY_FIELDS_PROMPT = """
Output a JSON object with exactly three top-level keys in this order: "mapped_json", "missing_attributes", "autofilled_attributes".

missing_attributes: field names absent from the source document or user prompt (simple names only, e.g. "Supplier", "InvoiceDate", "Line 1 LineAmount"). Empty array if none.

autofilled_attributes: field names the integration will default or resolve — include any you defaulted in mapped_json plus: SupplierSite, Distributions, Tax (always include those three). Simple names only. Empty array if none besides those three.

Do NOT use long sentences in either array — only short field identifiers.
"""


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            item = str(item)
        key = item.strip()
        if key and key.lower() not in seen:
            seen.add(key.lower())
            out.append(key)
    return out


def _parse_iso_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _canonicalize_term_label(term: str) -> str:
    term = term.strip()
    if not term:
        return term
    if term.lower() in _IMMEDIATE_ALIASES:
        return "Immediate"
    net_match = _NET_TERM_RE.match(term)
    if net_match:
        return f"Net {int(net_match.group(1))}"
    return term


def _days_from_terms_text(text: str) -> int | None:
    if not text or not isinstance(text, str):
        return None
    lowered = text.strip().lower()
    if lowered in _IMMEDIATE_ALIASES or "due on receipt" in lowered or "upon receipt" in lowered:
        return 0
    for pattern in _TERMS_DAY_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def _pick_net_term(days: int, oracle_terms: list[str] | None) -> str:
    if days <= 0:
        return _match_oracle_term("Immediate", oracle_terms) or "Immediate"
    candidate = f"Net {days}"
    if not oracle_terms:
        return candidate
    exact = _match_oracle_term(candidate, oracle_terms)
    if exact:
        return exact
    net_options: list[tuple[int, str]] = []
    for term in oracle_terms:
        match = _NET_TERM_RE.match(term.strip())
        if match:
            net_options.append((int(match.group(1)), term.strip()))
    if not net_options:
        return candidate
    _day, closest = min(net_options, key=lambda item: abs(item[0] - days))
    return closest


def _is_oracle_style_term(term: str) -> bool:
    if not term or not str(term).strip():
        return False
    lowered = str(term).strip().lower()
    if lowered == "immediate" or lowered in _IMMEDIATE_ALIASES:
        return True
    return bool(_NET_TERM_RE.match(str(term).strip()))


def _match_oracle_term(candidate: str, oracle_terms: list[str] | None) -> str | None:
    if not candidate:
        return None
    canonical = _canonicalize_term_label(candidate)
    if not _is_oracle_style_term(canonical):
        return None
    if not oracle_terms:
        return canonical
    lowered_map = {term.strip().lower(): term.strip() for term in oracle_terms if term}
    return lowered_map.get(canonical.lower()) or lowered_map.get(candidate.strip().lower())


def _fuzzy_match_oracle_term(text: str, oracle_terms: list[str]) -> str | None:
    if not text or not oracle_terms:
        return None
    lowered = text.strip().lower()
    days = _days_from_terms_text(text)
    if days is not None:
        return _pick_net_term(days, oracle_terms)
    for term in oracle_terms:
        if term and term.strip().lower() in lowered:
            return term.strip()
    return None


def _invoice_due_date(invoice: dict) -> date | None:
    for key in ("DueDate", "PaymentDueDate", "TermsDate"):
        parsed = _parse_iso_date(invoice.get(key))
        if parsed:
            return parsed
    return None


def extract_payment_hints_from_ocr(ocr_result: dict) -> dict[str, str]:
    """Pull DueDate and payment-term text directly from OCI document_fields."""
    hints: dict[str, str] = {}
    for page in ocr_result.get("pages") or []:
        for field in page.get("document_fields") or []:
            label_obj = field.get("field_label") or {}
            name = str(label_obj.get("name") or "").strip()
            if not name:
                continue

            value_obj = field.get("field_value") or {}
            raw_value = value_obj.get("value")
            if raw_value is None:
                raw_value = value_obj.get("text")
            if raw_value is None:
                continue

            norm_name = re.sub(r"[^a-z0-9]", "", name.lower())

            if norm_name in _OCR_DUE_DATE_LABELS or norm_name.endswith("duedate"):
                parsed = _parse_iso_date(raw_value)
                if parsed:
                    hints["DueDate"] = parsed.isoformat()

            if norm_name in _OCR_PAYMENT_TERM_LABELS:
                text = str(raw_value).strip()
                if text:
                    hints["PaymentTerms"] = text

    return hints


def enrich_invoice_payment_hints(invoice: dict, hints: dict[str, str] | None) -> list[str]:
    """Merge OCR payment hints into invoice when mapped JSON omitted them."""
    applied: list[str] = []
    if not hints:
        return applied

    if not _invoice_due_date(invoice) and hints.get("DueDate"):
        invoice["DueDate"] = hints["DueDate"]
        applied.append("DueDate")

    current_terms = invoice.get("PaymentTerms")
    if _is_empty(current_terms) and hints.get("PaymentTerms"):
        invoice["PaymentTerms"] = hints["PaymentTerms"]
        applied.append("PaymentTerms")

    return applied


def resolve_invoice_payment_terms(
    invoice: dict,
    *,
    oracle_terms: list[str] | None = None,
    supplier_site_term: str | None = None,
) -> tuple[str | None, str]:
    """
    Resolve PaymentTerms to an Oracle Fusion LOV value.
    Returns (resolved_term_or_none, resolution_source).
    """
    current = invoice.get("PaymentTerms")
    current_text = current.strip() if isinstance(current, str) else ""

    matched = _match_oracle_term(current_text, oracle_terms) if current_text else None
    if matched:
        return matched, "existing Oracle code"

    if current_text:
        days = _days_from_terms_text(current_text)
        if days is not None:
            resolved = _pick_net_term(days, oracle_terms)
            return resolved, f"parsed payment terms text ({days} days)"

        if oracle_terms:
            fuzzy = _fuzzy_match_oracle_term(current_text, oracle_terms)
            if fuzzy:
                return fuzzy, "fuzzy matched Oracle LOV"

    due_date = _invoice_due_date(invoice)
    invoice_date = _parse_iso_date(invoice.get("InvoiceDate"))
    if due_date and invoice_date:
        day_gap = (due_date - invoice_date).days
        if day_gap >= 0:
            resolved = _pick_net_term(day_gap, oracle_terms)
            return resolved, f"inferred from DueDate ({day_gap} days)"

    if supplier_site_term:
        matched_site = _match_oracle_term(supplier_site_term, oracle_terms)
        if matched_site:
            return matched_site, "supplier site default"

    return None, "unresolved"


def strip_internal_invoice_fields(invoice: dict) -> None:
    for field in INTERNAL_INVOICE_FIELDS:
        invoice.pop(field, None)


def _normalize_missing_token(item: str) -> str:
    """Collapse AI phrasing to a short field name."""
    item = item.strip()
    paren = re.search(r"\((\w+)\)\s*$", item)
    if paren:
        return paren.group(1)
    if " - " in item:
        item = item.split(" - ", 1)[0].strip()
    return item


def normalize_invoice_payload(
    mapped_json: Any,
    *,
    oracle_terms: list[str] | None = None,
    supplier_site_term: str | None = None,
) -> tuple[Any, list[str]]:
    """Apply defaults; return normalized payload and list of fields autofilled."""
    if mapped_json is None:
        return mapped_json, []

    normalized = copy.deepcopy(mapped_json)
    autofilled: list[str] = []
    invoices = normalized if isinstance(normalized, list) else [normalized]

    for inv_idx, invoice in enumerate(invoices):
        if not isinstance(invoice, dict):
            continue
        prefix = f"Invoice {inv_idx + 1} " if len(invoices) > 1 else ""

        if _is_empty(invoice.get("InvoiceType")):
            invoice["InvoiceType"] = "Standard"
            autofilled.append(f"{prefix}InvoiceType".strip())

        if _is_empty(invoice.get("SupplierSite")):
            autofilled.append(f"{prefix}SupplierSite".strip())

        resolved_term, source = resolve_invoice_payment_terms(
            invoice,
            oracle_terms=oracle_terms,
            supplier_site_term=supplier_site_term,
        )
        if resolved_term:
            invoice["PaymentTerms"] = resolved_term
            if source != "existing Oracle code":
                autofilled.append(f"{prefix}PaymentTerms".strip())
        strip_internal_invoice_fields(invoice)

        lines = invoice.get("invoiceLines") or []
        for idx, line in enumerate(lines):
            if not isinstance(line, dict):
                continue
            line_label = f"Line {idx + 1}"
            if _is_empty(line.get("LineType")):
                line["LineType"] = "Item"
                autofilled.append(f"{line_label} LineType")
            if _is_empty(line.get("LineNumber")):
                line["LineNumber"] = idx + 1
                autofilled.append(f"{line_label} LineNumber")

    autofilled.extend(["Distributions", "Tax"])
    return normalized, _dedupe(autofilled)


def compute_missing_mandatory_fields(mapped_json: Any) -> list[str]:
    if mapped_json is None:
        return ["mapped_json"]

    invoices = mapped_json if isinstance(mapped_json, list) else [mapped_json]
    missing: list[str] = []

    for inv_idx, invoice in enumerate(invoices):
        if not isinstance(invoice, dict):
            missing.append("mapped_json")
            continue

        prefix = f"Invoice {inv_idx + 1} " if len(invoices) > 1 else ""

        for field in ORACLE_MANDATORY_HEADER_FIELDS:
            value = invoice.get(field)
            if _is_empty(value):
                missing.append(f"{prefix}{field}".strip())
            elif field == "InvoiceNumber" and isinstance(value, str) and value.strip() in KNOWN_PLACEHOLDER_INVOICE_NUMBERS:
                missing.append("InvoiceNumber")

        lines = invoice.get("invoiceLines")
        if _is_empty(lines):
            missing.append(f"{prefix}invoiceLines".strip())
            continue

        for line_idx, line in enumerate(lines):
            if not isinstance(line, dict):
                continue
            line_label = f"Line {line_idx + 1}"
            for field in ORACLE_MANDATORY_LINE_FIELDS:
                if _is_empty(line.get(field)):
                    missing.append(f"{line_label} {field}")

    return _dedupe(missing)


def enrich_missing_attributes(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return payload

    mapped = payload.get("mapped_json")
    autofilled: list[str] = []
    if mapped is not None:
        mapped, autofilled = normalize_invoice_payload(mapped)
        payload["mapped_json"] = mapped

    computed_missing = compute_missing_mandatory_fields(mapped)

    ai_missing = payload.get("missing_attributes")
    if not isinstance(ai_missing, list):
        ai_missing = []
    ai_missing = [_normalize_missing_token(x) for x in ai_missing if x]

    payload["missing_attributes"] = _dedupe(ai_missing + computed_missing)
    payload["autofilled_attributes"] = _dedupe(autofilled)
    return payload


def format_attributes_summary(payload: dict) -> str:
    missing = payload.get("missing_attributes") or []
    autofilled = payload.get("autofilled_attributes") or []
    missing_text = ", ".join(missing) if missing else "none"
    autofilled_text = ", ".join(autofilled) if autofilled else "none"
    return (
        f"**missing_attributes:** {missing_text}\n"
        f"**autofilled_attributes:** {autofilled_text}"
    )
