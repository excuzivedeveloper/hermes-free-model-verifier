"""Manual, local model checks for Hermes. No background tasks or routing changes.

A model passing a probe at T0 is *not* guaranteed to stay available at T1.
This plugin tests a model's endpoint for the configured account, not its pricing
entitlement. NVIDIA and Gemini require an explicit billing acknowledgement.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

STATE_NAME = "free-model-verifier.json"
TIMEOUT = 25
MAX_RESPONSE_BYTES = 512_000
BASES = {
    "openrouter": "https://openrouter.ai/api/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}
CREDENTIAL_NAMES = {
    "openrouter": ("OPENROUTER_API_KEY",),
    "nvidia": ("NVIDIA_API_KEY",),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
}


class ProbeError(RuntimeError):
    """Sanitized error: no secret, URL, or provider response body is exposed."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def _state_path() -> Path:
    return _home() / STATE_NAME


def _get_secret(provider: str) -> str:
    from agent.secret_scope import get_secret
    for name in CREDENTIAL_NAMES[provider]:
        value = get_secret(name)
        if value and value.strip():
            return value.strip()
    raise ProbeError(f"missing Hermes credential for {provider}")


def _empty_state() -> dict:
    return {"schema": 1, "salt": secrets.token_hex(16), "providers": {}}


def _load_state() -> dict:
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
        if (isinstance(raw, dict) and raw.get("schema") == 1
                and isinstance(raw.get("providers"), dict)
                and isinstance(raw.get("salt"), str) and len(raw["salt"]) >= 32):
            return raw
    except FileNotFoundError:
        return _empty_state()
    except (OSError, ValueError, TypeError):
        raise ProbeError("saved state is unreadable; refusing to overwrite") from None
    raise ProbeError("saved state has an unsupported format; refusing to overwrite")


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".free-model-verifier-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(state, stream, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def _credential_id(provider: str, key: str, salt: str) -> str:
    """Private per-install identity; never write token material to the cache."""
    return hmac.new(bytes.fromhex(salt), (provider + "\0" + key).encode(), hashlib.sha256).hexdigest()[:32]


def _request(url: str, key: str, provider: str, body: dict | None = None) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "Hermes-Free-Model-Verifier/0.1"}
    if provider == "gemini":
        headers["x-goog-api-key"] = key
    else:
        headers["Authorization"] = "Bearer " + key
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(
        url, method="POST" if body is not None else "GET", headers=headers,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
    )
    try:
        with build_opener(NoRedirect()).open(req, timeout=TIMEOUT) as resp:
            payload = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ProbeError("oversized_response")
            result = json.loads(payload)
            if not isinstance(result, dict):
                raise ProbeError("invalid_response")
            return result
    except HTTPError as exc:
        # Never print the response body: it can contain account or request data.
        raise ProbeError(f"http_{exc.code}") from None
    except (URLError, TimeoutError, ConnectionError, OSError):
        raise ProbeError("network_error") from None
    except (UnicodeError, ValueError, TypeError):
        raise ProbeError("invalid_response") from None


def _is_zero(value: object) -> bool:
    try:
        return value is not None and float(value) == 0.0
    except (ValueError, TypeError):
        return False


def _openrouter_candidates(key: str) -> list[str]:
    # Account-keyed catalog honors the user's provider preferences/guardrails.
    raw = _request(BASES["openrouter"] + "/models/user", key, "openrouter")
    models = []
    for obj in raw.get("data", []):
        if not isinstance(obj, dict):
            continue
        mid = obj.get("id")
        pricing = obj.get("pricing") or {}
        is_free = isinstance(pricing, dict) and all(
            _is_zero(pricing.get(k)) for k in ("prompt", "completion")
        )
        if isinstance(mid, str) and (mid.endswith(":free") or is_free):
            models.append(mid)
    return sorted(set(models))


def _nvidia_candidates(key: str) -> list[str]:
    # Catalog does NOT prove zero-price account entitlement.
    raw = _request(BASES["nvidia"] + "/models", key, "nvidia")
    return sorted(set(m["id"] for m in raw.get("data", [])
                      if isinstance(m, dict) and isinstance(m.get("id"), str)))


