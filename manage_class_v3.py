"""Mandarin class V3: plan the Summer 2026 semester.

Reads all Spring 2026 lesson plans from OneNote, generates an 8-class Summer
2026 syllabus (one level above Spring — intermediate/HSK-2), lets the teacher
review and refine it interactively, then generates all 8 individual class plans.

All output is saved locally as .md files and as OneNote pages in a new
"Summer 2026" section (created if needed).

Run:
  python manage_class_v3.py           # interactive syllabus review
  python manage_class_v3.py --auto    # accept first syllabus draft automatically
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import markdown

from manage_class import (
    HERE,
    _generate,
    create_onenote_page,
    find_notebook,
    find_or_create_section,
    graph_authenticate,
    read_section_pages,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

SPRING_SECTION       = os.environ.get("SPRING_SECTION", "Spring 2026 Class Plans")
SUMMER_SECTION       = "Summer 2026"
SUMMER_SYLLABUS_FILE = os.path.join(HERE, "summer_2026_syllabus.md")


# --------------------------------------------------------------------------- #
# Generators
# --------------------------------------------------------------------------- #

def generate_summer_syllabus(
    spring_text: str,
    previous_draft: str | None = None,
    feedback: str | None = None,
) -> str:
    """Generate (or revise) a Summer 2026 syllabus building on Spring 2026 content."""
    prompt = (
        "You are an experienced Mandarin Chinese teacher planning the next semester "
        "for a student who just completed Spring 2026. Below are all the Spring 2026 "
        "lesson plans. Design a Summer 2026 course of exactly 8 classes at an "
        "intermediate level (approximately HSK 2), building directly on what the "
        "student already knows.\n\n"
        "For each class, provide:\n"
        "- A `## Class N: [Title]` heading\n"
        "- 3–5 learning objectives\n"
        "- 8–12 new vocabulary items (Hanzi | Pinyin | English) as a Markdown table\n"
        "- Key grammar point(s) with pattern in Hanzi/Pinyin/English and 1–2 examples\n"
        "- Thematic focus (e.g. dining out, giving directions, talking about hobbies)\n\n"
        "Return only the Markdown, no surrounding text or code fences.\n\n"
        "=== SPRING 2026 LESSON PLANS ===\n"
        f"{spring_text}"
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


def review_summer_syllabus(spring_text: str, auto: bool = False) -> str:
    """Generate a summer syllabus, then let the teacher approve or refine it.

    Mirrors the interactive loop from ``review_lesson_plan`` in manage_class.py.
    With ``auto=True`` or non-interactive stdin, accepts the first draft.
    """
    draft = generate_summer_syllabus(spring_text)
    if auto:
        print("(--auto: accepting the first syllabus draft without review)")
        return draft
    while True:
        print("\n" + "=" * 70)
        print("PROPOSED SUMMER 2026 SYLLABUS")
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
        print("\nRefining the syllabus with your feedback...")
        draft = generate_summer_syllabus(spring_text, previous_draft=draft, feedback=response)


def _extract_class_title(syllabus_md: str, class_num: int) -> str:
    """Extract the title from a '## Class N: Title' heading in the syllabus."""
    m = re.search(
        rf"^##\s+Class\s+{class_num}[:\s]+(.+)$",
        syllabus_md,
        re.MULTILINE,
    )
    return m.group(1).strip() if m else f"Class {class_num}"


def generate_summer_class_plan(
    spring_text: str, syllabus_md: str, class_num: int
) -> str:
    """Generate the full lesson plan for one Summer 2026 class."""
    prompt = (
        f"You are an experienced Mandarin Chinese teacher. Write the detailed lesson "
        f"plan for Summer 2026 Class {class_num} of 8, following the syllabus entry "
        f"for Class {class_num} as your scope guide.\n\n"
        "Structure the plan with these sections:\n"
        "1. Learning objectives\n"
        "2. Review of prior material (relevant Spring 2026 content + earlier Summer classes if applicable)\n"
        "3. New vocabulary (Markdown table: Hanzi | Pinyin | English)\n"
        "4. New grammar points (pattern + examples)\n"
        "5. Classroom activities (3–4 activities with approximate timing)\n"
        "6. Homework\n\n"
        "Return only the Markdown, no surrounding text or code fences.\n\n"
        "=== SUMMER 2026 SYLLABUS ===\n"
        f"{syllabus_md}\n\n"
        "=== SPRING 2026 LESSON PLANS (background context) ===\n"
        f"{spring_text}"
    )
    return _generate(prompt)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Summer 2026 Mandarin syllabus and 8 class plans."
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Accept the first syllabus draft without interactive review.",
    )
    args = parser.parse_args()

    token = graph_authenticate()
    notebook_id = find_notebook(token)
    spring_text, _ = read_section_pages(token, notebook_id, SPRING_SECTION)

    # Generate and optionally review the syllabus.
    syllabus_md = review_summer_syllabus(spring_text, auto=args.auto)
    with open(SUMMER_SYLLABUS_FILE, "w", encoding="utf-8") as fh:
        fh.write(syllabus_md)
    print(f"Saved {SUMMER_SYLLABUS_FILE}")

    summer_sid = find_or_create_section(token, notebook_id, SUMMER_SECTION)
    syllabus_html = markdown.markdown(syllabus_md, extensions=["tables", "fenced_code"])
    create_onenote_page(token, summer_sid, "Summer 2026 Course Syllabus", syllabus_html)

    # Generate all 8 individual class plans.
    for n in range(1, 9):
        print(f"Generating Summer 2026 Class {n}/8...")
        plan_md = generate_summer_class_plan(spring_text, syllabus_md, n)
        plan_file = os.path.join(HERE, f"summer_2026_class_{n:02d}.md")
        with open(plan_file, "w", encoding="utf-8") as fh:
            fh.write(plan_md)
        print(f"Saved {plan_file}")
        title = f"Summer 2026 — {_extract_class_title(syllabus_md, n)}"
        plan_html = markdown.markdown(plan_md, extensions=["tables", "fenced_code"])
        create_onenote_page(token, summer_sid, title, plan_html)

    print("\nDone. Syllabus + 8 class plans saved locally and to OneNote.")


if __name__ == "__main__":
    main()
