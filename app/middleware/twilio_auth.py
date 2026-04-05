"""
Twilio request signature verification dependency.

SEC-1: Any internet client can POST to /twilio/voice without this check, burning
Deepgram/Claude/ElevenLabs quota and injecting fabricated call records.

Usage:
    @router.post("/voice", dependencies=[Depends(verify_twilio_signature)])
    async def inbound_call(...):

How it works:
  Twilio signs every webhook request with HMAC-SHA1 over the URL + sorted POST params.
  The signature is sent as X-Twilio-Signature. We validate using twilio.request_validator.

Behind a reverse proxy (Azure App Service):
  Twilio signs using the public-facing URL (https://voice-ai-app.azurewebsites.net/...).
  The app sees http://127.0.0.1/... internally. We reconstruct the public URL using
  X-Forwarded-Proto and X-Forwarded-Host headers set by Azure.

Development:
  If TWILIO_AUTH_TOKEN is not set, validation is skipped entirely.
  Set it in .env to test locally with ngrok.
"""

import logging

from fastapi import Depends, HTTPException, Request
from twilio.request_validator import RequestValidator

from app.config import settings

logger = logging.getLogger(__name__)


def _reconstruct_public_url(request: Request) -> str:
    """
    Reconstruct the public-facing URL that Twilio signed.

    Azure App Service sets X-Forwarded-Proto and X-Forwarded-Host. Without these
    we'd validate against the internal URL and every request would fail.
    """
    proto = (
        request.headers.get("x-forwarded-proto")
        or request.url.scheme
    )
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or "localhost:8000"
    )
    path = request.url.path
    query = request.url.query

    url = f"{proto}://{host}{path}"
    if query:
        url += f"?{query}"
    return url


async def verify_twilio_signature(request: Request) -> None:
    """
    FastAPI dependency: validates X-Twilio-Signature on inbound Twilio webhooks.

    Raises HTTP 403 if the signature is missing or invalid.
    Skips validation if TWILIO_AUTH_TOKEN is not configured (development mode).
    """
    if not settings.twilio_auth_token:
        logger.debug("TWILIO_AUTH_TOKEN not set — skipping signature check (dev mode)")
        return

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        logger.warning("Missing X-Twilio-Signature header on Twilio webhook")
        raise HTTPException(status_code=403, detail="Missing Twilio signature")

    # Read and cache form data — FastAPI will serve Form(...) params from this cache.
    form = await request.form()
    params = dict(form)

    url = _reconstruct_public_url(request)
    validator = RequestValidator(settings.twilio_auth_token)

    if not validator.validate(url, params, signature):
        logger.warning(
            "Invalid Twilio signature",
            extra={"url": url, "signature_prefix": signature[:8]},
        )
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
