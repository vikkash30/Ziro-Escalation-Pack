"""Mock Helpdesk web application.

A small Streamlit + SQLite app used as a test harness for escalation workflows.
The database (``tickets.db``) is created and pre-populated automatically on
startup with the four official hackathon test cases (TC1 - TC4).

Run with:
    streamlit run mock_helpdesk.py
"""

import hashlib
import os
import sqlite3
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

import agent_core  # shared SLA timing, sanitization and the enterprise theme

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "tickets.db")

TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"

PRIORITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}

UNASSIGNED_LABELS = {"", "unassigned", "none", "n/a", "-"}


def work_progress(ticket):
    """Return (pct: int, label: str) representing ticket work completion.

    Unassigned tickets are always 0 % / Pending Assignment.
    Assigned tickets get a deterministic but realistic-looking percentage
    derived from the ticket_id hash — seeded so the value is stable across
    re-renders and page refreshes (10 % – 85 % range).

    No database column is required; this is a pure display helper.
    """
    owner = (ticket["owner"] or "").strip()
    if owner.lower() in UNASSIGNED_LABELS:
        return 0, "pending"

    digest = hashlib.md5(ticket["ticket_id"].encode()).hexdigest()
    # Map first 2 hex digits (0-255) → 10-85
    raw = int(digest[:2], 16)          # 0 – 255
    pct = 10 + int(raw / 255 * 75)    # 10 – 85

    # Escalated tickets show higher completion to reflect closed-loop work.
    if ticket["status"] == "Escalated":
        pct = min(pct + 10, 95)
    elif ticket["status"] in ("Resolved", "Closed"):
        pct = 100

    return pct, "active"


def render_work_progress(ticket):
    """Paint the Work Progress metric directly below the KPI stat tiles."""
    pct, state = work_progress(ticket)

    if state == "pending":
        st.markdown(
            '<div style="background:#f8fafc;border:1.5px solid #cbd5e1;border-radius:8px;'
            'padding:0.65rem 1rem;margin:0.6rem 0 0.2rem 0;">'
            '<div style="font-size:0.72rem;text-transform:uppercase;color:#64748b;'
            'font-weight:700;letter-spacing:0.04em;margin-bottom:0.35rem;">'
            '🔧 Work Progress</div>'
            '<div style="display:flex;align-items:center;gap:0.7rem;">'
            '<span style="display:inline-flex;align-items:center;'
            'background:#f1f5f9;color:#475569;border:1.5px solid #94a3b8;'
            'padding:0.2rem 0.7rem;border-radius:20px;font-size:0.82rem;font-weight:700;">'
            '⏸️ Pending Assignment (0%)</span>'
            '<span style="font-size:0.78rem;color:#94a3b8;">'
            'No owner assigned yet — work cannot begin.</span>'
            '</div></div>',
            unsafe_allow_html=True,
        )
        return

    # Choose a colour band matching the completion tier
    if pct >= 80:
        bar_color = "#10b981"   # green
        pill_bg   = "linear-gradient(135deg,#10b981,#047857)"
        status_label = f"🟢 {pct}% Complete"
    elif pct >= 50:
        bar_color = "#3b82f6"   # blue
        pill_bg   = "linear-gradient(135deg,#3b82f6,#1d4ed8)"
        status_label = f"🚧 {pct}% Complete"
    else:
        bar_color = "#f59e0b"   # amber
        pill_bg   = "linear-gradient(135deg,#f59e0b,#b45309)"
        status_label = f"⚠️ {pct}% Complete"

    st.markdown(
        '<div style="background:#f8fafc;border:1.5px solid #e2e8f0;border-radius:8px;'
        'padding:0.65rem 1rem;margin:0.6rem 0 0.2rem 0;">'
        '<div style="font-size:0.72rem;text-transform:uppercase;color:#64748b;'
        'font-weight:700;letter-spacing:0.04em;margin-bottom:0.45rem;">'
        '🔧 Work Progress</div>'
        '<div style="display:flex;align-items:center;gap:0.75rem;flex-wrap:wrap;">'
        f'<span style="display:inline-flex;align-items:center;background:{pill_bg};'
        'color:#fff;padding:0.2rem 0.7rem;border-radius:20px;font-size:0.82rem;'
        f'font-weight:700;box-shadow:0 1px 4px rgba(0,0,0,0.12);">{status_label}</span>'
        f'<span style="font-size:0.78rem;color:#64748b;">Assigned to '
        f'<b>{ticket["owner"]}</b></span>'
        '</div></div>',
        unsafe_allow_html=True,
    )
    st.progress(pct / 100, text=f"**{pct}% complete** — {ticket['owner']} is actively working this ticket.")


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #

