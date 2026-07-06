# NTTDATA Semantic Agent

A conversational AI agent for **Oracle Fusion** finance operations, built with
[Chainlit](https://chainlit.io), [LangGraph](https://langchain-ai.github.io/langgraph/)
and [OCI Document AI](https://www.oracle.com/artificial-intelligence/document-understanding/).

Upload a document (PDF / image / Excel) or type a request in natural language, and the
agent classifies it, extracts the relevant data, lets you review and approve it, and then
creates the corresponding record directly in Oracle Fusion.

---

## Features

- **AP Invoice creation** — classifies and extracts invoice data, matches purchase orders,
  resolves suppliers / sites / payment terms, generates distributions, calculates tax,
  validates the invoice, and uploads attachments to Oracle Fusion.
- **Expense report creation** — extracts expense details from receipts/tickets, infers the
  correct Oracle expense type (e.g. *Air*, *Hotel*, *Taxi*), and creates the expense report.
  - **Human-in-the-loop location selection** — for trips with a route (e.g. *"Frankfurt to
    London"*) it asks which place to use, looks it up in the Oracle `expenseLocations` LOV,
    and lets you pick the exact match. Location is optional — if nothing matches it proceeds
    silently.
  - **Receipt attachment** — after creation, optionally attach a receipt/bill to each report.
- **Excel FBDI data-quality assessment** — summarizes uploaded FBDI spreadsheets and reports
  data-quality findings.
- **Exception payload modification** — fetches exception `SOURCEPAYLOAD` from an Oracle ATP
  database and helps transform it.
- **Multi-file processing** — upload several expense files at once; each is reviewed and
  created in sequence.
- **Human-in-the-loop approvals** — nothing is created in Oracle Fusion until you approve.

---

## Tech stack

| Area | Technology |
|------|------------|
| Conversational UI | Chainlit |
| Orchestration | LangGraph + LangChain |
| LLM | OpenAI (via `langchain-openai`) |
| Document AI | OCI Document Understanding (classification + key-value extraction) |
| ERP integration | Oracle Fusion REST APIs (`/fscmRestApi/resources/latest`) |
| Database | Oracle ATP (`oracledb`, wallet-based mTLS) |
| Observability | Langfuse + OpenTelemetry |

---

## Project structure

```
Semanticagent/
├─ app/
│  ├─ app_final.py                # Main Chainlit app + LangGraph workflow (entry point)
│  ├─ expense.py                  # Oracle Fusion expense report helpers + location lookup
│  ├─ oracle_invoice_with_oci.py  # Oracle Fusion AP invoice create / attach / actions
│  ├─ invoice_field_validation.py # Invoice payload normalization & payment-hint enrichment
│  ├─ DocumentKeyValueExtract.py  # OCI Document AI: classify + key-value extraction
│  ├─ DataQualityAssessor.py      # Excel FBDI data-quality report generation
│  ├─ ConnectDB.py                # Oracle ATP connection + exception payload fetch
│  ├─ requirements.txt            # Python dependencies
│  ├─ chainlit.md                 # Chainlit welcome screen
│  ├─ config.toml                 # Oracle ATP DB connection config (wallet paths)
│  ├─ .env                        # Secrets & environment configuration (not for sharing)
│  └─ .chainlit/                  # Chainlit UI config + translations
├─ config/
│  └─ config.txt                  # OCI API config (DEFAULT profile) + private key
├─ Wallet_ATPDB/                  # Oracle ATP wallet (cwallet.sso, tnsnames.ora, etc.)
└─ README.md
```

---

## Prerequisites

- **Python 3.13** (a virtual environment is recommended)
- **OpenAI API key**
- **Oracle Cloud Infrastructure (OCI)** account with access to Document Understanding
- **Oracle Fusion** instance credentials with AP Invoice and Expense privileges
- **Oracle ATP** wallet (only required for the exception-payload / `modify_json` feature)

---

## Setup

1. **Create and activate a virtual environment** (from the `Semanticagent/` folder):

   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1        # Windows PowerShell
   # source venv/bin/activate          # macOS / Linux
   ```

2. **Install dependencies:**

   ```powershell
   pip install -r app/requirements.txt
   ```

---

## Configuration

### 1. Environment variables — `app/.env`

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | OpenAI API key for the LLM |
| `OCI_API_KEY` | OCI API private key (used with `config/config.txt`) |
| `FUSION_BASE_URL` | Oracle Fusion base URL, e.g. `https://<instance>.oraclecloud.com` |
| `FUSION_USER` / `FUSION_PASSWORD` | Oracle Fusion REST credentials |
| `FUSION_PERSON_ID` / `FUSION_ORG_ID` | Person & business-unit IDs used on expense reports |
| `LANGFUSE_SECRET_KEY` / `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_BASE_URL` | Langfuse tracing (optional) |
| `CHAINLIT_AUTH_SECRET` | Secret used by Chainlit for auth sessions |
| `OTEL_*` | OpenTelemetry export settings (optional) |

### 2. OCI config — `config/config.txt`

Standard OCI config file with a `[DEFAULT]` profile (`user`, `fingerprint`, `tenancy`,
`region`, `key_file`). Loaded automatically via a path relative to the app, so no code
changes are needed. Ensure `key_file` points to the private `.pem` in `config/`.

### 3. Oracle ATP — `app/config.toml`

Used only by the database exception-fetch feature. Update `config_dir` and
`wallet_location` to the absolute path of the `Wallet_ATPDB/` folder on your machine, e.g.:

```toml
config_dir      = "C:/Users/<you>/.../Semanticagent/Wallet_ATPDB"
wallet_location = "C:/Users/<you>/.../Semanticagent/Wallet_ATPDB"
```

---

## Running the app

From the `app/` directory (with the virtual environment activated):

```powershell
cd app
chainlit run app_final.py -w
```

Then open the URL shown in the terminal (default `http://localhost:8000`).

**Login:** username `admin`, password `admin` (configured in `auth_callback`).

---

## Usage

1. Log in and start a chat.
2. **Invoices:** upload an invoice PDF/image (or describe one). The agent extracts and maps
   the fields, shows the JSON, and asks for approval. On approval it creates the invoice in
   Oracle Fusion (PO matching, distributions, tax, validation, attachment).
3. **Expenses:** upload one or more receipts / tickets. The agent extracts each expense, asks
   you to confirm/select the location where applicable, and creates the expense report(s)
   on approval, then offers to attach receipts.
4. **Excel FBDI:** upload an FBDI spreadsheet to get a data-quality summary.

---

## Security notes

This project contains **live credentials** for development/demo purposes:
`app/.env`, `app/config.toml`, `config/*.pem`, and the `Wallet_ATPDB/` wallet. Treat the
folder as sensitive, do not commit secrets to public repositories, and **rotate all
credentials** if the package is shared externally.
"# SemanticAgent" 
