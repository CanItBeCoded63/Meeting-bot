"""Analyze LangWatch agent performance using the REST API."""
import os, json, requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv(".env")

api_key = os.getenv("LANGWATCH_API_KEY", "").strip('"')
project_id = os.getenv("LANGWATCH_PROJECT_ID", "").strip('"')
base_url = os.getenv("LANGWATCH_ENDPOINT", "https://app.langwatch.ai").strip('"')

# Correct auth header for project-scoped resources
headers = {
    "X-Auth-Token": api_key,
    "Content-Type": "application/json"
}

now = datetime.now(timezone.utc)
start_7d = int((now - timedelta(days=7)).timestamp() * 1000)
end_now  = int(now.timestamp() * 1000)

print("=" * 60)
print("  LANGWATCH AGENT PERFORMANCE ANALYSIS")
print("=" * 60)
print(f"Project: {project_id}")
print(f"Period : Last 7 days ({(now-timedelta(days=7)).strftime('%Y-%m-%d')} to {now.strftime('%Y-%m-%d')})")
print()

# ── 1. Search traces ──────────────────────────────────────────
print("[1] Recent Traces")
print("-" * 60)
payload = {
    "startDate": start_7d,
    "endDate": end_now,
    "pageSize": 20,
    "sortBy": "createdAt",
    "sortDirection": "desc"
}
r = requests.post(f"{base_url}/api/traces/search", headers=headers, json=payload, timeout=20)
print(f"HTTP {r.status_code}")
if r.ok:
    data = r.json()
    traces = data.get("traces", data.get("data", []))
    print(f"Total traces found: {data.get('total', len(traces))}")
    for t in traces[:5]:
        ts = t.get("startedAt") or t.get("createdAt") or t.get("timestamp", "")
        duration_ms = t.get("totalTimeMs") or t.get("durationMs", "?")
        tokens = t.get("totalTokens") or t.get("tokens", "?")
        cost = t.get("totalCost") or t.get("cost", "?")
        model = t.get("model") or (t.get("spans") or [{}])[0].get("model", "?")
        error = t.get("error") or (t.get("spans") or [{}])[0].get("error")
        print(f"  • ID: {t.get('id','?')[:20]}... | Duration: {duration_ms}ms | Tokens: {tokens} | Cost: ${cost} | Error: {bool(error)}")
else:
    print(f"Response: {r.text[:500]}")

print()

# ── 2. Analytics timeseries ───────────────────────────────────
print("[2] Analytics Timeseries (Last 7 Days)")
print("-" * 60)

metrics_to_query = [
    {"metric": "trace_count",     "aggregation": "count"},
    {"metric": "tokens",          "aggregation": "sum"},
    {"metric": "cost",            "aggregation": "sum"},
    {"metric": "latency",         "aggregation": "median"},
]

for m in metrics_to_query:
    payload = {
        "startDate": start_7d,
        "endDate": end_now,
        "series": [m]
    }
    r = requests.post(f"{base_url}/api/analytics/timeseries", headers=headers, json=payload, timeout=20)
    if r.ok:
        data = r.json()
        print(f"  {m['metric']} ({m['aggregation']}): {json.dumps(data, default=str)[:200]}")
    else:
        print(f"  {m['metric']}: HTTP {r.status_code} - {r.text[:100]}")

print()

# ── 3. Discover metrics ───────────────────────────────────────
print("[3] Schema / Available Metrics Discovery")
print("-" * 60)
r = requests.get(f"{base_url}/api/analytics/metrics", headers=headers, timeout=15)
print(f"HTTP {r.status_code}: {r.text[:500]}")
