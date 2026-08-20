"""Full-library inventory and audit."""
from __future__ import annotations

import os
import statistics as st
import time

from . import db, rules, space
from .arr import Radarr, Sonarr, radarr_from_env, sonarr_from_env

GB = 1024 ** 3

# The movie list is ~20 MB for this library. Re-pulling it on every dashboard
# render is both slow and rude to Radarr, and none of the numbers move
# minute-to-minute, so it's cached with a short TTL.
_MOVIE_CACHE: dict = {"ts": 0.0, "movies": None}
MOVIE_CACHE_TTL = 300.0


def cached_movies(radarr: Radarr, ttl: float = MOVIE_CACHE_TTL,
                  force: bool = False) -> list[dict]:
    now = time.time()
    if (not force and _MOVIE_CACHE["movies"] is not None
            and now - _MOVIE_CACHE["ts"] < ttl):
        return _MOVIE_CACHE["movies"]
    # Probe with the short timeout first: the movie pull is ~20 MB on a 120s
    # timeout, so without this a dead host stalls a page render for two
    # minutes instead of reporting itself down in eight seconds.
    radarr.ping()
    movies = radarr.movies()
    _MOVIE_CACHE["movies"] = movies
    _MOVIE_CACHE["ts"] = now
    return movies


def cache_age() -> float | None:
    if _MOVIE_CACHE["movies"] is None:
        return None
    return time.time() - _MOVIE_CACHE["ts"]


# Same latency shape that made movies_only=True necessary for the movie
# Candidates page (~13s render pulling ~12k episode files): building TV
# season candidates needs one episode_files() call per series, so both the
# series list and each series' episode files are cached with a short TTL.
_SERIES_CACHE: dict = {"ts": 0.0, "series": None}
SERIES_CACHE_TTL = 300.0
_EPISODE_FILE_CACHE: dict[int, tuple[float, list[dict]]] = {}


def cached_series(sonarr: Sonarr, ttl: float = SERIES_CACHE_TTL,
                  force: bool = False) -> list[dict]:
    now = time.time()
    if (not force and _SERIES_CACHE["series"] is not None
            and now - _SERIES_CACHE["ts"] < ttl):
        return _SERIES_CACHE["series"]
    sonarr.ping()
    series = sonarr.series()
    _SERIES_CACHE["series"] = series
    _SERIES_CACHE["ts"] = now
    return series


def cached_episode_files(sonarr: Sonarr, series_id: int,
                         ttl: float = SERIES_CACHE_TTL,
                         force: bool = False) -> list[dict]:
    now = time.time()
    cached = _EPISODE_FILE_CACHE.get(series_id)
    if not force and cached is not None and now - cached[0] < ttl:
        return cached[1]
    files = sonarr.episode_files(series_id)
    _EPISODE_FILE_CACHE[series_id] = (now, files)
    return files


def inventory(radarr: Radarr | None = None,
              sonarr: Sonarr | None = None,
              use_cache: bool = False,
              movies_only: bool = False) -> dict:
    """Pull tracked files. With movies_only, skips the (expensive) Sonarr walk.

    The candidate ranking only needs movies, and pulling all ~12k TV episode
    files on every Candidates-page load was the bulk of a ~13s render. The full
    audit still pulls both.
    """
    radarr = radarr or radarr_from_env()
    movies = cached_movies(radarr) if use_cache else radarr.movies()

    records: list[dict] = []
    for m in movies:
        mf = m.get("movieFile")
        if not mf:
            continue
        records.append(rules.file_record(
            kind="movie",
            title=m.get("title", "?"),
            path=mf.get("path"),
            size=int(mf.get("size") or 0),
            quality=mf.get("quality") or {},
            media_info=mf.get("mediaInfo") or {},
            ref_id=int(m["id"]),
            fallback_runtime_min=m.get("runtime"),
        ))

    if movies_only:
        return {"movies": movies, "records": records,
                "movie_records": records, "tv_records": []}

    tv_records: list[dict] = []
    try:
        sonarr = sonarr or sonarr_from_env()
        for s in sonarr.series():
            for f in sonarr.episode_files(int(s["id"])):
                tv_records.append(rules.file_record(
                    kind="tv",
                    title=f.get("relativePath") or s.get("title", "?"),
                    path=f.get("path"),
                    size=int(f.get("size") or 0),
                    quality=f.get("quality") or {},
                    media_info=f.get("mediaInfo") or {},
                    ref_id=int(f["id"]),
                ))
    except Exception as e:  # noqa: BLE001 - TV audit is best-effort
        db.log_run("inventory", False, f"Sonarr inventory failed: {e}")

    return {"movies": movies, "records": records + tv_records,
            "movie_records": records, "tv_records": tv_records}


