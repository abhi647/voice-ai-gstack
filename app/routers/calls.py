"""
Twilio inbound call webhook — entry point for every patient call.

Flow:
  POST /twilio/voice (Twilio sends this when a call comes in)
    ├── Look up practice by the Twilio number dialed (To field)
    ├── If not found → hang up gracefully
    ├── If subscription lapsed (is_active=False) → hang up
    └── Return TwiML to connect the call to LiveKit via SIP
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.twiml.voice_response import VoiceResponse

from app.database import get_db
from app.models.practice import Practice

router = APIRouter(prefix="/twilio", tags=["calls"])


def _twiml_hangup(message: str) -> Response:
    vr = VoiceResponse()
    vr.say(message)
    vr.hangup()
    return Response(content=str(vr), media_type="application/xml")


@router.post("/voice")
async def inbound_call(
    request: Request,
    To: str = Form(...),
    From: str = Form(...),
    CallSid: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Twilio calls this webhook when a patient dials the practice number.
    We look up the practice, then hand the call to LiveKit.
    """
    practice = await Practice.get_by_twilio_number(db, twilio_number=To)

    if practice is None:
        return _twiml_hangup("This number is not in service. Goodbye.")

    # Practice found — connect to LiveKit agent via SIP.
    # We serialize the full practice config into the TwiML <Parameter> block so the
    # LiveKit agent job receives everything it needs without a DB round-trip.
    import json

    config = practice.get_config()
    metadata = json.dumps({
        "practice_id": str(practice.id),
        "practice_name": practice.name,
        "practice_state": practice.state,
        "practice_timezone": practice.timezone,
        "escalation_number": practice.escalation_number,
        "staff_email": practice.staff_email,
        "call_sid": CallSid,
        "patient_phone": From,
        "stt_provider": practice.stt_provider,
        "tts_provider": practice.tts_provider,
        "config": config.model_dump(),
    })

    # TODO(v0.2): replace with real LiveKit SIP connect TwiML + metadata parameter
    vr = VoiceResponse()
    vr.say(
        f"Thank you for calling {practice.name}. "
        "Please hold while our AI assistant connects."
    )
    # TODO: vr.connect() → LiveKit SIP trunk with metadata=metadata
    return Response(content=str(vr), media_type="application/xml")


@router.post("/status")
async def call_status(
    CallSid: str = Form(...),
    CallStatus: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Twilio calls this when a call ends or changes status.
    Used to mark calls as ended and trigger S3 upload.
    """
    # TODO(v0.2): look up call by CallSid, update ended_at + disposition
    return {"status": "received"}
