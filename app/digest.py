"""
Weekly digest — Monday morning metrics email to each practice.

Sent every Monday at 8 AM practice-local time (scheduled via cron or
docker compose with a simple sleep loop — see bin/weekly-digest).

What it shows:
  - Total calls this week
  - Booking captures (with list of patient name + service)
  - Escalations (keyword + timeout)
  - Unanswered escalations
  - Hang-ups / FAQ-only calls
  - Average call duration (if available)

Why this matters:
  - Office managers forward it to the dentist — proof the AI is working
  - It's your stickiness metric. If the number goes up week over week,
    they never cancel.
  - "You captured 14 bookings last week while the office was closed on
    Saturday" is the sentence that gets you referrals.

Usage:
  python -m app.digest            # send digest for all active practices
  python -m app.digest --dry-run  # print to stdout, no email sent
  python -m app.digest --practice-id <UUID>  # single practice
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import click
from sqlalchemy import func, select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.call import Call
from app.models.practice import Practice

logger = logging.getLogger(__name__)


class WeeklyStats(NamedTuple):
    practice_id: str
    practice_name: str
    staff_email: str | None
    week_start: datetime
    week_end: datetime
    total_calls: int
    bookings: list[dict]          # [{patient_name, service_type, requested_time}]
    escalations: int
    unanswered_escalations: int
    hung_up: int
    faq_only: int


async def compute_stats(
    practice: Practice,
    week_start: datetime,
    week_end: datetime,
) -> WeeklyStats:
    async with AsyncSessionLocal() as db:
        # All calls for this practice in the window
        q = select(Call).where(
            Call.practice_id == practice.id,
            Call.started_at >= week_start,
            Call.started_at < week_end,
        )
        result = await db.execute(q)
        calls = result.scalars().all()

    bookings = [
        {
            "patient_name": c.patient_name or "unknown",
            "service_type": c.service_type or "not specified",
            "requested_time": c.requested_time or "not specified",
        }
        for c in calls
        if c.disposition == "BOOKING_CAPTURED"
    ]

    return WeeklyStats(
        practice_id=str(practice.id),
        practice_name=practice.name,
        staff_email=practice.staff_email,
        week_start=week_start,
        week_end=week_end,
        total_calls=len(calls),
        bookings=bookings,
        escalations=sum(1 for c in calls if c.disposition == "ESCALATED"),
        unanswered_escalations=sum(1 for c in calls if c.disposition == "ESCALATED_UNANSWERED"),
        hung_up=sum(1 for c in calls if c.disposition == "HUNG_UP"),
        faq_only=sum(1 for c in calls if c.disposition == "FAQ_ONLY"),
    )


async def send_digest(stats: WeeklyStats, dry_run: bool = False) -> bool:
    """
    Send the weekly digest email to practice staff.
    Returns True on success, False on failure.
    """
    if not stats.staff_email:
        logger.info(f"No staff_email for {stats.practice_name} — skipping digest")
        return False

    subject = _subject(stats)
    html = _email_html(stats)
    plain = _email_plain(stats)

    if dry_run:
        click.echo(f"\n{'='*60}")
        click.echo(f"TO: {stats.staff_email}")
        click.echo(f"SUBJECT: {subject}")
        click.echo(f"{'='*60}")
        click.echo(plain)
        return True

    if not settings.sendgrid_api_key:
        logger.warning("SendGrid not configured — cannot send digest")
        return False

    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={
                    "Authorization": f"Bearer {settings.sendgrid_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "personalizations": [{"to": [{"email": stats.staff_email}]}],
                    "from": {
                        "email": settings.sendgrid_from_email,
                        "name": "Voice AI Receptionist",
                    },
                    "subject": subject,
                    "content": [
                        {"type": "text/plain", "value": plain},
                        {"type": "text/html", "value": html},
                    ],
                },
                timeout=15.0,
            )
            resp.raise_for_status()
        logger.info(f"Digest sent to {stats.staff_email} for {stats.practice_name}")
        return True
    except Exception as e:
        logger.error(f"Failed to send digest to {stats.staff_email}: {e}")
        return False


async def run_all_digests(
    dry_run: bool = False,
    practice_id: str | None = None,
) -> None:
    """Compute and send digests for all active practices (or one specific practice)."""
    # Week window: last Monday 00:00 UTC → this Monday 00:00 UTC
    now = datetime.now(timezone.utc)
    days_since_monday = now.weekday()  # Monday=0
    week_end = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_since_monday)
    week_start = week_end - timedelta(days=7)

    async with AsyncSessionLocal() as db:
        q = select(Practice).where(Practice.is_active == True)
        if practice_id:
            import uuid
            q = q.where(Practice.id == uuid.UUID(practice_id))
        result = await db.execute(q)
        practices = result.scalars().all()

    if not practices:
        logger.info("No active practices found for digest.")
        return

    logger.info(
        f"Sending weekly digest for {len(practices)} practice(s). "
        f"Window: {week_start.date()} → {week_end.date()}"
    )

    for practice in practices:
        stats = await compute_stats(practice, week_start, week_end)
        await send_digest(stats, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Email formatters
# ---------------------------------------------------------------------------


def _subject(stats: WeeklyStats) -> str:
    date_str = stats.week_start.strftime("%b %d")
    if stats.total_calls == 0:
        return f"[{stats.practice_name}] Weekly digest — no calls this week ({date_str})"
    return (
        f"[{stats.practice_name}] Weekly digest — "
        f"{stats.total_calls} calls, {len(stats.bookings)} bookings ({date_str})"
    )


def _email_plain(stats: WeeklyStats) -> str:
    week_range = f"{stats.week_start.strftime('%b %d')} – {stats.week_end.strftime('%b %d, %Y')}"
    lines = [
        f"Weekly Digest — {stats.practice_name}",
        f"Period: {week_range}",
        "",
        f"Total calls:             {stats.total_calls}",
        f"Booking captures:        {len(stats.bookings)}",
        f"Escalations (connected): {stats.escalations}",
        f"Escalations (no answer): {stats.unanswered_escalations}",
        f"FAQ / info only:         {stats.faq_only}",
        f"Hung up:                 {stats.hung_up}",
    ]

    if stats.bookings:
        lines += ["", "Booking captures this week:"]
        for i, b in enumerate(stats.bookings, 1):
            lines.append(
                f"  {i}. {b['patient_name']} — {b['service_type']} — {b['requested_time']}"
            )

    if stats.unanswered_escalations > 0:
        lines += [
            "",
            f"⚠ {stats.unanswered_escalations} escalation(s) went unanswered.",
            "  Please check your escalation number is reachable during business hours.",
        ]

    lines += ["", "—", "Voice AI Receptionist"]
    return "\n".join(lines)


def _email_html(stats: WeeklyStats) -> str:
    week_range = f"{stats.week_start.strftime('%b %d')} – {stats.week_end.strftime('%b %d, %Y')}"

    booking_rows = ""
    if stats.bookings:
        rows = "".join(
            f"<tr>"
            f"<td style='padding:4px 12px 4px 0'>{i}.</td>"
            f"<td style='padding:4px 12px 4px 0'>{b['patient_name']}</td>"
            f"<td style='padding:4px 12px 4px 0'>{b['service_type']}</td>"
            f"<td style='padding:4px 0;color:#666'>{b['requested_time']}</td>"
            f"</tr>"
            for i, b in enumerate(stats.bookings, 1)
        )
        booking_rows = f"""
        <h3 style='color:#1a1a1a;margin-top:24px'>Booking captures</h3>
        <table style='border-collapse:collapse;width:100%;font-size:14px'>
          <thead>
            <tr style='color:#666;font-size:12px;text-transform:uppercase'>
              <th style='padding:4px 12px 4px 0;text-align:left'>#</th>
              <th style='padding:4px 12px 4px 0;text-align:left'>Patient</th>
              <th style='padding:4px 12px 4px 0;text-align:left'>Service</th>
              <th style='padding:4px 0;text-align:left'>Requested time</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>"""

    unanswered_banner = ""
    if stats.unanswered_escalations > 0:
        unanswered_banner = f"""
        <div style='background:#fff3cd;border:1px solid #ffc107;border-radius:4px;
                    padding:12px 16px;margin-top:20px;font-size:14px'>
          <b>⚠ {stats.unanswered_escalations} escalation(s) went unanswered.</b><br>
          Please ensure your escalation number is reachable during business hours.
        </div>"""

    def stat_row(label, value, highlight=False):
        color = "#2563eb" if highlight else "#1a1a1a"
        return (
            f"<tr>"
            f"<td style='padding:6px 24px 6px 0;color:#555'>{label}</td>"
            f"<td style='padding:6px 0;font-weight:600;color:{color}'>{value}</td>"
            f"</tr>"
        )

    return f"""<div style='font-family:sans-serif;max-width:520px;color:#1a1a1a'>
  <h2 style='margin-bottom:4px'>Weekly Digest</h2>
  <p style='color:#666;margin-top:0'>{stats.practice_name} &middot; {week_range}</p>

  <table style='border-collapse:collapse;width:100%;font-size:15px;margin-top:16px'>
    {stat_row("Total calls", stats.total_calls)}
    {stat_row("Booking captures", len(stats.bookings), highlight=len(stats.bookings) > 0)}
    {stat_row("Escalations (connected)", stats.escalations)}
    {stat_row("Escalations (no answer)", stats.unanswered_escalations)}
    {stat_row("FAQ / info only", stats.faq_only)}
    {stat_row("Hung up", stats.hung_up)}
  </table>

  {booking_rows}
  {unanswered_banner}

  <p style='margin-top:32px;color:#999;font-size:12px'>
    Voice AI Receptionist &mdash; weekly summary
  </p>
</div>"""


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


@click.command()
@click.option("--dry-run", is_flag=True, help="Print emails to stdout, don't send")
@click.option("--practice-id", default=None, help="Send for one practice only")
def main(dry_run, practice_id):
    """Send weekly digest emails to all active practices."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_all_digests(dry_run=dry_run, practice_id=practice_id))


if __name__ == "__main__":
    main()
