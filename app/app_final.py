import truststore
truststore.inject_into_ssl()

from langchain_core.messages import HumanMessage, AIMessageChunk, ToolMessage, AIMessage, SystemMessage
from langchain_core.prompts import PromptTemplate
from langchain_core.tools import tool
from langchain_core.runnables.config import RunnableConfig
from langchain_openai import ChatOpenAI
from langfuse.langchain import CallbackHandler

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph, END, START, MessagesState
import chainlit as cl
from DataQualityAssessor import generate_report

from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv
import os
import pandas as pd
from io import BytesIO
import base64
import openpyxl
import json
from ConnectDB import dbmain
import asyncio
import re
import uuid
from DocumentKeyValueExtract import process_document
import requests
from requests.auth import HTTPBasicAuth
from oracle_invoice_with_oci import create_invoice, build_headers, get_invoice_self_href, upload_attachment, post_action
from invoice_field_validation import (
    enrich_invoice_payment_hints,
    extract_payment_hints_from_ocr,
    normalize_invoice_payload,
)
from expense import (
    create_expense_report,
    upload_expense_attachment,
    EXPENSE_TYPE_KEYWORDS,
    normalize_expense_type,
    match_expense_type_label,
    search_expense_locations,
)
import pdfplumber

load_dotenv()

langfuse_handler = CallbackHandler()


def build_run_config(thread_id: str) -> RunnableConfig:
    """Build a LangGraph run config that includes Langfuse tracing when available."""
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    if langfuse_handler is not None:
        config["callbacks"] = [langfuse_handler]
    return config


def _msg_metadata(msg: object) -> dict:
    meta = getattr(msg, "metadata", None)
    return meta if isinstance(meta, dict) else {}


