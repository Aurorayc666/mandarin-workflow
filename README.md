# Mandarin Class Workflow

Three scripts that together manage a Mandarin Chinese class — from preparing
each lesson to wrapping up a semester and planning the next one.

| Script | When to run | What it does |
|--------|-------------|--------------|
| `manage_class.py` | Before each class | Reads all past OneNote plans → Gemini → new lesson plan + Gmail recap draft |
| `generate_handouts.py` | End of semester | Reads all class plans → Gemini → vocab table, sentence table, grammar summary, homework packet |
| `plan_semester.py` | Planning next semester | Reads Spring plans → Gemini → Summer syllabus (8 classes, one level up) + 8 individual class plans |

All three scripts share the same OneNote notebook (`Mandarin_Class`), the same
Gemini model (`gemini-2.5-flash`), and the same cached Microsoft auth token.

---

## OneNote structure

```
Mandarin_Class (notebook)
├── Spring 2026 Class Plans   ← source for all three scripts
├── Spring 2026 Handouts      ← created by manage_class_v2.py
└── Summer 2026               ← created by manage_class_v3.py
```

**Before running V2 or V3:** rename your existing class-plan section to
`Spring 2026 Class Plans` in OneNote. You can override the name via the
`SPRING_SECTION` environment variable if you prefer a different name.

---

## Setup

### 1. Python environment

```powershell
# Python 3.10+ required (Anaconda interpreter used here):
& "$env:USERPROFILE\anaconda3\python.exe" -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Gemini API key

Get a key at <https://aistudio.google.com/apikey>, then:

```powershell
$env:GEMINI_API_KEY = "your-key-here"
# or persist across terminals:
setx GEMINI_API_KEY "your-key-here"
```

### 3. Microsoft Azure app registration (OneNote)

At <https://entra.microsoft.com> → **App registrations** → **New registration**:

1. **Supported account types:** *Accounts in any organizational directory and
   personal Microsoft accounts*.
2. **Authentication** → **Add a platform** → **Mobile and desktop applications** →
   redirect URI **`http://localhost`**.
3. **Authentication** → **Advanced settings** → **Allow public client flows** = **Yes**.
4. Copy the **Application (client) ID**:
   ```powershell
   $env:MS_CLIENT_ID = "your-client-id-here"
   ```

No client secret needed — this is a public (desktop) client.

### 4. Gmail OAuth client (V1 only)

At <https://console.cloud.google.com/>:

1. Create/select a project; **enable the Gmail API**.
2. Configure the **OAuth consent screen** (External; add your Gmail as a test user).
3. **Credentials** → **Create Credentials** → **OAuth client ID** → **Desktop app**.
4. Download the JSON and save as **`credentials.json`** next to `manage_class.py`.

V2 and V3 do not use Gmail — only Microsoft Graph + Gemini.

### 5. Environment variables

```powershell
$env:RECAP_TO      = "student@example.com"   # V1: recap draft recipient
$env:RECAP_BCC     = "a@b.com, c@d.com"      # V1: optional BCC
$env:NOTEBOOK_NAME = "Mandarin_Class"        # all scripts (default shown)
$env:SPRING_SECTION = "Spring 2026 Class Plans"  # V2+V3 (default shown)
```

See `.env.example` for the full list.

---

## V1 — Lesson plan + recap (`manage_class.py`)

Run before each class session.

```powershell
.\.venv\Scripts\Activate.ps1
python manage_class.py
```

**What happens:**
1. Reads every page in `Mandarin_Class` (all sections) via Microsoft Graph.
2. Asks Gemini for a Markdown lesson plan for the next class.
3. Shows you the draft in the terminal — press Enter to approve or type feedback
   to regenerate.
4. Saves the approved plan to `next_lesson_plan.md` and a new OneNote page.
5. Generates an HTML recap email and saves it as a Gmail draft.

On the first run two browser windows open (Microsoft, then Google) for OAuth
consent. Tokens are cached so later runs are silent.

**Flags:**

```powershell
python manage_class.py --auto   # skip review; accept first draft (for scheduled runs)
```

---

## V2 — End-of-semester handouts (`generate_handouts.py`)

Run once at the end of a semester to produce student-facing reference materials.

```powershell
.\.venv\Scripts\Activate.ps1
python generate_handouts.py
```

**What happens:**
1. Reads all pages from the `Spring 2026 Class Plans` section (ascending order,
   Class 1 → Class N, so Gemini sees the curriculum in sequence).
2. Calls Gemini four times to generate:
   - **Vocabulary table** — every word introduced, sorted by Pinyin (Hanzi | Pinyin | English)
   - **Sentence table** — all key sentences grouped by class
   - **Grammar summary** — one section per grammar point with examples
   - **Homework packet** — character writing list, fill-in-the-blank, EN→ZH translation, composition prompt
3. Saves each document locally (`.md`) and as a OneNote page in a new
   `Spring 2026 Handouts` section (created automatically if needed).

No interactive review — V2 outputs are reference materials, not lesson plans.

**Local output files:**

| File | Content |
|------|---------|
| `vocab_table.md` | Vocabulary table |
| `sentence_table.md` | Sentence table |
| `grammar_summary.md` | Grammar summary |
| `homework.md` | Homework packet |

---

## V3 — Next-semester planning (`plan_semester.py`)

Run once when planning a new semester, after completing V2.

```powershell
.\.venv\Scripts\Activate.ps1
python plan_semester.py
```

**What happens:**
1. Reads all pages from `Spring 2026 Class Plans`.
2. Generates a Summer 2026 syllabus — 8 classes, one level above Spring 2026
   (approximately HSK 2 / intermediate), building directly on what was taught.
3. Shows the syllabus in the terminal — approve or give feedback to refine.
4. Creates a `Summer 2026` section in OneNote and saves the syllabus as the first page.
5. Generates 8 individual class plans (one Gemini call each) and saves each
   locally and as a OneNote page.

**Flags:**

```powershell
python plan_semester.py --auto   # accept first syllabus draft, then generate all 8 plans unattended
```

**Local output files:**

| File | Content |
|------|---------|
| `summer_2026_syllabus.md` | Approved Summer 2026 syllabus |
| `summer_2026_class_01.md` … `_08.md` | Individual Summer class plans |

---

## Using your custom Gemini "Gem"

Gems **cannot be called through the Gemini API**. To replicate your Gem's behavior,
copy its system instructions into `gem_instructions.txt` next to the scripts. All
three scripts will pick it up automatically on every Gemini call.

---

## Files

| File | Purpose |
|------|---------|
| `manage_class.py` | V1 — per-class lesson plan + recap |
| `generate_handouts.py` | V2 — end-of-semester student handouts |
| `plan_semester.py` | V3 — next-semester syllabus + class plans |
| `requirements.txt` | Python dependencies |
| `.env.example` | All environment variables with descriptions |
| `next_lesson_plan.md` | V1 generated output (git-ignored) |
| `vocab_table.md` / `sentence_table.md` / `grammar_summary.md` / `homework.md` | V2 generated outputs (git-ignored) |
| `summer_2026_syllabus.md` / `summer_2026_class_*.md` | V3 generated outputs (git-ignored) |
| `credentials.json` | Gmail OAuth client (you provide; git-ignored) |
| `gmail_token.json` / `ms_token_cache.json` | Cached auth tokens (auto-created; git-ignored) |
| `gem_instructions.txt` | Optional Gemini system instructions (git-ignored) |

Secrets and tokens are listed in `.gitignore` and must never be committed.
