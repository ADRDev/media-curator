"""Offline exercise of the tier ladder + force-import against fakes that
reproduce the two observed failures."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["MC_DB"] = os.path.join(tempfile.mkdtemp(), "verify.db")

from app import db, importer, loop, tv_loop, tv_score
db.init()
GB = 1024**3
P = lambda b: "\033[92mPASS\033[0m" if b else "\033[91mFAIL\033[0m"
fails = []
def check(name, cond):
    print(f"  {P(cond)}  {name}")
    if not cond: fails.append(name)

# Archive-HD profile as configured: 1080p ladder, Bluray-1080p ranked top.
ARCHIVE_HD = {"id": 9, "name": "Archive-HD", "items": [
    {"quality": {"id": 1, "name": "WEBRip-1080p"}, "allowed": True},
    {"quality": {"id": 2, "name": "WEBDL-1080p"}, "allowed": True},
    {"quality": {"id": 3, "name": "Bluray-1080p"}, "allowed": True},
]}

class FakeRadarrEmpty:
    def releases(self, movie_id): return []

print("=== 1. tier ladder")
check("depth 0 = top tier only (old behaviour)",
      loop._tier_ladder(ARCHIVE_HD, "Remux-2160p", 0) == ["Bluray-1080p"])
check("depth 3 walks the profile's allowed list downward",
      loop._tier_ladder(ARCHIVE_HD, "Remux-2160p", 3)
      == ["Bluray-1080p", "WEBDL-1080p", "WEBRip-1080p"])
check("never offers a tier at or above the current file (no accidental upgrade)",
      loop._tier_ladder(ARCHIVE_HD, "WEBDL-1080p", 3) == ["WEBRip-1080p"])
check("same-tier mode includes the current tier first",
    loop._tier_ladder(ARCHIVE_HD, "Bluray-1080p", 3, True)
    == ["Bluray-1080p", "WEBDL-1080p", "WEBRip-1080p"])
check("file already at the bottom -> empty ladder, nothing to grab",
      loop._tier_ladder(ARCHIVE_HD, "WEBRip-1080p", 3) == [])
check("empty ladder is handled, not an IndexError",
      loop._pick_release(FakeRadarrEmpty(), 1, []) == (None, None))

# A profile with more tiers than depth+1 below the current one: same-tier is
# an extra slot on top of the fallback budget, not a bite out of it -- turning
# it on must not cost a lower-tier rung that was reachable without it.
WIDE = {"id": 10, "name": "Wide", "items": [
    {"quality": {"id": 1, "name": "WEBRip-1080p"}, "allowed": True},
    {"quality": {"id": 2, "name": "WEBDL-1080p"}, "allowed": True},
    {"quality": {"id": 3, "name": "Bluray-1080p"}, "allowed": True},
    {"quality": {"id": 4, "name": "WEBDL-2160p"}, "allowed": True},
    {"quality": {"id": 5, "name": "Bluray-2160p"}, "allowed": True},
]}
check("same-tier doesn't shrink the strict-lower-tier fallback budget",
      loop._tier_ladder(WIDE, "Bluray-2160p", 1, True)
      == ["Bluray-2160p", "WEBDL-2160p", "Bluray-1080p"])

print("\n=== 2. release picking")
def rel(tier, size_gb, rejections=(), score=0, seeders=10):
    return {"guid": f"g-{tier}-{size_gb}", "indexerId": 1,
            "quality": {"quality": {"name": tier}}, "size": int(size_gb * GB),
            "rejections": list(rejections), "customFormatScore": score,
            "seeders": seeders}

class FakeRadarr:
    def __init__(self, releases): self._r = releases
    def releases(self, movie_id): return self._r

# The exact reported case: no Bluray-1080p exists, only WEBDL-1080p.
r, tier = loop._pick_release(
    FakeRadarr([rel("WEBDL-1080p", 8, ["Not an upgrade for existing movie file."])]),
    1, ["Bluray-1080p", "WEBDL-1080p", "WEBRip-1080p"], max_bytes=int(39.5 * GB))
check("falls back to WEBDL-1080p when no Bluray-1080p exists", tier == "WEBDL-1080p")

# Preference order still holds when the top tier IS available.
r, tier = loop._pick_release(
    FakeRadarr([rel("WEBDL-1080p", 8), rel("Bluray-1080p", 14)]),
    1, ["Bluray-1080p", "WEBDL-1080p"], max_bytes=int(39.5 * GB))
check("prefers the top tier when it exists", tier == "Bluray-1080p")

# Hard rejections are still respected at every rung.
r, tier = loop._pick_release(
    FakeRadarr([rel("WEBDL-1080p", 8, ["Unknown language"])]),
    1, ["Bluray-1080p", "WEBDL-1080p"], max_bytes=int(39.5 * GB))
check("a real rejection is still skipped at a fallback tier", r is None)

# Size guard: a 1080p remux bigger than the file we hold reclaims nothing.
r, tier = loop._pick_release(
    FakeRadarr([rel("Bluray-1080p", 45)]), 1, ["Bluray-1080p"],
    max_bytes=int(39.5 * GB))
check("refuses a replacement no smaller than the current file", r is None)

# Same-tier downgrade: a smaller Bluray-1080p replaces a bigger Bluray-1080p.
r, tier = loop._pick_release(
    FakeRadarr([rel("Bluray-1080p", 4)]), 1,
    loop._tier_ladder(ARCHIVE_HD, "Bluray-1080p", 3, True),
    max_bytes=int(20 * GB))
check("same-tier mode picks a smaller release in the same quality", tier == "Bluray-1080p")

# The size guard still applies to a same-tier match -- no smaller, no grab.
r, tier = loop._pick_release(
    FakeRadarr([rel("Bluray-1080p", 25)]), 1,
    loop._tier_ladder(ARCHIVE_HD, "Bluray-1080p", 3, True),
    max_bytes=int(20 * GB))
check("same-tier mode still refuses a same-or-larger replacement", r is None)

# Configurable minimum-reduction floor: two same-tier releases can be nearly
# the same size, so a merely-smaller release isn't a real downgrade.
r, tier = loop._pick_release(
    FakeRadarr([rel("Bluray-1080p", 12)]), 1,
    loop._tier_ladder(ARCHIVE_HD, "Bluray-1080p", 3, True),
    max_bytes=int(13 * GB), same_tier="Bluray-1080p", same_tier_min_reduction=0.5)
check("same-tier floor rejects a barely-smaller release", r is None)

r, tier = loop._pick_release(
    FakeRadarr([rel("Bluray-1080p", 6)]), 1,
    loop._tier_ladder(ARCHIVE_HD, "Bluray-1080p", 3, True),
    max_bytes=int(13 * GB), same_tier="Bluray-1080p", same_tier_min_reduction=0.5)
check("same-tier floor accepts a release that clears the reduction bar",
      tier == "Bluray-1080p")

# The floor is scoped to the same-tier rung only -- a genuine lower-tier
# fallback is unaffected even when it's only marginally smaller.
r, tier = loop._pick_release(
    FakeRadarr([rel("WEBDL-1080p", 12)]), 1,
    loop._tier_ladder(ARCHIVE_HD, "Bluray-1080p", 3, True),
    max_bytes=int(13 * GB), same_tier="Bluray-1080p", same_tier_min_reduction=0.5)
check("the reduction floor doesn't apply to a genuine lower-tier fallback",
      tier == "WEBDL-1080p")

print("\n=== 3. blocked-import detection")
STUCK = {
    "movieId": 42, "downloadId": "ABC123", "trackedDownloadState": "importPending",
    "trackedDownloadStatus": "warning",
    "statusMessages": [{"title": "Elemental (2023) (1080p BDRip...).mkv",
                        "messages": ["Not an upgrade for existing movie file. "
                                     "Existing quality: Remux-2160p. New Quality "
                                     "Bluray-1080p."]}],
    "movie": {"id": 42, "title": "Elemental", "qualityProfileId": 9},
}
UNRELATED = dict(STUCK, movieId=7, movie={"id": 7, "title": "Other", "qualityProfileId": 1})
HEALTHY = {"movieId": 8, "downloadId": "D", "trackedDownloadState": "downloading",
           "trackedDownloadStatus": "ok", "statusMessages": [],
           "movie": {"id": 8, "title": "Fine", "qualityProfileId": 9}}
STALLED = {"movieId": 9, "downloadId": "E", "trackedDownloadState": "importBlocked",
           "trackedDownloadStatus": "warning",
           "statusMessages": [{"title": "x", "messages": ["No files found are eligible for import"]}],
           "movie": {"id": 9, "title": "Stalled", "qualityProfileId": 9}}
# The same-tier downgrade version of the block: quality weight is a tie, so
# Radarr falls back to Custom Format score instead -- different wording, same
# stuck-import problem.
CF_STUCK = {
    "movieId": 43, "downloadId": "DEF456", "trackedDownloadState": "importPending",
    "trackedDownloadStatus": "warning",
    "statusMessages": [{"title": "Some Movie (2023) (1080p Bluray).mkv",
                        "messages": ["Not a Custom Format upgrade for existing "
                                     "movie file(s). New: [1080p Bluray, AAC, "
                                     "Banned Groups] (-999799) do not improve on "
                                     "Existing: [1080p Bluray, 1080p Quality Tier "
                                     "1, DTS] (300)"]}],
    "movie": {"id": 43, "title": "Some Movie", "qualityProfileId": 9},
}

class QRadarr:
    def __init__(self, recs): self._q = recs
    def queue(self, page_size=1000): return self._q

found = importer.stuck_items(
    QRadarr([STUCK, UNRELATED, HEALTHY, STALLED, CF_STUCK]), {9})
check("finds the downgrade-blocked item", any(r["movieId"] == 42 for r in found))
check("ignores a movie on an unmanaged profile", not any(r["movieId"] == 7 for r in found))
check("ignores a healthy download", not any(r["movieId"] == 8 for r in found))
check("ignores a differently-blocked item", not any(r["movieId"] == 9 for r in found))
check("finds the same-tier Custom Format-blocked item",
      any(r["movieId"] == 43 for r in found))
check("exactly two matches", len(found) == 2)

found_strict = importer.stuck_items(
    QRadarr([STUCK, UNRELATED, HEALTHY, STALLED, CF_STUCK]), {9}, upgrade_only=True)
check("the CF rejection is the sole reason, so the unattended sweep clears it too",
      any(r["movieId"] == 43 for r in found_strict))

print("\n=== 4. force import: delete-then-import ordering")
calls = []
class ImpRadarr:
    def queue(self, page_size=1000): return [STUCK]
    def movie(self, mid):
        return {"id": 42, "title": "Elemental", "qualityProfileId": 9,
                "movieFile": {"id": 555, "size": int(39.5*GB),
                              "quality": {"quality": {"name": "Remux-2160p"}}}}
    def manual_import_candidates(self, did):
        calls.append(("manualimport", did))
        return [{"path": "/downloads/sample.mkv", "size": 40*1024*1024,
                 "quality": {"quality": {"name": "Bluray-1080p"}}, "movieId": 42},
                {"path": "/downloads/Elemental.mkv", "size": int(9.2*GB),
                 "quality": {"quality": {"name": "Bluray-1080p"}},
                 "languages": [{"id": 1, "name": "English"}], "movieId": 42,
                 "releaseGroup": "JBENT", "indexerFlags": 0}]
    def delete_movie_file(self, fid): calls.append(("delete", fid))
    def command(self, name, **body): calls.append(("command", name, body))

res = importer.force_import(ImpRadarr(), STUCK, dry=False)
names = [c[0] for c in calls]
check("succeeded", res["ok"] is True)
check("deleted the outranking file before importing",
      names.index("delete") < names.index("command"))
check("deleted the right movie file id", ("delete", 555) in calls)
cmd = [c for c in calls if c[0] == "command"][0]
check("issued ManualImport", cmd[1] == "ManualImport")
f = cmd[2]["files"][0]
check("imported the feature, not the sample", f["path"] == "/downloads/Elemental.mkv")
check("carried the downloadId through", f["downloadId"] == "ABC123")
check("no stray 'size' key in the command payload", "size" not in f)
check("manifest row written", db.conn().execute(
    "SELECT COUNT(*) n FROM manifest WHERE action='import' AND status='imported'"
).fetchone()["n"] == 1)

print("\n=== 5. dry run touches nothing")
calls.clear()
res = importer.force_import(ImpRadarr(), STUCK, dry=True)
check("dry run performs no delete/command",
      not any(c[0] in ("delete", "command") for c in calls))

print("\n=== 6. TV season assembly")
import datetime as _dt
def _iso(days_ago):
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days_ago)).isoformat()

SERIES = {"id": 100, "title": "Test Show", "tags": [], "qualityProfileId": 1, "seasons": [
    {"seasonNumber": 0, "statistics": {"previousAiring": _iso(1000), "nextAiring": None}},
    {"seasonNumber": 1, "statistics": {"previousAiring": _iso(1000), "nextAiring": None}},
    {"seasonNumber": 2, "statistics": {"previousAiring": _iso(10), "nextAiring": None}},
    {"seasonNumber": 3, "statistics": {"previousAiring": _iso(1000), "nextAiring": _iso(-5)}},
]}
def epf(id_, season, size_gb, tier):
    return {"id": id_, "seasonNumber": season, "size": int(size_gb * GB),
            "quality": {"quality": {"name": tier}}}

TV_FILES = [
    epf(1, 0, 3, "Remux-1080p"),
    epf(2, 1, 3, "Remux-1080p"), epf(3, 1, 3, "Remux-1080p"),          # clean season, old enough
    epf(4, 2, 3, "Remux-1080p"),                                       # recent -- window should exclude
    epf(5, 3, 3, "Remux-1080p"),                                       # still airing -- excluded regardless of age
    epf(6, 1, 1, "WEBDL-1080p"),                                       # wrong tier -> not in season 1's source_files
]
seasons = tv_score.assemble_seasons(SERIES, TV_FILES, {"Remux-1080p"})
check("one SeasonCandidate per season number present", len(seasons) == 4)
s1 = next(s for s in seasons if s.season_number == 1)
check("season 1 groups only its own episode files as source", len(s1.source_files) == 2)
check("season 1 size is the sum of its source files only", s1.size == int(6 * GB))
check("season 1 not clean -- has a non-source-tier file (id=6) alongside it",
      s1.clean is False)
s0 = next(s for s in seasons if s.season_number == 0)
check("season 0 (specials) is clean -- its only file is source-tier", s0.clean is True)

print("\n=== 7. TV eligibility filters")
cands, rej = tv_score.eligible(
    seasons, {"tv_new_release_window_months": 6, "tv_include_specials": False},
    exclusion_tag_id=None, expected_per_episode_bytes=int(1 * GB))
kept = {c.season_number for c in cands}
check("season 0 excluded by default (specials)", 0 not in kept)
check("season 1 (old, clean-ish, has gain) is eligible", 1 in kept)
check("season 2 excluded -- aired inside the new-release window", 2 not in kept)
check("season 3 excluded -- still airing (nextAiring set)", 3 not in kept)
check("rejection counters attribute correctly",
      rej["specials_excluded"] == 1 and rej["inside_new_release_window"] == 1
      and rej["still_airing"] == 1)

cands2, rej2 = tv_score.eligible(
    seasons, {"tv_new_release_window_months": 6, "tv_include_specials": False},
    exclusion_tag_id=None, expected_per_episode_bytes=int(1 * GB),
    season_blocklist={(100, 1)})
check("season blocklist removes an otherwise-eligible season",
      1 not in {c.season_number for c in cands2} and rej2["season_blocklisted"] == 1)

cands3, rej3 = tv_score.eligible(
    seasons, {"tv_new_release_window_months": 6, "tv_include_specials": False},
    exclusion_tag_id=None, expected_per_episode_bytes=int(100 * GB))
check("no_gain fires when the expected target exceeds the season's own size",
      1 not in {c.season_number for c in cands3} and rej3["no_gain"] >= 1)

print("\n=== 8. TV ranking")
ranked = tv_score.rank(list(cands), {"tv_w_impact": 1.0, "tv_w_age": 0.5})
check("rank() returns candidates sorted by descending score",
      all(ranked[i].score >= ranked[i + 1].score for i in range(len(ranked) - 1)))

print("\n=== 9. TV release picking -- season pack vs per-episode fallback")
def tv_rel(tier, size_gb, full_season=False, rejections=(), score=0, seeders=10):
    return {"guid": f"g-{tier}-{size_gb}-{full_season}", "indexerId": 1,
            "quality": {"quality": {"name": tier}}, "size": int(size_gb * GB),
            "fullSeason": full_season, "rejections": list(rejections),
            "customFormatScore": score, "seeders": seeders}

class FakeSonarrSeason:
    """Mixed season-pack + per-episode results, as a real seasonNumber= search
    is expected to return (see tv_loop.py's noted open risk to verify live)."""
    def releases(self, series_id, season_number=None, episode_id=None):
        return [
            tv_rel("WEBDL-1080p", 4, full_season=True),
            tv_rel("WEBDL-1080p", 1.9, full_season=False),  # a per-episode result mixed in
        ]

rel, tier = tv_loop._pick_season_release(
    FakeSonarrSeason(), 100, 1, ["WEBDL-1080p"], max_bytes=int(6 * GB))
check("season-pack picker only considers fullSeason releases", rel is not None and rel["fullSeason"])

class FakeSonarrNoPack:
    def releases(self, series_id, season_number=None, episode_id=None):
        return [tv_rel("WEBDL-1080p", 1.9, full_season=False)]

rel, tier = tv_loop._pick_season_release(
    FakeSonarrNoPack(), 100, 1, ["WEBDL-1080p"], max_bytes=int(6 * GB))
check("no season pack available -> season picker returns nothing (caller falls back per-episode)",
      rel is None)

rel, tier = tv_loop._pick_episode_release(
    FakeSonarrNoPack(), 100, 55, ["WEBDL-1080p"], max_bytes=int(3 * GB))
check("per-episode picker finds a qualifying single-episode release", rel is not None and tier == "WEBDL-1080p")

print()
print("\033[91m%d FAILURE(S)\033[0m" % len(fails) if fails else "\033[92mall passed\033[0m")
sys.exit(1 if fails else 0)