def get_connection():
    """Open a new SQLite connection with row access by column name."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def query(sql, params=()):
    """Run a SELECT and return all rows."""
    with get_connection() as conn:
        return conn.execute(sql, params).fetchall()


def query_one(sql, params=()):
    """Run a SELECT and return the first row (or None)."""
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    """Run an INSERT/UPDATE/DELETE and commit."""
    with get_connection() as conn:
        conn.execute(sql, params)
        conn.commit()


def db_exists():
    """True when the database file is present and contains the tickets table."""
    if not os.path.exists(DB_PATH):
        return False
    try:
        row = query_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tickets'"
        )
        return row is not None
    except sqlite3.DatabaseError:
        return False


# --------------------------------------------------------------------------- #
# Schema + seed data
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id     TEXT PRIMARY KEY,
    test_case     TEXT NOT NULL,
    subject       TEXT NOT NULL,
    status        TEXT NOT NULL,
    priority      TEXT NOT NULL,
    sla_status    TEXT NOT NULL,
    sla_due_at    TEXT,
    owner         TEXT NOT NULL,
    requester     TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    description   TEXT NOT NULL,
    department    TEXT
);

CREATE TABLE IF NOT EXISTS timeline (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    author      TEXT NOT NULL,
    kind        TEXT NOT NULL,   -- note | system | escalation | email
    content     TEXT NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES tickets (ticket_id)
);

CREATE TABLE IF NOT EXISTS attachments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   TEXT NOT NULL,
    filename    TEXT NOT NULL,
    visibility  TEXT NOT NULL,   -- public | private
    uploaded_at TEXT NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES tickets (ticket_id)
);
"""


def _ts(hours=0, minutes=0):
    """Timestamp string relative to now."""
    return (datetime.now() + timedelta(hours=hours, minutes=minutes)).strftime(TIMESTAMP_FMT)


# Owning department seeded per official test case. Drives the colour coding in
# both the helpdesk and the approval dashboard, and mirrors what the routing
# agent picks for the same ticket.
SEED_DEPARTMENTS = {
    "TCK-101": "Identity and Access Management",
    "TCK-102": "End User Support",
    "TCK-103": "Cyber/Security Operations",
    "TCK-104": "Database Support",
}


def build_seed_data():
    """Return (tickets, timeline, attachments) for the 4 official test cases."""
    now = datetime.now()

    tickets = [
        {
            "ticket_id": "TCK-101",
            "test_case": "TC1",
            "subject": "VPN client fails to authenticate after certificate rotation",
            "status": "Open",
            "priority": "High",
            "sla_status": "Breached",
            "sla_due_at": _ts(hours=-3),
            "owner": "Priya Nair",
            "requester": "Arjun Mehta",
            "created_at": _ts(hours=-27),
            "updated_at": _ts(hours=-3, minutes=-12),
            "department": SEED_DEPARTMENTS["TCK-101"],
            "description": (
                "Corporate VPN rejects logins after the monthly certificate rotation. "
                "Affects the Bangalore branch (about 40 users). First response SLA was "
                "missed and the ticket is still unassigned to an escalation owner."
            ),
        },
        {
            "ticket_id": "TCK-102",
            "test_case": "TC2",
            "subject": "Laptop replacement shipment stuck - awaiting customer confirmation",
            "status": "Waiting on Customer",
            "priority": "Medium",
            "sla_status": "Clock Paused",
            "sla_due_at": None,
            "owner": "Daniel Okafor",
            "requester": "Sneha Rao",
            "created_at": _ts(hours=-50),
            "updated_at": _ts(hours=-6),
            "department": SEED_DEPARTMENTS["TCK-102"],
            "description": (
                "Replacement laptop was approved and dispatched. The SLA clock is paused "
                "while we wait for the customer to confirm their delivery address."
            ),
        },
        {
            "ticket_id": "TCK-103",
            "test_case": "TC3",
            "subject": "Production API returns 500s on the billing endpoint",
            "status": "Open",
            "priority": "Critical",
            "sla_status": "Breached",
            "sla_due_at": _ts(hours=-9),
            "owner": "Marcus Feld",
            "requester": "Leena Iyer",
            "created_at": _ts(hours=-20),
            "updated_at": _ts(hours=-2, minutes=-30),
            "department": SEED_DEPARTMENTS["TCK-103"],
            "description": (
                "Billing endpoint intermittently returns HTTP 500. Root cause suspected in "
                "the token refresh path. Ticket contains a restricted attachment that must "
                "never be forwarded outside the internal escalation flow."
            ),
        },
        {
            "ticket_id": "TCK-104",
            "test_case": "TC4",
            "subject": "Payroll data mismatch for the October cycle",
            "status": "Escalated",
            "priority": "High",
            "sla_status": "Breached",
            "sla_due_at": _ts(hours=-18),
            "owner": "Priya Nair",
            "requester": "Finance Operations",
            "created_at": _ts(hours=-46),
            "updated_at": _ts(hours=-11),
            "department": SEED_DEPARTMENTS["TCK-104"],
            "description": (
                "Payroll totals do not reconcile with the source ledger. Already escalated "
                "once to the payroll vendor; a duplicate escalation must not be raised."
            ),
        },
    ]

    timeline = [
        # TC1 - timeline notes present, SLA already breached.
        ("TCK-101", _ts(hours=-27), "Arjun Mehta", "note",
         "Submitted ticket after three failed VPN logins on the new client build."),
        ("TCK-101", _ts(hours=-26, minutes=-30), "Helpdesk Bot", "system",
         "Auto-triage completed. Classified as Network / VPN. Priority set to High."),
        ("TCK-101", _ts(hours=-24), "Priya Nair", "note",
         "Confirmed the certificate authority chain changed on the 12th. Requesting the "
         "updated profile from the security team."),
        ("TCK-101", _ts(hours=-8), "Priya Nair", "note",
         "Security team has not responded. This is now past the first-response SLA."),
        ("TCK-101", _ts(hours=-3, minutes=-12), "SLA Monitor", "system",
         "First response SLA missed. Ticket breached by 3h 00m and is a candidate for escalation."),

        # TC2 - clock paused, waiting on the customer.
        ("TCK-102", _ts(hours=-50), "Sneha Rao", "email",
         "Customer reported the replacement laptop has not arrived at the office."),
        ("TCK-102", _ts(hours=-42), "Daniel Okafor", "note",
         "Dispatch confirmed the parcel is held at the local hub pending address verification."),
        ("TCK-102", _ts(hours=-6), "Daniel Okafor", "system",
         "Status changed to 'Waiting on Customer'. SLA clock paused pending customer reply."),

        # TC3 - sensitive note must be handled carefully.
        ("TCK-103", _ts(hours=-20), "Leena Iyer", "note",
         "Billing endpoint failures observed from the production tenant since 03:15 UTC."),
        ("TCK-103", _ts(hours=-14), "Marcus Feld", "note",
         "Reproduced with a rotated service token. Captured the request trace below."),
        ("TCK-103", _ts(hours=-2, minutes=-30), "Marcus Feld", "note",
         "[Private Attachment: auth_tokens.pem]"),
        ("TCK-103", _ts(hours=-9), "SLA Monitor", "system",
         "Critical priority SLA breached. Immediate escalation is required."),

        # TC4 - an escalation already exists for this ticket.
        ("TCK-104", _ts(hours=-46), "Finance Operations", "email",
         "October payroll totals do not reconcile with the general ledger."),
        ("TCK-104", _ts(hours=-30), "Priya Nair", "note",
         "Variance traced to a duplicate adjustment entry in the vendor feed."),
        ("TCK-104", _ts(hours=-22), "Priya Nair", "escalation",
         "ESCALATION RAISED -> Payroll Vendor Tier-2. Reason: data integrity risk on a "
         "time-critical payroll cycle. Escalation ref: ESC-2024-0417."),
        ("TCK-104", _ts(hours=-11), "Payroll Vendor Tier-2", "system",
         "Escalation acknowledged. Vendor is investigating the adjustment feed."),
    ]

    attachments = [
        ("TCK-101", "vpn_error_log.txt", "public", _ts(hours=-25)),
        ("TCK-102", "dispatch_receipt.pdf", "public", _ts(hours=-41)),
        ("TCK-103", "auth_tokens.pem", "private", _ts(hours=-2, minutes=-30)),
        ("TCK-103", "api_trace_500.txt", "public", _ts(hours=-13)),
        ("TCK-104", "payroll_variance.xlsx", "public", _ts(hours=-45)),
    ]

    return tickets, timeline, attachments


