"""
Tests for HIPAA disclosure wording (app/agent/disclosures.py).

Coverage:
  get_disclosure
    ├── [✓] NY (default) — single-party consent wording
    ├── [✓] CA — two-party consent wording ("By continuing, you consent")
    ├── [✓] FL — two-party (all-party) state
    ├── [✓] WA — two-party state
    ├── [✓] sms_enabled=False strips SMS opt-in sentence
    └── [✓] state comparison is case-insensitive
"""

import pytest

from app.agent.disclosures import get_disclosure


class TestGetDisclosure:
    def test_ny_default_single_party(self):
        text = get_disclosure("NY")
        assert "may be recorded" in text
        assert "By continuing, you consent" not in text

    def test_ca_two_party_consent(self):
        text = get_disclosure("CA")
        assert "By continuing, you consent" in text

    def test_fl_is_two_party(self):
        text = get_disclosure("FL")
        assert "By continuing, you consent" in text

    def test_wa_is_two_party(self):
        text = get_disclosure("WA")
        assert "By continuing, you consent" in text

    def test_tx_is_single_party(self):
        text = get_disclosure("TX")
        assert "may be recorded" in text
        assert "By continuing" not in text

    def test_sms_disabled_removes_sms_sentence(self):
        text = get_disclosure("NY", sms_enabled=False)
        assert "text message" not in text

    def test_sms_enabled_includes_sms_sentence(self):
        text = get_disclosure("NY", sms_enabled=True)
        assert "text message" in text

    def test_lowercase_state_code_works(self):
        text_upper = get_disclosure("CA")
        text_lower = get_disclosure("ca")
        assert text_upper == text_lower
