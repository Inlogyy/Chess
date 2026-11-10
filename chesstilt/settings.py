"""Settings: defaults, overridden by config.toml, overridden by command-line flags."""
from __future__ import annotations

from pathlib import Path

DEFAULTS = {
    # extract
    "workers": 0,                 # 0 = CPU cores minus one
    "chunk_mb": 8,
    "keep_correspondence": False,
    # analysis
    "session_gap_minutes": 30,
    "max_rating_change": 30,
    "min_trigger_plies": 10,
    "speeds": ["ultrabullet", "bullet", "blitz", "rapid", "classical"],
    "min_games_within_player": 10,
    # system
    "memory_limit": "",           # e.g. "8GB"; empty = 60% of RAM
    "threads": 0,                 # 0 = all cores
}


def load(config_path: Path | None) -> dict:
    cfg = dict(DEFAULTS)
    if config_path and Path(config_path).exists():
        try:
            import tomllib
        except ImportError:  # Python 3.10
            raise SystemExit("Python 3.11 or newer is needed (to read config.toml).")
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        for section in data.values():
            if isinstance(section, dict):
                for k, v in section.items():
                    if k not in DEFAULTS:
                        raise SystemExit(f"Unknown setting '{k}' in {config_path}.")
                    cfg[k] = v
    speeds = [s.lower() for s in cfg["speeds"]]
    valid = {"ultrabullet", "bullet", "blitz", "rapid", "classical"}
    bad = [s for s in speeds if s not in valid]
    if bad:
        raise SystemExit(f"Unknown speed(s) in config: {bad}. Use: {sorted(valid)}")
    cfg["speeds"] = speeds
    return cfg
