#!/usr/bin/env python3
"""
expense.py - Oracle Fusion Expense Report Creation

Similar to oracle_invoice_with_oci.py, this module handles expense report creation
in Oracle Fusion using the REST API.

Usage:
  from expense import create_expense_report, build_headers
  from requests.auth import HTTPBasicAuth
  
  auth = HTTPBasicAuth(username, password)
  headers = build_headers("basic")
  created = create_expense_report(base_url, expense_payload, auth, headers)
"""

import json
import os
import re
import base64
import datetime
import requests
from difflib import SequenceMatcher
from typing import Optional, Dict, Any, List
from requests.auth import HTTPBasicAuth
from functools import lru_cache
# ==========================
# CONFIGURATION
# ==========================

# REST API path for expenses
API_BASE_PATH = "/fscmRestApi/resources/latest"

# Content type for resource creation
RESOURCE_CONTENT_TYPE = "application/vnd.oracle.adf.resourceitem+json"
ACTION_CONTENT_TYPE = "application/vnd.oracle.adf.action+json"

# Keyword map: exact Oracle Fusion type label -> document synonyms
# These keys MUST match the exact Oracle Fusion expense type names
EXPENSE_TYPE_KEYWORDS = {
    "Air":                    ["air", "airfare", "flight", "airline", "aeroplane", "airplane", "plane", "aviation", "air travel", "indigo", "spicejet", "emirates", "air india", "vistara", "go air", "akasa", "delta"],
    "Breakfast":              ["breakfast", "morning meal"],
    "Car Rental":             ["car rental", "rental car", "hertz", "avis", "enterprise"],
    "Car Rental - Fuel":      ["fuel", "petrol", "gas", "diesel", "gasoline"],
    "Conference":             ["conference", "seminar", "training", "workshop", "event", "registration"],
    "Dinner":                 ["dinner", "evening meal", "supper"],
    "Entertainment":          ["entertainment", "client entertainment", "team outing"],
    "Hotel":                  ["hotel", "lodging", "accommodation", "inn", "resort", "motel", "stay"],
    "Lunch":                  ["lunch", "midday meal", "meal", "meals", "food", "restaurant", "dining", "cafe", "canteen", "subway"],
    "Mileage":                ["mileage", "miles driven"],
    "Mileage with Commute":   ["mileage with commute", "commute mileage"],
    "Miscellaneous":          ["misc", "miscellaneous", "other", "general"],
    "Parking":                ["parking", "garage", "valet"],
    "Per Diem Daily Rate":    ["per diem", "daily rate", "daily allowance"],
    "Rail & Other Travel":    ["train", "rail", "railway", "metro", "subway", "amtrak", "bus", "ferry"],
    "Supplies":               ["supplies", "office supplies", "stationery"],
    "Taxi":                   ["taxi", "cab", "uber", "ola", "lyft", "rideshare", "ride share"],
    "Telephone":              ["telephone", "phone", "telecom", "mobile", "cell"],
}

# Map Oracle expense type label → Oracle template name
# Oracle templates are typically named "Travel", "Corporate Card", etc.
# Most expense types fall under the "Travel" template.
TYPE_TO_TEMPLATE = {
    "Air": "Travel",
    "Breakfast": "Travel",
    "Car Rental": "Travel",
    "Car Rental - Fuel": "Travel",
    "Conference": "Travel",
    "Dinner": "Travel",
    "Entertainment": "Travel",
    "Entertainment Non Employee Required": "Travel",
    "Hotel": "Travel",
    "Lunch": "Travel",
    "Mileage": "Travel",
    "Mileage with Commute": "Travel",
    "Miscellaneous": "Travel",
    "Parking": "Travel",
    "Per Diem Daily Rate": "Travel",
    "Rail & Other Travel": "Travel",
    "Supplies": "Travel",
    "Taxi": "Travel",
    "Telephone": "Travel",
}

# Canonical Oracle Fusion expense type labels (authoritative list)
ORACLE_EXPENSE_TYPES = [
    "Air", "Breakfast", "Car Rental", "Car Rental - Fuel", "Conference",
    "Dinner", "Entertainment", "Entertainment Non Employee Required",
    "Hotel", "Lunch", "Mileage", "Mileage with Commute", "Miscellaneous",
    "Parking", "Per Diem Daily Rate", "Rail & Other Travel", "Supplies",
    "Taxi", "Telephone",
]


def match_expense_type_label(text: str, keyword_map: Optional[Dict[str, List[str]]] = None) -> Optional[str]:
    """
    Find the canonical Oracle expense label whose keyword the text most strongly
    relates to.

    Matching rules (designed to avoid false positives such as "airbnb" → "Air"):
    - Keywords are matched on WORD BOUNDARIES, so "air" matches the standalone
      word "air" but NOT a substring inside "airbnb", "airport", etc.
    - When several keywords match, the LONGEST (most specific) matched keyword
      wins, so "airbnb"/"short stay" (Hotel) beats a shorter accidental match.
    - Ties are broken by the number of matched keywords for a label, then by the
      label's order in the keyword map.

    Returns the canonical label, or None when nothing matches.
    """
    if not text:
        return None

    if keyword_map is None:
        keyword_map = EXPENSE_TYPE_KEYWORDS

    text_lower = text.lower()
    best_label: Optional[str] = None
    best_specificity = 0  # length of the longest matched keyword
    best_match_count = 0

    for label, synonyms in keyword_map.items():
        longest_match = 0
        match_count = 0
        for syn in synonyms:
            syn_lower = syn.lower().strip()
            if not syn_lower:
                continue
            # \b boundaries prevent "air" from matching inside "airbnb".
            if re.search(r"\b" + re.escape(syn_lower) + r"\b", text_lower):
                match_count += 1
                longest_match = max(longest_match, len(syn_lower))

        if match_count == 0:
            continue

        if (longest_match > best_specificity) or (
            longest_match == best_specificity and match_count > best_match_count
        ):
            best_specificity = longest_match
            best_match_count = match_count
            best_label = label

    return best_label


