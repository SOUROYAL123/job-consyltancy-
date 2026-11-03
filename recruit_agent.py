import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import requests

from security_guard import SecurityGuard

DB_PATH = "recruit_pipeline.db"
CANDIDATES_FILE = "candidates.json"


class RecruitAgent:
    def __init__(self, db_path: str = DB_PATH, candidates_file: str = CANDIDATES_FILE) -> None:
        self.db_path = db_path
        self.candidates_file = Path(candidates_file)
        self.guard = SecurityGuard(db_path=self.db_path)
        self._init_db()
        self._seed_defaults_if_empty()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT NOT NULL,
                    required_skills TEXT NOT NULL,
                    min_exp_years REAL DEFAULT 0,
                    status TEXT DEFAULT 'OPEN',
                    created_utc TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    full_name TEXT NOT NULL,
                    email_masked TEXT NOT NULL,
                    phone_masked TEXT NOT NULL,
                    role_focus TEXT NOT NULL,
                    exp_years REAL DEFAULT 0,
                    skills TEXT NOT NULL,
                    raw_summary TEXT NOT NULL,
                    redacted_summary TEXT NOT NULL,
                    updated_utc TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    matched_skills TEXT NOT NULL,
                    missing_skills TEXT NOT NULL,
                    reasoning TEXT NOT NULL,
                    model_used TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    scored_utc TEXT NOT NULL,
                    UNIQUE(candidate_id, job_id)
                )
                """
            )
            conn.commit()

    def _seed_defaults_if_empty(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM jobs")
            if cur.fetchone()[0] == 0:
                sample_jobs = [
                    (
                        "JOB-101",
                        "Cloud & DevOps Support Engineer",
                        "Enterprise Cloud Solutions",
                        "Kolkata / Remote India",
                        json.dumps(["AWS", "Python", "Docker", "Linux", "CI/CD", "Terraform", "SQLite"]),
                        2.0,
                        "OPEN",
                        now,
                    ),
                    (
                        "JOB-102",
                        "AI Security & Implementation Specialist",
                        "FinTech Guard Systems",
                        "Remote India",
                        json.dumps(["Python", "LLM APIs", "Presidio", "GitHub Actions", "PowerShell", "REST APIs"]),
                        1.5,
                        "OPEN",
                        now,
                    ),
                ]
                conn.executemany(
                    "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    sample_jobs,
                )
                conn.commit()

        if self.candidates_file.exists():
            try:
                raw_data = json.loads(self.candidates_file.read_text(encoding="utf-8"))
                if isinstance(raw_data, list):
                    for item in raw_data:
                        self.ingest_candidate(item)
            except Exception as exc:
                self.guard.log_event("recruit_agent", "CANDIDATE_JSON_LOAD_WARN", str(self.candidates_file), "WARN", str(exc))

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM candidates")
            if cur.fetchone()[0] == 0:
                default_candidates = [
                    {
                        "candidate_id": "CAND-001",
                        "full_name": "Candidate Alpha",
                        "email": "alpha.candidate@example.com",
                        "phone": "+91-9876543210",
                        "role_focus": "Cloud Support Engineer / AWS DevOps",
                        "exp_years": 3.0,
                        "skills": ["AWS", "Python", "PowerShell", "Docker", "GitHub Actions", "Linux", "SQLite"],
                        "summary": "Experienced in AWS cloud support, Python automation, CI/CD pipelines, and Linux troubleshooting.",
                    },
                    {
                        "candidate_id": "CAND-002",
                        "full_name": "Candidate Beta",
                        "email": "beta.dev@example.com",
                        "phone": "+91-9123456789",
                        "role_focus": "Implementation Specialist & API Engineer",
                        "exp_years": 2.0,
                        "skills": ["Python", "REST APIs", "LLM APIs", "SQLite", "Pandas", "Streamlit"],
                        "summary": "Built automated API integrations, SQLite reporting pipelines, and Streamlit operations dashboards.",
                    },
                ]
                for cand in default_candidates:
                    self.ingest_candidate(cand)

    @staticmethod
    def _mask_email(email: str) -> str:
        if "@" not in email:
            return "***@***"
        name, domain = email.split("@", 1)
        return f"{name[:2]}***@{domain}"

    @staticmethod
    def _mask_phone(phone: str) -> str:
        digits = re.sub(r"\D", "", phone)
        return f"******{digits[-4:]}" if len(digits) >= 4 else "******"

    def ingest_candidate(self, record: Dict[str, Any]) -> str:
        cid = str(record.get("candidate_id") or f"CAND-{int(datetime.now().timestamp())}")
        full_name = str(record.get("full_name") or record.get("name") or "Redacted Candidate")
        email = str(record.get("email") or "unknown@example.com")
        phone = str(record.get("phone") or "0000000000")
        role_focus = str(record.get("role_focus") or record.get("role") or "General Engineer")
        exp_years = float(record.get("exp_years") or 1.0)
        skills = record.get("skills") or []
        if isinstance(skills, str):
            skills = [s.strip() for s in skills.split(",") if s.strip()]
        raw_summary = str(record.get("summary") or record.get("raw_summary") or " ".join(skills))

        redacted_summary, _ = self.guard.scan_and_redact(raw_summary, actor="ingest_candidate", target_id=cid)
        now = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO candidates
                (candidate_id, full_name, email_masked, phone_masked, role_focus, exp_years, skills, raw_summary, redacted_summary, updated_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    role_focus=excluded.role_focus,
                    exp_years=excluded.exp_years,
                    skills=excluded.skills,
                    raw_summary=excluded.raw_summary,
                    redacted_summary=excluded.redacted_summary,
                    updated_utc=excluded.updated_utc
                """,
                (
                    cid,
                    full_name,
                    self._mask_email(email),
                    self._mask_phone(phone),
                    role_focus,
                    exp_years,
                    json.dumps(skills),
                    raw_summary,
                    redacted_summary,
                    now,
                ),
            )
            conn.commit()
        return cid

    def _call_llm_gateway(self, redacted_prompt: str) -> Optional[Dict[str, Any]]:
        providers = [
            {
                "name": "groq",
                "url": "https://api.groq.com/openai/v1/chat/completions",
                "key_env": "GROQ_API_KEY",
                "model": "llama-3.3-70b-versatile",
            },
            {
                "name": "deepseek",
                "url": "https://api.deepseek.com/v1/chat/completions",
                "key_env": "DEEPSEEK_API_KEY",
                "model": "deepseek-chat",
            },
            {
                "name": "openrouter",
                "url": "https://openrouter.ai/api/v1/chat/completions",
                "key_env": "OPENROUTER_API_KEY",
                "model": "meta-llama/llama-3.1-8b-instruct:free",
            },
        ]

        for provider in providers:
            api_key = os.environ.get(provider["key_env"], "").strip()
            if not api_key:
                continue
            try:
                self.guard.validate_egress_url(provider["url"])
                resp = requests.post(
                    provider["url"],
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": provider["model"],
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "You are a technical recruitment evaluator. Return ONLY valid JSON with keys: "
                                    "score (int 0-100), reasoning (short string)."
                                ),
                            },
                            {"role": "user", "content": redacted_prompt},
                        ],
                        "temperature": 0.1,
                    },
                    timeout=15,
                )
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"]
                    match = re.search(r"\{.*\}", content, re.DOTALL)
                    if match:
                        parsed = json.loads(match.group(0))
                        return {
                            "score": max(0, min(100, int(parsed.get("score", 70)))),
                            "reasoning": str(parsed.get("reasoning", "Evaluated by LLM gateway.")),
                            "model_used": f"{provider['name']}:{provider['model']}",
                        }
            except Exception as exc:
                self.guard.log_event("recruit_agent", "LLM_PROVIDER_FALLBACK", provider["name"], "WARN", str(exc))
                continue
        return None

    def score_all_candidates(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            jobs = conn.execute("SELECT * FROM jobs WHERE status = 'OPEN'").fetchall()
            candidates = conn.execute("SELECT * FROM candidates").fetchall()

        for job in jobs:
            req_skills = [s.lower() for s in json.loads(job["required_skills"])]
            for cand in candidates:
                cand_skills_raw = json.loads(cand["skills"])
                cand_skills_lower = [s.lower() for s in cand_skills_raw]

                matched = [s for s in cand_skills_raw if s.lower() in req_skills]
                missing = [s for s in json.loads(job["required_skills"]) if s.lower() not in cand_skills_lower]

                skill_ratio = len(matched) / max(1, len(req_skills))
                exp_bonus = 15 if cand["exp_years"] >= job["min_exp_years"] else 5
                deterministic_score = min(100, int(skill_ratio * 85) + exp_bonus)

                raw_prompt = (
                    f"Job Title: {job['title']}\nRequired Skills: {', '.join(req_skills)}\n"
                    f"Candidate Role: {cand['role_focus']}\nCandidate Exp: {cand['exp_years']} yrs\n"
                    f"Candidate Skills: {', '.join(cand_skills_raw)}\n"
                    f"Sanitized Summary: {cand['redacted_summary']}"
                )
                sanitized_prompt, scan_meta = self.guard.scan_and_redact(
                    raw_prompt, actor="recruit_agent", target_id=f"{cand['candidate_id']}_{job['job_id']}"
                )

                llm_eval = self._call_llm_gateway(sanitized_prompt)
                if llm_eval:
                    final_score = llm_eval["score"]
                    reasoning = llm_eval["reasoning"]
                    model_used = llm_eval["model_used"]
                else:
                    final_score = deterministic_score
                    reasoning = (
                        f"Matched {len(matched)}/{len(req_skills)} core skills "
                        f"({', '.join(matched) if matched else 'none'}); {cand['exp_years']}y experience."
                    )
                    model_used = "local-deterministic-scorer"

                now = datetime.now(timezone.utc).isoformat()
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        """
                        INSERT INTO candidate_matches
                        (candidate_id, job_id, score, matched_skills, missing_skills, reasoning, model_used, prompt_hash, scored_utc)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(candidate_id, job_id) DO UPDATE SET
                            score=excluded.score,
                            matched_skills=excluded.matched_skills,
                            missing_skills=excluded.missing_skills,
                            reasoning=excluded.reasoning,
                            model_used=excluded.model_used,
                            prompt_hash=excluded.prompt_hash,
                            scored_utc=excluded.scored_utc
                        """,
                        (
                            cand["candidate_id"],
                            job["job_id"],
                            final_score,
                            json.dumps(matched),
                            json.dumps(missing),
                            reasoning,
                            model_used,
                            scan_meta["prompt_hash"],
                            now,
                        ),
                    )
                    conn.commit()

                if final_score >= 75:
                    draft = (
                        f"Hi {cand['full_name']}, your profile matched {final_score}% for {job['title']} "
                        f"({job['company']} - {job['location']}). Reply YES to opt in to interview scheduling."
                    )
                    self.guard.queue_outreach_for_approval(
                        candidate_id=cand["candidate_id"],
                        job_id=job["job_id"],
                        channel="WhatsApp/Email",
                        recipient_masked=f"{cand['email_masked']} / {cand['phone_masked']}",
                        draft_message=draft,
                    )

                results.append(
                    {
                        "candidate_id": cand["candidate_id"],
                        "job_id": job["job_id"],
                        "score": final_score,
                        "model_used": model_used,
                    }
                )
        return results


if __name__ == "__main__":
    agent = RecruitAgent()
    scored = agent.score_all_candidates()
    print(f"Scored {len(scored)} candidate-job pairs securely.")