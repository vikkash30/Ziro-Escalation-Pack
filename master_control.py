"""Master Control - Database Admin Dashboard.

Full read/write/update/delete control over ``tickets.db`` and the escalation
queue, in three tabs:

Tab 1  Live Database Editor    - edit the whole ``tickets`` table in a
                                 ``st.data_editor`` grid (add/delete rows) and
                                 write the result straight back to SQLite.
Tab 2  SLA Time-Travel Injector - insert a ticket whose creation time is
                                 back-dated by N hours so an SLA breach can be
                                 forced on demand.
Tab 3  System Reset             - drops the tickets/timeline/attachments and
                                 escalation tables and rebuilds the four
                                 official hackathon test cases (TC1 - TC4).

Run on its own port so it can sit alongside the mock helpdesk (8501) and the
approval dashboard (8502):

    streamlit run master_control.py --server.port 8504
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

import agent_core  # shared DB paths, SLA policy, PII redaction and the theme
import mock_helpdesk  # canonical schema + seed data for the official test cases

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DB_PATH = agent_core.TICKETS_DB
TIMESTAMP_FMT = agent_core.TIMESTAMP_FMT
PORT = 8503

# Exact column order of the tickets table - reused for SELECT and INSERT so the
# editor can never silently drift from the schema.
TICKET_COLUMNS = [
    "ticket_id", "test_case", "subject", "status", "priority", "sla_status",
    "sla_due_at", "owner", "requester", "created_at", "updated_at", "description",
    "department",
]

PRIORITIES = ["Critical", "High", "Medium", "Low"]
STATUSES = ["Open", "Waiting on Customer", "Escalated"]
SLA_STATUSES = ["Breached", "Clock Paused", "Healthy"]

# Applied to blank cells so a NOT NULL column can never be written as NULL.
FALLBACKS = {
    "test_case": "MANUAL",
    "owner": "Unassigned",
    "requester": "Master Control",
    "status": "Open",
    "priority": "Medium",
    "sla_status": "Healthy",
    "description": "(no description provided)",
}

# Timestamp shapes accepted from the editor, converted to TIMESTAMP_FMT on save.
ACCEPTED_TIMESTAMP_FORMATS = (
    TIMESTAMP_FMT,
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)

TICKET_ID_RE = re.compile(r"TCK-(\d+)")

TAB_EDITOR = "Live Database Editor"
TAB_INJECTOR = "SLA Time-Travel Injector"
TAB_RESET = "System Reset (Danger Zone)"
TAB_LABELS = [TAB_EDITOR, TAB_INJECTOR, TAB_RESET]
TABS_KEY = "master_control_tabs"


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #

def get_connection():
    """Open a new SQLite connection with row access by column name."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def load_tickets_frame():
    """The whole tickets table as a DataFrame, in schema order."""
    with closing(get_connection()) as conn:
        rows = conn.execute(
            f"SELECT {', '.join(TICKET_COLUMNS)} FROM tickets ORDER BY ticket_id"
        ).fetchall()
    return pd.DataFrame([dict(row) for row in rows], columns=TICKET_COLUMNS)


def next_ticket_id():
    """The next free TCK-### id (continues past the highest one in use)."""
    with closing(get_connection()) as conn:
        used = [row["ticket_id"] for row in conn.execute("SELECT ticket_id FROM tickets")]
    numbers = [
        int(match.group(1)) for match in (TICKET_ID_RE.search(tid or "") for tid in used)
        if match
    ]
    return f"TCK-{max(numbers, default=100) + 1}"


def database_counts():
    """Row counts for every table the dashboard can touch."""
    counts = {"tickets": 0, "timeline": 0, "attachments": 0, "escalation_drafts": 0}

    if os.path.exists(DB_PATH):
        with closing(get_connection()) as conn:
            for table in ("tickets", "timeline", "attachments"):
                try:
                    counts[table] = conn.execute(
                        f"SELECT COUNT(*) AS n FROM {table}"
                    ).fetchone()["n"]
                except sqlite3.OperationalError:
                    counts[table] = 0

    if os.path.exists(agent_core.DRAFTS_DB):
        with closing(agent_core.connect(agent_core.DRAFTS_DB)) as conn:
            try:
                counts["escalation_drafts"] = conn.execute(
                    "SELECT COUNT(*) AS n FROM escalation_drafts"
                ).fetchone()["n"]
            except sqlite3.OperationalError:
                counts["escalation_drafts"] = 0

    return counts


