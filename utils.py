"""Shared utilities for the Mandarin class workflow scripts.

Provides authenticated clients, OneNote/Graph helpers, Gemini wrappers, and
Gmail helpers used by generate_next_class_plan.py, generate_syllabus.py, and
generate_handouts.py.
"""

from __future__ import annotations

import base64
import os
import re
import sys
import time
from datetime import datetime
from email.message import EmailMessage

import msal
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.genai import types
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MS_CLIENT_ID   = os.environ.get("MS_CLIENT_ID")
RECAP_TO       = os.environ.get("RECAP_TO", "aurorahu.mandarin@gmail.com")
RECAP_BCC      = os.environ.get("RECAP_BCC")

NOTEBOOK_NAME = os.environ.get("NOTEBOOK_NAME", "Mandarin_Class")
MODEL         = "gemini-2.5-flash"

AUTHORITY    = "https://login.microsoftonline.com/common"
GRAPH_SCOPES = ["Notes.ReadWrite"]
GRAPH_BASE   = "https://graph.microsoft.com/v1.0"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly",
]

HERE                  = os.path.dirname(os.path.abspath(__file__))
GMAIL_CREDENTIALS_FILE = os.path.join(HERE, "credentials.json")
GMAIL_TOKEN_FILE      = os.path.join(HERE, "gmail_token.json")
MS_TOKEN_CACHE_FILE   = os.path.join(HERE, "ms_token_cache.json")
GEM_INSTRUCTIONS_FILE = os.path.join(HERE, "gem_instructions.txt")


# --------------------------------------------------------------------------- #
# Microsoft Graph / OneNote
# --------------------------------------------------------------------------- #

def graph_authenticate() -> str:
    """Authenticate to Microsoft Graph and return an access token."""
    if not MS_CLIENT_ID:
        sys.exit("ERROR: MS_CLIENT_ID is not set (Azure app registration client id).")

    cache = msal.SerializableTokenCache()
    if os.path.exists(MS_TOKEN_CACHE_FILE):
        with open(MS_TOKEN_CACHE_FILE, "r", encoding="utf-8") as fh:
            cache.deserialize(fh.read())

    app = msal.PublicClientApplication(
        MS_CLIENT_ID, authority=AUTHORITY, token_cache=cache
    )

    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])

    if not result:
        print("Opening browser for Microsoft sign-in...")
        result = app.acquire_token_interactive(GRAPH_SCOPES)

    if cache.has_state_changed:
        with open(MS_TOKEN_CACHE_FILE, "w", encoding="utf-8") as fh:
            fh.write(cache.serialize())

    if "access_token" not in result:
        sys.exit(
            "ERROR: Microsoft authentication failed: "
            f"{result.get('error')}: {result.get('error_description')}"
        )
    return result["access_token"]


