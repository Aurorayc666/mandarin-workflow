"""generate_handouts.py — student handout materials for a range of classes.

Prompts for which classes to generate handouts for (e.g. "1-2"), defaulting to
the next un-handed-out pair based on what's already been generated locally.
Reads the OneNote page(s) for those classes (the "Class N ..." pages in the
Summer 2026 section) plus their recap emails, then uses Gemini to produce two
student-facing documents scoped to those classes only:

  1. Study guide — vocabulary table, sentence table, and grammar summary
  2. Homework packet (writing list, fill-in-the-blank, translation, composition)

Anything already present in the prior semester's local handout files
(vocab_table.md, sentence_table.md, grammar_summary.md, homework.md) is
excluded, since the student already received that material.

Each document is saved as a local .md file and as a page in a
"Summer 2026 Handouts" OneNote section (created if needed), titled
"Class N-M - <Document>".

Run after the relevant classes' plans (and recaps) exist:
  python generate_handouts.py
"""

from __future__ import annotations

import os
import re
import sys

import markdown

from utils import (
    HERE,
    _generate,
    count_class_plan_pages,
    create_onenote_page,
    find_notebook,
    find_or_create_section,
    find_section,
    gmail_authenticate,
    graph_authenticate,
    read_class_plan_page,
    read_class_recap_emails,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

SUMMER_SECTION   = os.environ.get("SUMMER_SECTION", "Summer 2026 Class Plans")
HANDOUTS_SECTION = "Summer 2026 Handouts"

# Prior semester's handout files — used only as a do-not-repeat reference.
PREV_VOCAB_TABLE_FILE    = os.path.join(HERE, "vocab_table.md")
PREV_SENTENCE_TABLE_FILE = os.path.join(HERE, "sentence_table.md")
PREV_GRAMMAR_FILE        = os.path.join(HERE, "grammar_summary.md")
PREV_HOMEWORK_FILE       = os.path.join(HERE, "homework.md")

# This-semester local handout files, named by class range — scanned to figure
# out which classes have already had handouts generated.
_RANGE_FILE_RE = re.compile(r"summer_2026_class(\d+)(?:-(\d+))?_study_guide\.md$")


def _range_label(start: int, end: int) -> str:
    return f"{start}-{end}" if end != start else f"{start}"


def _local_path(start: int, end: int, suffix: str) -> str:
    return os.path.join(HERE, f"summer_2026_class{_range_label(start, end)}_{suffix}.md")


def _next_default_range(max_class: int) -> tuple[int, int]:
    """Look at existing study-guide files to find the next un-covered class range."""
    highest_covered = 0
    for name in os.listdir(HERE):
        m = _RANGE_FILE_RE.match(name)
        if m:
            end = int(m.group(2) or m.group(1))
            highest_covered = max(highest_covered, end)
    start = highest_covered + 1
    end = min(start + 1, max_class) if max_class else start + 1
    if end < start:
        end = start
    return start, end


def _prompt_for_range(max_class: int) -> tuple[int, int]:
    """Ask the teacher which classes to generate handouts for."""
    default_start, default_end = _next_default_range(max_class)
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

def _read_previous_handouts() -> str:
    """Concatenate any existing prior-semester handout files, if present."""
    chunks: list[str] = []
    for label, path in (
        ("Vocabulary Table", PREV_VOCAB_TABLE_FILE),
        ("Sentence Table", PREV_SENTENCE_TABLE_FILE),
        ("Grammar Summary", PREV_GRAMMAR_FILE),
        ("Homework Packet", PREV_HOMEWORK_FILE),
    ):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read().strip()
            if text:
                chunks.append(f"## {label}\n{text}")
    return "\n\n".join(chunks)


def _dedup_block(previous_handouts: str) -> str:
    """Return a prompt block instructing Gemini to exclude prior-semester content."""
    if not previous_handouts:
        return ""
    return (
        "\n\nDo not include anything that already appears in the prior semester's "
        "handouts below — the student has already received that material.\n\n"
        "=== PRIOR SEMESTER HANDOUTS (already covered — exclude) ===\n"
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
    previous_handouts = _read_previous_handouts()

    token = graph_authenticate()
    notebook_id = find_notebook(token)
    section_id = find_section(token, notebook_id, SUMMER_SECTION)
    if not section_id:
        sys.exit(f"ERROR: section {SUMMER_SECTION!r} not found.")

    max_class = count_class_plan_pages(token, section_id)
    if max_class == 0:
        sys.exit(f"ERROR: no 'Class N ...' pages found in {SUMMER_SECTION!r}.")

    start, end = _prompt_for_range(max_class)
    label = _range_label(start, end)

    gmail_service = gmail_authenticate()
    context_text = _read_classes_context(token, section_id, gmail_service, start, end)

    handouts_sid = find_or_create_section(token, notebook_id, HANDOUTS_SECTION)

    artifacts = [
        ("study guide", _local_path(start, end, "study_guide"), generate_study_guide, f"Class {label} - Study Guide"),
        ("homework packet", _local_path(start, end, "homework"), generate_homework, f"Class {label} - Homework"),
    ]

    for artifact_label, path, fn, title in artifacts:
        print(f"Generating {artifact_label}...")
        md = fn(context_text, previous_handouts)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"Saved {path}")
        html = markdown.markdown(md, extensions=["tables", "fenced_code"])
        create_onenote_page(token, handouts_sid, title, html)

    print(f"\nDone. 2 Class {label} handout files saved locally and 2 OneNote pages created.")


if __name__ == "__main__":
    main()
