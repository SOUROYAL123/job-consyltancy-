import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

try:
    from presidio_analyzer import AnalyzerEngine
    from presidio_anonymizer import AnonymizerEngine
    PRESIDIO_AVAILABLE = True
except ImportError:
    PRESIDIO_AVAILABLE = False

try:
    from llm_guard.input_scanners import PromptInjection, Secrets
    LLM_GUARD_AVAILABLE = True
except ImportError:
    LLM_GUARD_AVAILABLE = False

ALLOWED_LLM_DOMAINS = {
    "api.deepseek.com",
    "api.groq.com",
    "openrouter.ai",
    "generativelanguage.googleapis.com",
    "localhost",
    "127.0.0.1",
}

INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"system\s+prompt|developer\s+mode|jailbreak|do\s+anything\s+now", re.IGNORECASE),
    re.compile(r"(print|reveal|leak|output|send)\s+(all\s+)?(api\s*key|secret|token|password|env)", re.IGNORECASE),
    re.compile(r"<script.*?>|javascript:|data:text/html", re.IGNORECASE),
]

SECRET_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"gsk_[a-zA-Z0-9]{20,}"),
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),
    re.compile(r"-----BEGIN (RSA|OPENSSH|EC|PGP) PRIVATE KEY-----"),
]

REGEX_PII_PATTERNS = {
    "<EMAIL_REDACTED>": re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"),
    "<PHONE_REDACTED>": re.compile(r"(?:\+?91[\-\s]?)?[6-9]\d{9}\b|\b\+?1?\d{10,14}\b"),
    "<IN_PAN_REDACTED>": re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]{1}\b"),
    "<IN_AADHAAR_REDACTED>": re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b"),
}


