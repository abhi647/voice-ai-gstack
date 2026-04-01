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

from fastapi import APIRouter
from pydantic import BaseModel
from twilio.rest import Client

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


class EscalationRequest(BaseModel):
    call_sid: str
    practice_id: str
    patient_name: str | None = None
    patient_phone: str | None = None
    reason: str | None = None
    summary: str  # whisper text read to the human


@router.post("/escalate")
async def escalate_call(req: EscalationRequest) -> dict:
    """
    Initiate a Twilio warm transfer with whisper leg.

    The agent calls this when an escalation trigger fires.
    This endpoint is the control plane — LiveKit handles the audio.
    """
    logger.info(
        "Escalation triggered",
        extra={
            "call_sid": req.call_sid,
            "practice_id": req.practice_id,
            "reason": req.reason,
        },
    )

    # TODO(v0.2): look up practice escalation_number from DB by practice_id
    # For now: escalation number must be in the job metadata or config
    escalation_number = _get_escalation_number(req.practice_id)

    if not escalation_number:
        logger.error(f"No escalation number found for practice {req.practice_id}")
        return {"status": "error", "detail": "no escalation number configured"}

    try:
        _initiate_warm_transfer(
            call_sid=req.call_sid,
            escalation_number=escalation_number,
            patient_phone=req.patient_phone or "",
            whisper_text=req.summary,
        )
        return {"status": "escalating"}
    except Exception as e:
        logger.error(f"Twilio warm transfer failed: {e}")
        # Fall through to unanswered path
        await _handle_unanswered_escalation(req)
        return {"status": "unanswered"}


def _get_escalation_number(practice_id: str) -> str | None:
    """
    TODO(v0.2): look up from DB.
    For now returns None — caller handles the missing case.
    """
    return None


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

    # Dial the human with a whisper before bridging the patient
    client.calls.create(
        to=escalation_number,
        from_=patient_phone,
        twiml=f"""<Response>
  <Say>{whisper_text}</Say>
  <Dial>
    <Conference waitUrl="" beep="false">{conference_name}</Conference>
  </Dial>
</Response>""",
    )

    # Update the original call to join the same conference
    client.calls(call_sid).update(
        twiml=f"""<Response>
  <Dial>
    <Conference waitUrl="" beep="false">{conference_name}</Conference>
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
