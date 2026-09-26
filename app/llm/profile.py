"""The candidate profile a vacancy is scored against.

Profiles live in `profiles/<name>.json` so they are edited as data, not code,
and so several can coexist (one per role you are hunting for). The profile name
is stored on every assessment row, which keeps scores from different profiles
from being compared by accident.

`as_prompt_text()` renders the profile as compact labelled lines. A 4B model
handles that far better than raw JSON, which it tends to echo back.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "profiles",
)


class ProfileNotFound(FileNotFoundError):
    pass


@dataclass
class CandidateProfile:
    name: str
    headline: str = ""
    target_roles: list[str] = field(default_factory=list)
    seniority: list[str] = field(default_factory=list)
    strong_skills: list[str] = field(default_factory=list)
    familiar_skills: list[str] = field(default_factory=list)
    not_interested_in: list[str] = field(default_factory=list)
    languages: dict[str, list[str]] = field(default_factory=dict)
    location: dict[str, Any] = field(default_factory=dict)
    dealbreakers: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict) -> "CandidateProfile":
        known = {f for f in cls.__dataclass_fields__ if f != "raw"}
        return cls(**{k: v for k, v in data.items() if k in known}, raw=data)

    def as_prompt_text(self) -> str:
        """Render the profile for the assessment prompt."""
        languages = self.languages or {}
        fluent = ", ".join(languages.get("fluent", [])) or "unspecified"
        absent = ", ".join(languages.get("none", [])) or "none stated"
        location = self.location or {}

        lines = [
            f"Headline: {self.headline or 'unspecified'}",
            f"Target roles: {', '.join(self.target_roles) or 'any'}",
            f"Target seniority: {', '.join(self.seniority) or 'any'}",
            f"Strong skills: {', '.join(self.strong_skills) or 'unspecified'}",
            f"Familiar skills: {', '.join(self.familiar_skills) or 'none'}",
            f"Languages spoken: {fluent}",
            f"Languages NOT spoken: {absent}",
            f"Location: {location.get('country', 'any')}"
            + (f"; prefers {', '.join(location.get('preferred', []))}" if location.get("preferred") else ""),
            f"Work arrangement: {location.get('remote_preference', 'no preference')}",
            f"Not interested in: {', '.join(self.not_interested_in) or 'nothing excluded'}",
            f"Dealbreakers: {', '.join(self.dealbreakers) or 'none'}",
        ]
        return "\n".join(lines)


@lru_cache
def load_profile(name: str = "default") -> CandidateProfile:
    """Load `profiles/<name>.json`. Cached; call `load_profile.cache_clear()` after editing."""
    path = os.path.join(PROFILE_DIR, f"{name}.json")
    if not os.path.exists(path):
        available = (
            ", ".join(sorted(f[:-5] for f in os.listdir(PROFILE_DIR) if f.endswith(".json")))
            if os.path.isdir(PROFILE_DIR)
            else "none"
        )
        raise ProfileNotFound(f"no profile {name!r} in {PROFILE_DIR} (available: {available})")

    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    data.setdefault("name", name)
    return CandidateProfile.from_dict(data)
