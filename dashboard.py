"""
Recruitment-stack control panel (advanced).

Run:
    pip install streamlit requests pandas
    streamlit run dashboard.py

Optional but strongly recommended (PowerShell):
    $env:GITHUB_TOKEN="ghp_xxx"     # 60 -> 5000 API calls/hour

Tabs
  Overview   health score, activity, licence risk, local clone status per repo
  Coverage   which business layer has a repo, by phase (MVP / growth / scale)
  Releases   alerts when a tracked project publishes a new release
  Licences   what each licence means for hosted / white-label client work
  Sync       parallel shallow clone / pull into ./vendor
  Stack      generate a docker-compose.yml for the self-hostable pieces
  Manage     add repos (validated), edit the list, merge new defaults, export CSV
  Readiness  per-repo adoption status (not started / evaluating / running / dropped), phase gates
  Governance legal and policy checklist, tagged by the milestone it must be done before
  Live       health check of your local services (HTTP and TCP)
  Funnel     business milestones plus weekly funnel log: candidates -> placements, revenue
  Discover   search GitHub topics (ATS, recruitment, job boards, resume parsers) and add repos
State for Readiness/Governance/Live/Funnel lives in state.json next to this file.
"""
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

ROOT = Path(__file__).parent
VENDOR = ROOT / "vendor"
CONFIG = ROOT / "repos.json"
CACHE = ROOT / ".cache_meta.json"
SEEN = ROOT / ".seen_releases.json"
CACHE_TTL_H = 12

PHASES = {1: "1 MVP (now)", 2: "2 Growth (after first client)", 3: "3 Scale (when demand proves it)"}
MODES = ["service", "tool", "clone", "reference"]


def R(name, repo, layer, phase, mode, note=""):
    return {"name": name, "repo": repo, "layer": layer, "phase": phase, "mode": mode, "note": note}


# Owner/name pairs I am confident about. Blank repo = fill in yourself (Manage tab).
DEFAULTS = [
    # Core platform
    R("Supabase", "supabase/supabase", "Core platform", 1, "service", "Auth, Postgres, storage"),
    R("n8n", "n8n-io/n8n", "Core platform", 1, "service", "Workflow glue"),
    # Recruiter dashboard / analytics
    R("Streamlit", "streamlit/streamlit", "Recruiter dashboard", 1, "tool"),
    R("Metabase", "metabase/metabase", "Analytics & reporting", 2, "service", "BI over Supabase"),
    R("PostHog", "PostHog/posthog", "Analytics & reporting", 3, "service", "Product analytics"),
    # Job feeds
    R("Jobicy / ATS feeds", "", "Job feeds", 1, "reference", "API/feeds, not a repo; set if you have one"),
    # Candidate intake & parsing
    R("Resume-Matcher", "srbhr/Resume-Matcher", "Candidate intake & parsing", 1, "clone"),
    R("OpenResume", "xitanggg/open-resume", "Candidate intake & parsing", 2, "reference", "Resume builder/parser"),
    R("Docling", "docling-project/docling", "Candidate intake & parsing", 2, "tool", "PDF/DOCX to structured text"),
    R("Unstructured", "Unstructured-IO/unstructured", "Candidate intake & parsing", 2, "tool"),
    R("spaCy", "explosion/spaCy", "Candidate intake & parsing", 2, "tool", "Skill/entity extraction"),
    # AI matching
    R("Ollama", "ollama/ollama", "AI matching", 1, "service", "Local LLM"),
    R("sentence-transformers", "UKPLab/sentence-transformers", "AI matching", 2, "tool", "Embeddings"),
    R("pgvector", "pgvector/pgvector", "AI matching", 2, "service", "Vectors inside Postgres"),
    R("Qdrant", "qdrant/qdrant", "AI matching", 3, "service", "Only if pgvector is not enough"),
    R("LlamaIndex", "run-llama/llama_index", "AI matching", 3, "tool"),
    # LLM gateway (one OpenAI-compatible endpoint in front of many providers)
    R("OmniRoute", "diegosouzapw/OmniRoute", "LLM gateway", 2, "service",
      "Pools free tiers; NON-candidate data only. Check licence + provider ToS"),
    R("FreeLLMAPI", "platoba/freellmapi", "LLM gateway", 2, "service",
      "Verify owner/name: several repos use this name. NON-candidate data only"),
    R("LiteLLM", "BerriAI/litellm", "LLM gateway", 2, "service", "Mature gateway with budgets and logging"),
    # AI evaluation
    R("Promptfoo", "promptfoo/promptfoo", "AI evaluation & fairness", 2, "tool", "Test prompts on sample CVs"),
    R("Langfuse", "langfuse/langfuse", "AI evaluation & fairness", 2, "service", "Trace prompts, cost, feedback"),
    R("Fairlearn", "fairlearn/fairlearn", "AI evaluation & fairness", 3, "tool", "Bias metrics"),
    # Employer CRM / ATS
    R("Twenty", "twentyhq/twenty", "Employer CRM", 2, "service", "Open-source CRM"),
    R("OpenCATS", "opencats/OpenCATS", "Employer CRM", 3, "reference", "Classic open ATS"),
    R("FreeATS", "", "Employer CRM", 3, "reference", "Owner unconfirmed; autorun will suggest the repo"),
    R("JobSpy", "", "Job feeds", 1, "tool", "Job-board scraper; owner unconfirmed, autorun will suggest the repo"),
    R("Reqcore", "reqcore-inc/reqcore-app", "Employer CRM", 3, "reference", "Newer open-source ATS; verify maturity"),
    R("SpotAxis", "Assystant/SpotAxis", "Employer CRM", 3, "reference", "Django ATS / job portal; check activity"),
    # Scheduling
    R("Cal.com", "calcom/cal.com", "Interview scheduling", 1, "service"),
    # Communication
    R("Chatwoot", "chatwoot/chatwoot", "Communication inbox", 2, "service", "Shared inbox, WhatsApp/email"),
    R("Listmonk", "knadh/listmonk", "Communication inbox", 2, "service", "Job-alert newsletters"),
    R("Mautic", "mautic/mautic", "Communication inbox", 3, "service", "Marketing automation"),
    # Agreements
    R("Documenso", "documenso/documenso", "Agreements & e-signatures", 1, "service"),
    # Invoicing
    R("Invoice Ninja", "invoiceninja/invoiceninja", "Invoicing & payments", 1, "service"),
    # Market research / scraping
    R("Scrapy", "scrapy/scrapy", "Market research", 2, "tool"),
    R("Playwright (Python)", "microsoft/playwright-python", "Market research", 2, "tool"),
    R("Crawlee (Python)", "apify/crawlee-python", "Market research", 3, "tool"),
    # Security, backup, monitoring
    R("Uptime Kuma", "louislam/uptime-kuma", "Security & monitoring", 2, "service"),
    R("restic", "restic/restic", "Security & monitoring", 2, "tool", "Encrypted backups"),
    R("Grafana", "grafana/grafana", "Security & monitoring", 3, "service"),
    R("Sentry", "getsentry/sentry", "Security & monitoring", 3, "service"),
    R("Keycloak", "keycloak/keycloak", "Security & monitoring", 3, "service", "SSO for client portal"),
    # Compliance / PII
    R("Presidio", "microsoft/presidio", "Compliance & privacy", 2, "tool", "PII redaction before sending CVs to AI"),
    # Personal and dev security
    R("KeePassXC", "keepassxreboot/keepassxc", "Personal & dev security", 1, "tool", "Offline password manager"),
    R("Bitwarden clients", "bitwarden/clients", "Personal & dev security", 1, "tool", "Password manager apps"),
    R("Aegis Authenticator", "beemdevelopment/Aegis", "Personal & dev security", 1, "tool", "Android 2FA app"),
    R("Ente (Auth)", "ente-io/ente", "Personal & dev security", 2, "tool", "Encrypted 2FA codes with backup"),
    R("AdGuard Home", "AdguardTeam/AdGuardHome", "Personal & dev security", 2, "service", "Network-wide DNS filtering"),
    R("Pi-hole", "pi-hole/pi-hole", "Personal & dev security", 2, "service", "DNS filtering alternative"),
    R("uBlock Origin", "gorhill/uBlock", "Personal & dev security", 1, "tool", "Browser protection"),
    R("VeraCrypt", "veracrypt/VeraCrypt", "Personal & dev security", 2, "tool", "Disk/file encryption"),
    R("Cryptomator", "cryptomator/cryptomator", "Personal & dev security", 2, "tool", "Encrypted cloud folders"),
    R("Gitleaks", "gitleaks/gitleaks", "Personal & dev security", 1, "tool", "Stop secrets leaking into git"),
    R("TruffleHog", "trufflesecurity/trufflehog", "Personal & dev security", 2, "tool", "Secret scanning"),
    R("Wazuh", "wazuh/wazuh", "Personal & dev security", 3, "service", "Endpoint monitoring"),
    # Orchestration / data quality
    R("Prefect", "PrefectHQ/prefect", "Pipelines & data quality", 2, "tool"),
    R("Great Expectations", "great-expectations/great_expectations", "Pipelines & data quality", 2, "tool"),
    R("RapidFuzz", "rapidfuzz/RapidFuzz", "Pipelines & data quality", 2, "tool", "Duplicate candidate matching"),
    # Mobile
    R("Expo", "expo/expo", "Mobile candidate app", 3, "reference"),
    R("react-native-jobs", "", "Mobile candidate app", 3, "reference", "Fill owner/name"),
    # Multilingual
    R("Weblate", "WeblateOrg/weblate", "Multilingual", 3, "service"),
    R("awesome-translations", "", "Multilingual", 3, "reference", "Fill owner/name"),
    # Marketplace
    R("Medusa", "medusajs/medusa", "Marketplace / white-label", 3, "service"),
]

