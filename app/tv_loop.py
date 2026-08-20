"""TV (Sonarr) season demotion -- manual only, triggered from the TV
Candidates page.

A season, not a series or an episode, is the unit a user selects and
downgrades: series is too coarse (a show can span years of quality
history), episode is too fine (grabbing dozens of individual releases for
one show is slow and indexer-unfriendly). Downgrading a season tries a
single season-pack release first; when none qualifies it falls back to
searching + grabbing a smaller release per episode, so a season can
partially succeed.

Unlike the movie loop this has no space-driven automatic pass (yet) --
every downgrade here is a user's explicit selection, run once, with no
deficit/batch-size gating (same reasoning as loop.run_selected).

Reuses, unchanged, from loop.py: _tier_ladder (generic over any profile +
current tier), _SOFT_REJECTION_MARKERS/_rejection_is_soft (pure string
matching), validate_archive_profile (generic over any profile dict).
"""
from __future__ import annotations

import time

from . import db, tv_score
from .arr import Sonarr, sonarr_from_env
from .audit import cached_episode_files, cached_series, tv_archive_tier_median_bytes
from .loop import _rejection_is_soft, _tier_ladder, validate_archive_profile

GB = 1024 ** 3


class TvLoopAbort(RuntimeError):
    pass


def find_archive_profile(sonarr: Sonarr, name: str) -> dict | None:
    for p in sonarr.quality_profiles():
        if p.get("name", "").lower() == name.lower():
            return p
    return None


def build_season_candidates(settings: dict, sonarr: Sonarr) -> dict:
    """Rank season candidates by impact and age. Mirrors loop.build_candidates
    but at season grain; deliberately does NOT go through audit.inventory()
    since that Sonarr walk is best-effort/swallow-errors -- wrong for a page
    that needs to surface a hard Sonarr-down error to the user."""
    source_tiers = set(settings.get("tv_source_tiers") or ["Remux-1080p"])
    series_list = cached_series(sonarr, force=False)

    raw: list[tv_score.SeasonCandidate] = []
    records: list[dict] = []
    for s in series_list:
        files = cached_episode_files(sonarr, int(s["id"]))
        raw.extend(tv_score.assemble_seasons(s, files, source_tiers))
        for f in files:
            tier = ((f.get("quality") or {}).get("quality") or {}).get("name") or "?"
            records.append({"kind": "tv", "tier": tier, "size": int(f.get("size") or 0)})

    target_per_episode = tv_archive_tier_median_bytes(records, settings["tv_archive_tier"])

    excl_id = None
    try:
        for t in sonarr.tags():
            if t["label"].lower() == str(settings["tv_exclusion_tag"]).lower():
                excl_id = int(t["id"])
    except Exception:  # noqa: BLE001
        pass

    cands, rejected = tv_score.eligible(
        raw, settings, excl_id, target_per_episode, db.tv_blocklist_season_ids())
    ranked = tv_score.rank(cands, settings)
    return {"ranked": ranked, "rejected": rejected,
            "target_per_episode_bytes": target_per_episode}


def _filter_releases(releases: list[dict], tiers: list[str], max_bytes: int,
                     same_tier: str | None,
                     same_tier_min_reduction: float) -> tuple[dict | None, str | None]:
    """Tier-match / soft-rejection-only / size-below-max_bytes filtering,
    shared by the season-pack and per-episode pickers below -- the same
    logic as loop._pick_release's inner loop, factored out so a season and
    an episode search don't duplicate it."""
    for tier in tiers:
        matches = []
        for r in releases:
            name = ((r.get("quality") or {}).get("quality") or {}).get("name", "")
            if name.lower() != tier.lower():
                continue
            if not all(_rejection_is_soft(x) for x in (r.get("rejections") or [])):
                continue
            size = int(r.get("size") or 0)
            if max_bytes and size:
                if size >= max_bytes:
                    continue
                if same_tier and tier.lower() == same_tier.lower():
                    if size > max_bytes * (1 - same_tier_min_reduction):
                        continue
            matches.append(r)
        if matches:
            matches.sort(key=lambda r: (-(r.get("customFormatScore") or 0),
                                        -(r.get("seeders") or 0)))
            return matches[0], tier
    return None, None


def _pick_season_release(sonarr: Sonarr, series_id: int, season_number: int,
                         tiers: list[str], max_bytes: int,
                         same_tier: str | None = None,
                         same_tier_min_reduction: float = 0.0
                         ) -> tuple[dict | None, str | None]:
    try:
        releases = sonarr.releases(series_id, season_number=season_number)
    except Exception:  # noqa: BLE001
        return None, None
    packs = [r for r in releases if r.get("fullSeason")]
    return _filter_releases(packs, tiers, max_bytes, same_tier, same_tier_min_reduction)


