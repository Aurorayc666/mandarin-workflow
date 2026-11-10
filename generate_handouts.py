"""generate_handouts.py — student handout materials for a range of classes.

Uses the semester from SEMESTER in .env (or --semester for a one-off run),
then prompts for which classes to generate handouts for (e.g. "1-2"), defaulting to the next un-handed-out pair based on what's already been
generated locally for that semester. Reads the OneNote page(s) for those
classes (the "Class N ..." pages in the "<semester> Class Plans" section) plus
their recap emails, then uses Gemini to produce two student-facing documents
scoped to those classes only:

  1. Study guide — vocabulary table, sentence table, and grammar summary
  2. Homework packet (writing list, fill-in-the-blank, translation, composition)

Anything already present in ANY previously generated handout file, across every
semester (every local *_study_guide.md / *_homework.md, plus the legacy
vocab_table.md / sentence_table.md / grammar_summary.md / homework.md), is
excluded, since the student already received that material.

Each document is saved as a local .md file and as a page in the same
"<semester> Class Plans" OneNote section as the lesson plans, titled
"Class N-M - <Document>". (Lesson-plan lookups skip these pages by their
"Study Guide" / "Homework" title suffix — see utils.is_handout_title.)
A Word + PDF student copy of each also goes to For_Students/.

Run after the relevant classes' plans (and recaps) exist:
  python generate_handouts.py
  python generate_handouts.py --semester "Spring 2027"   # one-off semester override
"""

from __future__ import annotations

import argparse
import os
import re
import sys