# Layers from the "final completeness checklist"
LAYERS = [
    "Job feeds", "Candidate intake & parsing", "AI matching", "Recruiter dashboard", "Employer CRM",
    "Interview scheduling", "Communication inbox", "Agreements & e-signatures", "Invoicing & payments",
    "Analytics & reporting", "Market research", "Security & monitoring", "Mobile candidate app",
    "Multilingual", "AI evaluation & fairness", "Compliance & privacy", "Pipelines & data quality",
    "Marketplace / white-label", "Core platform", "LLM gateway", "Personal & dev security",
]

# Licences that need a human read before client / white-label use. Not legal advice.
LICENSE_NOTES = {
    "n8n-io/n8n": "Sustainable Use License (source-available): restricts offering it as a service to others. Verify.",
    "calcom/cal.com": "AGPL core, some features under a commercial licence. Verify.",
    "invoiceninja/invoiceninja": "Not plain open source (Elastic-style licence). Verify before hosting for clients.",
    "getsentry/sentry": "Functional Source License: converts to open source later; limits competing use. Verify.",
}
PERMISSIVE = {"MIT", "APACHE-2.0", "BSD-2-CLAUSE", "BSD-3-CLAUSE", "ISC", "UNLICENSE", "0BSD", "CC0-1.0"}

# Self-hostable pieces simple enough for one compose file. Host ports are shifted
# so they do not collide with Postgres/Ollama you may already run locally.
COMPOSE = {
    "pgvector/pgvector": ("""  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-change_me}
      POSTGRES_DB: recruit
    ports: ["5433:5432"]
    volumes: ["pgdata:/var/lib/postgresql/data"]
""", ["pgdata"]),
    "n8n-io/n8n": ("""  n8n:
    image: n8nio/n8n
    ports: ["5678:5678"]
    volumes: ["n8n_data:/home/node/.n8n"]
""", ["n8n_data"]),
    "ollama/ollama": ("""  ollama:
    image: ollama/ollama
    ports: ["11435:11434"]
    volumes: ["ollama_data:/root/.ollama"]
""", ["ollama_data"]),
    "metabase/metabase": ("""  metabase:
    image: metabase/metabase
    ports: ["3001:3000"]
    environment:
      MB_DB_FILE: /metabase-data/metabase.db
    volumes: ["metabase_data:/metabase-data"]
""", ["metabase_data"]),
    "louislam/uptime-kuma": ("""  uptime-kuma:
    image: louislam/uptime-kuma:1
    ports: ["3002:3001"]
    volumes: ["kuma_data:/app/data"]
""", ["kuma_data"]),
    "qdrant/qdrant": ("""  qdrant:
    image: qdrant/qdrant
    ports: ["6333:6333"]
    volumes: ["qdrant_data:/qdrant/storage"]
""", ["qdrant_data"]),
}


