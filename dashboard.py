import json
import os
import re
import sqlite3
import urllib.parse
from pathlib import Path

import pandas as pd
import streamlit as st

from recruit_agent import RecruitAgent
from security_guard import SecurityGuard

st.set_page_config(
    page_title="Recruitment Consultancy & Security Console",
    layout="wide",
)

DB_PATH = "recruit_pipeline.db"
REPOS_FILE = Path("repos.json")

agent = RecruitAgent(db_path=DB_PATH)
guard = SecurityGuard(db_path=DB_PATH)


def load_json_safe(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


# Sidebar: Live Controls, API Key Injection & Dummy Data Cleaner
with st.sidebar:
    st.header("System Controls")
    if st.button("Purge Sample / Dummy Data", type="primary", use_container_width=True):
        removed = agent.purge_dummy_data()
        st.success(f"Removed {removed} sample rows! Database now holds only your real jobs & CVs.")
        st.rerun()

    st.divider()
    st.subheader("Optional Live AI Scoring Key")
    st.caption("Leave blank to use the built-in local skill & experience scorer, or paste a key for LLM scoring.")
    groq_key = st.text_input("Groq API Key (gsk_...)", value=os.environ.get("GROQ_API_KEY", ""), type="password")
    deepseek_key = st.text_input("DeepSeek API Key (sk-...)", value=os.environ.get("DEEPSEEK_API_KEY", ""), type="password")
    if groq_key:
        os.environ["GROQ_API_KEY"] = groq_key.strip()
    if deepseek_key:
        os.environ["DEEPSEEK_API_KEY"] = deepseek_key.strip()

    active_mode = "Live LLM Gateway" if (os.environ.get("GROQ_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")) else "Local Deterministic Skill Scorer"
    st.info(f"Active Scoring Engine: **{active_mode}**")

st.title("Recruitment Consultancy Operations & Zero-Trust Console")

with sqlite3.connect(DB_PATH) as conn:
    total_jobs = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='OPEN'").fetchone()[0]
    total_cands = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    total_matches = conn.execute("SELECT COUNT(*) FROM candidate_matches").fetchone()[0]
    pending_outreach = conn.execute("SELECT COUNT(*) FROM outreach_approval_queue WHERE status='PENDING_APPROVAL'").fetchone()[0]

m1, m2, m3, m4 = st.columns(4)
m1.metric("Active Job Openings", total_jobs)
m2.metric("Candidates in Database", total_cands)
m3.metric("Scored Candidate-Job Pairs", total_matches)
m4.metric("Pending Outreach Approvals", pending_outreach)

tab_jobs, tab_candidates, tab_outreach, tab_security = st.tabs([
    "1. Live Jobs & Client Openings",
    "2. Upload Resumes (PDF/DOCX) & Match Shortlist",
    "3. WhatsApp / Email Outreach Center",
    "4. Security Shield & 59-Repo Self-Healing Registry",
])

# TAB 1: REAL JOBS INTAKE (LIVE WEB SCRAPER + CLIENT JOB FORM)
with tab_jobs:
    col_scrape, col_manual = st.columns(2)

    with col_scrape:
        st.subheader("Pull Real Live Jobs from Public Job Feeds")
        search_kw = st.text_input("Job Search Keyword", value="devops")
        max_pull = st.slider("Number of Live Jobs to Fetch", min_value=3, max_value=25, value=8)
        if st.button("Fetch Live Jobs Now", use_container_width=True):
            with st.spinner(f"Fetching live '{search_kw}' openings from Remotive public feed..."):
                added_count = agent.fetch_live_jobs_from_web(search_term=search_kw, max_jobs=max_pull)
                agent.score_all_candidates()
            st.success(f"Imported {added_count} live jobs and re-scored all candidates!")
            st.rerun()

    with col_manual:
        st.subheader("Post a New Consultancy Client Job")
        with st.form("add_client_job_form"):
            j_title = st.text_input("Job Title", placeholder="e.g., AWS Cloud Support Engineer")
            j_company = st.text_input("Client Company Name", placeholder="e.g., Apex Cloud India")
            j_loc = st.text_input("Location", value="Kolkata / Remote India")
            j_skills = st.text_input("Required Skills (comma-separated)", value="AWS, Python, Linux, Docker, SQL, GitHub Actions")
            j_exp = st.number_input("Minimum Experience (Years)", min_value=0.0, max_value=25.0, value=2.0, step=0.5)
            if st.form_submit_button("Save Client Job & Score Candidates", use_container_width=True):
                if j_title.strip():
                    jid = agent.add_job(
                        title=j_title,
                        company=j_company or "Direct Client",
                        location=j_loc,
                        required_skills=[s.strip() for s in j_skills.split(",") if s.strip()],
                        min_exp_years=j_exp,
                    )
                    agent.score_all_candidates()
                    st.success(f"Added {jid} ({j_title}) and scored candidates!")
                    st.rerun()
                else:
                    st.warning("Please enter a Job Title.")

    st.subheader("All Active Job Openings")
    with sqlite3.connect(DB_PATH) as conn:
        jobs_df = pd.read_sql_query(
            "SELECT job_id, title, company, location, required_skills, min_exp_years, source_url, created_utc FROM jobs ORDER BY created_utc DESC",
            conn,
        )
    st.dataframe(jobs_df, use_container_width=True)

# TAB 2: MULTI-FILE RESUME UPLOADER (.PDF / .DOCX / .TXT) & RANKED SHORTLIST
with tab_candidates:
    col_up, col_form = st.columns(2)

    with col_up:
        st.subheader("Upload Candidate Resumes (PDF, DOCX, TXT)")
        uploaded_files = st.file_uploader(
            "Drop 1 or more CV files here — automatically extracts contact details & skills and redacts PII before AI scoring",
            type=["pdf", "docx", "txt"],
            accept_multiple_files=True,
        )
        role_hint = st.text_input("Target Role Tag (Optional)", placeholder="e.g., Cloud / DevOps Engineer")
        if st.button("Parse, Sanitize & Score Uploaded Resumes", use_container_width=True):
            if uploaded_files:
                ingested_ids = []
                for uf in uploaded_files:
                    try:
                        cid = agent.parse_and_ingest_resume_file(uf.name, uf.read(), role_override=role_hint)
                        ingested_ids.append(cid)
                    except Exception as exc:
                        st.error(f"{uf.name}: {exc}")
                if ingested_ids:
                    agent.score_all_candidates()
                    st.success(f"Parsed and scored {len(ingested_ids)} resume(s): {', '.join(ingested_ids)}")
                    st.rerun()
            else:
                st.warning("Select at least one PDF, DOCX, or TXT resume file first.")

    with col_form:
        st.subheader("Or Add a Candidate Manually")
        with st.form("manual_candidate_form"):
            c_name = st.text_input("Candidate Full Name", placeholder="e.g., Rahul Sharma")
            c_email = st.text_input("Email Address", placeholder="rahul@example.com")
            c_phone = st.text_input("WhatsApp / Mobile Number", placeholder="+919876543210")
            c_role = st.text_input("Primary Role", value="Cloud & DevOps Engineer")
            c_exp = st.number_input("Experience (Years)", min_value=0.0, max_value=30.0, value=3.0, step=0.5)
            c_skills = st.text_input("Skills (comma-separated)", value="AWS, Python, Linux, Docker, Terraform, SQLite")
            c_sum = st.text_area("Resume Summary / Notes", value="Hands-on experience with AWS infrastructure, Python automation, and CI/CD.")
            if st.form_submit_button("Sanitize, Save & Score Candidate", use_container_width=True):
                if c_name.strip():
                    cid = agent.ingest_candidate({
                        "full_name": c_name,
                        "email": c_email,
                        "phone": c_phone,
                        "role_focus": c_role,
                        "exp_years": c_exp,
                        "skills": c_skills,
                        "summary": c_sum,
                    })
                    agent.score_all_candidates()
                    st.success(f"Added and scored {c_name} ({cid})!")
                    st.rerun()
                else:
                    st.warning("Please enter the candidate's name.")

    st.divider()
    st.subheader("Ranked Candidate-to-Job Shortlist")
    with sqlite3.connect(DB_PATH) as conn:
        matches_df = pd.read_sql_query(
            """
            SELECT m.score AS match_percent, c.full_name, c.role_focus, c.exp_years,
                   j.title AS job_title, j.company, j.location,
                   m.matched_skills, m.missing_skills, m.reasoning,
                   c.email_masked, c.phone_masked, m.model_used, m.candidate_id, m.job_id
            FROM candidate_matches m
            JOIN candidates c ON m.candidate_id = c.candidate_id
            JOIN jobs j ON m.job_id = j.job_id
            ORDER BY m.score DESC
            """,
            conn,
        )
    if not matches_df.empty:
        csv_bytes = matches_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="Download Client Shortlist Report (CSV)",
            data=csv_bytes,
            file_name="client_candidate_shortlist.csv",
            mime="text/csv",
        )
    st.dataframe(matches_df, use_container_width=True)