def normalize_expense_type(raw_type: str) -> str:
    """
    Normalize an expense type string to the closest Oracle Fusion label.

    1. Exact match (case-insensitive) → return immediately
    2. Keyword match via EXPENSE_TYPE_KEYWORDS → return the Oracle label key
    3. Similarity match via SequenceMatcher → return the closest label (threshold 0.4)
    4. Fallback → "Miscellaneous"
    """
    if not raw_type:
        return "Miscellaneous"

    # 1. Exact match (case-insensitive)
    for label in ORACLE_EXPENSE_TYPES:
        if raw_type.lower() == label.lower():
            return label

    # 2. Keyword match (word-boundary + specificity aware)
    matched = match_expense_type_label(raw_type)
    if matched:
        print(f"[NormalizeType] '{raw_type}' → keyword match → '{matched}'")
        return matched

    # 3. Similarity match (fuzzy)
    best_label = "Miscellaneous"
    best_score = 0.0
    for label in ORACLE_EXPENSE_TYPES:
        score = SequenceMatcher(None, raw_type.lower(), label.lower()).ratio()
        if score > best_score:
            best_score = score
            best_label = label

    if best_score >= 0.4:
        print(f"[NormalizeType] '{raw_type}' → similarity match → '{best_label}' (score={best_score:.2f})")
        return best_label

    print(f"[NormalizeType] '{raw_type}' → no match → fallback 'Miscellaneous'")
    return "Miscellaneous"


# Cache for lookups to avoid repeated API calls
_LOOKUP_CACHE = {
    "expense_types": None,
    "expense_templates": None,
    "locations": None,
    "business_units": None,
    "account_combinations": None,
    "child_resource_name": None,  # Correct child name for expense lines
    "endpoint": None,  # Discovered endpoint path
}


# ==========================
# HELPER FUNCTIONS
# ==========================

def generate_report_number() -> str:
    """Generate a unique expense report number: EXP-YYYYMMDD-HHMMSS"""
    now = datetime.datetime.now()
    return f"EXP-{now.strftime('%Y%m%d-%H%M%S')}"


def build_headers(auth_mode: str, token: Optional[str] = None) -> Dict[str, str]:
    """Build request headers for Oracle API calls."""
    headers = {
        "Accept": "application/json"
    }
    if auth_mode == "token" and token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def describe_expense_resource(base_url: str, auth, headers: dict) -> dict:
    """
    Query Oracle's /expenseReports/describe endpoint to find required fields
    and the correct child resource names. Also fetch an existing report as sample.
    """
    base = base_url.rstrip("/") + API_BASE_PATH
    result = {}

    # 1. Try the describe endpoint for schema info
    describe_url = base + "/expenseReports/describe"
    try:
        resp = requests.get(describe_url, auth=auth, headers=headers, timeout=15)
        print(f"[Describe] GET /expenseReports/describe -> {resp.status_code}")
        if resp.ok:
            data = resp.json()
            result = data
            # Extract attribute info
            attributes = data.get("attributes", [])
            children = data.get("children", {})
            print(f"[Describe] Top-level attributes: {[a.get('name') for a in attributes]}")
            required = [a.get("name") for a in attributes if a.get("required") or a.get("mandatory")]
            print(f"[Describe] Required attributes: {required}")
            if isinstance(children, dict):
                child_keys = list(children.keys())
                print(f"[Describe] Child resource names: {child_keys}")
                # Find the expense lines child
                for ck in child_keys:
                    if "expense" in ck.lower() and ("line" in ck.lower() or "item" in ck.lower()):
                        _LOOKUP_CACHE["child_resource_name"] = ck
                        print(f"[Describe] Using child resource: {ck}")
                        break
            elif isinstance(children, list):
                child_names = [c.get("name") or c.get("rel") for c in children if isinstance(c, dict)]
                print(f"[Describe] Child resource names: {child_names}")
                for cn in child_names:
                    if cn and "expense" in cn.lower() and ("line" in cn.lower() or "item" in cn.lower()):
                        _LOOKUP_CACHE["child_resource_name"] = cn
                        print(f"[Describe] Using child resource: {cn}")
                        break
            else:
                print(f"[Describe] Children (raw): {children}")
    except Exception as e:
        print(f"[Describe] describe endpoint failed: {e}")

    # 2. Fetch an existing expense report to see actual structure + child names
    sample_url = base + "/expenseReports?limit=1&onlyData=true&expand=all"
    try:
        resp = requests.get(sample_url, auth=auth, headers=headers, timeout=15)
        print(f"[Describe] GET /expenseReports?limit=1 -> {resp.status_code}")
        if resp.ok:
            data = resp.json()
            items = data.get("items", [])
            if items:
                sample = items[0]
                print(f"[Describe] Sample expense report keys: {list(sample.keys())}")
                # Look for child arrays (lists) to find the correct child resource name
                for key, val in sample.items():
                    if isinstance(val, list) and len(val) > 0:
                        print(f"[Describe] Found child array '{key}' with {len(val)} items")
                        if isinstance(val[0], dict):
                            print(f"[Describe]   Child '{key}' item keys: {list(val[0].keys())}")
            else:
                print(f"[Describe] No existing expense reports found. Full response keys: {list(data.keys())}")
                # Print count info
                print(f"[Describe] Response: {json.dumps(data, indent=2)[:1000]}")
    except Exception as e:
        print(f"[Describe] sample fetch failed: {e}")

    return result