def init_db(force=False):
    """Create the schema and seed the official test cases.

    Safe to call on every startup: the database is only seeded when it is empty
    (or when ``force`` is set by the 'Reset to Default Test Data' button).
    """
    with get_connection() as conn:
        if force:
            conn.executescript(
                "DROP TABLE IF EXISTS attachments;"
                "DROP TABLE IF EXISTS timeline;"
                "DROP TABLE IF EXISTS tickets;"
            )
            conn.commit()

        conn.executescript(SCHEMA)
        conn.commit()

        # Additive migration: a database created before department routing existed
        # gains the column without losing a single row (TC1-TC4 included).
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
        if "department" not in existing:
            conn.execute("ALTER TABLE tickets ADD COLUMN department TEXT")
            conn.commit()

        already_seeded = conn.execute("SELECT COUNT(*) AS n FROM tickets").fetchone()["n"]
        if already_seeded:
            _backfill_seed_departments(conn)
            return

        tickets, timeline, attachments = build_seed_data()

        conn.executemany(
            """
            INSERT INTO tickets (
                ticket_id, test_case, subject, status, priority, sla_status,
                sla_due_at, owner, requester, created_at, updated_at, description, department
            ) VALUES (
                :ticket_id, :test_case, :subject, :status, :priority, :sla_status,
                :sla_due_at, :owner, :requester, :created_at, :updated_at, :description,
                :department
            )
            """,
            tickets,
        )
        conn.executemany(
            """
            INSERT INTO timeline (ticket_id, created_at, author, kind, content)
            VALUES (?, ?, ?, ?, ?)
            """,
            timeline,
        )
        conn.executemany(
            """
            INSERT INTO attachments (ticket_id, filename, visibility, uploaded_at)
            VALUES (?, ?, ?, ?)
            """,
            attachments,
        )
        conn.commit()