def _pick_episode_release(sonarr: Sonarr, series_id: int, episode_id: int,
                          tiers: list[str], max_bytes: int,
                          same_tier: str | None = None,
                          same_tier_min_reduction: float = 0.0
                          ) -> tuple[dict | None, str | None]:
    try:
        releases = sonarr.releases(series_id, episode_id=episode_id)
    except Exception:  # noqa: BLE001
        return None, None
    return _filter_releases(releases, tiers, max_bytes, same_tier, same_tier_min_reduction)


def _episode_id_map(sonarr: Sonarr, series_id: int) -> dict[int, int]:
    """episodeFileId -> episodeId, needed because the per-episode release
    search takes episodeId, which the episodefile resource doesn't expose."""
    out: dict[int, int] = {}
    for ep in sonarr.episodes(series_id):
        fid = ep.get("episodeFileId")
        if fid:
            out[int(fid)] = int(ep["id"])
    return out


def _blocklist_episode_on_repeated_failure(series_id: int, season_number: int,
                                           episode_id: int, series_title: str,
                                           limit: int) -> bool:
    if limit <= 0:
        return False
    streak = db.tv_episode_failure_streak(series_id, season_number, episode_id)
    if streak < limit:
        return False
    db.tv_blocklist_add(series_id, season_number, episode_id, series_title,
                        reason=f"auto: {streak} consecutive grab failures")
    db.record_action(kind="tv", series_id=series_id, season_number=season_number,
                     episode_id=episode_id, title=series_title, action="blocklist",
                     dry_run=False, status="blocklisted",
                     detail=f"auto-blocklisted after {streak} consecutive "
                            f"failed grabs -- release likely pulled from indexer")
    return True


def _demote_season(sonarr: Sonarr, profile: dict, cand: tv_score.SeasonCandidate,
                   settings: dict, dry: bool) -> dict:
    depth = int(settings.get("tv_tier_fallback_depth", 0))
    allow_same_tier = bool(settings.get("tv_allow_same_tier_downgrades", False))
    same_tier_min_reduction = float(
        settings.get("tv_same_tier_min_reduction_pct", 50.0)) / 100.0
    max_failures = int(settings.get("tv_max_grab_failures", 3))
    series_id = cand.series_id
    title = cand.title

    ladder = _tier_ladder(profile, cand.tier, depth, allow_same_tier)

    if cand.clean:
        rel, tier = _pick_season_release(
            sonarr, series_id, cand.season_number, ladder, cand.size,
            same_tier=cand.tier if allow_same_tier else None,
            same_tier_min_reduction=same_tier_min_reduction)
        if rel:
            actual_reclaim = cand.size - int(rel.get("size") or 0)
            aid = db.record_action(
                kind="tv", series_id=series_id, season_number=cand.season_number,
                title=title, action="demote", old_tier=cand.tier, old_size=cand.size,
                new_profile_id=profile["id"], release_guid=rel.get("guid"),
                dry_run=dry, status="pending",
                detail=f"season pack -> {tier} score={cand.score:.3f} "
                       f"reclaim={actual_reclaim / GB:.1f}GB")
            if dry:
                db.update_action(aid, "dry-run")
                return {"series": cand.series.get("title"), "season": cand.season_number,
                        "mode": "pack", "episodes_total": len(cand.source_files),
                        "episodes_downgraded": len(cand.source_files),
                        "episodes_failed": 0, "reclaim_gb": actual_reclaim / GB,
                        "dry_run": True}
            try:
                sonarr.grab(rel["guid"], rel.get("indexerId"))
                db.update_action(aid, "grabbed")
                return {"series": cand.series.get("title"), "season": cand.season_number,
                        "mode": "pack", "episodes_total": len(cand.source_files),
                        "episodes_downgraded": len(cand.source_files),
                        "episodes_failed": 0, "reclaim_gb": actual_reclaim / GB,
                        "dry_run": False}
            except Exception as e:  # noqa: BLE001
                db.update_action(aid, "failed", str(e)[:300])
                # Fall through to per-episode fallback below rather than
                # giving up on the whole season.

    # Per-episode fallback: either no clean pack candidate, or the pack
    # search/grab came up empty/failed.
    ep_map = _episode_id_map(sonarr, series_id)
    downgraded = 0
    failed = 0
    reclaim_bytes = 0
    for f in cand.source_files:
        file_id = f.get("id")
        episode_id = ep_map.get(int(file_id)) if file_id is not None else None
        if episode_id is None:
            failed += 1
            continue
        if (series_id, cand.season_number, episode_id) in db.tv_blocklist_episode_ids():
            continue
        size = int(f.get("size") or 0)
        rel, tier = _pick_episode_release(
            sonarr, series_id, episode_id, ladder, size,
            same_tier=cand.tier if allow_same_tier else None,
            same_tier_min_reduction=same_tier_min_reduction)
        if not rel:
            db.record_action(
                kind="tv", series_id=series_id, season_number=cand.season_number,
                episode_id=episode_id, title=title, action="skip",
                old_tier=cand.tier, old_size=size, dry_run=dry, status="skipped",
                detail=f"no release available in {'/'.join(ladder)}")
            continue

        actual_reclaim = size - int(rel.get("size") or 0)
        aid = db.record_action(
            kind="tv", series_id=series_id, season_number=cand.season_number,
            episode_id=episode_id, title=title, action="demote",
            old_tier=cand.tier, old_size=size, new_profile_id=profile["id"],
            release_guid=rel.get("guid"), dry_run=dry, status="pending",
            detail=f"episode -> {tier} reclaim={actual_reclaim / GB:.1f}GB")

        if dry:
            db.update_action(aid, "dry-run")
            downgraded += 1
            reclaim_bytes += actual_reclaim
            continue

        try:
            sonarr.grab(rel["guid"], rel.get("indexerId"))
            db.update_action(aid, "grabbed")
            downgraded += 1
            reclaim_bytes += actual_reclaim
        except Exception as e:  # noqa: BLE001
            db.update_action(aid, "failed", str(e)[:300])
            failed += 1
            _blocklist_episode_on_repeated_failure(
                series_id, cand.season_number, episode_id,
                cand.series.get("title"), max_failures)

        time.sleep(float(settings.get("tv_search_throttle_seconds", 20)))

    return {"series": cand.series.get("title"), "season": cand.season_number,
            "mode": "fallback", "episodes_total": len(cand.source_files),
            "episodes_downgraded": downgraded, "episodes_failed": failed,
            "reclaim_gb": reclaim_bytes / GB, "dry_run": dry}


