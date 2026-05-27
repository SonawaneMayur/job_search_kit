# Job Search Kit

Local-first web app that implements the four-phase Job Application Strategist
workflow: screen a JD, generate tailored assets, run the pre-submission
checklist, and track everything in one place. Nothing leaves the machine
unless you explicitly enable the optional Adzuna job-search integration.

## Stack

- FastAPI + Jinja2 + HTMX (no Node toolchain)
- SQLite (file at `data/kit.db`)
- Ollama on `http://localhost:11434` for local LLM inference (JSON mode)
- Optional: Adzuna API for job search

## Setup

1. Install Ollama and pull a JSON-capable model:
   ```bash
   ollama pull llama3.1:8b      # or qwen2.5:14b, mistral, etc.
   ollama serve                  # usually started automatically by the app
   ```
2. Run the app:
   ```bash
   ./run.sh
   ```
   The script creates a virtualenv at `.venv/`, installs requirements, and
   starts uvicorn on http://127.0.0.1:8765.

3. (One-time) install Chromium for the "Render with browser" button:
   ```bash
   source .venv/bin/activate
   python -m playwright install chromium
   ```

4. Open the app, go to **Profile**, paste your master resume, set GC stage /
   AC21 eligibility / priority date, and save. (Optionally drop in Adzuna
   credentials for the Search page.)

## Workflow

1. **New application** → paste a JD URL (auto-fetch) or paste JD text.
   Click "Screen this JD". Phase 1 runs and the app saves the result.
2. The detail page shows sponsorship posture, role fit, top gaps, and the
   verdict. If `APPLY` or `APPLY_WITH_OUTREACH`, click **Generate assets** —
   Phase 2 produces a tailored resume, cover letter (with the conditional
   visa paragraph), and two LinkedIn outreach drafts.
3. Download each as `.md`, run through the Phase 3 checklist, set the
   status as you progress.
4. The dashboard surfaces stats and an at-a-glance list of every application
   with verdict, sponsorship posture, fit, status, and follow-up date.

## Phase 4 tracker JSON

Available per-application at `/applications/{id}/tracker.json`.

## Capturing JDs from any site

Three layered options, in order of robustness:

1. **Plain URL fetch** (`Fetch` button) — Greenhouse and Lever URLs hit
   their public JSON APIs directly. Other sites get a normal HTTP fetch +
   HTML extraction. Fast, no extra deps.

2. **Headless browser** (`Render with browser` button) — uses Playwright to
   render JS-heavy SPAs server-side. Good for Workday and Ashby public pages.
   Optional install:
   ```bash
   source .venv/bin/activate
   pip install playwright
   python -m playwright install chromium
   ```
   Will NOT solve LinkedIn / Indeed — those require login + active anti-bot
   evasion. Use the bookmarklet for those.

3. **Bookmarklet** — visit `/bookmarklet` and drag the button to your
   browser's bookmarks bar. From then on, open ANY job posting in your own
   browser (already logged in to LinkedIn etc.), click the bookmark, and a
   new tab opens at the local app's New Application form with the JD
   pre-filled. Works on every site because it runs in your own browser
   session.

## What's intentionally not built

- Server-side LinkedIn / Indeed scraping. Use the bookmarklet instead.
- Auto-submission to job boards — the workflow is "assets generated —
  pending user submission" by design.
- ATS keyword scanning. Use Jobscan or similar externally if you're willing
  to send the JD + resume to a third party.

## Files

- `app/main.py` — FastAPI bootstrap
- `app/db.py` — SQLite schema + CRUD
- `app/llm.py` — Ollama JSON-mode client
- `app/prompts.py` — Phase 1 / Phase 2 prompt templates
- `app/jd_fetch.py` — URL → JD text + Adzuna search
- `app/routes.py` — all HTTP routes
- `app/templates/` — Jinja2 templates
- `app/static/style.css` — dark UI

## Reset

```bash
rm data/kit.db
./run.sh   # recreates schema, seeds empty profile
```
