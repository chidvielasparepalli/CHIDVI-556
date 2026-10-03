from .manager import PersonalityManager
from .loader import discover_personalities

# Compatibility snapshot for callers that import PERSONALITIES directly.
PERSONALITIES = discover_personalities()

__all__ = ["PersonalityManager", "PERSONALITIES", "discover_personalities"]
