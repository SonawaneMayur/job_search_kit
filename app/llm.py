"""LLM dispatcher. Supports:
  - ollama    (local, default, no data leaves the machine)
  - anthropic (Claude — cloud; requires anthropic_api_key in profile)
  - openai    (GPT — cloud; requires openai_api_key in profile)

JSON-mode is enforced for all backends (Ollama format=json, OpenAI response_format,
Anthropic via strong prompt + assistant prefill).
"""
import json
import re
import httpx

from . import prompts


class LLMError(RuntimeError):
    pass


PROVIDERS = ("ollama", "anthropic", "openai")

ANTHROPIC_MODELS = ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
OPENAI_MODELS = ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"]


# ---------- helpers ----------

def _decide_visa_mode(profile: dict, sponsorship_posture: str) -> str:
    if sponsorship_posture in ("NEEDS_VERIFICATION", "TRANSFER_LIKELY_OK"):
        return "include"
    return "omit"


def _profile_vars(p: dict) -> dict:
    return {
        "USER_NAME": p.get("user_name") or "",
        "CURRENT_VISA": p.get("current_visa") or "",
        "GC_STAGE": p.get("gc_stage") or "",
        "PRIORITY_DATE": p.get("priority_date") or "",
        "AC21_ELIGIBLE": p.get("ac21_eligible") or "",
        "EAD": p.get("ead") or "",
        "TARGET_ROLES": p.get("target_roles") or "",
        "MASTER_RESUME": p.get("master_resume") or "",
    }


