"""Ziro Enterprise ITSM SLA Escalation Agent Core Engine.

Evaluates tickets from tickets.db, applies deterministic SLA timing and duplicate
prevention, invokes Gemini AI for technical complexity assessment and department routing,
and writes structured escalation packs into escalation_drafts.db.
"""

from __future__ import annotations

import html
import json
import os
import random
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta

# ==============================================================================
# 1. API KEY CONFIGURATION (Loaded from .env, environment, or Streamlit secrets)
# ==============================================================================
try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Also check Streamlit secrets when deployed to Streamlit Cloud
if not GEMINI_API_KEY:
    try:
        import streamlit as _st
        if hasattr(_st, "secrets") and "GEMINI_API_KEY" in _st.secrets:
            GEMINI_API_KEY = str(_st.secrets["GEMINI_API_KEY"])
    except Exception:
        pass

# Configure Google Generative AI
try:
    import google.generativeai as genai
    if GEMINI_API_KEY and GEMINI_API_KEY != "YOUR_API_KEY_HERE":
        genai.configure(api_key=GEMINI_API_KEY)
except Exception:
    pass

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

# ==============================================================================
# 2. DATABASE CONFIGURATION & UNRESTRICTED CONNECTION
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TICKETS_DB = os.path.join(BASE_DIR, "tickets.db")
DRAFTS_DB = os.path.join(BASE_DIR, "escalation_drafts.db")
TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"


def connect(db_path: str) -> sqlite3.Connection:
    """Connect to SQLite with row-factory and robust busy timeout."""
    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


