"""Full connectivity check for all services in .env"""
import os, sys, asyncio, time, requests
from dotenv import load_dotenv
load_dotenv(".env")

OK  = "[OK]"
ERR = "[FAIL]"

results = []

def check(name, fn):
    try:
        msg = fn()
        results.append(f"  {OK}  {name}: {msg}")
    except Exception as e:
        results.append(f"  {ERR} {name}: {e}")

# ── 1. LangWatch ──────────────────────────────────────────────────────────────
def check_langwatch():
    import langwatch
    langwatch.setup(
        api_key=os.getenv("LANGWATCH_API_KEY"),
        endpoint_url=os.getenv("LANGWATCH_ENDPOINT", "https://app.langwatch.ai"),
    )
    # Verify connectivity by sending a test trace
    @langwatch.trace(name="connectivity-check")
    def _ping():
        langwatch.get_current_trace().update(metadata={"test": True})
        return "trace sent"
    result = _ping()
    return f"Connected to {os.getenv('LANGWATCH_ENDPOINT')} | {result}"

check("LangWatch", check_langwatch)

# ── 2. OpenRouter / LLM ───────────────────────────────────────────────────────
def check_llm():
    import openai as openai_sdk, httpx
    client = openai_sdk.OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url="https://openrouter.ai/api/v1",
        timeout=httpx.Timeout(15.0),
    )
    t0 = time.time()
    resp = client.chat.completions.create(
        model=os.getenv("LLM_CHOICE", "openai/gpt-4o-mini"),
        messages=[{"role": "user", "content": "Say hi in one word."}],
        max_tokens=5,
    )
    text = resp.choices[0].message.content.strip()
    return f"model={os.getenv('LLM_CHOICE')} | reply={text!r} | {time.time()-t0:.1f}s"

check("LLM (OpenRouter)", check_llm)

# ── 3. Sarvam STT ─────────────────────────────────────────────────────────────
def check_sarvam_stt():
    r = requests.get(
        "https://api.sarvam.ai/",
        headers={"api-subscription-key": os.getenv("SARVAM_API_KEY", "")},
        timeout=10
    )
    return f"reachable (HTTP {r.status_code})"

check("Sarvam API", check_sarvam_stt)

# ── 4. Deepgram ───────────────────────────────────────────────────────────────
def check_deepgram():
    r = requests.get(
        "https://api.deepgram.com/v1/projects",
        headers={"Authorization": f"Token {os.getenv('DEEPGRAM_API_KEY', '')}"},
        timeout=10
    )
    if r.ok:
        projects = r.json().get("projects", [])
        return f"authenticated | {len(projects)} project(s)"
    return f"HTTP {r.status_code} - {r.text[:80]}"

check("Deepgram", check_deepgram)

# ── 5. Print summary ──────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("  SERVICE CONNECTIVITY REPORT")
print("=" * 60)
for r in results:
    print(r)
print("=" * 60)
print(f"\nDone. {sum(1 for r in results if OK in r)}/{len(results)} services OK\n")
