#!/usr/bin/env python3

"""
oracle_create_invoice.py

 

Usage:
  python oracle_create_invoice.py --input invoice.json \
    --base https://myinstance.fa.us2.oraclecloud.com \
    --auth basic --user myuser --pass mypass

 

Or with a Bearer token:
  python oracle_create_invoice.py --input invoice.json \
    --base https://myinstance.fa.us2.oraclecloud.com \
    --auth token --token <ACCESS_TOKEN>

 

References:
- Oracle Invoices POST: content type and payload shape. See Oracle docs. 
"""

 
import oci
import argparse
import json
import sys
import base64
from typing import Optional, Dict, Any
import requests
from requests.auth import HTTPBasicAuth

 

# default REST API resource path/version used in Oracle docs
API_BASE_PATH = "/fscmRestApi/resources/latest"

 

# Content type Oracle examples use for resource-item JSON payloads
RESOURCE_CONTENT_TYPE = "application/vnd.oracle.adf.resourceitem+json"
ACTION_CONTENT_TYPE = "application/vnd.oracle.adf.action+json"

 

 

def build_headers(auth_mode: str, token: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Accept": "application/json"
    }
    if auth_mode == "token" and token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

 

 

def create_invoice(base_url: str, invoice_payload: dict, auth: Optional[HTTPBasicAuth], headers: dict) -> dict:
    url = base_url.rstrip("/") + API_BASE_PATH + "/invoices"
    hdrs = headers.copy()
    hdrs["Content-Type"] = RESOURCE_CONTENT_TYPE
    resp = requests.post(url, json=invoice_payload, auth=auth, headers=hdrs, timeout=60)
    if not resp.ok:
        try:
            error_details = resp.json()
            error_msg = json.dumps(error_details, indent=2)
        except ValueError:
            error_msg = resp.text if resp.text else resp.reason
        raise RuntimeError(f"{resp.status_code} Oracle API Error: \n{error_msg}")
        
    try:
        data = resp.json()
    except ValueError:
        data = {}
    return data

 

 

def upload_attachment(base_url: str, invoice_self_href: str, attachment_payload: dict, auth: Optional[HTTPBasicAuth], headers: dict) -> dict:
    """
    Attachments are posted to the invoice's child attachments resource.
    The docs show POST on: /invoices/{invoicesUniqID}/child/attachments
    You can either construct that from invoicesUniqID or use the invoice 'self' href.
    """
    # If invoice_self_href contains the resource path we can append child/attachments
    if invoice_self_href.endswith("/"):
        invoice_self_href = invoice_self_href[:-1]
    url = invoice_self_href + "/child/attachments"
    hdrs = headers.copy()
    hdrs["Content-Type"] = RESOURCE_CONTENT_TYPE
    resp = requests.post(url, json=attachment_payload, auth=auth, headers=hdrs, timeout=60)
    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
    if not resp.ok:
        raise RuntimeError(f"Upload attachment failed: {resp.status_code} {resp.text}")
    return data

 

 

def post_action(base_url: str, action: str, action_payload: dict, auth: Optional[HTTPBasicAuth], headers: dict, invoice_id: Optional[str] = None) -> dict:
    """
    Generic POST to action endpoints, e.g. /invoices/{id}/action/generateDistributions
    or /invoices/action/validateInvoice
    """
    if invoice_id:
        url = base_url.rstrip("/") + API_BASE_PATH + f"/invoices/{invoice_id}/action/{action}"
    else:
        url = base_url.rstrip("/") + API_BASE_PATH + f"/invoices/action/{action}"
    hdrs = headers.copy()
    hdrs["Content-Type"] = ACTION_CONTENT_TYPE
    resp = requests.post(url, json=action_payload, auth=auth, headers=hdrs, timeout=60)
    try:
        data = resp.json()
    except ValueError:
        resp.raise_for_status()
    if not resp.ok:
        raise RuntimeError(f"Action {action} failed: {resp.status_code} {resp.text}")
    return data

 

 

def get_invoice_self_href(created_response: dict, base_url: str) -> Optional[str]:
    """
    Oracle responses commonly include links with 'rel' and 'href' or a self property.
    Try to extract a usable self href or invoicesUniqID.
    """
    # Example locations to look:
    # - created_response['links'] -> list of { 'rel': 'self', 'href': '...' }
    # - created_response.get('items', [])[0].get('links') in collection responses
    # - created_response.get('invoicesUniqID') / created_response.get('href')
    # We'll check common shapes.
    if isinstance(created_response, dict):
        links = created_response.get("links") or created_response.get("_links")
        if isinstance(links, list):
            for l in links:
                if l.get("rel") == "self" and l.get("href"):
                    return l.get("href")
        # sometimes wrapped in items:
        items = created_response.get("items")
        if isinstance(items, list) and len(items) > 0:
            first = items[0]
            links = first.get("links") or first.get("_links")
            if isinstance(links, list):
                for l in links:
                    if l.get("rel") == "self" and l.get("href"):
                        return l.get("href")
        # fallback: if invoicesUniqID is present, build URL
        uniq = created_response.get("invoicesUniqID")
        if uniq:
            return base_url.rstrip("/") + API_BASE_PATH + f"/invoices/{uniq}"
    return None

 

 

