"""Human-in-the-loop escalation review dashboard.

The approval stage that sits after ``agent_core.py``: it lists every draft with
``approval_status = 'Pending Approval'`` from ``escalation_drafts.db``, lets a
human reviewer approve or reject each one, and - on approval - posts the
escalation note back into ``tickets.db`` with an evidence link.

Run on the secondary port so it does not clash with ``mock_helpdesk.py``:

    streamlit run approval_dashboard.py --server.port 8502
"""

from __future__ import annotations

import os
import re
import time
from contextlib import closing
from datetime import datetime

import streamlit as st

import agent_core  # shared DB paths, statuses and sanitization patterns

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DRAFTS_DB = agent_core.DRAFTS_DB
TICKETS_DB = agent_core.TICKETS_DB
TIMESTAMP_FMT = agent_core.TIMESTAMP_FMT

# Where the evidence links point: the Mock Helpdesk ticket view.
HELPDESK_BASE_URL = os.environ.get("HELPDESK_BASE_URL", "http://localhost:8501").rstrip("/")

PENDING = "Pending Approval"
APPROVED = "Approved"
REJECTED = "Rejected"

REVIEWER = "Approval Dashboard (Human Reviewer)"
POSTED_KIND = "escalation"

# Autopilot: re-run the escalation agent on a fixed cadence while it is enabled.
AUTOPILOT_INTERVAL_SECONDS = 60
AUTOPILOT_INTERVAL = f"{AUTOPILOT_INTERVAL_SECONDS}s"


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #

def connect(path):
    return agent_core.connect(path)


def drafts_db_ready():
    if not os.path.exists(DRAFTS_DB):
        return False
    try:
        with closing(connect(DRAFTS_DB)) as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='escalation_drafts'"
            ).fetchone()
        return row is not None
    except Exception:
        return False


def load_drafts(approval_status=None):
    """Fetch escalation drafts, strictly pulling the latest draft per ticket_id using MAX(rowid)."""
    with closing(connect(DRAFTS_DB)) as conn:
        if approval_status:
            sql = """
                SELECT rowid, * FROM escalation_drafts
                WHERE rowid IN (
                    SELECT MAX(rowid) FROM escalation_drafts
                    WHERE approval_status = ?
                    GROUP BY ticket_id
                )
                ORDER BY created_at DESC, ticket_id ASC
            """
            return [dict(r) for r in conn.execute(sql, (approval_status,)).fetchall()]
        else:
            sql = """
                SELECT rowid, * FROM escalation_drafts
                WHERE rowid IN (
                    SELECT MAX(rowid) FROM escalation_drafts
                    GROUP BY ticket_id
                )
                ORDER BY created_at DESC, ticket_id ASC
            """
            return [dict(r) for r in conn.execute(sql).fetchall()]


def count_by_status():
    """Count unique tickets per status based on the latest draft per ticket_id."""
    with closing(connect(DRAFTS_DB)) as conn:
        sql = """
            SELECT approval_status, COUNT(*) AS n FROM (
                SELECT approval_status FROM escalation_drafts
                WHERE rowid IN (
                    SELECT MAX(rowid) FROM escalation_drafts
                    GROUP BY ticket_id
                )
            ) GROUP BY approval_status
        """
        rows = conn.execute(sql).fetchall()
    counts = {r["approval_status"]: r["n"] for r in rows}
    return {
        "pending": counts.get(PENDING, 0),
        "approved": counts.get(APPROVED, 0),
        "rejected": counts.get(REJECTED, 0),
        "total": sum(counts.values()),
    }


def load_ticket_index():
    """Bulk ticket lookup so the UI does not open a connection per card."""
    if not os.path.exists(TICKETS_DB):
        return {}
    try:
        with closing(connect(TICKETS_DB)) as conn:
            tickets = conn.execute("SELECT * FROM tickets").fetchall()
            private = {
                r["ticket_id"]: r["n"]
                for r in conn.execute(
                    "SELECT ticket_id, COUNT(*) AS n FROM attachments "
                    "WHERE visibility = 'private' GROUP BY ticket_id"
                ).fetchall()
            }
            approvals = {
                r["ticket_id"]: r["ts"]
                for r in conn.execute(
                    "SELECT ticket_id, MAX(created_at) AS ts FROM timeline "
                    "WHERE kind = ? AND author = ? GROUP BY ticket_id",
                    (POSTED_KIND, REVIEWER),
                ).fetchall()
            }
    except Exception:
        return {}

    index = {}
    for t in tickets:
        record = dict(t)
        record["private_attachments"] = private.get(t["ticket_id"], 0)
        record["approved_at"] = approvals.get(t["ticket_id"])
        index[t["ticket_id"]] = record
    return index


def set_approval_status(ticket_id, scenario_id, new_status):
    with closing(connect(DRAFTS_DB)) as conn:
        conn.execute(
            "UPDATE escalation_drafts SET approval_status = ? "
            "WHERE ticket_id = ? AND scenario_id = ?",
            (new_status, ticket_id, scenario_id),
        )
        conn.commit()


def set_final_assignee(ticket_id, scenario_id, assignee):
    """Record the human manager's final routing decision."""
    with closing(connect(DRAFTS_DB)) as conn:
        conn.execute(
            "UPDATE escalation_drafts SET final_assignee = ? "
            "WHERE ticket_id = ? AND scenario_id = ?",
            (assignee, ticket_id, scenario_id),
        )
        conn.commit()


def make_evidence_link(ticket_id, scenario_id, approved_at):
    return {
        "url": f"{HELPDESK_BASE_URL}/?ticket={ticket_id}",
        "ref": f"ESC-EVID-{ticket_id}-{scenario_id}-{approved_at.replace(' ', 'T')}",
    }


