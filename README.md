# Mandarin Class Workflow

Three scripts that together manage a Mandarin Chinese class — from planning
each semester to preparing each lesson and wrapping up with student handouts.

| Script | When to run | What it does |
|--------|-------------|--------------|
| `generate_syllabus.py` | Start of semester | Reads all past class plans + recap emails → Gemini → semester syllabus |
| `generate_next_class_plan.py` | Before each class | Reads all past class plans + syllabus + all sent Gmail → Gemini → next lesson plan + Gmail recap draft |
| `generate_handouts.py` | After every 2 classes | Reads the requested class range's plans + recap emails → Gemini → a Study Guide (vocab table, sentence table, grammar summary) and a Homework packet for that range (excludes anything already in the prior semester's handouts) |

All scripts share the same OneNote notebook (`Mandarin_Class`), the same
Gemini model (`gemini-2.5-flash`), and cached auth tokens.

---

## Correct run order

```
generate_syllabus.py        ← run once at the start of the semester
      ↓
generate_next_class_plan.py ← run before each class (repeat 8×)
      ↓
generate_handouts.py        ← run after every 2 classes, for that range's handouts
```

`generate_syllabus.py` saves a local `{slug}_syllabus.md` file (e.g.
`summer_2026_syllabus.md`) that `generate_next_class_plan.py` uses as a
scope guide. Run the syllabus script first so the file exists.

**Output folders** (both git-ignored):

- `back_files/` — local Markdown copies of every generated lesson plan,
  syllabus and handout. The scripts read these back (the class plan script
  loads the syllabus; the handouts script checks past handouts to avoid
  repeats and to suggest the next class range), so keep them here.
- `For_Students/` — Word + PDF copies of each syllabus and handout to send
  to students.

**Enrollment email:** after a syllabus is generated, `generate_syllabus.py`
creates a Gmail draft (never sent automatically) to `RECAP_TO` / `RECAP_BCC`
with the PDF syllabus attached. Its wording is in `syllabus_email_template.txt`
(first line = subject; `{semester}`, `{level}`, `{start_date}` etc. are filled
in). Course details come from `.env`: `COURSE_LEVEL`, `CLASS_START_DATE`
(YYYY-MM-DD), `CLASS_COUNT`, `CLASS_TIME`, `TUITION`, `PRIVATE_RATE` — update
them with `SEMESTER` each semester. The template is git-ignored (personal
details), like `.env`.

---

## OneNote structure

```
Mandarin_Class (notebook)
├── Spring 2026 Class Plans   ← read by all scripts
├── Summer 2026 Class Plans   ← created automatically; new plans written here
└── Summer 2026 Handouts      ← created by generate_handouts.py
```

Sections are created automatically — no manual OneNote setup needed.

---

## Setup

### 1. Python environment

```powershell
& "$env:USERPROFILE\anaconda3\python.exe" -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Environment variables

Copy `.env.example` to `.env` and fill in your values — all scripts load it
automatically at startup:

```powershell
Copy-Item .env.example .env
# then open .env and fill in the values
```

See `.env.example` for descriptions of each variable.

### 3. Gemini API key

Get a key at <https://aistudio.google.com/apikey> and set `GEMINI_API_KEY` in `.env`.

### 4. Microsoft Azure app registration (OneNote)

At <https://entra.microsoft.com> → **App registrations** → **New registration**:

1. **Supported account types:** *Accounts in any organizational directory and
   personal Microsoft accounts*.
2. **Authentication** → **Add a platform** → **Mobile and desktop applications** →
   redirect URI **`http://localhost`**.
3. **Authentication** → **Advanced settings** → **Allow public client flows** = **Yes**.
4. Copy the **Application (client) ID** into `MS_CLIENT_ID` in `.env`.

No client secret needed — this is a public (desktop) client.

### 5. Gmail OAuth client

At <https://console.cloud.google.com/>:

1. Create/select a project; **enable the Gmail API**.
2. Configure the **OAuth consent screen** (External). Add your Gmail address as a
   **test user** — without this, sign-in fails with Error 403.
3. **Credentials** → **Create Credentials** → **OAuth client ID** → **Desktop app**.
4. Download the JSON and save as **`credentials.json`** next to the scripts.

All three scripts use Gmail (reading sent emails for context; creating recap drafts).
A browser opens once on the first run to grant consent; the token is cached in
`gmail_token.json` for subsequent runs.

---

## generate_syllabus.py — Semester syllabus

Run once at the start of each semester.

```powershell
.\.venv\Scripts\Activate.ps1
python generate_syllabus.py
```

**What it does:**

1. Prompts you for a semester name (e.g. `Summer 2026`).
2. Reads OneNote class plans and recap emails:
   - **Spring** → reads only the Spring section (retrospective)
   - **Summer / other** → reads ALL "class plans" sections + all recap emails; targets HSK Level 2