# ---------- storage helpers ----------
def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def norm(r):
    return {
        "name": r.get("name", ""),
        "repo": (r.get("repo") or "").strip(),
        "layer": r.get("layer") or r.get("area") or "Other",
        "phase": int(r.get("phase") or 2),
        "mode": r.get("mode") if r.get("mode") in MODES else "reference",
        "note": r.get("note", ""),
    }


def load_repos():
    if not CONFIG.exists():
        _write(CONFIG, DEFAULTS)
    return [norm(r) for r in _read(CONFIG, DEFAULTS)]


def merge_defaults(current):
    have_repo = {r["repo"].lower() for r in current if r["repo"]}
    have_name = {r["name"].lower() for r in current}
    added = [d for d in DEFAULTS if (d["repo"] and d["repo"].lower() not in have_repo)
             or (not d["repo"] and d["name"].lower() not in have_name)]
    return current + added, len(added)


# ---------- GitHub ----------
def gh_headers():
    h = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return h


def _fetch(repo):
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}", headers=gh_headers(), timeout=15)
        rem = r.headers.get("x-ratelimit-remaining")
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}", "remaining": rem}
        d = r.json()
        meta = {
            "stars": d.get("stargazers_count"), "pushed": d.get("pushed_at"),
            "license": (d.get("license") or {}).get("spdx_id"), "archived": d.get("archived", False),
            "issues": d.get("open_issues_count"), "description": d.get("description"),
            "release": None, "error": None, "fetched": datetime.now(timezone.utc).isoformat(),
        }
        rel = requests.get(f"https://api.github.com/repos/{repo}/releases/latest", headers=gh_headers(), timeout=15)
        if rel.status_code == 200:
            meta["release"] = rel.json().get("tag_name")
        meta["remaining"] = rel.headers.get("x-ratelimit-remaining", rem)
        return meta
    except requests.RequestException as e:
        return {"error": str(e)[:80]}


def get_all_meta(repos, force=False):
    cache = _read(CACHE, {})
    now = datetime.now(timezone.utc)
    todo = []
    for r in repos:
        c = cache.get(r["repo"]) if r["repo"] else None
        if not r["repo"]:
            continue
        fresh = c and not force and (now - datetime.fromisoformat(c["fetched"])).total_seconds() < CACHE_TTL_H * 3600
        if not fresh:
            todo.append(r["repo"])
    out, remaining = {}, None
    if todo:
        with ThreadPoolExecutor(8) as ex:
            results = list(ex.map(_fetch, todo))
        for repo, res in zip(todo, results):
            remaining = res.get("remaining") or remaining
            if res.get("error"):
                old = cache.get(repo)
                out[repo] = dict(old, error="stale: " + res["error"]) if old else {"error": res["error"]}
            else:
                cache[repo] = res
        _write(CACHE, cache)
    for r in repos:
        if r["repo"] and r["repo"] not in out:
            out[r["repo"]] = cache.get(r["repo"], {"error": "no data"})
    return out, remaining


def days_ago(iso):
    if not iso:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days


def health(m):
    if not m or not m.get("pushed"):
        return None
    if m.get("archived"):
        return 0
    d = days_ago(m["pushed"])
    rec = 50 if d <= 14 else 40 if d <= 60 else 25 if d <= 180 else 10 if d <= 365 else 0
    pop = min(30, math.log10((m.get("stars") or 0) + 1) * 7)
    lic = 10 if m.get("license") not in (None, "NOASSERTION") else 0
    rel = 10 if m.get("release") else 0
    return round(rec + pop + lic + rel)


def license_risk(spdx, repo):
    if repo in LICENSE_NOTES:
        return "high", LICENSE_NOTES[repo]
    s = (spdx or "").upper()
    if s in PERMISSIVE:
        return "low", "Permissive: fine for hosted/white-label use with attribution."
    if s.startswith("AGPL"):
        return "high", "AGPL: if you modify and offer it over a network you must publish your changes."
    if s.startswith(("GPL", "LGPL", "MPL")):
        return "medium", "Copyleft: obligations when you distribute modified versions."
    return "high", "Unknown or custom licence: read LICENSE before client use."


# ---------- git ----------
def git(args, cwd=None):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def local_path(repo):
    return VENDOR / repo.replace("/", "__")


def is_cloned(repo):
    return (local_path(repo) / ".git").exists()


def local_status(repo):
    if not is_cloned(repo):
        return "not cloned"
    path = local_path(repo)
    git(["fetch", "--depth", "1", "origin"], cwd=path)
    code, out = git(["rev-list", "--count", "HEAD..@{u}"], cwd=path)
    if code == 0 and out.isdigit():
        return "up to date" if int(out) == 0 else f"behind by {out}"
    return "cloned"


def sync(repo):
    VENDOR.mkdir(exist_ok=True)
    path = local_path(repo)
    if is_cloned(repo):
        _, out = git(["pull", "--ff-only"], cwd=path)
    else:
        _, out = git(["clone", "--depth", "1", f"https://github.com/{repo}.git", str(path)])
    return repo, out


# ================= UI =================
st.set_page_config(page_title="Recruitment Stack", layout="wide")
st.title("Recruitment consultancy stack: control panel")

# ---- Automation status (written by autorun.py)
_auto = _read(ROOT / "status.json", {})
_digest = ROOT / "digest.md"
_alerts = (len(_auto.get("new_releases", [])) + len(_auto.get("newly_archived", [])) + _auto.get("new_suggestions", 0)
           + len(_auto.get("services_down", [])) + len(_auto.get("errors", [])))