def discover_expense_endpoint(base_url: str, auth, headers: dict) -> str:
    """
    Try known Oracle Fusion expense endpoint names and return the first valid one.
    A valid endpoint returns 200 on GET (collection) or 405 (method not allowed).
    An invalid endpoint returns 400 with empty body.
    """
    candidates = [
        "/expenseReports",
        "/expenses",
        "/expenseItems",
        "/erpexpenseReports",
    ]
    base = base_url.rstrip("/") + API_BASE_PATH

    for path in candidates:
        url = base + path
        try:
            resp = requests.get(url, auth=auth, headers=headers, timeout=15)
            print(f"[Discovery] GET {path} -> {resp.status_code}")
            # 200 = resource exists, 405 = exists but GET not allowed
            if resp.status_code in (200, 405):
                print(f"[Discovery] Found working endpoint: {path}")
                return path
            # 401/403 also means the endpoint exists but auth issue
            if resp.status_code in (401, 403):
                print(f"[Discovery] Endpoint {path} exists (auth issue: {resp.status_code})")
                return path
        except Exception as e:
            print(f"[Discovery] GET {path} failed: {e}")

    # Fallback: query the REST API catalog for any expense-related resource
    catalog_url = base
    print(f"[Discovery] Checking REST catalog at: {catalog_url}")
    try:
        resp = requests.get(catalog_url, auth=auth, headers=headers, timeout=15)
        print(f"[Discovery] Catalog response: {resp.status_code}")
        if resp.ok:
            data = resp.json()
            items = data.get("items", [])
            expense_resources = [item for item in items if "expense" in item.get("name", "").lower()]
            if expense_resources:
                for res in expense_resources:
                    print(f"[Discovery] Found in catalog: {res.get('name')}")
                return "/" + expense_resources[0].get("name")
            else:
                # Print all available resources for debugging
                all_names = [item.get("name") for item in items[:50]]
                print(f"[Discovery] No expense resource found. Available resources: {all_names}")
    except Exception as e:
        print(f"[Discovery] Catalog query failed: {e}")

    return "/expenseReports"  # default fallback


# ==========================
# LOOKUP/REFERENCE DATA FUNCTIONS
# ==========================

def fetch_expense_types(base_url: str, auth: Optional[HTTPBasicAuth], headers: dict, template_id: int = None) -> Dict[str, int]:
    """
    Fetch expense types from Oracle filtered by ExpenseTemplateId.
    Each template has its own set of type IDs — global IDs won't work.
    """
    cache_key = f"expense_types_{template_id}" if template_id else "expense_types"
    if _LOOKUP_CACHE.get(cache_key) is not None:
        return _LOOKUP_CACHE[cache_key]

    base = base_url.rstrip("/") + API_BASE_PATH + "/expenseTypes"
    if template_id:
        url = f"{base}?q=ExpenseTemplateId={template_id}&onlyData=true&limit=50"
    else:
        url = f"{base}?onlyData=true&limit=50"
    hdrs = headers.copy()

    try:
        print(f"[TypeFetch] GET {url}")
        resp = requests.get(url, auth=auth, headers=hdrs, timeout=30)
        if not resp.ok:
            print(f"⚠️  Warning: Could not fetch expense types: {resp.status_code}")
            return {}

        data = resp.json()
        expense_types_map = {}

        items = data.get("items", [])
        if not items and isinstance(data, dict):
            items = [data]

        for item in items:
            name = item.get("Name") or item.get("name") or item.get("Code") or item.get("code")
            exp_id = item.get("ExpenseTypeId") or item.get("Id") or item.get("id")

            if name and exp_id:
                expense_types_map[name] = exp_id

        _LOOKUP_CACHE[cache_key] = expense_types_map
        print(f"✅ Cached {len(expense_types_map)} expense types for template {template_id}: {list(expense_types_map.keys())}")
        return expense_types_map

    except Exception as e:
        print(f"⚠️  Error fetching expense types: {e}")
        return {}


def fetch_expense_templates(base_url: str, auth: Optional[HTTPBasicAuth], headers: dict, org_id: int = None) -> Dict[str, int]:
    """
    Fetch expense templates from Oracle filtered by OrgId (business unit).
    Returns map of name -> ExpenseTemplateId.
    Caches result to avoid repeated API calls.
    """
    cache_key = f"expense_templates_{org_id}" if org_id else "expense_templates"
    if _LOOKUP_CACHE.get(cache_key) is not None:
        return _LOOKUP_CACHE[cache_key]

    base = base_url.rstrip("/") + API_BASE_PATH + "/expenseTemplates"
    # Filter by OrgId to get only templates valid for this business unit
    if org_id:
        url = f"{base}?q=OrgId={org_id}&onlyData=true"
    else:
        url = f"{base}?onlyData=true"
    hdrs = headers.copy()

    try:
        print(f"[TemplateFetch] GET {url}")
        resp = requests.get(url, auth=auth, headers=hdrs, timeout=30)
        if not resp.ok:
            print(f"⚠️  Warning: Could not fetch expense templates: {resp.status_code}")
            return {}

        data = resp.json()
        templates_map = {}

        items = data.get("items", [])
        if not items and isinstance(data, dict):
            items = [data]

        for item in items:
            name = item.get("Name") or item.get("name")
            tmpl_id = item.get("ExpenseTemplateId")
            bu = item.get("BusinessUnit", "")
            if name and tmpl_id:
                templates_map[name] = tmpl_id
                print(f"[TemplateFetch] '{name}' → ID={tmpl_id}, BU='{bu}'")

        _LOOKUP_CACHE[cache_key] = templates_map
        print(f"✅ Cached {len(templates_map)} expense templates for OrgId={org_id}: {list(templates_map.keys())}")
        return templates_map

    except Exception as e:
        print(f"⚠️  Error fetching expense templates: {e}")
        return {}