def _gemini_candidates(key: str) -> list[str]:
    # Catalog does NOT expose project tier and per-model free entitlement.
    raw = _request(BASES["gemini"] + "/models?pageSize=1000", key, "gemini")
    models = []
    for item in raw.get("models", []):
        if not isinstance(item, dict) or "generateContent" not in item.get("supportedGenerationMethods", []):
            continue
        mid = item.get("name", "")
        if isinstance(mid, str) and mid.startswith("models/gemini-"):
            models.append(mid.removeprefix("models/"))
    return sorted(set(models))


def _candidates(provider: str, key: str) -> list[str]:
    return {
        "openrouter": _openrouter_candidates,
        "nvidia": _nvidia_candidates,
        "gemini": _gemini_candidates,
    }[provider](key)


def _tool_schema() -> dict:
    return {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"]}


def _messages_response(provider: str, response: dict) -> tuple[dict, list[dict], str]:
    if provider == "gemini":
        try:
            original = response["candidates"][0]["content"]
            parts = original["parts"]
        except (KeyError, TypeError, IndexError):
            raise ProbeError("invalid_response") from None
        calls = [part["functionCall"] for part in parts
                 if isinstance(part, dict) and isinstance(part.get("functionCall"), dict)]
        text = " ".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))
        return original, calls, text
    try:
        original = response["choices"][0]["message"]
    except (KeyError, TypeError, IndexError):
        raise ProbeError("invalid_response") from None
    if not isinstance(original, dict):
        raise ProbeError("invalid_response")
    calls = original.get("tool_calls") or []
    content = original.get("content") or ""
    return original, calls, str(content)


def _verified_call(provider: str, calls: list[dict]) -> dict:
    for call in calls:
        if not isinstance(call, dict):
            continue
        try:
            if provider == "gemini":
                name, args = call.get("name"), call.get("args")
            else:
                fn = call.get("function") or {}
                name, args = fn.get("name"), json.loads(fn.get("arguments") or "{}")
            if name == "hermes_add" and isinstance(args, dict) and (
                type(args.get("a")) is int and type(args.get("b")) is int
                and args["a"] == 7 and args["b"] == 5
            ):
                return call
        except (ValueError, TypeError):
            continue
    raise ProbeError("no_valid_tool_call")


def _probe_text(provider: str, model: str, key: str) -> str:
    if provider == "gemini":
        url = BASES[provider] + "/models/" + quote(model, safe="") + ":generateContent"
        body = {"contents": [{"role": "user", "parts": [{"text": "Reply with the word OK."}]}],
                "generationConfig": {"maxOutputTokens": 512}}
    else:
        url = BASES[provider] + "/chat/completions"
        body = {"model": model, "messages": [{"role": "user", "content": "Reply with the word OK."}],
                "max_tokens": 512, "stream": False}
    _, _, answer = _messages_response(provider, _request(url, key, provider, body))
    if not answer.strip():
        raise ProbeError("empty_text_response")
    return "text_pass"


