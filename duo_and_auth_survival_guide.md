# Duo Mobile & Critical Credentials Survival Guide

## 1. The Core Principle: Why Local Encrypted Backup is Mandatory
iOS local backups come in two formats:
- **Unencrypted (Default if unchecked):** Excludes Keychain, saved passwords, Wi-Fi passwords, Health data, and app security tokens. **Do NOT use this.**
- **Encrypted (With a password):** Archives the device Keychain, allowing app credentials, Wi-Fi profiles, saved Safari/Chrome logins, and authentication tokens to restore.

> **RULE #1:** Every backup performed must have **"Encrypt local backup"** checked in macOS Finder, and the password must be remembered/saved in your Mac Keychain.

---

## 2. Duo Mobile Architecture & Fail-Safe Plan

Duo Mobile handles two distinct types of accounts:

### A. Duo-Protected Organization Accounts (Duo Push / Enterprise / University)
- **How it works:** Duo Push generates a cryptographic key pair tied to the device.
- **Duo Restore for iOS:**
  1. Open **Duo Mobile** on the device.
  2. Tap the **Settings** gear icon (top right).
  3. Tap **Duo Restore**.
  4. Ensure **Duo Restore** is toggled **ON** and connected to iCloud Keychain.
  5. If prompted, set a recovery password and note it down.
- **Fail-Safe Pre-Flight Check (Crucial!):**
  - Before resetting any device with Duo:
    1. Log into your institution or company's Duo self-service management portal on your Mac browser (e.g., `identity.myorg.edu` or corporate Duo device management portal).
    2. Confirm that you either have:
       - A secondary enrolled device (e.g., another iPhone or Mac Touch ID), OR
       - A set of emergency bypass codes generated and printed/saved, OR
       - Duo SMS/phone call fallback enabled.
    3. Because devices are staged sequentially (e.g. device A first, then device B), you can use the active secondary device to approve logins while the other is being restored.

### B. Third-Party Accounts (6-digit TOTP codes for GitHub, AWS, Google, etc.)
- When **Duo Restore** is enabled, third-party accounts are securely synced to iCloud Keychain.
- When an **Encrypted Backup** is restored, Keychain data is re-injected.

---

## 3. Other Authentication & Communication Apps

| App / Service | Survival Mechanism | Pre-Wipe Action Required |
| :--- | :--- | :--- |
| **Apple Keychain** | Encrypted Finder Backup + iCloud Keychain | Verify iCloud Keychain is toggled ON in iOS Settings -> Apple ID -> iCloud -> Passwords & Keychain. |
| **Google Authenticator** | Google Account Cloud Sync | Verify profile icon in top-right shows green cloud sync checkmark. |
| **Microsoft Authenticator** | iCloud Backup | In app: Settings -> Backup -> Toggle ON "Cloud Backup". |
| **eSIM** | Cellular carrier profile | **CRITICAL:** When selecting "Erase All Content and Settings", ALWAYS choose **"Keep eSIMs and Erase Data"**. Never choose "Erase All". |
| **WhatsApp** | iCloud Chat Backup | WhatsApp -> Settings -> Chats -> Chat Backup -> tap **Back Up Now**. |
| **Signal** | In-app device transfer | Signal intentionally excludes chat databases from iOS backups for privacy. Signal chats will start fresh unless transferred live to another device. |
| **Apple Pay / Wallet** | Secure Enclave protection | Cards are automatically removed upon wipe. Keep CVVs handy to re-verify after restore. |
| **Banking / Financial** | App security policy | Most banking apps require logging in with password + SMS OTP after a restore because Face ID / Touch ID tokens are reset. |

---

## 4. Emergency Rollback & Verification Checklist

Before wiping ANY phone:
- [ ] Finder shows "Encrypt local backup" is ENABLED.
- [ ] Backup verified: `python3 backup_verifier.py --connected` shows **✅ RESULT: PASS**.
- [ ] Backup encryption password is known. The verifier's **proof-of-password gate now checks it**: a PASS means the password was cryptographically proven against the backup keybag (via `iOSbackup` or `mvt-ios`), or you supplied it with `--password`. If no proving tool is installed, the gate fails closed rather than passing blindly.
- [ ] Duo Restore is enabled inside Duo Mobile settings.
- [ ] Secondary Duo login method or bypass code is verified on the Mac.
- [ ] Apple ID password is known (required to disable Find My and activate after restore).
- [ ] Device passcode is known.
