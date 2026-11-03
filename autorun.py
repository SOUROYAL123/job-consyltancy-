"""
Headless automation for the recruitment-stack dashboard. Does not need Streamlit.

    python autorun.py            run once
    python autorun.py --force    ignore cache age and refresh everything the rate limit allows

Each run:
  1. refreshes GitHub metadata for tracked repos into .cache_meta.json (the cache the dashboard reads)
  2. detects what changed since the previous run: new releases, newly archived, newly stale (>180 days)
  3. fast-forward pulls repos you already cloned into ./vendor
  4. checks the services listed in state.json (Live tab)
  5. writes status.json and digest.md, appends to autorun.log
  6. if DIGEST_WEBHOOK is set (e.g. an n8n webhook URL), POSTs status.json to it

Schedule it hourly with Windows Task Scheduler (command shown in the dashboard's Automation panel).
"""
import argparse
import json
import os
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CONFIG = ROOT / "repos.json"
CACHE = ROOT / ".cache_meta.json"
STATE = ROOT / "state.json"
VENDOR = ROOT / "vendor"
BASELINE = ROOT / ".autorun_baseline.json"
STATUS = ROOT / "status.json"
DIGEST = ROOT / "digest.md"
LOG = ROOT / "autorun.log"

CACHE_TTL_H = 6
STALE_DAYS = 180
UNAUTH_BATCH = 25  # each repo costs 2 API calls; unauthenticated limit is 60/hour
SUGGEST = ROOT / "suggestions.json"
HISTORY = ROOT / "history.json"
META = ROOT / ".autorun_meta.json"
INBOX_MAX = 60
# (search query, layer). One is tried per run, rotating, so the search API is barely touched.
DISCOVER_PRESETS = [
    ("topic:applicant-tracking-system", "Employer CRM"),
    ("topic:ats", "Employer CRM"),
    ("topic:recruitment", "Employer CRM"),
    ("topic:job-board", "Job feeds"),
    ("topic:resume-parser", "Candidate intake & parsing"),
    ("topic:job-scraper", "Market research"),
    ("resume job matching in:description", "AI matching"),
    ("mcp server (jobs OR recruiting OR resume) in:description", "AI matching"),
    ('"ai recruiter" in:description', "AI matching"),
]


def read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def headers():
    h = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return h


def days_ago(iso):
    if not iso:
        return None
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days


def fetch(repo):
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}", headers=headers(), timeout=15)
        if r.status_code != 200:
            return repo, {"error": f"HTTP {r.status_code}"}
        d = r.json()
        meta = {
            "stars": d.get("stargazers_count"), "pushed": d.get("pushed_at"),
            "license": (d.get("license") or {}).get("spdx_id"), "archived": d.get("archived", False),
            "issues": d.get("open_issues_count"), "description": d.get("description"),
            "release": None, "error": None, "fetched": datetime.now(timezone.utc).isoformat(),
        }
        rel = requests.get(f"https://api.github.com/repos/{repo}/releases/latest", headers=headers(), timeout=15)
        if rel.status_code == 200:
            meta["release"] = rel.json().get("tag_name")
        meta["remaining"] = rel.headers.get("x-ratelimit-remaining")
        return repo, meta
    except requests.RequestException as e:
        return repo, {"error": str(e)[:80]}


def git(args, cwd=None):
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=300)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)[:100]


def ping(svc):
    url, t0 = svc["url"].strip(), time.time()
    try:
        if url.startswith("tcp://"):
            host, port = url[6:].rsplit(":", 1)
            socket.create_connection((host, int(port)), timeout=2).close()
            up = True
        else:
            up = requests.get(url, timeout=2).status_code < 500
    except Exception:
        up = False
    return {"name": svc["name"], "up": up, "ms": round((time.time() - t0) * 1000)}


def search(q, sort="stars", per_page=10):
    try:
        r = requests.get("https://api.github.com/search/repositories", headers=headers(), timeout=20,
                         params={"q": q, "sort": sort, "order": "desc", "per_page": per_page})
    except requests.RequestException:
        return []
    return r.json().get("items", []) if r.status_code == 200 else []


