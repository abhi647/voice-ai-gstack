"""
S3 storage for call transcripts and audio recordings.

All objects are stored with SSE-KMS encryption (AES-256 via AWS KMS).
Bucket: us-east-1, HIPAA-eligible region.
Retention: 6 years (configured via S3 lifecycle policy on the bucket).

Key layout:
  practices/{practice_id}/calls/{call_sid}/transcript.txt
  practices/{practice_id}/calls/{call_sid}/recording.mp3  ← uploaded by Twilio callback

Never store PHI in S3 object keys or metadata — only in the encrypted body.
"""

import logging
from io import BytesIO

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings

logger = logging.getLogger(__name__)


def _s3_client():
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
    )


def transcript_key(practice_id: str, call_sid: str) -> str:
    return f"practices/{practice_id}/calls/{call_sid}/transcript.txt"


def recording_key(practice_id: str, call_sid: str) -> str:
    return f"practices/{practice_id}/calls/{call_sid}/recording.mp3"


def upload_transcript(practice_id: str, call_sid: str, transcript_text: str) -> str:
    """
    Upload call transcript to S3 with SSE-KMS encryption.

    Returns the S3 key on success.
    Raises on error — caller decides whether to retry or log.
    """
    key = transcript_key(practice_id, call_sid)

    if not settings.s3_bucket:
        logger.warning("S3 bucket not configured — skipping transcript upload")
        return key

    client = _s3_client()
    client.put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=BytesIO(transcript_text.encode("utf-8")),
        ContentType="text/plain; charset=utf-8",
        ServerSideEncryption="aws:kms",  # SSE-KMS, AES-256
    )

    logger.info(f"Transcript uploaded: s3://{settings.s3_bucket}/{key}")
    return key


def upload_recording_from_url(practice_id: str, call_sid: str, twilio_recording_url: str) -> str:
    """
    Fetch a Twilio call recording and store it in S3 with SSE-KMS.

    Twilio makes recordings available at a URL after the call ends.
    We fetch the audio and re-upload to our HIPAA-eligible S3 bucket
    so we never rely on Twilio's storage for PHI.

    Returns the S3 key on success.
    """
    import httpx

    key = recording_key(practice_id, call_sid)

    if not settings.s3_bucket:
        logger.warning("S3 bucket not configured — skipping recording upload")
        return key

    # Fetch from Twilio (authenticated with account credentials)
    response = httpx.get(
        twilio_recording_url,
        auth=(settings.twilio_account_sid, settings.twilio_auth_token),
        timeout=30.0,
    )
    response.raise_for_status()

    client = _s3_client()
    client.put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=BytesIO(response.content),
        ContentType="audio/mpeg",
        ServerSideEncryption="aws:kms",
    )

    logger.info(f"Recording uploaded: s3://{settings.s3_bucket}/{key}")
    return key
