"""Small helper for structured (JSON-schema constrained) chats with local / Ollama-cloud models.

Models only ever return data that is validated afterwards; nothing they write is executed.
"""
import json
import os

import requests

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")


class LLMError(RuntimeError):
    pass


def installed_models():
    try:
        return {m["name"] for m in requests.get(f"{OLLAMA_URL}/api/tags", timeout=5).json().get("models", [])}
    except (requests.RequestException, ValueError):
        return set()


def chat_json(model, system, user, schema, think=False, on_thinking=None, temperature=0.2,
              timeout_s=300, keep_alive=None, seed=None):
    """Stream a chat whose answer is constrained to `schema`; returns (parsed dict, usage).

    think=True only for models that support a reasoning phase (e.g. qwen3); its text is passed
    to on_thinking as it arrives. keep_alive=0 unloads the model afterwards, so the next model
    gets the whole (small) laptop GPU."""
    body = {"model": model, "stream": True, "format": schema,
            "options": {"temperature": temperature, "num_ctx": 8192, **({"seed": seed} if seed is not None else {})},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if think or model.startswith("qwen3"):   # qwen3 reasons by default unless told not to
        body["think"] = bool(think)
    if keep_alive is not None:
        body["keep_alive"] = keep_alive
    content, last = [], {}
    try:
        with requests.post(f"{OLLAMA_URL}/api/chat", json=body, stream=True, timeout=(10, timeout_s)) as r:
            if r.status_code != 200:
                raise LLMError(f"{model}: Ollama error {r.status_code}: {r.text[:200]}")
            for line in r.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise LLMError(f"{model}: {chunk['error']}")
                msg = chunk.get("message", {})
                if msg.get("thinking") and on_thinking:
                    on_thinking(msg["thinking"])
                if msg.get("content"):
                    content.append(msg["content"])
                    if on_thinking and not think:
                        on_thinking(msg["content"])   # non-reasoning models: show the answer forming
                if chunk.get("done"):
                    last = chunk
    except requests.RequestException as e:
        raise LLMError(f"cannot reach Ollama at {OLLAMA_URL}: {e}") from e
    try:
        data = json.loads("".join(content))
    except ValueError as e:
        raise LLMError(f"{model} returned invalid JSON: {''.join(content)[:200]}") from e
    usage = {k: last.get(k) for k in ("prompt_eval_count", "eval_count", "total_duration")}
    return data, usage
