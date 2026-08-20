"""Candidate selection and ranking for TV season demotion.

Mirrors score.py's split between hard filters (eligibility) and weights
(ranking), at SEASON grain instead of per-movie. See app/tv_loop.py's module
docstring for why a season -- not a series, not an episode -- is the unit a
user selects and downgrades.

Deliberately NOT filtered here: the series' qualityProfileId. A Sonarr series
has one profile shared by every season, but downgrade is per-season, so
"already on the archive profile" would make every season but the first one
demoted permanently invisible. Eligibility is judged purely from the episode
files' own tier.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

GB = 1024 ** 3
MONTH_SECONDS = 30.44 * 24 * 3600


@dataclass
class SeasonCandidate:
    series: dict                 # Sonarr series resource
    season_number: int
    source_files: list[dict]     # episode files still on a tv_source_tiers tier
    all_files: list[dict]        # every episode file in the season, any tier
    size: int                    # sum(f.size for f in source_files)
    tier: str | None             # uniform tier of source_files; None if mixed
    clean: bool                  # all_files == source_files -> season-pack eligible
    last_aired_ts: float | None
    next_airing: bool
    reclaim: int = 0
    components: dict[str, float] = field(default_factory=dict)
    score: float = 0.0

    @property
    def series_id(self) -> int:
        return int(self.series["id"])

    @property
    def title(self) -> str:
        return f"{self.series.get('title', '?')} — Season {self.season_number}"


def _episode_file_tier(f: dict) -> str:
    return ((f.get("quality") or {}).get("quality") or {}).get("name") or "?"


def _season_statistics(series: dict, season_number: int) -> dict:
    for s in series.get("seasons") or []:
        if int(s.get("seasonNumber", -1)) == season_number:
            return s.get("statistics") or {}
    return {}


def _parse_date(value: Any) -> float | None:
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def assemble_seasons(series: dict, episode_files: list[dict],
                      source_tiers: set[str]) -> list[SeasonCandidate]:
    """Group one series' episode files by seasonNumber -- already present on
    the Sonarr episodefile resource, no episodes() join needed for this step.
    One SeasonCandidate per season number with at least one file."""
    by_season: dict[int, list[dict]] = {}
    for f in episode_files:
        sn = f.get("seasonNumber")
        if sn is None:
            continue
        by_season.setdefault(int(sn), []).append(f)

    out: list[SeasonCandidate] = []
    for sn, files in by_season.items():
        source = [f for f in files if _episode_file_tier(f) in source_tiers]
        tiers = {_episode_file_tier(f) for f in source}
        tier = tiers.pop() if len(tiers) == 1 else None
        stats = _season_statistics(series, sn)
        out.append(SeasonCandidate(
            series=series, season_number=sn,
            source_files=source, all_files=files,
            size=sum(int(f.get("size") or 0) for f in source),
            tier=tier, clean=(len(source) == len(files)),
            last_aired_ts=_parse_date(stats.get("previousAiring")),
            next_airing=bool(stats.get("nextAiring")),
        ))
    return out


def eligible(raw: list[SeasonCandidate], settings: dict,
             exclusion_tag_id: int | None,
             expected_per_episode_bytes: int,
             season_blocklist: set[tuple[int, int]] | None = None,
             ) -> tuple[list[SeasonCandidate], dict[str, int]]:
    """Apply hard filters. Returns (candidates, rejection counts)."""
    window_months = float(settings.get("tv_new_release_window_months", 6))
    window_seconds = window_months * MONTH_SECONDS
    include_specials = bool(settings.get("tv_include_specials", False))
    season_blocklist = season_blocklist or set()
    now = time.time()

    rejected = {"no_files": 0, "specials_excluded": 0, "still_airing": 0,
                "inside_new_release_window": 0, "no_gain": 0,
                "season_blocklisted": 0, "excluded_tag": 0}
    out: list[SeasonCandidate] = []

    for c in raw:
        if not c.source_files:
            rejected["no_files"] += 1
            continue

        if c.season_number == 0 and not include_specials:
            rejected["specials_excluded"] += 1
            continue

        if (c.series_id, c.season_number) in season_blocklist:
            rejected["season_blocklisted"] += 1
            continue

        if exclusion_tag_id is not None and \
                exclusion_tag_id in (c.series.get("tags") or []):
            rejected["excluded_tag"] += 1
            continue

        if c.next_airing:
            rejected["still_airing"] += 1
            continue

        # Unknown air date is treated as inside the window: the conservative
        # direction, same posture as score.home_release_ts.
        if c.last_aired_ts is None or (now - c.last_aired_ts) < window_seconds:
            rejected["inside_new_release_window"] += 1
            continue

        reclaim = c.size - expected_per_episode_bytes * len(c.source_files)
        if reclaim <= 0:
            rejected["no_gain"] += 1
            continue

        c.reclaim = reclaim
        out.append(c)
    return out, rejected


def _norm(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def rank(candidates: list[SeasonCandidate], settings: dict) -> list[SeasonCandidate]:
    if not candidates:
        return []
    now = time.time()

    w_impact = float(settings.get("tv_w_impact", 1.0))
    w_age = float(settings.get("tv_w_age", 0.5))

    impact = _norm([float(c.reclaim) for c in candidates])
    age = _norm([now - (c.last_aired_ts or now) for c in candidates])

    total_w = w_impact + w_age or 1.0
    for i, c in enumerate(candidates):
        c.components = {"impact": impact[i], "age": age[i]}
        c.score = (w_impact * impact[i] + w_age * age[i]) / total_w

    return sorted(candidates, key=lambda c: -c.score)
