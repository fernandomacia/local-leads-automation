"""API client for persisting and syncing leads.

Saves the pipeline output as a flat JSON file and, when ``API_URL`` is
configured, POSTs a nested representation to the external endpoint.
"""

import json
import requests
from urllib.parse import urlparse

from config import API_URL, API_KEY, SOCIAL_DOMAINS

_SOCIAL_FIELDS = tuple(SOCIAL_DOMAINS.keys())
_LOCATION_FIELDS = ("address", "zip_code", "city", "province")


def _serialize(lead: dict) -> dict:
    """Convert a flat lead dict to the nested API payload schema.

    The website is normalized to a bare hostname (no protocol, www, or path)
    and seo_issues is split from a pipe-delimited string into a list.
    """
    seo_issues = [i for i in lead.get("seo_issues", "").split("|") if i]
    raw_url = lead.get("website", "")
    host = urlparse(raw_url).netloc.lower().removeprefix("www.")
    return {
        "lead": lead.get("lead", ""),
        "website": host or raw_url,
        "maps_url": lead.get("maps_url", ""),
        "location": {f: lead.get(f, "") for f in _LOCATION_FIELDS},
        "contact": {
            "phone": lead.get("phone", ""),
            "email": lead.get("email", ""),
        },
        "social": {f: lead.get(f, "") for f in _SOCIAL_FIELDS},
        "analysis": {
            "cms": lead.get("cms", ""),
            "seo_score": lead.get("seo_score", 0),
            "seo_issues": seo_issues,
        },
        "outreach": {
            "subject": lead.get("subject", ""),
            "body": lead.get("body", ""),
        },
    }


def send(leads: list[dict]) -> None:
    """Persist leads locally and optionally sync to the remote API.

    Always writes ``data/leads.json`` as a flat list. The POST payload uses
    the nested schema from ``_serialize``; the local file retains the flat
    pipeline shape so the Streamlit app can read it directly.

    Args:
        leads: Flat lead dicts produced by the pipeline.
    """
    with open("data/leads.json", "w", encoding="utf-8") as f:
        json.dump(leads, f, ensure_ascii=False, indent=2)
    print("[+] Leads saved → data/leads.json")

    if not API_URL:
        print("[!] API_URL not configured — skipping API sync")
        return

    payload = [_serialize(lead) for lead in leads]
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    resp = requests.post(API_URL, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    print(f"[+] API sync: {len(payload)} leads sent → HTTP {resp.status_code}")