3. Asks Gemini to generate the syllabus with an HSK alignment analysis.
4. Shows the draft in the terminal — press Enter to approve or type feedback to refine.
5. Saves the approved syllabus locally as `{slug}_syllabus.md` and as a page in
   `"{semester} Class Plans"` in OneNote (created automatically).

**Flags:**

```powershell
python generate_syllabus.py --auto   # accept first draft without review
```

---

## generate_next_class_plan.py — Per-class lesson plan + recap

Run before each class session.

```powershell
.\.venv\Scripts\Activate.ps1
python generate_next_class_plan.py
```

**What it does:**

1. Reads every "class plans" section in OneNote (all semesters).
2. Loads the local syllabus file (`summer_2026_syllabus.md`) as a scope guide.
3. Reads all sent Gmail for additional context.
4. Asks Gemini for a Markdown lesson plan for the next class (no repeated content).
5. Shows the draft in the terminal — press Enter to approve or type feedback to refine.
6. Saves the plan to `next_lesson_plan.md` and a new OneNote page in
   `LESSON_PLAN_SECTION` (default: `Summer 2026 Class Plans`).
7. Generates an HTML recap email and saves it as a Gmail draft.

**Flags:**

```powershell
python generate_next_class_plan.py --auto   # skip review; accept first draft
```

---

## generate_handouts.py — Student handouts for a class range

Run after every 2 classes to produce those classes' student-facing reference
materials. Scoped to the requested range — reads only those classes' OneNote
pages and recap emails.

```powershell
.\.venv\Scripts\Activate.ps1
python generate_handouts.py
```

**What it does:**

1. Prompts for which classes to cover (e.g. `3-4`). Defaults to the next
   un-covered pair, computed by scanning existing local
   `summer_2026_class{N}-{M}_study_guide.md` files. Errors if the requested
   range goes past the number of `Class N ...` pages that exist in
   `SUMMER_SECTION` (default: `Summer 2026 Class Plans`).
2. Reads each class in the range's OneNote page plus its sent recap email.
3. Reads the prior semester's local handout files (`vocab_table.md`,
   `sentence_table.md`, `grammar_summary.md`, `homework.md`, if present) and
   instructs Gemini to exclude anything already covered there.
4. Calls Gemini to generate, for that range only:
   - **Study Guide** — vocabulary table, sentence table, and grammar summary
     (Hanzi | Pinyin | English throughout), combined into one document
   - **Homework packet** — character writing list, fill-in-the-blank, EN→ZH translation, composition prompt
5. Saves each document locally (`.md`) and as a OneNote page in
   `Summer 2026 Handouts` (created automatically), titled `Class N-M - <Document>`.

Re-run after each subsequent pair of classes to generate that range's own set of 2 pages.

**Local output files** (`N-M` = class range, e.g. `1-2`):

| File | Content |
|------|---------|
| `summer_2026_class{N-M}_study_guide.md` | Vocabulary table, sentence table, and grammar summary for classes N-M |
| `summer_2026_class{N-M}_homework.md` | Homework packet for classes N-M |

---

## Using a custom Gemini system instruction

Gemini Gems cannot be called through the API. To replicate Gem behavior, copy
its system instructions into `gem_instructions.txt` next to the scripts. All
scripts pick it up automatically on every Gemini call.

---

## Files

| File | Purpose |
|------|---------|
| `generate_next_class_plan.py` | Per-class lesson plan + recap email |
| `generate_handouts.py` | Student handouts (study guide + homework) for a class range |
| `generate_syllabus.py` | Semester syllabus generator |
| `utils.py` | Shared auth, OneNote, Gemini, and Gmail helpers |
| `requirements.txt` | Python dependencies |
| `.env` | Your secrets and config (git-ignored; copy from `.env.example`) |
| `.env.example` | All environment variables with descriptions |
| `next_lesson_plan.md` | Generated lesson plan output (git-ignored) |
| `vocab_table.md` / `sentence_table.md` / `grammar_summary.md` / `homework.md` | Prior semester's handout outputs, used as a do-not-repeat reference (git-ignored) |
| `summer_2026_class{N-M}_study_guide.md` / `..._homework.md` | Per-range handout outputs for the current semester (git-ignored) |
| `summer_2026_syllabus.md` | Generated syllabus (git-ignored) |
| `credentials.json` | Gmail OAuth client (you provide; git-ignored) |
| `gmail_token.json` / `ms_token_cache.json` | Cached auth tokens (auto-created; git-ignored) |
| `gem_instructions.txt` | Optional Gemini system instructions (git-ignored) |

Secrets and tokens are listed in `.gitignore` and must never be committed.
