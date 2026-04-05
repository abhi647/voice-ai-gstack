"""
Admin dashboard — enterprise call intelligence for practice staff.

Routes:
  GET /admin/             — dashboard with metrics + charts
  GET /admin/calls        — paginated + searchable call log
  GET /admin/calls/{id}   — full call detail with transcript
  GET /admin/analytics    — disposition breakdown + 30-day trend

Auth: none in v0.1 (should be behind Azure Easy Auth or IP allowlist in prod).
"""

import html as _html
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.call import Call

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

# ---------------------------------------------------------------------------
# Template setup
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_PAGE_SIZE = 25

# ---------------------------------------------------------------------------
# Template filters / globals
# ---------------------------------------------------------------------------

DISPOSITION_COLORS = [
    "#006877", "#00bdd6", "#fbbf24", "#f87171", "#a5b4fc", "#94a3b8",
]


def _disposition_badge(disposition: str | None) -> str:
    d = (disposition or "unknown").upper()
    cls_map = {
        "BOOKING_CAPTURED": "badge-booking",
        "ESCALATED": "badge-escalated",
        "ESCALATED_UNANSWERED": "badge-unanswered",
        "FAQ_ONLY": "badge-faq",
        "HUNG_UP": "badge-hung-up",
        "DUPLICATE_PREVENTED": "badge-duplicate",
    }
    css = cls_map.get(d, "badge-other")
    label = d.replace("_", " ").title()
    return f'<span class="badge {css}">{_html.escape(label)}</span>'


def _fmt_dt(dt) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%b %d, %H:%M")


def _fmt_dt_full(dt) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%b %d %Y · %H:%M UTC")


def _sms_status(call: Call) -> str:
    if call.sms_sent_at:
        return f'<span class="sms-ok">✓ {_fmt_dt(call.sms_sent_at)}</span>'
    if (call.disposition or "").upper() == "BOOKING_CAPTURED":
        return '<span class="sms-warn">⚠ not sent</span>'
    return '<span class="sms-na">—</span>'


def _duration(call: Call) -> str:
    if call.started_at is None or call.ended_at is None:
        return "—"
    delta = call.ended_at - call.started_at
    secs = int(delta.total_seconds())
    if secs < 0:
        return "—"
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m {secs % 60}s"


def _wordcount(text: str | None) -> int:
    if not text:
        return 0
    return len(text.split())


def _format_transcript(text: str | None) -> str:
    """Convert raw transcript to coloured HTML spans."""
    if not text:
        return ""
    lines = []
    for line in text.split("\n"):
        escaped = _html.escape(line)
        if line.upper().startswith("AGENT:"):
            lines.append(f'<span class="agent-line">{escaped}</span>')
        elif line.upper().startswith("PATIENT:"):
            lines.append(f'<span class="patient-line">{escaped}</span>')
        else:
            lines.append(escaped)
    return "\n".join(lines)


def _disposition_color(index: int) -> str:
    return DISPOSITION_COLORS[index % len(DISPOSITION_COLORS)]


# Register filters
templates.env.filters["disposition_badge"] = _disposition_badge
templates.env.filters["fmt_dt"] = _fmt_dt
templates.env.filters["fmt_dt_full"] = _fmt_dt_full
templates.env.filters["sms_status"] = _sms_status
templates.env.filters["duration"] = _duration
templates.env.filters["wordcount"] = _wordcount
templates.env.filters["format_transcript"] = _format_transcript
templates.env.filters["disposition_color"] = _disposition_color

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_stats(calls: list[Call]) -> dict:
    """Compute dashboard metrics from a full call list."""
    total = len(calls)
    today = datetime.now(timezone.utc).date()
    calls_today = sum(
        1 for c in calls
        if c.started_at and c.started_at.date() == today
    )
    bookings = [c for c in calls if (c.disposition or "").upper() == "BOOKING_CAPTURED"]
    escalated = [c for c in calls if "ESCALATED" in (c.disposition or "").upper()]
    sms_sent = [c for c in calls if c.sms_sent_at is not None]

    booking_rate = round(len(bookings) / total * 100) if total else 0
    escalation_rate = round(len(escalated) / total * 100) if total else 0
    sms_rate = round(len(sms_sent) / len(bookings) * 100) if bookings else 0

    return {
        "total_calls": total,
        "calls_today": calls_today,
        "total_bookings": len(bookings),
        "total_escalated": len(escalated),
        "sms_sent": len(sms_sent),
        "booking_rate_pct": booking_rate,
        "escalation_rate_pct": escalation_rate,
        "sms_success_rate": sms_rate,
    }


