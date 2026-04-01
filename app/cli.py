"""
Provisioning CLI — run this during the $7,900 customer onboarding.

Usage:
  python -m app.cli provision-practice [OPTIONS]
  python -m app.cli list-practices
  python -m app.cli deactivate-practice --practice-id <UUID>

Examples:
  # Onboard a new practice (minimal)
  python -m app.cli provision-practice \\
    --name "Sunrise Dental" \\
    --twilio-number +15551234567 \\
    --escalation-number +15559876543 \\
    --state NY

  # Onboard with full config
  python -m app.cli provision-practice \\
    --name "Valley Pediatric Dental" \\
    --twilio-number +16501234567 \\
    --escalation-number +16509876543 \\
    --staff-email front-desk@valleydental.com \\
    --state CA \\
    --timezone "America/Los_Angeles" \\
    --stt deepgram \\
    --tts elevenlabs \\
    --ehr-adapter notify \\
    --agent-name Sofia \\
    --services "cleaning,checkup,filling,pediatric exam" \\
    --custom-instructions "This is a pediatric practice. Speak gently. Parents call on behalf of children."
"""

import asyncio
import json
import sys
import uuid

import click
from sqlalchemy import select, text

from app.database import AsyncSessionLocal, engine
from app.models.practice import Practice
from app.models.practice_config import PracticeConfig


@click.group()
def cli():
    """Voice AI receptionist — provisioning tools."""
    pass


@cli.command("provision-practice")
@click.option("--name", required=True, help="Practice display name")
@click.option("--twilio-number", required=True, help="E.164 Twilio number (e.g. +15551234567)")
@click.option("--escalation-number", required=True, help="E.164 number to warm-transfer to")
@click.option("--staff-email", default=None, help="Staff email for booking notifications")
@click.option("--state", default="NY", help="US state code (default: NY)")
@click.option("--timezone", default="America/New_York", help="IANA timezone")
@click.option("--stt", default="deepgram", type=click.Choice(["deepgram", "sarvam"]))
@click.option("--tts", default="elevenlabs", type=click.Choice(["elevenlabs", "cartesia"]))
@click.option("--ehr-adapter", default="notify",
              type=click.Choice(["notify", "dentrix", "opendental", "eaglesoft", "curve"]))
@click.option("--agent-name", default="Aria", help="What the AI calls itself")
@click.option("--services", default=None,
              help="Comma-separated list of services (uses defaults if omitted)")
@click.option("--custom-instructions", default="", help="Extra instructions injected into system prompt")
@click.option("--dry-run", is_flag=True, help="Print what would be created without writing to DB")
def provision_practice(
    name, twilio_number, escalation_number, staff_email, state, timezone,
    stt, tts, ehr_adapter, agent_name, services, custom_instructions, dry_run,
):
    """Provision a new practice. Run this during the $7,900 onboarding."""
    asyncio.run(_provision_practice(
        name=name,
        twilio_number=twilio_number,
        escalation_number=escalation_number,
        staff_email=staff_email,
        state=state,
        timezone=timezone,
        stt=stt,
        tts=tts,
        ehr_adapter=ehr_adapter,
        agent_name=agent_name,
        services=[s.strip() for s in services.split(",")] if services else None,
        custom_instructions=custom_instructions,
        dry_run=dry_run,
    ))