def resolve_expense_type_id(text: str, base_url: str, auth: Optional[HTTPBasicAuth], headers: dict, template_id: int = None) -> int:
    """
    Resolve ExpenseTypeId by matching document text against EXPENSE_TYPE_KEYWORDS,
    then looking up the canonical Oracle type name in fetched expense types
    scoped to the specific template.
    """
    # Find canonical Oracle key from keyword map (word-boundary + specificity aware
    # so e.g. "airbnb" resolves to "Hotel", never "Air").
    matched_key = match_expense_type_label(text)

    expense_types = fetch_expense_types(base_url, auth, headers, template_id=template_id)
    print(f"[TypeLookup] text='{text}' → canonical='{matched_key}' | template={template_id} | available={list(expense_types.keys())}")

    if matched_key and expense_types:
        # Try exact match first, then case-insensitive partial match
        for name, eid in expense_types.items():
            if matched_key.lower() == name.lower():
                print(f"[TypeLookup] ✅ Matched '{name}' → ExpenseTypeId={eid}")
                return int(eid)
        for name, eid in expense_types.items():
            if matched_key.lower() in name.lower() or name.lower() in matched_key.lower():
                print(f"[TypeLookup] ✅ Partial match '{name}' → ExpenseTypeId={eid}")
                return int(eid)

    print(f"[TypeLookup] ⚠️  No match — using fallback ExpenseTypeId=10005")
    return 10005


def resolve_expense_template_id(text: str, base_url: str, auth: Optional[HTTPBasicAuth], headers: dict, org_id: int = None) -> tuple:
    """
    Resolve ExpenseTemplateId by fetching templates filtered by OrgId (business unit).
    If only one template exists for the BU, use it directly.

    Returns:
        tuple of (ExpenseTemplateId, matched_template_name)
    """
    expense_templates = fetch_expense_templates(base_url, auth, headers, org_id=org_id)
    print(f"[TemplateLookup] Templates for OrgId={org_id}: {list(expense_templates.keys())}")

    if expense_templates:
        # If there's only one template for this BU, use it
        if len(expense_templates) == 1:
            name, tid = next(iter(expense_templates.items()))
            print(f"[TemplateLookup] ✅ Single template for BU: '{name}' → ExpenseTemplateId={tid}")
            return int(tid), name

        # Multiple templates: try to find one matching "Travel" or "Expenses" pattern
        for name, tid in expense_templates.items():
            if "expense" in name.lower():
                print(f"[TemplateLookup] ✅ Matched '{name}' → ExpenseTemplateId={tid}")
                return int(tid), name
        for name, tid in expense_templates.items():
            if "travel" in name.lower():
                print(f"[TemplateLookup] ✅ Matched '{name}' → ExpenseTemplateId={tid}")
                return int(tid), name

        # Fallback to first available template for this BU
        name, tid = next(iter(expense_templates.items()))
        print(f"[TemplateLookup] ✅ Using first template: '{name}' → ExpenseTemplateId={tid}")
        return int(tid), name

    print(f"[TemplateLookup] ⚠️  No templates found for OrgId={org_id}")
    return 10024, "Travel"


def fetch_locations(base_url: str, auth: Optional[HTTPBasicAuth], headers: dict) -> Dict[str, str]:
    """
    Fetch locations from Oracle and return map of name -> location code.
    Caches result to avoid repeated API calls.
    
    Returns:
        Dict mapping location names -> location codes
    """
    if _LOOKUP_CACHE["locations"] is not None:
        return _LOOKUP_CACHE["locations"]
    
    url = base_url.rstrip("/") + API_BASE_PATH + "/locations"
    hdrs = headers.copy()
    
    try:
        resp = requests.get(url, auth=auth, headers=hdrs, timeout=30)
        if not resp.ok:
            print(f"⚠️  Warning: Could not fetch locations: {resp.status_code}")
            return {}
        
        data = resp.json()
        locations_map = {}
        
        items = data.get("items", [])
        if not items and isinstance(data, dict):
            items = [data]
        
        for item in items:
            name = item.get("LocationName") or item.get("Name") or item.get("name")
            code = item.get("LocationCode") or item.get("Code") or item.get("code")
            
            if name and code:
                locations_map[name] = code
        
        _LOOKUP_CACHE["locations"] = locations_map
        print(f"✅ Cached {len(locations_map)} locations")
        return locations_map
    
    except Exception as e:
        print(f"⚠️  Error fetching locations: {e}")
        return {}


def search_expense_locations(
    search_term: str,
    base_url: str,
    auth,
    headers: dict,
    limit: int = 200,
) -> list:
    """
    Search the Oracle Fusion *expenseLocations* LOV for locations relevant to the
    extracted location text.

    This is the authoritative geography list used by Expenses (the expense line's
    ``LocationId`` is the ``GeographyId`` returned here). The endpoint only supports
    server-side filtering on the ``Location`` attribute, so we query
    ``Location LIKE '*<city>*'`` and then filter the results client-side.

    Relevance rule (matches user intent precisely):
      - Only the FIRST comma-separated component of the extracted value (the city)
        is used for the search, so "Austin, TX" still finds "Austin".
      - A result is kept only when its ``Location`` string STARTS WITH the search
        term as a WHOLE WORD. So "Austin" keeps "Austin, TX, United States" and
        "Austin Lake, ..." but rejects "Austinburg", "Austintown", "Austinville"
        (different words) and "Port Austin"/"Bellville, Austin" (Austin not leading).

    Returns a ranked list of dicts:
        {"LocationId", "LocationName", "City", "State", "County", "Country"}
    """
    if not search_term:
        return []

    # Use the leading component (the city) for the LIKE search.
    term = search_term.split(",")[0].strip()
    if not term:
        return []

    base = base_url.rstrip("/") + API_BASE_PATH
    safe_term = term.replace("'", "''")  # escape single quotes for the q filter
    url = f"{base}/expenseLocations?q=Location LIKE '*{safe_term}*'&limit={limit}&onlyData=true"
    hdrs = headers.copy()

    try:
        print(f"[ExpenseLocationSearch] GET {url}")
        resp = requests.get(url, auth=auth, headers=hdrs, timeout=30)
        if not resp.ok:
            print(f"[ExpenseLocationSearch] ⚠️ Search failed: {resp.status_code} {resp.text[:200]}")
            return []
        items = resp.json().get("items", [])
    except Exception as e:
        print(f"[ExpenseLocationSearch] ⚠️ Error: {e}")
        return []

    # Keep only entries whose Location starts with the term as a whole word.
    # ^term\b rejects glued prefixes (Austinburg/Austintown/Austinville).
    pattern = re.compile(r"^" + re.escape(term) + r"\b", re.IGNORECASE)
    results = []
    for item in items:
        name = item.get("Location")
        gid = item.get("GeographyId")
        if not name or not gid:
            continue
        if not pattern.match(name.strip()):
            continue
        results.append({
            "LocationId": gid,
            "LocationName": name,
            "City": item.get("City"),
            "State": item.get("State"),
            "County": item.get("County"),
            "Country": item.get("Country"),
        })

    # Rank city-level matches ("Austin, ...") ahead of the rest, then alphabetically.
    term_lower = term.lower()
    results.sort(key=lambda r: (0 if r["LocationName"].lower().startswith(term_lower + ",") else 1, r["LocationName"].lower()))

    print(f"[ExpenseLocationSearch] '{search_term}' → {len(results)} relevant of {len(items)} raw matches for '{term}'")
    return results


