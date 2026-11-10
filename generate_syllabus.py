"""generate_syllabus.py — generate a Mandarin course syllabus for a given semester.

Uses the semester from SEMESTER in .env (or --semester for a one-off run).
For a forward-looking syllabus, the record of what the student has already
covered is every past semester's handout pages (study guides etc.) plus sent
Gmail recap emails, NOT past syllabi or class plans, because the last class or
two of a syllabus often isn't reached. (Spring is retrospective and reads its own
class plans.) Uses Gemini to generate the syllabus, with an interactive review
loop and a second revision gate after saving.

Run:
  python generate_syllabus.py           # interactive
  python generate_syllabus.py --auto    # accept first draft automatically
  python generate_syllabus.py --semester "Spring 2027"   # one-off semester override
"""

from __future__ import annotations

import argparse
import os

import markdown

from utils import (
    HERE,
    _generate,
    create_onenote_page,
    find_notebook,
    find_or_create_section,
    gmail_authenticate,
    graph_authenticate,
    read_past_handouts,
    read_recap_emails,
    read_section_pages,
    add_semester_argument,
    resolve_semester,
)


# --------------------------------------------------------------------------- #
# Generators
# --------------------------------------------------------------------------- #

def _build_syllabus_prompt(semester: str, all_notes: str, recap_emails: str) -> str:
    """Return a semester-appropriate prompt with the context blocks appended."""
    is_spring = "spring" in semester.lower()

    if is_spring:
        # Spring is the first semester — generate a retrospective syllabus that
        # reflects what was actually taught, with an HSK level analysis.
        body = (
            f"You are an experienced Mandarin Chinese teacher. Using the class plans "
            f"and recap emails below, produce a complete {semester} course syllabus "
            "that accurately reflects what was taught this semester.\n\n"
            "At the top, include a short \"## HSK Alignment\" section that identifies "
            "which HSK level (1–6) the semester content most closely corresponds to, "
            "noting any significant gaps or topics covered out of the standard order.\n\n"
            f"Then summarize the {semester} course as exactly 8 class entries. "
            "For each class provide:\n"
            "- A `## Class N: [Title]` heading\n"
            "- Key vocabulary covered (Hanzi | Pinyin | English table)\n"
            "- Grammar points introduced\n"
            "- Thematic focus\n\n"
            "Whenever you write a Chinese sentence anywhere (not just the vocabulary "
            "table), put its Pinyin (with tone marks) immediately after it.\n\n"
            "Return only the Markdown, no surrounding text or code fences.\n\n"
            "=== CLASS PLANS (OneNote) ===\n"
            f"{all_notes}"
        )
    else:
        # Non-spring semesters: forward-looking plan at HSK Level 2, building on all
        # past content so nothing is repeated.
        body = (
            f"You are an experienced Mandarin Chinese teacher planning the {semester} "
            "semester. You have two sources of context below: the handouts from each "
            "past semester (study guides with the vocabulary, sentences and grammar "
            "actually taught) and sent recap emails. Study both carefully so you know "
            "exactly what vocabulary and grammar the student has already covered — "
            "do not repeat any of it. Past syllabi are deliberately not included: "
            "their last classes were often never reached, so treat only what appears "
            "in the handouts or recap emails as covered.\n\n"
            "First, include a short \"## HSK Alignment\" section that:\n"
            "- States which HSK level the past content corresponds to\n"
            "- Lists key HSK Level 2 vocabulary and grammar topics not yet covered\n\n"
            f"Then design the {semester} course as exactly 8 classes at HSK Level 2, "
            "building directly on all content from past semesters. Every vocabulary "
            "item and grammar point must be new to the student and drawn from the "
            "standard HSK Level 2 syllabus.\n\n"
            "For each class provide:\n"
            "- A `## Class N: [Title]` heading\n"
            "- 3–5 learning objectives\n"
            "- 8–12 new vocabulary items (Hanzi | Pinyin | English) as a Markdown table\n"
            "- Key grammar point(s) with pattern in Hanzi/Pinyin/English and 1–2 examples\n"
            "- Thematic focus (e.g. dining out, giving directions, talking about hobbies)\n\n"
            "Whenever you write a Chinese sentence anywhere (not just the vocabulary "
            "table), put its Pinyin (with tone marks) immediately after it.\n\n"
            "Return only the Markdown, no surrounding text or code fences.\n\n"
            "=== HANDOUTS FROM EACH PAST SEMESTER (already covered) ===\n"
            f"{all_notes}"
        )

    if recap_emails:
        body += (
            "\n\n=== SENT RECAP EMAILS ===\n"
            f"{recap_emails}"
        )
    return body


