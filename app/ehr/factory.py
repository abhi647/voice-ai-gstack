"""
EHR adapter factory — returns the right adapter for a practice's config.

To add a new EHR:
  1. Implement the adapter in app/ehr/<name>.py
  2. Add a case here
  3. Update PracticeConfig.ehr_adapter docstring with the new option
"""

import logging

from app.ehr.base import EHRAdapter
from app.ehr.notify import NotifyAdapter

logger = logging.getLogger(__name__)

# Adapters that are implemented and ready to use
_REGISTRY: dict[str, type] = {
    "notify": NotifyAdapter,
}

# Adapters that are stubbed — raise on use, remind us to build them
_PLANNED: set[str] = {"dentrix", "opendental", "eaglesoft", "curve"}


def get_ehr_adapter(adapter_name: str) -> EHRAdapter:
    """
    Return an EHR adapter instance for the given adapter name.

    Falls back to NotifyAdapter if the adapter is not yet implemented,
    so a practice never silently loses a booking capture.
    """
    name = adapter_name.lower().strip()

    if name in _REGISTRY:
        return _REGISTRY[name]()

    if name in _PLANNED:
        logger.warning(
            f"EHR adapter '{name}' is planned but not yet implemented — "
            f"falling back to NotifyAdapter. Build app/ehr/{name}.py to enable it."
        )
        return NotifyAdapter()

    logger.error(
        f"Unknown EHR adapter '{name}' — falling back to NotifyAdapter"
    )
    return NotifyAdapter()
