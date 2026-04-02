"""
Internal API — called by the LiveKit agent, not by Twilio or external clients.
Should be firewalled from the public internet in production.

POST /internal/escalate — initiate a Twilio warm transfer with whisper leg.

Transfer sequence:
  1. Agent POSTs here with call context + whisper summary
  2. FastAPI calls Twilio REST API to create a conference
  3. Twilio dials the practice's escalation number
  4. Whisper plays to the human BEFORE the patient is bridged:
     "Transferring Jane, calling about a cleaning, requested Tuesday. Reason: bleeding."
  5. FastAPI bridges the patient into the conference
  6. LiveKit agent exits the room (handled by the agent after this returns)

If the escalation number doesn't answer:
  - Send SMS to patient: "We weren't able to reach the office. Someone will call you back."
  - Send email alert to practice
  - Log as ESCALATED_UNANSWERED
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.rest import Client

from app.config import settings
from app.database import get_db
from app.ehr import BookingRequest, get_ehr_adapter
from app.middleware.audit import audit_log
from app.models.call import Call
from app.models.practice import Practice
from app.storage import s3

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


class EscalationRequest(BaseModel):
    call_sid: str
    practice_id: str
    escalation_number: str  # E.164 — practice's on-call/emergency phone number
    patient_name: str | None = None
    patient_phone: str | None = None
    reason: str | None = None
    summary: str  # whisper text read to the human


@router.post("/escalate")
async def escalate_call(req: EscalationRequest) -> dict:
    """
    Initiate a Twilio warm transfer with whisper leg.

    The agent calls this when an escalation trigger fires.
    This endpoint is the control plane — Twilio handles the audio bridge.
    """
    logger.info(
        "Escalation triggered",
        extra={
            "call_sid": req.call_sid,
            "practice_id": req.practice_id,
            "reason": req.reason,
        },
    )

    if not req.escalation_number:
        logger.error(f"No escalation number for practice {req.practice_id}")
        return {"status": "error", "detail": "no escalation number configured"}

    try:
        _initiate_warm_transfer(
            call_sid=req.call_sid,
            escalation_number=req.escalation_number,
            patient_phone=req.patient_phone or "",
            whisper_text=req.summary,
        )
        return {"status": "escalating"}
    except Exception as e:
        logger.error(f"Twilio warm transfer failed: {e}")
        await _handle_unanswered_escalation(req)
        return {"status": "unanswered"}


def _initiate_warm_transfer(
    call_sid: str,
    escalation_number: str,
    patient_phone: str,
    whisper_text: str,
) -> None:
    """
    Use Twilio REST API to create a conference and connect the human.

    Warm transfer with whisper:
      - Twilio dials escalation_number
      - Before bridging the patient, Twilio plays whisper_text to the human only
      - Once human answers, patient is bridged in
    """
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        logger.warning("Twilio credentials not configured — skipping warm transfer")
        return

    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    conference_name = f"escalation-{call_sid}"

    # Dial the human with a whisper before bridging the patient.
    # from_ must be a Twilio-owned number — use the configured SMS number.
    client.calls.create(
        to=escalation_number,
        from_=settings.twilio_sms_from,
        twiml=f"""<Response>
  <Say>{whisper_text}</Say>
  <Dial>
    <Conference waitUrl="http://twimlets.com/holdmusic?Bucket=com.twilio.music.classical" beep="false">{conference_name}</Conference>
  </Dial>
</Response>""",
    )

    # Redirect the patient's call leg into the same conference.
    # They hear hold music until the human answers.
    client.calls(call_sid).update(
        twiml=f"""<Response>
  <Dial>
    <Conference waitUrl="http://twimlets.com/holdmusic?Bucket=com.twilio.music.classical" beep="false">{conference_name}</Conference>
  </Dial>
