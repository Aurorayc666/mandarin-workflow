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

load_dotenv(override=True)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
MS_CLIENT_ID   = os.environ.get("MS_CLIENT_ID")
RECAP_TO       = os.environ.get("RECAP_TO", "aurorahu.mandarin@gmail.com")
RECAP_BCC      = os.environ.get("RECAP_BCC")

NOTEBOOK_NAME = os.environ.get("NOTEBOOK_NAME", "Mandarin_Class")


def add_semester_argument(parser) -> None:
    """Add the shared --semester option (a one-off override of SEMESTER in .env)."""
    parser.add_argument(
        "--semester",
        help='One-off override, e.g. --semester "Spring 2027". '
        "Normally the semester comes from SEMESTER in .env.",
    )


def resolve_semester(cli_value: str | None) -> str:
    """Return the semester to work on: --semester if given, else SEMESTER from .env."""
    semester = (cli_value or os.environ.get("SEMESTER", "")).strip()
    if not semester:
        raise SystemExit(
            'ERROR: no semester set. Add SEMESTER=Fall 2026 (for example) to .env, '
            'or pass --semester "Fall 2026".'
        )
    print(f"Semester: {semester}")
    return semester
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
        try:
            resp = requests.request(method, url, headers=headers, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if attempt == 4:
                raise
            print(f"  connection error ({type(exc).__name__}); retrying in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
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


# Handout pages (from generate_handouts.py) live in the "<semester> Class Plans"
# section alongside the lesson plans, titled e.g. "Class 1-5 - Study Guide".
# They start with "Class N" too, so anything that counts or looks up lesson
# plans must skip them.
# Matches exactly the titles generate_handouts.py writes ("Class 1-5 - Study
# Guide", "Class 3 - Homework"), plus the older per-semester style ("Spring
# 2026 — Vocabulary Table" / Sentence Table / Grammar Summary / Homework Packet),
# so a lesson plan like "Class 7 — Doing Homework" is not mistaken for one.
_HANDOUT_TITLE_RE = re.compile(
    r"^(?:class\s+\d+(?:-\d+)?\s+-\s+(?:study guide|homework)"
    r"|.+\s[—–-]\s(?:vocabulary table|sentence table|grammar summary|homework packet))$",
    re.IGNORECASE,
)


def is_handout_title(title: str | None) -> bool:
    """True for handout pages like 'Class 1-5 - Study Guide' / 'Class 3 - Homework'."""
    return bool(_HANDOUT_TITLE_RE.match((title or "").strip()))


def read_section_pages(token: str, notebook_id: str, section_name: str) -> tuple[str, str]:
    """Read all pages from a single named section, sorted ascending by modification time.

    Handout pages are skipped: they only repeat the lesson plans and would bloat
    the Gemini context.

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
        pages.extend(
            p for p in data.get("value", []) if not is_handout_title(p.get("title"))
        )
        url = data.get("@odata.nextLink")

    pages.sort(key=lambda p: p.get("lastModifiedDateTime", ""))
    print(f"Reading {len(pages)} page(s) from section {section_name!r}...")
    chunks: list[str] = []
    for page in pages:
        title = page.get("title") or "(untitled)"
        text = _page_text(token, page["id"])
        chunks.append(f"# {title}\n{text}")
    return "\n\n---\n\n".join(chunks), section_id


_SEASON_ORDER = {"spring": 0, "summer": 1, "fall": 2, "winter": 3}


def _semester_sort_key(semester: str) -> tuple:
    """Sort 'Spring 2026' < 'Summer 2026' < 'Fall 2026' < 'Winter 2026' < 'Spring 2027'."""
    parts = semester.lower().split()
    year = next((int(p) for p in parts if p.isdigit()), 9999)
    season = next((_SEASON_ORDER[p] for p in parts if p in _SEASON_ORDER), 9)
    return (year, season, semester.lower())


def read_past_handouts(token: str, notebook_id: str, exclude_semester: str) -> str:
    """Read every past semester's handout pages — the record of what was actually
    covered — for use as syllabus context.

    Handouts live either in a "<semester> Handouts" section (older semesters) or
    as handout-titled pages inside "<semester> Class Plans" (current layout).
    Homework pages are skipped (they only practise the study-guide material), as
    is the semester being planned. Warns when a semester has lesson plans for
    classes its handouts don't cover yet, since those classes would be missed.
    """
    sections = graph_get(
        f"{GRAPH_BASE}/me/onenote/notebooks/{notebook_id}/sections?$select=id,displayName",
        token,
    ).get("value", [])

    by_semester: dict[str, list[tuple[str, str]]] = {}   # semester -> [(title, page_id)]
    covered_upto: dict[str, int] = {}                     # semester -> last class in handouts
    lesson_plans: dict[str, int] = {}                     # semester -> lesson plan count
    for sec in sections:
        name = sec["displayName"]
        lower = name.lower()
        if lower.endswith(" handouts"):
            semester, handout_section = name[: -len(" handouts")], True
        elif lower.endswith(" class plans"):
            semester, handout_section = name[: -len(" class plans")], False
        else:
            continue
        if semester.strip().lower() == exclude_semester.strip().lower():
            continue

        url = f"{GRAPH_BASE}/me/onenote/sections/{sec['id']}/pages?$select=id,title&$top=100"
        while url:
            data = graph_get(url, token)
            for page in data.get("value", []):
                title = page.get("title") or ""
                if not handout_section:
                    if not is_handout_title(title):
                        if re.match(r"^class\s+\d+", title, re.IGNORECASE):
                            lesson_plans[semester] = lesson_plans.get(semester, 0) + 1
                        continue
                m = re.match(r"^class\s+(?:\d+-)?(\d+)\b", title, re.IGNORECASE)
                if m:
                    covered_upto[semester] = max(covered_upto.get(semester, 0), int(m.group(1)))
                if "homework" in title.lower():
                    continue
                by_semester.setdefault(semester, []).append((title, page["id"]))
            url = data.get("@odata.nextLink")

    for semester, n_plans in sorted(lesson_plans.items(), key=lambda kv: _semester_sort_key(kv[0])):
        upto = covered_upto.get(semester)
        if semester not in by_semester:
            print(
                f"WARNING: {semester} has {n_plans} lesson plans but no handouts, so none "
                "of it will count as covered."
            )
        elif upto is not None and n_plans > upto:
            print(
                f"WARNING: {semester} has {n_plans} lesson plans but handouts only cover "
                f"Classes 1-{upto}. Classes {upto + 1}-{n_plans} won't count as covered — "
                f'run: python generate_handouts.py --semester "{semester}"'
            )

    chunks: list[str] = []
    for semester in sorted(by_semester, key=_semester_sort_key):
        pages = by_semester[semester]
        print(f"Reading {len(pages)} handout page(s) for {semester}...")
        texts = [f"# {title}\n{_page_text(token, pid)}" for title, pid in pages]
        chunks.append(f"=== {semester} handouts ===\n" + "\n\n---\n\n".join(texts))
    return "\n\n".join(chunks)


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
    """Count only lesson-plan pages titled 'Class N …' — ignores handouts and other pages."""
    count = 0
    url = f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages?$select=id,title&$top=100"
    while url:
        data = graph_get(url, token)
        for page in data.get("value", []):
            title = page.get("title") or ""
            if re.match(r"^class\s+\d+", title, re.IGNORECASE) and not is_handout_title(title):
                count += 1
        url = data.get("@odata.nextLink")
    return count


def read_class_plan_page(token: str, section_id: str, class_num: int) -> str:
    """Read only the single page titled 'Class {class_num} ...' from a section.

    Returns the page text prefixed with its title. Exits if no matching page exists.
    """
    pattern = re.compile(rf"^class\s+{class_num}\b", re.IGNORECASE)
    url = f"{GRAPH_BASE}/me/onenote/sections/{section_id}/pages?$select=id,title&$top=100"
    while url:
        data = graph_get(url, token)
        for page in data.get("value", []):
            title = page.get("title") or ""
            if pattern.match(title) and not is_handout_title(title):
                text = _page_text(token, page["id"])
                return f"# {page['title']}\n{text}"
        url = data.get("@odata.nextLink")
    sys.exit(f"ERROR: no page titled 'Class {class_num} ...' found in section.")


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
# Student copies (Word + PDF)
# --------------------------------------------------------------------------- #

FOR_STUDENTS_DIR = os.path.join(HERE, "For_Students")

_STUDENT_DOC_CSS = """
body  { font-family: Calibri, "Microsoft YaHei", "SimSun", sans-serif; font-size: 11pt; }
h1    { font-size: 20pt; color: #1F3864; }
h2    { font-size: 15pt; color: #2F5496; margin-top: 18pt; }
h3    { font-size: 12.5pt; color: #2F5496; }
table { border-collapse: collapse; margin: 6pt 0; }
th, td { border: 1px solid #8EA9DB; padding: 3pt 7pt; vertical-align: top; }
th    { background: #D9E2F3; }
"""


_BLOCK_START_RE = re.compile(r"^\s*(?:[*+-]\s|\d+[.)]\s|\|)")


def md_to_html(md_text: str) -> str:
    """Convert Gemini's Markdown to HTML (for OneNote and student copies).

    Gemini often puts a list or table directly under a line like
    "**New Vocabulary**" with no blank line between them, and Python-Markdown
    then renders the whole thing as one run-on paragraph. Likewise a heading
    right under a table gets swallowed into the table as an extra row. Insert
    the missing blank line before any list/table that starts right after
    ordinary text, and after a table that's followed directly by other text.
    """
    import markdown as md_lib

    out: list[str] = []
    prev = ""
    for line in md_text.splitlines():
        starts_block = (
            _BLOCK_START_RE.match(line)
            and prev.strip()
            and not _BLOCK_START_RE.match(prev)
            and not prev.startswith((" ", "\t"))
        )
        ends_table = (
            prev.lstrip().startswith("|")
            and line.strip()
            and not line.lstrip().startswith("|")
        )
        if starts_block or ends_table:
            out.append("")
        out.append(line)
        prev = line
    return md_lib.markdown("\n".join(out), extensions=["tables", "fenced_code"])


def strip_markdown_section(md_text: str, heading: str) -> str:
    """Remove a '## <heading>' section (up to the next '## ' heading) from Markdown."""
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$.*?(?=^##\s|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    return pattern.sub("", md_text).lstrip()


def export_for_students(md_text: str, title: str) -> None:
    """Save a student copy of a generated document as For_Students/<title>.docx and .pdf.

    Converts the Markdown to HTML and lets Microsoft Word (run hidden, in its own
    instance so the teacher's open documents are untouched) save it as Word and
    PDF, so Chinese characters, Pinyin tone marks and tables render properly.
    Never raises: on failure it prints a warning, since the content is already
    saved locally and in OneNote.
    """
    os.makedirs(FOR_STUDENTS_DIR, exist_ok=True)
    safe = re.sub(r'[<>:"/\\|?*]', "-", title).strip()
    docx_path = os.path.join(FOR_STUDENTS_DIR, f"{safe}.docx")
    pdf_path  = os.path.join(FOR_STUDENTS_DIR, f"{safe}.pdf")
    html_path = os.path.join(FOR_STUDENTS_DIR, f"~{safe}.tmp.html")

    body = md_to_html(md_text)
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            f"<title>{title}</title><style>{_STUDENT_DOC_CSS}</style></head>"
            f"<body><h1>{title}</h1>\n{body}</body></html>"
        )

    word = doc = None
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(html_path, ConfirmConversions=False, ReadOnly=True)
        doc.ActiveWindow.View.Type = 3          # print layout, not web layout
        doc.SaveAs2(docx_path, FileFormat=16)   # wdFormatDocumentDefault (.docx)
        doc.SaveAs2(pdf_path, FileFormat=17)    # wdFormatPDF
        print(f"Saved student copies: For_Students\\{safe}.docx and .pdf")
    except Exception as exc:
        print(
            f"WARNING: couldn't create the Word/PDF student copy of {title!r} ({exc}). "
            "If that file is open in Word or a PDF viewer, close it and re-run."
        )
    finally:
        try:
            if doc is not None:
                doc.Close(SaveChanges=False)
            if word is not None:
                word.Quit()
        except Exception:
            pass
        try:
            os.remove(html_path)
        except OSError:
            pass


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


def _retry_delay_seconds(exc) -> float | None:
    """Pull the server-suggested retry delay (seconds) out of a Gemini 429's details."""
    try:
        for d in exc.details.get("error", {}).get("details", []):
            if str(d.get("@type", "")).endswith("RetryInfo"):
                delay = d.get("retryDelay", "")
                if delay.endswith("s"):
                    return float(delay[:-1])
    except Exception:
        pass
    return None


def _generate(prompt: str) -> str:
    """Run one Gemini call, retrying with backoff on 503/overload errors and on
    429 rate-limit errors (honoring the API's own suggested retry delay)."""
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
        except genai_errors.ClientError as exc:
            if exc.code != 429 or attempt == 5:
                raise
            wait = _retry_delay_seconds(exc) or backoff
            print(f"  Gemini rate limit (429) hit; retrying in {wait:.0f}s...")
            time.sleep(wait + 2)
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
        resp = service.users().messages().list(**kwargs).execute(num_retries=5)
        messages = resp.get("messages", [])
        if not messages:
            break
        for msg in messages:
            full = service.users().messages().get(
                userId="me", id=msg["id"], format="full"
            ).execute(num_retries=5)
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
    """Fetch sent or drafted Gmail messages with 'recap' in the subject.

    Recaps created by generate_next_class_plan.py sit as drafts until the
    teacher sends them, so both in:sent and in:drafts must be searched.
    """
    chunks = _fetch_messages(service, "subject:recap (in:sent OR in:drafts)", max_results)
    print(f"Read {len(chunks)} recap email(s).")
    return "\n\n---\n\n".join(chunks)


def read_class_recap_emails(service, class_num: int, max_results: int = 100) -> str:
    """Fetch sent or drafted recap emails whose subject references the given class number only."""
    # Narrow the search server-side so only this class's recaps are downloaded;
    # fetching every recap per class blew through Gmail's per-minute quota.
    query = f'subject:recap subject:"class {class_num}" (in:sent OR in:drafts)'
    chunks = _fetch_messages(service, query, max_results)
    pattern = re.compile(rf"class\s+{class_num}\b", re.IGNORECASE)
    matched = [c for c in chunks if pattern.search(c.splitlines()[0] if c else "")]
    print(f"Read {len(matched)} recap email(s) for Class {class_num}.")
    return "\n\n---\n\n".join(matched)
