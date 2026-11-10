# Security Policy & Philosophy

Resetting and restoring an iOS device to purge phantom cache requires certainty that user
data, 2FA credentials, and system posture survive without risk of lockout. This document
describes exactly what the tooling does — and does not — do.

---

## 🛡️ Zero-Lockout Security Model

### 1. Credential handling
- **No plaintext passwords on disk.** The password prover records each attempt as a
  *salted hash* plus its result (and the keybag fingerprint it was tested against), only so
  duplicates can be skipped and a prior success re-used. Plaintext candidates live solely in
  the running process's memory, masked, for the current session's table.
- **Never on the command line.** Proof-of-password is established with `iOSbackup` (password
  passed over **stdin**) or `mvt-ios decrypt-backup` (password passed via the **environment**),
  never as an argv argument that `ps` could read.
- **Keychain without broad access.** When a proven password is saved to the macOS Keychain
  for Finder auto-fill, the item is created **without `-A`**, so it is not readable by every
  application; Finder prompts once to use it.
- **Local only.** Verification, storage math, and posture audits run entirely on the host with
  no outbound network calls.
- **Runtime state is private.** Snapshots, the market ledger, token manifests, and attempt
  history live in `~/.ios-system-data-cleaner/` (mode `0700`, overridable with
  `IOS_CLEANER_STATE_DIR`) — never inside the repository working tree.

### 2. Pre-erase verification gate
`backup_verifier.py` must report `✅ RESULT: PASS` before any erase. It checks:
1. `Info.plist`, `Manifest.plist`, `Status.plist`, and a non-empty `Manifest.db` are present
   and readable.
2. `Status.plist` reports `SnapshotState == "finished"` (the backup actually completed).
3. The backup is newer than `--max-age-hours` (default 24).
4. `Manifest.plist` reports `IsEncrypted == True` (Keychain, Wi-Fi, and 2FA data included);
   unencrypted backups are rejected.
5. **Size and file-count floor** — the backup is at least ~100 MB (configurable via
   `IOS_CLEANER_MIN_BACKUP_BYTES`) and holds a real payload, with a warning if the Info.plist
   UDID does not match the folder name.
6. **Proof of password** — for an encrypted backup, the password is cryptographically proven
   (candidate, `BACKUP_PASSWORD`/`MVT_IOS_BACKUP_PASSWORD`, a prior proof tied to the current
   keybag fingerprint, or a verified Keychain entry). If no proving tool is installed, the gate
   **fails closed** with an install hint. A prior proof is invalidated automatically when the
   backup is re-encrypted with a different password (the keybag fingerprint changes).

### 3. Immutable pre-wipe clone
Before any erase/restore, the verified backup is cloned to `${UDID}-prewipe` by one shared
helper that:
- clones to a temporary sibling, verifies the plists and a non-empty `Manifest.db`, then
  **atomically swaps** it into place; the clone it replaces is **kept** as
  `${UDID}-prewipe-<timestamp>`, never overwritten or deleted;
- is **idempotent and never nested** — if a valid clone already exists it is kept as-is;
- uses APFS copy-on-write (`cp -c`) on macOS, `robocopy` on Windows (exit code < 8 = success),
  or a plain recursive copy elsewhere after a free-space check.

Backups are never overwritten and never pruned automatically: each backup run renames the
previous one aside first (and puts it back if the run fails). Clean-up is proposed by
`python3 restorer.py backups` and only happens when the person says yes: it moves one backup to
the Trash and refuses the live backup, the `-prewipe` clone, hand-protected `.protected-*`
copies, another phone's backups, and the only backup holding the most apps.

### 4. Single-device & post-wipe guardrails
- Finder/iTunes restore automation aborts unless exactly one iOS device is connected and its
  UDID matches the expected target.
- The Finder restore (`restorer.py ui-restore`) only acts in the post-wipe Welcome view, proves
  the selected backup matches the validated one by name and capture time before pressing
  Continue, and refuses a set-up phone. There is no "Restore Backup…" path that could
  overwrite a live device.
- Windows restore refuses unless the device is confirmed wiped; no fixed-coordinate clicks are
  used — unresolved UI elements produce a structured "agent handoff" instead of a blind click.

---

## 🛑 The "Coattail" Boundary: Human Points of No Return

Automation runs autonomously up to each irreversible physical threshold, then stops for human
authorization:
- **Device wipe** — the human enters the passcode on the iPhone to start "Erase All Content".
- **eSIM preservation** — the human confirms "Keep eSIMs and Erase Data".
- **Backup password entry** — Finder/iTunes prompts the human (Keychain may pre-fill).

---

## 🚨 Reporting a Vulnerability

Please report security issues privately via **GitHub's private vulnerability reporting**:

1. Do **not** open a public issue.
2. Go to the repository's **Security** tab → **Report a vulnerability** (GitHub Security
   Advisories), and include the attack surface, edge case, and reproduction steps.

We will acknowledge the report and coordinate a fix.
