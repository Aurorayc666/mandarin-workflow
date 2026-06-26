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
import sys
from datetime import datetime

import markdown

from utils import (
    HERE,
    RECAP_BCC,
    RECAP_TO,
    _generate,
    create_draft,
    create_onenote_page,
    find_notebook,
    find_or_create_section,
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

LESSON_PLAN_FILE = os.path.join(HERE, "next_lesson_plan.md")
RECAP_SUBJECT    = "Mandarin Class Recap"

# Local syllabus file produced by generate_syllabus.py.
# Update this path when moving to a new semester.
SYLLABUS_FILE = os.path.join(HERE, "summer_2026_syllabus.md")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

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
        "Structure the plan with these sections:\n"
        "1. Learning objectives\n"
        "2. Review of previously covered material\n"
        "3. New vocabulary and grammar (Hanzi | Pinyin | English)\n"
        "4. Classroom activities\n"
        "5. Homework\n\n"
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


def generate_recap_email(all_notes: str) -> str:
    """Generate a warm, structured HTML recap email body."""
    prompt = (
        "You are an experienced, encouraging Mandarin Chinese teacher writing a "
        "recap email to your student after class. Using the notes below, identify "
        "the most recent class and write the BODY of an HTML email that:\n"
        "- opens with a warm, personal greeting,\n"
        "- briefly summarizes what was covered in that class,\n"
        "- includes an HTML <table> of the vocabulary covered with three columns: "
        "Hanzi, Pinyin, English (use a header row),\n"
        "- ends with an encouraging closing and a short note on what to practice.\n\n"
        "Return ONLY the HTML body content (you may use <p>, <h2>, <table>, etc.). "
        "Do not include <html>, <head>, or <body> tags, and no surrounding code fences.\n\n"
        "=== CLASS NOTES ===\n"
        f"{all_notes}"
    )
    return _generate(prompt)


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

    if not RECAP_TO:
        sys.exit("ERROR: RECAP_TO is not set (recipient of the recap draft).")

    # 1. Read all class plan sections from OneNote.
    token = graph_authenticate()
    notebook_id = find_notebook(token)
    all_notes = read_all_class_plan_sections(token, notebook_id)

    # 2. Load local syllabus and all sent Gmail.
    syllabus_text = _read_local_syllabus()
    if syllabus_text:
        print(f"Loaded syllabus from {SYLLABUS_FILE}")
    else:
        print(f"No syllabus found at {SYLLABUS_FILE} — proceeding without it.")

    print("Reading sent Gmail messages...")
    gmail_service = gmail_authenticate()
    sent_gmail_text = read_sent_gmail(gmail_service)

    # 3. Generate, review, and save the lesson plan.
    print("Generating next lesson plan with Gemini...")
    plan_md = review_lesson_plan(
        all_notes,
        syllabus_text=syllabus_text,
        sent_gmail_text=sent_gmail_text,
        auto=args.auto,
    )
    with open(LESSON_PLAN_FILE, "w", encoding="utf-8") as fh:
        fh.write(plan_md)
    print(f"Saved lesson plan to {LESSON_PLAN_FILE}")

    plan_html  = markdown.markdown(plan_md, extensions=["tables", "fenced_code"])
    page_title = f"Lesson Plan — {datetime.now():%Y-%m-%d}"
    section_id = find_or_create_section(token, notebook_id, LESSON_PLAN_SECTION)
    create_onenote_page(token, section_id, page_title, plan_html)

    # 4. Generate recap email and create Gmail draft.
    print("Generating recap email with Gemini...")
    recap_html = generate_recap_email(all_notes)
    create_draft(gmail_service, RECAP_TO, RECAP_SUBJECT, recap_html, bcc=RECAP_BCC)

    print("\nDone. Review the new OneNote page and your Gmail draft.")


if __name__ == "__main__":
    main()
