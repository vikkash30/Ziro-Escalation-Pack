"""Master Control - customer-facing portal and AI testing harness.

Two jobs:

1. Submit a ticket the way a customer would. The description is run through the
   PII redaction engine *before* anything is written, so no personal data ever
   reaches ``tickets.db``.
2. Act as a test harness for the escalation agent. A time-travel control
   back-dates a ticket so SLA breaches can be forced on demand, and the status
   control reaches the TC2/TC3/TC4 decision paths. A live panel shows exactly
   what the agent will decide.

Run on its own port so it can sit alongside the mock helpdesk and the approval
dashboard:

    streamlit run customer_portal.py --server.port 8503
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

import streamlit as st

import agent_core  # shared DB path, SLA policy targets, PII redaction and theme

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DB_PATH = agent_core.TICKETS_DB
TIMESTAMP_FMT = agent_core.TIMESTAMP_FMT

TICKET_ID_PREFIX = "TCK-"
TICKET_ID_RE = re.compile(rf"{TICKET_ID_PREFIX}(\d+)")

PRIORITIES = ["Low", "Medium", "High", "Critical"]
STATUSES = ["Open", "Waiting on Customer", "Escalated"]

# SLA targets come from the shared policy so the portal and the agent can never
# disagree: Critical 1h / High 4h / Medium 8h / Low 24h.
SLA_TARGETS = agent_core.PRIORITY_SLA_TARGET_HOURS

SLA_STATUS_HEALTHY = "Healthy"
SLA_STATUS_BREACHED = "Breached"
SLA_STATUS_PAUSED = "Clock Paused"

# 'test_case' is NOT NULL in the schema; customer-raised tickets get their own
# marker so they stay distinguishable from the official TC1-TC4 cases.
CUSTOMER_TEST_CASE = "CUSTOMER"
DEFAULT_OWNER = "Unassigned"
DEFAULT_REQUESTER = "Customer Portal"

# Matched by agent_core's private-marker pattern, so a ticket the customer flags
# as sensitive is redacted before any escalation note is drafted.
SENSITIVE_MARKER = "[Private Attachment: sensitivity flagged by the requester]"

MAX_AGE_HOURS = 336  # 14 days of time travel
PORT = 8503


# --------------------------------------------------------------------------- #
# Scheduling + persistence
# --------------------------------------------------------------------------- #

def compute_schedule(priority, status, age_hours, now=None):
    """Derive (created_at, sla_due_at, sla_status) for the given test settings.

    Time travel back-dates both the creation time and the SLA deadline, so aging
    a ticket past its priority target is what forces the breach.
    """
    now = now or datetime.now()
    created = now - timedelta(hours=age_hours)
    due = created + timedelta(hours=agent_core.sla_target_hours(priority))

    if status == "Waiting on Customer":
        sla_status = SLA_STATUS_PAUSED
    elif due <= now:
        sla_status = SLA_STATUS_BREACHED
    else:
        sla_status = SLA_STATUS_HEALTHY

    return created, due, sla_status


def predict_scenario(status, sla_status, sensitive):
    """Mirror agent_core.classify() so the tester can see the expected outcome.

    Order matters and matches the agent: duplicate prevention is evaluated before
    the SLA rules, because an already-escalated ticket must never re-escalate.
    """
    if status == "Escalated":
        return "TC4", "Duplicate prevented - the ticket is already escalated."
    if status == "Waiting on Customer":
        return "TC2", "Excluded from the SLA clock (paused) - no escalation."
    if sla_status == SLA_STATUS_BREACHED and status == "Open":
        if sensitive:
            return "TC3", "Breached + sensitive marker - pack prepared with private details sanitized."
        return "TC1", "Breached + Open - complete escalation pack prepared."
    return "-", "No escalation rule matches this combination - no action required."


def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def next_ticket_id(conn):
    """Return the next free id, one past the highest existing TCK number."""
    highest = 0
    for row in conn.execute(
        f"SELECT ticket_id FROM tickets WHERE ticket_id LIKE '{TICKET_ID_PREFIX}%'"
    ):
        match = TICKET_ID_RE.fullmatch(row["ticket_id"])
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{TICKET_ID_PREFIX}{highest + 1:03d}"


def insert_ticket(subject, description, priority, status, age_hours, sensitive):
    """Redact PII, then insert the ticket. Returns a result dict."""
    now = datetime.now()
    created, due, sla_status = compute_schedule(priority, status, age_hours, now=now)

    # Pre-processing: strip personal data before it can reach SQLite.
    clean_description, findings = agent_core.redact_pii(description)
    body = clean_description
    if sensitive:
        body = f"{SENSITIVE_MARKER}\n\n{body}"

    timestamp = now.strftime(TIMESTAMP_FMT)
    scenario, _ = predict_scenario(status, sla_status, sensitive)
    target_hours = agent_core.sla_target_hours(priority)

    history = (
        f"Ticket created through the Master Control harness. "
        f"Back-dated {age_hours}h; SLA target {target_hours}h ({sla_status}). "
        f"Predicted agent scenario: {scenario}."
    )
    if findings:
        summary = ", ".join(f"{count} {label}" for label, count in sorted(findings.items()))
        history += f" PII redacted before insert: {summary}."

    with closing(get_connection()) as conn:
        ticket_id = next_ticket_id(conn)
        conn.execute(
            """
            INSERT INTO tickets (
                ticket_id, test_case, subject, status, priority, sla_status,
                sla_due_at, owner, requester, created_at, updated_at, description
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticket_id,
                CUSTOMER_TEST_CASE,
                subject,
                status,
                priority,
                sla_status,
                due.strftime(TIMESTAMP_FMT),
                DEFAULT_OWNER,
                DEFAULT_REQUESTER,
                created.strftime(TIMESTAMP_FMT),
                timestamp,
                body,
            ),
        )
        conn.execute(
            "INSERT INTO timeline (ticket_id, created_at, author, kind, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticket_id, timestamp, "Customer Portal", "system", history),
        )
        conn.commit()

    return {
        "ticket_id": ticket_id,
        "created_at": created,
        "due_at": due,
        "sla_status": sla_status,
        "findings": findings,
        "scenario": scenario,
    }


