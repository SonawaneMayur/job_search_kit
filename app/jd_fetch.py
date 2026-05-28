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


# ---------- Adzuna ----------

REMOTE_RE = re.compile(r"\b(remote|fully\s*remote|work\s*from\s*home|wfh|telecommute|distributed)\b", re.I)

CURRENCY_BY_COUNTRY = {
    "us": "$", "gb": "£", "ca": "C$", "au": "A$",
    "de": "€", "fr": "€", "nl": "€", "in": "₹",
}


def adzuna_search(
    app_id: str,
    app_key: str,
    what: str,
    country: str = "us",
    where: str = "",
    distance_km: int | None = None,
    salary_min: int | None = None,
    sort_by: str = "relevance",
    remote_only: bool = False,
    h1b_only: bool = False,
    page: int = 1,
) -> dict:
    """Adzuna jobs search.
    country: 2-letter country path segment (us, gb, ca, ...).
    where:   free-form location WITHIN the country (e.g. 'Boston', '02115', 'Texas').
    remote_only: appends 'remote' to `what`, then post-filters results for remote keywords.
    h1b_only: post-filters to known-sponsor companies (curated list — not exhaustive).
    """
    from .data.h1b_sponsors import is_h1b_sponsor

    if not app_id or not app_key:
        return {"ok": False, "error": "Adzuna credentials missing. Add them in Settings."}

    keyword_query = (what or "").strip()
    if remote_only and "remote" not in keyword_query.lower():
        keyword_query = (keyword_query + " remote").strip()

    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "what": keyword_query,
        "results_per_page": 50,
        "content-type": "application/json",
    }
    if where:
        params["where"] = where
    if distance_km:
        params["distance"] = distance_km
    if salary_min:
        params["salary_min"] = salary_min
    if sort_by in ("date", "salary", "relevance"):
        params["sort_by"] = sort_by

    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"Adzuna error: {e}"}

    currency = CURRENCY_BY_COUNTRY.get(country.lower(), "")
    raw_results = data.get("results", [])
    results = []
    for item in raw_results:
        company = (item.get("company") or {}).get("display_name", "")
        location = (item.get("location") or {}).get("display_name", "")
        title = item.get("title", "")
        desc = item.get("description", "")
        text_pool = f"{title}\n{location}\n{desc}"
        is_remote = bool(REMOTE_RE.search(text_pool))

        smin = item.get("salary_min")
        smax = item.get("salary_max")
        predicted = bool(item.get("salary_is_predicted") == "1" or item.get("salary_is_predicted") is True)
        salary_str = _format_salary(smin, smax, currency, predicted)

        h1b_match = is_h1b_sponsor(company)

        if remote_only and not is_remote:
            continue
        if h1b_only and not h1b_match:
            continue

        results.append({
            "title": title,
            "company": company,
            "location": location,
            "url": item.get("redirect_url", ""),
            "description": desc,
            "created": item.get("created", ""),
            "salary": salary_str,
            "salary_min": smin,
            "salary_max": smax,
            "salary_predicted": predicted,
            "is_remote": is_remote,
            "h1b_likely": h1b_match,
            "contract_type": item.get("contract_type", ""),
            "contract_time": item.get("contract_time", ""),
        })
    if sort_by == "salary":
        # Defense-in-depth: even though Adzuna sorts desc, enforce locally so
        # missing-salary rows fall to the bottom and we always show big numbers first.
        def _key(r):
            s = r.get("salary_max") or r.get("salary_min") or 0
            try:
                return -float(s)
            except (TypeError, ValueError):
                return 0
        results.sort(key=_key)

    return {
        "ok": True,
        "results": results,
        "count": data.get("count", 0),
        "shown": len(results),
        "raw_count": len(raw_results),
        "applied_filters": {
            "remote_only": remote_only,
            "h1b_only": h1b_only,
            "salary_min": salary_min,
            "sort_by": sort_by,
        },
    }


def _format_salary(smin, smax, currency: str, predicted: bool) -> str:
    if not smin and not smax:
        return ""
    def fmt(n):
        try:
            n = int(round(float(n)))
            if n >= 1000:
                return f"{currency}{n // 1000}k"
            return f"{currency}{n}"
        except (ValueError, TypeError):
            return ""
    if smin and smax and smin != smax:
        s = f"{fmt(smin)} – {fmt(smax)}"
    else:
        s = fmt(smin or smax)
    if predicted:
        s += " (est)"
    return s