def post_escalation_to_ticket(ticket_id, scenario_id, draft, approved_at,
                              assignee=None, department=None):
    """Write the approved escalation note into tickets.db with an evidence link.

    The write-back sets ``status = 'Escalated'`` AND reassigns ``owner`` to the
    exact person the human picked in the dashboard selectbox, so the ticket row
    always reflects the routing decision that was approved.
    """
    if not os.path.exists(TICKETS_DB):
        return False, f"{os.path.basename(TICKETS_DB)} not found - cannot post the escalation note."

    evidence = make_evidence_link(ticket_id, scenario_id, approved_at)
    content = (
        f"[HUMAN APPROVAL] Escalation approved by {REVIEWER} on {approved_at}.\n"
        f"Evidence: {evidence['url']}\n"
        f"Evidence ref: {evidence['ref']}\n"
        f"Source: escalation_drafts.db (scenario {scenario_id})\n"
        + (f"Routed assignee: {assignee}\n" if assignee else "")
        + (f"Routed department: {department}\n" if department else "")
        + "\n--- Approved escalation note ---\n"
        f"{draft}"
    )

    try:
        with closing(connect(TICKETS_DB)) as conn:
            ticket = conn.execute(
                "SELECT ticket_id FROM tickets WHERE ticket_id = ?", (ticket_id,)
            ).fetchone()
            if ticket is None:
                return False, f"Ticket {ticket_id} does not exist in {os.path.basename(TICKETS_DB)}."

            # Build the SET clause defensively: `department` may not exist yet on a
            # database created before department routing was introduced.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
            assignments = ["status = 'Escalated'", "updated_at = ?"]
            params = [approved_at]
            if assignee:
                assignments.append("owner = ?")
                params.append(assignee)
            if department and "department" in columns:
                assignments.append("department = ?")
                params.append(department)
            params.append(ticket_id)

            conn.execute(
                "INSERT INTO timeline (ticket_id, created_at, author, kind, content) "
                "VALUES (?, ?, ?, ?, ?)",
                (ticket_id, approved_at, REVIEWER, POSTED_KIND, content),
            )
            conn.execute(
                f"UPDATE tickets SET {', '.join(assignments)} WHERE ticket_id = ?",
                params,
            )
            conn.commit()
    except Exception as exc:
        return False, f"Failed to post to tickets.db: {type(exc).__name__}: {exc}"

    return True, evidence["url"]


# --------------------------------------------------------------------------- #
# Presentation helpers
# --------------------------------------------------------------------------- #

def flash(kind, message):
    st.session_state["_flash"] = (kind, message)


def show_flash():
    item = st.session_state.pop("_flash", None)
    if not item:
        return
    kind, message = item
    (st.success if kind == "success" else st.error)(message)


def extract_routing(draft):
    """Pull the 'Proposed Escalation Target / Routing' section out of a draft."""
    if not draft:
        return None
    lines = draft.splitlines()
    for i, line in enumerate(lines):
        if "routing" in line.lower() and line.strip().endswith(":"):
            target = line.split(":", 1)[1].strip()
            if target:
                return target
            for follow in lines[i + 1:]:
                if follow.strip():
                    return follow.strip()
    return None


def sanitization_state(draft, private_attachments):
    """(is_sanitized, leak_found) for the compliance badge."""
    text = draft or ""
    leak = bool(
        agent_core.PRIVATE_MARKER_RE.search(text)
        or agent_core.SENSITIVE_FILE_RE.search(text)
    )
    sanitized = bool(private_attachments) or "[REDACTED" in text
    return sanitized, leak


# --------------------------------------------------------------------------- #
# Escalation report builder (Feature 2 - File Export)
# --------------------------------------------------------------------------- #

def _extract_section(draft, label):
    """Pull the first non-empty line after 'Label:' in the draft text."""
    pattern = re.compile(
        rf"^\s*{re.escape(label)}\s*:?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
    )
    m = pattern.search(draft or "")
    return m.group(1).strip() if m else "—"


def build_escalation_report(draft_row, ticket):
    """Return a beautifully formatted plain-text escalation report string.

    The file is suitable for download and contains every field required by the
    spec: Executive Summary, Ticket ID, Department, Level/Owner, and the
    exact SLA Breach calculation.
    """
    ticket_id = draft_row.get("ticket_id", "—")
    scenario_id = draft_row.get("scenario_id", "—")
    draft = draft_row.get("escalation_draft") or ""
    now_str = datetime.now().strftime(TIMESTAMP_FMT)

    department = (
        draft_row.get("recommended_department")
        or ticket.get("department")
        or "—"
    )
    level = draft_row.get("recommended_level") or "—"
    assignee = (
        draft_row.get("final_assignee")
        or draft_row.get("recommended_assignee")
        or "—"
    )
    routing_reason = draft_row.get("routing_reason") or "—"

    subject = ticket.get("subject", "—")
    priority = ticket.get("priority", "—")
    status = ticket.get("status", "—")
    sla_status = ticket.get("sla_status", "—")
    owner = ticket.get("owner", "—")
    requester = ticket.get("requester", "—")
    created_at = ticket.get("created_at", "—")
    sla_due_at = ticket.get("sla_due_at", "—")

    # Derive SLA breach duration from the ticket's timing data
    sla_breach_duration = "—"
    if sla_status == "Breached":
        timing = agent_core.compute_timing(ticket)
        breach_td = timing.get("breach")
        if breach_td is not None:
            sla_breach_duration = agent_core.format_spoken_duration(breach_td)
        else:
            sla_breach_duration = timing.get("breach_text", "—")
    elif sla_status == "Clock Paused" or status == "Waiting on Customer":
        sla_breach_duration = "N/A — SLA clock paused (Waiting on Customer)"

    # Pull structured fields from the draft when available
    exec_summary = _extract_section(draft, "Key Issue Summary")
    proposed_routing = _extract_section(draft, "Proposed Escalation Target / Routing")

    sc_meta = SCENARIO_META.get(scenario_id, {})
    scenario_label = sc_meta.get("label", scenario_id)

    sep  = "=" * 72
    sep2 = "-" * 72

    lines = [
        sep,
        "  ZIRO ENTERPRISE ITSM  |  SLA ESCALATION REPORT",
        sep,
        "",
        f"  Generated   : {now_str}",
        f"  Scenario    : {scenario_label}",
        f"  Draft Date  : {draft_row.get('created_at', '—')}",
        "",
        sep2,
        "  TICKET INFORMATION",
        sep2,
        f"  Ticket ID   : {ticket_id}",
        f"  Subject     : {subject}",
        f"  Priority    : {priority}",
        f"  Status      : {status}",
        f"  SLA Status  : {sla_status}",
        f"  Owner       : {owner}",
        f"  Requester   : {requester}",
        f"  Opened At   : {created_at}",
        f"  SLA Due At  : {sla_due_at}",
        "",
        sep2,
        "  SLA BREACH CALCULATION",
        sep2,
        f"  SLA Breach Duration : {sla_breach_duration}",
        f"  SLA Due Deadline    : {sla_due_at}",
        f"  Ticket SLA State    : {sla_status}",
        "",
        sep2,
        "  ROUTING DECISION",
        sep2,
        f"  Assigned Department : {department}",
        f"  Required Level      : {level}",
        f"  Recommended Owner   : {assignee}",
        f"  Routing Reason      : {routing_reason}",
        f"  Proposed Target     : {proposed_routing}",
        "",
        sep2,
        "  EXECUTIVE SUMMARY",
        sep2,
        "",
    ]

    # Wrap the executive summary at ~68 chars for readability
    import textwrap
    for para in exec_summary.splitlines():
        wrapped = textwrap.fill(para, width=68, initial_indent="  ",
                                subsequent_indent="  ")
        lines.append(wrapped or "")

    lines += [
        "",
        sep2,
        "  FULL ESCALATION DRAFT (AI-GENERATED)",
        sep2,
        "",
    ]
    for draft_line in draft.splitlines():
        lines.append(f"  {draft_line}")

    lines += [
        "",
        sep,
        "  END OF ESCALATION REPORT",
        sep,
        "",
        "  This report was generated by the Ziro SLA Breach Escalation Agent.",
        "  It is intended for internal use only.",
        sep,
    ]

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Progress-bar sweep (Feature 1 - Dynamic Processing Progress Bar)
# --------------------------------------------------------------------------- #

