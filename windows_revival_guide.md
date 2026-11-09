# iPhone Revival: Windows Quickstart Guide

A straightforward, step-by-step guide to purging iOS "System Data" cache bloat on a Windows laptop without losing passwords, Duo 2FA tokens, or cellular service.

---

## The 3 Golden Rules

1. **Always Check "Encrypt local backup":**
   Unencrypted backups **omit** Keychain, Wi-Fi passwords, and 2FA accounts. The encryption checkbox is mandatory.
2. **Never Delete eSIMs:**
   When erasing, always choose **"Keep eSIMs and Erase Data"**.
3. **Keep Phone Plugged In & Unlocked:**
   Ensure the USB cable stays firmly connected throughout the entire process.

---

## Step-by-Step Workflow

### Step 1: Install Official iTunes from Apple Website
Windows requires official Apple iTunes (which bundles the Apple Mobile Device USB driver service) to communicate with the iPhone and perform backups.
1. Download official iTunes directly from Apple based on your system bits:
   - **64-bit Windows (Most PCs):** 👉 **[https://www.apple.com/itunes/download/win64](https://www.apple.com/itunes/download/win64)**
   - **32-bit Windows:** 👉 **[https://www.apple.com/itunes/download/win32](https://www.apple.com/itunes/download/win32)**
2. Run the downloaded installer (`iTunes64Setup.exe` or `iTunesSetup.exe`) and complete setup.
3. Launch **iTunes**.

### Step 2: Unlock and Trust Your Phone
1. With your phone plugged into the laptop, unlock the screen.
2. If prompted with **"Trust This Computer?"**, tap **Trust** and enter your passcode.
3. In iTunes, you will see a small **iPhone icon** appear in the top-left navigation bar.

### Step 3: Make an Encrypted Backup in iTunes
1. Click the **iPhone icon** in iTunes.
2. In the **Summary** tab, under the **Backups** section:
   - Select **This Computer**.
   - Check the box: **☑ Encrypt local backup**.
   - 💡 **First-Time Password Setting Dialog:**
     If your phone has never had backup encryption turned on before, iTunes will display a modal dialog: **"Set Password"**:
     - Type a memorable password into the **Password** and **Verify** fields.
     - 📝 **CRITICAL:** Write down or save this password! Apple has no password reset mechanism for encrypted backups; you will need it to restore.
     - Click **Set Password**.
     - *(If your phone was already encrypted on an earlier computer, iTunes will simply use that existing password without prompting to create a new one).*
   - Click **Back Up Now** (or let the automation script drive it).
3. Let the backup finish completely (watch the progress bar at the top).

### Step 4: Run the Revival Verifier (`revive.bat`)
1. In the `phone_revival` folder, double-click **`revive.bat`** (or open PowerShell and run `python iphone_manager.py revive`).
2. The automated pipeline will:
   - Verify backup completion, freshness, and encryption.
   - Create a safe pre-wipe snapshot copy of your backup.
   - Audit and seal your 2FA accounts and keychain tokens.
   - Confirm everything is 100% safe to proceed.

### Step 5: Factory Reset the iPhone (Purge Cache Bloat)
1. On your iPhone, open **Settings -> General -> Transfer or Reset iPhone**.
2. Tap **Erase All Content and Settings**, then tap **Continue**.
3. 💡 **Skip iCloud Backup Prompt:**
   If asked *"Uploading Data to iCloud"* or *"Finish Uploading Then Erase"*, tap **"Skip Backup"** or **"Erase Now"**. Your complete encrypted backup is already verified and saved locally on your computer.
4. ⚠️ **CRITICAL WARNING FOR eSIM USERS:**
   If asked about cellular plans / eSIMs, select:
   👉 **"Keep eSIMs and Erase Data"** *(Never tap "Erase All")*.
5. Enter your passcode and Apple ID password. The phone will erase and reboot to the "Hello" screen.

### Step 6: Restore Your Data via iTunes
1. Keep the phone plugged in. When it reaches the "Hello" screen, verify post-wipe readiness:
   ```cmd
   python iphone_manager.py detect-wipe
   ```
2. In iTunes, you will see the **"Welcome to Your New iPhone"** screen:
   - Option: **☑ Restore from this backup: [your device]** is pre-selected by default.
   - Click the blue **Continue** button (or let the script actuate it automatically):
     ```cmd
     python iphone_manager.py itunes --restore
     ```
3. Enter your **Backup Encryption Password** from Step 3 when prompted by iTunes.
4. Let the restore complete. The phone will reboot with your restored data and System Data cache bloat wiped!

### Step 7: Post-Restore Verification
1. Take a **fresh** backup after the restore, then run the token verification tool. The
   pre-wipe manifest was sealed in your private state directory
   (`%USERPROFILE%\.ios-system-data-cleaner\<UDID>-tokens-prewipe.json`); `revive` prints its
   exact path:
   ```cmd
   python iphone_manager.py tokens verify --prewipe "%USERPROFILE%\.ios-system-data-cleaner\<UDID>-tokens-prewipe.json"
   ```
2. Open **Duo Mobile** and confirm accounts are present.
3. Open **Settings -> General -> iPhone Storage** and check **System Data** to see the recovered space!
