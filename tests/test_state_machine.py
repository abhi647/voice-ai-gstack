"""
Tests for the conversation state machine (app/agent/state.py).

Coverage:
  ConversationContext
    ├── [✓] initial state is GREETING
    ├── [✓] transition() changes state
    ├── [✓] append_transcript() accumulates lines
    ├── [✓] full_transcript() joins lines
    ├── [✓] check_for_escalation_keyword — emergency words trigger
    ├── [✓] check_for_escalation_keyword — human request words trigger
    ├── [✓] check_for_escalation_keyword — normal booking talk does NOT trigger
    ├── [✓] should_escalate_due_to_timeout — False before 4 minutes
    ├── [✓] should_escalate_due_to_timeout — True after 4 minutes (mocked)
    └── [✓] should_escalate_due_to_timeout — False when already ESCALATING

  BookingIntent
    ├── [✓] is_complete() True when name + service_type present
    ├── [✓] is_complete() False when name missing
    └── [✓] is_complete() False when service_type missing
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.agent.state import (
    BookingIntent,
    ConversationContext,
    ConversationState,
)


def make_ctx(**kwargs) -> ConversationContext:
    return ConversationContext(
        practice_id=kwargs.get("practice_id", "test-practice"),
        practice_name=kwargs.get("practice_name", "Sunrise Dental"),
        practice_state=kwargs.get("practice_state", "NY"),
        practice_timezone=kwargs.get("practice_timezone", "America/New_York"),
        call_sid=kwargs.get("call_sid", "CA123"),
        patient_phone=kwargs.get("patient_phone", "+15550000000"),
    )


class TestConversationContext:
    def test_initial_state_is_greeting(self):
        ctx = make_ctx()
        assert ctx.state == ConversationState.GREETING

    def test_transition_changes_state(self):
        ctx = make_ctx()
        ctx.transition(ConversationState.IDENTIFY_PATIENT)
        assert ctx.state == ConversationState.IDENTIFY_PATIENT

    def test_append_transcript_accumulates(self):
        ctx = make_ctx()
        ctx.append_transcript("AGENT", "Hello!")
        ctx.append_transcript("PATIENT", "Hi there.")
        assert len(ctx.transcript_lines) == 2

    def test_full_transcript_joins_lines(self):
        ctx = make_ctx()
        ctx.append_transcript("AGENT", "Hello!")
        ctx.append_transcript("PATIENT", "Hi.")
        transcript = ctx.full_transcript()
        assert "AGENT: Hello!" in transcript
        assert "PATIENT: Hi." in transcript

    # Escalation keyword tests
    def test_emergency_keyword_triggers_escalation(self):
        ctx = make_ctx()
        assert ctx.check_for_escalation_keyword("I'm bleeding") == "bleeding"

    def test_pain_keyword_triggers_escalation(self):
        ctx = make_ctx()
        assert ctx.check_for_escalation_keyword("I'm in a lot of pain") == "pain"

    def test_human_request_triggers_escalation(self):
        ctx = make_ctx()
        assert ctx.check_for_escalation_keyword("I want to speak to a real person") == "real person"

    def test_billing_triggers_escalation(self):
        ctx = make_ctx()
        assert ctx.check_for_escalation_keyword("I have a question about billing") == "billing"

    def test_normal_booking_does_not_trigger(self):
        ctx = make_ctx()
        assert ctx.check_for_escalation_keyword("I'd like to book a cleaning appointment") is None

    def test_greeting_does_not_trigger(self):
        ctx = make_ctx()
        assert ctx.check_for_escalation_keyword("Hi, yes I can make Tuesday work") is None

    # Timeout tests
    def test_no_timeout_before_4_minutes(self):
        ctx = make_ctx()
        # started_at is just now — well under 240 seconds
        assert ctx.should_escalate_due_to_timeout() is False

    def test_timeout_after_4_minutes(self):
        ctx = make_ctx()
        # Fake started_at to 5 minutes ago
        ctx.started_at = datetime.now(timezone.utc) - timedelta(seconds=300)
        assert ctx.should_escalate_due_to_timeout() is True

    def test_no_timeout_when_already_escalating(self):
        ctx = make_ctx()
        ctx.started_at = datetime.now(timezone.utc) - timedelta(seconds=300)
        ctx.transition(ConversationState.ESCALATING)
        # Already escalating — should not re-trigger
        assert ctx.should_escalate_due_to_timeout() is False

    def test_no_timeout_when_transferred(self):
        ctx = make_ctx()
        ctx.started_at = datetime.now(timezone.utc) - timedelta(seconds=300)
        ctx.transition(ConversationState.TRANSFERRED)
        assert ctx.should_escalate_due_to_timeout() is False


class TestBookingIntent:
    def test_complete_when_name_and_service_present(self):
        b = BookingIntent(patient_name="Jane Smith", service_type="cleaning")
        assert b.is_complete() is True

    def test_incomplete_without_name(self):
        b = BookingIntent(service_type="cleaning")
        assert b.is_complete() is False

    def test_incomplete_without_service(self):
        b = BookingIntent(patient_name="Jane Smith")
        assert b.is_complete() is False

    def test_incomplete_when_both_missing(self):
        b = BookingIntent()
        assert b.is_complete() is False
