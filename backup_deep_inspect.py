#!/usr/bin/env python3
"""
Deep inspection of an iOS backup, beyond MVT's indicator (IOC) matching.

MVT answers "does anything match a known-bad indicator?". These checks look for the
patterns an analyst reviews by hand, modelled on the manual parts of Amnesty
International's Pegasus forensic methodology and on common stalkerware tradecraft:

  posture      iOS version, backup encryption, Lockdown Mode
  profiles     configuration profiles with MDM / VPN / root-certificate / proxy / DNS payloads
  apps         apps not distributed through the App Store (TestFlight, enterprise, other stores)
  permissions  who can use the microphone / camera; grants to apps that aren't installed
  processes    networked processes with no bundle ID; lookalike names (ckkeyrollfd vs ckkeyrolld)
  attachments  message attachments whose bytes don't match their type (FORCEDENTRY hid PDF/PSD
               payloads in files named .gif)
  shortcuts    Shortcuts that both collect data (location, clipboard, photos...) and send it out
  links        punycode, raw-IP and shortened links in messages and web data
  clock        timestamps after the backup was taken (phone clock set ahead)
  jailbreak    jailbreak tool artifacts in the file manifest

It reads MVT's parsed results (the <UDID>_report folder `mvt-ios check-backup` writes)
and, for the attachments and shortcuts checks, raw files from the backup. The backup is
only opened read-only (SQLite in immutable mode), so the real MobileSync backup is safe.

Privacy: output holds security signals only (app/bundle IDs, process names, domains,
counts). Message text, phone numbers and contact details are never printed or saved.

Severity: FLAG = look at this now; REVIEW = usually benign, worth a glance; OK; INFO.
No heuristic here proves or rules out compromise. A FLAG is a reason to get expert
review (e.g. Amnesty Security Lab, Access Now Digital Security Helpline), not a verdict.

Usage:
  backup_deep_inspect.py --report mvt_reports/<UDID>_report [--backup <backup dir>]
    --backup defaults to the matching MobileSync backup when it is unencrypted. For an
    encrypted backup pass a decrypted copy, or the two file-based checks are skipped.
  run_mvt_inspection.py runs this automatically after every scan.

Exit code: 0 = nothing flagged, 1 = at least one FLAG, 2 = setup error.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import plistlib
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from backup_verifier import BACKUP_ROOT

ICON = {"FLAG": "🚩", "REVIEW": "🔎", "OK": "✅", "INFO": "ℹ️ ", "SKIP": "⏭️ "}


class Ctx:
    """Shared inputs: MVT's parsed results plus read-only access to backup files."""

    def __init__(self, report: Path, backup: Path | None, encrypted: bool | None):
        self.report, self.backup, self.encrypted = report, backup, encrypted
        self._cache: dict[str, object] = {}
        info = self.mvt("backup_info") or {}
        self.info = info
        self.backup_date = _ts(info.get("Last Backup Date"))
        self.installed = set(info.get("Installed Applications") or [])

    def mvt(self, module: str):
        """Records MVT extracted for one module, or None if MVT produced no file for it."""
        if module not in self._cache:
            p = self.report / f"{module}.json"
            self._cache[module] = json.loads(p.read_text()) if p.exists() else None
        return self._cache[module]

    def backup_file(self, domain: str, relpath: str) -> Path | None:
        """A file inside the backup; backups store each file at <sha1(domain-path)>[:2]/<sha1>."""
        if not self.backup:
            return None
        fid = hashlib.sha1(f"{domain}-{relpath}".encode()).hexdigest()
        p = self.backup / fid[:2] / fid
        return p if p.is_file() else None


class Section:
    def __init__(self, name: str):
        self.name, self.items, self.details = name, [], {}

    def add(self, level: str, msg: str):
        self.items.append((level, msg))


def _ts(value) -> str:
    """MVT timestamps as 'YYYY-MM-DD HH:MM:SS' so they compare correctly as strings."""
    return str(value or "")[:19].replace("T", " ")