with st.expander(
        ("Automation: last run " + _auto["last_run"].replace("T", " ") + (f"  |  {_alerts} item(s) need attention" if _alerts else "  |  all quiet"))
        if _auto else "Automation: not set up yet", expanded=bool(_alerts)):
    if _auto:
        a1, a2, a3, a4 = st.columns(4)
        a1.metric("New releases", len(_auto.get("new_releases", [])))
        a2.metric("Repos refreshed", f"{_auto.get('repos_refreshed', 0)}/{_auto.get('repos_tracked', 0)}")
        a3.metric("Services down", len(_auto.get("services_down", [])))
        a4.metric("Errors", len(_auto.get("errors", [])))
        if _digest.exists():
            st.markdown(_digest.read_text(encoding="utf-8"))
    else:
        st.write("Run `python autorun.py` once, then schedule it so it runs hourly while this page is closed.")
    if st.button("Run automation now"):
        with st.spinner("Running autorun.py..."):
            try:
                _p = subprocess.run([sys.executable, str(ROOT / "autorun.py"), "--force"],
                                    capture_output=True, text=True, timeout=600)
                st.session_state["_auto_out"] = (_p.stdout + _p.stderr).strip() or "Finished."
            except subprocess.TimeoutExpired:
                st.session_state["_auto_out"] = "Timed out after 10 minutes."
        st.rerun()
    if st.session_state.get("_auto_out"):
        st.code(st.session_state["_auto_out"])
    st.markdown("**Background scheduling:** run `start.bat -Schedule` once, or use the commands below.")
    st.code(
        '$py = (Resolve-Path .\\.venv\\Scripts\\python.exe).Path\n'
        '$sc = (Resolve-Path .\\autorun.py).Path\n'
        'schtasks /Create /SC HOURLY /TN "RecruitStackAutorun" /TR "`"$py`" `"$sc`"" /F\n\n'
        '# optional: start this dashboard at every login\n'
        '$dash = (Resolve-Path .\\dashboard.py).Path\n'
        'schtasks /Create /SC ONLOGON /TN "RecruitStackDashboard" /TR "`"$py`" -m streamlit run `"$dash`" --server.headless true" /F',
        language="powershell")
    st.caption("Set GITHUB_TOKEN permanently with setx so the scheduled task sees it. Optional: set DIGEST_WEBHOOK "
               "to an n8n webhook URL and each run's status.json is POSTed to it for email/WhatsApp alerts.")

repos = load_repos()

with st.sidebar:
    st.header("Filters")
    f_phase = st.multiselect("Phase", list(PHASES), default=list(PHASES), format_func=PHASES.get)
    f_layer = st.multiselect("Layer", sorted({r["layer"] for r in repos}))
    f_mode = st.multiselect("Mode", MODES)
    f_text = st.text_input("Search")
    check_local = st.checkbox("Check clones against upstream (git fetch)")
    force = st.button("Force refresh GitHub data")
    auto_refresh = st.checkbox("Auto-refresh this page")
    auto_min = st.selectbox("Every (minutes)", [1, 5, 15, 30], index=1) if auto_refresh else 5
    if not os.environ.get("GITHUB_TOKEN"):
        st.warning("No GITHUB_TOKEN set: unauthenticated limit is 60 calls/hour, "
                   "and each repo costs 2. Data is cached and fills in over a few loads.")

meta, remaining = get_all_meta(repos, force=force)
if remaining is not None:
    st.sidebar.caption(f"GitHub API calls left this hour: {remaining}")

if auto_refresh:
    @st.fragment(run_every=auto_min * 60)
    def _auto_refresh_tick():
        now_t, last_t = time.time(), st.session_state.get("_ar_last")
        if last_t is None:
            st.session_state["_ar_last"] = now_t
        elif now_t - last_t >= auto_min * 60 - 1:
            st.session_state["_ar_last"] = now_t
            st.rerun()  # full-page rerun: re-reads cache, status.json and service checks
    _auto_refresh_tick()


def visible(r):
    return (r["phase"] in f_phase and (not f_layer or r["layer"] in f_layer)
            and (not f_mode or r["mode"] in f_mode)
            and (not f_text or f_text.lower() in (r["name"] + r["repo"] + r["note"]).lower()))


view = [r for r in repos if visible(r)]

HIST = _read(ROOT / "history.json", {})


def star_delta(repo, current):
    """Stars gained since the earliest daily snapshot inside the last 30 days (needs autorun.py history)."""
    if not HIST or current is None:
        return None
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    for day in sorted(HIST):
        if day >= cutoff and repo in HIST[day]:
            return current - HIST[day][repo]
    return None


# Build one table used by several tabs
rows = []
for r in view:
    m = meta.get(r["repo"], {}) if r["repo"] else {}
    risk, risk_note = license_risk(m.get("license"), r["repo"]) if r["repo"] else ("", "")
    rows.append({
        "Name": r["name"], "Layer": r["layer"], "Phase": r["phase"], "Mode": r["mode"],
        "Health": health(m), "Stars": m.get("stars"), "Stars 30d": star_delta(r["repo"], m.get("stars")),
        "Days since push": days_ago(m.get("pushed")),
        "Release": m.get("release"), "Licence": m.get("license"), "Risk": risk,
        "Status": ("archived" if m.get("archived") else m.get("error") or ("ok" if r["repo"] else "no repo set")),
        "Open issues": m.get("issues"),
        "URL": f"https://github.com/{r['repo']}" if r["repo"] else None,
        "_repo": r["repo"], "_risk_note": risk_note, "_note": r["note"],
    })
df = pd.DataFrame(rows)

(tab_over, tab_cov, tab_rel, tab_lic, tab_sync, tab_stack, tab_manage,
 tab_ready, tab_gov, tab_live, tab_funnel, tab_disc, tab_trends) = st.tabs(
    ["Overview", "Coverage", "Releases", "Licences", "Sync", "Stack", "Manage",
     "Readiness", "Governance", "Live", "Funnel", "Discover", "Trends"])

