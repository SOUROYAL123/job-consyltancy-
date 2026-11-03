import io
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from security_guard import SecurityGuard

try:
    from pypdf import PdfReader
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

try:
    import docx
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

DB_PATH = "recruit_pipeline.db"
CANDIDATES_FILE = "candidates.json"

KNOWN_TECH_SKILLS = [
    "AWS", "Azure", "GCP", "Python", "PowerShell", "Bash", "Linux", "Windows Server",
    "Docker", "Kubernetes", "Terraform", "Ansible", "Jenkins", "GitHub Actions",
    "GitLab CI", "CI/CD", "Git", "SQL", "SQLite", "PostgreSQL", "MySQL", "MongoDB",
    "Redis", "REST APIs", "FastAPI", "Flask", "Django", "Streamlit", "Pandas",
    "NumPy", "LLM APIs", "OpenAI", "LangChain", "Presidio", "Prometheus", "Grafana",
    "Datadog", "Splunk", "Wazuh", "Networking", "TCP/IP", "DNS", "VPN", "Firewalls",
    "IAM", "Active Directory", "Jira", "ServiceNow", "ITIL", "DevOps", "SRE",
    "Cloud Support", "React", "Node.js", "JavaScript", "TypeScript", "Java", "Go"
]


class RecruitAgent:
    def __init__(self, db_path: str = DB_PATH, candidates_file: str = CANDIDATES_FILE) -> None:
        self.db_path = db_path
        self.candidates_file = Path(candidates_file)
        self.guard = SecurityGuard(db_path=self.db_path)
        self._init_and_heal_db_schema()

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table_name: str, required_columns: Dict[str, str]) -> None:
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
                    source_url TEXT DEFAULT '',
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
                    "source_url": "TEXT DEFAULT ''",
                    "created_utc": "TEXT NOT NULL DEFAULT ''",
                },
            )
            conn.execute("UPDATE jobs SET status = 'OPEN' WHERE status IS NULL OR status = ''")
            conn.execute("UPDATE jobs SET required_skills = '[\"Python\", \"AWS\"]' WHERE required_skills IS NULL OR required_skills = ''")
            conn.execute("UPDATE jobs SET min_exp_years = 1.0 WHERE min_exp_years IS NULL")
            conn.execute("UPDATE jobs SET job_id = 'JOB-' || rowid WHERE job_id IS NULL OR job_id = ''")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candidates (
                    candidate_id TEXT PRIMARY KEY,
                    full_name TEXT NOT NULL DEFAULT 'Candidate',
                    email_raw TEXT DEFAULT '',
                    phone_raw TEXT DEFAULT '',
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
                    "email_raw": "TEXT DEFAULT ''",
                    "phone_raw": "TEXT DEFAULT ''",
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

    def purge_dummy_data(self) -> int:
        """Removes hardcoded sample records so only real jobs and real candidates remain."""
        with sqlite3.connect(self.db_path) as conn:
            c1 = conn.execute("DELETE FROM jobs WHERE job_id IN ('JOB-101', 'JOB-102')").rowcount
            c2 = conn.execute("DELETE FROM candidates WHERE candidate_id IN ('CAND-001', 'CAND-002', 'CAND-003')").rowcount
            c3 = conn.execute(
                "DELETE FROM candidate_matches WHERE job_id IN ('JOB-101', 'JOB-102') OR candidate_id IN ('CAND-001', 'CAND-002', 'CAND-003')"
            ).rowcount
            conn.execute(
                "DELETE FROM outreach_approval_queue WHERE candidate_id IN ('CAND-001', 'CAND-002', 'CAND-003')"
            )
            conn.commit()
        total_removed = c1 + c2 + c3
        self.guard.log_event("recruiter_admin", "PURGE_DUMMY_DATA", "database", "CLEANED", f"Removed {total_removed} sample rows")
        return total_removed

    def add_job(
        self,
        title: str,
        company: str,
        location: str,
        required_skills: List[str],
        min_exp_years: float = 1.0,
        source_url: str = "",
        job_id: Optional[str] = None,
    ) -> str:
        jid = job_id or f"JOB-{int(datetime.now(timezone.utc).timestamp() * 1000) % 1000000}"
        now = datetime.now(timezone.utc).isoformat()
        clean_skills = self._parse_skills_safe(required_skills)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO jobs (job_id, title, company, location, required_skills, min_exp_years, status, source_url, created_utc)
                VALUES (?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    title=excluded.title,
                    company=excluded.company,
                    location=excluded.location,
                    required_skills=excluded.required_skills,
                    min_exp_years=excluded.min_exp_years,
                    source_url=excluded.source_url,
                    status='OPEN'
                """,
                (jid, title.strip(), company.strip(), location.strip(), json.dumps(clean_skills), float(min_exp_years), source_url, now),
            )
            conn.commit()
        return jid

    def fetch_live_jobs_from_web(self, search_term: str = "cloud", max_jobs: int = 10) -> int:
        """Pulls real live tech job openings from Remotive & Arbeitnow public JSON APIs without needing an API key."""
        added = 0
        url = f"https://remotive.com/api/remote-jobs?search={requests.utils.quote(search_term)}&limit={max_jobs}"
        try:
            resp = requests.get(url, timeout=12)
            if resp.status_code == 200:
                jobs_list = resp.json().get("jobs", [])[:max_jobs]
                for item in jobs_list:
                    raw_desc = re.sub(r"<[^>]+>", " ", str(item.get("description", "")))
                    extracted_skills = self.extract_skills_from_text(
                        f"{item.get('title', '')} {' '.join(item.get('tags', []))} {raw_desc}"
                    )
                    if not extracted_skills:
                        extracted_skills = ["Python", "Linux", "Cloud"]
                    jid = f"LIVE-{item.get('id')}"
                    self.add_job(
                        job_id=jid,
                        title=str(item.get("title", "Cloud Role")),
                        company=str(item.get("company_name", "Global Tech")),
                        location=str(item.get("candidate_required_location", "Remote")),
                        required_skills=extracted_skills,
                        min_exp_years=2.0,
                        source_url=str(item.get("url", "")),
                    )
                    added += 1
        except Exception as exc:
            self.guard.log_event("recruit_agent", "LIVE_JOB_FETCH_WARN", search_term, "WARN", str(exc))
        return added

    @staticmethod
    def extract_skills_from_text(text: str) -> List[str]:
        found: List[str] = []
        text_lower = text.lower()
        for skill in KNOWN_TECH_SKILLS:
            pattern = r"\b" + re.escape(skill.lower()) + r"\b"
            if re.search(pattern, text_lower):
                found.append(skill)
        return found

    def parse_and_ingest_resume_file(self, filename: str, file_bytes: bytes, role_override: str = "") -> str:
        """Extracts text from a real PDF, DOCX, or TXT resume, identifies contact details & skills, sanitizes PII, and saves."""
        ext = Path(filename).suffix.lower()
        raw_text = ""

        if ext == ".pdf" and PYPDF_AVAILABLE:
            reader = PdfReader(io.BytesIO(file_bytes))
            pages = [page.extract_text() or "" for page in reader.pages]
            raw_text = "\n".join(pages)
        elif ext == ".docx" and DOCX_AVAILABLE:
            doc = docx.Document(io.BytesIO(file_bytes))
            raw_text = "\n".join(p.text for p in doc.paragraphs)
        else:
            raw_text = file_bytes.decode("utf-8", errors="ignore")

        raw_text = re.sub(r"\s+", " ", raw_text).strip()
        if not raw_text:
            raise ValueError(f"Could not extract readable text from {filename}")

        # Extract candidate name from first non-empty line or filename
        stem_name = Path(filename).stem.replace("_", " ").replace("-", " ").title()
        email_match = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", raw_text)
        phone_match = re.search(r"(?:\+?91[\-\s]?)?[6-9]\d{9}\b|\b\+?1?\d{10,14}\b", raw_text)
        exp_match = re.search(r"(\d+(?:\.\d+)?)\+?\s*(?:years?|yrs?)", raw_text, re.IGNORECASE)

        extracted_email = email_match.group(0) if email_match else "not_found@example.com"
        extracted_phone = phone_match.group(0) if phone_match else "0000000000"
        extracted_exp = float(exp_match.group(1)) if exp_match else 2.0
        extracted_skills = self.extract_skills_from_text(raw_text) or ["Python", "Linux"]

        cid = f"CV-{re.sub(r'[^A-Z0-9]', '', stem_name.upper())[:8]}-{int(datetime.now().timestamp()) % 10000}"
        return self.ingest_candidate(
            {
                "candidate_id": cid,
                "full_name": stem_name,
                "email": extracted_email,
                "phone": extracted_phone,
                "role_focus": role_override.strip() or f"{extracted_skills[0]} Specialist",
                "exp_years": extracted_exp,
                "skills": extracted_skills,
                "summary": raw_text[:3000],
            }
        )

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
        full_name = str(record.get("full_name") or record.get("name") or "Candidate")
        email = str(record.get("email") or "unknown@example.com").strip()
        phone = str(record.get("phone") or "0000000000").strip()
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
                (candidate_id, full_name, email_raw, phone_raw, email_masked, phone_masked, role_focus, exp_years, skills, raw_summary, redacted_summary, updated_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    full_name=excluded.full_name,
                    email_raw=excluded.email_raw,
                    phone_raw=excluded.phone_raw,
                    email_masked=excluded.email_masked,
                    phone_masked=excluded.phone_masked,
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
                    email,
                    phone,
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
            job_id = str(job["job_id"])
            job_title = str(job["title"])
            job_company = str(job["company"])
            job_location = str(job["location"])
            req_skills_raw = self._parse_skills_safe(job["required_skills"])
            req_skills = [s.lower() for s in req_skills_raw]
            min_exp = float(job["min_exp_years"] or 0.0)

            for cand in candidates:
                cand_id = str(cand["candidate_id"])
                cand_name = str(cand["full_name"])
                cand_role = str(cand["role_focus"])
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
                        f"Matched {len(matched)}/{len(req_skills)} required skills "
                        f"({', '.join(matched) if matched else 'none'}); {cand_exp}y exp."
                    )
                    model_used = "local-skill-scorer"

                now = datetime.now(timezone.utc).isoformat()
                with sqlite3.connect(self.db_path) as conn:
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

                if final_score >= 70:
                    draft = (
                        f"Hi {cand_name}, we reviewed your profile for {job_title} at {job_company} ({job_location}) "
                        f"and your technical skills matched {final_score}%. Are you available for a quick screening call?"
                    )
                    with sqlite3.connect(self.db_path) as conn:
                        existing_q = conn.execute(
                            "SELECT id FROM outreach_approval_queue WHERE candidate_id = ? AND job_id = ?",
                            (cand_id, job_id),
                        ).fetchone()
                    if not existing_q:
                        self.guard.queue_outreach_for_approval(
                            candidate_id=cand_id,
                            job_id=job_id,
                            channel="WhatsApp & Email",
                            recipient_masked=f"{cand['email_masked']} | {cand['phone_masked']}",
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
