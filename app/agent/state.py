"""
Conversation state machine for the voice AI receptionist.

States:
  GREETING           — agent introduces itself, delivers HIPAA disclosure
  IDENTIFY_PATIENT   — collect patient name and date of birth
  UNDERSTAND_INTENT  — determine what the patient needs (booking, FAQ, other)
  COLLECT_DETAILS    — gather appointment details (date, time, service type)
  CONFIRM_BOOKING    — read back details, ask patient to confirm
  COMPLETE           — booking captured, goodbye

At any state:
  ESCALATING         — transferring to a human (triggered by keyword or timeout)
  TRANSFERRED        — call handed off, agent exits

Transitions:
  GREETING → IDENTIFY_PATIENT (after disclosure acknowledged)
  IDENTIFY_PATIENT → UNDERSTAND_INTENT (after name collected)
  UNDERSTAND_INTENT → COLLECT_DETAILS (booking intent)
  UNDERSTAND_INTENT → COMPLETE (FAQ only — no booking needed)
  UNDERSTAND_INTENT → ESCALATING (billing, insurance, results, etc.)
  COLLECT_DETAILS → CONFIRM_BOOKING (all details collected)
  CONFIRM_BOOKING → COMPLETE (patient confirms)
  CONFIRM_BOOKING → COLLECT_DETAILS (patient corrects details)
  * → ESCALATING (escalation keyword or 4-minute timeout)
  ESCALATING → TRANSFERRED (Twilio warm transfer initiated)
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class ConversationState(str, Enum):
    GREETING = "GREETING"
    IDENTIFY_PATIENT = "IDENTIFY_PATIENT"
    UNDERSTAND_INTENT = "UNDERSTAND_INTENT"
    COLLECT_DETAILS = "COLLECT_DETAILS"
    CONFIRM_BOOKING = "CONFIRM_BOOKING"
    COMPLETE = "COMPLETE"
    ESCALATING = "ESCALATING"
    TRANSFERRED = "TRANSFERRED"


# Keywords that trigger immediate escalation regardless of current state.
# Keep this list conservative — false negatives (missing an emergency) are
# worse than false positives (unnecessary transfer).
ESCALATION_KEYWORDS = frozenset(
    [
        # Emergency / medical urgency
        "emergency",
        "urgent",
        "hurting",
        "pain",
        "bleeding",
        "allergic",
        "reaction",
        "swelling",
        "swollen",
        "broken",
        "fell out",
        "knocked out",
        "can't breathe",
        # Human request
        "speak to someone",
        "talk to someone",
        "real person",
        "human",
        "speak to a person",
        "receptionist",
        # Clinical topics that need a human
        "billing",
        "insurance",
        "referral",
        "results",
        "prescription",
        "medication",
        "refill",
        "test results",
        "x-ray",
        "x ray",
        "records",
        "cancel",
        "cancellation",
    ]
)

# Maximum call duration before forcing escalation (seconds).
MAX_CALL_DURATION_BEFORE_ESCALATE = 240  # 4 minutes


@dataclass
class BookingIntent:
    """Structured capture of what the patient needs."""
    patient_name: str | None = None
    patient_phone: str | None = None  # populated from Twilio From field
    requested_date: str | None = None
    requested_time: str | None = None
    service_type: str | None = None  # "cleaning", "checkup", "extraction", etc.
    notes: str | None = None

    def is_complete(self) -> bool:
        """All fields needed to send the booking email are present."""
        return bool(self.patient_name and self.service_type)


@dataclass
class ConversationContext:
    """
    In-memory state for one call. Lives for the duration of the LiveKit session.
    Serialized to PostgreSQL at call end via POST /internal/finalize_call.
    """

    practice_id: str
    practice_name: str
    practice_state: str       # US state code — drives HIPAA disclosure wording
    practice_timezone: str    # IANA timezone — for after-hours check
    call_sid: str
    patient_phone: str
    escalation_number: str = ""   # where to warm-transfer + where to SMS booking requests
    staff_email: str | None = None  # where to email booking notifications
    ehr_adapter: str = "notify"     # which EHR adapter to use at call end
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    state: ConversationState = ConversationState.GREETING
    booking: BookingIntent = field(default_factory=BookingIntent)
    escalation_reason: str | None = None
    transcript_lines: list[str] = field(default_factory=list)

    def elapsed_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.started_at).total_seconds()

    def should_escalate_due_to_timeout(self) -> bool:
        return (
            self.elapsed_seconds() >= MAX_CALL_DURATION_BEFORE_ESCALATE
            and self.state not in (ConversationState.ESCALATING, ConversationState.TRANSFERRED)
        )

    def check_for_escalation_keyword(self, text: str) -> str | None:
        """Return the matched keyword if any escalation trigger is found, else None."""
        lowered = text.lower()
        for keyword in ESCALATION_KEYWORDS:
            if keyword in lowered:
                return keyword
        return None

    def transition(self, new_state: ConversationState) -> None:
        self.state = new_state

    def append_transcript(self, speaker: str, text: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.transcript_lines.append(f"[{ts}] {speaker}: {text}")

    def full_transcript(self) -> str:
        return "\n".join(self.transcript_lines)