# TAB 3: REAL WHATSAPP & EMAIL OUTREACH LAUNCHER
with tab_outreach:
    st.subheader("Human-Approved Candidate Outreach Center")
    st.caption("Approve high-scoring matches below to unlock one-click WhatsApp Web and Email launch buttons.")
    with sqlite3.connect(DB_PATH) as conn:
        outreach_rows = conn.execute(
            """
            SELECT q.id, q.status, q.candidate_id, c.full_name, c.email_raw, c.phone_raw,
                   q.job_id, j.title AS job_title, j.company, q.draft_message, q.reviewed_by
            FROM outreach_approval_queue q
            LEFT JOIN candidates c ON q.candidate_id = c.candidate_id
            LEFT JOIN jobs j ON q.job_id = j.job_id
            ORDER BY q.id DESC
            """
        ).fetchall()

    if not outreach_rows:
        st.info("No outreach messages queued yet. Upload a resume or score candidates with >= 70% match to populate this queue.")
    else:
        for row in outreach_rows:
            qid, qstatus, cid, cname, eraw, praw, jid, jtitle, jcomp, msg, rev = row
            with st.expander(f"#{qid} [{qstatus}] {cname or cid} ➔ {jtitle or jid} ({jcomp or ''})", expanded=(qstatus == "PENDING_APPROVAL")):
                st.write(f"**Draft Message:** {msg}")
                b1, b2, b3, b4 = st.columns(4)
                if b1.button(f"Approve #{qid}", key=f"app_{qid}"):
                    guard.review_outreach(qid, "lead_recruiter", approve=True)
                    st.rerun()
                if b2.button(f"Reject #{qid}", key=f"rej_{qid}"):
                    guard.review_outreach(qid, "lead_recruiter", approve=False)
                    st.rerun()

                if qstatus == "APPROVED_SENT":
                    clean_digits = re.sub(r"\D", "", str(praw or ""))
                    if len(clean_digits) == 10:
                        clean_digits = "91" + clean_digits
                    wa_url = f"https://wa.me/{clean_digits}?text={urllib.parse.quote(msg)}"
                    mail_subject = urllib.parse.quote(f"Interview Opportunity: {jtitle} at {jcomp}")
                    mail_body = urllib.parse.quote(msg)
                    mail_url = f"mailto:{eraw or ''}?subject={mail_subject}&body={mail_body}"

                    b3.link_button("Send on WhatsApp", wa_url)
                    b4.link_button("Send via Email", mail_url)