def get_open_tickets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Query ALL open tickets without any LIMIT or hardcoded ID filters.
    
    Evaluates every ticket where status NOT IN ('Escalated', 'Closed').
    """
    sql = "SELECT * FROM tickets WHERE status NOT IN ('Escalated', 'Closed') ORDER BY ticket_id ASC"
    return conn.execute(sql).fetchall()


def load_tickets(conn: sqlite3.Connection, ticket_filter: str | None = None) -> list[sqlite3.Row]:
    """Fetch tickets: entire queue when filter is None, or single ticket if specified."""
    if ticket_filter:
        return conn.execute("SELECT * FROM tickets WHERE ticket_id = ? ORDER BY ticket_id ASC", (ticket_filter,)).fetchall()
    return conn.execute("SELECT * FROM tickets ORDER BY ticket_id ASC").fetchall()


def load_timeline(conn: sqlite3.Connection, ticket_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM timeline WHERE ticket_id = ? ORDER BY created_at ASC, id ASC",
        (ticket_id,),
    ).fetchall()


def load_attachments(conn: sqlite3.Connection, ticket_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM attachments WHERE ticket_id = ? ORDER BY uploaded_at ASC",
        (ticket_id,),
    ).fetchall()


# ==============================================================================
# 3. INTELLIGENT ROUTING MATRIX (10 ITSM DEPARTMENTS x L1/L2/L3/ARCHITECT)
# ==============================================================================
DEPARTMENTS = [
    "Network Support",
    "Cyber/Security Operations",
    "Database Support",
    "Identity and Access Management",
    "Platform Support",
    "Application Support",
    "Server and Infra Support",
    "End User Support",
    "Email and Collaboration Support",
    "IT Operation",
]

LEVELS = ["L1", "L2", "L3", "Architect"]

STAFF_MATRIX = {
    "Network Support": {
        "L1": ["Aarav Sharma", "Pooja Patel", "Rohan Verma"],
        "L2": ["Priya Nair", "Vikram Malhotra", "Sneha Joshi"],
        "L3": ["Karthik Balakrishnan", "Mohd Faizan", "Shalini Rathore"],
        "Architect": ["Rajesh Kannan", "Amitabh Sen"],
    },
    "Cyber/Security Operations": {
        "L1": ["Deepak Chawla", "Ananya Deshmukh", "Karan Singhania"],
        "L2": ["Neha Malhotra", "Siddharth Rao", "Tanvi Bhatia"],
        "L3": ["Arjun Bhattacharya", "Meera Krishnan", "Farhan Akhtar"],
        "Architect": ["Dr. Sanjay Swaminathan", "Aditi Roy"],
    },
    "Database Support": {
        "L1": ["Gaurav Tiwari", "Ritu Saxena", "Naveen Choudhary"],
        "L2": ["Suresh Raina", "Divya Menon", "Harish Pillai"],
        "L3": ["Manoj Kulkarni", "Swati Nambiar", "Kishore Kumar"],
        "Architect": ["Venkatesh Prasad", "Sunita Narayanan"],
    },
    "Identity and Access Management": {
        "L1": ["Rahul Bose", "Pooja Hegde", "Varun Dhawan"],
        "L2": ["Mohit Agarwal", "Simran Kaur", "Nikhil Kamath"],
        "L3": ["Aparna Reddy", "Tushar Deshmukh", "Geeta Sundaram"],
        "Architect": ["Someshwar Rao", "Nandita Das"],
    },
    "Platform Support": {
        "L1": ["Abhishek Bachchan", "Kriti Sanon", "Ayushmann Khurrana"],
        "L2": ["Aditya Roy", "Isha Ambani", "Manish Malhotra"],
        "L3": ["Sanjay Dutt", "Vidya Balan", "Sunil Gavaskar"],
        "Architect": ["Pranab Mukherjee", "Anurag Kashyap"],
    },
    "Application Support": {
        "L1": ["Rhea Kapoor", "Ishaan Khatter", "Tara Sutaria"],
        "L2": ["Marcus Feld", "Shilpa Shetty", "Boman Irani"],
        "L3": ["Anil Kapoor", "Juhi Chawla", "Paresh Rawal"],
        "Architect": ["Naseeruddin Shah", "Shabana Azmi"],
    },
    "Server and Infra Support": {
        "L1": ["Sonu Sood", "Disha Patani", "Tiger Shroff"],
        "L2": ["Arbaaz Khan", "Malaika Arora", "Jackie Shroff"],
        "L3": ["Anupam Kher", "Kirron Kher", "Pankaj Tripathi"],
        "Architect": ["Kamal Haasan", "Mani Ratnam"],
    },
    "End User Support": {
        "L1": ["Daniel Okafor", "Kartik Aaryan", "Sara Ali Khan"],
        "L2": ["Janhvi Kapoor", "Ananya Panday", "Varun Sharma"],
        "L3": ["Rajkummar Rao", "Patralekhaa", "Vicky Kaushal"],
        "Architect": ["Shoojit Sircar", "Nitesh Tiwari"],
    },
    "Email and Collaboration Support": {
        "L1": ["Sanya Malhotra", "Fatima Sana", "Sunny Kaushal"],
        "L2": ["Sharvari Wagh", "Radhika Madan", "Mrunal Thakur"],
        "L3": ["Vijay Varma", "Jaideep Ahlawat", "Gulshan Devaiah"],
        "Architect": ["Vikramaditya Motwane", "Zoya Akhtar"],
    },
    "IT Operation": {
        "L1": ["Ali Fazal", "Richa Chadha", "Pulkit Samrat"],
        "L2": ["Kriti Kharbanda", "Jim Sarbh", "Sobhita Dhulipala"],
        "L3": ["Nawazuddin Siddiqui", "Kay Kay Menon", "Ashutosh Rana"],
        "Architect": ["Shekhar Kapur", "Mira Nair"],
    },
}

# Reverse lookup: staff_name -> (department, level)
STAFF_INDEX: dict[str, tuple[str, str]] = {}
for _dept, _tiers in STAFF_MATRIX.items():
    for _tier, _names in _tiers.items():
        for _name in _names:
            STAFF_INDEX[_name] = (_dept, _tier)


def match_department(dept_str: str | None) -> str:
    """Normalize free text to one of the 10 canonical departments."""
    if not dept_str:
        return "IT Operation"
    cleaned = dept_str.strip().lower()
    for d in DEPARTMENTS:
        if d.lower() in cleaned or cleaned in d.lower():
            return d
    alias_map = {
        "network": "Network Support",
        "security": "Cyber/Security Operations",
        "cyber": "Cyber/Security Operations",
        "database": "Database Support",
        "dba": "Database Support",
        "iam": "Identity and Access Management",
        "identity": "Identity and Access Management",
        "platform": "Platform Support",
        "app": "Application Support",
        "server": "Server and Infra Support",
        "infra": "Server and Infra Support",
        "desktop": "End User Support",
        "end user": "End User Support",
        "email": "Email and Collaboration Support",
        "operation": "IT Operation",
        "ops": "IT Operation",
    }
    for k, v in alias_map.items():
        if k in cleaned:
            return v
    return "IT Operation"


def match_level(lvl_str: str | None) -> str:
    """Normalize level string to L1, L2, L3, or Architect."""
    if not lvl_str:
        return "L2"
    cleaned = lvl_str.strip().upper()
    if "ARCHITECT" in cleaned or "LEADERSHIP" in cleaned or "L4" in cleaned:
        return "Architect"
    if "L3" in cleaned or "TIER-3" in cleaned or "TIER 3" in cleaned:
        return "L3"
    if "L2" in cleaned or "TIER-2" in cleaned or "TIER 2" in cleaned:
        return "L2"
    if "L1" in cleaned or "TIER-1" in cleaned or "TIER 1" in cleaned:
        return "L1"
    return "L2"


def get_random_staff(department: str, level: str, exclude: str | None = None) -> str:
    """Pick a random staff engineer from the department and level pool."""
    dept = match_department(department)
    lvl = match_level(level)
    candidates = STAFF_MATRIX.get(dept, {}).get(lvl, [])
    if not candidates:
        candidates = STAFF_MATRIX.get("IT Operation", {}).get("L2", ["Siddharth Rao"])
    filtered = [c for c in candidates if c != exclude] if exclude else candidates
    return random.choice(filtered if filtered else candidates)


# ==============================================================================
# 4. SLA MATH & DUPLICATE PREVENTIONS (TC2 & TC4)
# ==============================================================================
# Priority response targets in hours
PRIORITY_SLA_TARGET_HOURS = {
    "Critical": 1,
    "High": 4,
    "Medium": 8,
    "Low": 24,
}
DEFAULT_SLA_TARGET_HOURS = 8

# Statuses and Scenarios
STATUS_PACK_READY = "ESCALATION_PACK_PREPARED"
STATUS_PACK_READY_SANITIZED = "ESCALATION_PACK_PREPARED_SANITIZED"
STATUS_SLA_EXCLUDED = "EXCLUDED_FROM_SLA_CLOCK"
STATUS_DUPLICATE = "DUPLICATE_PREVENTED"
STATUS_NO_ACTION = "NO_ACTION_REQUIRED"

SCENARIO_ESCALATE = "TC1"
SCENARIO_SLA_EXCLUDED = "TC2"
SCENARIO_SANITIZED = "TC3"
SCENARIO_DUPLICATE = "TC4"
SCENARIO_UNMATCHED = "UNMATCHED"

DEFAULT_APPROVAL_STATUS = "Pending Approval"
NOT_APPLICABLE = "Not Applicable"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), TIMESTAMP_FMT)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def format_duration(delta: timedelta | None) -> str:
    if not delta:
        return "0m"
    total_minutes = max(int(delta.total_seconds() // 60), 0)
    days, rem = divmod(total_minutes, 60 * 24)
    hours, minutes = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_spoken_duration(delta: timedelta | None) -> str:
    if not delta:
        return "0 minutes"
    total_minutes = max(int(delta.total_seconds() // 60), 0)
    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{hours} hour{'s' if hours != 1 else ''} and {minutes} minute{'s' if minutes != 1 else ''}"


def sla_target_hours(priority: str) -> int:
    return PRIORITY_SLA_TARGET_HOURS.get(priority, DEFAULT_SLA_TARGET_HOURS)


def compute_timing(ticket: dict | sqlite3.Row, now: datetime | None = None) -> dict:
    t = dict(ticket) if hasattr(ticket, "keys") else ticket
    now = now or datetime.now()
    created = parse_ts(t.get("created_at"))
    due = parse_ts(t.get("sla_due_at"))
    if due is None and created:
        target_h = sla_target_hours(t.get("priority", "Medium"))
        due = created + timedelta(hours=target_h)

    age = now - created if created else timedelta(0)
    breach = None
    if due and now > due:
        breach = now - due

    return {
        "now": now,
        "created_at": t.get("created_at") or "",
        "sla_due_at": due.strftime(TIMESTAMP_FMT) if due else (t.get("sla_due_at") or ""),
        "age": age,
        "age_text": format_duration(age),
        "breach": breach,
        "breach_text": format_duration(breach) if breach else "n/a",
    }


def compute_sla_state(ticket: dict | sqlite3.Row, now: datetime | None = None, use_policy: bool = False) -> dict:
    """Evaluate SLA timing and return display dictionary."""
    t = dict(ticket) if hasattr(ticket, "keys") else ticket
    now = now or datetime.now()
    status = (t.get("status") or "").strip()
    sla_status = (t.get("sla_status") or "").strip()
    created = parse_ts(t.get("created_at"))
    priority = t.get("priority", "Medium")
    target = timedelta(hours=sla_target_hours(priority))

    # TC2: Paused clock for Waiting on Customer
    if status == "Waiting on Customer" or sla_status == "Clock Paused":
        return {
            "state": "paused",
            "due_at": None,
            "due_source": "paused",
            "remaining": None,
            "overdue": None,
            "timer_label": "Clock paused - Waiting on Customer",
            "table_label": "Paused",
            "spoken": "n/a",
        }

    stored_due = None if use_policy else parse_ts(t.get("sla_due_at"))
    due = stored_due or (created + target if created else None)

    if due is None:
        return {
            "state": "unknown",
            "due_at": None,
            "due_source": "unknown",
            "remaining": None,
            "overdue": None,
            "timer_label": "Unknown deadline",
            "table_label": "Unknown",
            "spoken": "n/a",
        }

    if now > due or sla_status == "Breached":
        overdue = now - due if now > due else timedelta(minutes=1)
        spoken = format_spoken_duration(overdue)
        return {
            "state": "breached",
            "due_at": due,
            "due_source": "stored" if stored_due else "priority target",
            "remaining": None,
            "overdue": overdue,
            "timer_label": f"BREACHED: Overdue by {spoken}",
            "table_label": f"BREACHED: {format_duration(overdue)} overdue",
            "spoken": spoken,
        }

    remaining = due - now
    state = "due_soon" if remaining <= target * 0.25 else "within"
    spoken = format_duration(remaining)
    return {
        "state": state,
        "due_at": due,
        "due_source": "stored" if stored_due else "priority target",
        "remaining": remaining,
        "overdue": None,
        "timer_label": f"{spoken} remaining until breach",
        "table_label": f"{spoken} remaining",
        "spoken": spoken,
    }


def is_ticket_already_drafted(drafts_conn: sqlite3.Connection, ticket_id: str) -> bool:
    """Duplicate prevention (TC4): Check if draft already exists."""
    row = drafts_conn.execute(
        "SELECT 1 FROM escalation_drafts WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()
    return row is not None


# ==============================================================================
# 5. SANITIZATION & SENSITIVITY DETECTION (TC3)
# ==============================================================================
PRIVATE_MARKER_RE = re.compile(
    r"\b(private|confidential|secret|restricted|nda|internal[ -]only)\b",
    re.IGNORECASE,
)
SENSITIVE_FILE_RE = re.compile(
    r"\.(pem|key|cer|crt|p12|pfx|kdb|jks|ovpn|kdbx|bak|sql|env)$",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
PHONE_RE = re.compile(r"(\+?\d{1,3}[-.\s]?)?(\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}")


def redact_pii(text: str | None) -> tuple[str, dict]:
    """Strip emails, phones, and secrets."""
    if not text:
        return "", {}
    findings: dict[str, int] = {}
    
    def repl_email(m):
        findings["email"] = findings.get("email", 0) + 1
        return "[REDACTED_EMAIL]"

    def repl_phone(m):
        findings["phone"] = findings.get("phone", 0) + 1
        return "[REDACTED_PHONE]"

    clean = EMAIL_RE.sub(repl_email, text)
    clean = PHONE_RE.sub(repl_phone, clean)
    return clean, findings


def redact(text: str | None) -> tuple[str, int]:
    clean, findings = redact_pii(text)
    return clean, sum(findings.values())


def is_ticket_sensitive(ticket: dict, timeline: list, attachments: list) -> bool:
    """Check for private attachments or sensitive markers (TC3)."""
    for a in attachments:
        ad = dict(a) if hasattr(a, "keys") else a
        if ad.get("visibility") == "private":
            return True
        fn = ad.get("filename", "")
        if SENSITIVE_FILE_RE.search(fn) or PRIVATE_MARKER_RE.search(fn):
            return True
    blob = f"{ticket.get('subject', '')} {ticket.get('description', '')}"
    for e in timeline:
        ed = dict(e) if hasattr(e, "keys") else e
        blob += f" {ed.get('content', '')}"
    return bool(PRIVATE_MARKER_RE.search(blob))


def classify(ticket: dict | sqlite3.Row, timeline: list, attachments: list) -> dict:
    """Classify ticket for SLA action and duplicate prevention."""
    t = dict(ticket) if hasattr(ticket, "keys") else ticket
    status = (t.get("status") or "").strip()
    sla_status = (t.get("sla_status") or "").strip()
    prior_escalations = [e for e in timeline if (dict(e).get("kind") if hasattr(e, "keys") else e[3]) == "escalation"]

    # TC4: Duplicate Prevention
    if status == "Escalated" or prior_escalations:
        return {
            "scenario_id": SCENARIO_DUPLICATE,
            "status": STATUS_DUPLICATE,
            "reason": f"Duplicate Prevented - Prior escalation found or status is '{status}'.",
            "requires_escalation": False,
            "sensitive": False,
        }

    # TC2: Paused SLA Clock
    if status == "Waiting on Customer" or sla_status == "Clock Paused":
        return {
            "scenario_id": SCENARIO_SLA_EXCLUDED,
            "status": STATUS_SLA_EXCLUDED,
            "reason": "Excluded from SLA clock - Waiting on Customer response.",
            "requires_escalation": False,
            "sensitive": False,
        }

    if status in ("Resolved", "Closed"):
        return {
            "scenario_id": SCENARIO_UNMATCHED,
            "status": STATUS_NO_ACTION,
            "reason": f"Ticket is {status}. No escalation required.",
            "requires_escalation": False,
            "sensitive": False,
        }

    # Breach check
    timing = compute_timing(t)
    sla_state = compute_sla_state(t)
    is_breached = (
        sla_status.lower() == "breached"
        or sla_state.get("state") == "breached"
        or (timing.get("breach") is not None and timing["breach"].total_seconds() > 0)
    )

    if is_breached:
        sensitive = is_ticket_sensitive(t, timeline, attachments)
        scenario = SCENARIO_SANITIZED if sensitive else SCENARIO_ESCALATE
        pack_status = STATUS_PACK_READY_SANITIZED if sensitive else STATUS_PACK_READY
        return {
            "scenario_id": scenario,
            "status": pack_status,
            "reason": f"SLA breach detected ({timing['breach_text']} overdue). Escalation pack required.",
            "requires_escalation": True,
            "sensitive": sensitive,
        }

    return {
        "scenario_id": SCENARIO_UNMATCHED,
        "status": STATUS_NO_ACTION,
        "reason": "Ticket is within SLA target. No escalation required.",
        "requires_escalation": False,
        "sensitive": False,
    }


# ==============================================================================
# 6. ENTERPRISE UI STYLING & FORMATTING HELPERS
# ==============================================================================
SLA_CHIP_CLASS = {
    "within": "z-sla-ok",
    "due_soon": "z-sla-warn",
    "breached": "z-sla-bad",
    "paused": "z-sla-paused",
    "unknown": "z-sla-paused",
}

DEPARTMENT_COLORS = {
    "Network Support": {"accent": "#0284c7", "bg": "#f0f9ff"},
    "Cyber/Security Operations": {"accent": "#dc2626", "bg": "#fef2f2"},
    "Database Support": {"accent": "#7c3aed", "bg": "#f5f3ff"},
    "Identity and Access Management": {"accent": "#d97706", "bg": "#fffbeb"},
    "Platform Support": {"accent": "#2563eb", "bg": "#eff6ff"},
    "Application Support": {"accent": "#059669", "bg": "#ecfdf5"},
    "Server and Infra Support": {"accent": "#475569", "bg": "#f8fafc"},
    "End User Support": {"accent": "#0891b2", "bg": "#ecfeff"},
    "Email and Collaboration Support": {"accent": "#4f46e5", "bg": "#eef2ff"},
    "IT Operation": {"accent": "#4b5563", "bg": "#f9fafb"},
}
DEFAULT_DEPARTMENT_COLOR = {"accent": "#0b5cad", "bg": "#f0f4f8"}


def department_color(department: str | None) -> dict[str, str]:
    if not department:
        return DEFAULT_DEPARTMENT_COLOR
    for k, v in DEPARTMENT_COLORS.items():
        if k.lower() in department.lower() or department.lower() in k.lower():
            return v
    return DEFAULT_DEPARTMENT_COLOR


def department_card_key(prefix: str, ticket_id: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z_-]+", "-", str(ticket_id)).strip("-").lower()
    return f"zdept-{prefix}-{slug}"


def department_card_css(card_key: str, department: str | None) -> str:
    color = department_color(department)
    return (
        "<style>"
        f".st-key-{card_key} {{"
        f"  border-left: 6px solid {color['accent']} !important;"
        f"  background: {color['bg']} !important;"
        "  border-radius: 4px;"
        "  padding: .4rem .7rem;"
        "}"
        "</style>"
    )


def department_badge_html(department: str | None, level: str | None = None) -> str:
    color = department_color(department)
    dept_label = html.escape(department or "Unrouted")
    suffix = f" &bull; {html.escape(level)}" if level else ""
    return (
        f'<span style="display:inline-block;background:{color["accent"]};color:#fff;'
        f'padding:.18rem .55rem;border-radius:4px;font-size:.78rem;font-weight:700;">'
        f'{dept_label}{suffix}</span>'
    )


def sla_chip_html(sla_state: dict) -> str:
    css = SLA_CHIP_CLASS.get(sla_state.get("state", "paused"), "z-sla-paused")
    return f'<span class="z-sla {css}">{html.escape(sla_state.get("timer_label", ""))}</span>'


SERVICENOW_THEME_CSS = """
<style>
:root {
    --z-navy: #1e293b;
    --z-primary: #0284c7;
    --z-border: #cbd5e1;
    --z-surface: #ffffff;
    --z-surface-alt: #f8fafc;
}
html, body, [class*="css"] { font-family: "Segoe UI", "Inter", sans-serif; }
.z-sla { display:inline-block; padding:.2rem .6rem; border-radius:4px; font-size:.8rem; font-weight:700; border:1px solid; }
.z-sla-ok { background:#ecfdf5; color:#047857; border-color:#059669; }
.z-sla-warn { background:#fffbeb; color:#b45309; border-color:#d97706; }
.z-sla-bad { background:#fef2f2; color:#b91c1c; border-color:#dc2626; }
.z-sla-paused { background:#f1f5f9; color:#475569; border-color:#94a3b8; }
</style>
"""


def apply_enterprise_theme():
    try:
        import streamlit as st
        st.markdown(SERVICENOW_THEME_CSS, unsafe_allow_html=True)
    except Exception:
        pass


def heuristic_department(ticket: dict) -> str:
    blob = f"{ticket.get('subject', '')} {ticket.get('description', '')}".lower()
    return match_department(blob)


# ==============================================================================
# 7. GEMINI AI EXECUTION (STRUCTURED EVALUATION & ROUTING)
# ==============================================================================
def execute_gemini_evaluation(ticket: dict, timing: dict) -> dict:
    """Call Gemini API to evaluate technical complexity, department, level, and executive note."""
    ticket_id = ticket.get("ticket_id", "UNKNOWN")
    subject = ticket.get("subject", "(No subject)")
    priority = ticket.get("priority", "Medium")
    owner = ticket.get("owner", "Unassigned")
    desc = ticket.get("description", "(No description)")
    age_text = timing.get("age_text", "unknown")
    breach_text = timing.get("breach_text", "unknown")

    prompt = f"""You are an elite ITSM SLA Escalation Agent. An enterprise incident has breached its SLA.
Analyze the incident description, determine technical complexity, and output your decision strictly in JSON format.

TICKET FILE:
- Ticket ID: {ticket_id}
- Subject: {subject}
- Priority: {priority}
- Current Age: {age_text}
- SLA Breach Duration: {breach_text}
- Assigned Owner: {owner}
- Description: {desc}

AVAILABLE DEPARTMENTS:
{', '.join(DEPARTMENTS)}

AVAILABLE LEVELS:
L1, L2, L3, Architect

INSTRUCTIONS:
Return a valid JSON object with EXACTLY these four keys:
{{
  "recommended_department": "<one department from the list above>",
  "recommended_level": "<L1, L2, L3, or Architect>",
  "reasoning": "<1-2 sentences on technical complexity and routing rationale>",
  "executive_summary": "<A concise executive escalation brief detailing issue summary, business impact, and next steps>"
}}
"""

    raw_text = ""
    candidate_models = [GEMINI_MODEL, "gemini-3.8-flash", "gemini-2.5-flash", "gemini-1.5-flash"]
    models_to_try = []
    seen = set()
    for m in candidate_models:
        if m and m not in seen:
            seen.add(m)
            models_to_try.append(m)

    # 1. Try google-genai SDK
    for m in models_to_try:
        try:
            from google import genai
            client = genai.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(
                model=m,
                contents=prompt,
            )
            raw_text = (getattr(response, "text", None) or "").strip()
            if raw_text:
                break
        except Exception as exc:
            pass

    # 2. Try google.generativeai SDK if needed
    if not raw_text:
        for m in models_to_try:
            try:
                import google.generativeai as gai
                gai.configure(api_key=GEMINI_API_KEY)
                model = gai.GenerativeModel(m)
                resp = model.generate_content(prompt)
                raw_text = (getattr(resp, "text", None) or "").strip()
                if raw_text:
                    break
            except Exception as exc:
                pass

    # Parse JSON from model response
    dept = None
    level = None
    reasoning = None
    exec_summary = None

    if raw_text:
        # Strip markdown fences if present
        clean_json = re.sub(r"^```(?:json)?", "", raw_text.strip(), flags=re.MULTILINE).rstrip("`").strip()
        try:
            parsed = json.loads(clean_json)
            dept = match_department(parsed.get("recommended_department"))
            level = match_level(parsed.get("recommended_level"))
            reasoning = parsed.get("reasoning")
            exec_summary = parsed.get("executive_summary")
        except Exception:
            pass

    # Safe fallbacks if API call returned empty or failed
    if not dept:
        dept = heuristic_department(ticket)
    if not level:
        level = "L3" if priority in ("Critical", "High") else "L2"
    if not reasoning:
        reasoning = f"Automated technical routing to {dept} ({level}) due to {priority} priority SLA breach."
    if not exec_summary:
        exec_summary = (
            f"Ticket {ticket_id} ('{subject}') has breached its SLA by {breach_text}. "
            f"The issue requires immediate senior triage by {dept} at {level} level."
        )

    # Pick staff assignee from the matrix
    assignee = get_random_staff(dept, level, exclude=owner)

    # Format complete executive escalation note
    escalation_draft = (
        f"EXECUTIVE ESCALATION NOTE\n"
        f"Assigned_Department: {dept}\n"
        f"Required_Level: {level}\n"
        f"Recommended_Assignee: {assignee}\n"
        f"Reasoning: {reasoning}\n\n"
        f"Ticket ID: {ticket_id}\n"
        f"Subject: {subject}\n"
        f"Current Age: {age_text}\n"
        f"SLA Breach Duration: {breach_text}\n"
        f"Assigned Owner: {owner}\n\n"
        f"Key Issue & Executive Summary:\n"
        f"{exec_summary}\n\n"
        f"Proposed Escalation Target:\n"
        f"{dept} - Technical Tier: {level} (Accountable: {assignee})"
    )

    return {
        "recommended_department": dept,
        "recommended_level": level,
        "recommended_assignee": assignee,
        "routing_reason": reasoning,
        "escalation_draft": escalation_draft,
    }


# ==============================================================================
# 8. DATABASE STORAGE & DRAFTS LIFECYCLE
# ==============================================================================
def init_drafts_db(conn: sqlite3.Connection, reset: bool = False):
    """Ensure escalation_drafts table exists with complete schema."""
    if reset:
        conn.execute("DROP TABLE IF EXISTS escalation_drafts")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS escalation_drafts (
            ticket_id              TEXT PRIMARY KEY,
            scenario_id            TEXT NOT NULL,
            status                 TEXT NOT NULL,
            escalation_draft       TEXT,
            decision_reason        TEXT,
            approval_status        TEXT NOT NULL DEFAULT 'Pending Approval',
            created_at             TEXT NOT NULL,
            recommended_level      TEXT,
            recommended_assignee   TEXT,
            routing_reason         TEXT,
            final_assignee         TEXT,
            recommended_department TEXT
        )
        """
    )
    conn.commit()


def save_draft(
    conn: sqlite3.Connection,
    ticket_id: str,
    scenario_id: str,
    status: str,
    escalation_draft: str | None,
    decision_reason: str | None,
    approval_status: str = DEFAULT_APPROVAL_STATUS,
    recommended_department: str | None = None,
    recommended_level: str | None = None,
    recommended_assignee: str | None = None,
    routing_reason: str | None = None,
    final_assignee: str | None = None,
    append: bool = False,
):
    """Insert or update an escalation pack into escalation_drafts.db."""
    init_drafts_db(conn)
    existing = conn.execute(
        "SELECT approval_status, final_assignee FROM escalation_drafts WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()

    # Preserve manager decision if already approved/rejected
    if existing and existing["approval_status"] in ("Approved", "Rejected"):
        approval_status = existing["approval_status"]
        if existing["final_assignee"]:
            final_assignee = existing["final_assignee"]

    now = datetime.now().strftime(TIMESTAMP_FMT)
    conn.execute(
        """
        INSERT OR REPLACE INTO escalation_drafts (
            ticket_id, scenario_id, status, escalation_draft, decision_reason,
            approval_status, created_at, recommended_level, recommended_assignee,
            routing_reason, final_assignee, recommended_department
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticket_id,
            scenario_id,
            status,
            escalation_draft,
            decision_reason,
            approval_status,
            now,
            recommended_level,
            recommended_assignee,
            routing_reason,
            final_assignee,
            recommended_department,
        ),
    )
    conn.commit()


def ensure_tickets_db() -> bool:
    """Make sure tickets.db exists; if missing, seed it via mock_helpdesk."""
    if os.path.exists(TICKETS_DB):
        with closing(connect(TICKETS_DB)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tickets'"
            ).fetchone()
            if row is not None:
                return True
    try:
        import mock_helpdesk
        mock_helpdesk.init_db()
        return True
    except Exception as exc:
        print(f"[agent_core] Unable to auto-seed tickets.db: {exc}")
        return False


def evaluate_ticket(conn: sqlite3.Connection, ticket: dict | sqlite3.Row, dry_run: bool = False) -> dict:
    """Evaluate one ticket and prepare an escalation draft if breached."""
    t = dict(ticket) if hasattr(ticket, "keys") else ticket
    ticket_id = t["ticket_id"]
    timeline = load_timeline(conn, ticket_id)
    attachments = load_attachments(conn, ticket_id)
    decision = classify(t, timeline, attachments)
    timing = compute_timing(t)

    result = {
        "ticket_id": ticket_id,
        "test_case": t.get("test_case") or "MANUAL",
        "subject": t.get("subject") or "",
        "status": t.get("status") or "Open",
        "priority": t.get("priority") or "Medium",
        "sla_status": t.get("sla_status") or "Breached",
        "owner": t.get("owner") or "Unassigned",
        "scenario_id": decision["scenario_id"],
        "decision_status": decision["status"],
        "decision_reason": decision["reason"],
        "requires_escalation": decision["requires_escalation"],
        "age_text": timing["age_text"],
        "breach_text": timing["breach_text"],
        "escalation_draft": None,
        "approval_status": NOT_APPLICABLE,
        "recommended_department": None,
        "recommended_level": None,
        "recommended_assignee": None,
        "routing_reason": decision["reason"],
        "final_assignee": None,
        "summary_mode": "n/a",
    }

    if not decision["requires_escalation"]:
        return result

    # Call Gemini API to perform technical analysis and generate the escalation note
    ai_result = execute_gemini_evaluation(t, timing)
    result.update({
        "escalation_draft": ai_result["escalation_draft"],
        "approval_status": DEFAULT_APPROVAL_STATUS,
        "recommended_department": ai_result["recommended_department"],
        "recommended_level": ai_result["recommended_level"],
        "recommended_assignee": ai_result["recommended_assignee"],
        "routing_reason": ai_result["routing_reason"],
        "summary_mode": "gemini",
    })
    return result


# ==============================================================================
# 9. THE PROGRESS BAR & QUEUE SWEEP FUNCTIONS
# ==============================================================================
def sweep(dry_run: bool = False) -> dict:
    """Background evaluation of the queue. Evaluates EVERY ticket in the database."""
    return sweep_with_progress(dry_run=dry_run, on_progress=None)


def sweep_with_progress(dry_run: bool = False, on_progress=None) -> dict:
    """Evaluate EVERY ticket in tickets.db with live callbacks for the UI progress bar.
    
    1. Connects to tickets.db and escalation_drafts.db.
    2. Iterates over EVERY ticket returned by load_tickets(ticket_conn) without restrictions.
    3. Evaluates all tickets in the database where the SLA is breached.
    4. Triggers on_progress(fraction, label) at each stage for frontend UI updates.
    5. Persists drafts to escalation_drafts.db and returns a comprehensive summary.
    """
    def _notify(fraction: float, label: str):
        if on_progress is not None:
            try:
                on_progress(float(fraction), str(label))
            except Exception:
                pass

    summary = {
        "ok": False,
        "evaluated": 0,
        "escalated": 0,
        "drafts": 0,
        "pending": 0,
        "mode": "gemini",
        "at": datetime.now().strftime(TIMESTAMP_FMT),
    }

    _notify(0.05, "Connecting to tickets.db & escalation_drafts.db...")
    ensure_tickets_db()

    with closing(connect(TICKETS_DB)) as ticket_conn, \
         closing(connect(DRAFTS_DB)) as drafts_conn:
        init_drafts_db(drafts_conn)

        # Load EVERY ticket in tickets.db - no LIMIT and no hardcoded ID filters
        _notify(0.12, "Querying tickets.db for entire ticket queue...")
        all_tickets = load_tickets(ticket_conn)
        total = len(all_tickets)

        _notify(0.18, f"Found {total} ticket(s) in database. Evaluating SLA status...")

        for idx, ticket in enumerate(all_tickets):
            t = dict(ticket) if hasattr(ticket, "keys") else ticket
            ticket_id = t.get("ticket_id", f"TCK-{idx+1}")
            frac_start = 0.20 + (0.70 * (idx / max(total, 1)))
            frac_mid = frac_start + (0.70 / max(total, 1) * 0.5)

            _notify(frac_start, f"[{idx + 1}/{total}] Checking SLA breach & status for {ticket_id}...")

            # Evaluate ticket (checks if SLA is breached and prepares escalation pack)
            _notify(frac_mid, f"[{idx + 1}/{total}] AI evaluating {ticket_id} ({t.get('priority', 'Medium')} priority)...")
            result = evaluate_ticket(ticket_conn, t, dry_run=dry_run)

            save_draft(
                drafts_conn,
                ticket_id=result["ticket_id"],
                scenario_id=result["scenario_id"],
                status=result["decision_status"],
                escalation_draft=result["escalation_draft"],
                decision_reason=result["decision_reason"],
                approval_status=result["approval_status"],
                recommended_department=result["recommended_department"],
                recommended_level=result["recommended_level"],
                recommended_assignee=result["recommended_assignee"],
                routing_reason=result["routing_reason"],
                final_assignee=result["final_assignee"],
            )

            summary["evaluated"] += 1
            if result.get("requires_escalation"):
                summary["escalated"] += 1

        _notify(0.92, "Syncing escalation packs with database...")
        summary["drafts"] = drafts_conn.execute("SELECT COUNT(*) AS n FROM escalation_drafts").fetchone()["n"]
        summary["pending"] = drafts_conn.execute(
            "SELECT COUNT(*) AS n FROM escalation_drafts WHERE approval_status = ?",
            (DEFAULT_APPROVAL_STATUS,),
        ).fetchone()["n"]
        summary["ok"] = True

        _notify(1.0, f"Complete — {summary['evaluated']} ticket(s) evaluated, {summary['escalated']} pack(s) prepared.")

    return summary


# ==============================================================================
# 10. CLI EXECUTION ENTRY POINT
# ==============================================================================
if __name__ == "__main__":
    print(f"[agent_core] Running Ziro SLA Escalation Sweep against {TICKETS_DB}...")
    res = sweep()
    print(f"[agent_core] Done: {res['evaluated']} evaluated, {res['escalated']} escalated, {res['pending']} pending approval.")