def normalize_location(raw_location: str, base_url: str, auth, headers: dict) -> dict:
    """
    Resolve a location string to an Oracle Fusion expense location automatically
    (used as a non-interactive fallback when no user selection is available).

    Oracle requires the numeric LocationId (== GeographyId); the free-text
    "Location" field is ignored on the expense line.

    Strategy:
    1. Search expenseLocations for the extracted city (whole-word leading match).
    2. Prefer an exact full-string match, else the top-ranked city-level match.
    3. No relevant match → return empty dict (Location is optional, omit it).
    """
    if not raw_location:
        return {}

    matches = search_expense_locations(raw_location.strip(), base_url, auth, headers)

    if not matches:
        print(f"[LocationNormalize] No expense locations found for '{raw_location}', omitting Location")
        return {}

    # 1. Exact match (case-insensitive) on full LocationName
    raw_lower = raw_location.lower().strip()
    for m in matches:
        if raw_lower == m["LocationName"].lower():
            print(f"[LocationNormalize] ✅ Exact match: '{raw_location}' → '{m['LocationName']}' (LocationId={m['LocationId']})")
            return {"LocationId": m["LocationId"], "LocationName": m["LocationName"]}

    # 2. Top-ranked relevant match (city-level entries are ranked first)
    first = matches[0]
    print(f"[LocationNormalize] ✅ Best available match: '{raw_location}' → '{first['LocationName']}' (LocationId={first['LocationId']})")
    return {"LocationId": first["LocationId"], "LocationName": first["LocationName"]}


def fetch_business_units(base_url: str, auth: Optional[HTTPBasicAuth], headers: dict) -> Dict[str, str]:
    """
    Fetch business units from Oracle.
    
    Returns:
        Dict mapping business unit names -> IDs
    """
    if _LOOKUP_CACHE["business_units"] is not None:
        return _LOOKUP_CACHE["business_units"]
    
    url = base_url.rstrip("/") + API_BASE_PATH + "/businessUnits"
    hdrs = headers.copy()
    
    try:
        resp = requests.get(url, auth=auth, headers=hdrs, timeout=30)
        if not resp.ok:
            print(f"⚠️  Warning: Could not fetch business units: {resp.status_code}")
            return {}
        
        data = resp.json()
        bu_map = {}
        
        items = data.get("items", [])
        if not items and isinstance(data, dict):
            items = [data]
        
        for item in items:
            name = item.get("Name") or item.get("name")
            bu_id = item.get("BusinessUnitId") or item.get("Id") or item.get("id")
            
            if name and bu_id:
                bu_map[name] = bu_id
        
        _LOOKUP_CACHE["business_units"] = bu_map
        print(f"✅ Cached {len(bu_map)} business units")
        return bu_map
    
    except Exception as e:
        print(f"⚠️  Error fetching business units: {e}")
        return {}


def validate_expense_payload(payload: dict) -> tuple[bool, list]:
    """
    Validate expense payload against required fields.
    Note: This validates the INPUT payload (simple format from LLM).
    The transform_to_oracle_expense() function converts it to Oracle format.
    
    Returns:
        (is_valid, list_of_errors)
    """
    errors = []
    
    # Required fields for expense report (INPUT format)
    required_fields = {
        "ReceiptDate": "receipt date (YYYY-MM-DD)",
        "ReceiptAmount": "receipt amount (number)",
        "ReimbursableAmount": "reimbursable amount (number)",
        "MerchantName": "merchant/vendor name"
    }
    
    for field, description in required_fields.items():
        if field not in payload:
            errors.append(f"❌ Missing required field: {field} ({description})")
        elif payload[field] is None:
            errors.append(f"❌ Field {field} is null (required)")
    
    # Validate amounts are numbers
    if "ReceiptAmount" in payload:
        try:
            amount = float(payload["ReceiptAmount"])
            if amount <= 0:
                errors.append(f"❌ ReceiptAmount must be > 0, got: {amount}")
        except (ValueError, TypeError):
            errors.append(f"❌ ReceiptAmount must be a number")
    
    if "ReimbursableAmount" in payload:
        try:
            amount = float(payload["ReimbursableAmount"])
            if amount <= 0:
                errors.append(f"❌ ReimbursableAmount must be > 0, got: {amount}")
        except (ValueError, TypeError):
            errors.append(f"❌ ReimbursableAmount must be a number")
    
    # Validate date format (YYYY-MM-DD)
    if "ReceiptDate" in payload:
        date_str = payload["ReceiptDate"]
        if not isinstance(date_str, str) or len(date_str) != 10:
            errors.append(f"❌ ReceiptDate must be YYYY-MM-DD format, got: {date_str}")
    
    return len(errors) == 0, errors


