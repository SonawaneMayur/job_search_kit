from datetime import date, timedelta
from pathlib import Path
import json

from fastapi import APIRouter, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from . import db, llm, jd_fetch

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")

router = APIRouter()


# ---------- helpers ----------

def _nav_active(request: Request) -> str:
    p = request.url.path
    if p.startswith("/profile"):
        return "profile"
    if p.startswith("/search"):
        return "search"
    if p.startswith("/bookmarklet"):
        return "bookmarklet"
    if p.startswith("/applications/new"):
        return "new"
    return "dashboard"


def _ctx(request: Request, **extra) -> dict:
    return {
        "request": request,
        "nav": _nav_active(request),
        "profile": db.fetch_profile(),
        **extra,
    }


# ---------- dashboard ----------

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, status: str = "All"):
    apps = db.list_applications(status_filter=status)
    s = db.stats()
    return templates.TemplateResponse(
        "dashboard.html",
        _ctx(request, applications=apps, stats=s, status_filter=status),
    )


# ---------- profile ----------

@router.get("/profile", response_class=HTMLResponse)
def profile_get(request: Request):
    models = llm.list_local_models(db.fetch_profile().get("ollama_url") or "")
    return templates.TemplateResponse("profile.html", _ctx(request, ollama_models=models))


@router.post("/profile", response_class=HTMLResponse)
def profile_post(
    request: Request,
    user_name: str = Form(""),
    current_visa: str = Form(""),
    gc_stage: str = Form(""),
    priority_date: str = Form(""),
    ac21_eligible: str = Form(""),
    ead: str = Form(""),
    target_roles: str = Form(""),
    master_resume: str = Form(""),
    ollama_url: str = Form("http://localhost:11434"),
    ollama_model: str = Form("llama3.1:8b"),
    adzuna_app_id: str = Form(""),
    adzuna_app_key: str = Form(""),
):
    db.update_profile({
        "user_name": user_name,
        "current_visa": current_visa,
        "gc_stage": gc_stage,
        "priority_date": priority_date,
        "ac21_eligible": ac21_eligible,
        "ead": ead,
        "target_roles": target_roles,
        "master_resume": master_resume,
        "ollama_url": ollama_url,
        "ollama_model": ollama_model,
        "adzuna_app_id": adzuna_app_id,
        "adzuna_app_key": adzuna_app_key,
    })
    return RedirectResponse("/profile?saved=1", status_code=303)


# ---------- new application ----------

@router.get("/applications/new", response_class=HTMLResponse)
def new_app_get(
    request: Request,
    url: str = "",
    jd: str = "",
    company: str = "",
    role: str = "",
):
    return templates.TemplateResponse(
        "application_new.html",
        _ctx(
            request,
            prefill_url=url,
            prefill_jd=jd,
            prefill_company=company,
            prefill_role=role,
        ),
    )


@router.post("/fetch-url", response_class=HTMLResponse)
def fetch_url(request: Request, url: str = Form(...)):
    result = jd_fetch.fetch_jd_from_url(url)
    return templates.TemplateResponse(
        "partials/fetch_result.html",
        {"request": request, "result": result, "url": url},
    )


@router.post("/fetch-render-url", response_class=HTMLResponse)
def fetch_render_url(request: Request, url: str = Form(...)):
    result = jd_fetch.fetch_jd_with_browser(url)
    return templates.TemplateResponse(
        "partials/fetch_result.html",
        {"request": request, "result": result, "url": url},
    )


@router.post("/import", response_class=HTMLResponse)
def import_jd(
    request: Request,
    url: str = Form(""),
    title: str = Form(""),
    text: str = Form(""),
):
    """Receives a POST from the bookmarklet running in the user's logged-in browser.
    Renders the New Application form pre-filled so the user can review and screen."""
    return templates.TemplateResponse(
        "application_new.html",
        _ctx(
            request,
            prefill_url=url,
            prefill_jd=text,
            prefill_company="",
            prefill_role=title,
            imported=True,
        ),
    )


@router.get("/bookmarklet", response_class=HTMLResponse)
def bookmarklet(request: Request):
    from urllib.parse import quote
    base = str(request.base_url).rstrip("/")
    src = _build_bookmarklet(base + "/import")
    href = "javascript:" + quote(src, safe="")
    return templates.TemplateResponse(
        "bookmarklet.html",
        _ctx(
            request,
            base_url=base,
            bookmarklet_href=href,
            bookmarklet_source=src,
        ),
    )


def _build_bookmarklet(target_url: str) -> str:
    """Single-line JS that runs in any page. Grabs the JD text and form-POSTs
    it to the local app, opening the New Application form in a new tab."""
    return (
        "(function(){"
        f"var APP={target_url!r};"
        "var sels=['.jobs-description__content','.jobs-description-content',"
        "'[data-automation-id=\"jobPostingDescription\"]','#job-details',"
        "'.job-description','main','article','#content'];"
        "var node=null;"
        "for(var i=0;i<sels.length;i++){"
          "var n=document.querySelector(sels[i]);"
          "if(n&&(n.innerText||'').length>400){node=n;break;}"
        "}"
        "if(!node)node=document.body;"
        "var t=(node.innerText||'').replace(/\\n{3,}/g,'\\n\\n').trim();"
        "var f=document.createElement('form');"
        "f.method='POST';f.action=APP;f.target='_blank';f.style.display='none';"
        "function a(k,v){var i=document.createElement('input');i.type='hidden';i.name=k;i.value=v;f.appendChild(i);}"
        "a('url',location.href);a('title',document.title);a('text',t);"
        "document.body.appendChild(f);f.submit();"
        "})();"
    )


