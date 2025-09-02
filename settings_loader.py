import os, sys, json, re
from pathlib import Path

def _slugify(s: str) -> str:
    s = s.lower().strip()
    return re.sub(r"[^a-z0-9_-]+", "-", s).strip("-")

def _load_profiles(settings_dir: Path) -> dict:
    files = sorted(settings_dir.glob("*.json"))
    if not files:
        raise SystemExit(f"No JSON files found in {settings_dir}")
    profiles = {}
    for f in files:
        try:
            with f.open(encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:
            raise SystemExit(f"Failed to parse {f.name}: {e}")
        name = data.get("name") or f.stem
        key = _slugify(name)
        if key in profiles:
            raise SystemExit(f"Duplicate profile name '{key}' from {f.name}")
        profiles[key] = data
    return profiles

def load_settings(settings_dir: str = "/app/settings", env_var: str = "OPSDESK_ENV"):
    select = os.getenv(env_var)
    settings_path = Path(settings_dir)
    profiles = _load_profiles(settings_path)

    if select:
        key = _slugify(select)
        if key not in profiles:
            raise SystemExit(f"Unknown {env_var}='{select}'. Available: {', '.join(profiles)}")
        return key, profiles[key]

    if "default" in profiles:
        return "default", profiles["default"]
    if len(profiles) == 1:
        k = next(iter(profiles))
        return k, profiles[k]
    raise SystemExit(f"Set {env_var} to one of: {', '.join(profiles)}")