</Response>""",
    )


async def _handle_unanswered_escalation(req: EscalationRequest) -> None:
    """
    Escalation number didn't answer (or Twilio call failed).
    Log ESCALATED_UNANSWERED. SMS and email alerts are TODO(v0.2).
    """
    logger.warning(
        "Escalation unanswered",
        extra={
            "call_sid": req.call_sid,
            "practice_id": req.practice_id,
            "patient_phone": req.patient_phone,
        },
    )
    # TODO(v0.2): send SMS to patient + email to practice


# ---------------------------------------------------------------------------
# Finalize call — called by the LiveKit agent when the session ends
# ---------------------------------------------------------------------------


class FinalizeCallRequest(BaseModel):
    call_sid: str
    practice_id: str
    practice_name: str = ""
    practice_timezone: str = "America/New_York"
    escalation_number: str = ""   # practice's phone — for EHR adapter SMS
    staff_email: str | None = None  # practice's email — for EHR adapter email
    ehr_adapter: str = "notify"   # which EHR adapter to use
    patient_phone: str
    started_at: str  # ISO 8601 UTC datetime string
    disposition: str  # BOOKING_CAPTURED | ESCALATED | ESCALATED_UNANSWERED | HUNG_UP | FAQ_ONLY
    patient_name: str | None = None
    requested_time: str | None = None
    service_type: str | None = None
    notes: str | None = None
    transcript: str | None = None
    twilio_recording_url: str | None = None  # if set, fetch from Twilio and upload to S3


@router.post("/finalize_call")
async def finalize_call(req: FinalizeCallRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """
    Persist a completed call to PostgreSQL and S3.

    Called by the LiveKit agent at session end.
    Idempotent on call_sid — if the record already exists (e.g. duplicate delivery),
    returns the existing call_id.
    """
    logger.info(
        "Finalizing call",
        extra={
            "call_sid": req.call_sid,
            "practice_id": req.practice_id,
            "disposition": req.disposition,
        },
    )

    # Idempotency check — don't double-write if agent delivers twice
    existing = await db.scalar(select(Call).where(Call.twilio_call_sid == req.call_sid))
    if existing:
        logger.info(f"Call {req.call_sid} already finalized — skipping")
        return {"status": "already_finalized", "call_id": str(existing.id)}

    # Upload transcript to S3 before writing the DB row
    transcript_key = None
    if req.transcript:
        try:
            transcript_key = s3.upload_transcript(req.practice_id, req.call_sid, req.transcript)
        except Exception as e:
            logger.error(f"Transcript S3 upload failed: {e} — proceeding without S3 key")

    # Upload recording from Twilio to S3 (if URL provided)
    audio_key = None
    if req.twilio_recording_url:
        try:
            audio_key = s3.upload_recording_from_url(
                req.practice_id, req.call_sid, req.twilio_recording_url
            )
        except Exception as e:
            logger.error(f"Recording S3 upload failed: {e} — proceeding without audio")

    # Parse started_at
    try:
        started_at = datetime.fromisoformat(req.started_at.replace("Z", "+00:00"))
    except ValueError:
        started_at = datetime.now(timezone.utc)

    # Write Call record
    call = Call(
        twilio_call_sid=req.call_sid,
        practice_id=req.practice_id,
        patient_phone=req.patient_phone,
        started_at=started_at,
        ended_at=datetime.now(timezone.utc),
        disposition=req.disposition,
        patient_name=req.patient_name,
        requested_time=req.requested_time,
        service_type=req.service_type,
        transcript=req.transcript,
        transcript_s3_key=transcript_key,
        audio_s3_key=audio_key,
    )
    db.add(call)
    await db.flush()  # get the auto-generated call.id before commit

    # HIPAA audit log
    await audit_log(
        db=db,
        practice_id=req.practice_id,
        event_type="call_finalized",
        actor="livekit_agent",
        call_id=call.id,
    )

    await db.commit()

    # For booking captures, notify practice staff via the configured EHR adapter.
    # This runs after the DB commit — a notification failure never rolls back the call record.
    booking_result = None
    if req.disposition == "BOOKING_CAPTURED":
        adapter = get_ehr_adapter(req.ehr_adapter)
        booking_req = BookingRequest(
            practice_id=req.practice_id,
            practice_name=req.practice_name,
            practice_timezone=req.practice_timezone,
            escalation_number=req.escalation_number,
            staff_email=req.staff_email,
            patient_name=req.patient_name,
            patient_phone=req.patient_phone,
            service_type=req.service_type,
            requested_time=req.requested_time,
            notes=req.notes,
            call_sid=req.call_sid,
        )
        booking_result = await adapter.submit_booking(booking_req)
        logger.info(
            f"EHR adapter '{req.ehr_adapter}' result for {req.call_sid}: "
            f"success={booking_result.success}, {booking_result.message}"
        )

    logger.info(f"Call {req.call_sid} finalized — id={call.id}, disposition={req.disposition}")
    return {
        "status": "ok",
        "call_id": str(call.id),
        "notification": booking_result.message if booking_result else None,
    }