PROGRESS_STEPS = [
    (0.05, "🔍 Scanning database for active tickets..."),
    (0.10, "📂 Database connected. Loading ticket queue..."),
    (0.18, "📋 Applying SLA decision rules to all tickets..."),
    (0.50, "🤖 Gemini AI evaluating technical complexity and routing..."),
    (0.80, "🏢 Routing escalations to departments..."),
    (0.88, "💾 Saving escalation drafts to database..."),
    (1.00, "✅ Complete — all tickets evaluated."),
]


def render_progress_sweep(sidebar_placeholder=None):
    """Run the escalation agent sweep with a live st.status + st.progress display.

    Uses ``agent_core.sweep_with_progress`` to receive per-step callbacks and
    update the progress bar in real-time.  The entire visual lives inside an
    ``st.status`` container so the main review queue is not obscured.
    Returns the summary dict from the sweep.
    """
    result_holder = {"summary": None}

    with st.status(
        "🤖 Escalation Agent Processing...",
        expanded=True,
        state="running",
    ) as status_box:
        pbar = st.progress(0.0, text="Initialising agent...")
        info_slot = st.empty()

        def on_progress(fraction, label):
            pct_int = int(fraction * 100)
            pbar.progress(fraction, text=f"**{pct_int}%** — {label}")
            info_slot.markdown(
                f'<div style="'
                'background:linear-gradient(135deg,#0f172a,#1e293b);'
                'color:#e2e8f0;border-radius:8px;padding:0.65rem 1rem;'
                'font-size:0.84rem;margin-top:0.3rem;'
                'border-left:4px solid #6366f1;'
                f'">'
                f'<span style="color:#818cf8;font-weight:700;">{pct_int}%</span> '
                f'&nbsp;{label}</div>',
                unsafe_allow_html=True,
            )

        on_progress(0.02, "🚀 Initialising Ziro Escalation Agent...")
        summary = agent_core.sweep_with_progress(on_progress=on_progress)
        result_holder["summary"] = summary

        mode_icon = "🟢" if summary.get("mode") == "gemini" else "🟡"
        status_box.update(
            label=(
                f"{mode_icon} Agent run complete — "
                f"{summary['evaluated']} ticket(s) evaluated, "
                f"{summary['escalated']} escalation pack(s) prepared."
            ),
            state="complete",
            expanded=False,
        )
        pbar.progress(1.0, text="✅ 100% — Complete")
        info_slot.empty()

    return result_holder["summary"]


# --------------------------------------------------------------------------- #
# Confirmation Dialog (Feature 3)
# --------------------------------------------------------------------------- #

