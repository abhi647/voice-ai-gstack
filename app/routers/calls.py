"""
Twilio inbound call webhook — entry point for every patient call.

Flow:
  POST /twilio/voice (Twilio sends this when a call comes in)
    ├── Look up practice by the Twilio number dialed (To field)
    ├── If not found → hang up gracefully
    └── Return TwiML <Connect><Stream> to open a Media Streams WebSocket

Twilio Media Streams flow:
  1. Twilio calls POST /twilio/voice
  2. We return <Connect><Stream url="wss://{host}/twilio/stream"> with custom
     parameters carrying practice_id and patient_phone
  3. Twilio opens the WebSocket to /twilio/stream (app/routers/stream.py)
  4. Bidirectional μ-law 8 kHz audio flows over the WebSocket for the call duration
  5. The stream handler runs STT → Claude → TTS inline — no separate worker process
"""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.twiml.voice_response import VoiceResponse

from app.database import get_db
from app.middleware.twilio_auth import verify_twilio_signature
from app.models.practice import Practice

router = APIRouter(prefix="/twilio", tags=["calls"])


def _twiml_hangup(message: str) -> Response:
    vr = VoiceResponse()
    vr.say(message)
    vr.hangup()
    return Response(content=str(vr), media_type="application/xml")


@router.post("/voice", dependencies=[Depends(verify_twilio_signature)])
async def inbound_call(
    request: Request,
    To: str = Form(...),
    From: str = Form(...),
    CallSid: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Twilio calls this webhook when a patient dials the practice number.
    We look up the practice, then connect the call to our Media Streams handler.
    """
    practice = await Practice.get_by_twilio_number(db, twilio_number=To)

    if practice is None:
        return _twiml_hangup("This number is not in service. Goodbye.")

    # Build WebSocket URL from the incoming request host.
    # X-Forwarded-Host is set by Azure App Service / reverse proxy.
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or "localhost:8000"
    )
    stream_url = f"wss://{host}/twilio/stream"

    vr = VoiceResponse()
    connect = vr.connect()
    stream = connect.stream(url=stream_url)
    # Pass practice context as custom parameters — received in the WebSocket start event.
    stream.parameter(name="practice_id", value=str(practice.id))
    stream.parameter(name="patient_phone", value=From)

    return Response(content=str(vr), media_type="application/xml")


@router.post("/status", dependencies=[Depends(verify_twilio_signature)])
async def call_status(
    request: Request,
    CallSid: str = Form(...),
    CallStatus: str = Form(...),
) -> Response:
    """
    Twilio posts call status updates here (completed, failed, busy, etc.).
    Informational only — the stream WebSocket handles all call logic.
    Must return TwiML XML (Twilio requires Content-Type: application/xml).
    """
    import logging
    logger = logging.getLogger(__name__)
    form_data = await request.form()
    logger.info(f"Call status: {CallSid} → {CallStatus} | params={dict(form_data)}")
    return Response(content=str(VoiceResponse()), media_type="application/xml")