# ---- Overview
with tab_over:
    tracked = df[df["_repo"] != ""] if len(df) else df
    c = st.columns(5)
    c[0].metric("Repos in view", len(df))
    c[1].metric("Missing repo URL", int((df["_repo"] == "").sum()) if len(df) else 0)
    c[2].metric("Stale (>180 days)", int((tracked["Days since push"].fillna(0) > 180).sum()) if len(tracked) else 0)
    c[3].metric("High licence risk", int((tracked["Risk"] == "high").sum()) if len(tracked) else 0)
    c[4].metric("Cloned locally", sum(is_cloned(r["repo"]) for r in view if r["repo"]))
    if len(df):
        local = {}
        if check_local:
            todo = [r["repo"] for r in view if r["repo"]]
            with ThreadPoolExecutor(6) as ex:
                local = dict(zip(todo, ex.map(local_status, todo)))
        else:
            local = {r["repo"]: ("cloned" if is_cloned(r["repo"]) else "not cloned") for r in view if r["repo"]}
        show = df.copy()
        show["Local"] = show["_repo"].map(local)
        st.dataframe(
            show.drop(columns=["_repo", "_risk_note", "_note"]),
            use_container_width=True, hide_index=True,
            column_config={
                "Health": st.column_config.ProgressColumn("Health", min_value=0, max_value=100, format="%d"),
                "URL": st.column_config.LinkColumn("URL"),
            },
        )
    else:
        st.info("Nothing matches the filters.")

# ---- Coverage
with tab_cov:
    st.caption("Every layer from the completeness checklist, with how many repos you have per phase. "
               "Build phase 1 first; add the rest only when a paying client needs it.")
    cov = []
    for layer in LAYERS:
        items = [r for r in repos if r["layer"] == layer]
        row = {"Layer": layer}
        for p in PHASES:
            row[PHASES[p]] = ", ".join(r["name"] for r in items if r["phase"] == p)
        row["Gap"] = "" if any(r["repo"] for r in items) else "no repo set"
        cov.append(row)
    st.dataframe(pd.DataFrame(cov), use_container_width=True, hide_index=True)
    counts = pd.Series([PHASES[r["phase"]] for r in repos]).value_counts().sort_index()
    st.bar_chart(counts)

# ---- Releases
with tab_rel:
    current = {r["repo"]: meta.get(r["repo"], {}).get("release") for r in repos if r["repo"]}
    current = {k: v for k, v in current.items() if v}
    if not SEEN.exists():
        _write(SEEN, current)
        st.info("Baseline saved. Future new releases will show up here.")
    seen = _read(SEEN, {})
    new = [(repo, seen.get(repo), tag) for repo, tag in current.items() if seen.get(repo) != tag]
    if new:
        st.subheader(f"{len(new)} new release(s) since you last checked")
        st.dataframe(pd.DataFrame(new, columns=["Repo", "Previously seen", "Now"]),
                     use_container_width=True, hide_index=True)
        if st.button("Mark all as seen"):
            _write(SEEN, current)
            st.rerun()
    else:
        st.success("No new releases since your last check.")

# ---- Licences
with tab_lic:
    st.caption("Heuristic only, not legal advice. Check each LICENSE file before using a project "
               "for a client or offering it as a hosted/white-label service.")
    if len(df):
        lic = df[df["_repo"] != ""][["Name", "Mode", "Licence", "Risk", "_risk_note"]].rename(columns={"_risk_note": "What it means"})
        order = {"high": 0, "medium": 1, "low": 2}
        lic = lic.sort_values("Risk", key=lambda s: s.map(order))
        st.dataframe(lic, use_container_width=True, hide_index=True)

# ---- Sync
with tab_sync:
    syncable = [r for r in view if r["repo"]]
    names = [r["name"] for r in syncable]
    chosen = st.multiselect("Clone / pull (shallow, into ./vendor)", names,
                            default=[r["name"] for r in syncable if r["mode"] == "clone"])
    st.caption("Services (Supabase, n8n, Cal.com...) are normally run via Docker or hosted, not cloned.")
    if st.button("Sync selected") and chosen:
        targets = [r["repo"] for r in syncable if r["name"] in chosen]
        with st.spinner("Syncing..."), ThreadPoolExecutor(4) as ex:
            for repo, out in ex.map(sync, targets):
                st.code(f"{repo}\n{out}")

# ---- Stack
with tab_stack:
    avail = {r["repo"]: r["name"] for r in repos if r["repo"] in COMPOSE}
    picks = st.multiselect("Services for docker-compose", list(avail), default=[k for k in ("pgvector/pgvector", "n8n-io/n8n", "ollama/ollama") if k in avail],
                           format_func=lambda k: avail[k])
    if picks:
        vols = sorted({v for k in picks for v in COMPOSE[k][1]})
        text = "# Host ports are shifted to avoid clashing with local Postgres/Ollama.\nservices:\n"
        text += "".join(COMPOSE[k][0] for k in picks)
        text += "\nvolumes:\n" + "".join(f"  {v}:\n" for v in vols)
        st.code(text, language="yaml")
        if st.button("Write docker-compose.generated.yml"):
            (ROOT / "docker-compose.generated.yml").write_text(text, encoding="utf-8")
            st.success("Saved next to dashboard.py. Run: docker compose -f docker-compose.generated.yml up -d")
    st.caption("Cal.com, Documenso, Invoice Ninja, Langfuse, Twenty, Chatwoot and Supabase need several "
               "containers each: use the compose files from their own docs.")

# ---- Manage
with tab_manage:
    st.subheader("Add a repo")
    with st.form("add"):
        c1, c2, c3, c4 = st.columns(4)
        n_name = c1.text_input("Name")
        n_repo = c2.text_input("owner/name")
        n_layer = c3.selectbox("Layer", LAYERS)
        n_phase = c4.selectbox("Phase", list(PHASES), format_func=PHASES.get)
        n_mode = st.selectbox("Mode", MODES)
        if st.form_submit_button("Add"):
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", n_repo):
                st.error("Use the form owner/name.")
            else:
                res = _fetch(n_repo)
                if res.get("error"):
                    st.error(f"GitHub says: {res['error']}. Not added.")
                else:
                    repos.append(norm({"name": n_name or n_repo, "repo": n_repo, "layer": n_layer,
                                       "phase": n_phase, "mode": n_mode}))
                    _write(CONFIG, repos)
                    st.success("Added.")
                    st.rerun()

    st.subheader("Edit list")
    edited = st.data_editor(
        pd.DataFrame(repos), num_rows="dynamic", use_container_width=True, hide_index=True,
        column_config={
            "phase": st.column_config.SelectboxColumn("phase", options=list(PHASES)),
            "mode": st.column_config.SelectboxColumn("mode", options=MODES),
            "layer": st.column_config.SelectboxColumn("layer", options=LAYERS),
        },
    )
    c1, c2, c3 = st.columns(3)
    if c1.button("Save changes"):
        _write(CONFIG, [norm(r) for r in edited.to_dict("records") if r.get("name")])
        st.success("Saved.")
        st.rerun()
    if c2.button("Merge new defaults"):
        merged, n = merge_defaults(repos)
        _write(CONFIG, merged)
        st.success(f"Added {n} new default repo(s).")
        st.rerun()
    c3.download_button("Export CSV", pd.DataFrame(rows).drop(columns=["_repo", "_risk_note", "_note"]).to_csv(index=False),
                       "stack.csv", "text/csv") if rows else None


