"""Offline exercise of the tier ladder + force-import against fakes that
reproduce the two observed failures."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["MC_DB"] = os.path.join(tempfile.mkdtemp(), "verify.db")

from app import db, importer, loop
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

class QRadarr:
    def __init__(self, recs): self._q = recs
    def queue(self, page_size=1000): return self._q

found = importer.stuck_items(QRadarr([STUCK, UNRELATED, HEALTHY, STALLED]), {9})
check("finds the downgrade-blocked item", any(r["movieId"] == 42 for r in found))
check("ignores a movie on an unmanaged profile", not any(r["movieId"] == 7 for r in found))
check("ignores a healthy download", not any(r["movieId"] == 8 for r in found))
check("ignores a differently-blocked item", not any(r["movieId"] == 9 for r in found))
check("exactly one match", len(found) == 1)

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

print()
print("\033[91m%d FAILURE(S)\033[0m" % len(fails) if fails else "\033[92mall passed\033[0m")
sys.exit(1 if fails else 0)
