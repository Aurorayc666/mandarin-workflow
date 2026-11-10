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
    BACK_FILES_DIR,
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
    return os.path.join(BACK_FILES_DIR, f"{slug}_class{_range_label(start, end)}_{suffix}.md")


def _next_default_range(slug: str, max_class: int) -> tuple[int, int]:
    """Look at this semester's existing study-guide files to find the next
    un-covered class range."""
    range_file_re = re.compile(
        rf"^{re.escape(slug)}_class(\d+)(?:-(\d+))?_study_guide\.md$"
    )
    highest_covered = 0
    for name in os.listdir(BACK_FILES_DIR):
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
    for name in sorted(os.listdir(BACK_FILES_DIR)):
        path = os.path.join(BACK_FILES_DIR, name)
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


_ANSWER_KEY_LABEL_RE = re.compile(r"^\s*(?:#{2,6}\s*|\*\*).*(?:answer key|答案)", re.IGNORECASE)
_PART_HEADING_RE = re.compile(r"^(#{2,6})\s+(.*\S)\s*$")


def move_answer_keys_to_end(md: str) -> str:
    """Pull each part's answer key out of the packet and collect them in one
    "Answer Key" section at the end, grouped by part, so students can do the
    homework without the answers sitting right under the questions.

    A key starts at a bold/heading line mentioning "Answer Key" or 答案 and runs
    until the next '---' rule or heading. A packet that already ends with a
    consolidated "### Answer Key" section is returned unchanged.
    """
    if re.search(r"^#{2,6}\s*Answer Key\s*$", md, re.IGNORECASE | re.MULTILINE):
        return md
    body: list[str] = []
    keys: list[tuple[str, list[str]]] = []   # (part heading, key lines)
    current_part = ""
    collecting: list[str] | None = None

    for line in md.splitlines():
        heading = _PART_HEADING_RE.match(line)
        is_key_label = bool(_ANSWER_KEY_LABEL_RE.match(line))
        if collecting is not None:
            if line.strip() == "---" or (heading and not is_key_label):
                collecting = None          # key ended; fall through to keep this line
            else:
                collecting.append(line)
                continue
        if is_key_label:
            collecting = []
            keys.append((current_part, collecting))
            # drop trailing blank lines left above the removed key
            while body and not body[-1].strip():
                body.pop()
            body.append("")
            continue
        if heading:
            current_part = heading.group(2)
        body.append(line)

    if not keys:
        return md

    out = "\n".join(body).rstrip() + "\n\n---\n\n### Answer Key\n"
    for part, lines in keys:
        text = "\n".join(lines).strip()
        if text:
            out += f"\n**{part}**\n\n{text}\n" if part else f"\n{text}\n"
    return out


def generate_homework(
    context_text: str, previous_handouts: str, start: int = 1, end: int = 1
) -> str:
    """Generate a four-part homework packet that covers EVERY class in the range.

    Without an explicit per-class requirement Gemini filled a fixed 5–8 item
    quota from the most recent class only, so question counts scale with the
    number of classes and each item is tagged with the class it practises.
    """
    n = end - start + 1
    classes = ", ".join(f"Class {i}" for i in range(start, end + 1))
    chars = f"{max(10, 3 * n)}–{max(15, 4 * n)}"
    blanks = f"{max(5, 2 * n)}–{max(8, 3 * n)}"
    translations = max(5, 2 * n)
    prompt = (
        "You are an experienced Mandarin Chinese teacher. Below are the lesson "
        f"plans (and recap emails, if available) for {classes}. Create a four-part "
        "homework packet using ONLY vocabulary and grammar introduced in these "
        "classes.\n\n"
        f"COVERAGE IS REQUIRED: the packet must practise material from every one of "
        f"{classes} in balanced proportion — do not concentrate on the most recent "
        "class. Before writing, list for yourself each class's key grammar points "
        "and vocabulary, then make sure every class's grammar points appear in Part 2 "
        "and Part 3. Tag every item in Parts 1–3 with the class it comes from, e.g. "
        "\"(Class 2)\", and order items by class number.\n\n"
        "Whenever you write a Chinese sentence anywhere in this packet, put its Pinyin "
        "(with tone marks) in parentheses immediately after it. In fill-in-the-blank "
        "sentences, leave the same blank (_______) in the Pinyin too, so the Pinyin "
        "does not give away the answer.\n\n"
        "## Part 1: Character Writing List\n"
        f"List {chars} Hanzi characters, each with Pinyin and English meaning, drawn "
        "from every class's key vocabulary.\n\n"
        "## Part 2: Fill in the Blank\n"
        f"Write {blanks} sentences in Chinese, each with exactly ONE word or particle "
        "blanked out (at least two per class). Each blank should target a key grammar "
        "pattern. Give the answers right after this part under a line reading "
        "exactly **Answer Key:**.\n\n"
        "## Part 3: Translation (English → Chinese)\n"
        f"Provide {translations} English sentences for the student to translate into "
        "Chinese (at least two per class), practising key grammar structures. The "
        "questions must be in English ONLY — no Chinese characters or Pinyin under "
        "or after them, since that would give away the answer. Put the Chinese "
        "(with Pinyin) only in the answers, right after this part under a line "
        "reading exactly **Answer Key:**.\n\n"
        "Double-check that every Chinese sentence, including each completed "
        "fill-in-the-blank sentence, is natural and grammatically correct.\n\n"
        "## Part 4: Short Composition\n"
        "Give a creative writing prompt (2–3 sentences describing a scenario). "
        "Ask the student to write 3–5 sentences in Chinese using at least 5 vocabulary "
        "items and grammar from several different classes in this range.\n\n"
        "Return only the Markdown, no surrounding text or code fences."
        f"{_dedup_block(previous_handouts)}\n\n"
        "=== THESE CLASSES' LESSON PLANS & RECAP EMAILS ===\n"
        f"{context_text}"
    )
    # Code (not Gemini) moves every part's answers into one section at the end,
    # so the layout is the same in the .md, OneNote, Word and PDF.
    return move_answer_keys_to_end(_generate(prompt))


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
        (
            "homework packet",
            homework_path,
            lambda ctx, prev: generate_homework(ctx, prev, start, end),
            f"Class {label} - Homework",
        ),
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