def archive_tier_median_bytes(records: list[dict], archive_tier: str) -> int:
    """What a demoted title is expected to weigh, measured from this library.

    Uses the real median of the target tier rather than an assumed constant --
    the same self-tuning principle as the cohort thresholds.
    """
    sizes = [r["size"] for r in records
             if r["kind"] == "movie" and r["tier"] == archive_tier and r["size"] > 0]
    if len(sizes) < 5:
        return int(14.6 * GB)  # measured library average, as a floor-fallback
    return int(st.median(sizes))


def tv_archive_tier_median_bytes(records: list[dict], archive_tier: str) -> int:
    """Per-EPISODE median size of TV files already on the archive tier in
    this library. A season's expected size after downgrade is this value
    times its episode count -- same self-tuning-from-the-library principle
    as archive_tier_median_bytes, just at per-episode grain since a season's
    episode count varies.
    """
    sizes = [r["size"] for r in records
             if r["kind"] == "tv" and r["tier"] == archive_tier and r["size"] > 0]
    if len(sizes) < 5:
        return int(1.5 * GB)  # conservative floor-fallback until the library
                              # has enough archive-tier episodes to measure from
    return int(st.median(sizes))


def tier_breakdown(records: list[dict], kind: str) -> list[dict]:
    agg: dict[str, dict] = {}
    for r in records:
        if r["kind"] != kind:
            continue
        a = agg.setdefault(r["tier"], {"tier": r["tier"], "bytes": 0, "count": 0})
        a["bytes"] += r["size"]
        a["count"] += 1
    out = sorted(agg.values(), key=lambda a: -a["bytes"])
    for a in out:
        a["gb"] = a["bytes"] / GB
        a["avg_gb"] = (a["bytes"] / a["count"] / GB) if a["count"] else 0
    return out


def run_audit(tag: bool = True) -> dict:
    started = time.time()
    settings = db.all_settings()
    radarr = radarr_from_env()

    inv = inventory(radarr=radarr)
    records = inv["records"]

    findings = rules.classify(records, settings)
    findings += rules.find_duplicates(records)

    # Orphans: folders on disk Radarr doesn't track. Invisible to any
    # API-derived total, which is why the API figure is a floor not the truth.
    orphans: list[dict] = []
    try:
        known = set()
        for m in inv["movies"]:
            p = m.get("path")
            if p:
                known.add(os.path.basename(p.rstrip("/")))
        for o in space.find_orphans(known):
            orphans.append(o)
            findings.append({
                "kind": "movie", "klass": "orphan", "ref_id": None,
                "title": o["name"], "path": o["path"], "size": o["size"],
                "tier": None, "codec": None, "bitrate": None,
                "cohort_median": None, "ratio": None,
                "detail": "on disk, not tracked by Radarr",
            })
    except Exception as e:  # noqa: BLE001
        db.log_run("orphans", False, f"orphan walk failed: {e}")

    new_count = 0
    for f in findings:
        if db.upsert_finding(f):
            new_count += 1

    if tag and not settings.get("dry_run", True):
        _apply_tags(radarr, findings)

    counts: dict[str, int] = {}
    for f in findings:
        counts[f["klass"]] = counts.get(f["klass"], 0) + 1

    summary = (f"{len(findings)} findings ({new_count} new): "
               + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    db.log_run("audit", True, summary)

    return {
        "elapsed": time.time() - started,
        "counts": counts,
        "new": new_count,
        "total_findings": len(findings),
        "orphans": orphans[:50],
        "orphan_bytes": sum(o["size"] for o in orphans),
        "tracked_bytes": sum(r["size"] for r in records),
        "summary": summary,
    }


def _apply_tags(radarr: Radarr, findings: list[dict]) -> None:
    """Tag flagged movies so they're filterable in Radarr's own UI."""
    by_class: dict[str, set[int]] = {}
    for f in findings:
        if f["kind"] == "movie" and f.get("ref_id") and f["klass"] in (
                "underweight", "bloated", "upscale", "broken"):
            by_class.setdefault(f["klass"], set()).add(int(f["ref_id"]))

    for klass, ids in by_class.items():
        try:
            tag_id = radarr.ensure_tag(f"audit-{klass}")
        except Exception:  # noqa: BLE001
            continue
        for mid in ids:
            try:
                m = radarr.movie(mid)
                tags = set(m.get("tags") or [])
                if tag_id not in tags:
                    m["tags"] = sorted(tags | {tag_id})
                    radarr.update_movie(m)
            except Exception:  # noqa: BLE001
                continue