def ro_sqlite(path: Path) -> sqlite3.Connection:
    # immutable=1: no locks, journals or WAL files, so nothing is ever written next to the DB.
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def lev1(a: str, b: str) -> bool:
    """True if a and b differ by exactly one insertion, deletion, substitution or swap."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        d = [i for i in range(len(a)) if a[i] != b[i]]
        return len(d) == 1 or (len(d) == 2 and d[1] == d[0] + 1
                               and a[d[0]] == b[d[1]] and a[d[1]] == b[d[0]])
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


# --------------------------------------------------------------------------- checks

def check_posture(c: Ctx, s: Section):
    i = c.info
    if not i:
        s.add("SKIP", "MVT produced no backup_info.json")
        return
    s.add("INFO", f"{i.get('Product Name', '?')} on iOS {i.get('Product Version', '?')} "
                  f"(build {i.get('Build Version', '?')}); backup taken {c.backup_date[:16]} UTC")
    if c.encrypted is False:
        s.add("REVIEW", "Backup is unencrypted, so call history, website history and Health data "
                        "weren't available to check. An encrypted backup gives a fuller scan.")
    prefs = c.mvt("global_preferences")
    if prefs is None:
        s.add("SKIP", "Lockdown Mode: MVT found no global preferences")
    elif any(p.get("entry") == "LDMGlobalEnabled" and str(p.get("value")).lower() in ("true", "1")
             for p in prefs):
        s.add("OK", "Lockdown Mode is ON")
    else:
        s.add("INFO", "Lockdown Mode is off. If you could be a target (journalist, activist, "
                      "official), it shuts the message-attachment and web attack surface that "
                      "Pegasus-class exploits have used.")


RISKY_PAYLOADS = {
    "com.apple.mdm": ("FLAG", "MDM enrollment: a server can remotely manage this phone"),
    "com.apple.security.root": ("FLAG", "root certificate: can enable HTTPS interception if trusted"),
    "com.apple.proxy.http.global": ("FLAG", "global HTTP proxy: all web traffic can be routed elsewhere"),
    "com.apple.webcontent-filter": ("FLAG", "web content filter: sees browsing"),
    "com.apple.dnsSettings.managed": ("FLAG", "managed DNS: lookups go to a chosen server"),
    "com.apple.dnsProxy.managed": ("FLAG", "DNS proxy"),
    "com.apple.vpn.managed": ("REVIEW", "VPN: traffic can be routed through it"),
    "com.apple.vpn.managed.applayer": ("REVIEW", "per-app VPN"),
    "com.apple.security.pkcs1": ("REVIEW", "certificate"),
    "com.apple.security.pem": ("REVIEW", "certificate"),
    "com.apple.security.pkcs12": ("REVIEW", "identity certificate"),
}


def check_profiles(c: Ctx, s: Section):
    profiles = c.mvt("configuration_profiles")
    if not profiles:
        s.add("OK", "no configuration profiles found")
        return
    for p in profiles:
        pl = p.get("plist") or {}
        name = pl.get("PayloadDisplayName") or pl.get("PayloadIdentifier") or p.get("relative_path")
        types = [x.get("PayloadType", "?") for x in pl.get("PayloadContent") or [] if isinstance(x, dict)]
        risky = [RISKY_PAYLOADS[t] for t in types if t in RISKY_PAYLOADS]
        level = "FLAG" if any(lv == "FLAG" for lv, _ in risky) else "REVIEW" if risky else "INFO"
        why = "; ".join(d for _, d in risky) or "no high-risk payloads"
        s.add(level, f"'{name}' from {pl.get('PayloadOrganization') or 'unknown org'}, installed "
                     f"{_ts(p.get('install_date'))[:10]}: {', '.join(types) or 'no payloads'} ({why})")


def check_apps(c: Ctx, s: Section):
    apps = c.mvt("applications")
    if apps is None:
        s.add("SKIP", "MVT produced no applications.json")
        return
    seen, odd = set(), 0
    for a in apps:
        bid = a.get("softwareVersionBundleId") or a.get("name") or "?"
        seen.add(bid)
        di = a.get("distributorInfo") or {}
        flags, reviews = [], []
        if a.get("isB2BCustomApp"):
            flags.append("custom B2B / enterprise app")
        if a.get("sideLoadedDeviceBasedVPP"):
            flags.append("side-loaded VPP app")
        if not a.get("itemId"):
            flags.append("no App Store item ID")
        if di.get("betaTesterType"):
            reviews.append("TestFlight beta")
        if di.get("distributorID") and di["distributorID"] != "com.apple.AppStore":
            reviews.append(f"distributed by {di.get('localizedDistributorName') or di['distributorID']}")
        if flags or reviews:
            odd += 1
            s.add("FLAG" if flags else "REVIEW", f"{bid}: {', '.join(flags + reviews)}")
    for bid in sorted(c.installed - seen):
        odd += 1
        s.add("FLAG", f"{bid}: installed but has no App Store metadata (sideloaded or developer build?)")
    if not odd:
        s.add("OK", f"all {len(apps)} apps are ordinary App Store installs")


SENSITIVE_TCC = {
    "kTCCServiceMicrophone": "microphone", "kTCCServiceCamera": "camera",
    "kTCCServiceAddressBook": "contacts", "kTCCServicePhotos": "photos",
    "kTCCServiceCalendar": "calendar", "kTCCServiceSpeechRecognition": "speech recognition",
    "kTCCServiceMotion": "motion", "kTCCServiceMediaLibrary": "media library",
}


def check_permissions(c: Ctx, s: Section):
    tcc = c.mvt("tcc")
    if tcc is None:
        s.add("SKIP", "MVT produced no tcc.json")
        return
    by_app: dict[str, set[str]] = defaultdict(set)
    for t in tcc:
        if t.get("auth_value") in ("allowed", "limited") and t.get("service") in SENSITIVE_TCC:
            by_app[t["client"]].add(SENSITIVE_TCC[t["service"]])

    def installed(bid: str) -> bool:  # Apple apps aren't listed; extensions share the app's prefix
        return bid.startswith("com.apple.") or any(bid == i or bid.startswith(i + ".") for i in c.installed)

    for bid in sorted(b for b in by_app if not installed(b)):
        hot = by_app[bid] & {"microphone", "camera"}
        s.add("FLAG" if hot else "REVIEW",
              f"{bid} has {', '.join(sorted(by_app[bid]))} access but isn't an installed app")
    for what in ("microphone", "camera"):
        apps = sorted(b for b, svcs in by_app.items() if what in svcs)
        s.add("REVIEW" if apps else "OK",
              f"{len(apps)} apps may use the {what}" + (f": {', '.join(apps)}" if apps else ""))
    s.details = {b: sorted(v) for b, v in sorted(by_app.items())}


UUID_RE = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-", re.I)


def check_processes(c: Ctx, s: Section):
    du = c.mvt("datausage")
    if du is None:
        s.add("SKIP", "MVT produced no datausage.json")
        return
    bundles: dict[str, set[str]] = defaultdict(set)
    rows: Counter = Counter()
    for r in du:  # proc_name is "<executable>/<bundle id>"
        exe, _, tail = (r.get("proc_name") or "").partition("/")
        if not exe or UUID_RE.match(exe):
            continue
        rows[exe] += 1
        bundles[exe].update(b for b in (tail, r.get("bundle_id")) if b)
    bare = sorted(e for e, b in bundles.items() if not b)
    s.add("REVIEW" if bare else "OK",
          f"{len(bare)} of {len(bundles)} networked processes have no bundle ID. Spyware daemons "
          "aren't apps, so they'd show up here; on a normal phone this is all Apple components "
          "(names are cut at 16 characters)" + (f": {', '.join(bare)}" if bare else ""))
    for n in bare:
        if not 6 <= len(n) < 16:  # short names collide naturally; 16 = truncated, can't compare
            continue
        for o in bundles:
            if o != n and lev1(n.lower(), o.lower()) and not o.lower().startswith(n.lower()):
                s.add("FLAG", f"'{n}' (no bundle ID, {rows[n]} records) is one letter off '{o}' "
                              f"({rows[o]} records). Lookalike names like ckkeyrollfd/ckkeyrolld "
                              "are a Pegasus hallmark.")
    s.details = {"no_bundle_id": {n: rows[n] for n in bare}}


SIGNATURES = [(b"GIF8", "gif"), (b"\x89PNG", "png"), (b"\xff\xd8\xff", "jpeg"), (b"%PDF", "pdf"),
              (b"8BPS", "psd"), (b"PK\x03\x04", "zip"), (b"bplist", "bplist"),
              (b"II*\x00", "tiff"), (b"MM\x00*", "tiff")]
HEIF_BRANDS = {b"heic", b"heix", b"heis", b"hevc", b"mif1", b"msf1", b"avif"}
EXPECTED = {".gif": "gif", ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".heic": "heic",
            ".avif": "heic", ".mov": "video", ".mp4": "video", ".m4v": "video", ".pdf": "pdf",
            ".webp": "webp", ".tif": "tiff", ".tiff": "tiff"}
IMAGE_KINDS = {"gif", "png", "jpeg", "heic", "webp", "tiff"}


def sniff(head: bytes) -> str:
    for sig, kind in SIGNATURES:
        if head.startswith(sig):
            return kind
    if head[4:8] == b"ftyp":
        return "heic" if head[8:12] in HEIF_BRANDS else "video"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return "unknown"


def check_attachments(c: Ctx, s: Section):
    atts = c.mvt("sms_attachments")
    if atts is None:
        s.add("SKIP", "MVT produced no sms_attachments.json")
        return
    if not c.backup:
        s.add("SKIP", "file content check needs the unencrypted backup or a decrypted copy")
        return
    checked = missing = skipped = 0
    for a in atts:
        fn = a.get("filename") or ""
        rel = re.sub(r"^(~/|/var/mobile/|/private/var/mobile/)", "", fn)
        ext = Path(fn).suffix.lower()
        if ext not in EXPECTED:
            skipped += 1  # e.g. .pluginpayloadattachment (iMessage app data), no extension
            continue
        p = c.backup_file("MediaDomain", rel)
        if not p:
            missing += 1  # not downloaded, or kept only in iCloud
            continue
        with open(p, "rb") as f:
            actual = sniff(f.read(16))
        checked += 1
        expected = EXPECTED[ext]
        if actual == expected:
            continue
        label = f"{a.get('direction') or '?'} {(a.get('isodate') or '')[:10]} '{Path(fn).name}'"
        if ext == ".gif" or (actual in ("pdf", "psd", "unknown") and expected in IMAGE_KINDS):
            s.add("FLAG", f"{label} is named {ext} but its bytes are {actual}. FORCEDENTRY's exploit "
                          "files were PDF/PSD data named .gif. Keep this backup for expert review.")
        else:
            s.add("REVIEW", f"{label} is named {ext} but its bytes are {actual} (often a mislabelled image)")
    if not any(lv == "FLAG" for lv, _ in s.items):
        s.add("OK", f"{checked} attachment(s) checked: contents match their file type")
    s.add("INFO", f"{len(atts)} attachments in the database: {checked} checked, {missing} not in the "
                  f"backup (iCloud / not downloaded), {skipped} without a checkable image/video/PDF type")


SHORTCUT_COLLECT = {
    "getcurrentlocation": "location", "getclipboard": "clipboard", "recordaudio": "audio recording",
    "takephoto": "camera", "takevideo": "camera", "takescreenshot": "screenshot",
    "getlastphoto": "photos", "getlatestphotos": "photos", "filter.photos": "photos",
    "selectphoto": "photos", "filter.contacts": "contacts", "selectcontacts": "contacts",
    "filter.health.quantity": "Health data", "filter.calendarevents": "calendar",
    "getdevicedetails": "device details",
}
SHORTCUT_SEND = {"downloadurl": "web request (Get Contents of URL)", "sendmessage": "message",
                 "sendemail": "email", "openurl": "opening a URL"}


def check_shortcuts(c: Ctx, s: Section):
    db = c.backup_file("HomeDomain", "Library/Shortcuts/Shortcuts.sqlite")
    if not db:
        s.add("SKIP" if not c.backup else "OK",
              "needs the unencrypted backup or a decrypted copy" if not c.backup else "no Shortcuts database")
        return
    conn = ro_sqlite(db)
    try:
        rows = conn.execute("SELECT ZSHORTCUT.ZNAME, ZSHORTCUTACTIONS.ZDATA FROM ZSHORTCUT "
                            "LEFT JOIN ZSHORTCUTACTIONS ON ZSHORTCUTACTIONS.ZSHORTCUT = ZSHORTCUT.Z_PK").fetchall()
    except sqlite3.Error as e:
        s.add("SKIP", f"unreadable Shortcuts database ({e})")
        return
    finally:
        conn.close()
    risky = 0
    for name, data in rows:
        try:
            actions = [a.get("WFWorkflowActionIdentifier", "") for a in plistlib.loads(data or b"")]
        except Exception:  # noqa: BLE001 - one odd blob shouldn't stop the check
            continue
        tails = [a.removeprefix("is.workflow.actions.") for a in actions]
        got = sorted({SHORTCUT_COLLECT[t] for t in tails if t in SHORTCUT_COLLECT})
        out = sorted({SHORTCUT_SEND[t] for t in tails if t in SHORTCUT_SEND})
        if got and out:
            risky += 1
            s.add("REVIEW", f"'{name}' gathers {', '.join(got)} and sends via {', '.join(out)}. Fine if "
                            "you made it; stalkerware setups use Shortcuts automations this way.")
    if not risky:
        s.add("OK", f"{len(rows)} shortcut(s): none both collect personal data and send it out")


SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "is.gd", "ow.ly", "cutt.ly", "rebrand.ly",
              "shorturl.at", "rb.gy", "tiny.cc", "t.ly", "buff.ly", "v.gd", "s.id"}


def _host_issue(host: str, shorteners: bool = True) -> tuple[str, str] | None:
    """Shorteners only matter in messages (lures); in web data they're ordinary link trackers."""
    host = host.lower().strip(".")
    if host.startswith("xn--") or ".xn--" in host:
        return "FLAG", "punycode domain (can imitate a real one)"
    try:
        ipaddress.ip_address(host.strip("[]"))
        return "FLAG", "raw IP address instead of a domain"
    except ValueError:
        pass
    if shorteners and host in SHORTENERS:
        return "REVIEW", "URL shortener (hides the real destination)"
    return None