# ================= Business-side state (state.json) =================
ADOPTION = ["not started", "evaluating", "running", "dropped"]
FUNNEL = [("candidates", "Candidates sourced"), ("shortlisted", "Shortlisted"), ("interviews", "Interviews"),
          ("offers", "Offers"), ("placements", "Placements")]
MILESTONES = [("niche", "Niche chosen"), ("client", "First paying client"),
              ("submission", "First candidate submitted"), ("placement", "First placement")]
GOVERNANCE = [
    ("Privacy policy and cookie policy", "before first CV"),
    ("Candidate consent and data-retention policy", "before first CV"),
    ("Vendor review for every AI/API tool that touches candidate data", "before first CV"),
    ("Incident-response plan for data breaches", "before first CV"),
    ("Client master services agreement", "before first client"),
    ("Placement-fee agreement", "before first client"),
    ("Candidate representation policy (no false claims on experience or eligibility)", "before first submission"),
    ("Anti-discrimination and fair-hiring policy", "before first submission"),
    ("Recruiter code of conduct", "before first submission"),
    ("Human review step before any candidate is submitted", "before first submission"),
    ("Bias check on AI match outputs (age, gender, nationality, other protected traits)", "before first submission"),
    ("Financial controls for placement fees, refunds and guarantees", "before first placement"),
]
SERVICES_DEFAULT = [
    {"name": "Ollama (local)", "url": "http://localhost:11434/api/tags"},
    {"name": "n8n", "url": "http://localhost:5678/healthz"},
    {"name": "Metabase", "url": "http://localhost:3001/api/health"},
    {"name": "Uptime Kuma", "url": "http://localhost:3002"},
    {"name": "Qdrant", "url": "http://localhost:6333/healthz"},
    {"name": "Postgres (compose)", "url": "tcp://localhost:5433"},
]
STATE = ROOT / "state.json"


def load_state():
    s = _read(STATE, {})
    for k, v in (("adoption", {}), ("governance", {}), ("funnel", []), ("milestones", {}), ("services", None)):
        s.setdefault(k, v)
    if not s["services"]:
        s["services"] = SERVICES_DEFAULT
    return s


def save_state(s):
    _write(STATE, s)


def ping(svc):
    url, t0 = svc["url"].strip(), time.time()
    try:
        if url.startswith("tcp://"):
            host, port = url[6:].rsplit(":", 1)
            socket.create_connection((host, int(port)), timeout=2).close()
            up, detail = True, "port open"
        else:
            resp = requests.get(url, timeout=2)
            up, detail = resp.status_code < 500, f"HTTP {resp.status_code}"
    except Exception as e:  # any failure means the service is not reachable
        up, detail = False, type(e).__name__
    return {"Service": svc["name"], "Status": "up" if up else "down", "Detail": detail,
            "ms": round((time.time() - t0) * 1000)}


state = load_state()

# ---- Readiness
with tab_ready:
    st.caption("Track what you have actually stood up, not just what exists. "
               "Mark a repo 'running' only when it works end to end in your environment.")
    ready_rows = [{"Name": r["name"], "Layer": r["layer"], "Phase": r["phase"],
                   "Status": state["adoption"].get(r["name"], "not started")} for r in repos]
    for p in PHASES:
        items = [x for x in ready_rows if x["Phase"] == p]
        done = sum(x["Status"] == "running" for x in items)
        st.progress(done / len(items) if items else 0.0, text=f"{PHASES[p]}: {done} of {len(items)} running")
    started_p2 = [x["Name"] for x in ready_rows if x["Phase"] >= 2 and x["Status"] in ("evaluating", "running")]
    if started_p2 and not state["milestones"].get("client"):
        st.warning("You have phase 2/3 items in progress but no paying client logged yet "
                   f"({', '.join(started_p2[:6])}{'...' if len(started_p2) > 6 else ''}). "
                   "Consider finishing phase 1 and landing the first client before building further.")
    ed = st.data_editor(
        pd.DataFrame(ready_rows), hide_index=True, use_container_width=True, key="ready_editor",
        disabled=["Name", "Layer", "Phase"],
        column_config={"Status": st.column_config.SelectboxColumn("Status", options=ADOPTION)},
    )
    if st.button("Save readiness"):
        state["adoption"] = dict(zip(ed["Name"], ed["Status"]))
        save_state(state)
        st.success("Saved.")

    gov_done = sum(bool(state["governance"].get(t)) for t, _ in GOVERNANCE)
    ms_done = [lbl for k, lbl in MILESTONES if state["milestones"].get(k)]
    report = [f"# Stack progress report ({datetime.now().strftime('%Y-%m-%d')})", ""]
    for p in PHASES:
        items = [x for x in ready_rows if x["Phase"] == p]
        report.append(f"- {PHASES[p]}: {sum(x['Status'] == 'running' for x in items)}/{len(items)} running")
    report += [f"- Governance: {gov_done}/{len(GOVERNANCE)} items done",
               f"- Milestones reached: {', '.join(ms_done) or 'none yet'}"]
    st.download_button("Download progress report (.md)", "\n".join(report), "stack_report.md", "text/markdown")

