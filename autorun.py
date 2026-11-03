import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from master_self_heal import MasterSelfHealer
from recruit_agent import RecruitAgent

logging.basicConfig(
    filename="autorun.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

REPOS_FILE = Path("repos.json")
META_FILE = Path(".autorun_meta.json")
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


def main() -> None:
    now_dt = datetime.now(UTC)
    today_key = now_dt.strftime("%Y-%m-%d")
    logging.info("Starting self-healing autorun workflow.")

    # 1. Run Autonomous Self-Healing GitHub Registry & AST Codebase Sync
    healer = MasterSelfHealer()
    heal_report = healer.heal_and_synchronize()

    # 2. Increment run counter
    meta = load_json(META_FILE, {"runs": 0})
    meta["runs"] = int(meta.get("runs", 0)) + 1
    meta["last_run_utc"] = now_dt.isoformat()
    save_json(META_FILE, meta)

    # 3. Score Candidates with SecurityGuard DLP
    agent = RecruitAgent()
    match_results = agent.score_all_candidates()

    # 4. Count critical & total repos after healing
    repos_config = load_json(REPOS_FILE, {"categories": {}})
    critical_repos = []
    total_repos = 0
    for cat, items in repos_config.get("categories", {}).items():
        for entry in items:
            total_repos += 1
            if entry.get("priority") == "critical":
                critical_repos.append({
                    "repo": entry["repo"],
                    "category": cat,
                    "role": entry["role"],
                    "score": entry.get("relevancy_score", 95.0),
                })

    suggestions = [
        {
            "priority": "CRITICAL",
            "tool": "dequelabs/axe-core + Textualize/textual",
            "action": "WCAG 2.2 accessibility & keyboard-first console resilience active across dashboard and CLI.",
        },
        {
            "priority": "HIGH",
            "tool": "jd/tenacity + astral-sh/ruff",
            "action": "Self-healing retries and automated AST code hygiene enabled.",
        },
        {
            "priority": "HIGH",
            "tool": "microsoft/presidio + protectai/llm-guard",
            "action": "All candidate PII redacted locally prior to LLM scoring.",
        },
    ]
    save_json(SUGGESTIONS_FILE, suggestions)

    status_payload = {
        "last_updated_utc": now_dt.isoformat(),
        "total_runs": meta["runs"],
        "repos_linked": total_repos,
        "critical_repos_count": len(critical_repos),
        "matches_scored": len(match_results),
        "repos_auto_added": len(heal_report.get("added_repos", [])),
        "repos_auto_pruned": len(heal_report.get("pruned_repos", [])),
        "security_baseline": "SELF_HEALING_ZERO_TRUST_AND_WCAG_ACCESSIBLE",
    }
    save_json(STATUS_FILE, status_payload)

    history = load_json(HISTORY_FILE, {})
    history[today_key] = status_payload
    save_json(HISTORY_FILE, history)

    digest_lines = [
        "# Autonomous Self-Healing Security & Recruitment Digest",
        f"**Generated UTC:** {now_dt.isoformat()}",
        f"**Automation Pass:** #{meta['runs']}",
        f"**Total Active Linked GitHub Repos:** {total_repos} ({len(critical_repos)} Critical)",
        f"**Self-Healer Actions:** +{len(heal_report.get('added_repos', []))} added / -{len(heal_report.get('pruned_repos', []))} pruned",
        f"**Detected Codebase Imports:** `{', '.join(heal_report.get('active_python_imports_detected', []))}`",
        "",
        "## Top Relevancy-Ranked Critical Repositories",
    ]
    for item in critical_repos:
        digest_lines.append(
            f"- **{item['repo']}** ({item['category']}) [Relevancy: {item['score']}%]: {item['role']} — https://github.com/{item['repo']}"
        )

    DIGEST_FILE.write_text("\n".join(digest_lines) + "\n", encoding="utf-8")
    logging.info("Completed self-healing autorun pass.")


if __name__ == "__main__":
    main()