def _build_chart_data(calls: list[Call], days: int = 7) -> tuple[list[str], list[int]]:
    """Return (labels, values) for a day-by-day call volume chart."""
    today = datetime.now(timezone.utc).date()
    counts: dict = defaultdict(int)
    for c in calls:
        if c.started_at:
            d = c.started_at.date()
            if d >= today - timedelta(days=days - 1):
                counts[d] += 1
    labels = []
    values = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        labels.append(d.strftime("%b %d"))
        values.append(counts.get(d, 0))
    return labels, values


def _build_disposition_data(calls: list[Call]) -> list[dict]:
    """Return disposition breakdown for charts and analytics."""
    total = len(calls)
    counter: Counter = Counter()
    for c in calls:
        d = (c.disposition or "UNKNOWN").upper()
        label = d.replace("_", " ").title()
        counter[label] += 1
    result = []
    for label, count in counter.most_common():
        pct = round(count / total * 100) if total else 0
        result.append({"label": label, "count": count, "pct": pct})
    return result


# ---------------------------------------------------------------------------
# GET /admin/
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Dashboard with metrics, volume chart, disposition donut, recent calls."""
    result = await db.execute(select(Call).order_by(desc(Call.started_at)))
    all_calls: list[Call] = list(result.scalars().all())

    recent_calls = all_calls[:10]
    stats = _build_stats(all_calls)
    chart_labels, chart_values = _build_chart_data(all_calls, days=7)
    disposition_data = _build_disposition_data(all_calls)

    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        context={
            "active_page": "dashboard",
            "stats": stats,
            "recent_calls": recent_calls,
            "chart_labels": chart_labels,
            "chart_values": chart_values,
            "disposition_data": disposition_data,
        },
    )


# ---------------------------------------------------------------------------
# GET /admin/calls
# ---------------------------------------------------------------------------

@router.get("/calls", response_class=HTMLResponse)
async def call_log(
    request: Request,
    page: int = Query(default=1, ge=1),
    q: Optional[str] = Query(default=None),
    disposition: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Paginated, searchable call log."""
    stmt = select(Call)

    # Search
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(Call.patient_phone.ilike(like), Call.patient_name.ilike(like))
        )

    # Disposition filter
    if disposition:
        stmt = stmt.where(Call.disposition == disposition)

    stmt = stmt.order_by(desc(Call.started_at))

    result = await db.execute(stmt)
    all_calls: list[Call] = list(result.scalars().all())
    total = len(all_calls)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    offset = (page - 1) * _PAGE_SIZE
    calls = all_calls[offset : offset + _PAGE_SIZE]

    return templates.TemplateResponse(
        request,
        "admin/calls.html",
        context={
            "active_page": "calls",
            "calls": calls,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "q": q,
            "disposition_filter": disposition,
        },
    )


# ---------------------------------------------------------------------------
# GET /admin/calls/{call_id}
# ---------------------------------------------------------------------------

@router.get("/calls/{call_id}", response_class=HTMLResponse)
async def call_detail(
    call_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Detail view for a single call — shows all fields + full transcript."""
    from uuid import UUID

    try:
        uid = UUID(call_id)
    except ValueError:
        return HTMLResponse(content="<p>Invalid call ID.</p>", status_code=400)

    result = await db.execute(select(Call).where(Call.id == uid))
    call = result.scalar_one_or_none()
    if call is None:
        return HTMLResponse(content="<p>Call not found.</p>", status_code=404)

    return templates.TemplateResponse(
        request,
        "admin/call_detail.html",
        context={
            "active_page": "calls",
            "call": call,
        },
    )


# ---------------------------------------------------------------------------
# GET /admin/analytics
# ---------------------------------------------------------------------------

@router.get("/analytics", response_class=HTMLResponse)
async def analytics(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Analytics page — disposition breakdown + 30-day volume trend."""
    result = await db.execute(select(Call).order_by(desc(Call.started_at)))
    all_calls: list[Call] = list(result.scalars().all())

    stats = _build_stats(all_calls)
    chart_labels, chart_values = _build_chart_data(all_calls, days=30)
    disposition_data = _build_disposition_data(all_calls)

    return templates.TemplateResponse(
        request,
        "admin/analytics.html",
        context={
            "active_page": "analytics",
            "stats": stats,
            "chart_labels": chart_labels,
            "chart_values": chart_values,
            "disposition_data": disposition_data,
        },
    )