# ---- Governance
with tab_gov:
    st.caption("Practical checklist, not legal advice. Have a lawyer review the documents. "
               "For Indian candidates the DPDP Act applies; if you handle EU candidates, GDPR does too.")
    st.progress(gov_done / len(GOVERNANCE), text=f"{gov_done} of {len(GOVERNANCE)} done")
    for when in dict.fromkeys(w for _, w in GOVERNANCE):
        st.markdown(f"**{when.capitalize()}**")
        for title, w in GOVERNANCE:
            if w == when:
                val = st.checkbox(title, value=bool(state["governance"].get(title)), key=f"gov_{title}")
                if val != bool(state["governance"].get(title)):
                    state["governance"][title] = val
                    save_state(state)
                    st.rerun()

# ---- Live
with tab_live:
    st.caption("Checks run from the machine hosting this dashboard, so localhost URLs mean your own PC. "
               "Edit the table to add services; use tcp://host:port for databases.")
    svc = st.data_editor(pd.DataFrame(state["services"]), num_rows="dynamic", hide_index=True,
                         use_container_width=True, key="svc_editor")
    c1, c2 = st.columns(2)
    if c2.button("Save service list"):
        state["services"] = [s for s in svc.to_dict("records") if s.get("name") and s.get("url")]
        save_state(state)
        st.success("Saved.")
    if c1.button("Check now"):
        targets = [s for s in svc.to_dict("records") if s.get("name") and s.get("url")]
        with ThreadPoolExecutor(6) as ex:
            res = pd.DataFrame(list(ex.map(ping, targets)))
        st.dataframe(res, hide_index=True, use_container_width=True)
        st.caption(f"{int((res['Status'] == 'up').sum())} of {len(res)} reachable.")

# ---- Funnel
with tab_funnel:
    st.subheader("Milestones")
    cols = st.columns(len(MILESTONES))
    for col, (k, label) in zip(cols, MILESTONES):
        v = col.checkbox(label, value=bool(state["milestones"].get(k)), key=f"ms_{k}")
        if v != bool(state["milestones"].get(k)):
            state["milestones"][k] = v
            save_state(state)
            st.rerun()

    st.subheader("Log a week")
    with st.form("funnel_form"):
        wk = st.date_input("Week ending")
        fc = st.columns(len(FUNNEL))
        vals = [c.number_input(lbl, min_value=0, step=1) for c, (_, lbl) in zip(fc, FUNNEL)]
        rev = st.number_input("Revenue this week (your currency)", min_value=0.0, step=1000.0)
        if st.form_submit_button("Save entry"):
            state["funnel"].append({"week": str(wk), **{k: int(v) for (k, _), v in zip(FUNNEL, vals)}, "revenue": rev})
            save_state(state)
            st.rerun()

    if state["funnel"]:
        fdf = pd.DataFrame(state["funnel"]).sort_values("week")
        tot = {k: int(fdf[k].sum()) for k, _ in FUNNEL}
        mc = st.columns(len(FUNNEL))
        for c, (k, lbl) in zip(mc, FUNNEL):
            c.metric(lbl, tot[k])
        conv = []
        for (a, la), (b, lb) in zip(FUNNEL, FUNNEL[1:]):
            conv.append({"Step": f"{la} -> {lb}", "Conversion": f"{tot[b] / tot[a]:.0%}" if tot[a] else "n/a"})
        st.dataframe(pd.DataFrame(conv), hide_index=True, use_container_width=True)
        st.line_chart(fdf.set_index("week")[["revenue"]])
        st.dataframe(fdf, hide_index=True, use_container_width=True)
    else:
        st.info("No entries yet. Log your first week to see conversion rates.")


# ================= Discover =================
DISCOVER_PRESETS = {
    "ATS: topic applicant-tracking-system": ("topic:applicant-tracking-system", "Employer CRM"),
    "ATS: topic ats": ("topic:ats", "Employer CRM"),
    "Recruitment": ("topic:recruitment", "Employer CRM"),
    "Job boards": ("topic:job-board", "Job feeds"),
    "Resume parsers": ("topic:resume-parser", "Candidate intake & parsing"),
    "Job scrapers": ("topic:job-scraper", "Market research"),
    "Underrated: resume/job matching": ("resume job matching in:description", "AI matching"),
    "Underrated: MCP servers for jobs/hiring": ("mcp server (jobs OR recruiting OR resume) in:description", "AI matching"),
    "Underrated: AI recruiter agents": ('"ai recruiter" in:description', "AI matching"),
}


@st.cache_data(ttl=3600, show_spinner=False)
def search_repos(q, min_stars, max_stars, days, per_page, sort):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    stars = f"stars:{min_stars}..{max_stars}" if max_stars > 0 else f"stars:>={min_stars}"
    query = f"{q} {stars} pushed:>={since} archived:false"
    try:
        r = requests.get("https://api.github.com/search/repositories", headers=gh_headers(), timeout=20,
                         params={"q": query, "sort": sort, "order": "desc", "per_page": per_page})
    except requests.RequestException as e:
        return {"error": str(e)[:100], "items": []}
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code} (search is limited to 10 calls/min without a token)", "items": []}
    return {"error": None, "items": r.json().get("items", [])}


def add_found(found, layer, phase, q):
    cur = load_repos()
    have = {r["repo"].lower() for r in cur if r["repo"]}
    added = 0
    for i in found:
        if i["full_name"].lower() in have:
            continue
        cur.append(norm({"name": i["name"], "repo": i["full_name"], "layer": layer, "phase": phase,
                         "mode": "reference", "note": f"Found via: {q}"[:100]}))
        have.add(i["full_name"].lower())
        added += 1
    _write(CONFIG, cur)
    return added