def ensure_database():
    """Make sure tickets.db exists (seeded with the official test cases)."""
    return agent_core.ensure_tickets_db()


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

st.set_page_config(
    page_title="Master Control - Customer Portal",
    page_icon=":material/tune:",
    layout="wide",
)
agent_core.apply_enterprise_theme()

if not ensure_database():
    st.error(
        "The helpdesk database could not be opened or created. "
        "Run `streamlit run mock_helpdesk.py` once, then reload this page.",
        title="Database unavailable",
    )
    st.stop()

st.title("Master Control - Customer Portal")
st.caption(
    "Raise a ticket like a customer, or back-date it to force SLA breaches and "
    "exercise the escalation agent's decision paths."
)

form_col, preview_col = st.columns([3, 2], gap="large")

# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #

with form_col:
    st.subheader("Ticket details")

    with st.form("new_ticket", clear_on_submit=True):
        subject = st.text_input(
            "Subject",
            placeholder="Short summary, e.g. 'VPN login fails on the new laptop'",
        )
        description = st.text_area(
            "Description",
            placeholder=(
                "What happened, when it started, and who is affected. "
                "Phone numbers, emails, SSNs and card numbers are redacted automatically."
            ),
            height=170,
        )

        c1, c2 = st.columns(2)
        priority = c1.selectbox(
            "Priority", options=PRIORITIES, index=PRIORITIES.index("Medium")
        )
        status = c2.selectbox("Status", options=STATUSES, index=0)

        st.markdown("##### Time travel")
        age_hours = st.slider(
            "Set Ticket Age (Hours)",
            min_value=0,
            max_value=MAX_AGE_HOURS,
            value=0,
            step=1,
            help=(
                "Back-dates the ticket and its SLA deadline by this many hours. "
                "Age it past the priority target to force a breach."
            ),
        )

        sensitive = st.toggle(
            "Contains sensitive/private information",
            help=(
                "Adds the private-attachment marker the agent looks for. Combined "
                "with a breach this exercises the TC3 sanitization path."
            ),
        )

        submitted = st.form_submit_button(
            "Submit ticket",
            type="primary",
            icon=":material/send:",
            width="stretch",
        )