class SecurityGuard:
    def __init__(self, db_path: str = "recruit_pipeline.db", max_tokens: int = 3500) -> None:
        self.db_path = db_path
        self.max_tokens = max_tokens
        self._init_security_tables()
        if PRESIDIO_AVAILABLE:
            try:
                self.analyzer = AnalyzerEngine()
                self.anonymizer = AnonymizerEngine()
            except Exception:
                self.analyzer = None
                self.anonymizer = None
        else:
            self.analyzer = None
            self.anonymizer = None

        if LLM_GUARD_AVAILABLE:
            try:
                self.injection_scanner = PromptInjection()
                self.secrets_scanner = Secrets()
            except Exception:
                self.injection_scanner = None
                self.secrets_scanner = None
        else:
            self.injection_scanner = None
            self.secrets_scanner = None

    def _init_security_tables(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS security_audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_utc TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    prompt_hash TEXT,
                    status TEXT NOT NULL,
                    details TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outreach_approval_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_utc TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    recipient_masked TEXT NOT NULL,
                    draft_message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING_APPROVAL',
                    reviewed_by TEXT,
                    reviewed_utc TEXT
                )
                """
            )
            conn.commit()

    def validate_egress_url(self, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("https", "http"):
            raise PermissionError(f"Blocked insecure scheme in URL: {url}")
        if host not in ALLOWED_LLM_DOMAINS:
            self.log_event("system", "EGRESS_BLOCKED", host, "BLOCKED", f"Unauthorized domain: {url}")
            raise PermissionError(f"Egress blocked: '{host}' is not in ALLOWED_LLM_DOMAINS.")
        return True

    def scan_and_redact(
        self, raw_text: str, actor: str = "recruit_agent", target_id: str = "cv_payload"
    ) -> tuple[str, dict[str, Any]]:
        if not raw_text:
            return "", {"tokens_estimated": 0, "redactions": 0, "prompt_hash": "empty", "status": "EMPTY"}

        for pattern in SECRET_PATTERNS:
            if pattern.search(raw_text):
                self.log_event(actor, "SECRET_DETECTED", target_id, "BLOCKED", "Raw API key pattern found in input")
                raise ValueError("SecurityGuard Blocked: Raw API key or credential detected in payload.")

        if self.secrets_scanner is not None:
            _, is_valid_secret, _ = self.secrets_scanner.scan(raw_text)
            if not is_valid_secret:
                self.log_event(actor, "LLM_GUARD_SECRET", target_id, "BLOCKED", "llm-guard Secrets scanner triggered")
                raise ValueError("SecurityGuard Blocked: Secret detected by llm-guard.")

        for pattern in INJECTION_PATTERNS:
            if pattern.search(raw_text):
                self.log_event(actor, "PROMPT_INJECTION", target_id, "BLOCKED", "Regex injection signature matched")
                raise ValueError("SecurityGuard Blocked: Prompt injection attempt detected in candidate text.")

        if self.injection_scanner is not None:
            _, is_valid_prompt, risk_score = self.injection_scanner.scan(raw_text)
            if not is_valid_prompt:
                self.log_event(actor, "LLM_GUARD_INJECTION", target_id, "BLOCKED", f"Score: {risk_score}")
                raise ValueError(f"SecurityGuard Blocked: Prompt injection risk score {risk_score:.2f}")

        redacted_text = raw_text
        redaction_count = 0

        if self.analyzer is not None and self.anonymizer is not None:
            try:
                results = self.analyzer.analyze(
                    text=redacted_text,
                    entities=["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "LOCATION"],
                    language="en",
                )
                redaction_count += len(results)
                redacted_text = self.anonymizer.anonymize(text=redacted_text, analyzer_results=results).text
            except Exception:
                pass

        for placeholder, regex in REGEX_PII_PATTERNS.items():
            matches = regex.findall(redacted_text)
            if matches:
                redaction_count += len(matches)
                redacted_text = regex.sub(placeholder, redacted_text)

        max_chars = self.max_tokens * 4
        if len(redacted_text) > max_chars:
            redacted_text = redacted_text[:max_chars] + "\n...[TRUNCATED_BY_TOKEN_GUARD]"

        prompt_hash = hashlib.sha256(redacted_text.encode("utf-8")).hexdigest()[:16]
        meta = {
            "tokens_estimated": len(redacted_text) // 4,
            "redactions": redaction_count,
            "prompt_hash": prompt_hash,
            "status": "SANITIZED",
        }
        self.log_event(
            actor=actor,
            action="PII_SANITIZE_AND_SCAN",
            target_id=target_id,
            status="ALLOWED",
            details=json.dumps(meta),
            prompt_hash=prompt_hash,
        )
        return redacted_text, meta

    def queue_outreach_for_approval(
        self, candidate_id: str, job_id: str, channel: str, recipient_masked: str, draft_message: str
    ) -> int:
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO outreach_approval_queue
                (created_utc, candidate_id, job_id, channel, recipient_masked, draft_message, status)
                VALUES (?, ?, ?, ?, ?, ?, 'PENDING_APPROVAL')
                """,
                (now, candidate_id, job_id, channel, recipient_masked, draft_message),
            )
            conn.commit()
            row_id = int(cur.lastrowid)
        self.log_event("recruit_agent", "OUTREACH_QUEUED", f"candidate:{candidate_id}", "PENDING_APPROVAL", f"Queue ID: {row_id}")
        return row_id

    def review_outreach(self, queue_id: int, reviewer: str, approve: bool) -> str:
        new_status = "APPROVED_SENT" if approve else "REJECTED"
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE outreach_approval_queue
                SET status = ?, reviewed_by = ?, reviewed_utc = ?
                WHERE id = ?
                """,
                (new_status, reviewer, now, queue_id),
            )
            conn.commit()
        self.log_event(reviewer, f"OUTREACH_{new_status}", f"queue_id:{queue_id}", new_status, f"Reviewed by {reviewer}")
        return new_status

    def log_event(
        self, actor: str, action: str, target_id: str, status: str, details: str = "", prompt_hash: str = ""
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO security_audit_log
                (timestamp_utc, actor, action, target_id, prompt_hash, status, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (now, actor, action, target_id, prompt_hash, status, details),
            )
            conn.commit()
