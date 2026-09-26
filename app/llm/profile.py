"""The candidate profile a vacancy is scored against.

Profiles live in `profiles/<name>.json` so they are edited as data, not code, and
so several can coexist. The profile name is stored on every assessment, which
keeps scores produced under different profiles from being compared by accident.

**Skills are tiered, not flat.** A vacancy built on a stack you use in
production every day is a different proposition from one built on a language you
have only touched, even though both "match" a flat keyword list. `skills.core`,
`skills.proficient` and `skills.familiar` drive the scoring bands in the
assessment prompt - see `prompts.ASSESSMENT_SYSTEM` and ADR-0008.

**Eligibility is described separately from skill.** Where you are, what you are
authorised to work as, and which languages you speak decide whether a role is
*reachable*; they must not quietly drag down how well the work itself fits.

`as_prompt_text()` renders the profile as compact labelled lines. A 4B model
handles that far better than raw JSON, which it tends to echo back.

The older flat format (`strong_skills` / `familiar_skills`) still loads, so
existing profiles keep working.
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
    summary: str = ""
    years_experience: float | None = None
    seniority: list[str] = field(default_factory=list)

    # Tiered skills. `skills` is the current shape; the flat fields below are
    # kept so older profiles still load.
    skills: dict[str, list[str]] = field(default_factory=dict)
    strong_skills: list[str] = field(default_factory=list)
    familiar_skills: list[str] = field(default_factory=list)

    # What makes this candidate unusual, beyond a list of technologies.
    differentiators: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)

    target_roles: list[str] = field(default_factory=list)
    not_interested_in: list[str] = field(default_factory=list)

    languages: dict[str, Any] = field(default_factory=dict)
    location: dict[str, Any] = field(default_factory=dict)
    dealbreakers: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, data: dict) -> "CandidateProfile":
        known = {f for f in cls.__dataclass_fields__ if f != "raw"}
        return cls(**{k: v for k, v in data.items() if k in known}, raw=data)

    # -- tier accessors, tolerant of either profile shape --

    @property
    def core_skills(self) -> list[str]:
        return self.skills.get("core", self.strong_skills)

    @property
    def proficient_skills(self) -> list[str]:
        return self.skills.get("proficient", [])

    @property
    def peripheral_skills(self) -> list[str]:
        return self.skills.get("familiar", self.familiar_skills)

    def _language_line(self) -> tuple[str, str]:
        languages = self.languages or {}
        spoken: list[str] = []
        for level in ("native", "fluent", "proficient", "basic"):
            values = languages.get(level)
            if not values:
                continue
            if isinstance(values, str):
                values = [values]
            spoken.append(f"{', '.join(values)} ({level})")
        absent = languages.get("none") or []
        if isinstance(absent, str):
            absent = [absent]
        return ("; ".join(spoken) or "unspecified", ", ".join(absent) or "none stated")

    def as_prompt_text(self) -> str:
        """Render the profile for the assessment prompt."""
        spoken, absent = self._language_line()
        location = self.location or {}

        lines = [f"Headline: {self.headline or 'unspecified'}"]
        if self.summary:
            lines.append(f"Summary: {self.summary}")
        if self.years_experience is not None:
            lines.append(f"Years of professional experience: {self.years_experience}")

        lines += [
            f"Target seniority: {', '.join(self.seniority) or 'any'}",
            f"Target roles: {', '.join(self.target_roles) or 'any'}",
            "",
            "SKILL TIERS (these decide the score band):",
            f"  CORE - used in production, daily: {', '.join(self.core_skills) or 'unspecified'}",
            f"  PROFICIENT - used and comfortable: {', '.join(self.proficient_skills) or 'none'}",
            f"  FAMILIAR - touched only, not production depth: "
            f"{', '.join(self.peripheral_skills) or 'none'}",
        ]

        if self.differentiators:
            lines.append(f"Particular strengths: {'; '.join(self.differentiators)}")
        if self.domains:
            lines.append(f"Domains worked in: {', '.join(self.domains)}")

        lines += [
            "",
            "ELIGIBILITY (never lowers the fit score; report it separately):",
            f"  Languages spoken: {spoken}",
            f"  Languages NOT spoken: {absent}",
            f"  Currently based in: {location.get('based_in', 'unspecified')}",
            f"  Work authorisation: {location.get('work_authorization', 'unspecified')}",
            f"  Relocation: {location.get('relocation', 'unspecified')}",
            f"  Work arrangement: {location.get('remote_preference', 'no preference')}",
            "",
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