def _backfill_seed_departments(conn):
    """Stamp the seeded department onto the official test cases when it is unset.

    Only rows whose ``department`` is still NULL are touched, so a department set
    by the helpdesk or the routing agent is never overwritten.
    """
    for ticket_id, department in SEED_DEPARTMENTS.items():
        conn.execute(
            "UPDATE tickets SET department = ? "
            "WHERE ticket_id = ? AND (department IS NULL OR department = '')",
            (department, ticket_id),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #

def fetch_tickets():
    rows = query("SELECT * FROM tickets")
    rows = sorted(rows, key=lambda r: (PRIORITY_ORDER.get(r["priority"], 9), r["ticket_id"]))
    return rows


def fetch_ticket(ticket_id):
    return query_one("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,))


def fetch_timeline(ticket_id):
    return query(
        "SELECT * FROM timeline WHERE ticket_id = ? ORDER BY created_at ASC, id ASC",
        (ticket_id,),
    )


def fetch_attachments(ticket_id):
    return query(
        "SELECT * FROM attachments WHERE ticket_id = ? ORDER BY uploaded_at ASC",
        (ticket_id,),
    )


def has_escalation(ticket_id):
    row = query_one(
        "SELECT COUNT(*) AS n FROM timeline WHERE ticket_id = ? AND kind = 'escalation'",
        (ticket_id,),
    )
    return bool(row and row["n"])


def get_stats():
    """Sidebar quick stats.

    'Pending Escalations' = tickets that still look like they need an escalation,
    i.e. they are High/Critical or SLA-breached and no escalation note exists yet.
    """
    tickets = fetch_tickets()
    total = len(tickets)
    breached = sum(1 for t in tickets if t["sla_status"] == "Breached")
    pending = sum(
        1
        for t in tickets
        if (t["sla_status"] == "Breached" or t["priority"] in ("High", "Critical"))
        and not has_escalation(t["ticket_id"])
    )
    return {"total": total, "breached": breached, "pending_escalations": pending}


def add_escalation_note(ticket_id, author, reason, note, escalate_flag):
    """Append an internal escalation note (and optional escalation event)."""
    now = datetime.now().strftime(TIMESTAMP_FMT)
    content = f"Reason: {reason}\n\n{note}" if reason else note

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO timeline (ticket_id, created_at, author, kind, content)
            VALUES (?, ?, ?, 'escalation', ?)
            """,
            (ticket_id, now, author, content),
        )
        new_status = None
        if escalate_flag:
            new_status = "Escalated"
            conn.execute(
                "UPDATE tickets SET status = ?, updated_at = ? WHERE ticket_id = ?",
                (new_status, now, ticket_id),
            )
        else:
            conn.execute(
                "UPDATE tickets SET updated_at = ? WHERE ticket_id = ?",
                (now, ticket_id),
            )
        conn.commit()
    return new_status


# --------------------------------------------------------------------------- #
# Routing helpers (query params so an automated browser can target a ticket)
# --------------------------------------------------------------------------- #

def get_selected_param():
    """Read ?ticket=TCK-101 from the URL (works on old and new Streamlit)."""
    try:
        return st.query_params.get("ticket")
    except Exception:
        pass
    try:
        values = st.experimental_get_query_params().get("ticket")
        return values[0] if values else None
    except Exception:
        return None


def set_selected_param(ticket_id):
    """Write ?ticket=TCK-101 into the URL."""
    try:
        st.query_params["ticket"] = ticket_id
        return
    except Exception:
        pass
    try:
        st.experimental_set_query_params(ticket=ticket_id)
    except Exception:
        pass


def clear_selected_param():
    try:
        if "ticket" in st.query_params:
            del st.query_params["ticket"]
        return
    except Exception:
        pass
    try:
        st.experimental_set_query_params()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

SLA_BADGE = {
    "Breached": "🚨 Breached",
    "Clock Paused": "⏸️ Clock Paused",
    "Healthy": "✅ Within SLA",
}

PRIORITY_BADGES = {
    "Critical": {"bg": "linear-gradient(135deg, #ef4444 0%, #b91c1c 100%)", "color": "#ffffff", "icon": "🔥"},
    "High": {"bg": "linear-gradient(135deg, #f97316 0%, #c2410c 100%)", "color": "#ffffff", "icon": "⚡"},
    "Medium": {"bg": "linear-gradient(135deg, #f59e0b 0%, #d97706 100%)", "color": "#ffffff", "icon": "⚠️"},
    "Low": {"bg": "linear-gradient(135deg, #10b981 0%, #047857 100%)", "color": "#ffffff", "icon": "🟢"},
}

STATUS_BADGES = {
    "Open": {"bg": "#eff6ff", "color": "#1d4ed8", "border": "#3b82f6", "icon": "📬"},
    "Waiting on Customer": {"bg": "#fffbeb", "color": "#b45309", "border": "#f59e0b", "icon": "⏳"},
    "Escalated": {"bg": "#fef2f2", "color": "#b91c1c", "border": "#ef4444", "icon": "🚀"},
    "Resolved": {"bg": "#ecfdf5", "color": "#047857", "border": "#10b981", "icon": "✅"},
    "Closed": {"bg": "#f3f4f6", "color": "#4b5563", "border": "#9ca3af", "icon": "🔒"},
}

HELPDESK_COLORFUL_CSS = """
<style>
/* Modern colorful theme injection */
.main-header-banner {
    background: linear-gradient(135deg, #1e3a8a 0%, #3b82f6 50%, #06b6d4 100%);
    padding: 1.5rem 1.8rem;
    border-radius: 12px;
    color: #ffffff;
    box-shadow: 0 8px 24px -4px rgba(30, 58, 138, 0.25);
    margin-bottom: 1.8rem;
    position: relative;
    overflow: hidden;
}
.main-header-banner h1 {
    color: #ffffff !important;
    border-bottom: none !important;
    margin: 0;
    font-size: 1.9rem;
    font-weight: 800;
    letter-spacing: -0.02em;
    display: flex;
    align-items: center;
    gap: 0.6rem;
}
.main-header-banner p {
    color: rgba(255, 255, 255, 0.9);
    margin: 0.4rem 0 0 0;
    font-size: 0.95rem;
}

/* Vibrant KPI metric tiles */
.kpi-tile {
    border-radius: 10px;
    padding: 1rem 1.2rem;
    color: #ffffff;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.08);
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    transition: transform 0.15s ease, box-shadow 0.15s ease;
}
.kpi-tile:hover {
    transform: translateY(-2px);
    box-shadow: 0 6px 16px rgba(0, 0, 0, 0.12);
}
.kpi-tile-blue { background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%); }
.kpi-tile-red { background: linear-gradient(135deg, #dc2626 0%, #991b1b 100%); }
.kpi-tile-amber { background: linear-gradient(135deg, #d97706 0%, #b45309 100%); }
.kpi-tile-purple { background: linear-gradient(135deg, #7c3aed 0%, #5b21b6 100%); }
.kpi-label { font-size: 0.78rem; text-transform: uppercase; font-weight: 700; letter-spacing: 0.05em; opacity: 0.9; }
.kpi-value { font-size: 1.7rem; font-weight: 800; line-height: 1.1; }

/* Enhanced Pill Badges */
.badge-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.24rem 0.65rem;
    border-radius: 9999px;
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0.01em;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}

.ticket-card-glow {
    border-radius: 10px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.05);
    transition: all 0.2s ease;
}

/* Timeline colorful styled item */
.timeline-card-note {
    border-left: 4px solid #3b82f6 !important;
    background: #f8fafc !important;
}
.timeline-card-escalation {
    border-left: 4px solid #ef4444 !important;
    background: #fef2f2 !important;
}
.timeline-card-system {
    border-left: 4px solid #8b5cf6 !important;
    background: #fbfbfe !important;
}
.timeline-card-email {
    border-left: 4px solid #10b981 !important;
    background: #f0fdf4 !important;
}
</style>
"""


def render_sidebar(tickets):
    st.sidebar.markdown(
        """
        <div style="background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); 
                    padding: 1rem; border-radius: 8px; color: white; margin-bottom: 1rem;">
            <div style="font-size: 1.25rem; font-weight: 700; display:flex; align-items:center; gap:0.4rem;">
                🛠️ Helpdesk Console
            </div>
            <div style="font-size: 0.78rem; color: #94a3b8; margin-top: 0.2rem;">
                Database: <code>""" + os.path.basename(DB_PATH) + """</code>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    stats = get_stats()
    st.sidebar.markdown("### 📊 Live Operations")
    c1, c2 = st.sidebar.columns(2)
    with c1:
        st.markdown(
            f"""<div class="kpi-tile kpi-tile-blue">
                <span class="kpi-label">Tickets</span>
                <span class="kpi-value">{stats['total']}</span>
            </div>""",
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            f"""<div class="kpi-tile kpi-tile-red">
                <span class="kpi-label">Breached</span>
                <span class="kpi-value">{stats['breached']}</span>
            </div>""",
            unsafe_allow_html=True,
        )
    st.sidebar.markdown("<div style='margin-top: 0.6rem;'></div>", unsafe_allow_html=True)
    st.sidebar.markdown(
        f"""<div class="kpi-tile kpi-tile-amber">
            <span class="kpi-label">Pending Escalation</span>
            <span class="kpi-value">{stats['pending_escalations']}</span>
        </div>""",
        unsafe_allow_html=True,
    )

    st.sidebar.divider()

    st.sidebar.subheader("🎯 Open a ticket")
    ticket_ids = [t["ticket_id"] for t in tickets]
    current = get_selected_param()
    index = ticket_ids.index(current) if current in ticket_ids else None

    if index is not None:
        st.sidebar.info("🔍 Active Ticket: `%s`" % current)

    choice = st.sidebar.selectbox(
        "Jump to ticket",
        options=["-- none --"] + ticket_ids,
        index=0 if index is None else index + 1,
        key="jump_to_ticket",
        format_func=lambda tid: tid if tid == "-- none --" else f"🎫 {tid}",
    )
    if choice != "-- none --":
        set_selected_param(choice)
    elif index is not None:
        clear_selected_param()
        st.rerun()

    st.sidebar.divider()

    st.sidebar.subheader("⚠️ Danger zone")
    if st.sidebar.button("🔄 Reset to Default Test Data", use_container_width=True):
        init_db(force=True)
        clear_selected_param()
        st.sidebar.success("Database reset to the 4 official test cases.")
        st.rerun()

    st.sidebar.caption("URL routing: `/?ticket=TCK-103`")


def build_queue_dataframe(tickets, use_policy=False):
    rows = []
    for t in tickets:
        sla = agent_core.compute_sla_state(t, use_policy=use_policy)
        prio_icon = PRIORITY_BADGES.get(t["priority"], {}).get("icon", "•")
        status_icon = STATUS_BADGES.get(t["status"], {}).get("icon", "•")
        rows.append(
            {
                "Ticket ID": t["ticket_id"],
                "Priority": f"{prio_icon} {t['priority']}",
                "Subject": t["subject"],
                "Department": t["department"] or "Unrouted",
                "Status": f"{status_icon} {t['status']}",
                "SLA Status": SLA_BADGE.get(t["sla_status"], t["sla_status"]),
                "SLA Timer": sla["table_label"],
                "Assigned Owner": f"👤 {t['owner']}",
            }
        )
    return pd.DataFrame(rows)


def render_department_board(tickets):
    """One colour-coded chip per ticket, keyed to its owning department with gradient/drop shadow."""
    chips = []
    for t in tickets:
        color = agent_core.department_color(t["department"])
        label = t["department"] or "Unrouted"
        chips.append(
            f'<span style="display:inline-flex;align-items:center;gap:0.35rem;background:{color["bg"]};'
            f'color:{color["accent"]};border:1.5px solid {color["accent"]};'
            'padding:.25rem .75rem;border-radius:20px;font-size:.82rem;font-weight:700;'
            'box-shadow: 0 2px 6px rgba(0,0,0,0.06);transition: all 0.2s ease;" '
            f'title="{t["subject"]}">🏢 <b>{t["ticket_id"]}</b> &#183; {label}</span>'
        )
    if chips:
        st.markdown(
            '<div style="margin: 0.6rem 0 0.4rem 0;"><span style="font-size:0.8rem;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.04em;">Department Filter Board:</span></div>'
            + '<div class="z-sla-board">' + "".join(chips) + "</div>",
            unsafe_allow_html=True,
        )


def render_sla_board(tickets, use_policy=False):
    """Glowing color-coded SLA timing badges for every ticket in the queue."""
    chips = []
    for t in tickets:
        sla = agent_core.compute_sla_state(t, use_policy=use_policy)
        css = agent_core.SLA_CHIP_CLASS.get(sla["state"], "z-sla-paused")
        status_symbol = "🚨" if sla["state"] == "breached" else ("⚠️" if sla["state"] == "due_soon" else ("⏸️" if sla["state"] == "paused" else "⏳"))
        chips.append(
            f'<span class="z-sla {css}" style="box-shadow: 0 2px 6px rgba(0,0,0,0.06); border-radius: 20px; padding: 0.25rem 0.75rem;" title="{t["subject"]}">'
            f'{status_symbol} <b>{t["ticket_id"]}</b> &#183; {sla["timer_label"]}</span>'
        )
    if chips:
        st.markdown(
            '<div style="margin: 0.6rem 0 0.4rem 0;"><span style="font-size:0.8rem;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.04em;">Live SLA Timers:</span></div>'
            + '<div class="z-sla-board">' + "".join(chips) + "</div>",
            unsafe_allow_html=True,
        )


def render_queue(tickets, use_policy=False):
    st.markdown("### 📋 Unified Ticket Queue")
    st.caption(
        "✨ Select any row to open full ticket details, or query `/?ticket=<ID>`. "
        "Dynamic timers follow priority SLAs (Critical 1h ⚡ / High 4h / Medium 8h / Low 24h)."
    )

    render_department_board(tickets)
    render_sla_board(tickets, use_policy=use_policy)

    df = build_queue_dataframe(tickets, use_policy=use_policy)
    if df.empty:
        st.info("No tickets in the queue.")
        return

    event = None
    try:
        event = st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="ticket_queue_table",
        )
    except TypeError:
        # Older Streamlit without row selection support.
        st.dataframe(df, use_container_width=True, hide_index=True)

    if event is not None:
        selected_rows = getattr(getattr(event, "selection", None), "rows", []) or []
        if selected_rows:
            ticket_id = df.iloc[selected_rows[0]]["Ticket ID"]
            if ticket_id != get_selected_param():
                set_selected_param(ticket_id)
                st.rerun()


def render_sla_line(ticket, use_policy=False):
    """Dynamic SLA timer: green countdown, or a red breach warning."""
    sla = agent_core.compute_sla_state(ticket, use_policy=use_policy)
    st.markdown(agent_core.sla_chip_html(sla), unsafe_allow_html=True)

    if sla["state"] == "paused":
        st.warning(
            "SLA clock paused - waiting on the customer. Excluded from the SLA clock."
        )
        return

    if sla["state"] == "unknown":
        st.info("SLA timing unavailable for this ticket.")
        return

    due_text = sla["due_at"].strftime(TIMESTAMP_FMT)
    if sla["state"] == "breached":
        st.error(
            f"SLA BREACHED: Overdue by {sla['spoken']} (deadline {due_text}, "
            f"basis: {sla['due_source']})."
        )
    else:
        st.success(
            f"Within SLA: {sla['spoken']} remaining until breach "
            f"(deadline {due_text}, basis: {sla['due_source']})."
        )


def render_timeline(ticket_id):
    st.markdown("#### 💬 Ticket Timeline & Events")
    entries = fetch_timeline(ticket_id)
    if not entries:
        st.info("No timeline entries yet.")
        return

    icons = {
        "note": ("📝 Note", "#3b82f6", "#eff6ff"),
        "system": ("⚙️ System", "#8b5cf6", "#f5f3ff"),
        "escalation": ("🚨 Escalation", "#ef4444", "#fef2f2"),
        "email": ("✉️ Email", "#10b981", "#ecfdf5"),
    }
    for entry in reversed(entries):
        kind_info = icons.get(entry["kind"], (f"📌 {entry['kind'].title()}", "#64748b", "#f8fafc"))
        label, accent_col, bg_col = kind_info
        header_html = (
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.35rem;">'
            f'<span style="font-weight:700;color:#1e293b;font-size:0.88rem;">👤 {entry["author"]}</span>'
            f'<span style="background:{bg_col};color:{accent_col};border:1px solid {accent_col};'
            f'padding:0.12rem 0.55rem;border-radius:12px;font-size:0.75rem;font-weight:700;">{label}</span>'
            f'</div>'
            f'<div style="font-size:0.75rem;color:#64748b;margin-bottom:0.4rem;">🕒 {entry["created_at"]}</div>'
        )
        with st.container(border=True):
            st.markdown(header_html, unsafe_allow_html=True)
            st.markdown(f'<div style="color:#334155;font-size:0.88rem;line-height:1.45;">{entry["content"]}</div>', unsafe_allow_html=True)


def render_attachments(ticket_id):
    st.markdown("#### 📎 Attachments")
    rows = fetch_attachments(ticket_id)
    if not rows:
        st.caption("No attachments uploaded.")
        return
    for a in rows:
        if a["visibility"] == "private":
            st.markdown(
                f'<div style="background:#fef2f2;border:1px solid #f87171;padding:0.45rem 0.75rem;border-radius:6px;margin-bottom:0.4rem;font-size:0.85rem;">'
                f'🔒 <b style="color:#b91c1c;">{a["filename"]}</b> &nbsp;'
                f'<span style="background:#fee2e2;color:#991b1b;padding:0.1rem 0.4rem;border-radius:4px;font-size:0.75rem;font-weight:700;">RESTRICTED</span>'
                f'<div style="font-size:0.72rem;color:#7f1d1d;margin-top:0.2rem;">Uploaded {a["uploaded_at"]}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f'<div style="background:#f0fdf4;border:1px solid #86efac;padding:0.45rem 0.75rem;border-radius:6px;margin-bottom:0.4rem;font-size:0.85rem;">'
                f'📄 <b style="color:#166534;">{a["filename"]}</b> &nbsp;'
                f'<span style="background:#dcfce7;color:#15803d;padding:0.1rem 0.4rem;border-radius:4px;font-size:0.75rem;font-weight:700;">PUBLIC</span>'
                f'<div style="font-size:0.72rem;color:#14532d;margin-top:0.2rem;">Uploaded {a["uploaded_at"]}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )


def render_escalation_form(ticket):
    st.markdown("#### ⚡ Raise Internal Escalation Note")
    if has_escalation(ticket["ticket_id"]):
        st.warning(
            "An escalation already exists for this ticket. Review the timeline before "
            "adding another one."
        )

    with st.form(key=f"escalation_form_{ticket['ticket_id']}"):
        author = st.text_input("Agent name", value=ticket["owner"])
        reason = st.selectbox(
            "Escalation reason",
            options=[
                "SLA breach - no first response",
                "SLA breach - critical business impact",
                "Customer impact - repeat contact",
                "Existing escalation follow-up",
                "Other",
            ],
        )
        note = st.text_area(
            "Internal escalation note",
            placeholder="Describe the impact, what has been tried, and what is needed next.",
            height=140,
        )
        escalate_flag = st.checkbox(
            "Mark ticket status as 'Escalated'", value=ticket["status"] != "Escalated"
        )
        submitted = st.form_submit_button("🚀 Submit Escalation Note", type="primary")

    if submitted:
        if not note.strip():
            st.error("Please write the escalation note before submitting.")
            return
        new_status = add_escalation_note(
            ticket["ticket_id"], author.strip() or ticket["owner"], reason, note.strip(),
            escalate_flag,
        )
        if new_status:
            st.success(f"Escalation note saved. Ticket status set to '{new_status}'.")
        else:
            st.success("Escalation note saved to the timeline.")
        st.rerun()


def render_detail(ticket_id, tickets, use_policy=False):
    ticket = fetch_ticket(ticket_id)
    if ticket is None:
        st.error(
            f"Ticket `{ticket_id}` was not found. Use the sidebar to pick a valid ticket."
        )
        return

    # Paint the whole ticket card with its department's colour.
    card_key = agent_core.department_card_key("helpdesk", ticket["ticket_id"])
    st.markdown(
        agent_core.department_card_css(card_key, ticket["department"]),
        unsafe_allow_html=True,
    )

    with st.container(key=card_key):
        render_detail_body(ticket, use_policy=use_policy)


def render_detail_body(ticket, use_policy=False):
    dept_color = agent_core.department_color(ticket["department"])
    prio_badge = PRIORITY_BADGES.get(ticket["priority"], {"bg": "#2563eb", "color": "#fff", "icon": "•"})
    status_badge = STATUS_BADGES.get(ticket["status"], {"bg": "#f3f4f6", "color": "#1f2937", "border": "#d1d5db", "icon": "•"})

    st.markdown(
        f"""
        <div style="display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:0.5rem; margin-bottom: 0.8rem;">
            <div>
                <h2 style="margin:0; font-size:1.6rem; color:#0f172a; font-weight:800; border:none; padding:0;">
                    {ticket['ticket_id']} — {ticket['subject']}
                </h2>
                <div style="margin-top:0.3rem; display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap;">
                    <span style="background:{dept_color['accent']}; color:white; padding:0.2rem 0.7rem; border-radius:16px; font-size:0.8rem; font-weight:700;">
                        🏢 {ticket['department'] or 'Unrouted'}
                    </span>
                    <span style="background:{prio_badge['bg']}; color:{prio_badge['color']}; padding:0.2rem 0.7rem; border-radius:16px; font-size:0.8rem; font-weight:700; box-shadow:0 1px 3px rgba(0,0,0,0.1);">
                        {prio_badge['icon']} Priority: {ticket['priority']}
                    </span>
                    <span style="background:{status_badge['bg']}; color:{status_badge['color']}; border:1px solid {status_badge['border']}; padding:0.2rem 0.7rem; border-radius:16px; font-size:0.8rem; font-weight:700;">
                        {status_badge['icon']} {ticket['status']}
                    </span>
                </div>
            </div>
            <div style="background:white; border:1px solid #e2e8f0; border-radius:8px; padding:0.4rem 0.8rem; font-size:0.82rem; color:#64748b; box-shadow:0 1px 3px rgba(0,0,0,0.04);">
                <b>Test Case:</b> <code>{ticket['test_case']}</code> &nbsp;|&nbsp; <b>Requester:</b> {ticket['requester']}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    sla = agent_core.compute_sla_state(ticket, use_policy=use_policy)
    target_hours = agent_core.sla_target_hours(ticket["priority"])

    # Colorful Stats Cards
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.markdown(
            f"""<div class="kpi-tile kpi-tile-blue">
                <span class="kpi-label">Status</span>
                <span class="kpi-value" style="font-size:1.3rem;">{status_badge['icon']} {ticket['status']}</span>
            </div>""",
            unsafe_allow_html=True,
        )
    with m2:
        st.markdown(
            f"""<div class="kpi-tile kpi-tile-amber">
                <span class="kpi-label">Priority Target</span>
                <span class="kpi-value" style="font-size:1.3rem;">{ticket['priority']} ({target_hours}h SLA)</span>
            </div>""",
            unsafe_allow_html=True,
        )
    with m3:
        sla_bg = "kpi-tile-red" if sla["state"] == "breached" else ("kpi-tile-blue" if sla["state"] == "within" else "kpi-tile-purple")
        st.markdown(
            f"""<div class="kpi-tile {sla_bg}">
                <span class="kpi-label">SLA State</span>
                <span class="kpi-value" style="font-size:1.3rem;">{sla['table_label']}</span>
            </div>""",
            unsafe_allow_html=True,
        )
    with m4:
        st.markdown(
            f"""<div class="kpi-tile kpi-tile-purple">
                <span class="kpi-label">Owner</span>
                <span class="kpi-value" style="font-size:1.3rem;">👤 {ticket['owner']}</span>
            </div>""",
            unsafe_allow_html=True,
        )

    st.markdown("<div style='margin-top: 1rem;'></div>", unsafe_allow_html=True)
    render_work_progress(ticket)
    render_sla_line(ticket, use_policy=use_policy)

    st.markdown(
        f"""<div style="background:#ffffff; border-left:4px solid #3b82f6; border-radius:6px; padding:0.8rem 1rem; margin:1rem 0; box-shadow:0 1px 4px rgba(0,0,0,0.05);">
            <div style="font-size:0.75rem; text-transform:uppercase; color:#64748b; font-weight:700; margin-bottom:0.3rem;">Issue Description:</div>
            <div style="font-size:0.95rem; color:#1e293b; line-height:1.5;">{ticket['description']}</div>
        </div>""",
        unsafe_allow_html=True,
    )

    st.divider()
    left, right = st.columns([3, 2])
    with left:
        render_timeline(ticket["ticket_id"])
    with right:
        render_attachments(ticket["ticket_id"])
        st.divider()
        st.caption(
            f"📅 **Created:** `{ticket['created_at']}`  \n"
            f"🔄 **Last updated:** `{ticket['updated_at']}`  \n"
            f"🔗 **Direct link:** `/?ticket={ticket['ticket_id']}`"
        )

    st.divider()
    render_escalation_form(ticket)


def main():
    st.set_page_config(page_title="Mock Helpdesk - IT Operations", page_icon="🧰", layout="wide")
    agent_core.apply_enterprise_theme()
    st.markdown(HELPDESK_COLORFUL_CSS, unsafe_allow_html=True)

    # Always run init_db: it is idempotent, and it is what applies the additive
    # `department` migration (plus the seed backfill) to an existing database.
    init_db()

    # Vibrant Header Hero Banner
    st.markdown(
        """
        <div class="main-header-banner">
            <h1>🧰 Mock Helpdesk Operations Console</h1>
            <p>Ziro Escalation Test Harness — Real-time SLA monitoring, intelligent department routing & AI escalation pack generator.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    tickets = fetch_tickets()
    render_sidebar(tickets)

    st.sidebar.divider()
    st.sidebar.subheader("⚙️ SLA policy (testing)")
    use_policy = st.sidebar.toggle(
        "Recompute from priority targets",
        value=False,
        help=(
            "Off (default): use each ticket's stored sla_due_at, so the four official "
            "test cases keep their seeded breach durations. On: ignore the stored "
            "deadline and recompute it from the priority target "
            "(Critical 1h / High 4h / Medium 8h / Low 24h)."
        ),
    )

    selected = get_selected_param()
    valid_ids = {t["ticket_id"] for t in tickets}
    if selected and selected not in valid_ids:
        st.warning(f"Unknown ticket `{selected}`. Showing the full queue instead.")
        selected = None

    if selected:
        render_detail(selected, tickets, use_policy=use_policy)
        st.divider()
        with st.expander("🔙 Back to the full ticket queue"):
            render_queue(tickets, use_policy=use_policy)
    else:
        render_queue(tickets, use_policy=use_policy)
        st.info("💡 Select a ticket above, or open `/?ticket=TCK-101` to view ticket details.")


if __name__ == "__main__":
    main()
