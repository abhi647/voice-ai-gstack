"""
HIPAA call recording disclosures, parameterized by practice state.

California requires two-party consent — different wording from the default.
The AI must deliver the disclosure verbatim in the first utterance, not paraphrase it.

SMS opt-in language is appended when the practice has SMS enabled (default true).
"""

# Default: single-party consent states
_DEFAULT_DISCLOSURE = (
    "This call may be recorded for quality and training purposes. "
    "We may also send you a text message confirmation."
)

# California: two-party consent required
_CA_DISCLOSURE = (
    "This call is being recorded. By continuing, you consent to the recording. "
    "We may also send you a text message confirmation."
)

# States with two-party (all-party) consent requirements
_TWO_PARTY_STATES = frozenset(["CA", "FL", "IL", "MD", "MA", "MI", "MT", "NH", "OR", "PA", "WA"])


def get_disclosure(state: str, sms_enabled: bool = True) -> str:
    """
    Return the correct recording disclosure for the practice's state.

    Args:
        state: Two-letter US state code (e.g. "CA", "NY")
        sms_enabled: Whether the practice has SMS confirmation enabled

    Returns:
        Disclosure string to be read verbatim at the start of the call.
    """
    if state.upper() in _TWO_PARTY_STATES:
        base = _CA_DISCLOSURE
    else:
        base = _DEFAULT_DISCLOSURE

    if not sms_enabled:
        # Strip the SMS opt-in sentence
        base = base.replace(" We may also send you a text message confirmation.", "")

    return base