def check_links(c: Ctx, s: Section):
    found: Counter = Counter()
    n_links = 0
    for m in c.mvt("sms") or []:
        for link in m.get("links") or []:
            n_links += 1
            host = urlsplit(link if "://" in link else "http://" + link).hostname or ""
            issue = _host_issue(host)
            if issue:
                found[(issue, f"message link to {host} ({m.get('direction', '?')})")] += 1
    domains = {w.get("registrable_domain") or w.get("domain") or ""
               for w in c.mvt("webkit_resource_load_statistics") or []}
    for d in domains:
        issue = _host_issue(d, shorteners=False)
        if issue:
            found[(issue, f"web data domain {d}")] += 1
    for ((level, why), what), n in sorted(found.items()):
        s.add(level, f"{what}: {why}" + (f" x{n}" if n > 1 else ""))
    if not any(level == "FLAG" for (level, _), _ in found):
        s.add("OK", f"{n_links} message link(s) and {len(domains)} web domains: "
                    "no punycode or raw-IP hosts")


def check_clock(c: Ctx, s: Section):
    if not c.backup_date:
        s.add("SKIP", "backup date unknown")
        return
    by_day: dict[str, list[str]] = defaultdict(list)
    for r in c.mvt("datausage") or []:
        t = _ts(r.get("isodate"))
        if t > c.backup_date:
            by_day[t[:10]].append(t)
    for m in c.mvt("manifest") or []:
        t = _ts(m.get("modified"))
        if t > c.backup_date:
            by_day[t[:10]].append(t)
    if not by_day:
        s.add("OK", "no records dated after the backup was taken")
        return
    scattered = 0
    for day, ts in sorted(by_day.items()):
        if len(ts) < 50:
            scattered += len(ts)
            continue
        s.add("REVIEW", f"{len(ts)} records stamped {min(ts)[:16]} to {max(ts)[11:16]} UTC, after the "
                        f"backup was taken ({c.backup_date[:16]}). The phone's clock was set ahead "
                        "(manual date change or bad network time). Not a spyware sign by itself, but "
                        "timelines around then are unreliable.")
    if scattered:
        s.add("INFO", f"{scattered} other file(s) carry scattered future dates (apps often set cache "
                      "files to expire that way)")
    s.details = {day: len(ts) for day, ts in sorted(by_day.items())}


