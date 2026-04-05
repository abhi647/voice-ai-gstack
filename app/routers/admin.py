"""
Admin dashboard — basic read-only call log for practice staff.

Routes:
  GET /admin/calls           — paginated table of all calls (most recent first)
  GET /admin/calls/{call_id} — detail view with full transcript

Auth: none in v0.1 (should be behind Azure Easy Auth or IP allowlist in prod).
"""

import html
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.call import Call

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

# ---------------------------------------------------------------------------
# Styles shared across pages
# ---------------------------------------------------------------------------

_CSS = """
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f5f5f5; color: #1a1a1a; padding: 24px; }
  h1 { font-size: 1.4rem; font-weight: 600; margin-bottom: 16px; }
  a { color: #0070f3; text-decoration: none; }
  a:hover { text-decoration: underline; }

  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 0.75rem; font-weight: 600; white-space: nowrap;
  }
  .badge-booking   { background: #d1fae5; color: #065f46; }
  .badge-escalated { background: #fef3c7; color: #92400e; }
  .badge-unanswered{ background: #fee2e2; color: #991b1b; }
  .badge-faq       { background: #e0e7ff; color: #3730a3; }
  .badge-hung-up   { background: #f3f4f6; color: #6b7280; }
  .badge-other     { background: #f3f4f6; color: #6b7280; }

  table { width: 100%; border-collapse: collapse; background: #fff;
          border-radius: 8px; overflow: hidden;
          box-shadow: 0 1px 3px rgba(0,0,0,.1); }
  th { background: #f9fafb; text-align: left; padding: 10px 14px;
       font-size: 0.75rem; text-transform: uppercase; letter-spacing: .05em;
       color: #6b7280; border-bottom: 1px solid #e5e7eb; }
  td { padding: 10px 14px; border-bottom: 1px solid #f3f4f6;
       font-size: 0.875rem; vertical-align: top; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #fafafa; }

  .pagination { display: flex; gap: 8px; margin-top: 16px; align-items: center;
                font-size: 0.875rem; }
  .pagination a, .pagination span {
    padding: 6px 12px; border-radius: 4px; border: 1px solid #e5e7eb;
    background: #fff; color: #374151;
  }
  .pagination a:hover { background: #f3f4f6; text-decoration: none; }
  .pagination .current { background: #0070f3; color: #fff; border-color: #0070f3; }

  .detail-box { background: #fff; border-radius: 8px; padding: 20px;
                box-shadow: 0 1px 3px rgba(0,0,0,.1); margin-bottom: 16px; }
  .detail-grid { display: grid; grid-template-columns: 160px 1fr; gap: 8px 12px; }
  .detail-label { font-size: 0.8rem; color: #6b7280; padding-top: 2px; }
  .transcript-box { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px;
                    padding: 16px; white-space: pre-wrap; font-size: 0.875rem;
                    line-height: 1.6; max-height: 500px; overflow-y: auto; }
  .back { margin-bottom: 16px; font-size: 0.875rem; }
  .no-calls { text-align: center; padding: 48px; color: #6b7280; }
</style>
"""

_PAGE_SIZE = 25


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _disposition_badge(disposition: str | None) -> str:
    d = (disposition or "unknown").upper()
    cls_map = {
        "BOOKING_CAPTURED": "badge-booking",
        "ESCALATED": "badge-escalated",
        "ESCALATED_UNANSWERED": "badge-unanswered",
        "FAQ_ONLY": "badge-faq",
        "HUNG_UP": "badge-hung-up",
    }
    css = cls_map.get(d, "badge-other")
    label = d.replace("_", " ").title()
    return f'<span class="badge {css}">{html.escape(label)}</span>'


def _fmt_dt(dt) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%b %d %H:%M")


def _sms_status(call: Call) -> str:
    if call.sms_sent_at:
        return f'<span style="color:#059669">✓ {_fmt_dt(call.sms_sent_at)}</span>'
    if (call.disposition or "").upper() == "BOOKING_CAPTURED":
        return '<span style="color:#d97706">⚠ not sent</span>'
    return '<span style="color:#d1d5db">—</span>'