def transform_to_oracle_expense(
    simple_payload: dict,
    base_url: str,
    auth: Optional[HTTPBasicAuth],
    headers: dict
) -> tuple[bool, Dict[str, Any], List[str]]:
    
    """
    Transform simple expense payload (from LLM) into Oracle Fusion expense format.
    
    Input format (from LLM extraction):
    {
        "Description": "...",
        "ReceiptDate": "2026-03-06",
        "ExpenseTypeId": 10005,
        "ReceiptAmount": 500,
        "ReimbursableAmount": 500,
        "MerchantName": "...",
        ... other fields
    }
    
    Returns:
        (is_valid, oracle_formatted_payload, list_of_errors)
    """
    errors = []

    # Ensure all required expense fields are present
    required_oracle_fields = [
        "ReceiptDate",
        "ReceiptAmount",
        "ReimbursableAmount",
        "MerchantName"
    ]

    for field in required_oracle_fields:
        if field not in simple_payload or simple_payload[field] is None:
            errors.append(f"Missing required field: {field}")

    if errors:
        return False, {}, errors

    # Validate amounts
    try:
        receipt_amt = float(simple_payload["ReceiptAmount"])
        reimb_amt = float(simple_payload["ReimbursableAmount"])

        if receipt_amt <= 0:
            errors.append(f"ReceiptAmount must be > 0")
        if reimb_amt <= 0:
            errors.append(f"ReimbursableAmount must be > 0")
        if reimb_amt > receipt_amt:
            errors.append(f"ReimbursableAmount ({reimb_amt}) cannot exceed ReceiptAmount ({receipt_amt})")
    except (ValueError, TypeError) as e:
        errors.append(f"Amount validation failed: {e}")

    if errors:
        return False, {}, errors

    # Ensure date is string in YYYY-MM-DD format
    receipt_date = simple_payload["ReceiptDate"]
    if not isinstance(receipt_date, str):
        receipt_date = str(receipt_date)

    # Description
    description = simple_payload.get("Description") or f"Expense from {simple_payload.get('MerchantName', 'Receipt')}"

    # Build the expense line (only Oracle-recognized fields)
    expense_line = {
        "ReceiptDate": receipt_date,
        "ReceiptAmount": receipt_amt,
        "ReimbursableAmount": reimb_amt,
        "MerchantName": simple_payload["MerchantName"],
        "Description": description,
    }

    # Add ExpenseTypeId only if present in payload
    if simple_payload.get("ExpenseTypeId") is not None:
        expense_line["ExpenseTypeId"] = int(simple_payload["ExpenseTypeId"])

    # Add optional line-level fields if present
    if simple_payload.get("TicketNumber"):
        expense_line["TicketNumber"] = simple_payload["TicketNumber"]
    if simple_payload.get("ExpenseTemplateId"):
        expense_line["ExpenseTemplateId"] = int(simple_payload["ExpenseTemplateId"])
    else:
        expense_line["ExpenseTemplateId"] = 10024  # Default template

    # Add CurrencyCode to the expense line if present
    currency = simple_payload.get("CurrencyCode") or simple_payload.get("InvoiceCurrency") or "USD"
    expense_line["CurrencyCode"] = currency

    # Use the discovered child resource name, or try common Oracle names
    child_name = _LOOKUP_CACHE.get("child_resource_name") or "expenseLines"
    print(f"[Transform] Using child resource name: {child_name}")

    # Build the expense report (top-level) with nested expense lines
    oracle_payload = {
        "Purpose": description,
        "ExpenseReportDate": receipt_date,
        "ReimbursementCurrencyCode": currency,
        child_name: [expense_line]
    }

    # Add optional top-level fields if available
    if simple_payload.get("BusinessUnit"):
        oracle_payload["BusinessUnitName"] = simple_payload["BusinessUnit"]
    if simple_payload.get("PersonId"):
        oracle_payload["PersonId"] = simple_payload["PersonId"]

    return True, oracle_payload, []


# ==========================
# CREATE EXPENSE REPORT
# ==========================

def _parse_error(resp) -> str:
    """Extract a readable error message from an Oracle API error response."""
    raw = resp.text[:2000] if resp.text else "(empty response body)"
    try:
        error_json = resp.json()
        if "o:errorDetails" in error_json:
            details = error_json["o:errorDetails"]
            if isinstance(details, list) and details:
                msgs = "; ".join(d.get("detail", str(d)) for d in details)
                return f"{msgs} | raw: {raw}"
        if "detail" in error_json:
            return f"{error_json['detail']} | raw: {raw}"
        if "title" in error_json:
            return f"{error_json.get('title')}: {error_json.get('detail', '')} | raw: {raw}"
        if "message" in error_json:
            return f"{error_json['message']} | raw: {raw}"
    except ValueError:
        pass
    return raw


def _post(url: str, payload: dict, auth, hdrs: dict) -> dict:
    """POST to Oracle API and raise RuntimeError on failure."""
    print(f"📤 POST {url}")
    print(f"📄 Payload: {json.dumps(payload, indent=2)}")
    resp = requests.post(url, json=payload, auth=auth, headers=hdrs, timeout=60)
    print(f"📥 Status: {resp.status_code}")
    print(f"📥 Response headers: {dict(resp.headers)}")
    print(f"📥 Response body: {resp.text[:3000]}")
    if not resp.ok:
        raise RuntimeError(f"{resp.status_code}: {_parse_error(resp)}")
    try:
        return resp.json()
    except ValueError:
        raise RuntimeError(f"Non-JSON response ({resp.status_code}): {resp.text[:500]}")

_context_cache = {}

