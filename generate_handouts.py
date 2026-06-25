"""generate_handouts.py — end-of-semester student handout materials.

Reads all past lesson plans from the Spring 2026 OneNote section, then uses
Gemini to produce four student-facing documents:

  1. Vocabulary table (Hanzi | Pinyin | English, sorted by Pinyin, deduplicated)
  2. Sentence table (key sentences grouped by class)
  3. Grammar summary (one section per grammar point with examples)
  4. Homework packet (writing list, fill-in-the-blank, translation, composition)

Each document is saved as a local .md file and as a page in a
"Spring 2026 Handouts" OneNote section (created if needed).

Run: python generate_handouts.py
"""

from __future__ import annotations

import os

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

SPRING_SECTION   = os.environ.get("SPRING_SECTION", "Spring 2026 Class Plans")
HANDOUTS_SECTION = "Spring 2026 Handouts"

VOCAB_TABLE_FILE    = os.path.join(HERE, "vocab_table.md")
SENTENCE_TABLE_FILE = os.path.join(HERE, "sentence_table.md")
GRAMMAR_FILE        = os.path.join(HERE, "grammar_summary.md")
HOMEWORK_FILE       = os.path.join(HERE, "homework.md")


# --------------------------------------------------------------------------- #
# Generators
# --------------------------------------------------------------------------- #

def generate_vocab_table(plans_text: str) -> str:
    """Extract all vocabulary introduced across all lessons into a single table."""
    prompt = (
        "You are an experienced Mandarin Chinese teacher. Below are all the lesson "
        "plans from a completed semester. Extract every vocabulary item that was "
        "explicitly introduced across all classes.\n\n"
        "Output a single Markdown table with exactly three columns: "
        "**Hanzi | Pinyin | English**. Include a header row. Sort rows by Pinyin "
        "(A–Z). Deduplicate — if a word appears in multiple lessons, include it only "
        "once. Return only the Markdown table, no surrounding text or code fences.\n\n"
        "=== LESSON PLANS ===\n"
        f"{plans_text}"
    )
    return _generate(prompt)


def generate_sentence_table(plans_text: str) -> str:
    """Extract all key sentences and grammar examples, grouped by class."""
    prompt = (
        "You are an experienced Mandarin Chinese teacher. Below are all the lesson "
        "plans from a completed semester. Extract every key sentence and "
        "grammar-example sentence introduced in each class.\n\n"
        "Organize the output with a `## Class N` heading for each class (use the "
        "actual class number or title from the lesson plan). Under each heading, "
        "include a Markdown table with three columns: **Hanzi | Pinyin | English**. "
        "Include a header row in each table. Return only the Markdown, no surrounding "
        "text or code fences.\n\n"
        "=== LESSON PLANS ===\n"
        f"{plans_text}"
    )
    return _generate(prompt)


def generate_grammar_summary(plans_text: str) -> str:
    """Summarize every grammar point introduced across all lessons."""
    prompt = (
        "You are an experienced Mandarin Chinese teacher. Below are all the lesson "
        "plans from a completed semester. Produce a comprehensive grammar reference "
        "document.\n\n"
        "For each distinct grammar point introduced during the semester, create a "
        "`###` section. Each section should include:\n"
        "- The grammar pattern in Hanzi, Pinyin, and English\n"
        "- Which class first introduced it\n"
        "- 2–3 example sentences (Hanzi + Pinyin + English)\n\n"
        "Return only the Markdown, no surrounding text or code fences.\n\n"
        "=== LESSON PLANS ===\n"
        f"{plans_text}"
    )
    return _generate(prompt)


def generate_homework(plans_text: str) -> str:
    """Generate a four-part end-of-semester homework packet."""
    prompt = (
        "You are an experienced Mandarin Chinese teacher. Below are all the lesson "
        "plans from a completed semester. Create a four-part end-of-semester homework "
        "packet using ONLY vocabulary and grammar introduced during the course.\n\n"
        "## Part 1: Character Writing List\n"
        "List 10–15 Hanzi characters, each with Pinyin and English meaning. "
        "Choose characters that reinforce key vocabulary from the course.\n\n"
        "## Part 2: Fill in the Blank\n"
        "Write 5–8 sentences in Chinese with one word or particle blanked out. "
        "Each blank should target a key grammar pattern. Include an answer key at the end.\n\n"
        "## Part 3: Translation (English → Chinese)\n"
        "Provide 5 English sentences for the student to translate into Chinese. "
        "Choose sentences that practice key grammar structures. Include an answer key.\n\n"
        "## Part 4: Short Composition\n"
        "Give a creative writing prompt (2–3 sentences describing a scenario). "
        "Ask the student to write 3–5 sentences in Chinese using at least 5 vocabulary "
        "items from the course.\n\n"
        "Return only the Markdown, no surrounding text or code fences.\n\n"
        "=== LESSON PLANS ===\n"
        f"{plans_text}"
    )
    return _generate(prompt)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def main() -> None:
    token = graph_authenticate()
    notebook_id = find_notebook(token)
    plans_text, _ = read_section_pages(token, notebook_id, SPRING_SECTION)
    handouts_sid = find_or_create_section(token, notebook_id, HANDOUTS_SECTION)

    artifacts = [
        ("vocabulary table",  VOCAB_TABLE_FILE,    generate_vocab_table,    "Spring 2026 — Vocabulary Table"),
        ("sentence table",    SENTENCE_TABLE_FILE,  generate_sentence_table, "Spring 2026 — Sentence Table"),
        ("grammar summary",   GRAMMAR_FILE,         generate_grammar_summary,"Spring 2026 — Grammar Summary"),
        ("homework packet",   HOMEWORK_FILE,        generate_homework,       "Spring 2026 — Homework Packet"),
    ]

    for label, path, fn, title in artifacts:
        print(f"Generating {label}...")
        md = fn(plans_text)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"Saved {path}")
        html = markdown.markdown(md, extensions=["tables", "fenced_code"])
        create_onenote_page(token, handouts_sid, title, html)

    print("\nDone. 4 handout files saved locally and 4 OneNote pages created.")


if __name__ == "__main__":
    main()
