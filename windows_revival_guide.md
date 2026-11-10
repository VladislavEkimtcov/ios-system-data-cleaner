# iPhone Revival: Windows Quickstart Guide

A step-by-step guide to purging iOS "System Data" cache bloat on a Windows PC without losing passwords, Duo 2FA
tokens, or cellular service. The assistant (an AI agent running this repo) does almost all of it; you do the few
things only a person can do, marked **You**.

> Status (2026-11-10): the backup half runs on Windows through the same engine as on a Mac. The restore half needs
> iTunes and is driven by the agent looking at the screen until the iTunes restore driver is finished (TODO WD).
> A Mac can always do the restore instead.

---

## The 3 Golden Rules

1. **Backups are always encrypted.** Unencrypted backups **omit** Keychain, Wi-Fi passwords and 2FA accounts. The
   engine turns encryption on itself (it asks you for a new password once, if the phone never had one).
2. **Never delete eSIMs.** When erasing, always choose **"Keep eSIMs and Erase Data"**.
3. **Don't erase until the assistant says so.** It checks the backup, proves you know its password, makes a safety
   copy, and checks this PC can restore (iTunes installed). Until all of that passes, erasing is blocked.

---

## Step-by-Step Workflow

### Step 1: Set up (one time)
- Apple's phone driver (*Apple Mobile Device Support*) is enough for **backups**.
- **iTunes is needed for the restore.** Without it, `revive` takes and checks the backup but tells you not to erase
  (exit code 2). Install it from Apple: 64-bit <https://www.apple.com/itunes/download/win64>, 32-bit
  <https://www.apple.com/itunes/download/win32>. **You** click "Yes" on the Windows permission prompt.
- The assistant installs the rest: `python dep_installer.py --tools --yes` (the helper tools, pinned and
  hash-checked) and `python dep_installer.py --proof-tool` (proves the backup password).

### Step 2: Unlock and trust the phone (**You**)
Plug the phone in, unlock it, tap **Trust** and enter the passcode if asked. If the phone isn't seen, the assistant
runs a connection check that says whether it's the cable, a locked phone, or a pending Trust prompt.

### Step 3: Back up, verify, safety copy (`revive.bat`)
Double-click **`revive.bat`** (or `python iphone_manager.py revive`). It:
1. Takes a **whole new backup** (the previous one is kept, never overwritten). **You** type the passcode on the
   phone when it asks; you hear a beep and see a banner at that moment, and again when it's accepted.
2. Verifies it (finished, encrypted, fresh) and **proves the password** (the password helper page opens if you
   aren't sure of it; the proven password is kept in Windows Credential Manager for this Windows user).
3. Makes a **safety copy** of the backup (on Windows a full copy: a few minutes, about as much space again).
4. Lists every backup of the phone with its app count. **A backup listing 0 apps (or far fewer than the phone has)
   is a trap**: don't erase; let the apps finish downloading and back up again.
5. Checks this PC can restore. If iTunes is missing it stops here with "do NOT erase yet".

### Step 4: Erase the phone (**You**)
1. On the phone: **Settings > General > Transfer or Reset iPhone > Erase All Content and Settings**.
2. If it offers to upload to iCloud first ("Finish Uploading Then Erase"), choose **"Skip Backup" / "Erase Now"**:
   the full backup is already on this PC, and the upload can take a very long time.
3. ⚠️ If asked about cellular plans, choose **"Keep eSIMs and Erase Data"** (never "Erase All").
4. The screen may be slow or stop responding for a minute or two while it erases. That's normal: wait, don't tap
   repeatedly, don't unplug.
5. When the phone shows **"Hello"**, tell the assistant.

### Step 5: Restore (the assistant drives iTunes)
1. The assistant runs the restore dry run (`python restorer.py ui-restore`). It checks: one phone, erased, the
   backup is this phone's, encrypted, finished, recent; it lists every backup and says which one it will restore.
2. It asks you to confirm the phone shows "Hello" and that it may start.
3. In iTunes it picks **"Restore from this backup"** and only the backup it named (never another phone's), then
   **Continue**. It never clicks **"Restore Backup..."** on the Summary page (that overwrites a working phone).
4. **You** type the backup password into iTunes' box when it appears. Two wrong tries open the password helper page.
5. Leave the phone alone until the assistant says it's finished (one restart; 10-30 minutes).

### Step 6: After the restore
1. The assistant takes the after-restore storage snapshot and shows the space and money report.
2. Optional: a fresh backup, then `python iphone_manager.py tokens verify --prewipe <path revive printed>`.
3. Open **Duo Mobile** and confirm the accounts are there.