def fetch_user_context(base_url: str, auth, headers: dict) -> dict:
    """
    Fetch AssignmentId and BusinessUnit from an existing expense report.
    PersonId and OrgId come from .env.
    """
    person_id = os.environ.get("FUSION_PERSON_ID")
    org_id = os.environ.get("FUSION_ORG_ID")

    if not person_id:
        raise RuntimeError("FUSION_PERSON_ID is missing in .env")
    if not org_id:
        raise RuntimeError("FUSION_ORG_ID is missing in .env")

    cache_key = f"{base_url}:{person_id}"
    if cache_key in _context_cache:
        print("[Context] ⚡ Using cached context")
        return _context_cache[cache_key]

    base = base_url.rstrip("/") + API_BASE_PATH

    context = {
        "PersonId": int(person_id),
        "OrgId": int(org_id),
        "AssignmentId": None,
        "BusinessUnit": None,
    }

    # Fetch AssignmentId and BusinessUnit from existing expense report
    url = f"{base}/expenseReports?limit=1&onlyData=true"
    print(f"[Context] GET {url}")
    resp = requests.get(url, auth=auth, headers=headers, timeout=30)
    print(f"[Context] Status: {resp.status_code}")

    if resp.ok:
        items = resp.json().get("items", [])
        if items:
            sample = items[0]
            context["AssignmentId"] = sample.get("AssignmentId")
            context["BusinessUnit"] = sample.get("BusinessUnit") or sample.get("BusinessUnitName")
            print(f"[Context] From existing report: AssignmentId={context['AssignmentId']}, BusinessUnit={context['BusinessUnit']}")
    else:
        print(f"[Context] Could not fetch existing reports ({resp.status_code}), AssignmentId/BusinessUnit will be omitted")

    print(f"[Context] ✅ {context}")
    _context_cache[cache_key] = context
    return context


def create_expense_report(
    base_url: str,
    expense_payload: dict,
    auth: Optional[HTTPBasicAuth],
    headers: dict,
) -> dict:
    """
    Single POST to /expenseReports with header + nested Expense lines.
    """
    print("[ExpenseReport] *** CODE VERSION: 2026-03-23-v2 (with MerchantName, ReceiptDate, ExpenseType string fields) ***")
    base = base_url.rstrip("/") + API_BASE_PATH
    report_url = base + "/expenseReports"

    hdrs = headers.copy()
    hdrs["Content-Type"] = "application/json"
    hdrs["REST-Framework-Version"] = "4"

    # -----------------------------
    # 1. FETCH CONTEXT
    # -----------------------------
    context = fetch_user_context(base_url, auth, headers)
    person_id = context["PersonId"]
    org_id = context["OrgId"]
    assignment_id = context["AssignmentId"]
    bu_name = context["BusinessUnit"]

    # -----------------------------
    # 2. EXTRACT LLM PAYLOAD
    # -----------------------------
    currency = expense_payload.get("CurrencyCode") or "USD"

    receipt_amount = float(expense_payload.get("ReceiptAmount", 0))
    reimbursable_amount = float(
        expense_payload.get("ReimbursableAmount", receipt_amount)
    )

    description = (
        expense_payload.get("Description")
        or f"Expense from {expense_payload.get('MerchantName', 'Receipt')}"
    )

    raw_expense_type = expense_payload.get("ExpenseType") or "Miscellaneous"
    expense_type = normalize_expense_type(raw_expense_type)
    print(f"[ExpenseReport] ExpenseType: '{raw_expense_type}' → normalized: '{expense_type}'")

    raw_location = expense_payload.get("Location") or ""
    # Honor a location already chosen by the user (human-in-the-loop selection).
    # "_resolved_location" present means the location step has already run; its
    # LocationId may be None to intentionally omit the location.
    preset_location = expense_payload.get("_resolved_location")
    if isinstance(preset_location, dict):
        location_info = {
            "LocationId": preset_location.get("LocationId"),
            "LocationName": preset_location.get("LocationName", raw_location),
        }
        print(f"[ExpenseReport] Location: using user-selected → {location_info}")
    else:
        location_info = normalize_location(raw_location, base_url, auth, headers)
        print(f"[ExpenseReport] Location: '{raw_location}' → resolved: {location_info}")
    receipt_date = expense_payload.get("ReceiptDate")

    # Use BusinessUnit from document extraction if available, otherwise from context
    doc_bu = expense_payload.get("BusinessUnit")
    if doc_bu:
        bu_name = doc_bu

    if receipt_amount <= 0 or reimbursable_amount <= 0:
        raise ValueError("Amounts must be greater than zero")

    # -----------------------------
    # 3. STEP 1: CREATE HEADER
    # -----------------------------
    header_payload = {
        "PersonId": person_id,
        "OrgId": org_id,
        "Purpose": description,
        "ReimbursementCurrencyCode": currency,
        "ExpenseReportDate": receipt_date,
    }

    print(f"[ExpenseReport] Step 1 - Creating header...")
    print(f"[ExpenseReport] POST {report_url}")
    print(f"[ExpenseReport] Header payload:\n{json.dumps(header_payload, indent=2)}")

    header_resp = _post(report_url, header_payload, auth, hdrs)

    report_id = header_resp.get("ExpenseReportId")
    if not report_id:
        print(f"[ExpenseReport] Header response keys: {list(header_resp.keys())}")
        raise RuntimeError("No ExpenseReportId in header response")

    print(f"[ExpenseReport] ✅ Header created. ExpenseReportId={report_id}")

    # -----------------------------
    # 4. STEP 2: ADD EXPENSE LINE
    # -----------------------------
    expense_line_url = f"{report_url}/{report_id}/child/Expense"

    # Resolve template first (by OrgId), then type scoped to that template
    expense_template_id, template_name = resolve_expense_template_id(expense_type, base_url, auth, headers, org_id=org_id)
    expense_type_id = resolve_expense_type_id(expense_type, base_url, auth, headers, template_id=expense_template_id)
    print(f"[ExpenseReport] Resolved ExpenseTemplateId={expense_template_id} ('{template_name}'), ExpenseTypeId={expense_type_id}")

    # Get merchant name from LLM payload
    merchant_name = expense_payload.get("MerchantName") or ""

    # Oracle auto-populates string names from IDs — sending both causes 500
    line_payload = {
        "ExpenseTypeId": expense_type_id,
        "ExpenseTemplateId": expense_template_id,
        "ReceiptAmount": receipt_amount,
        "ReceiptCurrencyCode": currency,
        "ReimbursableAmount": reimbursable_amount,
        "ReimbursementCurrencyCode": currency,
        "ReceiptDate": receipt_date,
        "Description": description,
        "MerchantName": merchant_name,
    }

    # Add LocationId if resolved from Oracle API (Location string is ignored by Oracle)
    if location_info.get("LocationId"):
        line_payload["LocationId"] = location_info["LocationId"]
        print(f"[ExpenseReport] Using LocationId={location_info['LocationId']} ({location_info.get('LocationName', '')})")

    print(f"[ExpenseReport] Step 2 - Adding expense line...")
    print(f"[ExpenseReport] POST {expense_line_url}")
    print(f"[ExpenseReport] Line payload:\n{json.dumps(line_payload, indent=2)}")

    line_resp = _post(expense_line_url, line_payload, auth, hdrs)

    print(f"[ExpenseReport] ✅ Expense line added successfully")

    return {
        "header": header_resp,
        "expense_line": line_resp,
        "ExpenseReportId": report_id,
    }


