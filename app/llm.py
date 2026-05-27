"""Ollama JSON-mode client. Single dependency: httpx."""
import json
import httpx
from . import prompts


class LLMError(RuntimeError):
    pass


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


def _call_ollama(profile: dict, prompt: str) -> dict:
    """Stream from Ollama so a slow model doesn't trip an idle-read timeout.
    Accumulates the `response` field across chunks until `done: true`."""
    url = (profile.get("ollama_url") or "http://localhost:11434").rstrip("/")
    model = profile.get("ollama_model") or "llama3.1:8b"
    payload = {
        "model": model,
        "prompt": prompt,
        "format": "json",
        "stream": True,
        "options": {"temperature": 0.2},
    }
    # connect quickly, but allow arbitrarily long generation
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

    raw = "".join(chunks)
    if not raw:
        raise LLMError("Empty response from Ollama")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        snippet = raw[:400]
        raise LLMError(f"Model did not return valid JSON. First 400 chars:\n{snippet}") from e


def screen_jd(profile: dict, jd_text: str) -> dict:
    prompt = prompts.render(
        prompts.SCREEN_PROMPT,
        JD_TEXT=jd_text,
        **_profile_vars(profile),
    )
    return _call_ollama(profile, prompt)


def generate_assets(profile: dict, jd_text: str, screening: dict) -> dict:
    sponsorship_posture = (
        screening.get("sponsorship", {}).get("posture", "NEUTRAL")
        if isinstance(screening, dict)
        else "NEUTRAL"
    )
    visa_mode = _decide_visa_mode(profile, sponsorship_posture)
    prompt = prompts.render(
        prompts.ASSETS_PROMPT,
        JD_TEXT=jd_text,
        SCREENING_JSON=json.dumps(screening, indent=2),
        VISA_MODE=visa_mode,
        **_profile_vars(profile),
    )
    return _call_ollama(profile, prompt)


def list_local_models(ollama_url: str) -> list[str]:
    url = (ollama_url or "http://localhost:11434").rstrip("/")
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{url}/api/tags")
            r.raise_for_status()
            data = r.json()
            return [m["name"] for m in data.get("models", [])]
    except httpx.HTTPError:
        return []
