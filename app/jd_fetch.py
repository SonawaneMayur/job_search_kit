"""JD fetching: URL -> cleaned text, with site-specific extractors for the most
common ATS platforms (Greenhouse, Lever). Also: optional Adzuna search."""
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)


# ---------- Greenhouse ----------

GREENHOUSE_PATHS = (
    re.compile(r"^/([^/]+)/jobs/(\d+)"),                  # boards.greenhouse.io/{board}/jobs/{id}
    re.compile(r"^/embed/job_app\?for=([^&]+)&token=(\d+)"),
)

def _fetch_greenhouse(parsed) -> dict:
    board = job_id = None
    for pat in GREENHOUSE_PATHS:
        m = pat.search(parsed.path) or pat.search(parsed.path + "?" + (parsed.query or ""))
        if m:
            board, job_id = m.group(1), m.group(2)
            break
    if not board:
        return {"ok": False, "error": "Could not parse Greenhouse URL.", "title": "", "text": ""}

    api = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
    try:
        with httpx.Client(timeout=30.0, headers=HEADERS, follow_redirects=True) as c:
            r = c.get(api)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"Greenhouse API call failed: {e}", "title": "", "text": ""}

    title = data.get("title", "")
    location = (data.get("location") or {}).get("name", "")
    company = (data.get("company") or {}).get("name", "") or board
    body = _html_to_text(data.get("content", ""))
    header = f"{title}\n{company} — {location}".strip()
    text = f"{header}\n\n{body}".strip()
    return {"ok": True, "title": title, "text": text}


# ---------- Lever ----------

LEVER_PATH = re.compile(r"^/([^/]+)/([0-9a-f-]{6,})")

def _fetch_lever(parsed) -> dict:
    m = LEVER_PATH.search(parsed.path)
    if not m:
        return {"ok": False, "error": "Could not parse Lever URL.", "title": "", "text": ""}
    company, posting_id = m.group(1), m.group(2)
    api = f"https://api.lever.co/v0/postings/{company}/{posting_id}?mode=json"
    try:
        with httpx.Client(timeout=30.0, headers=HEADERS, follow_redirects=True) as c:
            r = c.get(api)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"Lever API call failed: {e}", "title": "", "text": ""}

    title = data.get("text", "") or data.get("title", "")
    cats = data.get("categories") or {}
    header_bits = [title]
    if cats.get("team"): header_bits.append(cats["team"])
    if cats.get("location"): header_bits.append(cats["location"])
    if cats.get("commitment"): header_bits.append(cats["commitment"])
    header = " — ".join([b for b in header_bits if b])

    parts = [header]
    if data.get("descriptionPlain"):
        parts.append(data["descriptionPlain"])
    elif data.get("description"):
        parts.append(_html_to_text(data["description"]))

    for lst in data.get("lists") or []:
        section_title = lst.get("text", "")
        section_body = _html_to_text(lst.get("content", ""))
        parts.append(f"\n{section_title}\n{section_body}".strip())

    if data.get("additionalPlain"):
        parts.append(data["additionalPlain"])
    elif data.get("additional"):
        parts.append(_html_to_text(data["additional"]))

    text = "\n\n".join([p for p in parts if p]).strip()
    return {"ok": True, "title": title, "text": text}


# ---------- Generic fallback ----------

def _fetch_generic(url: str) -> dict:
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True, headers=HEADERS) as c:
            r = c.get(url)
            r.raise_for_status()
            html = r.text
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"Fetch failed: {e}", "title": "", "text": ""}

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    title = (soup.title.string.strip() if soup.title and soup.title.string else "")

    # Prefer structured selectors common across ATS / career pages
    selectors = [
        "main", "article", "[role=main]", "#content", "#main-content",
        ".job-description", ".jobsearch-jobDescriptionText",
        ".section.page-centered", "#app", "[data-automation-id='jobPostingDescription']",
    ]
    target = None
    for sel in selectors:
        node = soup.select_one(sel)
        if node:
            txt = node.get_text("\n", strip=True)
            if len(txt) > 400:
                target = node
                break
    if target is None:
        target = soup.body or soup

    text = target.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cleaned = "\n".join(lines)

    if len(cleaned) < 400:
        host = urlparse(url).netloc
        hint = ""
        if "linkedin.com" in host:
            hint = " (LinkedIn job descriptions are JS-rendered behind auth — paste JD text manually.)"
        elif "workday" in host or "myworkdayjobs" in host:
            hint = " (Workday is JS-rendered — paste JD text manually or use the company's own career page.)"
        elif "indeed.com" in host:
            hint = " (Indeed blocks bots — paste JD text manually.)"
        elif "ashbyhq" in host:
            hint = " (Ashby is a SPA — paste JD text manually.)"
        return {
            "ok": False,
            "error": f"Page returned very little text — site is likely JS-rendered or blocking bots.{hint}",
            "title": title,
            "text": cleaned,
        }
    return {"ok": True, "title": title, "text": cleaned}


def fetch_jd_from_url(url: str) -> dict:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "greenhouse.io" in host:
        return _fetch_greenhouse(parsed)
    if "lever.co" in host:
        return _fetch_lever(parsed)
    return _fetch_generic(url)


def fetch_jd_with_browser(url: str) -> dict:
    """Render the page with a real headless browser (Playwright). Used as a
    fallback for JS-heavy SPAs like Workday / Ashby. Will NOT solve LinkedIn —
    that needs your own logged-in browser session (use the bookmarklet)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "ok": False,
            "title": "",
            "text": "",
            "error": (
                "Playwright is not installed. Install it with:\n"
                "  pip install playwright\n"
                "  python -m playwright install chromium"
            ),
        }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            title = page.title()
            # Try common JD containers first, fall back to body.
            text = page.evaluate(
                """() => {
                  const sels = [
                    '[data-automation-id="jobPostingDescription"]',
                    '.jobs-description__content',
                    '.jobs-description-content',
                    '.job-description',
                    '#job-details',
                    'main', 'article'
                  ];
                  for (const s of sels) {
                    const n = document.querySelector(s);
                    if (n && n.innerText && n.innerText.length > 400) return n.innerText;
                  }
                  return document.body ? document.body.innerText : '';
                }"""
            )
            browser.close()
    except Exception as e:
        return {"ok": False, "title": "", "text": "", "error": f"Browser render failed: {e}"}

    cleaned = "\n".join(ln.strip() for ln in (text or "").splitlines() if ln.strip())
    if len(cleaned) < 300:
        return {
            "ok": False,
            "title": title or "",
            "text": cleaned,
            "error": "Browser rendered the page but extracted very little text. "
                     "The site may require login — try the bookmarklet from your own browser.",
        }
    return {"ok": True, "title": title or "", "text": cleaned}


# ---------- Adzuna (unchanged) ----------

def adzuna_search(app_id: str, app_key: str, what: str, where: str = "us", page: int = 1) -> dict:
    if not app_id or not app_key:
        return {"ok": False, "error": "Adzuna credentials missing. Add them in Settings."}
    url = f"https://api.adzuna.com/v1/api/jobs/{where}/search/{page}"
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "what": what,
        "results_per_page": 20,
        "content-type": "application/json",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"Adzuna error: {e}"}

    results = []
    for item in data.get("results", []):
        results.append({
            "title": item.get("title", ""),
            "company": (item.get("company") or {}).get("display_name", ""),
            "location": (item.get("location") or {}).get("display_name", ""),
            "url": item.get("redirect_url", ""),
            "description": item.get("description", ""),
            "created": item.get("created", ""),
        })
    return {"ok": True, "results": results, "count": data.get("count", 0)}
