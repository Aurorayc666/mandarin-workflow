# Mandarin Class Workflow

Reads your past Mandarin lesson plans from **OneNote**, uses the **Gemini API** to
generate a lesson plan for the next class and a recap email, then:

- saves the new lesson plan back into **OneNote** (and a local `next_lesson_plan.md`), and
- creates the recap as a draft in your **Gmail**.

Voice-recording digestion is planned for a future version.

## How it works

`manage_class.py`:

1. Authenticates to Microsoft Graph (browser opens once; token cached in
   `ms_token_cache.json`).
2. Reads every page in the `NOTEBOOK_NAME` notebook (all sections).
3. Asks Gemini (`gemini-2.5-flash`) for a Markdown lesson plan + an HTML recap.
4. Writes the plan to `next_lesson_plan.md` and to a new OneNote page in the
   section of your most recently edited plan.
5. Authenticates to Gmail (browser opens once; token cached in `gmail_token.json`)
   and creates the recap draft.

## Setup

### 1. Python environment

A virtual environment lives in `.venv`. To recreate it:

```powershell
# Use any Python 3.10+ (this project was set up with the Anaconda interpreter):
& "$env:USERPROFILE\anaconda3\python.exe" -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Gemini API key

1. Get a key at <https://aistudio.google.com/apikey>.
2. Set it: `$env:GEMINI_API_KEY = "AQ.Ab8RN6IrjmvmFkWf902GOTcJxc0vzlfdKwwFSoGcM98NbsJjyQ"` (or `setx GEMINI_API_KEY "your-key"`
   to persist across terminals).

### 3. Microsoft Azure app registration (OneNote)

At <https://entra.microsoft.com> → **App registrations** → **New registration**:

1. **Supported account types:** *Accounts in any organizational directory and
   personal Microsoft accounts* (matches the `/common` authority used in the script).
2. **Authentication** → **Add a platform** → **Mobile and desktop applications** →
   redirect URI **`http://localhost`**.
3. **Authentication** → **Advanced settings** → **Allow public client flows** =
   **Yes**.
4. Copy the **Application (client) ID** and set it: `$env:MS_CLIENT_ID = "faab42e9-c5f1-4b9f-8a9a-582c61f74fa4"`.

No client secret is needed — this is a public (desktop) client. App-only auth is
retired for OneNote, so the script signs you in interactively (delegated).

### 4. Gmail OAuth client

At <https://console.cloud.google.com/>:

1. Create/select a project; **enable the Gmail API**.
2. Configure the **OAuth consent screen** (External; add your Gmail as a test user).
3. **Credentials** → **Create Credentials** → **OAuth client ID** →
   application type **Desktop app**.
4. Download the JSON and save it as **`credentials.json`** next to `manage_class.py`.

### 5. Recipient + notebook

- `$env:RECAP_TO = "student@example.com"` — who the recap draft is addressed to (visible To field).
- `$env:RECAP_BCC = "a@example.com, b@example.com"` — optional comma-separated BCC recipients.
- `$env:NOTEBOOK_NAME = "Mandarin"` — the OneNote notebook to read (defaults to
  `Mandarin_Class`; you can also edit the default in `manage_class.py`).

See `.env.example` for the full list of variables.

## Run

```powershell
.\.venv\Scripts\Activate.ps1
python manage_class.py
```

On the first run, two browser windows open (Microsoft, then Google) for consent.
After that, the cached tokens are reused.

You review and refine the lesson plan in the terminal before anything is saved;
only after you approve it does the script save to OneNote and draft the recap.

### Unattended runs

`python manage_class.py --auto` skips the review loop and accepts the first
lesson plan — intended for fully automatic / scheduled runs (the recap is only
ever a draft, never sent automatically).

## Using your custom Gemini "Gem"

Gems live in the Gemini app and **cannot be called through the Gemini API**. To get
equivalent behavior, copy your Mandarin Gem's instructions into a file named
`gem_instructions.txt` next to `manage_class.py`. If that file exists, its
contents are sent as the system instruction on every Gemini call. (If your Gem
relies on uploaded knowledge files, those aren't included — only the instruction
text.)

## Files

| File | Purpose |
|------|---------|
| `manage_class.py` | Main script |
| `requirements.txt` | Dependencies |
| `.env.example` | Required environment variables |
| `next_lesson_plan.md` | Generated lesson plan (local copy) |
| `credentials.json` | Gmail OAuth client (you provide; git-ignored) |
| `gmail_token.json` / `ms_token_cache.json` | Cached tokens (auto-created; git-ignored) |

Secrets and tokens are listed in `.gitignore` and must never be committed.
