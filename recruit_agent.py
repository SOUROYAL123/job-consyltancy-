"""
Operational Recruitment Consultancy Agent
Ingests live job feeds, matches candidate profiles against openings,
and logs shortlisted matches into a local SQLite database (recruit_pipeline.db).
"""
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import requests

ROOT = Path(__file__).parent
DB_PATH = ROOT / "recruit_pipeline.db"
CANDIDATES_FILE = ROOT / "candidates.json"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")

SAMPLE_CANDIDATES = [
    {
        "id": "CAND-001",
        "name": "Aarav Sharma",
        "role": "Cloud / DevOps Engineer",
        "skills": ["aws", "terraform", "docker", "kubernetes", "python", "linux", "ci/cd"],
        "experience_years": 3,
        "summary": "AWS Certified Cloud Support & DevOps Engineer experienced in Terraform, Docker, Linux, and Python automation."
    },
    {
        "id": "CAND-002",
        "name": "Priya Nair",
        "role": "Full Stack Python Developer",
        "skills": ["python", "fastapi", "react", "postgresql", "docker", "rest api"],
        "experience_years": 4,
        "summary": "Backend-focused Full Stack Engineer building FastAPI microservices, PostgreSQL data pipelines, and React dashboards."
    }
]

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            url TEXT,
            tags TEXT,
            description TEXT,
            fetched_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id TEXT,
            candidate_name TEXT,
            job_id TEXT,
            job_title TEXT,
            company TEXT,
            score INTEGER,
            rationale TEXT,
            url TEXT,
            created_at TEXT,
            UNIQUE(candidate_id, job_id)
        )
    """)
    conn.commit()
    return conn

def load_candidates():
    if not CANDIDATES_FILE.exists():
        CANDIDATES_FILE.write_text(json.dumps(SAMPLE_CANDIDATES, indent=2), encoding="utf-8")
    return json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))

def fetch_jobs(limit=25):
    jobs = []
    try:
        r = requests.get("https://remotive.com/api/remote-jobs?limit=25", timeout=15)
        if r.status_code == 200:
            for item in r.json().get("jobs", [])[:limit]:
                clean_desc = re.sub(r"<[^>]+>", " ", item.get("description", ""))
                jobs.append({
                    "job_id": f"remotive-{item['id']}",
                    "title": item.get("title", ""),
                    "company": item.get("company_name", ""),
                    "url": item.get("url", ""),
                    "tags": ", ".join(item.get("tags", [])),
                    "description": re.sub(r"\s+", " ", clean_desc).strip()[:1500],
                })
    except requests.RequestException as e:
        print(f"[warn] Remotive feed error: {e}")
    return jobs

def score_match(candidate, job):
    text = f"{job['title']} {job['tags']} {job['description']}".lower()
    matched = [s for s in candidate["skills"] if s.lower() in text]
    base_score = min(100, int((len(matched) / max(1, len(candidate["skills"]))) * 100))

    if base_score < 30:
        return base_score, f"Keyword overlap ({len(matched)}/{len(candidate['skills'])}): {', '.join(matched)}"

    prompt = (
        f"Rate candidate fit from 0 to 100 and give a 1-sentence reason.\n"
        f"Candidate: {candidate['role']} | Skills: {', '.join(candidate['skills'])} | {candidate['summary']}\n"
        f"Job: {job['title']} at {job['company']} | Tags: {job['tags']}\n"
        f"Description snippet: {job['description'][:500]}\n"
        f"Respond strictly in JSON: {{\"score\": <int>, \"reason\": \"<text>\"}}"
    )

    if LLM_API_KEY:
        try:
            resp = requests.post(
                LLM_BASE_URL,
                headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
                json={"model": LLM_MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1},
                timeout=15,
            )
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"]
                m = re.search(r"\{.*\}", content, re.DOTALL)
                if m:
                    data = json.loads(m.group(0))
                    return int(data.get("score", base_score)), data.get("reason", "Evaluated via LLM gateway")
        except Exception:
            pass

    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=10,
        )
        if resp.status_code == 200:
            content = resp.json().get("response", "")
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
                return int(data.get("score", base_score)), data.get("reason", "Evaluated via local Ollama")
    except Exception:
        pass

    return base_score, f"Matched skills: {', '.join(matched)}"

def main():
    conn = init_db()
    candidates = load_candidates()
    jobs = fetch_jobs(limit=25)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for j in jobs:
        conn.execute(
            "INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (j["job_id"], j["title"], j["company"], j["url"], j["tags"], j["description"], now),
        )

    shortlisted = 0
    for c in candidates:
        for j in jobs:
            score, reason = score_match(c, j)
            if score >= 40:
                conn.execute(
                    """INSERT OR IGNORE INTO matches
                       (candidate_id, candidate_name, job_id, job_title, company, score, rationale, url, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (c["id"], c["name"], j["job_id"], j["title"], j["company"], score, reason, j["url"], now),
                )
                shortlisted += 1

    conn.commit()
    conn.close()
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} Processed {len(jobs)} jobs across {len(candidates)} candidates. "
          f"Shortlisted {shortlisted} matches (>=40%) in {DB_PATH.name}.")

if __name__ == "__main__":
    main()