@router.post("/applications", response_class=HTMLResponse)
def create_application(
    request: Request,
    company: str = Form(""),
    role: str = Form(""),
    jd_url: str = Form(""),
    jd_text: str = Form(...),
):
    profile = db.fetch_profile()
    if not (profile.get("master_resume") or "").strip():
        raise HTTPException(400, "Master resume is empty. Fill in your profile first.")

    try:
        screening = llm.screen_jd(profile, jd_text)
    except llm.LLMError as e:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(e)},
            status_code=502,
        )

    verdict = (screening.get("verdict") or {}).get("decision", "")
    posture = (screening.get("sponsorship") or {}).get("posture", "")
    fit = screening.get("fit") or {}
    company = company or screening.get("company") or ""
    role = role or screening.get("role_title") or ""

    fields = {
        "company": company,
        "role": role,
        "jd_url": jd_url,
        "jd_text": jd_text,
        "seniority_match": fit.get("seniority", ""),
        "sponsorship_posture": posture,
        "verdict": verdict,
        "screening_json": json.dumps(screening, indent=2),
        "status": "Screened" if verdict != "SKIP" else "Skipped",
        "date_generated": date.today().isoformat(),
        "follow_up_date": (date.today() + timedelta(days=7)).isoformat(),
    }
    app_id = db.create_application(fields)
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


# ---------- detail ----------

@router.get("/applications/{app_id}", response_class=HTMLResponse)
def app_detail(request: Request, app_id: int):
    app_row = db.get_application(app_id)
    if not app_row:
        raise HTTPException(404)
    screening = {}
    if app_row.get("screening_json"):
        try:
            screening = json.loads(app_row["screening_json"])
        except json.JSONDecodeError:
            screening = {}
    tracker = _build_tracker(app_row)
    return templates.TemplateResponse(
        "application_detail.html",
        _ctx(request, app=app_row, screening=screening, tracker=tracker),
    )


@router.post("/applications/{app_id}/generate", response_class=HTMLResponse)
def generate(request: Request, app_id: int):
    app_row = db.get_application(app_id)
    if not app_row:
        raise HTTPException(404)
    profile = db.fetch_profile()
    screening = json.loads(app_row.get("screening_json") or "{}")
    try:
        assets = llm.generate_assets(profile, app_row["jd_text"], screening)
    except llm.LLMError as e:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": str(e)},
            status_code=502,
        )
    db.update_application(app_id, {
        "resume_md": assets.get("resume_md", ""),
        "cover_letter_md": assets.get("cover_letter_md", ""),
        "outreach_md": _join_outreach(assets),
        "ac21_used_in_letter": 1 if assets.get("ac21_used_in_letter") else 0,
        "status": "Assets generated — pending user submission",
    })
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


def _join_outreach(assets: dict) -> str:
    hm = (assets.get("outreach_hm_md") or "").strip()
    peer = (assets.get("outreach_peer_md") or "").strip()
    parts = []
    if hm:
        parts.append(f"### Hiring manager\n\n{hm}")
    if peer:
        parts.append(f"### Peer / referral\n\n{peer}")
    return "\n\n".join(parts)


@router.post("/applications/{app_id}/status", response_class=HTMLResponse)
def update_status(request: Request, app_id: int, status: str = Form(...), notes: str = Form("")):
    db.update_application(app_id, {"status": status, "notes": notes})
    return RedirectResponse(f"/applications/{app_id}", status_code=303)


@router.post("/applications/{app_id}/delete")
def delete_app(app_id: int):
    db.delete_application(app_id)
    return RedirectResponse("/", status_code=303)


@router.get("/applications/{app_id}/download/{kind}", response_class=PlainTextResponse)
def download(app_id: int, kind: str):
    app_row = db.get_application(app_id)
    if not app_row:
        raise HTTPException(404)
    field_map = {
        "resume": "resume_md",
        "cover": "cover_letter_md",
        "outreach": "outreach_md",
    }
    field = field_map.get(kind)
    if not field:
        raise HTTPException(400, "Unknown asset")
    content = app_row.get(field) or ""
    fname = f"{(app_row['company'] or 'company').replace(' ', '_')}_{kind}.md"
    return PlainTextResponse(
        content,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/applications/{app_id}/tracker.json")
def tracker_json(app_id: int):
    app_row = db.get_application(app_id)
    if not app_row:
        raise HTTPException(404)
    return JSONResponse(_build_tracker(app_row))


def _build_tracker(app_row: dict) -> dict:
    return {
        "company": app_row.get("company", ""),
        "role": app_row.get("role", ""),
        "jd_url": app_row.get("jd_url", ""),
        "seniority_match": app_row.get("seniority_match", ""),
        "sponsorship_posture": app_row.get("sponsorship_posture", ""),
        "verdict": app_row.get("verdict", ""),
        "ac21_used_in_letter": bool(app_row.get("ac21_used_in_letter")),
        "date_generated": app_row.get("date_generated", ""),
        "outreach_targets": [],
        "follow_up_date": app_row.get("follow_up_date", ""),
        "status": app_row.get("status", ""),
    }


# ---------- search ----------

@router.get("/search", response_class=HTMLResponse)
def search_get(request: Request):
    return templates.TemplateResponse("search.html", _ctx(request, results=None))


@router.post("/search", response_class=HTMLResponse)
def search_post(
    request: Request,
    what: str = Form(...),
    where: str = Form("us"),
):
    profile = db.fetch_profile()
    result = jd_fetch.adzuna_search(
        profile.get("adzuna_app_id", ""),
        profile.get("adzuna_app_key", ""),
        what=what,
        where=where,
    )
    return templates.TemplateResponse(
        "search.html",
        _ctx(request, results=result, query=what, where=where),
    )