async def _provision_practice(
    name, twilio_number, escalation_number, staff_email, state, timezone,
    stt, tts, ehr_adapter, agent_name, services, custom_instructions, dry_run,
):
    config = PracticeConfig(
        agent_name=agent_name,
        ehr_adapter=ehr_adapter,
        custom_instructions=custom_instructions,
    )
    if services:
        config.services = services

    practice = Practice(
        id=uuid.uuid4(),
        twilio_number=twilio_number,
        name=name,
        escalation_number=escalation_number,
        staff_email=staff_email,
        timezone=timezone,
        state=state.upper(),
        stt_provider=stt,
        tts_provider=tts,
        config=config.model_dump(),
        is_active=True,
    )

    click.echo("\n" + "=" * 60)
    click.echo(f"  Practice:          {name}")
    click.echo(f"  Twilio number:     {twilio_number}")
    click.echo(f"  Escalation to:     {escalation_number}")
    click.echo(f"  Staff email:       {staff_email or '(not set)'}")
    click.echo(f"  State:             {state.upper()}")
    click.echo(f"  Timezone:          {timezone}")
    click.echo(f"  STT provider:      {stt}")
    click.echo(f"  TTS provider:      {tts}")
    click.echo(f"  EHR adapter:       {ehr_adapter}")
    click.echo(f"  Agent name:        {agent_name}")
    click.echo(f"  Services:          {config.services_text()}")
    if custom_instructions:
        click.echo(f"  Custom:            {custom_instructions[:60]}...")
    click.echo(f"  Practice ID:       {practice.id}")
    click.echo("=" * 60)

    if dry_run:
        click.echo("\n[DRY RUN] No changes written to database.")
        return

    async with AsyncSessionLocal() as db:
        # Check for duplicate Twilio number
        existing = await db.scalar(
            select(Practice).where(Practice.twilio_number == twilio_number)
        )
        if existing:
            click.echo(
                f"\n[ERROR] Twilio number {twilio_number} is already assigned to "
                f"'{existing.name}' (id={existing.id}). Aborting.",
                err=True,
            )
            sys.exit(1)

        db.add(practice)
        await db.commit()

    click.echo(f"\n[OK] Practice '{name}' provisioned successfully.")
    click.echo(f"     ID: {practice.id}")
    click.echo("\nNext steps:")
    click.echo(f"  1. Point {twilio_number} webhook to: POST https://your-domain/twilio/voice")
    click.echo(f"  2. Set status callback to:          POST https://your-domain/twilio/status")
    click.echo(f"  3. Test with a call to {twilio_number}")
    if not staff_email:
        click.echo(f"  4. [RECOMMENDED] Set staff email: UPDATE practices SET staff_email='...' WHERE id='{practice.id}';")


@cli.command("list-practices")
@click.option("--active-only", is_flag=True, default=False, help="Show only active practices")
def list_practices(active_only):
    """List all provisioned practices."""
    asyncio.run(_list_practices(active_only=active_only))


async def _list_practices(active_only: bool):
    async with AsyncSessionLocal() as db:
        q = select(Practice)
        if active_only:
            q = q.where(Practice.is_active == True)
        result = await db.execute(q.order_by(Practice.created_at))
        practices = result.scalars().all()

    if not practices:
        click.echo("No practices found.")
        return

    click.echo(f"\n{'ID':<38} {'Name':<30} {'Number':<15} {'State':<6} {'Active'}")
    click.echo("-" * 100)
    for p in practices:
        click.echo(
            f"{str(p.id):<38} {p.name:<30} {p.twilio_number:<15} {p.state:<6} "
            f"{'YES' if p.is_active else 'no'}"
        )
    click.echo(f"\nTotal: {len(practices)}")


@cli.command("deactivate-practice")
@click.option("--practice-id", required=True, help="Practice UUID")
@click.confirmation_option(prompt="This will stop all calls for this practice. Continue?")
def deactivate_practice(practice_id):
    """Deactivate a practice (stops all inbound calls)."""
    asyncio.run(_deactivate_practice(practice_id))


async def _deactivate_practice(practice_id: str):
    async with AsyncSessionLocal() as db:
        practice = await db.scalar(
            select(Practice).where(Practice.id == uuid.UUID(practice_id))
        )
        if not practice:
            click.echo(f"[ERROR] Practice {practice_id} not found.", err=True)
            sys.exit(1)

        practice.is_active = False
        await db.commit()

    click.echo(f"[OK] Practice '{practice.name}' deactivated. Inbound calls will be rejected.")


if __name__ == "__main__":
    cli()
