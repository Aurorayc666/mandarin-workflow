"""Mandarin class workflow: OneNote -> Gemini -> Gmail.

Reads all past Mandarin lesson plans from a OneNote notebook (via the Microsoft
Graph API), asks the Gemini API to generate (1) a lesson plan for the next class
and (2) a warm, structured recap email, then:

  * saves the new lesson plan back into OneNote as a page (and a local
    ``next_lesson_plan.md`` copy), and
  * creates the recap as a draft in Gmail.

Run once to authorize (a browser opens for Microsoft and for Google); tokens are
cached locally so later runs are non-interactive.

See README.md for the one-time Azure / Google / Gemini setup.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from datetime import datetime
from email.message import EmailMessage

import markdown
import msal
import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Secrets / per-user values come from environment variables.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID")          # Azure app registration (public client)
RECAP_TO = os.environ.get("RECAP_TO")                  # recipient of the recap draft
RECAP_BCC = os.environ.get("RECAP_BCC")               # comma-separated BCC recipients

# Edit these to match your setup.
NOTEBOOK_NAME = os.environ.get("NOTEBOOK_NAME", "Mandarin_Class")
MODEL = "gemini-2.5-flash"

# Microsoft Graph.
AUTHORITY = "https://login.microsoftonline.com/common"  # personal + work/school
GRAPH_SCOPES = ["Notes.ReadWrite"]
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Gmail. `gmail.compose` is the minimal scope that allows creating drafts.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]

# Local files (relative to this script).
HERE = os.path.dirname(os.path.abspath(__file__))
LESSON_PLAN_FILE = os.path.join(HERE, "next_lesson_plan.md")
GMAIL_CREDENTIALS_FILE = os.path.join(HERE, "credentials.json")
GMAIL_TOKEN_FILE = os.path.join(HERE, "gmail_token.json")
MS_TOKEN_CACHE_FILE = os.path.join(HERE, "ms_token_cache.json")

# Optional: paste your Gemini "Gem" instructions into this file. If it exists,
# its contents are sent as the system instruction on every Gemini call, so the
# model behaves like your custom Mandarin Gem. (Gems themselves are not callable
# via the Gemini API — this replicates the Gem's instructions.)
GEM_INSTRUCTIONS_FILE = os.path.join(HERE, "gem_instructions.txt")

RECAP_SUBJECT = "Mandarin Class Recap"


# --------------------------------------------------------------------------- #
# Microsoft Graph / OneNote
# --------------------------------------------------------------------------- #

def graph_authenticate() -> str:
    """Authenticate to Microsoft Graph and return an access token.

    Uses MSAL's public-client interactive flow, caching tokens in
    ``ms_token_cache.json`` so subsequent runs are silent.
    """
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
    """Call Graph with the bearer token, honoring 429 Retry-After with backoff.

    Also retries on transient 5xx gateway errors (502, 503, 504).
    """
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
    """Return the id of the notebook named ``NOTEBOOK_NAME``."""
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


def read_lesson_plans(token: str, notebook_id: str) -> tuple[str, str]:
    """Read every page in the notebook (all sections).

    Returns ``(notes_text, latest_section_id)`` where ``latest_section_id`` is the
    section containing the most-recently-modified page (the write target for the
    new plan). Pages are fetched per-section to avoid the "too many sections"
    error on ``/me/onenote/pages``.
    """
    sections = graph_get(
        f"{GRAPH_BASE}/me/onenote/notebooks/{notebook_id}/sections?$select=id,displayName",
        token,
    ).get("value", [])
    if not sections:
        sys.exit(f"ERROR: notebook {NOTEBOOK_NAME!r} has no sections.")

    pages: list[dict] = []  # each: {id, title, lastModifiedDateTime, sectionId}
    for section in sections:
        section_id = section["id"]
        url = (
            f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages"
            "?$select=id,title,lastModifiedDateTime"
            "&$top=100&$orderby=lastModifiedDateTime desc"
        )
        while url:
            data = graph_get(url, token)
            for page in data.get("value", []):
                page["sectionId"] = section_id
                pages.append(page)
            url = data.get("@odata.nextLink")

    if not pages:
        sys.exit(f"ERROR: no pages found in notebook {NOTEBOOK_NAME!r}.")

    # Most-recently-modified page across the whole notebook -> write target.
    pages.sort(key=lambda p: p.get("lastModifiedDateTime", ""), reverse=True)
    latest_section_id = pages[0]["sectionId"]

    print(f"Reading {len(pages)} page(s) from notebook {NOTEBOOK_NAME!r}...")
    chunks: list[str] = []
    for page in pages:
        title = page.get("title") or "(untitled)"
        text = _page_text(token, page["id"])
        chunks.append(f"# {title}\n{text}")

    return "\n\n---\n\n".join(chunks), latest_section_id


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
    """Run one Gemini call, applying the Gem system instruction if configured."""
    client = _gemini_client()
    config = None
    instructions = _gem_instructions()
    if instructions:
        config = types.GenerateContentConfig(system_instruction=instructions)
    resp = client.models.generate_content(model=MODEL, contents=prompt, config=config)
    return _strip_code_fences(resp.text)


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


def generate_lesson_plan(
    notes_text: str, previous_draft: str | None = None, feedback: str | None = None
) -> str:
    """Generate (or revise) a Markdown lesson plan for the next class.

    If ``previous_draft`` and ``feedback`` are given, the model revises that draft
    according to the feedback instead of starting from scratch.
    """
    prompt = (
        "You are an experienced Mandarin Chinese teacher. Below are the notes and "
        "lesson plans from past classes with a student. Based on the progression "
        "you see, write a lesson plan in **Markdown** for the NEXT class.\n\n"
        "Structure it with these sections:\n"
        "1. Learning objectives\n"
        "2. Review of previously covered material\n"
        "3. New vocabulary and grammar (include Hanzi, Pinyin, and English)\n"
        "4. Classroom activities\n"
        "5. Homework\n\n"
        "Return only the Markdown, with no surrounding code fences.\n\n"
        "=== PAST NOTES ===\n"
        f"{notes_text}"
    )
    if previous_draft and feedback:
        prompt += (
            "\n\n=== PREVIOUS DRAFT ===\n"
            f"{previous_draft}\n\n"
            "=== REVISION REQUEST ===\n"
            "Revise the previous draft according to this feedback, keeping "
            "everything else that already works:\n"
            f"{feedback}"
        )
    return _generate(prompt)


def review_lesson_plan(notes_text: str, auto: bool = False) -> str:
    """Generate a lesson plan, then let the user approve or refine it in the terminal.

    Loops: shows the draft, accepts an empty line / "y" to approve, or any other
    text as refinement instructions to regenerate. With ``auto=True`` (or when
    stdin is not interactive, e.g. a scheduled run), the first draft is accepted
    without review.
    """
    draft = generate_lesson_plan(notes_text)
    if auto:
        print("(--auto: accepting the first lesson plan without review)")
        return draft
    while True:
        print("\n" + "=" * 70)
        print("PROPOSED LESSON PLAN")
        print("=" * 70)
        print(draft)
        print("=" * 70)
        try:
            response = input(
                "\nPress Enter (or 'y') to approve, "
                "or type what to change and press Enter: "
            ).strip()
        except EOFError:
            print("(no interactive input — approving automatically)")
            return draft
        if response == "" or response.lower() in ("y", "yes"):
            return draft
        print("\nRefining the lesson plan with your feedback...")
        draft = generate_lesson_plan(notes_text, previous_draft=draft, feedback=response)


def generate_recap_email(notes_text: str) -> str:
    """Generate a warm, structured HTML recap email body."""
    prompt = (
        "You are an experienced, encouraging Mandarin Chinese teacher writing a "
        "recap email to your student after class. Using the notes below, write the "
        "BODY of an HTML email that:\n"
        "- opens with a warm, personal greeting,\n"
        "- briefly summarizes what was covered in class,\n"
        "- includes an HTML <table> of the vocabulary covered with three columns: "
        "Hanzi, Pinyin, English (use a header row),\n"
        "- ends with an encouraging closing and a short note on what to practice.\n\n"
        "Return ONLY the HTML body content (you may use <p>, <h2>, <table>, etc.). "
        "Do not include <html>, <head>, or <body> tags, and no surrounding code "
        "fences.\n\n"
        "=== CLASS NOTES ===\n"
        f"{notes_text}"
    )
    return _generate(prompt)


# --------------------------------------------------------------------------- #
# Gmail
# --------------------------------------------------------------------------- #

def gmail_authenticate():
    """Authenticate to Gmail and return a built service, caching the token."""
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)

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
    # Plain-text fallback + HTML alternative.
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


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a Mandarin lesson plan and recap.")
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Skip the interactive review and accept the first lesson plan "
        "(for unattended/scheduled runs).",
    )
    args = parser.parse_args()

    if not RECAP_TO:
        sys.exit("ERROR: RECAP_TO is not set (recipient of the recap draft).")

    # 1. Read past lesson plans from OneNote.
    token = graph_authenticate()
    notebook_id = find_notebook(token)
    notes_text, section_id = read_lesson_plans(token, notebook_id)

    # 2. Generate the next lesson plan, review/refine it, then save it
    #    (local .md + OneNote page) once approved.
    print("Generating next lesson plan with Gemini...")
    plan_md = review_lesson_plan(notes_text, auto=args.auto)
    with open(LESSON_PLAN_FILE, "w", encoding="utf-8") as fh:
        fh.write(plan_md)
    print(f"Saved lesson plan to {LESSON_PLAN_FILE}")

    plan_html = markdown.markdown(plan_md, extensions=["tables", "fenced_code"])
    page_title = f"Lesson Plan — {datetime.now():%Y-%m-%d}"
    create_onenote_page(token, section_id, page_title, plan_html)

    # 3. Only after the lesson plan is confirmed: generate the recap and draft it.
    print("Generating recap email with Gemini...")
    recap_html = generate_recap_email(notes_text)
    service = gmail_authenticate()
    create_draft(service, RECAP_TO, RECAP_SUBJECT, recap_html, bcc=RECAP_BCC)

    print("\nDone. Review the new OneNote page and your Gmail draft.")


if __name__ == "__main__":
    main()
