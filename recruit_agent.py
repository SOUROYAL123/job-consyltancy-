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
        self._init_and_heal_db_schema()
        self._seed_defaults_if_empty()

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table_name: str, required_columns: Dict[str, str]) -> None:
        """Self-healing SQLite migrator: inspects existing table columns and adds any missing ones automatically."""
        existing = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        for col_name, col_def in required_columns.items():
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")

    def _init_and_heal_db_schema(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT 'Open Role',
                    company TEXT NOT NULL DEFAULT 'Consultancy Client',
                    location TEXT NOT NULL DEFAULT 'Remote India',
                    required_skills TEXT NOT NULL DEFAULT '["Python"]',
                    min_exp_years REAL DEFAULT 0,
                    status TEXT DEFAULT 'OPEN',
                    created_utc TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._ensure_columns(
                conn,
                "jobs",
                {
                    "job_id": "TEXT",
                    "title": "TEXT NOT NULL DEFAULT 'Open Role'",
                    "company": "TEXT NOT NULL DEFAULT 'Consultancy Client'",
                    "location": "TEXT NOT NULL DEFAULT 'Remote India'",
                    "required_skills": "TEXT NOT NULL DEFAULT '[\"Python\"]'",
                    "min_exp_years": "REAL DEFAULT 0",
                    "status": "TEXT DEFAULT 'OPEN'",
                    "created_utc": "TEXT NOT NULL DEFAULT ''",
                },
            )
            # Ensure any legacy rows with NULL status or required_skills are healed
            conn.execute("UPDATE jobs SET status = 'OPEN' WHERE status IS NULL OR status = ''")
            conn.execute("UPDATE jobs SET required_skills = '[\"Python\", \"AWS\", \"SQLite\"]' WHERE required_skills IS NULL OR required_skills = ''")
            conn.execute("UPDATE jobs SET min_exp_years = 1.0 WHERE min_exp_years IS NULL")
            conn.execute("UPDATE jobs SET job_id = 'JOB-' || rowid WHERE job_id IS NULL OR job_id = ''")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    full_name TEXT NOT NULL DEFAULT 'Candidate',
                    email_masked TEXT NOT NULL DEFAULT '***@***',
                    phone_masked TEXT NOT NULL DEFAULT '******',
                    role_focus TEXT NOT NULL DEFAULT 'Engineer',
                    exp_years REAL DEFAULT 0,
                    skills TEXT NOT NULL DEFAULT '["Python"]',
                    raw_summary TEXT NOT NULL DEFAULT '',
                    redacted_summary TEXT NOT NULL DEFAULT '',
                    updated_utc TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._ensure_columns(
                conn,
                "candidates",
                {
                    "candidate_id": "TEXT",
                    "full_name": "TEXT NOT NULL DEFAULT 'Candidate'",
                    "email_masked": "TEXT NOT NULL DEFAULT '***@***'",
                    "phone_masked": "TEXT NOT NULL DEFAULT '******'",
                    "role_focus": "TEXT NOT NULL DEFAULT 'Engineer'",
                    "exp_years": "REAL DEFAULT 0",
                    "skills": "TEXT NOT NULL DEFAULT '[\"Python\"]'",
                    "raw_summary": "TEXT NOT NULL DEFAULT ''",
                    "redacted_summary": "TEXT NOT NULL DEFAULT ''",
                    "updated_utc": "TEXT NOT NULL DEFAULT ''",
                },
            )
            conn.execute("UPDATE candidates SET candidate_id = 'CAND-' || rowid WHERE candidate_id IS NULL OR candidate_id = ''")
            conn.execute("UPDATE candidates SET skills = '[\"Python\", \"AWS\"]' WHERE skills IS NULL OR skills = ''")
            conn.execute("UPDATE candidates SET exp_years = 1.0 WHERE exp_years IS NULL")
            conn.execute("UPDATE candidates SET redacted_summary = 'Sanitized profile' WHERE redacted_summary IS NULL")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    score INTEGER NOT NULL DEFAULT 0,
                    matched_skills TEXT NOT NULL DEFAULT '[]',
                    missing_skills TEXT NOT NULL DEFAULT '[]',
                    reasoning TEXT NOT NULL DEFAULT '',
                    model_used TEXT NOT NULL DEFAULT 'local-deterministic-scorer',
                    prompt_hash TEXT NOT NULL DEFAULT '',
                    scored_utc TEXT NOT NULL DEFAULT '',
                    UNIQUE(candidate_id, job_id)
                )
                """
            )
            self._ensure_columns(
                conn,
                "candidate_matches",
                {
                    "candidate_id": "TEXT NOT NULL DEFAULT ''",
                    "job_id": "TEXT NOT NULL DEFAULT ''",
                    "score": "INTEGER NOT NULL DEFAULT 0",
                    "matched_skills": "TEXT NOT NULL DEFAULT '[]'",
                    "missing_skills": "TEXT NOT NULL DEFAULT '[]'",
                    "reasoning": "TEXT NOT NULL DEFAULT ''",
                    "model_used": "TEXT NOT NULL DEFAULT 'local-deterministic-scorer'",
                    "prompt_hash": "TEXT NOT NULL DEFAULT ''",
                    "scored_utc": "TEXT NOT NULL DEFAULT ''",
                },
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
                    "INSERT INTO jobs (job_id, title, company, location, required_skills, min_exp_years, status, created_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    sample_jobs,
                )
                conn.commit()

        if self.candidates_file.exists():
            try:
                raw_data = json.loads(self.candidates_file.read_text(encoding="utf-8"))
                if isinstance(raw_data, list):
                    for item in raw_data:
                        if isinstance(item, dict):
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

    @staticmethod
    def _parse_skills_safe(raw_val: Any) -> List[str]:
        if isinstance(raw_val, list):
            return [str(x).strip() for x in raw_val if str(x).strip()]
        if isinstance(raw_val, str):
            try:
                parsed = json.loads(raw_val)
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except Exception:
                pass
            return [s.strip() for s in raw_val.split(",") if s.strip()]
        return ["Python"]

    def ingest_candidate(self, record: Dict[str, Any]) -> str:
        cid = str(record.get("candidate_id") or record.get("id") or f"CAND-{int(datetime.now().timestamp())}")
        full_name = str(record.get("full_name") or record.get("name") or "Redacted Candidate")
        email = str(record.get("email") or "unknown@example.com")
        phone = str(record.get("phone") or "0000000000")
        role_focus = str(record.get("role_focus") or record.get("role") or "General Engineer")
        try:
            exp_years = float(record.get("exp_years") or record.get("experience") or 1.0)
        except Exception:
            exp_years = 1.0
        skills = self._parse_skills_safe(record.get("skills") or ["Python"])
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
            job_id = str(job["job_id"] or "JOB-101")
            job_title = str(job["title"] or "Engineering Role")
            job_company = str(job["company"] or "Consultancy Client")
            job_location = str(job["location"] or "Remote India")
            req_skills_raw = self._parse_skills_safe(job["required_skills"])
            req_skills = [s.lower() for s in req_skills_raw]
            min_exp = float(job["min_exp_years"] or 0.0)

            for cand in candidates:
                cand_id = str(cand["candidate_id"] or "CAND-001")
                cand_name = str(cand["full_name"] or "Candidate")
                cand_role = str(cand["role_focus"] or "Engineer")
                cand_exp = float(cand["exp_years"] or 0.0)
                cand_skills_raw = self._parse_skills_safe(cand["skills"])
                cand_skills_lower = [s.lower() for s in cand_skills_raw]

                matched = [s for s in cand_skills_raw if s.lower() in req_skills]
                missing = [s for s in req_skills_raw if s.lower() not in cand_skills_lower]

                skill_ratio = len(matched) / max(1, len(req_skills))
                exp_bonus = 15 if cand_exp >= min_exp else 5
                deterministic_score = min(100, int(skill_ratio * 85) + exp_bonus)

                raw_prompt = (
                    f"Job Title: {job_title}\nRequired Skills: {', '.join(req_skills)}\n"
                    f"Candidate Role: {cand_role}\nCandidate Exp: {cand_exp} yrs\n"
                    f"Candidate Skills: {', '.join(cand_skills_raw)}\n"
                    f"Sanitized Summary: {cand['redacted_summary']}"
                )
                sanitized_prompt, scan_meta = self.guard.scan_and_redact(
                    raw_prompt, actor="recruit_agent", target_id=f"{cand_id}_{job_id}"
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
                        f"({', '.join(matched) if matched else 'none'}); {cand_exp}y experience."
                    )
                    model_used = "local-deterministic-scorer"

                now = datetime.now(timezone.utc).isoformat()
                with sqlite3.connect(self.db_path) as conn:
                    # Clean up any duplicate rows if table existed prior to UNIQUE constraint
                    conn.execute(
                        "DELETE FROM candidate_matches WHERE candidate_id = ? AND job_id = ?",
                        (cand_id, job_id),
                    )
                    conn.execute(
                        """
                        INSERT INTO candidate_matches
                        (candidate_id, job_id, score, matched_skills, missing_skills, reasoning, model_used, prompt_hash, scored_utc)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cand_id,
                            job_id,
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
                        f"Hi {cand_name}, your profile matched {final_score}% for {job_title} "
                        f"({job_company} - {job_location}). Reply YES to opt in to interview scheduling."
                    )
                    self.guard.queue_outreach_for_approval(
                        candidate_id=cand_id,
                        job_id=job_id,
                        channel="WhatsApp/Email",
                        recipient_masked=f"{cand['email_masked']} / {cand['phone_masked']}",
                        draft_message=draft,
                    )

                results.append(
                    {
                        "candidate_id": cand_id,
                        "job_id": job_id,
                        "score": final_score,
                        "model_used": model_used,
                    }
                )
        return results


if __name__ == "__main__":
    agent = RecruitAgent()
    scored = agent.score_all_candidates()
    print(f"Scored {len(scored)} candidate-job pairs securely.")