def _parse_json_response(raw: str) -> dict:
    if not raw:
        raise LLMError("Empty response from LLM")
    raw = raw.strip()
    # strip code fences if any
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```\s*$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # try to extract the largest {...} block
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        snippet = raw[:400]
        raise LLMError(f"Model did not return valid JSON. First 400 chars:\n{snippet}")


# ---------- Ollama ----------

def _call_ollama(profile: dict, prompt: str, model_override: str | None = None) -> dict:
    url = (profile.get("ollama_url") or "http://localhost:11434").rstrip("/")
    model = model_override or profile.get("ollama_model") or "llama3.1:8b"
    payload = {
        "model": model,
        "prompt": prompt,
        "format": "json",
        "stream": True,
        # num_predict=-1 lets the model run to natural completion instead of
        # capping at Ollama's default (often 128/256 tokens), which was
        # truncating long resumes.
        "options": {"temperature": 0.2, "num_predict": -1, "num_ctx": 16384},
    }
    timeout = httpx.Timeout(connect=15.0, read=None, write=60.0, pool=15.0)
    chunks: list[str] = []
    try:
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", f"{url}/api/generate", json=payload) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "error" in msg:
                        raise LLMError(f"Ollama error: {msg['error']}")
                    if "response" in msg:
                        chunks.append(msg["response"])
                    if msg.get("done"):
                        break
    except httpx.HTTPError as e:
        raise LLMError(f"Ollama call failed: {e}") from e

    return _parse_json_response("".join(chunks))


def list_local_models(ollama_url: str) -> list[str]:
    url = (ollama_url or "http://localhost:11434").rstrip("/")
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{url}/api/tags")
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
    except httpx.HTTPError:
        return []


# ---------- Anthropic (Claude) ----------

def _call_anthropic(profile: dict, prompt: str, model_override: str | None = None) -> dict:
    api_key = profile.get("anthropic_api_key") or ""
    if not api_key:
        raise LLMError(
            "Anthropic API key missing. Add it in Profile to use Claude. "
            "Note: selecting Claude sends the JD + master resume to Anthropic's API."
        )
    model = model_override or profile.get("anthropic_model") or "claude-opus-4-7"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": model,
        # 16k is enough for a 3-page resume + cover letter + 2 outreach drafts.
        # Claude Opus / Sonnet 4.x both support this output length.
        "max_tokens": 16384,
        "temperature": 0.2,
        "messages": [
            {"role": "user", "content": prompt},
            # Prefill the assistant's response so it MUST start with '{'.
            # We re-prepend '{' to the returned text before parsing.
            {"role": "assistant", "content": "{"},
        ],
    }
    timeout = httpx.Timeout(connect=15.0, read=None, write=60.0, pool=15.0)
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        # surface the response body if available — Anthropic returns useful error JSON
        msg = str(e)
        if isinstance(e, httpx.HTTPStatusError):
            try:
                msg = f"{e.response.status_code}: {e.response.text}"
            except Exception:
                pass
        raise LLMError(f"Anthropic call failed: {msg}") from e

    parts = data.get("content") or []
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return _parse_json_response("{" + text)


# ---------- OpenAI ----------

def _call_openai(profile: dict, prompt: str, model_override: str | None = None) -> dict:
    api_key = profile.get("openai_api_key") or ""
    if not api_key:
        raise LLMError(
            "OpenAI API key missing. Add it in Profile to use OpenAI. "
            "Note: selecting OpenAI sends the JD + master resume to OpenAI's API."
        )
    model = model_override or profile.get("openai_model") or "gpt-4o"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "max_tokens": 16384,
        "messages": [{"role": "user", "content": prompt}],
    }
    timeout = httpx.Timeout(connect=15.0, read=None, write=60.0, pool=15.0)
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers, json=body,
            )
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        msg = str(e)
        if isinstance(e, httpx.HTTPStatusError):
            try:
                msg = f"{e.response.status_code}: {e.response.text}"
            except Exception:
                pass
        raise LLMError(f"OpenAI call failed: {msg}") from e

    choices = data.get("choices") or []
    if not choices:
        raise LLMError("OpenAI returned no choices")
    text = choices[0].get("message", {}).get("content", "")
    return _parse_json_response(text)


# ---------- dispatcher ----------

def _call(profile: dict, prompt: str, provider: str, model: str | None) -> dict:
    provider = (provider or "ollama").lower()
    if provider == "ollama":
        return _call_ollama(profile, prompt, model)
    if provider == "anthropic":
        return _call_anthropic(profile, prompt, model)
    if provider == "openai":
        return _call_openai(profile, prompt, model)
    raise LLMError(f"Unknown provider: {provider!r}. Use one of: {PROVIDERS}")


# ---------- public API ----------

def screen_jd(profile: dict, jd_text: str, provider: str = "ollama", model: str | None = None) -> dict:
    prompt = prompts.render(
        prompts.SCREEN_PROMPT,
        JD_TEXT=jd_text,
        **_profile_vars(profile),
    )
    return _call(profile, prompt, provider, model)


def generate_assets(
    profile: dict,
    jd_text: str,
    screening: dict,
    provider: str = "ollama",
    model: str | None = None,
) -> dict:
    sponsorship_posture = (
        screening.get("sponsorship", {}).get("posture", "NEUTRAL")
        if isinstance(screening, dict) else "NEUTRAL"
    )
    visa_mode = _decide_visa_mode(profile, sponsorship_posture)
    prompt = prompts.render(
        prompts.ASSETS_PROMPT,
        JD_TEXT=jd_text,
        SCREENING_JSON=json.dumps(screening, indent=2),
        VISA_MODE=visa_mode,
        **_profile_vars(profile),
    )
    return _call(profile, prompt, provider, model)


def provider_options(profile: dict) -> list[dict]:
    """Return the options to show in the UI selector, in order. Each item:
    { id, label, model, available, note }"""
    opts = []
    local_model = profile.get("ollama_model") or "llama3.1:8b"
    opts.append({
        "id": "ollama",
        "label": f"Local Ollama ({local_model})",
        "model": local_model,
        "available": True,
        "note": "Private — nothing leaves your machine.",
    })
    a_model = profile.get("anthropic_model") or "claude-opus-4-7"
    opts.append({
        "id": "anthropic",
        "label": f"Anthropic Claude ({a_model})",
        "model": a_model,
        "available": bool(profile.get("anthropic_api_key")),
        "note": "Cloud — sends JD + master resume to Anthropic's API.",
    })
    o_model = profile.get("openai_model") or "gpt-4o"
    opts.append({
        "id": "openai",
        "label": f"OpenAI ({o_model})",
        "model": o_model,
        "available": bool(profile.get("openai_api_key")),
        "note": "Cloud — sends JD + master resume to OpenAI's API.",
    })
    return opts