def _demote_tv_candidates(sonarr: Sonarr, profile: dict,
                          candidates: list[tv_score.SeasonCandidate],
                          settings: dict, dry: bool) -> list[dict]:
    switched_series: set[int] = set()
    acted: list[dict] = []

    for cand in candidates:
        series_id = cand.series_id
        if series_id not in switched_series:
            switched_series.add(series_id)
            if cand.series.get("qualityProfileId") != profile["id"] and not dry:
                try:
                    s = cand.series
                    s["qualityProfileId"] = profile["id"]
                    tag_id = sonarr.ensure_tag(str(settings["tv_archived_tag"]))
                    s["tags"] = sorted(set(s.get("tags") or []) | {tag_id})
                    sonarr.update_series(s)
                except Exception as e:  # noqa: BLE001
                    db.record_action(
                        kind="tv", series_id=series_id, season_number=cand.season_number,
                        title=cand.series.get("title"), action="skip",
                        dry_run=dry, status="failed",
                        detail=f"series profile switch failed: {str(e)[:200]}")
                    continue

        result = _demote_season(sonarr, profile, cand, settings, dry)
        downgraded = result["episodes_downgraded"]
        total = result["episodes_total"]
        title = cand.title
        if result["mode"] == "fallback" and downgraded < total:
            title += f" ({downgraded}/{total} episodes)"
        acted.append({"title": title, "reclaim_gb": result["reclaim_gb"],
                     "score": cand.score, "dry_run": result["dry_run"]})
        time.sleep(float(settings.get("tv_search_throttle_seconds", 20)))

    return acted


def _archive_profile_or_abort(sonarr: Sonarr, settings: dict) -> dict:
    profile = find_archive_profile(sonarr, settings["tv_archive_profile_name"])
    if not profile:
        msg = (f"quality profile '{settings['tv_archive_profile_name']}' not found "
               "in Sonarr -- create it first")
        db.log_run("tv_loop", False, msg)
        raise TvLoopAbort(msg)

    check = validate_archive_profile(profile, settings["tv_archive_tier"])
    if not check["ok"]:
        db.log_run("tv_loop", False, check["reason"])
        raise TvLoopAbort(check["reason"])
    return profile


def run_selected(series_season_pairs: list[tuple[int, int]]) -> dict:
    """Downgrade specific seasons hand-picked on the TV Candidates page.
    No deficit/batch-size gating -- a manual selection is the user's own
    budget, same as loop.run_selected."""
    settings = db.all_settings()
    dry = bool(settings.get("dry_run", True))
    sonarr = sonarr_from_env()

    profile = _archive_profile_or_abort(sonarr, settings)

    built = build_season_candidates(settings, sonarr)
    wanted = {(int(sid), int(sn)) for sid, sn in series_season_pairs}
    chosen = [c for c in built["ranked"]
             if (c.series_id, c.season_number) in wanted]

    acted = _demote_tv_candidates(sonarr, profile, chosen, settings, dry)

    missing = len(wanted) - len(chosen)
    summary = (f"{'DRY-RUN: ' if dry else ''}{len(acted)} downgrade(s) of "
               f"{len(chosen)} selected season(s)"
               + (f", {missing} no longer eligible" if missing else ""))
    db.log_run("tv_loop", True, summary)
    return {"acted": True, "chosen": acted, "summary": summary,
            "requested": len(wanted), "matched": len(chosen)}
