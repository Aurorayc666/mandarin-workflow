"""generate_next_class_plan.py — generate the next Mandarin class plan and recap.

Reads all past class plans from every OneNote section named like 'class plans',
reads the current semester's syllabus (if available locally), and reads all sent
Gmail. Uses Gemini to generate the next class plan. After interactive review,
saves it to OneNote and creates a recap email draft in Gmail.

Run:
  python generate_next_class_plan.py           # interactive review
  python generate_next_class_plan.py --auto    # accept first draft automatically
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime

import markdown

from utils import (
    HERE,
    RECAP_BCC,
    RECAP_TO,
    _generate,
    count_class_plan_pages,
    create_draft,
    create_onenote_page,
    find_notebook,
    find_section,
    gmail_authenticate,
    graph_authenticate,
    read_all_class_plan_sections,
    read_sent_gmail,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Target section in OneNote where new class plans are saved.
LESSON_PLAN_SECTION = os.environ.get("LESSON_PLAN_SECTION", "Summer 2026 Class Plans")

# Local syllabus file produced by generate_syllabus.py.
# Update this path when moving to a new semester.
SYLLABUS_FILE = os.path.join(HERE, "summer_2026_syllabus.md")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _extract_plan_topic(plan_md: str) -> str:
    """Return a short topic string from the first # heading in the lesson plan."""
    for prefix_len, marker in ((2, "# "), (3, "## ")):
        for line in plan_md.splitlines():
            stripped = line.strip()
            if stripped.startswith(marker):
                topic = stripped[prefix_len:].strip()
                for lp in ("Lesson Plan:", "Lesson Plan —", "Lesson Plan -", "Lesson Plan –"):
                    if topic.lower().startswith(lp.lower()):
                        topic = topic[len(lp):].strip()
                # Strip leading semester labels like "Summer 2026 " or "Fall 2025 - "
                topic = re.sub(
                    r"^(spring|summer|fall|winter)\s+\d{4}\s*[-—–]?\s*",
                    "",
                    topic,
                    flags=re.IGNORECASE,
                ).strip()
                if topic:
                    return topic
    return "Next Class"