def main():
    parser = argparse.ArgumentParser(description="Create an AP invoice in Oracle Fusion using REST API.")
    parser.add_argument("--input", "-i", help="Invoice JSON file (or '-' for stdin)", required=True)
    parser.add_argument("--base", "-b", help="Base URL for Fusion instance, e.g. https://myinst.fa.us2.oraclecloud.com", required=True)
    parser.add_argument("--auth", choices=["basic", "token"], default="basic", help="Auth mode")
    parser.add_argument("--user", help="Username (for basic auth)")
    parser.add_argument("--pass", dest="password", help="Password (for basic auth)")
    parser.add_argument("--token", help="Bearer token (for token auth)")
    parser.add_argument("--attach", help="Path to attachment file to include (optional)", default=None)
    parser.add_argument("--attachment-description", help="Attachment description", default="Invoice attachment")
    parser.add_argument("--generate-distributions", action="store_true", help="Call generateDistributions after create")
    parser.add_argument("--validate", action="store_true", help="Call validateInvoice after create")
    args = parser.parse_args()

 

    # load invoice JSON
    if args.input == "-":
        invoice_payload = json.load(sys.stdin)
    else:
        with open(args.input, "r", encoding="utf-8") as f:
            invoice_payload = json.load(f)

 

    auth = None
    headers = build_headers(args.auth, args.token)
    if args.auth == "basic":
        if not args.user or not args.password:
            parser.error("--user and --pass required for basic auth")
        auth = HTTPBasicAuth(args.user, args.password)
    elif args.auth == "token":
        # token already added to headers
        auth = None

 

    # Create invoice
    print("Creating invoice...")
    created = create_invoice(args.base, invoice_payload, auth, headers)
    print("Create response (raw):")
    print(json.dumps(created, indent=2))

 

    # extract invoice self href for child operations
    invoice_href = None
    # helper inline extraction (simple)
    if isinstance(created, dict):
        # direct href
        if created.get("href"):
            invoice_href = created.get("href")
        else:
            # try links list
            links = created.get("links")
            if isinstance(links, list):
                for l in links:
                    if l.get("rel") == "self" and l.get("href"):
                        invoice_href = l.get("href")
                        break
            # try items[0].links
            if not invoice_href:
                items = created.get("items")
                if isinstance(items, list) and len(items) > 0:
                    first = items[0]
                    lks = first.get("links") or first.get("_links")
                    if isinstance(lks, list):
                        for l in lks:
                            if l.get("rel") == "self" and l.get("href"):
                                invoice_href = l.get("href")
                                break

 

    if invoice_href:
        print(f"Invoice resource located at: {invoice_href}")
    else:
        print("Warning: Could not locate invoice self link in create response. You may need to query invoices collection to find the created invoice.")

 

    # optional: upload attachment
    if args.attach and invoice_href:
        print(f"Uploading attachment {args.attach} ...")
        with open(args.attach, "rb") as f:
            content = f.read()
        b64 = base64.b64encode(content).decode("ascii")
        attachment_payload = {
            "FileContents": b64,
            "FileName": args.attach.split("/")[-1],
            "Description": args.attachment_description,
            # optionally set UploadedFileContentType etc
        }
        uploaded = upload_attachment(args.base, invoice_href, attachment_payload, auth, headers)
        print("Attachment upload response:")
        print(json.dumps(uploaded, indent=2))

 

    # optional: generate distributions
    if args.generate_distributions:
        print("Calling generateDistributions action...")
        try:
            uniq_id = created.get("InvoiceId", created.get("invoicesUniqID"))
            gd_resp = post_action(args.base, "generateDistributions", {}, auth, headers, invoice_id=uniq_id)
            print("Generate distributions response:")
            print(json.dumps(gd_resp, indent=2))
        except Exception as e:
            print("Generate distributions failed:", e)



    # optional: validate
    if args.validate:
        print("Calling validateInvoice action...")
        try:
            uniq_id = created.get("InvoiceId", created.get("invoicesUniqID"))
            val_resp = post_action(args.base, "validateInvoice", {}, auth, headers, invoice_id=uniq_id)
            print("Validate response:")
            print(json.dumps(val_resp, indent=2))
        except Exception as e:
            print("Validate failed:", e)



    print("Done.")


if __name__ == "__main__":
    main()