def _probe_tools(provider: str, model: str, key: str) -> str:
    prompt = "Call hermes_add with a=7 and b=5. Then tell me the tool result."
    if provider == "gemini":
        url = BASES[provider] + "/models/" + quote(model, safe="") + ":generateContent"
        gs = {"type": "OBJECT", "properties": {"a": {"type": "INTEGER"},
                                                   "b": {"type": "INTEGER"}}, "required": ["a", "b"]}
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"functionDeclarations": [{"name": "hermes_add", "description": "Add integers",
                                                 "parameters": gs}]}],
            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
            "generationConfig": {"maxOutputTokens": 512},
        }
    else:
        url = BASES[provider] + "/chat/completions"
        body = {
            "model": model, "messages": [{"role": "user", "content": prompt}],
            "tools": [{"type": "function", "function": {"name": "hermes_add",
                         "description": "Add integers", "parameters": _tool_schema()}}],
            "tool_choice": "auto", "max_tokens": 512, "stream": False,
        }
    first = _request(url, key, provider, body)
    original, calls, _ = _messages_response(provider, first)
    call = _verified_call(provider, calls)
    if provider == "gemini":
        response = {"name": "hermes_add", "response": {"result": 12}}
        if call.get("id"):
            response["id"] = call["id"]
        # The original content carries thoughtSignature(s) for thinking models.
        body["contents"].extend([original, {"role": "user", "parts": [{"functionResponse": response}]}])
        body["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
    else:
        if not call.get("id"):
            raise ProbeError("missing_tool_call_id")
        body["messages"].extend([original, {"role": "tool", "tool_call_id": call["id"],
                                            "content": json.dumps({"result": 12})}])
        body["tool_choice"] = "none"
    _, _, answer = _messages_response(provider, _request(url, key, provider, body))
    import re
    if not re.search(r"(?<!\d)12(?!\d)", answer):
        raise ProbeError("incorrect_tool_result_followup")
    return "full_pass"


def run_scan(provider: str, requested: list[str], mode: str, limit: int, ack_unpriced: bool) -> None:
    if limit < 0:
        raise ProbeError("--limit must not be negative")
    if provider in ("nvidia", "gemini") and not ack_unpriced:
        raise ProbeError(
            f"{provider} catalogs do not prove zero-cost access; add --ack-unpriced only after "
            "confirming your account/project tier and the selected model's pricing"
        )
    key = _get_secret(provider)
    state = _load_state()
    fingerprint = _credential_id(provider, key, state["salt"])
    if requested:
        models = sorted(set(requested))
        if provider == "openrouter" and any(not mid.endswith(":free") for mid in models):
            raise ProbeError("explicit OpenRouter IDs must end with :free; use discovery for priced metadata")
    else:
        models = _candidates(provider, key)
    if limit:
        models = models[:limit]
    if not models:
        print(f"{provider}: no candidates")
        return
    print(f"Manual {mode} check of {len(models)} {provider} model(s). Real API quota is consumed.", flush=True)
    provider_state = state["providers"].get(provider)
    if not isinstance(provider_state, dict) or provider_state.get("identity") != fingerprint:
        provider_state = {"identity": fingerprint, "models": {}}
        state["providers"][provider] = provider_state
    for model in models:
        try:
            status = (_probe_tools if mode == "tools" else _probe_text)(provider, model, key)
        except ProbeError as exc:
            status = str(exc)
        provider_state["models"][model] = {"mode": mode, "status": status,
                                           "checked_at": int(time.time())}
        _save_state(state)
        print(f"{provider}/{model}: {status}", flush=True)
        if status == "http_429":
            print("Stopped on provider rate limit; manual retry later is optional.")
            break
    print("Finished. No Hermes models, schedules, or configuration were changed.")


def _display_state(state: dict, *, current_account_only: bool = True) -> list[dict]:
    rows = []
    for provider, record in sorted(state["providers"].items()):
        if not isinstance(record, dict):
            continue
        if current_account_only:
            try:
                key = _get_secret(provider)
                if _credential_id(provider, key, state["salt"]) != record.get("identity"):
                    continue
            except ProbeError:
                continue
        for model, item in sorted(record.get("models", {}).items()):
            if isinstance(item, dict):
                rows.append({"provider": provider, "model": model,
                             "status": item.get("status", "unknown"),
                             "mode": item.get("mode", "unknown"),
                             "checked_at": item.get("checked_at")})
    return rows


def run_list(as_json: bool = False) -> None:
    rows = _display_state(_load_state())
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("No saved checks. Run: hermes freeverify scan --provider openrouter")
    for item in rows:
        print(f"{item['provider']}/{item['model']} [{item['mode']}] => {item['status']}")
    print("Saved receipts are historical, not a guarantee of current availability.")


def chat_status(args: str) -> str:
    if args.strip():
        return "Usage: /freeverify (saved status only). To probe manually: hermes freeverify scan --provider openrouter"
    rows = _display_state(_load_state())
    if not rows:
        return "Free-model checks: none saved. Run `hermes freeverify scan --provider openrouter` in a terminal."
    passed = [row for row in rows if row["status"] in ("full_pass", "text_pass")]
    out = [f"Free-model verifier: {len(passed)} passed / {len(rows)} checked (historical results)."]
    out.extend(f"• {row['provider']}/{row['model']} [{row['mode']}]" for row in passed[:20])
    if len(passed) > 20:
        out.append(f"... and {len(passed) - 20} more")
    out.append("Manual rescan: hermes freeverify scan --provider <provider>. No automatic switching.")
    return "\n".join(out)