def _read_local_syllabus() -> str:
    """Return the contents of the local syllabus file, or empty string if absent."""
    if os.path.exists(SYLLABUS_FILE):
        with open(SYLLABUS_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    return ""


# --------------------------------------------------------------------------- #
# Generators
# --------------------------------------------------------------------------- #

def generate_lesson_plan(
    all_notes: str,
    syllabus_text: str = "",
    sent_gmail_text: str = "",
    previous_draft: str | None = None,
    feedback: str | None = None,
) -> str:
    """Generate (or revise) a Markdown lesson plan for the next class."""
    prompt = (
        "You are an experienced Mandarin Chinese teacher. Below are all past class "
        "plans and, where available, a course syllabus and past email recaps. "
        "Based on this context, write a lesson plan in **Markdown** for the NEXT class.\n\n"
        "Do not re-teach vocabulary or grammar that already appears in the class plans "
        "below or in the sent recap emails.\n\n"
        "Begin with a single top-level Markdown heading (# Class N — Topic) that "
        "states the class number and a short topic name, then structure the plan "
        "with exactly these seven `##` sections, in this order, using these exact "
        "headings verbatim and nothing else on the heading line (put any "
        "instructions in the section body, never in the heading itself):\n"
        "## Learning Objectives\n"
        "## Review of Previously Covered Material\n"
        "## New Vocabulary\n"
        "## Grammar Points\n"
        "## Classroom Activities\n"
        "## Homework\n"
        "## Recommended YouTube Videos\n\n"
        "For 'New Vocabulary', use a Hanzi | Pinyin | English table.\n"
        "For 'Grammar Points', give the pattern in Hanzi/Pinyin/English with 2–3 "
        "example sentences each.\n"
        "For 'Recommended YouTube Videos', find one real YouTube video relevant to "
        "the new vocabulary and one real YouTube video relevant to the new grammar "
        "point; for each, provide the video title, channel name, and full URL.\n\n"
        "Whenever you write a Chinese sentence anywhere in the plan (vocabulary tables, "
        "grammar examples, activities, homework, etc.), put its Pinyin (with tone marks) "
        "immediately after it.\n\n"
        "Return only the Markdown, with no surrounding code fences.\n\n"
        "=== PAST CLASS PLANS (OneNote — all sections) ===\n"
        f"{all_notes}"
    )
    if syllabus_text:
        prompt += (
            "\n\n=== COURSE SYLLABUS (scope guide — follow this progression) ===\n"
            f"{syllabus_text}"
        )
    if sent_gmail_text:
        prompt += (
            "\n\n=== SENT GMAIL (past recaps and communications) ===\n"
            f"{sent_gmail_text}"
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


def review_lesson_plan(
    all_notes: str,
    syllabus_text: str = "",
    sent_gmail_text: str = "",
    auto: bool = False,
) -> str:
    """Generate a lesson plan and let the teacher approve or refine it interactively."""
    draft = generate_lesson_plan(all_notes, syllabus_text=syllabus_text, sent_gmail_text=sent_gmail_text)
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
        draft = generate_lesson_plan(
            all_notes,
            syllabus_text=syllabus_text,
            sent_gmail_text=sent_gmail_text,
            previous_draft=draft,
            feedback=response,
        )


_COPIED_PLAN_SECTIONS = (
    ("grammar", "Grammar Points"),
    ("activit", "Classroom Activities"),
    ("homework", "Homework"),
    ("youtube", "Recommended YouTube Videos"),
)


def _extract_section(plan_md: str, keyword: str) -> str:
    """Return the Markdown body under the first heading containing `keyword`
    (case-insensitive), up to the next heading of the same or shallower level."""
    lines = plan_md.splitlines()
    start = level = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") and keyword.lower() in stripped.lower():
            level = len(stripped) - len(stripped.lstrip("#"))
            start = i + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("#"):
            this_level = len(stripped) - len(stripped.lstrip("#"))
            if this_level <= level:
                end = j
                break
    return "\n".join(lines[start:end]).strip()


def _copied_plan_sections_html(plan_md: str) -> str:
    """Render the Grammar/Activities/Homework/YouTube sections straight from the
    already-generated lesson plan, unchanged, as HTML."""
    chunks: list[str] = []
    for keyword, title in _COPIED_PLAN_SECTIONS:
        section_md = _extract_section(plan_md, keyword)
        if not section_md:
            continue
        section_html = markdown.markdown(section_md, extensions=["tables", "fenced_code"])
        chunks.append(f"<h3>{title}</h3>\n{section_html}")
    return "\n".join(chunks)


def generate_recap_email(plan_md: str) -> tuple[str, str]:
    """Generate the greeting/covered/vocabulary opening and the closing line as two
    HTML fragments, sourced ONLY from today's just-generated lesson plan. The
    Grammar/Activities/Homework/YouTube sections are NOT generated here — they're
    copied verbatim from the same lesson plan in main()."""
    prompt = (
        "You are a Mandarin Chinese teacher writing a brief recap email to your student "
        "right after today's class. Below is TODAY'S lesson plan — the class that was "
        "just taught. Using ONLY this lesson plan, write TWO separate HTML fragments "
        "for the BODY of the email:\n\n"
        "FRAGMENT 1 (the opening):\n"
        "1. A short, warm greeting (1–2 sentences).\n"
        "2. <h3>What We Covered</h3> — a concise <ul> bullet list of the main topics "
        "from today's lesson plan below.\n"
        "3. <h3>Vocabulary</h3> — an HTML <table> with columns: Hanzi | Pinyin | English "
        "(include a header row), listing the New Vocabulary from today's lesson plan below.\n\n"
        "FRAGMENT 2 (the closing):\n"
        "A one-sentence encouraging closing.\n\n"
        "Keep the whole email short and scannable — prefer bullets over paragraphs.\n"
        "Whenever you write a Chinese sentence anywhere in the email, put its Pinyin "
        "(with tone marks) immediately after it.\n"
        "Return ONLY raw HTML for the two fragments (no <html>, <head>, or <body> tags, "
        "no code fences), separated by a line containing exactly this marker: "
        "<!--CLOSING-->\n\n"
        "=== TODAY'S LESSON PLAN ===\n"
        f"{plan_md}"
    )
    raw = _generate(prompt)
    if "<!--CLOSING-->" in raw:
        opening_html, closing_html = raw.split("<!--CLOSING-->", 1)
    else:
        opening_html, closing_html = raw, ""
    return opening_html.strip(), closing_html.strip()


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the next Mandarin class plan and recap email."
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Skip the interactive review and accept the first lesson plan.",
    )
    args = parser.parse_args()

    # 1. Authenticate and verify the target section exists.
    token = graph_authenticate()
    notebook_id = find_notebook(token)
    section_id = find_section(token, notebook_id, LESSON_PLAN_SECTION)
    if section_id is None:
        sys.exit(
            f"ERROR: section {LESSON_PLAN_SECTION!r} does not exist in OneNote. "
            "Run generate_syllabus.py first to create it."
        )

    # 2. Read all class plan sections from OneNote.
    all_notes = read_all_class_plan_sections(token, notebook_id)

    # 3. Load local syllabus and all sent Gmail.
    syllabus_text = _read_local_syllabus()
    if syllabus_text:
        print(f"Loaded syllabus from {SYLLABUS_FILE}")
    else:
        print(f"No syllabus found at {SYLLABUS_FILE} — proceeding without it.")

    print("Reading sent Gmail messages...")
    gmail_service = gmail_authenticate()
    sent_gmail_text = read_sent_gmail(gmail_service)

    # 4. Generate, review, and save the lesson plan.
    print("Generating next lesson plan with Gemini...")
    plan_md = review_lesson_plan(
        all_notes,
        syllabus_text=syllabus_text,
        sent_gmail_text=sent_gmail_text,
        auto=args.auto,
    )
    topic    = _extract_plan_topic(plan_md)
    date_str = datetime.now().strftime("%Y-%m-%d")

    # If the model already numbered the class (e.g. "Class 1 - Greetings"), use it
    # directly; otherwise fall back to the section page count.
    if re.match(r"^class\s+\d+", topic, re.IGNORECASE):
        page_title = topic
    else:
        class_num  = count_class_plan_pages(token, section_id) + 1
        page_title = f"Class {class_num} — {topic}"

    slug = re.sub(r"[^\w\s-]", "", page_title).strip().replace(" ", "_").lower()
    lesson_plan_file = os.path.join(HERE, f"{slug}_{date_str}.md")

    with open(lesson_plan_file, "w", encoding="utf-8") as fh:
        fh.write(plan_md)
    print(f"Saved lesson plan to {lesson_plan_file}")

    plan_html = markdown.markdown(plan_md, extensions=["tables", "fenced_code"])
    create_onenote_page(token, section_id, page_title, plan_html)

    # 5. Generate recap email and create Gmail draft.
    print("Generating recap email with Gemini...")
    opening_html, closing_html = generate_recap_email(plan_md)
    copied_html   = _copied_plan_sections_html(plan_md)
    recap_html    = "\n".join(part for part in (opening_html, copied_html, closing_html) if part)
    recap_subject = "Recap " + re.sub(r"\s*[-—–]\s*", ": ", page_title, count=1)
    create_draft(gmail_service, RECAP_TO, recap_subject, recap_html, bcc=RECAP_BCC)

    print("\nDone. Review the new OneNote page and your Gmail draft.")


if __name__ == "__main__":
    main()