@st.dialog("⚠️ Confirm Escalation")
def confirm_escalation_dialog(ticket_id, scenario_id, draft_content,
                               chosen, recommended_department, safe_key):
    """Modal confirmation popup before writing the escalation to tickets.db.

    Opened by the 'Approve & Route' button. On 'Confirm Assignment' the DB
    write-back happens and st.toast fires a success notification. Cancelling
    closes the dialog without any side effects.
    """
    assignee_display = chosen or "the next escalation tier"

    st.markdown(
        f"""
        <div style="background:linear-gradient(135deg,#fefce8,#fef9c3);
                    border:1.5px solid #fbbf24;border-radius:10px;
                    padding:1rem 1.2rem;margin-bottom:0.8rem;">
            <div style="font-size:1rem;font-weight:800;color:#92400e;margin-bottom:0.4rem;">
                🎫 {ticket_id}
            </div>
            <div style="font-size:0.88rem;color:#78350f;line-height:1.5;">
                You are about to <b>finalise this escalation</b> and assign it to
                <span style="background:#fcd34d;color:#1c1917;padding:0.1rem 0.45rem;
                border-radius:6px;font-weight:700;">{assignee_display}</span>.<br><br>
                This action will:
                <ul style="margin:0.4rem 0 0 1rem;padding:0;">
                    <li>Post the approved escalation note to <code>tickets.db</code></li>
                    <li>Set the ticket status to <b>Escalated</b></li>
                    <li>Record the routing decision in the audit log</li>
                </ul>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    confirm_col, cancel_col = st.columns(2)
    with confirm_col:
        if st.button(
            "✅ Confirm Assignment",
            type="primary",
            use_container_width=True,
            key=f"dialog_confirm_{safe_key}",
        ):
            approved_at = datetime.now().strftime(TIMESTAMP_FMT)
            # Persist any final-assignee override
            with st.spinner("Saving escalation..."):
                ok, message = post_escalation_to_ticket(
                    ticket_id, scenario_id, draft_content, approved_at,
                    assignee=chosen, department=recommended_department,
                )
            if not ok:
                st.error(f"Failed: {message}")
            else:
                set_approval_status(ticket_id, scenario_id, APPROVED)
                st.toast(
                    f"✅ Ticket escalated! The information has been sent to "
                    f"{assignee_display} and they will start working on it immediately.",
                    icon="🚀",
                )
                st.session_state[f"_approved_{safe_key}"] = True
                st.rerun()

    with cancel_col:
        if st.button(
            "↩️ Cancel",
            use_container_width=True,
            key=f"dialog_cancel_{safe_key}",
        ):
            st.rerun()


# Rich colour look-up tables shared by all card renderers
SCENARIO_META = {
    agent_core.SCENARIO_ESCALATE: {
        "label": "TC1 · SLA BREACH", "icon": "🚨",
        "bg": "linear-gradient(135deg,#ef4444,#b91c1c)", "color": "#fff",
        "desc": "SLA breached with status Open — full escalation pack prepared.",
    },
    agent_core.SCENARIO_SANITIZED: {
        "label": "TC3 · SANITIZED PACK", "icon": "🛡️",
        "bg": "linear-gradient(135deg,#f97316,#c2410c)", "color": "#fff",
        "desc": "Private attachment details redacted before drafting external note.",
    },
    agent_core.SCENARIO_SLA_EXCLUDED: {
        "label": "TC2 · SLA EXCLUDED", "icon": "⏸️",
        "bg": "linear-gradient(135deg,#64748b,#475569)", "color": "#fff",
        "desc": "Waiting on Customer — SLA clock paused, no escalation required.",
    },
    agent_core.SCENARIO_DUPLICATE: {
        "label": "TC4 · DUPLICATE PREVENTED", "icon": "🔁",
        "bg": "linear-gradient(135deg,#7c3aed,#5b21b6)", "color": "#fff",
        "desc": "Prior escalation exists — duplicate prevented.",
    },
}

PRIORITY_STYLE = {
    "Critical": {"icon": "🔥", "pill_bg": "linear-gradient(135deg,#ef4444,#b91c1c)"},
    "High":     {"icon": "⚡", "pill_bg": "linear-gradient(135deg,#f97316,#c2410c)"},
    "Medium":   {"icon": "⚠️", "pill_bg": "linear-gradient(135deg,#f59e0b,#d97706)"},
    "Low":      {"icon": "🟢", "pill_bg": "linear-gradient(135deg,#10b981,#047857)"},
}

LEVEL_STYLE = {
    "L1": {"color": "#1d4ed8", "bg": "#eff6ff", "border": "#3b82f6"},
    "L2": {"color": "#7c3aed", "bg": "#f5f3ff", "border": "#8b5cf6"},
    "L3": {"color": "#b91c1c", "bg": "#fef2f2", "border": "#ef4444"},
    "Leadership/Architect": {"color": "#0f766e", "bg": "#f0fdfa", "border": "#14b8a6"},
}


def _pill(label, bg, color, border=None):
    b = f"border:1.5px solid {border};" if border else ""
    return (
        f'<span style="display:inline-flex;align-items:center;background:{bg};color:{color};'
        f'{b}padding:0.18rem 0.6rem;border-radius:20px;font-size:0.8rem;font-weight:700;'
        'box-shadow:0 1px 4px rgba(0,0,0,0.08);">'
        f'{label}</span>'
    )


def render_badges(draft, private_attachments, scenario_id):
    sanitized, leak = sanitization_state(draft, private_attachments)
    parts = []

    if leak:
        parts.append(
            '<span style="display:inline-flex;align-items:center;'
            'background:#fef2f2;color:#b91c1c;border:1.5px solid #f87171;'
            'padding:0.22rem 0.7rem;border-radius:20px;font-size:0.8rem;font-weight:700;'
            'box-shadow:0 2px 6px rgba(185,28,28,0.15);">'
            '⛔ SENSITIVE DATA DETECTED — DO NOT APPROVE</span>'
        )
    elif sanitized:
        detail = f" · {private_attachments} private attachment(s) redacted" if private_attachments else ""
        parts.append(
            '<span style="display:inline-flex;align-items:center;'
            'background:#f0fdf4;color:#166534;border:1.5px solid #86efac;'
            'padding:0.22rem 0.7rem;border-radius:20px;font-size:0.8rem;font-weight:700;'
            'box-shadow:0 2px 6px rgba(22,101,52,0.12);">'
            f'🔒 SANITIZED{detail}</span>'
        )

    sc = SCENARIO_META.get(scenario_id, {})
    if sc:
        parts.append(
            f'<span style="display:inline-flex;align-items:center;'
            f'background:{sc["bg"]};color:{sc["color"]};'
            'padding:0.22rem 0.7rem;border-radius:20px;font-size:0.8rem;font-weight:700;'
            'box-shadow:0 2px 8px rgba(0,0,0,0.12);">'
            f'{sc["icon"]} {sc["label"]}</span>'
        )

    if parts:
        st.markdown(
            '<div style="display:flex;flex-wrap:wrap;gap:0.4rem;margin:0.5rem 0 0.6rem 0;">'
            + "".join(parts) + "</div>",
            unsafe_allow_html=True,
        )


def persist_final_assignee(ticket_id, scenario_id, widget_key):
    """Selectbox callback - saves the manager's chosen assignee to the database."""
    chosen = st.session_state.get(widget_key)
    if chosen:
        set_final_assignee(ticket_id, scenario_id, chosen)
        flash("success", f"{ticket_id}: final assignee set to {chosen}.")


def assignee_pool(department, level):
    """Everyone in that department + level, or every department at that level."""
    if not level:
        return []
    if department:
        return list(agent_core.STAFF_MATRIX.get(department, {}).get(level, []))
    pool = []
    for levels in agent_core.STAFF_MATRIX.values():
        pool.extend(levels.get(level, []))
    return pool


def render_review_card(draft_row, tickets):
    ticket_id = draft_row["ticket_id"]
    scenario_id = draft_row["scenario_id"]
    row_id = draft_row.get("rowid") or draft_row.get("id") or "1"
    content = draft_row.get("escalation_draft") or ""
    ticket = tickets.get(ticket_id, {})
    subject = ticket.get("subject", "(tickets.db unavailable)")
    priority = ticket.get("priority", "Medium")
    private_attachments = ticket.get("private_attachments", 0)

    recommended_department = draft_row.get("recommended_department") or ticket.get("department")
    recommended_level = draft_row.get("recommended_level")
    recommended_assignee = draft_row.get("recommended_assignee")
    routing_reason = draft_row.get("routing_reason")
    final_assignee = draft_row.get("final_assignee")

    prio = PRIORITY_STYLE.get(priority, PRIORITY_STYLE["Medium"])
    dept_color = agent_core.department_color(recommended_department)
    sc_meta = SCENARIO_META.get(scenario_id, {})
    lvl = LEVEL_STYLE.get(recommended_level, LEVEL_STYLE["L2"])

    # Paint the review card with unique card_key incorporating rowid to prevent duplicate key crashes.
    slug = re.sub(r"[^0-9a-zA-Z_-]+", "-", str(ticket_id)).strip("-").lower()
    card_key = f"zdept-approval-{slug}-{row_id}"
    st.markdown(
        agent_core.department_card_css(card_key, recommended_department),
        unsafe_allow_html=True,
    )

    with st.container(border=True, key=card_key):
        # ── Header ───────────────────────────────────────────────────────────
        header_pills = (
            _pill(f'{prio["icon"]} {priority}', prio["pill_bg"], "#fff")
            + _pill(f'🏢 {recommended_department or "Unrouted"}',
                    dept_color["bg"], dept_color["accent"], dept_color["accent"])
            + _pill(f'🎯 {recommended_level or "—"}', lvl["bg"], lvl["color"], lvl["border"])
            + _pill(f'{sc_meta.get("icon","📋")} {scenario_id}', "#f8fafc", "#334155", "#cbd5e1")
        )
        st.markdown(
            f'<div style="margin-bottom:0.5rem;">'
            f'<div style="font-size:1.3rem;font-weight:800;color:#0f172a;margin-bottom:0.4rem;">'
            f'🎫 {ticket_id} <span style="color:{dept_color["accent"]}">· {subject}</span></div>'
            f'<div style="display:flex;flex-wrap:wrap;gap:0.4rem;align-items:center;">'
            f'{header_pills}</div></div>',
            unsafe_allow_html=True,
        )

        # ── Context strip ─────────────────────────────────────────────────
        st.markdown(
            f'<div style="background:white;border:1px solid #e2e8f0;border-radius:6px;'
            f'padding:0.45rem 0.85rem;font-size:0.82rem;color:#64748b;margin-bottom:0.5rem;'
            f'display:flex;gap:1.2rem;flex-wrap:wrap;">'
            f'<span>📅 <b>Drafted:</b> {draft_row["created_at"]}</span>'
            f'<span>👤 <b>Owner:</b> {ticket.get("owner","—")}</span>'
            f'<span>📋 <b>Requester:</b> {ticket.get("requester","—")}</span>'
            f'<span>⚙️ <b>Status:</b> {draft_row.get("status","—")}</span>'
            '</div>',
            unsafe_allow_html=True,
        )

        # ── Scenario banner ──────────────────────────────────────────────
        if sc_meta.get("desc"):
            st.markdown(
                f'<div style="background:{sc_meta["bg"]};color:{sc_meta["color"]};'
                'border-radius:6px;padding:0.4rem 0.85rem;font-size:0.85rem;'
                'font-weight:600;margin-bottom:0.5rem;">'
                f'{sc_meta["icon"]} {sc_meta["desc"]}</div>',
                unsafe_allow_html=True,
            )

        # ── Compliance badges ────────────────────────────────────────────
        render_badges(content, private_attachments, scenario_id)

        # ── Two-column info block ────────────────────────────────────────
        info_left, info_right = st.columns([3, 2])
        with info_left:
            rec_html = (
                '<div style="display:flex;flex-wrap:wrap;gap:0.4rem;margin-bottom:0.4rem;">'
                + _pill(f'👤 {recommended_assignee}', "#eff6ff", "#1d4ed8", "#3b82f6")
                + _pill(f'🎯 {recommended_level}', lvl["bg"], lvl["color"], lvl["border"])
                + _pill(f'🏢 {recommended_department or "—"}',
                        dept_color["bg"], dept_color["accent"], dept_color["accent"])
                + '</div>'
                if (recommended_assignee or recommended_level)
                else '<div style="color:#94a3b8;font-size:0.85rem;font-style:italic;">No routing recommendation available.</div>'
            )
            reason_html = (
                f'<div style="font-size:0.8rem;color:#64748b;border-top:1px solid #e2e8f0;'
                f'margin-top:0.4rem;padding-top:0.4rem;">💬 {routing_reason}</div>'
                if routing_reason else ""
            )
            st.markdown(
                '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;'
                'padding:0.75rem 1rem;margin-bottom:0.7rem;">'
                '<div style="font-size:0.72rem;text-transform:uppercase;color:#64748b;'
                'font-weight:700;letter-spacing:0.04em;margin-bottom:0.45rem;">🤖 AI Routing Recommendation</div>'
                + rec_html + reason_html + '</div>',
                unsafe_allow_html=True,
            )
            routing = extract_routing(content)
            if routing:
                st.markdown(
                    '<div style="background:#fdf4ff;border:1px solid #e9d5ff;border-radius:8px;'
                    'padding:0.75rem 1rem;margin-bottom:0.7rem;">'
                    '<div style="font-size:0.72rem;text-transform:uppercase;color:#7c3aed;'
                    'font-weight:700;letter-spacing:0.04em;margin-bottom:0.35rem;">📡 Proposed Escalation Target</div>'
                    f'<div style="font-size:0.88rem;color:#4c1d95;font-weight:500;">{routing}</div>'
                    '</div>',
                    unsafe_allow_html=True,
                )

        with info_right:
            status_map = {
                agent_core.STATUS_PACK_READY: ("#ecfdf5", "#166534", "#86efac"),
                agent_core.STATUS_PACK_READY_SANITIZED: ("#fff7ed", "#9a3412", "#fdba74"),
                agent_core.STATUS_SLA_EXCLUDED: ("#f1f5f9", "#475569", "#94a3b8"),
                agent_core.STATUS_DUPLICATE: ("#f5f3ff", "#5b21b6", "#c4b5fd"),
                agent_core.STATUS_NO_ACTION: ("#f1f5f9", "#475569", "#94a3b8"),
            }
            s_bg, s_col, s_bdr = status_map.get(
                draft_row.get("status", ""), ("#f8fafc", "#334155", "#e2e8f0")
            )
            st.markdown(
                f'<div style="background:{s_bg};border:1.5px solid {s_bdr};border-radius:8px;'
                f'padding:0.7rem 0.9rem;margin-bottom:0.6rem;">'
                f'<div style="font-size:0.72rem;text-transform:uppercase;font-weight:700;'
                f'letter-spacing:0.04em;color:{s_col};margin-bottom:0.25rem;">⚙️ Agent Status</div>'
                f'<div style="font-size:0.85rem;font-weight:600;color:{s_col};">{draft_row.get("status","—")}</div>'
                '</div>',
                unsafe_allow_html=True,
            )
            sla_val = ticket.get("sla_status", "—")
            sla_col = "#b91c1c" if sla_val == "Breached" else ("#b45309" if "Paused" in sla_val else "#166534")
            priv_col = "#b91c1c" if private_attachments else "#166534"
            priv_txt = f"🔒 {private_attachments} file(s)" if private_attachments else "✅ None"
            st.markdown(
                '<div style="background:white;border:1px solid #e2e8f0;border-radius:8px;'
                'padding:0.7rem 0.9rem;">'
                '<div style="font-size:0.72rem;text-transform:uppercase;color:#64748b;'
                'font-weight:700;letter-spacing:0.04em;margin-bottom:0.35rem;">📊 Ticket Context</div>'
                '<div style="display:grid;grid-template-columns:auto 1fr;gap:0.2rem 0.6rem;font-size:0.83rem;">'
                f'<span style="color:#94a3b8;font-weight:600;">SLA</span>'
                f'<span style="color:{sla_col};font-weight:700;">{sla_val}</span>'
                f'<span style="color:#94a3b8;font-weight:600;">Status</span>'
                f'<span style="color:#334155;">{ticket.get("status","—")}</span>'
                f'<span style="color:#94a3b8;font-weight:600;">Private</span>'
                f'<span style="color:{priv_col};">{priv_txt}</span>'
                '</div></div>',
                unsafe_allow_html=True,
            )

        # ── Manager routing decision ──────────────────────────────────────
        pool = assignee_pool(recommended_department, recommended_level)
        default_assignee = final_assignee or recommended_assignee
        options = []
        for name in [default_assignee] + pool:
            if name and name not in options:
                options.append(name)
        safe_key = f"{ticket_id}_{scenario_id}_{row_id}".replace("-", "_")
        chosen = default_assignee

        ai_hint = (
            f'<div style="font-size:0.8rem;color:#0c4a6e;margin-bottom:0.4rem;">'
            f'AI suggested: <b>{recommended_assignee or "—"}</b> '
            f'({recommended_department or "—"}, {recommended_level or "—"}). '
            'Override below if needed.</div>'
            if recommended_assignee else ""
        )
        st.markdown(
            '<div style="background:#f0f9ff;border:1.5px solid #7dd3fc;border-radius:8px;'
            'padding:0.7rem 0.9rem;margin:0.4rem 0 0.6rem 0;">'
            '<div style="font-size:0.72rem;text-transform:uppercase;color:#0369a1;'
            'font-weight:700;letter-spacing:0.04em;margin-bottom:0.35rem;">'
            '👔 Manager Final Routing Decision</div>'
            + ai_hint + '</div>',
            unsafe_allow_html=True,
        )

        if options:
            widget_key = f"final_assignee_{safe_key}"
            chosen = st.selectbox(
                "Final assignee (human decision)",
                options=options,
                index=options.index(default_assignee) if default_assignee in options else 0,
                key=widget_key,
                on_change=persist_final_assignee,
                args=(ticket_id, scenario_id, widget_key),
                help="Defaults to the AI recommendation. Changing this records your final routing decision.",
            )
        else:
            st.caption("No staff pool is defined for this ticket's department and level.")

        # ── Editable Escalation Draft (Feature 2) ────────────────────────
        draft_key = f"editable_draft_{safe_key}"
        # Seed the text area on first render; preserve user edits across re-renders.
        if draft_key not in st.session_state:
            st.session_state[draft_key] = content

        with st.expander("✏️ Review & Edit Escalation Draft", expanded=True):
            st.markdown(
                '<div style="font-size:0.78rem;color:#6366f1;font-weight:600;'
                'margin-bottom:0.4rem;">'
                '💡 You may edit the AI-generated draft below. '
                'Your changes will be saved on approval and exported in the .txt report.'
                '</div>',
                unsafe_allow_html=True,
            )
            edited_draft = st.text_area(
                "Escalation draft (editable)",
                value=st.session_state[draft_key],
                height=280,
                key=f"textarea_{safe_key}",
                label_visibility="collapsed",
                help="Edit the AI draft before approving. Changes are used for the DB write-back and the .txt export.",
            )
            # Keep session state in sync so the download button uses the latest text
            st.session_state[draft_key] = edited_draft

            # Reset to original AI draft
            if st.button(
                "↩️ Reset to AI original",
                key=f"reset_draft_{safe_key}",
                help="Discard your edits and restore the original AI-generated draft.",
            ):
                st.session_state[draft_key] = content
                st.rerun()

        with st.expander("🔍 Agent Decision Reasoning", expanded=False):
            st.markdown(
                '<div style="background:#fafafa;border-left:4px solid #6366f1;'
                'padding:0.7rem 1rem;border-radius:4px;font-size:0.88rem;color:#334155;">'
                f'{draft_row.get("decision_reason","")}'
                '</div>',
                unsafe_allow_html=True,
            )

        # ── Action buttons ────────────────────────────────────────────────
        st.markdown("<div style='margin-top:0.6rem;'></div>", unsafe_allow_html=True)
        # Always use the (possibly edited) draft from session state
        live_draft = st.session_state.get(draft_key, content)
        _, sanitization_leak = sanitization_state(live_draft, private_attachments)
        approve_col, reject_col, spacer_col = st.columns([1, 1, 2])

        with approve_col:
            approve = st.button(
                "✅ Approve & Route",
                key=f"approve_{safe_key}",
                type="primary",
                use_container_width=True,
                disabled=bool(sanitization_leak),
            )
        with reject_col:
            reject = st.button(
                "❌ Reject",
                key=f"reject_{safe_key}",
                use_container_width=True,
            )
        with spacer_col:
            if sanitization_leak:
                st.markdown(
                    '<span style="color:#b91c1c;font-size:0.82rem;font-weight:600;">'
                    '⛔ Approval blocked — sensitive data leak detected.</span>',
                    unsafe_allow_html=True,
                )

        # Feature 3: open confirmation dialog instead of writing immediately
        if approve:
            chosen = st.session_state.get(f"final_assignee_{safe_key}") or default_assignee
            if chosen and chosen != draft_row.get("final_assignee"):
                set_final_assignee(ticket_id, scenario_id, chosen)
            confirm_escalation_dialog(
                ticket_id, scenario_id, live_draft,
                chosen, recommended_department, safe_key,
            )

        if reject:
            set_approval_status(ticket_id, scenario_id, REJECTED)
            flash("error", f"{ticket_id}: escalation rejected. No note was routed.")
            st.rerun()

        # ── Export / Download button uses the live (edited) draft ─────────
        if live_draft:  # only render when there is actually a draft to export
            # Build an ad-hoc draft_row copy with the live (possibly edited) draft
            live_draft_row = dict(draft_row)
            live_draft_row["escalation_draft"] = live_draft
            report_text = build_escalation_report(live_draft_row, ticket)
            file_name = f"Escalation_Report_{ticket_id}.txt"
            st.markdown("<div style='margin-top:0.5rem;'></div>",
                        unsafe_allow_html=True)
            st.markdown(
                f"""
                <style>
                div[class*="st-key-dl_{safe_key}"] button {{
                    background: linear-gradient(135deg,#0891b2,#0e7490) !important;
                    border-color: #0e7490 !important;
                    color: #fff !important;
                    font-weight: 700 !important;
                    box-shadow: 0 2px 8px rgba(14,116,144,0.28) !important;
                    width: 100%;
                }}
                div[class*="st-key-dl_{safe_key}"] button:hover {{
                    background: linear-gradient(135deg,#0e7490,#155e75) !important;
                }}
                </style>
                """,
                unsafe_allow_html=True,
            )
            with st.container(key=f"dl_{safe_key}"):
                st.download_button(
                    label="📥 Export Escalation Report (.txt)",
                    data=report_text,
                    file_name=file_name,
                    mime="text/plain",
                    key=f"download_{safe_key}",
                    help=(
                        f"Download the escalation report for {ticket_id}. "
                        "Exports your current (edited) draft text."
                    ),
                    use_container_width=True,
                )


def render_execution_log(tickets):
    st.markdown(
        '<div style="display:flex;align-items:center;gap:0.6rem;margin-bottom:0.4rem;">'
        '<div style="font-size:1.25rem;font-weight:800;color:#0f172a;">📜 Execution Log</div>'
        '<span style="background:#dcfce7;color:#166534;border:1px solid #86efac;'
        'padding:0.1rem 0.5rem;border-radius:12px;font-size:0.75rem;font-weight:700;">'
        'APPROVED ESCALATIONS</span></div>',
        unsafe_allow_html=True,
    )
    st.caption(
        f"All approved escalations. 'Approved At' comes from the note posted into `{os.path.basename(TICKETS_DB)}`."
    )

    approved = load_drafts(APPROVED)
    if not approved:
        st.markdown(
            '<div style="background:#f8fafc;border:1.5px dashed #cbd5e1;border-radius:10px;'
            'padding:1.5rem;text-align:center;color:#94a3b8;font-size:0.95rem;">'
            '📭 No escalations have been approved yet. Review the cards above to get started.'
            '</div>',
            unsafe_allow_html=True,
        )
        return

    rows = []
    for row in approved:
        ticket_id = row["ticket_id"]
        ticket = tickets.get(ticket_id, {})
        approved_at = ticket.get("approved_at") or "-"
        evidence = make_evidence_link(ticket_id, row["scenario_id"], approved_at)
        rows.append(
            {
                "Ticket ID": ticket_id,
                "Subject": ticket.get("subject", "(unknown)"),
                "Department": row.get("recommended_department") or ticket.get("department") or "-",
                "Scenario": row["scenario_id"],
                "Final Assignee": row.get("final_assignee") or row.get("recommended_assignee") or "-",
                "Approved At": approved_at,
                "Evidence Ref": evidence["ref"],
                "Evidence Link": evidence["url"],
            }
        )

    st.dataframe(
        rows,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Evidence Link": st.column_config.LinkColumn("Evidence Link", display_text="🔗 Open ticket"),
            "Final Assignee": st.column_config.TextColumn("Final Assignee", width="medium"),
        },
    )


