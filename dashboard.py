import json
import sqlite3
from pathlib import Path
import pandas as pd
import streamlit as st

from recruit_agent import RecruitAgent
from security_guard import SecurityGuard

st.set_page_config(
    page_title="Secure Recruitment & AI Guardrail Console",
    layout="wide",
)

DB_PATH = "recruit_pipeline.db"
REPOS_FILE = Path("repos.json")
STATUS_FILE = Path("status.json")
SUGGESTIONS_FILE = Path("suggestions.json")
DIGEST_FILE = Path("digest.md")

agent = RecruitAgent(db_path=DB_PATH)
guard = SecurityGuard(db_path=DB_PATH)


def load_json_safe(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


st.title("Zero-Trust Recruitment Consultancy & Security Stack")

status_data = load_json_safe(STATUS_FILE, {})
col1, col2, col3, col4 = st.columns(4)
col1.metric("Linked GitHub Repos", status_data.get("repos_linked", 51))
col2.metric("Tier-1 Critical Controls", status_data.get("critical_repos_count", 21))
col3.metric("Scored Candidate Pairs", status_data.get("matches_scored", 0))
col4.metric("Automation Passes", status_data.get("total_runs", 1))

tab_pipeline, tab_outreach, tab_github, tab_security = st.tabs([
    "1. Candidate Scoring & DLP Intake",
    "2. Human Outreach Approval Queue",
    "3. Linked GitHub Security & AI Catalog",
    "4. Immutable Audit Logs & Live Guardrail Test",
])

with tab_pipeline:
    st.subheader("Candidate-Job Matching Pipeline (PII-Sanitized Inference)")
    if st.button("Run Sanitized Scoring Pass Now"):
        scored = agent.score_all_candidates()
        st.success(f"Scored {len(scored)} candidate-job pairs with Presidio + LLM-Guard active.")

    with sqlite3.connect(DB_PATH) as conn:
        matches_df = pd.read_sql_query(
            """
            SELECT m.candidate_id, c.full_name, c.role_focus, c.email_masked,
                   m.job_id, j.title AS job_title, m.score, m.matched_skills,
                   m.missing_skills, m.model_used, m.prompt_hash, m.scored_utc
            FROM candidate_matches m
            JOIN candidates c ON m.candidate_id = c.candidate_id
            JOIN jobs j ON m.job_id = j.job_id
            ORDER BY m.score DESC
            """,
            conn,
        )
    st.dataframe(matches_df, use_container_width=True)

    st.subheader("Ingest New Candidate Profile (Auto-Redacts PII Before DB & LLM)")
    with st.form("candidate_intake_form"):
        c_id = st.text_input("Candidate ID", value="CAND-003")
        c_name = st.text_input("Full Name", value="Sourav Candidate")
        c_email = st.text_input("Email (Masked at Storage)", value="candidate@example.com")
        c_phone = st.text_input("Phone (Masked at Storage)", value="+91-9800000000")
        c_role = st.text_input("Role Focus", value="AWS Cloud & DevOps Support Engineer")
        c_exp = st.number_input("Experience (Years)", min_value=0.0, max_value=30.0, value=2.5, step=0.5)
        c_skills = st.text_input("Skills (comma-separated)", value="AWS, Python, Docker, Linux, GitHub Actions, SQLite")
        c_summary = st.text_area(
            "Raw Resume Summary (PAN/Aadhaar/Email/Phone automatically stripped before LLM)",
            value="Cloud engineer in Kolkata. Email: candidate@example.com, Phone: +91-9800000000. Skilled in AWS, Python, and CI/CD.",
        )
        submitted = st.form_submit_button("Sanitize & Save Candidate")
        if submitted:
            try:
                saved_id = agent.ingest_candidate({
                    "candidate_id": c_id,
                    "full_name": c_name,
                    "email": c_email,
                    "phone": c_phone,
                    "role_focus": c_role,
                    "exp_years": c_exp,
                    "skills": c_skills,
                    "summary": c_summary,
                })
                agent.score_all_candidates()
                st.success(f"Candidate {saved_id} sanitized, stored, and scored.")
            except Exception as exc:
                st.error(f"Blocked by SecurityGuard: {exc}")

with tab_outreach:
    st.subheader("Human-in-the-Loop Outreach Gate (Meta WhatsApp / Email Policy Compliance)")
    st.caption("No external message is sent by an agent without explicit recruiter approval.")
    with sqlite3.connect(DB_PATH) as conn:
        queue_df = pd.read_sql_query(
            "SELECT * FROM outreach_approval_queue ORDER BY id DESC",
            conn,
        )
    st.dataframe(queue_df, use_container_width=True)

    col_a, col_b, col_c = st.columns(3)
    queue_id_input = col_a.number_input("Queue ID to Review", min_value=1, value=1, step=1)
    reviewer_name = col_b.text_input("Reviewer Identity", value="lead_recruiter")
    if col_c.button("Approve Selected Outreach"):
        status = guard.review_outreach(int(queue_id_input), reviewer_name, approve=True)
        st.success(f"Queue ID {queue_id_input} marked as {status}.")
    if col_c.button("Reject Selected Outreach"):
        status = guard.review_outreach(int(queue_id_input), reviewer_name, approve=False)
        st.warning(f"Queue ID {queue_id_input} marked as {status}.")

with tab_github:
    st.subheader("Verified GitHub Security & AI Architecture Registry")
    repos_data = load_json_safe(REPOS_FILE, {"categories": {}})
    rows = []
    for cat, items in repos_data.get("categories", {}).items():
        for item in items:
            rows.append({
                "Category": cat,
                "Repository": item["repo"],
                "GitHub URL": f"https://github.com/{item['repo']}",
                "Priority": item["priority"].upper(),
                "Tier": item["tier"],
                "Role in Stack": item["role"],
            })
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

    st.subheader("Automated Stack Suggestions")
    suggestions = load_json_safe(SUGGESTIONS_FILE, [])
    if suggestions:
        st.table(pd.DataFrame(suggestions))

    if DIGEST_FILE.exists():
        with st.expander("View Latest Generated Markdown Digest (digest.md)"):
            st.markdown(DIGEST_FILE.read_text(encoding="utf-8"))

with tab_security:
    st.subheader("Live DLP & Prompt-Injection Firewall Testbench")
    sample_input = st.text_area(
        "Test Candidate Input / Prompt Payload",
        value="Contact Rahul at rahul@example.com or +91-9876543210, PAN: ABCDE1234F. Experienced in Python and AWS.",
    )
    if st.button("Run SecurityGuard Sanitizer"):
        try:
            sanitized, meta = guard.scan_and_redact(sample_input, actor="dashboard_tester", target_id="manual_test")
            st.code(sanitized, language="text")
            st.json(meta)
        except Exception as exc:
            st.error(str(exc))

    st.subheader("Immutable SQLite Security Audit Log (security_audit_log)")
    with sqlite3.connect(DB_PATH) as conn:
        audit_df = pd.read_sql_query(
            "SELECT * FROM security_audit_log ORDER BY id DESC LIMIT 100",
            conn,
        )
    st.dataframe(audit_df, use_container_width=True)