JAILBREAK_MARKERS = ("cydia", "sileo", "com.saurik", "org.coolstar", "xyz.willy.zebra", "mobilesubstrate",
                     "ellekit", "libhooker", "checkra1n", "palera1n", "unc0ver", "com.opa334",
                     "trollstore", "com.tigisoftware.filza", "/var/jb", "procursus")


def check_jailbreak(c: Ctx, s: Section):
    manifest = c.mvt("manifest")
    if manifest is None:
        s.add("SKIP", "MVT produced no manifest.json")
        return
    hits = []
    for m in manifest:
        path = f"{m.get('domain', '')}/{m.get('relative_path', '')}"
        if any(t in path.lower() for t in JAILBREAK_MARKERS):
            hits.append((_ts(m.get("modified")), path))
    if not hits:
        s.add("OK", f"no jailbreak tool traces in {len(manifest)} files (weak check: most jailbreak "
                    "files aren't included in backups)")
        return
    newest = max(t for t, _ in hits)
    try:
        age_days = (datetime.fromisoformat(c.backup_date) - datetime.fromisoformat(newest)).days
    except ValueError:
        age_days = None
    if age_days is not None and age_days > 365:
        s.add("REVIEW", f"{len(hits)} file(s) from jailbreak tools, newest modified {newest[:10]} "
                        f"({age_days // 365}+ years before this backup). Most likely leftovers from a "
                        "past jailbreak, carried over by restoring older backups; an active jailbreak "
                        "would normally leave newer traces. Erasing and restoring keeps them.")
    else:
        s.add("FLAG", f"{len(hits)} recent file(s) from jailbreak tools (newest {newest[:10]}). A jailbroken "
                      "phone can run unsigned code, including spyware.")
    for t, path in sorted(hits, reverse=True)[:5]:
        s.add("INFO", f"  {t[:10]}  {path}")
    s.details = {path: t for t, path in hits}