# --------------------------------------------------------------------------- #
# Tab 1 - Live Database Editor
# --------------------------------------------------------------------------- #

def normalize_timestamp(value):
    """Return *value* as TIMESTAMP_FMT, or None when it is not a timestamp."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ACCEPTED_TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime(TIMESTAMP_FMT)
        except ValueError:
            continue
    return None


def canonical_choice(value, allowed):
    """Match *value* against *allowed* case-insensitively."""
    if value is None:
        return None
    wanted = str(value).strip().casefold()
    for option in allowed:
        if option.casefold() == wanted:
            return option
    return None


def clean_editor_frame(frame):
    """Turn the edited grid into insert-ready dictionaries.

    Returns ``(rows, problems)``. Any problem means nothing is written, so a
    half-valid edit can never corrupt the table.
    """
    problems = []
    rows = []

    frame = frame.copy()
    for column in TICKET_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    frame = frame[TICKET_COLUMNS].astype(object).where(pd.notna(frame[TICKET_COLUMNS]), None)

    seen_ids = set()
    for position, record in enumerate(frame.to_dict("records"), start=1):
        cleaned = {}
        for column in TICKET_COLUMNS:
            value = record.get(column)
            if value is None:
                cleaned[column] = None
            else:
                cleaned[column] = str(value).strip() or None

        # A dynamic editor can leave a fully blank trailing row behind.
        if not cleaned["ticket_id"] and not cleaned["subject"]:
            continue

        ticket_id = cleaned["ticket_id"]
        if not ticket_id:
            problems.append(f"Row {position}: ticket_id is required.")
            continue
        if ticket_id in seen_ids:
            problems.append(f"Row {position}: duplicate ticket_id `{ticket_id}`.")
            continue
        seen_ids.add(ticket_id)

        # Constrained columns: canonicalise, or fall back to a safe default.
        for column, allowed in (
            ("priority", PRIORITIES),
            ("status", STATUSES),
            ("sla_status", SLA_STATUSES),
        ):
            if cleaned[column] is None:
                cleaned[column] = FALLBACKS[column]
                continue
            choice = canonical_choice(cleaned[column], allowed)
            if choice is None:
                problems.append(
                    f"`{ticket_id}`: {column} must be one of {', '.join(allowed)}."
                )
            cleaned[column] = choice

        if not cleaned["subject"]:
            problems.append(f"`{ticket_id}`: subject is required.")

        for column in ("test_case", "owner", "requester", "description"):
            if cleaned[column] is None:
                cleaned[column] = FALLBACKS[column]

        for column in ("created_at", "updated_at"):
            parsed = normalize_timestamp(cleaned[column])
            if parsed is None:
                problems.append(
                    f"`{ticket_id}`: {column} must look like "
                    f"'{datetime.now().strftime(TIMESTAMP_FMT)}'."
                )
            cleaned[column] = parsed

        if cleaned["sla_due_at"] is not None:
            parsed = normalize_timestamp(cleaned["sla_due_at"])
            if parsed is None:
                problems.append(
                    f"`{ticket_id}`: sla_due_at must be a timestamp or left empty "
                    "(empty = paused/unknown clock)."
                )
            cleaned["sla_due_at"] = parsed

        rows.append(cleaned)

    if not rows and not problems:
        problems.append("The editor is empty - refusing to wipe the tickets table.")

    return rows, problems


def save_tickets_frame(frame):
    """Overwrite the tickets table with *frame*. Returns (written, problems)."""
    rows, problems = clean_editor_frame(frame)
    if problems:
        return 0, problems

    with closing(get_connection()) as conn:
        conn.execute("DELETE FROM tickets")
        conn.executemany(
            f"INSERT INTO tickets ({', '.join(TICKET_COLUMNS)}) VALUES "
            f"({', '.join(':' + column for column in TICKET_COLUMNS)})",
            rows,
        )
        conn.commit()

    return len(rows), []


def render_editor_tab():
    st.subheader("Live Database Editor")
    st.caption(
        "Edit any cell in place. Add rows with the `+` control at the bottom of the "
        "grid and delete rows by selecting them and pressing `Delete`. Nothing is "
        f"written until you save. Timestamps must look like `{datetime.now().strftime(TIMESTAMP_FMT)}`."
    )

    frame = load_tickets_frame()
    edited = st.data_editor(
        frame,
        key=f"tickets_editor_{st.session_state['editor_version']}",
        num_rows="dynamic",
        width="stretch",
        hide_index=True,
        height=460,
        column_config={
            "ticket_id": st.column_config.TextColumn(
                "Ticket ID", help="Primary key - must be unique.", width="small"
            ),
            "test_case": st.column_config.TextColumn("Test Case", width="small"),
            "subject": st.column_config.TextColumn("Subject", width="large"),
            "status": st.column_config.SelectboxColumn("Status", options=STATUSES),
            "priority": st.column_config.SelectboxColumn("Priority", options=PRIORITIES),
            "sla_status": st.column_config.SelectboxColumn("SLA Status", options=SLA_STATUSES),
            "sla_due_at": st.column_config.TextColumn(
                "SLA Due At", help="Leave empty to pause/disable the clock.", width="medium"
            ),
            "owner": st.column_config.TextColumn("Owner"),
            "requester": st.column_config.TextColumn("Requester"),
            "created_at": st.column_config.TextColumn("Created At", width="medium"),
            "updated_at": st.column_config.TextColumn("Updated At", width="medium"),
            "description": st.column_config.TextColumn("Description", width="large"),
        },
    )

    st.caption(
        f"{len(edited)} row(s) in the grid | {len(frame)} row(s) currently in "
        f"`{os.path.basename(DB_PATH)}`."
    )

    save_col, reload_col = st.columns([2, 1])
    save_clicked = save_col.button(
        "Save Changes to Database", type="primary", width="stretch"
    )
    reload_clicked = reload_col.button("Discard Edits & Reload", width="stretch")

    if save_clicked:
        written, problems = save_tickets_frame(edited)
        if problems:
            st.error("Nothing was saved. Fix these first:")
            for problem in problems:
                st.markdown(f"- {problem}")
        else:
            st.session_state["editor_version"] += 1
            flash(
                f"Saved {written} ticket(s) to `{os.path.basename(DB_PATH)}`. "
                "The table was fully overwritten.",
                "success",
            )
            st.rerun()

    if reload_clicked:
        st.session_state["editor_version"] += 1
        flash("Reloaded the grid from the database. Unsaved edits were discarded.", "info")
        st.rerun()

    st.caption(
        "Note: deleting a ticket here does not delete its `timeline` or `attachments` "
        "rows, so those history entries stay behind for the removed ticket id."
    )


# --------------------------------------------------------------------------- #
# Tab 2 - SLA Time-Travel Injector
# --------------------------------------------------------------------------- #

def describe_findings(findings):
    """Render a redaction report, e.g. 'email x1, phone x2'."""
    if not findings:
        return "no sensitive data detected"
    return ", ".join(f"{label} x{count}" for label, count in findings.items())


def insert_spoofed_ticket(subject, description, priority, hours):
    """INSERT a ticket whose creation time is *hours* in the past."""
    now = datetime.now()
    created_at = now - timedelta(hours=hours)
    target_hours = agent_core.sla_target_hours(priority)
    due_at = created_at + timedelta(hours=target_hours)
    sla_status = "Breached" if due_at <= now else "Healthy"

    # Regulations first: no personal data may reach SQLite.
    clean_subject, subject_findings = agent_core.redact_pii(subject)
    clean_description, description_findings = agent_core.redact_pii(description)

    findings = dict(subject_findings)
    for label, count in description_findings.items():
        findings[label] = findings.get(label, 0) + count

    ticket_id = next_ticket_id()
    record = {
        "ticket_id": ticket_id,
        "test_case": "MANUAL",
        "subject": clean_subject,
        "status": "Open",
        "priority": priority,
        "sla_status": sla_status,
        "sla_due_at": due_at.strftime(TIMESTAMP_FMT),
        "owner": FALLBACKS["owner"],
        "requester": FALLBACKS["requester"],
        "created_at": created_at.strftime(TIMESTAMP_FMT),
        "updated_at": now.strftime(TIMESTAMP_FMT),
        "description": clean_description,
    }
    # Pre-route the injected ticket with the same heuristic the escalation agent
    # uses, so the helpdesk/approval department colouring has something to show.
    record["department"] = agent_core.heuristic_department(record)

    timeline_note = (
        f"Time-travel injector: creation time spoofed to {record['created_at']} "
        f"({hours:g}h in the past). Priority {priority} -> {target_hours}h SLA target, "
        f"deadline {record['sla_due_at']}. SLA status computed as {sla_status}."
    )

    with closing(get_connection()) as conn:
        conn.execute(
            f"INSERT INTO tickets ({', '.join(TICKET_COLUMNS)}) VALUES "
            f"({', '.join(':' + column for column in TICKET_COLUMNS)})",
            record,
        )
        conn.execute(
            "INSERT INTO timeline (ticket_id, created_at, author, kind, content) "
            "VALUES (?, ?, ?, 'system', ?)",
            (ticket_id, record["updated_at"], "Master Control", timeline_note),
        )
        conn.commit()

    return {
        "record": record,
        "hours": hours,
        "target_hours": target_hours,
        "findings": findings,
        "timeline_note": timeline_note,
    }


def render_injection_result(result):
    record = result["record"]
    st.success(
        f"Injected `{record['ticket_id']}` - created {result['hours']:g}h before now, "
        f"SLA status **{record['sla_status']}**."
    )
    st.markdown(
        agent_core.sla_chip_html(agent_core.compute_sla_state(record)),
        unsafe_allow_html=True,
    )
    st.write(
        f"- Creation time: `{record['created_at']}` (now minus {result['hours']:g}h)\n"
        f"- SLA deadline: `{record['sla_due_at']}` "
        f"({record['priority']} target = {result['target_hours']}h after creation)\n"
        f"- PII redaction on the subject/description: {describe_findings(result['findings'])}"
    )


def render_injector_tab():
    st.subheader("SLA Time-Travel Injector")
    st.caption(
        "Spawn a ticket with a spoofed creation time so an SLA breach can be forced "
        "without waiting. The stored `created_at` is the current time minus the hours "
        "you enter, and the deadline follows the priority target "
        "(Critical 1h / High 4h / Medium 8h / Low 24h)."
    )

    result = st.session_state.get("last_injection")
    if result:
        render_injection_result(result)
        st.divider()

    st.caption(f"Next ticket id: `{next_ticket_id()}`")

    with st.form("time_travel_form", clear_on_submit=True):
        subject = st.text_input(
            "Subject", placeholder="e.g. Checkout service returns 502 for all users"
        )
        description = st.text_area(
            "Description",
            height=140,
            placeholder=(
                "Describe the issue. Emails, phone numbers, SSNs and card numbers are "
                "redacted to [REDACTED PII] before the row is written."
            ),
        )
        priority = st.selectbox("Priority", PRIORITIES, index=PRIORITIES.index("High"))
        hours = st.number_input(
            "Subtract Hours from Creation Time",
            min_value=0.0,
            max_value=8760.0,
            value=6.0,
            step=0.5,
            format="%.1f",
            help="Time Override. 6 makes the ticket exactly 6 hours old.",
        )
        submitted = st.form_submit_button("Inject Ticket", type="primary")

    if submitted:
        if not subject.strip():
            st.error("Please give the ticket a subject.")
            return
        st.session_state["last_injection"] = insert_spoofed_ticket(
            subject.strip(), description.strip(), priority, float(hours)
        )
        st.rerun()


# --------------------------------------------------------------------------- #
# Tab 3 - System Reset (Danger Zone)
# --------------------------------------------------------------------------- #

def reset_to_official_test_data():
    """Drop the tickets and escalation tables, then rebuild TC1 - TC4.

    ``mock_helpdesk.init_db(force=True)`` is the single source of truth for the
    official test cases, so a reset can never produce a subtly different seed.
    """
    mock_helpdesk.init_db(force=True)

    with closing(agent_core.connect(agent_core.DRAFTS_DB)) as conn:
        conn.execute("DROP TABLE IF EXISTS escalation_drafts")
        conn.commit()
        agent_core.init_drafts_db(conn)

    return database_counts()


def render_reset_tab():
    st.subheader("System Reset (Danger Zone)")
    st.caption("Current contents of the two databases this dashboard writes to.")

    counts = database_counts()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Tickets", counts["tickets"])
    c2.metric("Timeline entries", counts["timeline"])
    c3.metric("Attachments", counts["attachments"])
    c4.metric("Escalation drafts", counts["escalation_drafts"])

    st.error(
        "This drops the tickets, timeline, attachments and escalation tables and "
        "rebuilds the four official hackathon test cases (TC1, TC2, TC3, TC4) with "
        "their original dummy data. Everything edited in Tab 1 or injected in Tab 2 "
        "is destroyed."
    )

    confirmed = st.checkbox(
        f"I understand this permanently wipes `{os.path.basename(DB_PATH)}` and "
        f"`{os.path.basename(agent_core.DRAFTS_DB)}`"
    )

    if st.button(
        "Drop Tables & Restore the 4 Official Test Cases",
        type="primary",
        disabled=not confirmed,
    ):
        counts = reset_to_official_test_data()
        st.session_state["editor_version"] += 1
        st.session_state.pop("last_injection", None)
        flash(
            f"Rebuilt TC1-TC4: {counts['tickets']} tickets, {counts['timeline']} timeline "
            f"entries, {counts['attachments']} attachments. Escalation queue cleared.",
            "success",
        )
        go_to_tab(TAB_RESET)
        st.rerun()

    if not confirmed:
        st.caption("Tick the confirmation box to enable the reset button.")


# --------------------------------------------------------------------------- #
# Shell
# --------------------------------------------------------------------------- #

def flash(message, level="success"):
    st.session_state["flash"] = (level, message)


def render_flash():
    payload = st.session_state.pop("flash", None)
    if payload:
        level, message = payload
        getattr(st, level)(message)


def go_to_tab(label):
    """Queue a tab switch. Applied before st.tabs() on the next run."""
    st.session_state["pending_tab"] = label


def apply_pending_tab():
    """Move a queued tab switch onto the tabs widget before it is created."""
    pending = st.session_state.pop("pending_tab", None)
    if pending:
        st.session_state[TABS_KEY] = pending


def main():
    st.set_page_config(page_title="Master Control", page_icon="🛠️", layout="wide")
    agent_core.apply_enterprise_theme()

    # Always run init_db: it is idempotent, and it is what applies the additive
    # `department` migration (plus the official-seed backfill) to an existing
    # database - without it the editor's SELECT of that column would fail.
    mock_helpdesk.init_db()

    if "editor_version" not in st.session_state:
        st.session_state["editor_version"] = 0

    st.title("Master Control - Database Admin Dashboard")
    st.caption(
        f"Read/write console for `{os.path.basename(DB_PATH)}` and "
        f"`{os.path.basename(agent_core.DRAFTS_DB)}`. Port {PORT}."
    )

    render_flash()
    apply_pending_tab()

    tab_editor, tab_injector, tab_reset = st.tabs(
        TAB_LABELS,
        key=TABS_KEY,
        on_change="rerun",
        default=TAB_EDITOR,
    )

    with tab_editor:
        render_editor_tab()
    with tab_injector:
        render_injector_tab()
    with tab_reset:
        render_reset_tab()


if __name__ == "__main__":
    main()
