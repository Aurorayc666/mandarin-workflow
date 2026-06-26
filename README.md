# Mandarin Class Workflow

Three scripts that together manage a Mandarin Chinese class — from planning
each semester to preparing each lesson and wrapping up with student handouts.

| Script | When to run | What it does |
|--------|-------------|--------------|
| `generate_syllabus.py` | Start of semester | Reads all past class plans + recap emails → Gemini → semester syllabus |
| `generate_next_class_plan.py` | Before each class | Reads all past class plans + syllabus + all sent Gmail → Gemini → next lesson plan + Gmail recap draft |
| `generate_handouts.py` | End of semester | Reads all class plans → Gemini → vocab table, sentence table, grammar summary, homework packet |

All scripts share the same OneNote notebook (`Mandarin_Class`), the same
Gemini model (`gemini-2.5-flash`), and cached auth tokens.

---

## Correct run order

```
generate_syllabus.py        ← run once at the start of the semester
      ↓
generate_next_class_plan.py ← run before each class (repeat 8×)
      ↓
generate_handouts.py        ← run once at the end of the semester
```

`generate_syllabus.py` saves a local `{slug}_syllabus.md` file (e.g.
`summer_2026_syllabus.md`) that `generate_next_class_plan.py` uses as a
scope guide. Run the syllabus script first so the file exists.

---

## OneNote structure

```
Mandarin_Class (notebook)
├── Spring 2026 Class Plans   ← read by all scripts
├── Spring 2026 Handouts      ← created by generate_handouts.py
└── Summer 2026 Class Plans   ← created automatically; new plans written here
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

## generate_handouts.py — End-of-semester student handouts

Run once at the end of a semester to produce student-facing reference materials.

```powershell
.\.venv\Scripts\Activate.ps1
python generate_handouts.py
```

**What it does:**

1. Reads all pages from the `SPRING_SECTION` section (default: `Spring 2026 Class Plans`).
2. Calls Gemini four times to generate:
   - **Vocabulary table** — every word introduced, sorted by Pinyin (Hanzi | Pinyin | English)
   - **Sentence table** — all key sentences grouped by class
   - **Grammar summary** — one section per grammar point with examples
   - **Homework packet** — character writing list, fill-in-the-blank, EN→ZH translation, composition prompt
3. Saves each document locally (`.md`) and as a OneNote page in
   `Spring 2026 Handouts` (created automatically).

**Local output files:**

| File | Content |
|------|---------|
| `vocab_table.md` | Vocabulary table |
| `sentence_table.md` | Sentence table |
| `grammar_summary.md` | Grammar summary |
| `homework.md` | Homework packet |

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
| `generate_handouts.py` | End-of-semester student handouts |
| `generate_syllabus.py` | Semester syllabus generator |
| `utils.py` | Shared auth, OneNote, Gemini, and Gmail helpers |
| `requirements.txt` | Python dependencies |
| `.env` | Your secrets and config (git-ignored; copy from `.env.example`) |
| `.env.example` | All environment variables with descriptions |
| `next_lesson_plan.md` | Generated lesson plan output (git-ignored) |
| `vocab_table.md` / `sentence_table.md` / `grammar_summary.md` / `homework.md` | Handout outputs (git-ignored) |
| `summer_2026_syllabus.md` | Generated syllabus (git-ignored) |
| `credentials.json` | Gmail OAuth client (you provide; git-ignored) |
| `gmail_token.json` / `ms_token_cache.json` | Cached auth tokens (auto-created; git-ignored) |
| `gem_instructions.txt` | Optional Gemini system instructions (git-ignored) |

Secrets and tokens are listed in `.gitignore` and must never be committed.
