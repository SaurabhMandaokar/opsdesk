import json
import os
from pathlib import Path
from typing import Any, Dict


def load_config() -> Dict[str, Any]:
    """Load config from OPSDESK_CONFIG or ~/.config/opsdesk/config.json.

    Returns an empty dict if no file is found or parsing fails.
    Supported keys (initial):
      - kubeconfig_glob: string glob pattern for kubeconfig files
    """
    path = os.environ.get("OPSDESK_CONFIG", "").strip() or str(
        Path.home() / ".config" / "opsdesk" / "config.json"
    )
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