def generate_syllabus(
    all_notes: str,
    recap_emails: str = "",
    semester: str = "Summer 2026",
    previous_draft: str | None = None,
    feedback: str | None = None,
) -> str:
    """Generate (or revise) a course syllabus for the given semester."""
    prompt = _build_syllabus_prompt(semester, all_notes, recap_emails)
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


def review_syllabus(
    all_notes: str,
    recap_emails: str = "",
    semester: str = "Summer 2026",
    auto: bool = False,
) -> str:
    """Generate a syllabus and let the teacher approve or refine it interactively."""
    draft = generate_syllabus(all_notes, recap_emails=recap_emails, semester=semester)
    if auto:
        print("(--auto: accepting the first syllabus draft without review)")
        return draft
    while True:
        print("\n" + "=" * 70)
        print(f"PROPOSED {semester.upper()} SYLLABUS")
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
        draft = generate_syllabus(
            all_notes,
            recap_emails=recap_emails,
            semester=semester,
            previous_draft=draft,
            feedback=response,
        )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a Mandarin course syllabus.")
    parser.add_argument(
        "--auto", action="store_true", help="Accept the first draft without review."
    )
    add_semester_argument(parser)
    args = parser.parse_args()
    semester = resolve_semester(args.semester)

    slug          = semester.lower().replace(" ", "_")   # "summer_2026"
    syllabus_file = os.path.join(HERE, f"{slug}_syllabus.md")
    section_name  = f"{semester} Class Plans"            # created in OneNote if needed

    # Read context from OneNote and Gmail recaps.
    # Spring: read only the spring section (retrospective — don't mix in future semesters).
    # Summer / Fall / other: read past semesters' handouts (what was actually
    # covered) rather than past syllabi, whose last classes often weren't reached.
    token = graph_authenticate()
    notebook_id = find_notebook(token)
    if "spring" in semester.lower():
        all_notes, _ = read_section_pages(token, notebook_id, section_name)
    else:
        all_notes = read_past_handouts(token, notebook_id, exclude_semester=semester)
        if not all_notes:
            print("WARNING: no past handouts found; the syllabus will rely on recap emails only.")

    print("Reading recap emails from Gmail...")
    gmail_service = gmail_authenticate()
    recap_emails  = read_recap_emails(gmail_service)

    # Generate and interactively review the syllabus.
    syllabus_md = review_syllabus(
        all_notes, recap_emails=recap_emails, semester=semester, auto=args.auto
    )
    with open(syllabus_file, "w", encoding="utf-8") as fh:
        fh.write(syllabus_md)
    print(f"Saved {syllabus_file}")

    section_id   = find_or_create_section(token, notebook_id, section_name)
    syllabus_html = markdown.markdown(syllabus_md, extensions=["tables", "fenced_code"])
    create_onenote_page(token, section_id, f"{semester} Course Syllabus", syllabus_html)

    # Second gate: offer another revision pass after seeing the saved confirmation.
    if not args.auto:
        while True:
            try:
                go = input(
                    f"\nSyllabus saved to OneNote and to {syllabus_file}.\n"
                    "Press Enter (or 'y') to finish, "
                    "or type feedback to revise the syllabus: "
                ).strip()
            except EOFError:
                break
            if go == "" or go.lower() in ("y", "yes"):
                break
            print("\nRevising syllabus with your feedback...")
            syllabus_md = generate_syllabus(
                all_notes,
                recap_emails=recap_emails,
                semester=semester,
                previous_draft=syllabus_md,
                feedback=go,
            )
            with open(syllabus_file, "w", encoding="utf-8") as fh:
                fh.write(syllabus_md)
            print(f"Saved {syllabus_file}")
            syllabus_html = markdown.markdown(syllabus_md, extensions=["tables", "fenced_code"])
            create_onenote_page(token, section_id, f"{semester} Course Syllabus", syllabus_html)
            print("Updated OneNote syllabus page.")

    print(f"\nDone. {semester} syllabus saved locally and to OneNote.")


if __name__ == "__main__":
    main()