# ---------------------------------------------------------------------------
# GET /admin/calls
# ---------------------------------------------------------------------------

@router.get("/calls", response_class=HTMLResponse)
async def call_log(
    page: int = Query(default=1, ge=1),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Paginated call log, newest first."""
    offset = (page - 1) * _PAGE_SIZE

    total_result = await db.execute(select(Call))
    all_calls = total_result.scalars().all()
    total = len(all_calls)

    result = await db.execute(
        select(Call).order_by(desc(Call.started_at)).offset(offset).limit(_PAGE_SIZE)
    )
    calls = result.scalars().all()

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    rows = ""
    if not calls:
        rows = f'<tr><td colspan="7" class="no-calls">No calls yet.</td></tr>'
    else:
        for c in calls:
            detail_url = f"/admin/calls/{c.id}"
            rows += f"""<tr>
              <td><a href="{detail_url}">{_fmt_dt(c.started_at)}</a></td>
              <td>{html.escape(c.patient_phone or "—")}</td>
              <td>{html.escape(c.patient_name or "—")}</td>
              <td>{html.escape(c.service_type or "—")}</td>
              <td>{_disposition_badge(c.disposition)}</td>
              <td>{_sms_status(c)}</td>
              <td>{html.escape(c.requested_time or "—")}</td>
            </tr>"""

    # Pagination
    pagination = '<div class="pagination">'
    if page > 1:
        pagination += f'<a href="?page={page - 1}">← Prev</a>'
    pagination += f'<span class="current">{page} / {total_pages}</span>'
    if page < total_pages:
        pagination += f'<a href="?page={page + 1}">Next →</a>'
    pagination += f'<span style="color:#6b7280;border:none;background:none">{total} calls total</span>'
    pagination += "</div>"

    body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Call Log — Admin</title>{_CSS}</head>
<body>
  <h1>Call Log</h1>
  <table>
    <thead>
      <tr>
        <th>Time</th>
        <th>Patient phone</th>
        <th>Name</th>
        <th>Service</th>
        <th>Disposition</th>
        <th>Patient SMS</th>
        <th>Requested time</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  {pagination}
</body>
</html>"""

    return HTMLResponse(content=body)


# ---------------------------------------------------------------------------
# GET /admin/calls/{call_id}
# ---------------------------------------------------------------------------

@router.get("/calls/{call_id}", response_class=HTMLResponse)
async def call_detail(call_id: str, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
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

    def row(label: str, value: str) -> str:
        return f'<div class="detail-label">{html.escape(label)}</div><div>{value}</div>'

    transcript_section = ""
    if call.transcript:
        transcript_section = f"""
        <div class="detail-box">
          <h2 style="font-size:1rem;margin-bottom:12px">Transcript</h2>
          <div class="transcript-box">{html.escape(call.transcript)}</div>
        </div>"""

    body = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Call {call.twilio_call_sid} — Admin</title>{_CSS}</head>
<body>
  <div class="back"><a href="/admin/calls">← Back to call log</a></div>
  <h1>Call detail</h1>
  <div class="detail-box">
    <div class="detail-grid">
      {row("Call SID", html.escape(call.twilio_call_sid))}
      {row("Started", _fmt_dt(call.started_at))}
      {row("Ended", _fmt_dt(call.ended_at))}
      {row("Duration", _duration(call))}
      {row("Patient phone", html.escape(call.patient_phone or "—"))}
      {row("Patient name", html.escape(call.patient_name or "—"))}
      {row("Service type", html.escape(call.service_type or "—"))}
      {row("Requested time", html.escape(call.requested_time or "—"))}
      {row("Disposition", _disposition_badge(call.disposition))}
      {row("Patient SMS", _sms_status(call))}
      {row("Transcript S3", html.escape(call.transcript_s3_key or "—"))}
      {row("Audio S3", html.escape(call.audio_s3_key or "—"))}
    </div>
  </div>
  {transcript_section}
</body>
</html>"""

    return HTMLResponse(content=body)


def _duration(call: Call) -> str:
    if call.started_at is None or call.ended_at is None:
        return "—"
    delta = call.ended_at - call.started_at
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    return f"{secs // 60}m {secs % 60}s"