def _content_as_str(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and "text" in part:
                parts.append(str(part["text"]))
            else:
                parts.append(str(part))
        return " ".join(parts)
    return str(content)


# -----------------------------
# Expense constants & helpers
# -----------------------------
# Extend shared keyword map with short-term lodging providers
EXPENSE_TYPE_KEYWORDS_EXT = {
    **EXPENSE_TYPE_KEYWORDS,
    "Hotel": EXPENSE_TYPE_KEYWORDS["Hotel"] + [
        "airbnb", "vrbo", "booking.com", "short-term rental", "short stay", "homestay",
    ],
}

EXPENSE_FILE_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png")

FUSION_EXPENSE_URL = (
    "https://fa-eqjw-dev3-saasfademo1.ds-fa.oraclepdemos.com/fscmUI/faces/FndOverview"
    "?fndGlobalItemNodeId=itemNode_my_information_expenses&_adf.ctrl-state=pqzrz76yl_1"
    "&_adf.no-new-window-redirect=true&_afrLoop=2017732684041670&_afrWindowMode=2"
    "&_afrWindowId=null&_afrFS=16&_afrMT=screen&_afrMFW=1280&_afrMFH=551&_afrMFDW=1280"
    "&_afrMFDH=720&_afrMFC=8&_afrMFCI=0&_afrMFM=0&_afrMFR=144&_afrMFG=0&_afrMFS=0&_afrMFO=0"
)

DOCUMENT_CLASSIFIER_PROMPT = """You classify uploaded financial documents.
Respond with exactly one word: expense or invoice.

- expense: employee expense receipts (travel, meals, hotel, Airbnb, taxi, mileage, parking, supplies, etc.)
- invoice: supplier/vendor invoices with PO numbers, supplier sites, invoice lines, or distribution accounts"""


def infer_expense_type_from_input(expense_input: dict) -> str:
    """Resolve ExpenseType from merchant, description, and LLM label using keyword map.

    Uses word-boundary + specificity matching so that, e.g., an "Airbnb" merchant
    resolves to "Hotel" instead of accidentally matching "Air" (airfare) via the
    "air" substring. The most specific keyword across merchant/description/label
    wins, so the chosen type strictly reflects what the expense most relates to.
    """
    combined = " ".join(filter(None, [
        expense_input.get("MerchantName", ""),
        expense_input.get("Description", ""),
        expense_input.get("ExpenseType", ""),
    ]))
    matched = match_expense_type_label(combined, EXPENSE_TYPE_KEYWORDS_EXT)
    if matched:
        return matched
    return normalize_expense_type(expense_input.get("ExpenseType") or "Miscellaneous")


def is_expense_eligible_file(file_name: str) -> bool:
    return file_name.lower().endswith(EXPENSE_FILE_EXTENSIONS)


def _merchant_name_counts(expense_inputs: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for expense_input in expense_inputs:
        merchant = (expense_input.get("MerchantName") or "").strip().lower()
        if merchant:
            counts[merchant] = counts.get(merchant, 0) + 1
    return counts


def build_expense_display_name(
    expense_input: dict,
    file_name: str = "",
    merchant_counts: dict[str, int] | None = None,
) -> str:
    """Human-readable expense label; adds expense type when merchant names duplicate."""
    merchant = (expense_input.get("MerchantName") or "").strip()
    expense_type = infer_expense_type_from_input(expense_input)
    type_label = "Flight" if expense_type == "Air" else expense_type

    if merchant and merchant_counts and merchant_counts.get(merchant.lower(), 0) > 1:
        return f"{merchant} – {type_label} Expense"
    if merchant:
        return f"{merchant} Expense"
    if expense_type == "Air":
        return "Flight Expense"
    return f"{expense_type} Expense"


def get_expense_display_name(expense_input: dict, file_name: str = "") -> str:
    return build_expense_display_name(expense_input, file_name)


def assign_expense_display_names(candidates: list[dict]) -> list[dict]:
    expense_inputs = [c.get("expense_input") or {} for c in candidates]
    merchant_counts = _merchant_name_counts(expense_inputs)
    for candidate in candidates:
        expense_input = candidate.get("expense_input") or {}
        candidate["display_name"] = build_expense_display_name(
            expense_input,
            candidate.get("file_name", ""),
            merchant_counts,
        )
    return candidates


def assign_attachment_queue_display_names(queue: list[dict]) -> list[dict]:
    expense_inputs = [item.get("expense_input") or {} for item in queue]
    merchant_counts = _merchant_name_counts(expense_inputs)
    for item in queue:
        expense_input = item.get("expense_input") or {}
        item["display_name"] = build_expense_display_name(
            expense_input,
            item.get("file_name", ""),
            merchant_counts,
        )
    return queue


def format_expense_amount(expense_input: dict) -> str:
    amount = expense_input.get("ReceiptAmount") or expense_input.get("ReimbursableAmount") or 0
    try:
        return f"${float(amount):,.2f}"
    except (TypeError, ValueError):
        return f"${amount}"


def format_expense_summary_line(index: int, candidate: dict) -> str:
    """Approval list entry: display name plus amount and optional description."""
    expense_input = candidate.get("expense_input") or {}
    display_name = candidate.get("display_name") or build_expense_display_name(
        expense_input,
        candidate.get("file_name", ""),
    )
    amount_str = format_expense_amount(expense_input)
    description = (expense_input.get("Description") or "").strip()
    if description:
        return f"{index}. {display_name} – {description} – {amount_str}"
    return f"{index}. {display_name} – {amount_str}"


def format_expense_candidate_line(index: int, expense_input: dict, file_name: str) -> str:
    expense_type = infer_expense_type_from_input(expense_input)
    description = (
        expense_input.get("Description")
        or expense_input.get("MerchantName")
        or os.path.splitext(file_name)[0]
    )
    amount = expense_input.get("ReceiptAmount") or expense_input.get("ReimbursableAmount") or 0
    try:
        amount_str = f"${float(amount):,.2f}"
    except (TypeError, ValueError):
        amount_str = f"${amount}"
    return f"{index}. {expense_type} Expense – {description} – {amount_str}"


def read_uploaded_file(uploaded) -> tuple[str, bytes | None, bool, bool, bool]:
    file_name = uploaded.name
    file_bytes = None
    file_path = uploaded.path if hasattr(uploaded, "path") else None
    if file_path is not None:
        with open(file_path, "rb") as f:
            file_bytes = f.read()
    is_pdf = file_name.lower().endswith(".pdf")
    is_excel = file_name.lower().endswith((".xls", ".xlsx", ".xlsm"))
    is_jpg = file_name.lower().endswith((".jpg", ".jpeg", ".png"))
    return file_name, file_bytes, is_pdf, is_excel, is_jpg


def build_expense_candidate(
    extracted_json: dict,
    file_name: str,
    file_bytes: bytes | None,
) -> dict | None:
    mapped = extracted_json.get("mapped_json", extracted_json)
    if not mapped or not isinstance(mapped, dict):
        return None
    if not mapped.get("ReceiptAmount"):
        return None
    if not (mapped.get("MerchantName") or mapped.get("Description")):
        return None
    expense_input = dict(mapped)
    expense_input["ExpenseType"] = infer_expense_type_from_input(expense_input)
    return {
        "file_name": file_name,
        "file_bytes": file_bytes,
        "payload": extracted_json,
        "expense_input": expense_input,
        "display_name": get_expense_display_name(expense_input, file_name),
    }


def parse_expense_candidates_from_extraction(
    extracted_json: dict,
    file_name: str,
    file_bytes: bytes | None,
) -> list[dict]:
    mapped = extracted_json.get("mapped_json", extracted_json)
    if isinstance(mapped, list):
        candidates = []
        for item in mapped:
            if not isinstance(item, dict):
                continue
            wrapped = {"mapped_json": item, "missing_attributes": extracted_json.get("missing_attributes", [])}
            candidate = build_expense_candidate(wrapped, file_name, file_bytes)
            if candidate:
                candidates.append(candidate)
        return candidates
    candidate = build_expense_candidate(extracted_json, file_name, file_bytes)
    return [candidate] if candidate else []


def get_fusion_auth_headers():
    base_url = os.environ.get("FUSION_BASE_URL")
    user = os.environ.get("FUSION_USER")
    password = os.environ.get("FUSION_PASSWORD")
    if not all([base_url, user, password]):
        raise ValueError(
            "Oracle Fusion credentials (FUSION_BASE_URL, FUSION_USER, FUSION_PASSWORD) are missing in .env"
        )
    auth = HTTPBasicAuth(user, password)
    headers = build_headers("basic", None)
    return base_url, auth, headers


def extract_pdf_text(file_bytes: bytes | None) -> str:
    if not file_bytes:
        return ""
    try:
        with pdfplumber.open(BytesIO(file_bytes)) as pdf:
            pages_text = []
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    pages_text.append(page_text)
            return "\n".join(pages_text)
    except Exception as pdf_err:
        print(f"[ExpenseProcess] pdfplumber failed: {pdf_err}")
        return ""


def build_expense_mapping_messages(file_bytes: bytes | None, doc_extraction) -> list:
    full_text = extract_pdf_text(file_bytes)
    doc_extraction_str = json.dumps(doc_extraction, indent=2)
    content = (
        f"=== FULL DOCUMENT TEXT ===\n{full_text}\n\n"
        f"=== STRUCTURED EXTRACTION (supplementary) ===\n{doc_extraction_str}"
    )
    return [
        SystemMessage(content=map_expense_prompt),
        HumanMessage(content=content),
    ]


def classify_document_as_expense_or_invoice(user_content: str, doc_extraction_str: str) -> str:
    classify_messages = [
        SystemMessage(content=DOCUMENT_CLASSIFIER_PROMPT),
        HumanMessage(
            content=(
                f"User message: {user_content or '(none)'}\n\n"
                f"Document extraction:\n{doc_extraction_str}"
            )
        ),
    ]
    result = model.invoke(classify_messages)
    classification = (result.content or "").strip().lower()
    first_token = classification.split()[0] if classification.split() else classification
    if first_token == "expense" or classification.startswith("expense"):
        return "expense"
    return "invoice"

# -----------------------------
# Prompt Templates
# -----------------------------
excel_summarization_prompt = PromptTemplate.from_template(
    "Summarize the contents of the FBDI Excel file named {file_name} with relevant insights."
)

data_quality_prompt = '''You are a data quality assistant designed to analyze tabular data and provide insights on data quality, with a focus explaining the duplicate records, suggesting corrections, and offering detailed explanations when requested.

Your responsibilities are:

Data Quality Score Articulation:

Clearly articulate the formula used to derive the Data Quality Score, including all relevant components mandatory fields, completeness, uniqueness, validity, consistency).

Duplicate Records Summary:

Summarize all detected duplicate records.

Group duplicates based on relevant fields (e.g., name, ID, email, etc.).

Duplicate Correction Suggestions:

Offer a clear and concise recommendation for resolving duplicates.

Provide automated merging, selection, or deduplication options.

Ensure suggestions preserve data integrity.

Optional Record-Level Explanation:

When the user explicitly requests, provide a detailed explanation of any individual record, including why it was flagged and how it impacts the Data Quality Score.

Ensure that all outputs are user-friendly and easily actionable. Use markdown formatting if appropriate, and maintain a professional, informative tone.'''

fetch_json_prompt = '''You are a helpful assistant designed to modify JSON messages based on the provided user input. 
Accept a valid JSON structure and a set of values to update. 
Carefully modify only the specified fields in the JSON, preserving the original structure.
Ensure the output is well-formatted and valid JSON.

After presenting the updated JSON, provide a short, clear action prompt and explicitly request user approval using the exact phrase: "Provide Your Approval" for the user to submit or apply the updated payload to their ERP system.

Do not explain the process unless explicitly asked and outline the steps emphasizing on the validating the payload against the Purchase Order. 

Steps:
1. Fetch the JSON payload using the exception Info.
2. Based on the exception, invoke the PO API and validate the line quantity.
3. Retrieve the correct quantity and update the JSON payload with the new values.
4. Return the updated JSON payload along with the explicit phrase "Provide Your Approval" to request confirmation before submission or application to the ERP system.

Focus on precision, validity, and readiness for ERP integration. 
Do not explain the changes unless explicitly asked.'''


map_document_prompt = '''You are an intelligent JSON transformation and validation assistant.

When the incoming document is a RECEIPT, you must output a JSON object with EXACTLY two top-level keys in this order: "mapped_json", then "missing_attributes".

1) "mapped_json" must contain the mapped receipt JSON following this exact structure and key sequence (top-to-bottom):

{
    "InvoiceNumber": "<string>",
    "PurchaseOrderNumber": "<string>", // OPTIONAL: Only include if explicitly listed
    "InvoiceCurrency": "<string>",
    "InvoiceAmount": <number>,
    "InvoiceDate": "<YYYY-MM-DD>",
    "DueDate": "<YYYY-MM-DD>", // OPTIONAL: extract when Due Date is on the document (used to resolve PaymentTerms)
    "BusinessUnit": "<string>",
    "Supplier": "<string>",
    "SupplierSite": "<string>",
    "Description": "<string>",
    "PaymentTerms": "<string>", // OPTIONAL: Payment terms e.g., "Immediate", "Net 30", etc.
    "InvoiceType": "<string>",
    "invoiceLines": [
        {
            "LineNumber": <integer>,
            "LineType": "<string>",
            "LineAmount": <number>,
            "Description": "<string>"
        }
        /* additional line objects in ascending LineNumber order */
    ]
}

Rules for "mapped_json":
•⁠  ⁠Always include the top-level keys in the exact sequence shown above. If a value for an optional key is not available, omit the key but keep the ordering of the remaining keys.
•⁠  ⁠"invoiceLines" must be an array. Within it, objects must be ordered by ascending "LineNumber".
•⁠  ⁠Numeric values must be numbers (no strings). Dates must follow YYYY-MM-DD.
•⁠  ⁠Do not include any extra top-level keys outside the specified structure.

CRITICAL INSTRUCTIONS FOR BUSINESS UNIT:
•  Locate the "Billed To" or "Bill To" label on the invoice document.
•  The very first line exactly underneath "Billed To" is the Business Unit name. You MUST extract this exact string character-for-character without truncating any words (e.g., do NOT drop the word "Unit").
•  NEVER map the Supplier Company name as the Business Unit. They must always be different.
•  EXAMPLE OF EXACT EXTRACTION:
     If the document layout reads:
     "Company: Advanced Corp" AND "Billed To: US1 Business Unit"
     YOU MUST OUTPUT:
     "Supplier": "Advanced Corp",
     "BusinessUnit": "US1 Business Unit"

CRITICAL INSTRUCTIONS FOR IDENTIFYING PO:
•⁠  If the invoice has an identifying Purchase Order (PO) number printed on it (e.g., "PO: US164157" or "Purchase Order: 1234"), extract it into the "PurchaseOrderNumber" field at the top level.
•⁠  If there is no PO explicitly listed on the invoice, DO NOT include the "PurchaseOrderNumber" key in your JSON output.

CRITICAL INSTRUCTIONS FOR SUPPLIER:
•⁠  ⁠Do NOT use the value "SupplierS ite", "SupplierSite", or "YOUR COMPANY" as the Supplier name OR the SupplierSite name. These are OCR label artifacts or placeholders.
•⁠  ⁠Extract the actual vendor name and vendor site directly from the document.
•⁠  ⁠For 'SupplierSite', look for the specific branch, region, sub-entity name, or physical location address directly associated with the Supplier on the invoice. Use that exact extracted string.
•⁠  ⁠BEWARE OF OCR ERRORS: If the extracted site code contains "USI" (with the letter 'I'), it is ALWAYS an OCR error for "US1" (with the number '1'). You MUST explicitly correct "USI" to "US1" in your output schema. 
•⁠  ⁠DO NOT invent, guess, or map to internal Oracle site codes (e.g., do NOT generate codes like "AC US1" unless it is explicitly printed on the document). You MUST use ONLY the exact text from the document for the SupplierSite.
•⁠  ⁠If SupplierSite is not printed on the document, OMIT the SupplierSite key entirely — it will be auto-resolved from Oracle using Supplier + BusinessUnit at submission. Never output an empty string "".

CRITICAL INSTRUCTIONS FOR PAYMENT TERMS:
•  Look for labels such as "Payment Terms", "Terms", or "Payment Due" on the invoice document.
•  ALWAYS extract "DueDate" (YYYY-MM-DD) when the document has a Due Date, Payment Due Date, Pay By Date, or similar date field — even if no payment terms text is printed. DueDate alone is enough to resolve Oracle payment terms (e.g. DueDate 30 days after InvoiceDate → Net 30).
•  Cross-reference document_fields for OCI labels DueDate, PaymentTerm, etc. Do not skip DueDate just because PaymentTerms prose is absent.
•  If payment terms are already Oracle-style codes (e.g., "Immediate", "Net 30", "Net 60"), put that exact value in "PaymentTerms".
•  If payment terms are prose (e.g., "Please pay within 30 days"), put the exact printed text in "PaymentTerms" — it will be normalized to an Oracle code automatically.
•  If neither payment terms text nor any due/pay-by date is on the document, omit PaymentTerms and DueDate.

CRITICAL INSTRUCTIONS FOR INVOICE LINES:
•⁠  ⁠OCI extraction sometimes merges multiple line items into a single LINE_ITEM_GROUP. If the document_fields appear merged or quantities/amounts are misaligned, you MUST cross-reference the pages[0].lines array (which contains the raw document text in order) to accurately reconstruct the distinct invoice lines, descriptions, quantities, and amounts.
•⁠  ⁠When reading pages[0].lines for the Items table, note that the text is interleaved horizontally across columns. A single row block follows this exact sequence:
   1. Description (first line)
   2. Rate (e.g. "$55.00 + Tax")
   3. Qty (e.g. "10")
   4. Amount (e.g. "$550.00"). THIS is the total LineAmount. The LineAmount MUST be the total Amount for the item (e.g. for Consulting it should be 1125.00, not 75.00). Do NOT use Rate as LineAmount.
   5. (OPTIONAL) Distribution Combination. If an accounting code exists for the item on the invoice, extract it and ensure it only contains numbers and periods (e.g. replace commas with periods). If none exists on the document, DO NOT invent one.
   6. Description continuation (e.g. "various services.", which wraps to the next line).
   You must combine the Description parts into a single string for each distinct item, and ensure the LineAmount maps exactly to the "Amount" column value (e.g. 550.00, 1125.00, 123.39), NEVER use the Rate.
•⁠  ⁠For each invoice line, the "LineType" must be "Item" or "Freight".
•⁠  ⁠CRITICAL FOR DISTRIBUTIONS: By default, DO NOT include the "invoiceDistributions" key. If NO valid accounting code is explicitly printed on the document for an item, you MUST completely OMIT the "invoiceDistributions" key from that line object entirely. Do NOT output an empty array `[]`. Do NOT invent dummy codes like "1234.5678". If (and ONLY if) a valid code is found printed on the document, you MUST append it exactly like this: `"invoiceDistributions": [{"DistributionLineNumber": 1, "DistributionLineType": "Item", "DistributionAmount": <LineAmount>, "DistributionCombination": "<extracted code>"}]`

2) "missing_attributes" must be an array of strings listing any mandatory attributes that are missing or null/empty in the input. Mandatory attributes are:
•⁠  ⁠InvoiceNumber, InvoiceCurrency, InvoiceAmount, InvoiceDate, Supplier, invoiceLines (must be non-empty). For each invoiceLine: LineNumber, LineType, LineAmount.

Behavior & Output Format Requirements:
•⁠  ⁠Return the valid JSON object followed immediately by the text: "Provide Your Approval".
•⁠  ⁠Do not include any other markdown or commentary, just the JSON and the approval phrase.
•⁠  ⁠The JSON must have exactly two keys in this order: "mapped_json", then "missing_attributes".
•⁠  ⁠If parsing/mapping fails, set "mapped_json" to null and include an explanatory string in "missing_attributes".

Example valid response for a successfully mapped receipt:
{
    "mapped_json": { ... },
    "missing_attributes": []
}
Provide Your Approval

Strictness note: The downstream system extracts the JSON and looks for the phrase "Provide Your Approval" to enable the submission buttons. Ensure both are present.

Now map the input document fields (found under the tool message key "document_fields") to the above schema. 
CRITICAL: You MUST end your response with the phrase "Provide Your Approval" on a new line after the JSON.
'''

batch_excel_prompt = """You are an intelligent data mapping assistant for an ERP system.
The user has provided data extracted from an Excel file in JSON format. Each element in the array represents a separate invoice.
Map each element to the required Oracle AP Invoice JSON structure.
Output a JSON object with EXACTLY two top-level keys in this order: "mapped_json", then "missing_attributes".
The "mapped_json" must be an array of mapped invoice objects. Each object must follow this structure:
{
    "InvoiceNumber": "<string>",
    "PurchaseOrderNumber": "<string>", 
    "InvoiceCurrency": "<string>",
    "InvoiceAmount": <number>,
    "InvoiceDate": "<YYYY-MM-DD>",
    "BusinessUnit": "<string>",
    "Supplier": "<string>",
    "SupplierSite": "<string>",
    "Description": "<string>",
    "PaymentTerms": "<string>", // OPTIONAL: Payment terms e.g., "Immediate", "Net 30", etc.
    "InvoiceType": "Standard",
    "invoiceLines": [
        {
            "LineNumber": 1,
            "LineType": "Item",
            "LineAmount": <number>,
            "Description": "<string>"
            // ONLY include invoiceDistributions if a Distribution/Accounting Combination is provided in the excel row.
            // If provided, format it EXACTLY like this:
            // "invoiceDistributions": [{"DistributionLineNumber": 1, "DistributionLineType": "Item", "DistributionAmount": <LineAmount>, "DistributionCombination": "<provided_combination>"}]
        }
    ]
}

Make sure numeric amounts are numbers (not strings), and dates are YYYY-MM-DD format.
"missing_attributes" must be an array of strings listing any mandatory attributes missing.
Return the valid JSON object followed immediately by the exact text: "Provide Your Approval" on a new line.
"""

text_invoice_prompt = '''You are an intelligent JSON transformation and validation assistant for an ERP system.
If the user asks you to create, generate, or submit an invoice based on provided text details, you MUST extract the details and output a JSON object with EXACTLY two top-level keys in this order: "mapped_json", then "missing_attributes".
The "mapped_json" must contain the mapped invoice JSON following this exact structure and key sequence (top-to-bottom):
{
    "InvoiceNumber": "<string>",
    "PurchaseOrderNumber": "<string>", // OPTIONAL: Only include if explicitly listed
    "InvoiceCurrency": "<string>", // Default to "USD" if not provided
    "InvoiceAmount": <number>,
    "InvoiceDate": "<YYYY-MM-DD>", // Default to today if not provided
    "BusinessUnit": "<string>", // E.g., "US1 Business Unit"
    "Supplier": "<string>",
    "SupplierSite": "<string>", // OPTIONAL: omit if not in the prompt — auto-resolved from Oracle at submission
    "Description": "<string>", // OPTIONAL: omit if not provided
    "PaymentTerms": "<string>", // OPTIONAL: Payment terms e.g., "Immediate", "Net 30", etc.
    "InvoiceType": "<string>", // Default to "Standard"
    "invoiceLines": [
        {
            "LineNumber": <integer>,
            "LineType": "Item",
            "LineAmount": <number>,
            "Description": "<string>"
            // ONLY include invoiceDistributions if a Distribution/Accounting Combination is provided in the prompt.
            // If provided, format it EXACTLY like this:
            // "invoiceDistributions": [{"DistributionLineNumber": 1, "DistributionLineType": "Item", "DistributionAmount": <LineAmount>, "DistributionCombination": "<provided_combination>"}]
        }
    ]
}

Behavior & Output Format Requirements:
• Modifying an Existing Invoice: If the user asks to modify an existing invoice (e.g. "replace description in Line 2 from ABC to XYZ"), locate the most recent JSON payload in the chat history, apply the requested changes perfectly, and output the entirely updated JSON object. Do NOT invent new fields, only change what is requested.
• Copying an Existing Invoice: If the user asks to create a copy (e.g. "create copy of this invoice with suffix 1"), locate the most recent JSON payload, update the InvoiceNumber as requested, and output the new JSON object.
• Return the valid JSON object followed immediately by the exact text: "Provide Your Approval" on a new line.
• Do not include any other markdown or commentary, just the JSON and the approval phrase.
• The JSON must have exactly two keys in this order: "mapped_json", then "missing_attributes".
• "missing_attributes" must be an array of strings listing any mandatory attributes that are missing in the prompt. Mandatory attributes are: InvoiceNumber, InvoiceAmount, Supplier, BusinessUnit. If they are missing, you should guess reasonable defaults (like InvoiceNumber="INV-PROMPT-01", BusinessUnit="US1 Business Unit", etc.) or list them as missing.
• OPTIONAL FIELDS (SupplierSite, Description, PaymentTerms): If not provided in the prompt, OMIT the key entirely. Never output empty strings (""). SupplierSite and PaymentTerms are auto-resolved from Oracle using Supplier + BusinessUnit when you approve the invoice.
• If the user's message is a general question or greeting, just respond normally and do NOT output JSON.
'''

# Expense report mapping prompt
map_expense_prompt = '''You are an intelligent JSON transformation assistant for Oracle Fusion Expense Reports.

Extract expense details from the document and output a JSON object with EXACTLY two top-level keys: "mapped_json", then "missing_attributes".

1) "mapped_json" must include the following fields (include only fields that can be extracted):

{
    "ExpenseType": "<exact Oracle Fusion type label>",
    "Description": "<string>",
    "ReceiptDate": "<YYYY-MM-DD>",
    "ReceiptAmount": <number>,
    "ReimbursableAmount": <number>,
    "MerchantName": "<string>",
    "Location": "<city/country where expense occurred; for trips use 'Origin to Destination'>",
    "BusinessUnit": "<string>"
}

Rules:
- "ExpenseType": MUST be one of these exact Oracle Fusion type labels:
    "Air", "Breakfast", "Car Rental", "Car Rental - Fuel", "Conference",
    "Dinner", "Entertainment", "Entertainment Non Employee Required",
    "Hotel", "Lunch", "Mileage", "Mileage with Commute", "Miscellaneous",
    "Parking", "Per Diem Daily Rate", "Rail & Other Travel", "Supplies",
    "Taxi", "Telephone"
  Mapping guidance:
    - Flight/airline receipt → "Air" (NOT "Airfare" or "Travel")
    - Hotel/lodging/accommodation/Airbnb/VRBO/inn/resort/motel/short stay → "Hotel"
    - Lunch/midday meal → "Lunch"
    - Dinner/evening meal → "Dinner"
    - Breakfast/morning meal → "Breakfast"
    - Generic meal (cannot determine which) → "Lunch"
    - Train/rail/metro → "Rail & Other Travel"
    - Taxi/Uber/Ola/cab → "Taxi"
    - Fuel/petrol/gas → "Car Rental - Fuel"
    - Parking → "Parking"
    - Phone/telecom → "Telephone"
    - Office supplies → "Supplies"
  Do NOT use generic terms like "Travel" or "Meals".
  Do NOT use "Miscellaneous" for lodging, travel, meals, or transportation when a specific category above applies.
  Omit ExpenseType only if not determinable.
- "Description": brief description of the expense (e.g. "Flight for client visit").
- "ReceiptDate": expense date in YYYY-MM-DD format.
- "ReceiptAmount": the GRAND TOTAL of the receipt/expense report (the final total amount, NOT an individual line item amount). If the document shows itemized lines (e.g. Air $590, Service Fee $52.75) and a Total ($642.75), use the Total as ReceiptAmount.
- "ReimbursableAmount": the reimbursement amount. If explicitly stated use that value; otherwise use the same value as ReceiptAmount.
- "MerchantName": vendor/merchant/airline name from the document.
- "Location": the city/country where the expense occurred (e.g. "United States", "New York", "Chicago").
    - For travel that has an origin AND a destination (flights, trains, bus, cab routes, etc.), capture BOTH as "<Origin> to <Destination>", with the DEPARTURE/origin city FIRST — e.g. a flight from Frankfurt to London → "Frankfurt to London". Do NOT collapse a route to a single city and do NOT drop the origin.
    - For a stay/meal/purchase at one place, use that single city/country.
    - Omit only if no location at all can be determined from the document.
- "BusinessUnit": the business unit mentioned in the document (e.g. "US1 Business Unit"). Omit if not present.

2) "missing_attributes" must list any mandatory fields that could not be extracted:
- Mandatory: Description, ReceiptDate, ReceiptAmount, ReimbursableAmount, MerchantName.

Output Format:
- Return the valid JSON object followed immediately by the text: "Provide Your Expense Approval"
- Do not include any other markdown or commentary.

Example:
{
    "mapped_json": {
        "ExpenseType": "Air",
        "Description": "Flight for client visit",
        "ReceiptDate": "2026-01-19",
        "ReceiptAmount": 850.50,
        "ReimbursableAmount": 850.50,
        "MerchantName": "Indigo Airlines",
        "Location": "Chicago to New York",
        "BusinessUnit": "US1 Business Unit"
    },
    "missing_attributes": []
}
Provide Your Expense Approval

CRITICAL: You MUST end your response with the phrase "Provide Your Expense Approval" on a new line after the JSON.
Now extract the expense details from the document fields provided.
'''

# -----------------------------
# Tool Definitions
# -----------------------------
@tool(description="Summarizes the contents of the uploaded FBDI Excel file.")
def summarize_excel(file):
    execution_result = generate_report(file)
    return execution_result

@tool(description="Map the docuemnt extract to JSON object for further processing.")
def document_process(encoded):
    document_result = process_document("", encoded)
    return document_result

@tool(description="Modifies a JSON payload using the fetch_id provided.")
def modify_json(fetch_id: str) -> str:
    print(f"[Debug] inside modify_json: {fetch_id}")

    # Fetch the JSON payload using the fetch_id
    payload = dbmain(fetch_id)
    sourcepayload = {}
    for item in payload or []:
        print(item)
        sourcepayload = item["SOURCEPAYLOAD"]

    json_string = json.dumps(sourcepayload)
    update_qty = 1
    return f"update the Quantity value of json `{json_string}` with this value {update_qty}"

# -----------------------------
# LangGraph Nodes
# -----------------------------
model = ChatOpenAI(model="gpt-4o-mini", temperature=0)


def summarize_excel_node(state: MessagesState):
    msg = state["messages"][-1]
    meta = _msg_metadata(msg)
    file_name = meta.get("file_name", "unknown.xlsx")
    print(file_name)
    file_bytes = meta.get("file_bytes")

    try:
        if not file_bytes:
            raise ValueError("No file bytes in message metadata")
        if isinstance(file_bytes, str):
            selected_file = base64.b64decode(file_bytes)
        elif isinstance(file_bytes, bytes):
            selected_file = file_bytes
        else:
            raise ValueError("Invalid file bytes in message metadata")
        xls = pd.ExcelFile(BytesIO(selected_file), engine='openpyxl')
        result = summarize_excel.invoke({"file": xls})
        tool_message = []

        for section, metrics in result.items():
            print(f"[Debug] return section: {section} and metrics: {metrics}")
            quality = "N/A"
            df = pd.DataFrame()
            if isinstance(metrics, list) and metrics:
                summary = metrics[0]
                quality = summary.get("Quality_Score", "N/A")
                df = summary.get("Duplicate_Record_Details", pd.DataFrame())
            if isinstance(df, pd.DataFrame) and not df.empty:
                df_md = df.to_markdown(index=False)
                summary_md = (
                    f"### 📊 Section: `{section} - **Quality Score**: {quality}\n **Top Duplicates:**\n\n{df_md}"
                )
            else:
                summary_md = (
                    f"### 📊 Section: `{section} - **Quality Score**: {quality}\n"
                )
            tool_message.append(summary_md)

        data_quality_system_prompt = SystemMessage(content=data_quality_prompt)
        data_quality_tool_message = HumanMessage(content="\n\n".join(tool_message))
        messages = [data_quality_system_prompt, data_quality_tool_message]
    except Exception as e:
        summary_text = f"Failed to process Excel file `{file_name}`: {e}"
        print(summary_text)
        messages = [HumanMessage(content=summary_text)]

    return {"messages": messages}


def modify_json_node(state: MessagesState):
    msg = _content_as_str(state["messages"][-1].content)
    fetch_id = msg.split("exception_id=")[-1].split()[0]
    print(f"[Debug] inside modify_json_node processing fetch_id: {fetch_id}")
    result = modify_json.invoke({"fetch_id": fetch_id})
    print(f"[Debug] Result from tool calling: {result}")
    system_prompt = SystemMessage(content=fetch_json_prompt)
    tool_message = HumanMessage(content=result)
    messages = [system_prompt, tool_message]
    return {"messages": messages}


def process_batch_excel_node(state: MessagesState):
    msg = state["messages"][-1]
    meta = _msg_metadata(msg)
    file_name = meta.get("file_name", "unknown.xlsx")
    file_bytes = meta.get("file_bytes")

    try:
        if not file_bytes:
            raise ValueError("No file bytes in message metadata")
        if isinstance(file_bytes, str):
            selected_file = base64.b64decode(file_bytes)
        elif isinstance(file_bytes, bytes):
            selected_file = file_bytes
        else:
            raise ValueError("Invalid file bytes in message metadata")
        df = pd.read_excel(BytesIO(selected_file), engine='openpyxl')

        df = df.where(pd.notnull(df), None)
        records = df.to_dict(orient="records")
        records_json = json.dumps(records, default=str)

        system_prompt = SystemMessage(content=batch_excel_prompt)
        document_info = f"Batch invoices from Excel file `{file_name}`.\n"

        tool_message = HumanMessage(content=document_info + records_json)
        messages = [system_prompt, tool_message]

    except Exception as e:
        summary_text = f"Failed to process batch Excel file `{file_name}`: {e}"
        print(summary_text)
        messages = [HumanMessage(content=summary_text)]
    return {"messages": messages}


def document_process_node(state: MessagesState):
    """Process an uploaded PDF/image: extract, classify (expense vs invoice), and map."""
    msg = state["messages"][-1]
    meta = _msg_metadata(msg)
    file_name = meta.get("file_name", "unknown.pdf")
    print(file_name)
    file_bytes = meta.get("file_bytes")
    user_content = _content_as_str(msg.content) or ""

    try:
        if not file_bytes:
            raise ValueError("No file bytes in message metadata")
        encoded_file = base64.b64encode(file_bytes).decode("utf-8")
        result = document_process.invoke({"encoded": encoded_file})
        doc_type = result["document_type"]
        print(f"Detected doc_type: {doc_type}")

        doc_extraction = result["extraction_result"]
        doc_extraction_str = json.dumps(doc_extraction, indent=2)
        doc_extraction_str = doc_extraction_str.replace("USI", "US1")

        doc_kind = classify_document_as_expense_or_invoice(user_content, doc_extraction_str)
        print(f"[DocumentProcess] LLM classifier result: {doc_kind}")

        if doc_kind == "expense":
            messages = build_expense_mapping_messages(file_bytes, doc_extraction)
        else:
            # Invoice path — store OCR payment hints, then map with the invoice prompt.
            try:
                payment_hints = extract_payment_hints_from_ocr(doc_extraction)
                if payment_hints:
                    ocr_hints_by_file = cl.user_session.get("ocr_payment_hints") or {}
                    ocr_hints_by_file[file_name] = payment_hints
                    cl.user_session.set("ocr_payment_hints", ocr_hints_by_file)
                    print(f"[Debug] Stored OCR payment hints for {file_name}: {payment_hints}")
            except Exception as hint_err:
                print(f"[Warning] Could not store OCR payment hints: {hint_err}")

            document_info = f"The document type is identified as `{doc_type}`.\n"
            system_prompt = SystemMessage(content=map_document_prompt)
            tool_message = HumanMessage(content=document_info + doc_extraction_str)
            messages = [system_prompt, tool_message]

    except Exception as e:
        summary_text = f"Failed to process file `{file_name}`: {e}"
        print(summary_text)
        messages = [HumanMessage(content=summary_text)]
    return {"messages": messages}


def expense_process_node(state: MessagesState):
    """Explicit expense path (triggered when the user message mentions 'expense')."""
    msg = state["messages"][-1]
    meta = _msg_metadata(msg)
    file_name = meta.get("file_name", "unknown.pdf")
    file_bytes = meta.get("file_bytes")

    try:
        full_text = extract_pdf_text(file_bytes)
        print(f"[ExpenseProcess] Full PDF text:\n{full_text}")

        encoded_file = base64.b64encode(file_bytes).decode("utf-8")
        result = document_process.invoke({"encoded": encoded_file})
        doc_extraction = result["extraction_result"]
        messages = build_expense_mapping_messages(file_bytes, doc_extraction)
    except Exception as e:
        summary_text = f"Failed to process expense file `{file_name}`: {e}"
        print(summary_text)
        messages = [HumanMessage(content=summary_text)]
    return {"messages": messages}


def call_model(state: MessagesState):
    print(f"[Debug] inside call_model")
    msgs = state["messages"]
    prev = msgs[-2] if len(msgs) >= 2 else None

    # If a mapping node injected a SystemMessage for this turn (document/expense/
    # excel/modify_json), use that system prompt + the mapped content as-is.
    # Otherwise this is a plain text request → prepend the text invoice prompt so
    # the LLM can handle text-based invoice creation/edits.
    if isinstance(prev, SystemMessage):
        messages_to_send = [prev, msgs[-1]]
    else:
        recent_messages = msgs[-4:] if len(msgs) >= 4 else msgs
        messages_to_send = [SystemMessage(content=text_invoice_prompt)] + recent_messages

    response = model.invoke(messages_to_send)
    print(response)
    return {"messages": [response]}


# This function is only used for routing decisions
def route_condition(state: MessagesState):
    """Determine the next node based on the message content and metadata."""
    msg = state["messages"][-1]
    print(f"[Debug] inside route_condition")

    meta = _msg_metadata(msg)
    if meta:
        print(f"[Debug] metadata info: {meta}")
        file_name = str(meta.get("file_name", ""))
        content_lower = _content_as_str(msg.content).lower()
        if meta.get("is_excel"):
            if "Template" in file_name:
                print("[Debug] routing to summarize excel")
                return "summarize_excel"
            else:
                print("[Debug] routing to process batch excel")
                return "process_batch_excel"
        elif meta.get("is_pdf") and file_name.lower().endswith(".pdf"):
            if "expense" in content_lower:
                print("[Debug] routing to expense_process")
                return "expense_process"
            print("[Debug] routing to document_process")
            return "document_process"
        elif meta.get("is_jpg") and file_name.lower().endswith((".jpg", ".jpeg", ".png")):
            if "expense" in content_lower:
                print("[Debug] routing to expense_process")
                return "expense_process"
            print("[Debug] routing to document_process")
            return "document_process"

    if isinstance(msg.content, str) and "exception_id" in msg.content:
        print(f"[Debug] inside modify_json condition")
        return "modify_json"

    return "call_model"

# -----------------------------
# LangGraph Assembly
# -----------------------------
workflow = StateGraph(state_schema=MessagesState)

workflow.add_node("route", lambda state: state)
workflow.add_node("summarize_excel", summarize_excel_node)
workflow.add_node("modify_json", modify_json_node)
workflow.add_node("process_batch_excel", process_batch_excel_node)
workflow.add_node("document_process", document_process_node)
workflow.add_node("expense_process", expense_process_node)
workflow.add_node("call_model", call_model)

workflow.add_conditional_edges(
    "route",
    route_condition,
    {
        "summarize_excel": "summarize_excel",
        "process_batch_excel": "process_batch_excel",
        "modify_json": "modify_json",
        "document_process": "document_process",
        "expense_process": "expense_process",
        "call_model": "call_model",
    }
)

workflow.add_edge("summarize_excel", "call_model")
workflow.add_edge("process_batch_excel", "call_model")
workflow.add_edge("modify_json", "call_model")
workflow.add_edge("document_process", "call_model")
workflow.add_edge("expense_process", "call_model")
workflow.add_edge("call_model", END)

workflow.set_entry_point("route")

memory = MemorySaver()
app = workflow.compile(checkpointer=memory)

# -----------------------------
# Helper Function for Extracting JSON from Content
# -----------------------------

def extract_json_from_text(text: str):
    """Extract the first JSON object from a string."""
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            print(f"[Debug] Matched for extracting JSON: {match.group(0)}")
            return json.loads(match.group(0))
    except Exception as e:
        print(f"[Debug] Failed to extract JSON: {e}")
    return None


def strip_json_and_approval_prompt(text: str) -> str:
    """Remove JSON payload and approval prompt from streamed message content."""
    text = re.sub(r"\{.*\}", "", text, count=1, flags=re.DOTALL)
    for phrase in ("Provide Your Expense Approval", "Provide Your Approval"):
        text = text.replace(phrase, "")
    return text.strip()

# -----------------------------
# Oracle payment terms helpers (invoice)
# -----------------------------

def fetch_oracle_payment_terms(base_url: str, auth, headers: dict) -> list[str]:
    """Fetch valid Payment Terms LOV values from Oracle Fusion."""
    terms: list[str] = []
    seen: set[str] = set()
    endpoints = [
        "/fscmRestApi/resources/latest/paymentTerms?limit=500",
        "/fscmRestApi/resources/latest/payablesPaymentTerms?limit=500",
        "/fscmRestApi/resources/latest/standardLookups?q=LookupType='AP_TERMS'&limit=500",
        "/fscmRestApi/resources/latest/standardLookups?q=LookupType='PAYMENT TERMS'&limit=500",
    ]
    try:
        for path in endpoints:
            url = base_url.rstrip("/") + path
            resp = requests.get(url, auth=auth, headers=headers, timeout=60)
            if not resp.ok:
                print(f"[Warning] Oracle payment terms lookup {path} returned {resp.status_code}")
                continue
            for item in resp.json().get("items", []):
                for key in ("Name", "PaymentTerms", "Meaning", "LookupCode", "Description"):
                    name = item.get(key)
                    if name and isinstance(name, str):
                        cleaned = name.strip()
                        if cleaned.lower() not in seen:
                            seen.add(cleaned.lower())
                            terms.append(cleaned)
            if terms:
                print(f"[Debug] Loaded Oracle payment terms from {path}")
                break
    except Exception as e:
        print(f"[Warning] Could not fetch Oracle payment terms LOV: {e}")
    return terms


def _parse_supplier_sites(supplier_record: dict) -> list[dict]:
    sites_obj = supplier_record.get("sites")
    if isinstance(sites_obj, dict):
        return sites_obj.get("items", []) or []
    if isinstance(sites_obj, list):
        return sites_obj
    return []


def _site_assignments(site: dict) -> list[dict]:
    assignments = site.get("assignments", [])
    if isinstance(assignments, dict):
        return assignments.get("items", []) or []
    if isinstance(assignments, list):
        return assignments
    return []


def _assignment_matches_bu(assignment: dict, business_unit: str) -> bool:
    if not business_unit:
        return True
    return (
        assignment.get("BillToBU") == business_unit
        or assignment.get("ClientBU") == business_unit
        or assignment.get("ProcurementBU") == business_unit
    )


def _site_payment_terms(site: dict, business_unit: str) -> str | None:
    for assignment in _site_assignments(site):
        if business_unit and not _assignment_matches_bu(assignment, business_unit):
            continue
        assign_term = assignment.get("PaymentTerms") or assignment.get("PayTerms")
        if assign_term:
            return str(assign_term).strip()
    site_term = site.get("PaymentTerms") or site.get("PayTerms")
    return str(site_term).strip() if site_term else None


def fetch_supplier_from_oracle(
    base_url: str,
    supplier_name: str,
    auth,
    headers: dict,
    *,
    timeout: int = 90,
    retries: int = 2,
) -> dict | None:
    if not supplier_name:
        return None
    sup_url = (
        base_url.rstrip("/")
        + f"/fscmRestApi/resources/latest/suppliers?q=Supplier='{supplier_name}'&expand=sites.assignments"
    )
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            sup_resp = requests.get(sup_url, auth=auth, headers=headers, timeout=timeout)
            if not sup_resp.ok:
                print(f"[Warning] Oracle Supplier API returned {sup_resp.status_code}")
                return None
            items = sup_resp.json().get("items", [])
            return items[0] if items else None
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_error = exc
            print(f"[Warning] Supplier API attempt {attempt}/{retries} failed: {exc}")
    if last_error:
        print(f"[Error] Failed to fetch supplier '{supplier_name}': {last_error}")
    return None


def resolve_supplier_site(
    supplier_record: dict | None,
    *,
    business_unit: str,
    requested_site: str = "",
) -> tuple[str | None, str | None]:
    """
    Pick an Oracle supplier site for the invoice.
    Returns (supplier_site_name, default_payment_terms).
    """
    if not supplier_record:
        return None, None

    sup_sites = _parse_supplier_sites(supplier_record)
    if not sup_sites:
        return None, None

    requested_site = (requested_site or "").strip()
    if requested_site:
        for site in sup_sites:
            site_name = (site.get("SupplierSite") or "").strip()
            if site_name.lower() == requested_site.lower():
                return site_name, _site_payment_terms(site, business_unit)

    matched_site: str | None = None
    matched_terms: str | None = None
    for site in sup_sites:
        site_name = (site.get("SupplierSite") or "").strip()
        if not site_name:
            continue
        for assignment in _site_assignments(site):
            if _assignment_matches_bu(assignment, business_unit):
                matched_site = site_name
                matched_terms = _site_payment_terms(site, business_unit)
                break
        if matched_site:
            break

    if matched_site:
        return matched_site, matched_terms

    fallback_site = (sup_sites[0].get("SupplierSite") or "").strip()
    if fallback_site:
        print(f"[Debug] Warning: No BU match for '{business_unit}'. Falling back to SupplierSite: {fallback_site}")
        return fallback_site, _site_payment_terms(sup_sites[0], business_unit)

    return None, None


def fetch_supplier_site_payment_terms(
    base_url: str,
    supplier_name: str,
    supplier_site: str,
    business_unit: str,
    auth,
    headers: dict,
) -> str | None:
    """Return PaymentTerms configured on the matched supplier site, if any."""
    supplier_record = fetch_supplier_from_oracle(base_url, supplier_name, auth, headers)
    _site, payment_terms = resolve_supplier_site(
        supplier_record,
        business_unit=business_unit,
        requested_site=supplier_site,
    )
    return payment_terms


def sanitize_invoice_payload_for_oracle(invoice_payload: dict) -> dict:
    """Remove empty optional strings Oracle rejects (e.g. SupplierSite='')."""
    for key in ("SupplierSite", "Description", "PurchaseOrderNumber"):
        value = invoice_payload.get(key)
        if isinstance(value, str) and not value.strip():
            invoice_payload.pop(key, None)
    return invoice_payload

# -----------------------------
# helper Funciton for Action Plan
# -----------------------------
async def send_approval_actions(json_data: str, file_name: str = ""):
    """Send approve/reject actions in Chainlit for invoices."""
    await cl.Message(
        content=f"Create the invoice for {file_name}?" if file_name else "Create the invoice?",
        actions=[
            cl.Action(
                name="approve_json",
                label=f"✅ Yes {file_name}" if file_name else "✅ Yes",
                payload={"json_data": json_data, "file_name": file_name}
            ),
            cl.Action(
                name="reject_json",
                label=f"❌ Reject {file_name}" if file_name else "❌ Reject",
                payload={"json_data": json_data, "file_name": file_name}
            )
        ]
    ).send()


async def send_expense_approval_actions(json_data: str):
    """Send approve/reject actions in Chainlit for expense reports."""
    await cl.Message(
        content="Create the expense report?",
        actions=[
            cl.Action(
                name="approve_expense_json",
                label="Yes",
                payload={"json_data": json_data}
            ),
            cl.Action(
                name="reject_expense_json",
                label="No",
                payload={"json_data": json_data}
            )
        ]
    ).send()

# -----------------------------
# Expense workflow helpers
# -----------------------------

def run_expense_extraction_sync(
    msg_input: HumanMessage,
    thread_suffix: str,
    session_thread_id: str,
) -> dict | None:
    """Run LangGraph expense extraction without blocking the Chainlit event loop."""
    config = build_run_config(f"{session_thread_id}-{thread_suffix}")
    answer_content = ""
    for msg, _ in app.stream({"messages": [msg_input]}, config, stream_mode="messages"):
        if isinstance(msg, AIMessageChunk):
            answer_content += _content_as_str(msg.content)
            if "Provide Your Expense Approval" in answer_content:
                return extract_json_from_text(answer_content)
    return None


async def stream_expense_extraction(
    msg_input: HumanMessage,
    thread_suffix: str,
    session_thread_id: str,
) -> dict | None:
    return await asyncio.to_thread(
        run_expense_extraction_sync, msg_input, thread_suffix, session_thread_id
    )


async def extract_candidates_from_upload(
    uploaded,
    user_message: str,
    session_thread_id: str,
) -> list[dict]:
    file_name, file_bytes, is_pdf, is_excel, is_jpg = read_uploaded_file(uploaded)
    if not file_bytes or not is_expense_eligible_file(file_name):
        return []

    msg_input = HumanMessage(
        content=user_message or "expense",
        metadata={
            "file_name": file_name,
            "file_bytes": file_bytes,
            "is_excel": is_excel,
            "is_pdf": is_pdf,
            "is_jpg": is_jpg,
        },
    )
    thread_suffix = f"{uuid.uuid4().hex}-{file_name}"
    extracted_json = await stream_expense_extraction(msg_input, thread_suffix, session_thread_id)
    if not extracted_json:
        return []
    return parse_expense_candidates_from_extraction(extracted_json, file_name, file_bytes)


async def send_multi_expense_approval_actions(candidates: list[dict]):
    session_candidates = [{k: v for k, v in c.items() if k != "file_bytes"} for c in candidates]
    cl.user_session.set("pending_expense_candidates", session_candidates)
    lines = [
        f"I found {len(candidates)} expense{'s' if len(candidates) != 1 else ''} from the uploaded files:\n"
    ]
    for index, candidate in enumerate(candidates, start=1):
        lines.append(format_expense_summary_line(index, candidate))
    lines.append("\nWould you like me to create these expenses?")
    await cl.Message(
        content="\n".join(lines),
        actions=[
            cl.Action(name="approve_multi_expense_json", label="Yes", payload={}),
            cl.Action(name="reject_expense_json", label="No", payload={}),
        ],
    ).send()


async def send_receipt_workflow_complete_message(attached_reports: list):
    if attached_reports:
        summary_lines = []
        for entry in attached_reports:
            if isinstance(entry, dict):
                display_name = entry.get("display_name") or "Expense"
                report_id = entry.get("report_id")
                summary_lines.append(f"- **{display_name}** – Report `{report_id}`")
            else:
                summary_lines.append(f"- Report `{entry}`")

        await cl.Message(
            content=(
                "✅ **All receipt attachment steps are complete.**\n\n"
                + "\n".join(summary_lines)
                + f"\n\n🔗 [Open Expense Reports in Oracle Fusion]({FUSION_EXPENSE_URL})"
            )
        ).send()
    else:
        await cl.Message(
            content=(
                "All expense receipt attachment prompts are complete.\n\n"
                f"[Open Expense Reports in Oracle Fusion]({FUSION_EXPENSE_URL})"
            )
        ).send()


async def run_receipt_attachment_workflow(attachment_queue: list[dict]):
    """Sequential receipt prompts using AskActionMessage (no action callbacks)."""
    queue = assign_attachment_queue_display_names(list(attachment_queue))
    attached_reports: list = []

    if not queue:
        await send_receipt_workflow_complete_message(attached_reports)
        return

    base_url, auth, headers = get_fusion_auth_headers()

    for item in queue:
        expense_id = item["expense_id"]
        report_id = item["report_id"]
        display_name = item.get("display_name") or "expense"

        action_response = await cl.AskActionMessage(
            content=f"Would you like to attach a receipt or bill to the **{display_name}**?",
            actions=[
                cl.Action(
                    name=f"attach_receipt_{expense_id}",
                    label="📎 Attach Receipt/Bill",
                    payload={"action": "attach", "expense_id": str(expense_id)},
                ),
                cl.Action(
                    name=f"skip_receipt_{expense_id}",
                    label="Skip",
                    payload={"action": "skip", "expense_id": str(expense_id)},
                ),
            ],
            timeout=3600,
            raise_on_timeout=False,
        ).send()

        action_name = (action_response or {}).get("name", "")
        action_payload = (action_response or {}).get("payload") or {}
        is_skip = (
            not action_response
            or action_payload.get("action") == "skip"
            or str(action_name).startswith("skip_receipt_")
        )

        if is_skip:
            await cl.Message(content=f"Okay, no receipt attached for **{display_name}**.").send()
            continue

        files = await cl.AskFileMessage(
            content=f"Please upload the receipt/bill for **{display_name}** (PDF, JPG, or PNG):",
            accept=["application/pdf", "image/jpeg", "image/png"],
            max_size_mb=10,
            timeout=3600,
            raise_on_timeout=False,
        ).send()

        if not files:
            await cl.Message(content="No file uploaded. Skipping attachment.").send()
            continue

        uploaded = files[0]
        with open(uploaded.path, "rb") as f:
            file_bytes = f.read()
        upload_name = uploaded.name

        try:
            await cl.Message(content=f"Uploading **{upload_name}** to expense report...").send()
            await cl.make_async(upload_expense_attachment)(
                base_url, report_id, expense_id,
                file_bytes, upload_name, auth, headers,
            )
            attached_reports.append({
                "display_name": display_name,
                "report_id": report_id,
            })
            await cl.Message(
                content=(
                    f"Receipt attached successfully!\n\n"
                    f"File: `{upload_name}` attached to **{display_name}** (Report `{report_id}`)."
                )
            ).send()
        except Exception as e:
            error_msg = f"Failed to attach receipt for **{display_name}**\n\nError: {str(e)}"
            print(error_msg)
            await cl.Message(content=error_msg).send()

    await send_receipt_workflow_complete_message(attached_reports)


LOCATION_CHOICE_LIMIT = 40
LOCATION_SELECT_TIMEOUT = 600

# Separators that indicate a route / multiple places in a single extracted value,
# e.g. "Delhi-Mumbai", "Delhi to Mumbai", "Delhi → Mumbai", "Delhi / Mumbai".
# Note: commas are NOT split here (a single place like "Austin, TX" must stay intact;
# search_expense_locations already uses the leading comma-component as the city).
_LOCATION_SPLIT_RE = re.compile(r"\s*(?:->|=>|→|–|—|/|\bto\b|-)\s*", re.IGNORECASE)

# Leading travel words the model may prepend to a route, e.g. "Flight from Delhi to Mumbai".
_LOCATION_PREFIX_RE = re.compile(
    r"^(?:flight|flights|air|airfare|airline|train|rail|bus|cab|taxi|trip|travel|journey|ride|ticket|route)s?\b[\s:–—-]*",
    re.IGNORECASE,
)
_LOCATION_FROM_RE = re.compile(r"^from\s+", re.IGNORECASE)


def split_location_candidates(raw: str) -> list:
    """
    Split a raw extracted location into the distinct places it mentions.

    "Delhi-Mumbai" / "Delhi to Mumbai"     -> ["Delhi", "Mumbai"]
    "Flight from Frankfurt to London"      -> ["Frankfurt", "London"]
    "Austin, TX, United States"            -> ["Austin, TX, United States"]  (single place)
    "" / None                              -> []
    """
    if not raw:
        return []
    cleaned = _LOCATION_PREFIX_RE.sub("", raw.strip())
    seen = set()
    out = []
    for part in _LOCATION_SPLIT_RE.split(cleaned):
        part = _LOCATION_FROM_RE.sub("", part.strip().strip(",").strip()).strip()
        if part and part.lower() not in seen:
            seen.add(part.lower())
            out.append(part)
    return out


async def _ask_which_file_location(candidates: list) -> "str | None":
    """
    When the file lists more than one place (e.g. a flight route), ask the user which
    one should be used as the expense location. Returns the chosen place, or None if
    the user skips / dismisses (location is optional).
    """
    actions = [
        cl.Action(name="select_file_location", label=c, payload={"value": c})
        for c in candidates
    ]
    actions.append(
        cl.Action(
            name="select_file_location",
            label="None / skip location",
            payload={"value": None},
        )
    )
    res = await cl.AskActionMessage(
        content=(
            "This expense file mentions more than one location: "
            + ", ".join(f"**{c}**" for c in candidates)
            + ".\n\nWhich location should be used for this expense?"
        ),
        actions=actions,
        timeout=LOCATION_SELECT_TIMEOUT,
    ).send()
    if not res:
        return None
    return res.get("payload", {}).get("value")


async def resolve_location_with_user(
    expense_input: dict,
    base_url: str,
    auth,
    headers,
) -> dict:
    """
    Human-in-the-loop location resolution. Location is OPTIONAL — if anything is
    missing or unresolved we silently create the expense without a location (no
    noisy messages).

    Flow:
    1. No location in the file            -> proceed silently, no location.
    2. File has several places / a route  -> ask which place to use (pick the
       starting point "A" for a flight), then continue with that one.
    3. Look the chosen place up in the Oracle Fusion expenseLocations LOV and, when
       relevant matches exist, ask the user to pick the exact Oracle location.
    4. No Oracle match / user skips        -> proceed silently, no location.

    Mutates ``expense_input`` in place by setting ``_resolved_location`` so that the
    downstream create step uses exactly what the user picked. Returns that dict.
    """
    none_location = {"LocationId": None, "LocationName": ""}

    raw_location = (expense_input.get("Location") or "").strip()
    if not raw_location:
        # No location in the file — location isn't mandatory, just continue.
        expense_input["_resolved_location"] = none_location
        return none_location

    # A single field value may contain a route ("Delhi-Mumbai") or multiple places.
    candidates = split_location_candidates(raw_location)
    if len(candidates) > 1:
        search_term = await _ask_which_file_location(candidates)
        if not search_term:
            # User skipped — proceed without a location, silently.
            expense_input["_resolved_location"] = none_location
            return none_location
    else:
        search_term = candidates[0] if candidates else raw_location

    matches = await cl.make_async(search_expense_locations)(
        search_term, base_url, auth, headers
    )

    if not matches:
        # No relevant Oracle location — location is optional, so just continue.
        expense_input["_resolved_location"] = none_location
        return none_location

    shown = matches[:LOCATION_CHOICE_LIMIT]
    actions = [
        cl.Action(
            name="select_expense_location",
            label=loc["LocationName"],
            payload={"LocationId": loc["LocationId"], "LocationName": loc["LocationName"]},
        )
        for loc in shown
    ]
    actions.append(
        cl.Action(
            name="select_expense_location",
            label="None of these / skip location",
            payload={"LocationId": None, "LocationName": ""},
        )
    )

    extra = (
        f" (showing first {LOCATION_CHOICE_LIMIT} of {len(matches)})"
        if len(matches) > LOCATION_CHOICE_LIMIT
        else ""
    )
    prompt = (
        f"Found **{len(matches)}** Oracle Fusion location(s) for **{search_term}**{extra}.\n\n"
        f"Please select the exact location for this expense:"
    )

    res = await cl.AskActionMessage(
        content=prompt,
        actions=actions,
        timeout=LOCATION_SELECT_TIMEOUT,
    ).send()

    if not res:
        # Timed out / dismissed → omit location rather than guessing (no message).
        expense_input["_resolved_location"] = none_location
        return none_location

    payload = res.get("payload", {})
    selected = {
        "LocationId": payload.get("LocationId"),
        "LocationName": payload.get("LocationName", ""),
    }
    expense_input["_resolved_location"] = selected
    if selected["LocationId"]:
        expense_input["Location"] = selected["LocationName"]
        await cl.Message(content=f"Location set to **{selected['LocationName']}**.").send()
    else:
        # User explicitly chose "skip" — continue quietly without a location.
        expense_input["_resolved_location"] = none_location
    return expense_input["_resolved_location"]


async def create_expense_and_collect_attachment_info(
    expense_input: dict,
    file_name: str,
    base_url: str,
    auth,
    headers,
) -> dict | None:
    expense_input = dict(expense_input)
    expense_input["ExpenseType"] = infer_expense_type_from_input(expense_input)
    print(f"[ExpenseApproval] Resolved ExpenseType: {expense_input['ExpenseType']}")

    # Human-in-the-loop: confirm the Oracle Fusion expense location before creating,
    # unless one has already been selected (e.g. via a prior selection step).
    if not isinstance(expense_input.get("_resolved_location"), dict):
        await resolve_location_with_user(expense_input, base_url, auth, headers)

    display_input = {k: v for k, v in expense_input.items() if k != "_resolved_location"}
    await cl.Message(
        content=f"**Expense Input:**\n\n```json\n{json.dumps(display_input, indent=2)}\n```"
    ).send()
    await cl.Message(content="Creating expense report in Oracle Fusion...").send()

    created_expense = await cl.make_async(create_expense_report)(
        base_url,
        expense_input,
        auth,
        headers,
    )
    await cl.Message(
        content=f"Expense Report Created!\n\n```json\n{json.dumps(created_expense, indent=2)}\n```"
    ).send()

    report_id = created_expense.get("ExpenseReportId")
    expense_id = created_expense.get("expense_line", {}).get("ExpenseId")
    if not report_id or not expense_id:
        print("[Attachment] Could not find ExpenseId in line response, skipping attachment queue entry")
        return None

    return {
        "report_id": report_id,
        "expense_id": expense_id,
        "display_name": get_expense_display_name(expense_input, file_name),
        "file_name": file_name,
        "expense_input": expense_input,
    }


async def process_multi_file_expenses(uploaded_files: list, user_message: str):
    expense_files = [f for f in uploaded_files if is_expense_eligible_file(f.name)]
    total_files = len(expense_files)
    if total_files == 0:
        await cl.Message(content="No supported expense files were uploaded.").send()
        return

    session_thread_id = cl.context.session.thread_id
    progress_msg = cl.Message(
        content=(
            f"Analyzing **{total_files}** uploaded file{'s' if total_files != 1 else ''}. "
            f"This may take several minutes — please keep this tab open.\n\n"
            f"Progress: 0/{total_files} complete"
        )
    )
    await progress_msg.send()

    all_candidates: list[dict] = []
    processed_files = 0

    for index, uploaded in enumerate(expense_files, start=1):
        progress_msg.content = (
            f"Analyzing **{total_files}** uploaded files. Please keep this tab open.\n\n"
            f"Progress: {index}/{total_files} — processing `{uploaded.name}`..."
        )
        await progress_msg.update()
        try:
            candidates = await extract_candidates_from_upload(
                uploaded, user_message, session_thread_id
            )
            processed_files += 1
            if candidates:
                all_candidates.extend(candidates)
                progress_msg.content = (
                    f"Analyzing **{total_files}** uploaded files. Please keep this tab open.\n\n"
                    f"Progress: {index}/{total_files} complete — found expense in `{uploaded.name}`."
                )
            else:
                progress_msg.content = (
                    f"Analyzing **{total_files}** uploaded files. Please keep this tab open.\n\n"
                    f"Progress: {index}/{total_files} complete — no expense in `{uploaded.name}`."
                )
            await progress_msg.update()
        except Exception as e:
            print(f"[MultiExpense] Failed to process {uploaded.name}: {e}")
            await cl.Message(
                content=f"Failed to process `{uploaded.name}`: {e}"
            ).send()

    progress_msg.content = (
        f"Finished analyzing **{total_files}** file{'s' if total_files != 1 else ''}. "
        f"Found **{len(all_candidates)}** expense candidate{'s' if len(all_candidates) != 1 else ''}."
    )
    await progress_msg.update()

    if processed_files < total_files:
        await cl.Message(
            content="Not all uploaded files were processed. Additional uploaded files must be processed before continuing."
        ).send()
        return

    if not all_candidates:
        await cl.Message(content="No valid expenses were found in the uploaded files.").send()
        return

    all_candidates = assign_expense_display_names(all_candidates)
    await send_multi_expense_approval_actions(all_candidates)

# -----------------------------
# Chainlit Setup
# -----------------------------

@cl.set_chat_profiles
async def chat_profile(current_user: cl.User | None = None):
    return [
        cl.ChatProfile(
            name=" SEMANTIC AGENT",
            markdown_description="The underlying LLM model is Finetuned Llama.",
            icon="https://picsum.photos/200",
        ),
        cl.ChatProfile(
            name="GPT-4",
            markdown_description="The underlying LLM model is **GPT-4**.",
            icon="https://picsum.photos/250",
        ),
        cl.ChatProfile(
            name="FINANCE AGENT",
            markdown_description="The underlying LLM model is **Cohere Command**.",
            icon="https://picsum.photos/260",
        ),
        cl.ChatProfile(
            name="HCM AGENT",
            markdown_description="The underlying LLM model is **Anthropic Claude Sonnet4**.",
            icon="https://picsum.photos/280",
        ),
    ]

@cl.on_chat_start
async def start(input_data: dict | None = None):
    fetch_id = "anonymous"
    try:
        url = input_data.get("url", "") if input_data else ""
        print(f"[Debug] input_data url: {url}")
        if "fetch_id=" in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            fetch_id = params.get("fetch_id", ["anonymous"])[0]
    except Exception as e:
        print(f"Error parsing URL: {e}")

    cl.user_session.set("fetch_id", fetch_id)
    print(f"[Session] fetch_id: {fetch_id}")
    await cl.Message(content=f"Welcome! Admin:").send()

@cl.on_chat_resume
async def on_chat_resume(thread):
    pass

@cl.password_auth_callback
async def auth_callback(username: str, password: str):
    if (username, password) == ("admin", "admin"):
        return cl.User(identifier="admin", metadata={"role": "admin", "provider": "credentials"})
    return None


async def _stream_and_send_approvals(answer: cl.Message, msg_input: HumanMessage, config: RunnableConfig, file_name: str = ""):
    """Stream the graph, render text, and raise the right approval buttons
    (expense vs invoice) based on the approval phrase the LLM emits."""
    approval_sent = False
    async for msg, _ in app.astream({"messages": [msg_input]}, config, stream_mode="messages"):
        if isinstance(msg, AIMessageChunk):
            if approval_sent:
                continue
            answer.content += _content_as_str(msg.content)
            await answer.update()

            if "Provide Your Expense Approval" in answer.content:
                extracted_json = extract_json_from_text(answer.content)
                if extracted_json:
                    answer.content = strip_json_and_approval_prompt(answer.content)
                    await answer.update()
                    await send_expense_approval_actions(json.dumps(extracted_json, indent=2))
                    approval_sent = True
            elif "Provide Your Approval" in answer.content:
                extracted_json = extract_json_from_text(answer.content)
                if extracted_json:
                    await send_approval_actions(json.dumps(extracted_json, indent=2), file_name)
                    approval_sent = True
        elif isinstance(msg, ToolMessage):
            await cl.Message(content=_content_as_str(msg.content)).send()


@cl.on_message
async def main(message: cl.Message):
    user_message = message.content or ""
    is_expense_intent = "expense" in user_message.lower()

    # Multi-file expense fast path: extract candidates, then batch-approve.
    if message.elements:
        expense_files = [el for el in message.elements if is_expense_eligible_file(el.name)]
        if is_expense_intent and len(expense_files) > 1:
            await cl.Message(
                content=f"Received **{len(expense_files)}** files for expense processing."
            ).send()
            await process_multi_file_expenses(expense_files, user_message)
            return

    uploaded_files: dict[str, bytes] = cl.user_session.get("uploaded_files") or {}
    base_thread_id = cl.context.session.thread_id

    if not message.elements:
        # No files attached, process as a standard message
        answer = cl.Message(content="")
        await answer.send()
        config = build_run_config(base_thread_id)
        msg_input = HumanMessage(content=message.content)
        await _stream_and_send_approvals(answer, msg_input, config)
        return

    # Helper function to process a single file concurrently
    async def process_single_file(uploaded):
        answer = cl.Message(content="")
        await answer.send()

        file_name = uploaded.name
        file_bytes = None
        file_key = 'Template'
        msgs = message.content or "FBDI Excel file uploaded"

        parts = file_name.rsplit('.', 1)
        extension = parts[1] if len(parts) == 2 else ""

        is_pdf = file_name.lower().endswith(".pdf")
        is_excel = file_name.lower().endswith((".xls", ".xlsx", ".xlsm"))
        is_jpg = file_name.lower().endswith((".jpg", ".jpeg", ".png"))

        if file_key in file_name:
            answer.content = f"Processing FBDI file: {file_name}...\n"
            file_path = uploaded.path if hasattr(uploaded, 'path') else None
            if file_path is not None:
                with open(file_path, "rb") as f:
                    file_bytes = f.read()
        elif is_excel and file_key not in file_name:
            answer.content = f"Processing Batch Invoice Excel file: {file_name}...\n"
            file_path = uploaded.path if hasattr(uploaded, 'path') else None
            if file_path is not None:
                with open(file_path, "rb") as f:
                    file_bytes = f.read()
        elif is_pdf or is_jpg:
            answer.content = f"Processing {extension} file: {file_name}...\n"
            file_path = uploaded.path if hasattr(uploaded, 'path') else None
            if file_path is not None:
                with open(file_path, "rb") as f:
                    file_bytes = f.read()
            # Track the file name so the expense attachment workflow can reference it.
            cl.user_session.set("uploaded_file_name", file_name)

        msg_input = HumanMessage(
            content=msgs,
            metadata={
                "file_name": file_name,
                "file_bytes": file_bytes,
                "is_excel": is_excel,
                "is_pdf": is_pdf,
                "is_jpg": is_jpg
            }
        )

        if file_name and file_bytes:
            uploaded_files[file_name] = file_bytes
            cl.user_session.set("uploaded_files", uploaded_files)

        # Unique thread ID per file to prevent state corruption
        config = build_run_config(f"{base_thread_id}_{file_name}")
        await _stream_and_send_approvals(answer, msg_input, config, file_name)

    # Process all uploaded elements concurrently
    tasks = [process_single_file(element) for element in message.elements]
    await asyncio.gather(*tasks)

# -----------------------------
# Chainlit Action Handlers — Invoices
# -----------------------------
@cl.action_callback("approve_json")
async def on_approve(action: cl.Action):
    json_data = action.payload.get("json_data")
    file_name = action.payload.get("file_name", "")
    print(f"[Debug] Approved JSON data for {file_name}: {json_data}")

    # Notify user we are starting
    await cl.Message(content=f"✅ Approved. Initiating invoice creation in Oracle Fusion for {file_name}...").send()

    try:
        # Load credentials
        base_url = os.environ.get("FUSION_BASE_URL")
        user = os.environ.get("FUSION_USER")
        password = os.environ.get("FUSION_PASSWORD")

        if not base_url or not user or not password:
            # Check if placeholders are still there or empty
            raise ValueError("Oracle Fusion credentials (FUSION_BASE_URL, FUSION_USER, FUSION_PASSWORD) are missing in .env")

        # Basic Auth
        auth = HTTPBasicAuth(user, password)
        headers = build_headers("basic", None)

        # Parse payload
        if not json_data:
            raise ValueError("No JSON data provided for invoice creation")
        full_payload = json.loads(json_data)

        # Extract the actual invoice payload.
        # The agent returns { "mapped_json": { ... }, "missing_attributes": ... }
        # We need to send just the value of "mapped_json".
        if "mapped_json" in full_payload:
            invoice_payloads = full_payload["mapped_json"]
        else:
            invoice_payloads = full_payload

        if not isinstance(invoice_payloads, list):
            invoice_payloads = [invoice_payloads]

        oracle_payment_terms = fetch_oracle_payment_terms(base_url, auth, headers)
        if oracle_payment_terms:
            print(f"[Debug] Loaded {len(oracle_payment_terms)} Oracle payment terms from LOV")

        for invoice_payload in invoice_payloads:
            supplier_name = invoice_payload.get("Supplier")
            business_unit = invoice_payload.get("BusinessUnit")
            requested_site = (invoice_payload.get("SupplierSite") or "").strip()
            supplier_site_term = None

            supplier_record = None
            if supplier_name:
                supplier_record = fetch_supplier_from_oracle(base_url, supplier_name, auth, headers)

            if supplier_name and not requested_site:
                resolved_site, supplier_site_term = resolve_supplier_site(
                    supplier_record,
                    business_unit=business_unit or "",
                )
                if resolved_site:
                    print(f"[Debug] Auto-resolved SupplierSite: {resolved_site}")
                    invoice_payload["SupplierSite"] = resolved_site
                else:
                    print(f"[Debug] Warning: Could not resolve SupplierSite for '{supplier_name}' / '{business_unit}'")
            else:
                _, supplier_site_term = resolve_supplier_site(
                    supplier_record,
                    business_unit=business_unit or "",
                    requested_site=requested_site,
                )

            if supplier_name and not (invoice_payload.get("SupplierSite") or "").strip():
                raise ValueError(
                    f"SupplierSite is required but could not be resolved for Supplier '{supplier_name}' "
                    f"and Business Unit '{business_unit}'. Verify the supplier exists in Oracle and has a "
                    f"site assigned to that business unit, then retry."
                )

            if supplier_site_term:
                print(f"[Debug] Supplier site default PaymentTerms: {supplier_site_term}")

            # =========================================================================
            # DYNAMIC PO LINE MATCHING (Intercept before API call)
            # =========================================================================
            po_num = invoice_payload.get("PurchaseOrderNumber")
            inv_lines = invoice_payload.get("invoiceLines", [])

            print(f"[Deep Debug] invoice_payload type: {type(invoice_payload)}")
            print(f"[Deep Debug] po_num = {repr(po_num)}, type = {type(po_num)}")
            print(f"[Deep Debug] inv_lines len = {len(inv_lines) if isinstance(inv_lines, list) else type(inv_lines)}")
            print(f"[Deep Debug] evaluating condition: {bool(po_num and inv_lines)}")

            if po_num and inv_lines:
                try:
                    print(f"[Debug] Fetching actual lines for PO {po_num} from Oracle...")
                    po_url = base_url.rstrip("/") + f"/fscmRestApi/resources/latest/purchaseOrders?q=OrderNumber='{po_num}'&expand=lines"
                    po_resp = requests.get(po_url, auth=auth, headers=headers, timeout=60)

                    if po_resp.ok:
                        po_data = po_resp.json()
                        po_items = po_data.get("items", [])

                        if po_items:
                            po_header = po_items[0]

                            # Handle both Oracle API schema variations for expanded child collections
                            lines_obj = po_header.get("lines", [])
                            if isinstance(lines_obj, dict):
                                po_lines = lines_obj.get("items", [])
                            elif isinstance(lines_obj, list):
                                po_lines = lines_obj
                            else:
                                po_lines = []

                            print(f"[Debug] Successfully retrieved {len(po_lines)} lines for PO {po_num}.")

                            # Now iterate over invoice lines to find the matching PO line
                            for inv_line in inv_lines:
                                inv_amt = float(inv_line.get("LineAmount", 0))
                                inv_desc = str(inv_line.get("Description", "")).lower().strip()
                                best_match_line = None

                                for pl in po_lines:
                                    # Get PO Amount (for amount-based lines) or Quantity * Price (for quantity-based)
                                    po_amt = pl.get("LineAmount", pl.get("OrderedAmount"))
                                    if po_amt is None:
                                        qty = pl.get("Quantity")
                                        price = pl.get("Price")
                                        if qty is not None and price is not None:
                                            po_amt = float(qty) * float(price)
                                        else:
                                            po_amt = 0
                                    else:
                                        po_amt = float(po_amt)

                                    po_desc = str(pl.get("ItemDescription", "")).lower().strip()

                                    # Match priority 1: Exact amount
                                    if inv_amt > 0 and abs(inv_amt - po_amt) < 0.01:
                                        best_match_line = pl
                                        break
                                    # Match priority 2: Description string match
                                    elif inv_desc and po_desc and (inv_desc in po_desc or po_desc in inv_desc):
                                        best_match_line = pl

                                if best_match_line:
                                    matched_po_line = best_match_line.get("LineNumber")
                                    inv_line["PurchaseOrderNumber"] = po_num  # Oracle requires PO Number at the line level too
                                    inv_line["PurchaseOrderLineNumber"] = matched_po_line  # Must be cast to int maybe? JSON preserves type
                                    inv_line["PurchaseOrderScheduleLineNumber"] = 1  # Force schedule based on PO constraint

                                    # Oracle AP-811013 / AP-810787: Quantity and UnitPrice are REQUIRED for quantity-based PO lines
                                    po_price = best_match_line.get("Price")
                                    if po_price is not None and float(po_price) > 0:
                                        inv_line["UnitPrice"] = float(po_price)
                                        # Derive invoice quantity from the exact invoice amount and PO price
                                        inv_line["Quantity"] = round(inv_amt / float(po_price), 5)

                                    print(f"[Debug] Auto-Matched Invoice Item '{inv_line.get('Description')}' -> Oracle PO Line {matched_po_line}")
                                else:
                                    print(f"[Debug] Warning: Could not match Invoice Item '{inv_line.get('Description')}' to any PO Line.")
                        else:
                            print(f"[Debug] PO {po_num} not found natively via purchaseOrders API query.")
                    else:
                        print(f"[Warning] Oracle PO API returned {po_resp.status_code}")

                except Exception as e:
                    print(f"[Error] Failed during dynamic PO matching: {e}")
            # =========================================================================

            ocr_hints = (cl.user_session.get("ocr_payment_hints") or {}).get(file_name)
            if ocr_hints:
                applied_hints = enrich_invoice_payment_hints(invoice_payload, ocr_hints)
                if applied_hints:
                    print(f"[Debug] Enriched invoice from OCR hints: {applied_hints}")

            invoice_payload, _ = normalize_invoice_payload(
                invoice_payload,
                oracle_terms=oracle_payment_terms or None,
                supplier_site_term=supplier_site_term,
            )
            invoice_payload = sanitize_invoice_payload_for_oracle(invoice_payload)
            resolved_term = invoice_payload.get("PaymentTerms")
            if resolved_term:
                print(f"[Debug] Resolved PaymentTerms for Oracle: {resolved_term}")
            print(f"[Debug] Sending Invoice Payload: {json.dumps(invoice_payload, indent=2)}")

            # Create Invoice
            # Run in thread to avoid blocking async loop
            created_invoice = await cl.make_async(create_invoice)(base_url, invoice_payload, auth, headers)

            # Success message
            await cl.Message(
                content=f"""🎉 **Invoice Created Successfully!**

[View in Oracle Fusion Studio]({base_url})

```json
{json.dumps(created_invoice, indent=2)}
```"""
            ).send()

            # Generate Distributions
            try:
                # Check if distributions were extracted from the document or if it's tied to a PO
                has_distributions = any(
                    line.get("invoiceDistributions") and len(line["invoiceDistributions"]) > 0
                    for line in invoice_payload.get("invoiceLines", [])
                )
                has_po = bool(invoice_payload.get("PurchaseOrderNumber"))

                # Oracle action generateDistributions is an item-level action
                uniq_id = created_invoice.get("InvoiceId", created_invoice.get("invoicesUniqID"))

                if uniq_id:
                    gd_resp = await cl.make_async(post_action)(base_url, "generateDistributions", {}, auth, headers, invoice_id=uniq_id)

                    # Wait for Oracle's background distribution generation process to commit (async delay)
                    check_url = f"{base_url}/fscmRestApi/resources/latest/invoices/{uniq_id}/child/invoiceLines"
                    check_headers = {k: v for k, v in headers.items() if k.lower() != 'content-type'}

                    has_any_generated_distributions = False
                    for _ in range(5):
                        await asyncio.sleep(2)
                        check_resp = await cl.make_async(requests.get)(check_url, auth=auth, headers=check_headers)
                        if check_resp.status_code == 200:
                            for line in check_resp.json().get("items", []):
                                if line.get("DistributionCombination"):
                                    has_any_generated_distributions = True
                                    break
                        if has_any_generated_distributions:
                            break
                else:
                    print(f"⚠️ **Could not find Invoice identifier to generate distributions.**")
            except Exception as e:
                print(f"⚠️ **Invoice created, but failed to generate distributions:**\n{str(e)}")

            # Calculate Tax
            try:
                uniq_id = created_invoice.get("InvoiceId", created_invoice.get("invoicesUniqID"))
                if uniq_id:
                    print("[Debug] Calling calculateTax action...")
                    tax_resp = await cl.make_async(post_action)(base_url, "calculateTax", {}, auth, headers, invoice_id=uniq_id)
                    print(f"[Debug] Calculate Tax Response: {json.dumps(tax_resp, indent=2)}")

                    # Fetch invoice lines to calculate the sum of tax lines and update InvoiceAmount
                    print("[Debug] Fetching invoice lines to check tax amount...")
                    lines_url = f"{base_url.rstrip('/')}/fscmRestApi/resources/latest/invoices/{uniq_id}/child/invoiceLines"
                    lines_resp = requests.get(lines_url, auth=auth, headers=headers)
                    if lines_resp.ok:
                        lines_data = lines_resp.json().get("items", [])
                        tax_amount = sum(float(l.get("LineAmount", 0)) for l in lines_data if l.get("LineType") == "Tax")
                        if tax_amount > 0:
                            get_url = f"{base_url.rstrip('/')}/fscmRestApi/resources/latest/invoices/{uniq_id}"
                            get_resp = requests.get(get_url, auth=auth, headers=headers)
                            if get_resp.ok:
                                inv_data = get_resp.json()
                                original_amount = inv_data.get("InvoiceAmount", 0)
                                new_amount = float(original_amount) + tax_amount
                                print(f"[Debug] Updating InvoiceAmount from {original_amount} to {new_amount} to include tax.")

                                patch_payload = {"InvoiceAmount": new_amount}
                                patch_headers = headers.copy()
                                patch_headers["Content-Type"] = "application/json"

                                patch_resp = requests.patch(get_url, json=patch_payload, auth=auth, headers=patch_headers)
                                if patch_resp.ok:
                                    print(f"[Debug] Successfully updated InvoiceAmount to {new_amount}.")
                                else:
                                    print(f"[Debug] Failed to update InvoiceAmount: {patch_resp.text}")
            except Exception as e:
                print(f"⚠️ **Invoice created, but failed to calculate tax:**\n{str(e)}")

            # Validate Invoice
            try:
                print("[Debug] Calling validateInvoice action...")
                invoice_num = invoice_payload.get("InvoiceNumber")
                if invoice_num:
                    validate_payload = {
                        "name": "validateInvoice",
                        "parameters": [
                            {"InvoiceNumber": invoice_num},
                            {"ProcessAction": "Validate"}
                        ]
                    }
                    validate_url = f"{base_url.rstrip('/')}/fscmRestApi/resources/latest/invoices"
                    val_headers = headers.copy()
                    val_headers["Content-Type"] = "application/vnd.oracle.adf.action+json"

                    val_resp = await cl.make_async(requests.post)(validate_url, json=validate_payload, auth=auth, headers=val_headers)
                    if val_resp.ok:
                        print(f"[Debug] Validate Response: {val_resp.text}")
                        # Check actual validation status
                        await asyncio.sleep(2)
                        uniq_id = created_invoice.get("InvoiceId", created_invoice.get("invoicesUniqID"))
                        get_inv_url = f"{base_url.rstrip('/')}/fscmRestApi/resources/latest/invoices/{uniq_id}"
                        get_headers = {k: v for k, v in headers.items() if k.lower() != 'content-type'}
                        check_resp = await cl.make_async(requests.get)(get_inv_url, auth=auth, headers=get_headers)

                        if check_resp.ok:
                            val_status = check_resp.json().get("ValidationStatus", "Unknown")
                            if val_status == "Validated":
                                await cl.Message(content=f"✅ **Invoice Validated Successfully!**").send()
                            else:
                                # Fetch specific hold reasons
                                holds_url = f"{base_url.rstrip('/')}/fscmRestApi/resources/latest/invoiceHolds?q=InvoiceNumber='{invoice_num}'"
                                holds_resp = await cl.make_async(requests.get)(holds_url, auth=auth, headers=get_headers)
                                hold_reasons = []
                                if holds_resp.ok:
                                    for h in holds_resp.json().get("items", []):
                                        if h.get("ReleaseReason") is None:  # Active hold
                                            hold_reasons.append(h.get("HoldReason", "Unknown System Hold"))

                                # Dedup reasons (often repeats per line)
                                hold_reasons = list(set(hold_reasons))

                                if hold_reasons:
                                    reasons_str = "\n- " + "\n- ".join(hold_reasons)
                                    await cl.Message(content=f"⚠️ **Validation Incomplete:** The invoice hit a system hold during validation (Status: `{val_status}`).\n\n**Active Holds:**{reasons_str}\n\nPlease check the Manage Holds page in Oracle Fusion to release it.").send()
                                else:
                                    await cl.Message(content=f"⚠️ **Validation Incomplete:** The invoice hit a system hold during validation (Status: `{val_status}`). Please check the Manage Holds page in Oracle Fusion to release it.").send()
                        else:
                            await cl.Message(content=f"✅ **Invoice Validation Action Completed!** (Could not verify final status)").send()
                    else:
                        error_msg = val_resp.json().get('detail', val_resp.text)
                        print(f"[Debug] Failed to validate invoice: {error_msg}")
                        await cl.Message(content=f"❌ **Invoice Validation Failed:**\n{error_msg}").send()
            except Exception as e:
                print(f"⚠️ **Failed to validate invoice:**\n{str(e)}")
                await cl.Message(content=f"❌ **Invoice Validation Exception:**\n{str(e)}").send()

            # Check for attachment capability (only for single invoices to prevent prompt spam)
            if len(invoice_payloads) == 1:
                invoice_href = get_invoice_self_href(created_invoice, base_url)
                if invoice_href:
                    await cl.Message(
                        content=f"Would you like to upload a document as an attachment to {file_name}?" if file_name else "Would you like to upload a document as an attachment to this invoice?",
                        actions=[
                            cl.Action(
                                name="upload_attachment_action",
                                label="📎 Upload Attachment",
                                payload={"invoice_href": invoice_href}
                            ),
                            cl.Action(
                                name="skip_attachment_action",
                                label="⏭️ Skip",
                                payload={"action": "skip"}
                            )
                        ]
                    ).send()

        # After processing all invoices in the batch
        if len(invoice_payloads) > 1:
            await cl.Message(content=f"✅ **Batch Processing Complete!** Successfully processed {len(invoice_payloads)} invoices from the Excel file.").send()

    except Exception as e:
        error_msg = f"❌ **Failed to create invoice**\n\nError: {str(e)}"
        print(error_msg)
        await cl.Message(content=error_msg).send()


@cl.action_callback("reject_json")
async def on_reject(action: cl.Action):
    json_data = action.payload.get("json_data")
    print(f"[Debug] Rejected JSON data: {json_data}")
    await cl.Message(
        content=f"❌ Rejected the change for JSON:\n```json\n{json_data}\n```"
    ).send()


@cl.action_callback("upload_attachment_action")
async def on_upload_attachment(action: cl.Action):
    payload = action.payload or {}
    invoice_href = payload.get("invoice_href")

    if not invoice_href:
        await cl.Message(content="❌ Could not determine invoice URL.").send()
        return

    files = None

    # Wait for the user to upload a file
    while files == None:
        files = await cl.AskFileMessage(
            content="Please upload the document(s) you would like to attach (e.g., pdf, png, jpg, docx, etc.) You can select multiple.",
            accept=["application/pdf", "image/jpeg", "image/png", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
            max_size_mb=20,
            max_files=10,
            timeout=180,
        ).send()

    if files:
        base_url = os.environ.get("FUSION_BASE_URL")
        user = os.environ.get("FUSION_USER")
        password = os.environ.get("FUSION_PASSWORD")
        if not base_url or not user or not password:
            await cl.Message(content="❌ Oracle Fusion credentials are missing in .env").send()
            return
        auth = HTTPBasicAuth(user, password)
        headers = build_headers("basic", None)

        for file in files:
            file_name = file.name

            with open(file.path, "rb") as f:
                file_bytes = f.read()

            if not file_bytes:
                await cl.Message(content=f"❌ Document `{file_name}` could not be read.").send()
                continue

            await cl.Message(content=f"Uploading `{file_name}` as an attachment...").send()

            try:
                b64 = base64.b64encode(file_bytes).decode("ascii")
                attachment_payload = {
                    "FileContents": b64,
                    "FileName": file_name,
                    "Description": "Invoice attachment uploaded via agent"
                }

                uploaded = await cl.make_async(upload_attachment)(base_url, invoice_href, attachment_payload, auth, headers)
                await cl.Message(content=f"✅ **Attachment `{file_name}` Uploaded Successfully!**").send()
            except Exception as e:
                await cl.Message(content=f"❌ **Failed to upload attachment `{file_name}`**\n\nError: {str(e)}").send()


@cl.action_callback("skip_attachment_action")
async def on_skip_attachment(action: cl.Action):
    await cl.Message(content="Skipped attachment upload.").send()


# -----------------------------
# Chainlit Action Handlers — Expenses
# -----------------------------
@cl.action_callback("approve_expense_json")
async def on_approve_expense(action: cl.Action):
    json_data = action.payload.get("json_data")
    print(f"[Debug] Approved expense JSON: {json_data}")

    try:
        base_url, auth, headers = get_fusion_auth_headers()
        full_payload = json.loads(json_data)
        expense_input = full_payload.get("mapped_json", full_payload)
        file_name = cl.user_session.get("uploaded_file_name") or "receipt"

        created = await create_expense_and_collect_attachment_info(
            expense_input, file_name, base_url, auth, headers
        )
        if created:
            await run_receipt_attachment_workflow([created])

    except Exception as e:
        error_msg = f"Failed to create expense report\n\nError: {str(e)}"
        print(error_msg)
        await cl.Message(content=error_msg).send()


@cl.action_callback("approve_multi_expense_json")
async def on_approve_multi_expense(action: cl.Action):
    candidates = cl.user_session.get("pending_expense_candidates") or []
    if not candidates:
        await cl.Message(content="No pending expenses to create.").send()
        return

    try:
        base_url, auth, headers = get_fusion_auth_headers()
        attachment_queue = []

        for index, candidate in enumerate(candidates, start=1):
            try:
                await cl.Message(
                    content=f"Creating expense {index}/{len(candidates)}: **{candidate['display_name']}**"
                ).send()
                created = await create_expense_and_collect_attachment_info(
                    candidate["expense_input"],
                    candidate["file_name"],
                    base_url,
                    auth,
                    headers,
                )
                if created:
                    attachment_queue.append(created)
            except Exception as e:
                error_msg = f"Failed to create **{candidate.get('display_name', 'expense')}**: {str(e)}"
                print(error_msg)
                await cl.Message(content=error_msg).send()

        cl.user_session.set("pending_expense_candidates", [])
        if attachment_queue:
            await run_receipt_attachment_workflow(attachment_queue)
        else:
            await cl.Message(content="No expense reports were available for receipt attachment.").send()

    except Exception as e:
        error_msg = f"Failed to create expense reports\n\nError: {str(e)}"
        print(error_msg)
        await cl.Message(content=error_msg).send()


@cl.action_callback("reject_expense_json")
async def on_reject_expense(action: cl.Action):
    json_data = action.payload.get("json_data")
    cl.user_session.set("pending_expense_candidates", [])
    if json_data:
        print(f"[Debug] Rejected expense JSON: {json_data}")
        await cl.Message(
            content=f"Expense report rejected.\n```json\n{json_data}\n```"
        ).send()
    else:
        await cl.Message(content="Expense creation cancelled.").send()





