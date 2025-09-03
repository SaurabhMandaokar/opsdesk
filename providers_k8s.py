from typing import List, Dict, Any


def get_tabs(env: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    """Optional provider hook for future use.

    Returns an empty list by default so current behavior is unchanged.
    Fill this out later to add Kubernetes-specific tabs programmatically.
    """
    return []