DASHBOARD_CSS = """
<style>
.approval-hero {
    background: linear-gradient(135deg,#0f172a 0%,#1e1b4b 40%,#312e81 75%,#4338ca 100%);
    padding: 1.6rem 2rem;
    border-radius: 12px;
    color: #fff;
    box-shadow: 0 8px 32px -4px rgba(15,23,42,0.35);
    margin-bottom: 1.8rem;
    position: relative;
    overflow: hidden;
}
.approval-hero::before {
    content: "";
    position: absolute;
    top: -50%; right: -10%;
    width: 300px; height: 300px;
    background: radial-gradient(circle,rgba(129,140,248,0.18) 0%,transparent 70%);
    border-radius: 50%;
    pointer-events: none;
}
.approval-hero h1 {
    color:#fff !important; border-bottom:none !important;
    margin:0 0 0.3rem 0; font-size:2rem; font-weight:800; letter-spacing:-0.025em;
}
.approval-hero p { color:rgba(255,255,255,0.82); margin:0; font-size:0.96rem; }

.kpi-card {
    border-radius:12px; padding:1.1rem 1.3rem; color:#fff;
    box-shadow:0 4px 16px rgba(0,0,0,0.10);
    transition:transform 0.15s ease, box-shadow 0.15s ease;
}
.kpi-card:hover { transform:translateY(-3px); box-shadow:0 8px 24px rgba(0,0,0,0.14); }
.kpi-card-pending  { background:linear-gradient(135deg,#f59e0b,#b45309); }
.kpi-card-approved { background:linear-gradient(135deg,#10b981,#047857); }
.kpi-card-rejected { background:linear-gradient(135deg,#ef4444,#b91c1c); }
.kpi-card-total    { background:linear-gradient(135deg,#6366f1,#4338ca); }
.kpi-label { font-size:0.75rem; text-transform:uppercase; font-weight:700;
             letter-spacing:0.06em; opacity:0.88; margin-bottom:0.2rem; }
.kpi-val   { font-size:2rem; font-weight:800; line-height:1; }

div[class*="st-key-approve_"] button {
    background:linear-gradient(135deg,#10b981,#047857) !important;
    border-color:#059669 !important; color:#fff !important;
    font-weight:700 !important; box-shadow:0 2px 8px rgba(16,185,129,0.3) !important;
}
div[class*="st-key-approve_"] button:hover {
    background:linear-gradient(135deg,#059669,#065f46) !important;
}
div[class*="st-key-reject_"] button {
    background:linear-gradient(135deg,#ef4444,#b91c1c) !important;
    border-color:#dc2626 !important; color:#fff !important;
    font-weight:700 !important; box-shadow:0 2px 8px rgba(239,68,68,0.28) !important;
}
div[class*="st-key-reject_"] button:hover {
    background:linear-gradient(135deg,#dc2626,#991b1b) !important;
}

/* Legacy badge classes kept for compatibility */
.z-badge {
    display:inline-block; padding:0.2rem 0.65rem;
    margin:0.15rem 0.4rem 0.55rem 0; border-radius:999px;
    font-size:0.78rem; font-weight:600; border:1px solid transparent;
}
.z-badge-ok   { background:#d7f5dd; color:#0f5132; border-color:#0f5132; }
.z-badge-bad  { background:#fbdcd7; color:#842029; border-color:#842029; }
.z-badge-info { background:#e2e3f5; color:#2b2f6b; border-color:#2b2f6b; }
</style>
"""