# TAB 4: SECURITY SHIELD & SELF-HEALING 59-REPO REGISTRY
with tab_security:
    st.subheader("Live PII Redaction & Prompt-Injection Firewall Test")
    test_text = st.text_area(
        "Paste any raw CV snippet to verify what the AI model actually receives:",
        value="Sourav Candidate | Email: sourav@example.com | Phone: +91-9876543210 | PAN: ABCDE1234F | Skills: AWS, Python, Docker",
    )
    if st.button("Test SecurityGuard Redaction"):
        try:
            clean_out, meta_out = guard.scan_and_redact(test_text, actor="recruiter_test", target_id="manual")
            st.code(clean_out, language="text")
            st.json(meta_out)
        except Exception as exc:
            st.error(str(exc))

    st.subheader("Immutable SQLite Security Audit Log")
    with sqlite3.connect(DB_PATH) as conn:
        audit_df = pd.read_sql_query("SELECT * FROM security_audit_log ORDER BY id DESC LIMIT 50", conn)
    st.dataframe(audit_df, use_container_width=True)

    st.subheader("Self-Healing 59-Repository Security & AI Registry (repos.json)")
    repos_data = load_json_safe(REPOS_FILE, {"categories": {}})
    repo_rows = []
    for cat, items in repos_data.get("categories", {}).items():
        for item in items:
            repo_rows.append({
                "Category": cat,
                "Repository": item["repo"],
                "Relevancy Score": item.get("relevancy_score", 95.0),
                "Priority": item.get("priority", "high").upper(),
                "Role": item.get("role", ""),
            })
    if repo_rows:
        st.dataframe(pd.DataFrame(repo_rows), use_container_width=True)