CHECKS = [("posture", check_posture), ("profiles", check_profiles), ("apps", check_apps),
          ("permissions", check_permissions), ("processes", check_processes),
          ("attachments", check_attachments), ("shortcuts", check_shortcuts),
          ("links", check_links), ("clock", check_clock), ("jailbreak", check_jailbreak)]


def inspect(report: Path, backup: Path | None, encrypted: bool | None) -> tuple[int, int]:
    """Run every check, print the findings, save deep_inspect.json. Returns (#FLAG, #REVIEW)."""
    c = Ctx(report, backup, encrypted)
    results, counts = {}, Counter()
    for name, fn in CHECKS:
        s = Section(name)
        try:
            fn(c, s)
        except Exception as e:  # noqa: BLE001 - one broken check shouldn't hide the others
            s.add("SKIP", f"check failed: {type(e).__name__}: {e}")
        print(f"\n  [{name}]")
        for level, msg in s.items:
            print(f"    {ICON[level]} {msg}")
            counts[level] += 1
        results[name] = {"findings": [{"level": lv, "message": m} for lv, m in s.items], "details": s.details}
    (report / "deep_inspect.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\n  Deep inspection: {counts['FLAG']} FLAG, {counts['REVIEW']} REVIEW "
          f"(saved to {report / 'deep_inspect.json'})")
    return counts["FLAG"], counts["REVIEW"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Heuristic deep inspection of an iOS backup, using MVT's results.")
    ap.add_argument("--report", required=True, type=Path, help="MVT check-backup output folder (<UDID>_report)")
    ap.add_argument("--backup", type=Path, help="unencrypted backup or decrypted copy (default: matching "
                                                "MobileSync backup, if unencrypted)")
    args = ap.parse_args()
    report = args.report.expanduser().resolve()
    if not (report / "backup_info.json").exists():
        print(f"❌ {report} doesn't look like an MVT check-backup report (no backup_info.json)")
        return 2

    original = BACKUP_ROOT / report.name.removesuffix("_report")
    encrypted = None
    try:
        with open(original / "Manifest.plist", "rb") as f:
            encrypted = bool(plistlib.load(f).get("IsEncrypted"))
    except (OSError, plistlib.InvalidFileException):
        pass
    backup = args.backup.expanduser().resolve() if args.backup else (original if encrypted is False else None)
    if backup is None:
        print("ℹ️  No readable backup given: the attachments and shortcuts checks will be skipped.")

    print("=" * 70 + "\n   DEEP BACKUP INSPECTION (heuristics beyond MVT's indicators)\n" + "=" * 70)
    n_flag, _ = inspect(report, backup, encrypted)
    return 1 if n_flag else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