def summarize(i, source, layer):
    return {"repo": i["full_name"], "stars": i.get("stargazers_count"),
            "license": (i.get("license") or {}).get("spdx_id"), "pushed_days": days_ago(i.get("pushed_at")),
            "description": (i.get("description") or "")[:140], "source": source, "layer": layer,
            "found": datetime.now().strftime("%Y-%m-%d")}


def discover(repos):
    """Finds repos worth a look and adds them to suggestions.json. Never edits repos.json itself."""
    meta = read(META, {"runs": 0})
    run = int(meta.get("runs", 0))
    sug = read(SUGGEST, {"items": [], "dismissed": []})
    known = {r["repo"].lower() for r in repos if r.get("repo")}
    known |= {x["repo"].lower() for x in sug["items"]} | {d.lower() for d in sug.get("dismissed", [])}
    new = []

    # a) try to fill in entries whose GitHub owner/name is unknown
    blanks = [r for r in repos if not r.get("repo") and "not a repo" not in (r.get("note") or "").lower()]
    if blanks:
        k = run % len(blanks)
        for r in (blanks[k:] + blanks[:k])[:3]:
            added = 0
            for i in search(f'"{r["name"]}" in:name', per_page=5):
                if i["full_name"].lower() in known or added >= 2:
                    continue
                new.append(summarize(i, f"resolve:{r['name']}", r.get("layer") or "Other"))
                known.add(i["full_name"].lower())
                added += 1

    # b) rotating topic search: popular on even runs, underrated and recently updated on odd runs
    q, layer = DISCOVER_PRESETS[run % len(DISCOVER_PRESETS)]
    since = (datetime.now(timezone.utc) - timedelta(days=180)).strftime("%Y-%m-%d")
    if run % 2 == 0:
        items = search(f"{q} stars:>=100 pushed:>={since} archived:false", "stars", 15)
    else:
        items = search(f"{q} stars:10..300 pushed:>={since} archived:false", "updated", 15)
    added = 0
    for i in items:
        if i["full_name"].lower() in known or added >= 8:
            continue
        new.append(summarize(i, q, layer))
        known.add(i["full_name"].lower())
        added += 1

    sug["items"] = (new + sug["items"])[:INBOX_MAX]
    write(SUGGEST, sug)
    write(META, {"runs": run + 1})
    return new, len(sug["items"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    started = datetime.now()

    repos = read(CONFIG, [])
    if not repos:
        log("No repos.json yet. Open the dashboard once so it creates it, then run again.")
        return
    cache = read(CACHE, {})
    tracked = sorted({r["repo"] for r in repos if r.get("repo")})
    has_token = bool(os.environ.get("GITHUB_TOKEN"))

    # 1. refresh metadata (oldest first; capped when unauthenticated)
    now = datetime.now(timezone.utc)

    def age_h(repo):
        c = cache.get(repo)
        if not c or not c.get("fetched"):
            return 1e9
        return (now - datetime.fromisoformat(c["fetched"])).total_seconds() / 3600

    todo = [r for r in tracked if args.force or age_h(r) >= CACHE_TTL_H]
    todo.sort(key=age_h, reverse=True)
    if not has_token:
        todo = todo[:UNAUTH_BATCH]
    errors, remaining = [], None
    if todo:
        with ThreadPoolExecutor(6) as ex:
            for repo, meta in ex.map(fetch, todo):
                if meta.get("error"):
                    errors.append(f"{repo}: {meta['error']}")
                else:
                    cache[repo] = meta
                    remaining = meta.get("remaining") or remaining
        write(CACHE, cache)

    # 2. detect changes against the previous run's baseline
    current = {}
    for repo in tracked:
        m = cache.get(repo)
        if m and not m.get("error"):
            d = days_ago(m.get("pushed"))
            current[repo] = {"release": m.get("release"), "archived": bool(m.get("archived")),
                             "stale": d is not None and d > STALE_DAYS}
    baseline = read(BASELINE, None)
    new_releases, newly_archived, newly_stale = [], [], []
    if baseline is not None:
        for repo, now_s in current.items():
            old = baseline.get(repo)
            if not old:
                continue
            if now_s["release"] and now_s["release"] != old.get("release"):
                new_releases.append({"repo": repo, "from": old.get("release"), "to": now_s["release"]})
            if now_s["archived"] and not old.get("archived"):
                newly_archived.append(repo)
            if now_s["stale"] and not old.get("stale"):
                newly_stale.append(repo)
    write(BASELINE, {**(baseline or {}), **current})

    # 2b. one star-count snapshot per day, for the dashboard's trend views
    hist = read(HISTORY, {})
    hist[datetime.now().strftime("%Y-%m-%d")] = {
        repo: cache[repo]["stars"] for repo in tracked
        if cache.get(repo) and cache[repo].get("stars") is not None}
    for old_day in sorted(hist)[:-120]:
        del hist[old_day]
    write(HISTORY, hist)

    # 3. pull repos that are already cloned
    pulled, pull_errors = [], []
    for repo in tracked:
        path = VENDOR / repo.replace("/", "__")
        if (path / ".git").exists():
            code, out = git(["pull", "--ff-only"], cwd=path)
            if code != 0:
                pull_errors.append(f"{repo}: {out[:100]}")
            elif "Already up to date" not in out:
                pulled.append(repo)

    # 4. services
    services = read(STATE, {}).get("services") or []
    services = [s for s in services if s.get("name") and s.get("url")]
    svc_results = []
    if services:
        with ThreadPoolExecutor(6) as ex:
            svc_results = list(ex.map(ping, services))
    down = [s["name"] for s in svc_results if not s["up"]]

    # 4b. look for new repos worth tracking (suggestions only; you approve them in the dashboard)
    new_found, inbox_total = discover(repos)

    # 5. status + digest
    status = {
        "new_suggestions": len(new_found), "inbox_total": inbox_total,
        "last_run": started.isoformat(timespec="seconds"),
        "repos_tracked": len(tracked), "repos_refreshed": len(todo) - len(errors),
        "authenticated": has_token, "api_calls_left": remaining,
        "first_run": baseline is None,
        "new_releases": new_releases, "newly_archived": newly_archived, "newly_stale": newly_stale,
        "pulled": pulled, "errors": errors + pull_errors,
        "services_checked": len(svc_results), "services_down": down,
    }
    write(STATUS, status)

    lines = [f"# Stack digest, {started:%Y-%m-%d %H:%M}", ""]
    if status["first_run"]:
        lines += ["First run: baseline saved. Changes will be reported from the next run.", ""]
    if new_releases:
        lines += ["## New releases"] + [f"- {x['repo']}: {x['from'] or 'none'} -> {x['to']}" for x in new_releases] + [""]
    if newly_archived:
        lines += ["## Archived upstream (consider replacing)"] + [f"- {r}" for r in newly_archived] + [""]
    if newly_stale:
        lines += [f"## No commits for {STALE_DAYS}+ days"] + [f"- {r}" for r in newly_stale] + [""]
    if pulled:
        lines += ["## Local clones updated"] + [f"- {r}" for r in pulled] + [""]
    if new_found:
        lines += [f"## {len(new_found)} new repo(s) suggested (review in the dashboard's Discover tab)"]
        lines += [f"- {x['repo']} ({x['stars']} stars): {x['source']}" for x in new_found[:10]] + [""]
    if down:
        lines += ["## Services down"] + [f"- {n}" for n in down] + [""]
    if status["errors"]:
        lines += ["## Errors"] + [f"- {e}" for e in status["errors"][:15]] + [""]
    if len(lines) <= 2:
        lines += ["Nothing changed since the last run.", ""]
    if not has_token:
        lines += [f"_No GITHUB_TOKEN: only {UNAUTH_BATCH} repos are refreshed per run._", ""]
    DIGEST.write_text("\n".join(lines), encoding="utf-8")

    log(f"run ok: refreshed {status['repos_refreshed']}/{len(tracked)}, releases {len(new_releases)}, "
        f"pulled {len(pulled)}, services down {len(down)}, errors {len(status['errors'])}")

    # 6. optional webhook (e.g. n8n)
    hook = os.environ.get("DIGEST_WEBHOOK")
    if hook:
        try:
            requests.post(hook, json=status, timeout=10)
        except requests.RequestException as e:
            log(f"webhook failed: {str(e)[:80]}")


if __name__ == "__main__":
    main()
