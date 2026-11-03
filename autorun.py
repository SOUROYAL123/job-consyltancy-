import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List
import requests

from recruit_agent import RecruitAgent

logging.basicConfig(
    filename="autorun.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

REPOS_FILE = Path("repos.json")
META_FILE = Path(".autorun_meta.json")
SEEN_RELEASES_FILE = Path(".seen_releases.json")
STATUS_FILE = Path("status.json")
SUGGESTIONS_FILE = Path("suggestions.json")
HISTORY_FILE = Path("history.json")
DIGEST_FILE = Path("digest.md")


def load_json(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def audit_github_repos(repos_config: Dict[str, Any], seen_releases: Dict[str, str]) -> Dict[str, Any]:
    headers = {"Accept": "application/vnd.github+json"}
    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if gh_token:
        headers["Authorization"] = f"Bearer {gh_token}"

    total_repos = 0
    critical_repos: List[Dict[str, Any]] = []
    new_releases: List[Dict[str, str]] = []

    categories = repos_config.get("categories", {})
    for category_name, repo_list in categories.items():
        for entry in repo_list:
            total_repos += 1
            repo_slug = entry["repo"]
            if entry.get("priority") == "critical":
                critical_repos.append(
                    {
                        "repo": repo_slug,
                        "category": category_name,
                        "role": entry["role"],
                        "url": f"https://github.com/{repo_slug}",
                    }
                )

            if gh_token and entry.get("tier") == 1:
                try:
                    resp = requests.get(
                        f"https://api.github.com/repos/{repo_slug}/releases/latest",
                        headers=headers,
                        timeout=5,
                    )
                    if resp.status_code == 200:
                        tag = resp.json().get("tag_name", "")
                        if tag and seen_releases.get(repo_slug) != tag:
                            seen_releases[repo_slug] = tag
                            new_releases.append({"repo": repo_slug, "tag": tag})
                except Exception as exc:
                    logging.warning("GitHub release check skipped for %s: %s", repo_slug, exc)

    return {
        "total_repos_linked": total_repos,
        "critical_count": len(critical_repos),
        "critical_repos": critical_repos,
        "new_releases": new_releases,
    }


def generate_suggestions() -> List[Dict[str, str]]:
    suggestions: List[Dict[str, str]] = []
    if not Path(".pre-commit-config.yaml").exists():
        suggestions.append(
            {
                "priority": "HIGH",
                "tool": "gitleaks/gitleaks + PyCQA/bandit",
                "action": "Run 'python -m pre_commit install' to block secrets and insecure AST patterns before git commit.",
            }
        )
    if not os.environ.get("INFISICAL_TOKEN"):
        suggestions.append(
            {
                "priority": "MEDIUM",
                "tool": "Infisical/infisical",
                "action": "Set INFISICAL_TOKEN or launch via 'infisical run' so LLM keys never sit in plaintext .env files.",
            }
        )
    suggestions.append(
        {
            "priority": "HIGH",
            "tool": "microsoft/presidio + protectai/llm-guard",
            "action": "Keep SecurityGuard active in recruit_agent.py so candidate PAN, Aadhaar, phone, and email never reach external LLMs.",
        }
    )
    suggestions.append(
        {
            "priority": "HIGH",
            "tool": "pomerium/pomerium + goauthentik/authentik",
            "action": "Keep Streamlit bound to 127.0.0.1 and expose only via Tailscale or Pomerium + Authentik MFA.",
        }
    )
    return suggestions


def main() -> None:
    now_dt = datetime.now(timezone.utc)
    today_key = now_dt.strftime("%Y-%m-%d")
    logging.info("Starting autorun security and recruitment workflow pass.")

    meta = load_json(META_FILE, {"runs": 0})
    meta["runs"] = int(meta.get("runs", 0)) + 1
    meta["last_run_utc"] = now_dt.isoformat()
    save_json(META_FILE, meta)

    repos_config = load_json(REPOS_FILE, {"categories": {}})
    seen_releases = load_json(SEEN_RELEASES_FILE, {})
    repo_audit = audit_github_repos(repos_config, seen_releases)
    save_json(SEEN_RELEASES_FILE, seen_releases)

    agent = RecruitAgent()
    match_results = agent.score_all_candidates()

    suggestions = generate_suggestions()
    save_json(SUGGESTIONS_FILE, suggestions)

    status_payload = {
        "last_updated_utc": now_dt.isoformat(),
        "total_runs": meta["runs"],
        "repos_linked": repo_audit["total_repos_linked"],
        "critical_repos_count": repo_audit["critical_count"],
        "matches_scored": len(match_results),
        "security_baseline": "ENFORCED_LOCAL_DLP_AND_LOOPBACK",
    }
    save_json(STATUS_FILE, status_payload)

    history = load_json(HISTORY_FILE, {})
    history[today_key] = {
        "runs": meta["runs"],
        "matches_scored": len(match_results),
        "repos_linked": repo_audit["total_repos_linked"],
        "timestamp_utc": now_dt.isoformat(),
    }
    save_json(HISTORY_FILE, history)

    digest_lines = [
        "# Recruitment & Security Stack Daily Digest",
        f"**Generated UTC:** {now_dt.isoformat()}",
        f"**Total Automation Runs:** {meta['runs']}",
        f"**Linked GitHub Security & AI Repos:** {repo_audit['total_repos_linked']} ({repo_audit['critical_count']} Tier-1 Critical)",
        f"**Candidate-Job Pairs Scored:** {len(match_results)}",
        "",
        "## Tier-1 Critical GitHub Stack Active",
    ]
    for item in repo_audit["critical_repos"]:
        digest_lines.append(f"- **{item['repo']}** ({item['category']}): {item['role']} — {item['url']}")

    digest_lines.append("")
    digest_lines.append("## Active Security Action Items")
    for sug in suggestions:
        digest_lines.append(f"- **[{sug['priority']}] {sug['tool']}**: {sug['action']}")

    DIGEST_FILE.write_text("\n".join(digest_lines) + "\n", encoding="utf-8")
    logging.info("Completed autorun workflow pass successfully.")


if __name__ == "__main__":
    main()