from utils import (
    md_to_html,
    HERE,
    _generate,
    count_class_plan_pages,
    create_onenote_page,
    find_notebook,
    find_section,
    gmail_authenticate,
    graph_authenticate,
    read_class_plan_page,
    read_class_recap_emails,
    add_semester_argument,
    resolve_semester,
    export_for_students,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Legacy pre-2026-07-08 handout files (one set, no semester/class-range in the
# name) — still included in the do-not-repeat scan if present.
_LEGACY_HANDOUT_FILES = {
    "vocab_table.md",
    "sentence_table.md",
    "grammar_summary.md",
    "homework.md",
}

# Any semester's study guide / homework packet, e.g. "fall_2026_class1-2_homework.md".
_HANDOUT_FILE_RE = re.compile(r".*_(?:study_guide|homework)\.md$")


def _range_label(start: int, end: int) -> str:
    return f"{start}-{end}" if end != start else f"{start}"


def _local_path(slug: str, start: int, end: int, suffix: str) -> str:
    return os.path.join(HERE, f"{slug}_class{_range_label(start, end)}_{suffix}.md")


def _next_default_range(slug: str, max_class: int) -> tuple[int, int]:
    """Look at this semester's existing study-guide files to find the next
    un-covered class range."""
    range_file_re = re.compile(
        rf"^{re.escape(slug)}_class(\d+)(?:-(\d+))?_study_guide\.md$"
    )
    highest_covered = 0
    for name in os.listdir(HERE):
        m = range_file_re.match(name)
        if m:
            end = int(m.group(2) or m.group(1))
            highest_covered = max(highest_covered, end)
    start = highest_covered + 1
    end = min(start + 1, max_class) if max_class else start + 1
    if end < start:
        end = start
    return start, end


def _prompt_for_range(slug: str, max_class: int) -> tuple[int, int]:
    """Ask the teacher which classes to generate handouts for."""
    default_start, default_end = _next_default_range(slug, max_class)
    default_label = _range_label(default_start, default_end)
    try:
        raw = input(
            f"Lesson plans exist for classes 1-{max_class}. "
            f"Which classes should handouts cover? [default {default_label}]: "
        ).strip()
    except EOFError:
        raw = ""
    if not raw:
        return default_start, default_end
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", raw)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
    else:
        m = re.match(r"^(\d+)$", raw)
        if not m:
            sys.exit(f"ERROR: could not parse class range {raw!r} (expected e.g. '3-4' or '5').")
        start = end = int(m.group(1))
    if start < 1 or end < start:
        sys.exit(f"ERROR: invalid class range {start}-{end}.")
    if end > max_class:
        sys.exit(f"ERROR: class {end} has no lesson plan yet (only 1-{max_class} exist).")
    return start, end


# --------------------------------------------------------------------------- #
# Context helpers
# --------------------------------------------------------------------------- #

def _read_previous_handouts(exclude_paths: set[str]) -> str:
    """Concatenate every previously generated handout file across every semester
    (legacy single-file set plus every *_study_guide.md / *_homework.md), so
    Gemini can avoid repeating material the student has already received."""
    chunks: list[str] = []
    for name in sorted(os.listdir(HERE)):
        path = os.path.join(HERE, name)
        if path in exclude_paths:
            continue
        if name not in _LEGACY_HANDOUT_FILES and not _HANDOUT_FILE_RE.match(name):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
        if text:
            chunks.append(f"## {name}\n{text}")
    return "\n\n".join(chunks)


def _dedup_block(previous_handouts: str) -> str:
    """Return a prompt block instructing Gemini to exclude already-covered content."""
    if not previous_handouts:
        return ""
    return (
        "\n\nDo not include anything that already appears in the previously "
        "generated handouts below — the student has already received that "
        "material.\n\n"
        "=== PREVIOUSLY GENERATED HANDOUTS, ALL SEMESTERS (already covered — exclude) ===\n"
        f"{previous_handouts}"
    )


def _read_classes_context(token: str, section_id: str, gmail_service, start: int, end: int) -> str:
    """Concatenate each class's lesson plan and recap email for the given range."""
    blocks: list[str] = []
    for class_num in range(start, end + 1):
        class_text = read_class_plan_page(token, section_id, class_num)
        print(f"Reading recap email for Class {class_num} from Gmail...")
        recap_email = read_class_recap_emails(gmail_service, class_num)
        block = class_text
        if recap_email:
            block += f"\n\n=== SENT RECAP EMAIL (Class {class_num}) ===\n{recap_email}"
        blocks.append(block)
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- #
# Generators
# --------------------------------------------------------------------------- #

def generate_vocab_table(context_text: str, previous_handouts: str) -> str:
    """Extract all vocabulary introduced in these classes into a single table."""
    prompt = (
        "You are an experienced Mandarin Chinese teacher. Below are the lesson "
        "plan(s) (and recap email(s), if available) for one or more classes. Extract "
        "every vocabulary item that was explicitly introduced in these classes only.\n\n"
        "Output a single Markdown table with exactly three columns: "
        "**Hanzi | Pinyin | English**. Include a header row. Sort rows by Pinyin "
        "(A–Z). Deduplicate. Return only the Markdown table, no surrounding text "
        "or code fences."
        f"{_dedup_block(previous_handouts)}\n\n"
        "=== THESE CLASSES' LESSON PLANS & RECAP EMAILS ===\n"
        f"{context_text}"
    )
    return _generate(prompt)


def generate_sentence_table(context_text: str, previous_handouts: str) -> str:
    """Extract all key sentences and grammar examples from these classes."""
    prompt = (
        "You are an experienced Mandarin Chinese teacher. Below are the lesson "
        "plan(s) (and recap email(s), if available) for one or more classes. Extract "
        "every key sentence and grammar-example sentence introduced in these classes "
        "only.\n\n"
        "Output a single Markdown table with three columns: **Hanzi | Pinyin | "
        "English**. Include a header row. Return only the Markdown, no surrounding "
        "text or code fences."
        f"{_dedup_block(previous_handouts)}\n\n"
        "=== THESE CLASSES' LESSON PLANS & RECAP EMAILS ===\n"
        f"{context_text}"
    )
    return _generate(prompt)


def generate_grammar_summary(context_text: str, previous_handouts: str) -> str:
    """Summarize every grammar point introduced in these classes."""
    prompt = (
        "You are an experienced Mandarin Chinese teacher. Below are the lesson "
        "plan(s) (and recap email(s), if available) for one or more classes. Produce "
        "a grammar reference document covering only these classes.\n\n"
        "For each distinct grammar point introduced in these classes, create a `###` "
        "section. Each section should include:\n"
        "- The grammar pattern in Hanzi, Pinyin, and English\n"
        "- 2–3 example sentences (Hanzi + Pinyin + English)\n\n"
        "Return only the Markdown, no surrounding text or code fences."
        f"{_dedup_block(previous_handouts)}\n\n"
        "=== THESE CLASSES' LESSON PLANS & RECAP EMAILS ===\n"
        f"{context_text}"
    )
    return _generate(prompt)


def generate_study_guide(context_text: str, previous_handouts: str) -> str:
    """Combine the vocabulary table, sentence table, and grammar summary into one doc."""
    vocab_md    = generate_vocab_table(context_text, previous_handouts)
    sentence_md = generate_sentence_table(context_text, previous_handouts)
    grammar_md  = generate_grammar_summary(context_text, previous_handouts)
    return (
        f"## Vocabulary\n\n{vocab_md}\n\n"
        f"## Key Sentences\n\n{sentence_md}\n\n"
        f"## Grammar\n\n{grammar_md}"
    )


def generate_homework(context_text: str, previous_handouts: str) -> str:
    """Generate a four-part homework packet covering only these classes."""
    prompt = (
        "You are an experienced Mandarin Chinese teacher. Below are the lesson "
        "plan(s) (and recap email(s), if available) for one or more classes. Create "
        "a four-part homework packet using ONLY vocabulary and grammar introduced "
        "in these classes.\n\n"
        "Whenever you write a Chinese sentence anywhere in this packet, put its Pinyin "
        "(with tone marks) in parentheses immediately after it.\n\n"
        "## Part 1: Character Writing List\n"
        "List 10–15 Hanzi characters, each with Pinyin and English meaning. "
        "Choose characters that reinforce key vocabulary from these classes.\n\n"
        "## Part 2: Fill in the Blank\n"
        "Write 5–8 sentences in Chinese with one word or particle blanked out. "
        "Each blank should target a key grammar pattern. Include an answer key at the end.\n\n"
        "## Part 3: Translation (English → Chinese)\n"
        "Provide 5 English sentences for the student to translate into Chinese. "
        "Choose sentences that practice key grammar structures. Include an answer key.\n\n"
        "## Part 4: Short Composition\n"
        "Give a creative writing prompt (2–3 sentences describing a scenario). "
        "Ask the student to write 3–5 sentences in Chinese using at least 5 vocabulary "
        "items from these classes.\n\n"
        "Return only the Markdown, no surrounding text or code fences."
        f"{_dedup_block(previous_handouts)}\n\n"
        "=== THESE CLASSES' LESSON PLANS & RECAP EMAILS ===\n"
        f"{context_text}"
    )
    return _generate(prompt)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a study guide and homework packet for a range of classes."
    )
    add_semester_argument(parser)
    args = parser.parse_args()
    semester = resolve_semester(args.semester)

    slug             = semester.lower().replace(" ", "_")
    section_name     = f"{semester} Class Plans"

    token = graph_authenticate()
    notebook_id = find_notebook(token)
    section_id = find_section(token, notebook_id, section_name)
    if not section_id:
        sys.exit(f"ERROR: section {section_name!r} not found.")

    max_class = count_class_plan_pages(token, section_id)
    if max_class == 0:
        sys.exit(f"ERROR: no 'Class N ...' pages found in {section_name!r}.")

    start, end = _prompt_for_range(slug, max_class)
    label = _range_label(start, end)

    study_guide_path = _local_path(slug, start, end, "study_guide")
    homework_path    = _local_path(slug, start, end, "homework")
    previous_handouts = _read_previous_handouts({study_guide_path, homework_path})

    gmail_service = gmail_authenticate()
    context_text = _read_classes_context(token, section_id, gmail_service, start, end)

    artifacts = [
        ("study guide", study_guide_path, generate_study_guide, f"Class {label} - Study Guide"),
        ("homework packet", homework_path, generate_homework, f"Class {label} - Homework"),
    ]

    for artifact_label, path, fn, title in artifacts:
        print(f"Generating {artifact_label}...")
        md = fn(context_text, previous_handouts)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"Saved {path}")
        html = md_to_html(md)
        create_onenote_page(token, section_id, title, html)
        export_for_students(md, f"{semester} {title}")

    print(f"\nDone. 2 Class {label} handout files saved locally and 2 OneNote pages created.")


if __name__ == "__main__":
    main()