# ==========================
# ATTACHMENT UPLOAD
# ==========================

def upload_expense_attachment(
    base_url: str, report_id, expense_id,
    file_bytes: bytes, file_name: str,
    auth, headers: dict
) -> dict:
    """Attach a receipt/bill file to an expense line in Oracle Fusion."""
    url = (base_url.rstrip("/") + API_BASE_PATH +
           f"/expenseReports/{report_id}/child/Expense/{expense_id}/child/Attachments")

    mime_map = {
        "pdf": "application/pdf",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
    }
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "pdf"
    content_type = mime_map.get(ext, "application/octet-stream")
    payload = {
        "DatatypeCode": "FILE",
        "CategoryName": "MISC",
        "FileName": file_name,
        "Title": file_name,
        "Description": "Expense receipt",
        "FileContents": base64.b64encode(file_bytes).decode("ascii"),
        "UploadedFileContentType": content_type,
        "UploadedFileLength": len(file_bytes),
    }

    hdrs = headers.copy()
    hdrs["Content-Type"] = "application/vnd.oracle.adf.resourceitem+json"

    print(f"[Attachment] POST {url}")
    print(f"[Attachment] File: {file_name} ({len(file_bytes)} bytes, type={ext})")
    return _post(url, payload, auth, hdrs)


# ==========================
# PARSE EXPENSE RESPONSE
# ==========================

def get_expense_self_href(created_response: dict, base_url: str) -> Optional[str]:
    """
    Extract the self href from Oracle API response.
    
    Looks for patterns like:
    - response['links'] with 'rel': 'self'
    - response['items'][0]['links'] for collection responses
    """
    if isinstance(created_response, dict):
        links = created_response.get("links") or created_response.get("_links")
        if isinstance(links, list):
            for l in links:
                if l.get("rel") == "self" and l.get("href"):
                    return l.get("href")
        
        # Try items array
        items = created_response.get("items")
        if isinstance(items, list) and len(items) > 0:
            first = items[0]
            links = first.get("links") or first.get("_links")
            if isinstance(links, list):
                for l in links:
                    if l.get("rel") == "self" and l.get("href"):
                        return l.get("href")
        
        # Fallback: if expenseId is present
        expense_id = created_response.get("ExpenseReportId") or created_response.get("expenseId")
        if expense_id:
            return base_url.rstrip("/") + API_BASE_PATH + f"/expenseReports/{expense_id}"
    
    return None


# ==========================
# SAMPLE EXPENSE PAYLOAD (Oracle Fusion Format)
# ==========================

SAMPLE_EXPENSE = {
    "Description": "Flight for client visit",
    "ReceiptDate": "2026-01-19",
    "ReceiptAmount": 850.50,
    "ReimbursableAmount": 850.50,
    "MerchantName": "Indigo Airlines",
}


# ==========================
# MAIN (FOR TESTING)
# ==========================

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    
    base_url = os.getenv("FUSION_BASE_URL")
    username = os.getenv("FUSION_USER")
    password = os.getenv("FUSION_PASSWORD")
    
    if not all([base_url, username, password]):
        print("❌ Missing Oracle Fusion credentials in .env")
        exit(1)
    
    print("🔐 Testing Expense Report Creation")
    print(f"📍 URL: {base_url}")
    
    # Create auth and headers
    auth = HTTPBasicAuth(username, password)
    headers = build_headers("basic")
    
    # Fetch lookups first
    print("\n📥 Fetching reference data...")
    expense_types = fetch_expense_types(base_url, auth, headers)
    locations = fetch_locations(base_url, auth, headers)
    print(f"✅ Available expense types: {list(expense_types.keys())}")
    print(f"✅ Available locations: {list(locations.keys())}")
    
    # Validate sample payload
    print("\n✓ Validating expense payload...")
    is_valid, errors = validate_expense_payload(SAMPLE_EXPENSE)
    if not is_valid:
        print("❌ Payload validation failed:")
        for error in errors:
            print(f"  {error}")
        exit(1)

    print("✅ Payload validation passed")
    print(f"📄 Expense payload: {json.dumps(SAMPLE_EXPENSE, indent=2)}")

    # Create expense report
    try:
        print("\n✓ Creating expense report in Oracle Fusion...")
        response = create_expense_report(base_url, SAMPLE_EXPENSE, auth, headers)
        
        print("\n✅ Expense Report Created Successfully")
        print(f"📄 Response: {json.dumps(response, indent=2)}")
        
        # Extract self link
        expense_href = get_expense_self_href(response, base_url)
        if expense_href:
            print(f"🔗 Expense Resource: {expense_href}")
        
    except Exception as e:
        print(f"\n❌ Failed to create expense: {e}")