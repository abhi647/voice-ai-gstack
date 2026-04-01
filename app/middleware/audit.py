"""
HIPAA audit logging — cross-cutting dependency.
Every endpoint that reads transcript or audio data must call audit_log().
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog


async def audit_log(
    db: AsyncSession,
    practice_id: uuid.UUID,
    event_type: str,
    actor: str,
    call_id: uuid.UUID | None = None,
) -> None:
    entry = AuditLog(
        practice_id=practice_id,
        call_id=call_id,
        event_type=event_type,
        actor=actor,
    )
    db.add(entry)
    await db.commit()