# --------------------------------------------------------------------------- #
# Live preview - what will be written and what the agent will decide
# --------------------------------------------------------------------------- #

with preview_col:
    st.subheader("Live preview")

    target_hours = agent_core.sla_target_hours(priority)
    created, due, sla_status = compute_schedule(priority, status, age_hours)
    scenario, scenario_reason = predict_scenario(status, sla_status, sensitive)

    p1, p2 = st.columns(2)
    p1.metric("SLA Target", f"{target_hours}h")
    p2.metric("Effective Age", agent_core.format_duration(timedelta(hours=age_hours)))

    simulated = {
        "status": status,
        "sla_status": sla_status,
        "priority": priority,
        "created_at": created.strftime(TIMESTAMP_FMT),
        "sla_due_at": due.strftime(TIMESTAMP_FMT),
    }
    sla_view = agent_core.compute_sla_state(simulated, now=datetime.now())
    st.markdown(agent_core.sla_chip_html(sla_view), unsafe_allow_html=True)

    st.caption(
        f"Created: {simulated['created_at']}  \n"
        f"Deadline: {simulated['sla_due_at']}  \n"
        f"Stored SLA status: `{sla_status}`"
    )

    st.markdown("##### Predicted agent decision")
    scenario_box = st.container(border=True)
    with scenario_box:
        st.markdown(f"**Scenario {scenario}**")
        st.write(scenario_reason)

    st.markdown("##### PII redaction preview")
    if description.strip():
        preview_text, preview_findings = agent_core.redact_pii(description)
        if preview_findings:
            counts = ", ".join(
                f"{count} {label}" for label, count in sorted(preview_findings.items())
            )
            st.error(f"Detected and will redact: {counts}")
        else:
            st.success("No personal data detected in the description.")
        st.code(preview_text, language="text")
    else:
        st.caption("Start typing a description to preview redaction.")

# --------------------------------------------------------------------------- #
# Submit
# --------------------------------------------------------------------------- #

if submitted:
    if not subject.strip() or not description.strip():
        st.error("Please fill in both the subject and the description.")
    else:
        result = insert_ticket(
            subject.strip(),
            description.strip(),
            priority,
            status,
            age_hours,
            sensitive,
        )
        st.success(
            f"Ticket **{result['ticket_id']}** created - {priority} priority, "
            f"status {status}, stored SLA status {result['sla_status']}, "
            f"predicted scenario **{result['scenario']}**.",
            title="Ticket submitted",
            icon=":material/check_circle:",
        )
        if result["findings"]:
            counts = ", ".join(
                f"{count} {label}"
                for label, count in sorted(result["findings"].items())
            )
            st.info(
                f"PII redacted before insert: {counts}. The original values were "
                f"never written to {os.path.basename(DB_PATH)}.",
                icon=":material/privacy_tip:",
            )
        st.caption(
            f"Next: run `python agent_core.py` to evaluate it, then review it in the "
            f"approval dashboard on port 8502. View it in the helpdesk at "
            f"`http://localhost:8501/?ticket={result['ticket_id']}`."
        )
        st.rerun()

st.divider()
st.caption(
    f"New tickets are written to `{os.path.basename(DB_PATH)}` as test case "
    f"`{CUSTOMER_TEST_CASE}`, so the four official TC1-TC4 cases stay untouched."
)

port = st.get_option("server.port")
if port != PORT:
    st.warning(
        f"This portal is expected on port {PORT}: "
        f"`streamlit run customer_portal.py --server.port {PORT}`"
    )