# --------------------------------------------------------------------------- #
# Autopilot - 60-second agent auto-sweep
# --------------------------------------------------------------------------- #

@st.fragment(run_every=AUTOPILOT_INTERVAL)
def render_autopilot_panel():
    """Sidebar toggle plus the recurring agent sweep.

    The fragment re-runs on its own 60-second timer. On each tick it evaluates
    every ticket through ``agent_core.sweep()`` and then triggers a full app
    rerun so the review cards and metrics reflect the freshly drafted
    escalations. A monotonic wall-clock guard stops the render that immediately
    follows our own ``st.rerun`` from re-triggering (which would otherwise spin).
    """
    with st.sidebar:
        st.markdown(
            '<div style="background:linear-gradient(135deg,#0f172a,#1e293b);'
            'padding:1rem;border-radius:8px;color:white;margin-bottom:1rem;">'
            '<div style="font-size:1.15rem;font-weight:700;">🤖 Autopilot Controls</div>'
            '<div style="font-size:0.75rem;color:#94a3b8;margin-top:0.2rem;">'
            'Auto-sweep every 60 seconds</div></div>',
            unsafe_allow_html=True,
        )
        enabled = st.toggle(
            "🤖 Autopilot Mode",
            value=False,
            key="autopilot_enabled",
            help=(
                f"Re-run the escalation agent every {AUTOPILOT_INTERVAL_SECONDS} seconds so "
                "newly breaching tickets are drafted instantly."
            ),
        )
        if enabled:
            st.markdown(
                '<span style="display:inline-flex;align-items:center;'
                'background:#dcfce7;color:#166534;border:1.5px solid #86efac;'
                'padding:0.25rem 0.7rem;border-radius:20px;font-size:0.82rem;font-weight:700;">'
                '🟢 LIVE — AUTO-SWEEP ACTIVE</span>',
                unsafe_allow_html=True,
            )
            last = st.session_state.get("autopilot_last")
            if last:
                st.markdown(
                    f'<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:6px;'
                    f'padding:0.5rem 0.7rem;margin-top:0.4rem;font-size:0.8rem;color:#166534;">'
                    f'⏰ <b>Last sweep:</b> {last["at"]}<br>'
                    f'📊 <b>Evaluated:</b> {last["evaluated"]} ticket(s)<br>'
                    f'🚀 <b>Escalated:</b> {last.get("escalated",0)}<br>'
                    f'🔧 <b>Engine:</b> {last["mode"]}</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.caption(f"Sweeping every {AUTOPILOT_INTERVAL_SECONDS} seconds…")
        else:
            st.markdown(
                '<span style="display:inline-flex;align-items:center;'
                'background:#fef2f2;color:#b91c1c;border:1.5px solid #fca5a5;'
                'padding:0.22rem 0.65rem;border-radius:20px;font-size:0.8rem;font-weight:700;">'
                '⭕ OFFLINE — manual runs only</span>',
                unsafe_allow_html=True,
            )
            st.caption("Enable Autopilot to continuously draft escalations.")

        # ── Manual Run button (Feature 1 - triggers progress-bar sweep) ──
        st.markdown("<div style='margin-top:0.6rem;'></div>", unsafe_allow_html=True)
        if st.button(
            "🚀 Run Agent Now",
            key="manual_run_btn",
            use_container_width=True,
            help="Manually trigger a full agent sweep with a live progress bar.",
            type="primary",
        ):
            st.session_state["trigger_manual_sweep"] = True

    if not enabled:
        st.session_state["autopilot_last_tick"] = None
        return

    now = time.monotonic()
    last_tick = st.session_state.get("autopilot_last_tick")
    if last_tick is not None and (now - last_tick) < AUTOPILOT_INTERVAL_SECONDS:
        return

    st.session_state["autopilot_last_tick"] = now
    st.session_state["autopilot_last"] = agent_core.sweep()
    st.rerun()


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

def main():
    st.set_page_config(
        page_title="Escalation Approval Dashboard", page_icon="🛡️", layout="wide"
    )
    agent_core.apply_enterprise_theme()
    st.markdown(DASHBOARD_CSS, unsafe_allow_html=True)

    # ── Hero banner ──────────────────────────────────────────────────────────
    st.markdown(
        '<div class="approval-hero">'
        '<h1>🛡️ Escalation Approval Dashboard</h1>'
        '<p>Human-in-the-loop review stage — approve or reject AI-drafted escalation packs, '
        'finalise routing, and post approved notes back into the helpdesk.</p>'
        '</div>',
        unsafe_allow_html=True,
    )

    if not drafts_db_ready():
        st.error(
            f"`{os.path.basename(DRAFTS_DB)}` was not found or has no `escalation_drafts` table.\n\n"
            "Generate the drafts first:\n\n"
            "```\npython agent_core.py\n```"
        )
        return

    render_autopilot_panel()

    # ── Sidebar: DB status ───────────────────────────────────────────────────
    with st.sidebar:
        st.divider()
        st.markdown("### 🗄️ Database Status")
        drafts_ok = drafts_db_ready()
        tickets_ok = os.path.exists(TICKETS_DB)
        st.markdown(
            f'<div style="font-size:0.85rem;">'
            f'{"✅" if drafts_ok else "❌"} <b>escalation_drafts.db</b><br>'
            f'{"✅" if tickets_ok else "❌"} <b>tickets.db</b>'
            '</div>',
            unsafe_allow_html=True,
        )
        st.caption(f"Helpdesk: `{HELPDESK_BASE_URL}`")

    show_flash()

    counts = count_by_status()
    tickets = load_ticket_index()

    # ── KPI tiles ────────────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    for col, css_cls, label, val in [
        (c1, "kpi-card-pending",  "⏳ Pending Review", counts["pending"]),
        (c2, "kpi-card-approved", "✅ Approved",       counts["approved"]),
        (c3, "kpi-card-rejected", "❌ Rejected",       counts["rejected"]),
        (c4, "kpi-card-total",    "📊 Total Drafts",   counts["total"]),
    ]:
        with col:
            st.markdown(
                f'<div class="kpi-card {css_cls}">'
                f'<div class="kpi-label">{label}</div>'
                f'<div class="kpi-val">{val}</div>'
                '</div>',
                unsafe_allow_html=True,
            )

    if not tickets:
        st.warning(
            f"`{os.path.basename(TICKETS_DB)}` is unavailable — approvals cannot be routed back."
        )

    # ── Manual sweep with progress bar (Feature 1) ─────────────────────────
    if st.session_state.pop("trigger_manual_sweep", False):
        summary = render_progress_sweep()
        if summary and summary.get("ok"):
            st.session_state["autopilot_last"] = summary
            flash(
                "success",
                f"Agent sweep complete — {summary['evaluated']} ticket(s) evaluated, "
                f"{summary['escalated']} escalation pack(s) prepared "
                f"(engine: {summary['mode']}).",
            )
        else:
            flash("error", "Agent sweep failed. Check the database and try again.")
        st.rerun()

    # ── Pending review queue ──────────────────────────────────────────────────
    st.markdown("<div style='margin-top:1.5rem;'></div>", unsafe_allow_html=True)
    st.markdown(
        '<div style="font-size:1.3rem;font-weight:800;color:#0f172a;margin-bottom:0.3rem;">'
        '🔎 Pending Review Queue</div>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Only drafts awaiting a human decision are shown. "
        "Excluded (TC2) and duplicate-prevented (TC4) outcomes are 'Not Applicable' "
        "and never require review."
    )

    pending = load_drafts(PENDING)
    if not pending:
        st.markdown(
            '<div style="background:linear-gradient(135deg,#ecfdf5,#f0fdf4);'
            'border:1.5px solid #86efac;border-radius:12px;padding:2rem;text-align:center;">'
            '<div style="font-size:2rem;margin-bottom:0.5rem;">🎉</div>'
            '<div style="font-size:1.1rem;font-weight:700;color:#166534;">All Clear!</div>'
            '<div style="color:#4ade80;margin-top:0.3rem;">No drafts are awaiting approval.</div>'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div style="background:#fffbeb;border:1.5px solid #fcd34d;border-radius:8px;'
            f'padding:0.6rem 1rem;margin-bottom:1rem;font-size:0.88rem;color:#92400e;">'
            f'⚠️ <b>{len(pending)} draft(s)</b> are awaiting your decision. '
            'Review each card carefully before approving or rejecting.</div>',
            unsafe_allow_html=True,
        )
        for row in pending:
            render_review_card(row, tickets)

    st.divider()
    render_execution_log(tickets)

    port = st.get_option("server.port")
    st.divider()
    st.caption(f"🖥️ Running on port {port}.")
    if port != 8502:
        st.warning(
            "This dashboard is expected on port 8502: "
            "`streamlit run approval_dashboard.py --server.port 8502`"
        )


if __name__ == "__main__":
    main()