with tab_disc:
    sug = _read(ROOT / "suggestions.json", {"items": [], "dismissed": []})
    _tracked_l = {r["repo"].lower() for r in repos if r["repo"]}
    inbox = [x for x in sug.get("items", []) if x["repo"].lower() not in _tracked_l]
    with st.expander(f"Inbox from automation: {len(inbox)} suggested repo(s)", expanded=bool(inbox)):
        if not inbox:
            st.write("Nothing waiting. autorun.py searches GitHub on every run and drops new finds here.")
        else:
            st.caption("'resolve:NAME' means autorun thinks this is the repo for a blank entry in your list. "
                       "Check it is the right project before adding. Nothing is added until you apply.")
            idf = pd.DataFrame([{
                "Add": False, "Dismiss": False, "Repo": x["repo"], "Why": x["source"], "Stars": x["stars"],
                "Licence": x["license"], "Days since push": x["pushed_days"], "Description": x["description"],
            } for x in inbox])
            sel = st.data_editor(idf, hide_index=True, use_container_width=True, key="inbox_editor",
                                 disabled=[c for c in idf.columns if c not in ("Add", "Dismiss")])
            if st.button("Apply inbox choices"):
                cur, by, resolved, added = load_repos(), {x["repo"]: x for x in inbox}, set(), 0
                for _, row in sel.iterrows():
                    x = by[row["Repo"]]
                    if row["Add"]:
                        target = None
                        if x["source"].startswith("resolve:"):
                            name = x["source"][len("resolve:"):]
                            target = next((c for c in cur if c["name"] == name and not c["repo"]), None)
                            resolved.add(name)
                        if target:
                            target["repo"] = x["repo"]
                        else:
                            cur.append(norm({"name": x["repo"].split("/")[-1], "repo": x["repo"],
                                             "layer": x.get("layer") or "Employer CRM", "phase": 3, "mode": "reference",
                                             "note": f"Suggested by automation ({x['source']})"[:100]}))
                        added += 1
                    if row["Add"] or row["Dismiss"]:
                        sug.setdefault("dismissed", []).append(x["repo"])
                _write(CONFIG, cur)
                gone = set(sug["dismissed"])
                sug["items"] = [x for x in sug["items"] if x["repo"] not in gone
                                and not (x["source"].startswith("resolve:") and x["source"][8:] in resolved)]
                _write(ROOT / "suggestions.json", sug)
                st.success(f"Added {added} repo(s).")
                st.rerun()
    st.caption("Searches GitHub live, so the list is never stale. Stars are not quality: many ATS repos are "
               "student projects or abandoned. Check licence, recent activity and docs before relying on one.")
    st.caption("To find underrated repos: pick an 'Underrated' search, set min stars 5, max stars 300, "
               "pushed within 90 days, sorted by recently updated. Treat every result as unproven.")
    d1, d2, d3, d4, d5, d6 = st.columns(6)
    preset = d1.selectbox("Search", list(DISCOVER_PRESETS))
    min_stars = d2.number_input("Min stars", 0, 100000, 100, 50)
    max_stars = d3.number_input("Max stars (0 = none)", 0, 1000000, 0, 50)
    within = d4.number_input("Pushed within (days)", 30, 3650, 365, 30)
    max_n = d5.number_input("Max results", 5, 100, 30, 5)
    sort_by = d6.selectbox("Sort by", ["stars", "updated"])
    custom = st.text_input("Or a custom GitHub search", placeholder="e.g. topic:ats language:python")
    q, default_layer = (custom.strip(), "Employer CRM") if custom.strip() else DISCOVER_PRESETS[preset]
    res = search_repos(q, int(min_stars), int(max_stars), int(within), int(max_n), sort_by)
    if res["error"]:
        st.error(res["error"])
    else:
        tracked_now = {r["repo"].lower() for r in repos if r["repo"]}
        items = [i for i in res["items"] if i["full_name"].lower() not in tracked_now]
        st.write(f"{len(items)} new repos found ({len(res['items']) - len(items)} already tracked).")
        if items:
            table = pd.DataFrame([{
                "Add": False, "Repo": i["full_name"], "Stars": i["stargazers_count"],
                "Licence": (i.get("license") or {}).get("spdx_id"), "Days since push": days_ago(i.get("pushed_at")),
                "Flags": ", ".join(f for f in (
                    "no licence" if not i.get("license") else "",
                    "unproven (<50 stars)" if i["stargazers_count"] < 50 else "",
                ) if f),
                "Description": (i.get("description") or "")[:120],
            } for i in items])
            picked = st.data_editor(table, hide_index=True, use_container_width=True,
                                    disabled=[c for c in table.columns if c != "Add"],
                                    key=f"disc_{q}_{min_stars}_{max_stars}_{within}_{max_n}_{sort_by}")
            e1, e2, e3 = st.columns(3)
            layer = e1.selectbox("Add to layer", LAYERS,
                                 index=LAYERS.index(default_layer) if default_layer in LAYERS else 0)
            phase = e2.selectbox("Phase", list(PHASES), index=2, format_func=PHASES.get)
            top_n = e3.number_input("Auto-add top N", 1, 25, 5)
            b1, b2 = st.columns(2)
            by_name = {i["full_name"]: i for i in items}
            if b1.button("Add ticked repos"):
                found = [by_name[n] for n in picked.loc[picked["Add"], "Repo"]]
                st.success(f"Added {add_found(found, layer, phase, q)} repo(s). Reload to see them.")
            if b2.button(f"Auto-add top {int(top_n)} results"):
                st.success(f"Added {add_found(items[:int(top_n)], layer, phase, q)} repo(s). Reload to see them.")

# ---- Trends
with tab_trends:
    days_logged = sorted(HIST)
    if len(days_logged) < 2:
        st.info("History builds up with each autorun, one snapshot per day. Check back after a couple of days.")
    else:
        all_names = sorted({n for d in HIST.values() for n in d})
        phase1 = [r["repo"] for r in repos if r["repo"] and r["phase"] == 1 and r["repo"] in all_names]
        pick_t = st.multiselect("Repos to compare", all_names, default=phase1[:6])
        if pick_t:
            tdf = pd.DataFrame({d: {n: HIST[d].get(n) for n in pick_t} for d in days_logged}).T
            st.line_chart(tdf)
            first, last = days_logged[0], days_logged[-1]
            mom = pd.DataFrame([{"Repo": n, "Stars now": HIST[last].get(n),
                                 "Change": (HIST[last].get(n) or 0) - (HIST[first].get(n) or 0)} for n in pick_t])
            st.caption(f"Change is measured from the first snapshot ({first}) to the latest ({last}).")
            st.dataframe(mom.sort_values("Change", ascending=False), hide_index=True, use_container_width=True)