def _graph_request(method: str, url: str, token: str, **kwargs) -> requests.Response:
    """Call Graph with the bearer token, honoring 429 Retry-After with backoff."""
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    backoff = 2
    for attempt in range(5):
        resp = requests.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", backoff))
            print(f"  throttled (429); retrying in {wait}s...")
            time.sleep(wait)
            backoff = min(backoff * 2, 60)
        elif resp.status_code in (502, 503, 504):
            print(f"  transient {resp.status_code}; retrying in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        else:
            return resp
    return resp


def graph_get(url: str, token: str) -> dict:
    resp = _graph_request("GET", url, token)
    resp.raise_for_status()
    return resp.json()


def find_notebook(token: str) -> str:
    """Return the id of the notebook named NOTEBOOK_NAME."""
    data = graph_get(f"{GRAPH_BASE}/me/onenote/notebooks?$select=id,displayName", token)
    for nb in data.get("value", []):
        if nb.get("displayName") == NOTEBOOK_NAME:
            return nb["id"]
    available = ", ".join(nb.get("displayName", "?") for nb in data.get("value", []))
    sys.exit(
        f"ERROR: notebook {NOTEBOOK_NAME!r} not found. "
        f"Available notebooks: {available or '(none)'}. "
        "Set NOTEBOOK_NAME to match."
    )


def _page_text(token: str, page_id: str) -> str:
    """Fetch a page's HTML content and return it as plain text."""
    resp = _graph_request("GET", f"{GRAPH_BASE}/me/onenote/pages/{page_id}/content", token)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    return soup.get_text(separator="\n", strip=True)


def read_section_pages(token: str, notebook_id: str, section_name: str) -> tuple[str, str]:
    """Read all pages from a single named section, sorted ascending by modification time.

    Returns (concatenated_text, section_id). Exits if the section is not found.
    """
    sections = graph_get(
        f"{GRAPH_BASE}/me/onenote/notebooks/{notebook_id}/sections?$select=id,displayName",
        token,
    ).get("value", [])
    section = next((s for s in sections if s["displayName"] == section_name), None)
    if not section:
        available = ", ".join(s.get("displayName", "?") for s in sections)
        sys.exit(
            f"ERROR: section {section_name!r} not found. "
            f"Available: {available or '(none)'}."
        )
    section_id = section["id"]

    pages: list[dict] = []
    url = (
        f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages"
        "?$select=id,title,lastModifiedDateTime&$top=100"
    )
    while url:
        data = graph_get(url, token)
        pages.extend(data.get("value", []))
        url = data.get("@odata.nextLink")

    pages.sort(key=lambda p: p.get("lastModifiedDateTime", ""))
    print(f"Reading {len(pages)} page(s) from section {section_name!r}...")
    chunks: list[str] = []
    for page in pages:
        title = page.get("title") or "(untitled)"
        text = _page_text(token, page["id"])
        chunks.append(f"# {title}\n{text}")
    return "\n\n---\n\n".join(chunks), section_id


def read_all_class_plan_sections(token: str, notebook_id: str) -> str:
    """Read all pages from every section whose name contains 'class plans'."""
    sections = graph_get(
        f"{GRAPH_BASE}/me/onenote/notebooks/{notebook_id}/sections?$select=id,displayName",
        token,
    ).get("value", [])
    matching = [
        s for s in sections
        if "class plans" in s["displayName"].lower().replace("_", " ")
    ]
    if not matching:
        sys.exit("ERROR: No sections found with 'class plans' in the name.")
    chunks: list[str] = []
    for section in matching:
        text, _ = read_section_pages(token, notebook_id, section["displayName"])
        chunks.append(f"=== {section['displayName']} ===\n{text}")
    return "\n\n".join(chunks)


def find_section(token: str, notebook_id: str, name: str) -> str | None:
    """Return the id of a named section, or None if it doesn't exist."""
    sections = graph_get(
        f"{GRAPH_BASE}/me/onenote/notebooks/{notebook_id}/sections?$select=id,displayName",
        token,
    ).get("value", [])
    for s in sections:
        if s["displayName"] == name:
            return s["id"]
    return None


def count_class_plan_pages(token: str, section_id: str) -> int:
    """Count only pages whose title matches 'Class N …' — ignores other pages in the section."""
    count = 0
    url = f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages?$select=id,title&$top=100"
    while url:
        data = graph_get(url, token)
        for page in data.get("value", []):
            if re.match(r"^class\s+\d+", page.get("title") or "", re.IGNORECASE):
                count += 1
        url = data.get("@odata.nextLink")
    return count


def find_or_create_section(token: str, notebook_id: str, name: str) -> str:
    """Return the id of a named section, creating it if it doesn't exist."""
    sections = graph_get(
        f"{GRAPH_BASE}/me/onenote/notebooks/{notebook_id}/sections?$select=id,displayName",
        token,
    ).get("value", [])
    for s in sections:
        if s["displayName"] == name:
            return s["id"]
    print(f"Creating section {name!r}...")
    resp = _graph_request(
        "POST",
        f"{GRAPH_BASE}/me/onenote/notebooks/{notebook_id}/sections",
        token,
        headers={"Content-Type": "application/json"},
        json={"displayName": name},
    )
    if resp.status_code not in (200, 201):
        sys.exit(
            f"ERROR: failed to create section {name!r} ({resp.status_code}): {resp.text}"
        )
    return resp.json()["id"]


def create_onenote_page(token: str, section_id: str, title: str, html_body: str) -> None:
    """Create a new OneNote page in the given section."""
    created = datetime.now().astimezone().isoformat()
    page_html = (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "  <head>\n"
        f"    <title>{title}</title>\n"
        f'    <meta name="created" content="{created}" />\n'
        "  </head>\n"
        f"  <body>{html_body}</body>\n"
        "</html>"
    )
    resp = _graph_request(
        "POST",
        f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages",
        token,
        headers={"Content-Type": "text/html; charset=utf-8"},
        data=page_html.encode("utf-8"),
    )
    if resp.status_code != 201:
        sys.exit(f"ERROR: failed to create OneNote page ({resp.status_code}): {resp.text}")
    links = resp.json().get("links", {})
    url = links.get("oneNoteClientUrl", {}).get("href") or links.get(
        "oneNoteWebUrl", {}
    ).get("href", "(no link returned)")
    print(f"Created OneNote page: {url}")


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #

def _gemini_client() -> genai.Client:
    if not GEMINI_API_KEY:
        sys.exit("ERROR: GEMINI_API_KEY is not set.")
    return genai.Client(api_key=GEMINI_API_KEY)


def _gem_instructions() -> str | None:
    """Return the optional Gem system instruction, if gem_instructions.txt exists."""
    if os.path.exists(GEM_INSTRUCTIONS_FILE):
        with open(GEM_INSTRUCTIONS_FILE, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
        return text or None
    return None


def _generate(prompt: str) -> str:
    """Run one Gemini call with exponential backoff on 503/overload errors."""
    from google.genai import errors as genai_errors

    client = _gemini_client()
    config = None
    instructions = _gem_instructions()
    if instructions:
        config = types.GenerateContentConfig(system_instruction=instructions)

    backoff = 10
    for attempt in range(6):
        try:
            resp = client.models.generate_content(model=MODEL, contents=prompt, config=config)
            return _strip_code_fences(resp.text)
        except genai_errors.ServerError as exc:
            if attempt == 5:
                raise
            print(f"  Gemini unavailable ({exc}); retrying in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)


def _strip_code_fences(text: str) -> str:
    """Remove a leading/trailing ```lang fence if the model wrapped its output."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


# --------------------------------------------------------------------------- #
# Gmail
# --------------------------------------------------------------------------- #

def gmail_authenticate():
    """Authenticate to Gmail and return a built service, caching the token."""
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)

    if creds and creds.valid and not all(s in (creds.scopes or set()) for s in GMAIL_SCOPES):
        creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
        else:
            if not os.path.exists(GMAIL_CREDENTIALS_FILE):
                sys.exit(
                    f"ERROR: {GMAIL_CREDENTIALS_FILE} not found. Download your Gmail "
                    "OAuth client (Desktop app) and save it there. See README.md."
                )
            print("Opening browser for Google sign-in...")
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def create_draft(service, to: str, subject: str, html_body: str, bcc: str | None = None) -> None:
    """Create a Gmail draft with an HTML body."""
    message = EmailMessage()
    message["To"] = to
    if bcc:
        message["Bcc"] = bcc
    message["Subject"] = subject
    message.set_content("This recap is best viewed in an HTML-capable email client.")
    message.add_alternative(html_body, subtype="html")

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    draft = (
        service.users()
        .drafts()
        .create(userId="me", body={"message": {"raw": raw}})
        .execute()
    )
    print(f"Created Gmail draft (id: {draft['id']}) addressed to {to}.")


def _extract_email_text(payload: dict) -> str:
    """Recursively extract plain text from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    elif mime == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            html = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            return BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)
    for part in payload.get("parts", []):
        text = _extract_email_text(part)
        if text:
            return text
    return ""


def _fetch_messages(service, query: str, max_results: int) -> list[str]:
    """Fetch Gmail messages matching a query and return them as text chunks."""
    chunks: list[str] = []
    page_token = None
    while len(chunks) < max_results:
        batch = min(100, max_results - len(chunks))
        kwargs: dict = dict(userId="me", q=query, maxResults=batch)
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        messages = resp.get("messages", [])
        if not messages:
            break
        for msg in messages:
            full = service.users().messages().get(
                userId="me", id=msg["id"], format="full"
            ).execute()
            headers = {
                h["name"]: h["value"]
                for h in full.get("payload", {}).get("headers", [])
            }
            subject = headers.get("Subject", "(no subject)")
            date    = headers.get("Date", "")
            to      = headers.get("To", "")
            body    = _extract_email_text(full.get("payload", {}))
            chunks.append(f"Subject: {subject}\nDate: {date}\nTo: {to}\n\n{body}")
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return chunks


def read_sent_gmail(service, max_results: int = 500) -> str:
    """Fetch all sent Gmail messages and return them as concatenated plain text."""
    chunks = _fetch_messages(service, "in:sent", max_results)
    print(f"Read {len(chunks)} sent Gmail message(s).")
    return "\n\n---\n\n".join(chunks)


def read_recap_emails(service, max_results: int = 100) -> str:
    """Fetch sent Gmail messages with 'recap' in the subject."""
    chunks = _fetch_messages(service, "in:sent subject:recap", max_results)
    print(f"Read {len(chunks)} recap email(s).")
    return "\n\n---\n\n".join(chunks)
