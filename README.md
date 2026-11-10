# ios-system-data-cleaner

<p align="center">
  <em>“CLAUDE! Reclaim space on my iPhone!”</em>
</p>

> **Safely purge 20–50+ GB of phantom iOS "System Data" cache bloat without data loss, defer costly hardware upgrades, and track household storage ROI.**

[![CI Tests](https://github.com/VladislavEkimtcov/ios-system-data-cleaner/actions/workflows/tests.yml/badge.svg)](https://github.com/VladislavEkimtcov/ios-system-data-cleaner/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Zero Dependencies](https://img.shields.io/badge/Dependencies-Standard%20Library-brightgreen)](https://docs.python.org/3/)
[![macOS](https://img.shields.io/badge/Platform-macOS-lightgrey.svg)](https://apple.com/macos)

---

## ⚡ The Problem: Phantom "System Data" Cache Bloat

Every iPhone accumulates gigabytes of cached media previews, diagnostic traces, indexing databases, and orphaned app temporary files categorized under **System Data** (formerly "Other"). Apple provides no button in iOS Settings to clear this cache. Over time, it chokes 64 GB and 128 GB iPhones, triggering low-storage warnings, sluggish performance, and premature hardware upgrades.

The only reliable, Apple-supported method to reclaim this space is an **Encrypted Backup → Factory Wipe → Restore** cycle.

`ios-system-data-cleaner` automates, verifies, and audits this entire workflow under the **Zero-Lockout Principle**—guaranteeing your eSIM, 2FA authenticator tokens (Duo, Google, Microsoft), and Keychains survive intact.

---

## 📊 Proven Results & Economic ROI

```
=================================================================
      PHONE REVIVAL: STORAGE & HOUSEHOLD ROI REPORT
=================================================================
  Device:         Family iPhone (iPhone10,4 / 64 GB)
  User Partition: 59.14 GB

  [BEFORE RESET]
  [██████████████████████░░░░░░] 46.88 GB used (79.3%) | 12.26 GB free

  [POST RESTORE]
  [██████░░░░░░░░░░░░░░░░░░░░░░] 12.12 GB used (20.5%) | 47.02 GB free

  ---------------------------------------------------------------
  🎯 DEVICE STORAGE RECOVERED:   +34.76 GB (-74.1% cache purged)
  📈 AVAILABLE CAPACITY GAINED:  +34.76 GB (+283.5% free space)
  💰 AVOIDED HARDWARE COSTS (LIVE MARKET VALUATION):
     • Like-for-Like Refurbished:  $75.00 [LIVE_VERIFIED] (Swappa / Back Market / eBay)
     • New Retail Upgrade Avoided: $799.00 [Apple Store Base MSRP]
  💵 APPLE NAND EQUIVALENT:      $27.11 (34.76 GB at $0.78/GB)
  ---------------------------------------------------------------
  🏡 HOUSEHOLD CUMULATIVE EFFORT (3 Devices Serviced):
     • Total Clean Storage Recovered:  +68.81 GB
     • Total Refurbished Replacements: $325.00 (Secondary market value)
     • Total New Purchases Deferred:   $2,397.00 (New retail hardware avoided)
     • Total Reclaimed NAND Value:     $53.67
=================================================================
```

---

## 🛡️ The No-Lockout Architecture

| Safety Gate | How It Protects You |
| :--- | :--- |
| **Mandatory Encrypted Backup** | Rejects unencrypted backups; verifies that Keychain, Wi-Fi passwords, and 2FA tokens are included. |
| **5-Point Backup Verification** | Checks `Manifest.plist`, `Status.plist`, `Info.plist`, file counts, and disk volume before any erase command. |
| **APFS Copy-on-Write Clone** | Instantly clones your backup to `${UDID}-prewipe` via `cp -c -R` (zero extra disk space) for an immutable fallback. |
| **Single-Device USB Lock** | Restorer automation aborts if more than one iOS device is plugged in, preventing cross-device misconfigurations. |
| **eSIM & 2FA Coattail Guard** | Halts at physical device boundaries so you keep eSIMs and authenticate Duo/2FA safely. |

---

## 🚀 Quickstart (3 Steps)

### Requirements
- A Mac running macOS Sonoma (14.0+) or Sequoia (15.0+). A Windows 10+ machine will also do.
- Python 3.10+ (standard macOS Python; no third-party libraries required).
- Lightning or USB-C cable.

### 0. The easy way
Just tell your AI to use this to help you with the system file bloat.

ALTERNATIVELY...

### 1. Clone & Configure
```bash
git clone https://github.com/VladislavEkimtcov/ios-system-data-cleaner.git
cd ios-system-data-cleaner
cp .env.example .env
```

### 2. Inspect Storage & Take "Before" Snapshot
Plug in your iPhone, tap **Trust** if prompted, and record your baseline storage:
```bash
# Capture pre-wipe storage baseline
python3 storage_monitor.py --snap before_reset

# Verify device connection and host storage status
python3 iphone_manager.py status
```

### 3. Back Up, Erase & Restore
1. In macOS **Finder**, select your iPhone and check **"Encrypt local backup"**. Click **Back Up Now**.
2. Run the pre-wipe verification gate:
   ```bash
   python3 backup_verifier.py --connected
   ```
   *(Must show `✅ RESULT: PASS` before proceeding).*
3. On your iPhone: Go to **Settings → General → Transfer or Reset iPhone → Erase All Content and Settings**.
   > 💡 If it offers to back up to iCloud first (**"Finish Uploading Then Erase"**), choose **"Skip Backup" / "Erase Now"**: your full backup is already on this Mac and the upload can take a very long time.
   > ⏳ The phone's screen can be slow or stop responding for a minute or two while it erases. That's normal: wait, don't tap repeatedly, don't unplug.
   > ⚠️ **IMPORTANT:** If prompted regarding eSIMs, select **"Keep eSIMs and Erase Data"**.
4. Once the phone shows the "Hello" screen, restore through **Finder**. (There is no command-line restore: it left the phone booting empty, without setup or apps.) Your agent drives Finder for you:
   ```bash
   python3 iphone_manager.py restorer wait
   python3 restorer.py ui-restore                                   # dry run: checks everything, changes nothing
   python3 restorer.py ui-restore --confirm --hello-confirmed       # the real restore
   ```
   It opens the phone in Finder (activating it if needed), checks the backup name, presses **Continue**, and presses **Restore** if your Keychain filled in the backup password; if not, it waits for you to type it. If any step goes wrong it stops and hands over to the agent, which fixes the screen and resumes with `--resume`.
5. When restore finishes, capture your "After" snapshot and view your savings:
   ```bash
   python3 storage_monitor.py --snap post_restore
   python3 storage_monitor.py --diff
   ```

---

## 🤖 Model Context Protocol (MCP) Server

Includes a built-in 15-tool MCP server (`ios_mcp_server.py`) for AI assistants like Claude Desktop and Antigravity:

```json
{
  "mcpServers": {
    "ios-cleaner": {
      "command": "python3",
      "args": ["/PATH/TO/ios-system-data-cleaner/ios_mcp_server.py"]
    }
  }
}
```

Capabilities exposed to your agent:
- Local MobileSync backup integrity verifiers & keybag token audits.
- Real-time host disk storage headroom budgeters.
- Anti-Pegasus spyware scanning & installed app catalog inspection.
- Xcode iOS Simulator replication and asset transfer.
- Token-Saver compactor (filters 98%+ unnecessary output tokens).
- USB device detection and Windows/macOS iTunes automation.

---

## ⚙️ CLI Command Summary

| Command | Action |
| :--- | :--- |
| `python3 storage_monitor.py --diff` | Render visual before/after storage bar graphs and household savings ledger. |
| `python3 backup_runner.py --authoritative` | Autonomous backup engine with sub-second PTY sensory feedback and verification. |
| `python3 backup_runner.py --watch` | Follow a running backup: one line when it needs your passcode, when it starts, a check-in every 5 min (GB copied, %, speed, ETA), and the result. |
| `python3 backup_verifier.py --connected` | Run 6-point cryptographic backup audit gate and proof-of-password check. |
| `python3 iphone_manager.py security` | Anti-Pegasus MVT spyware scan and deep heuristic security audit. |
| `python3 iphone_manager.py restorer wait` | Detect USB return of an iPhone resetting into Setup Assistant. |
| `python3 token_saver.py wrap -- <command>` | Run any shell command through the RTK token-reduction filter. |
| `pytest` | Run complete unit and integration test suite. |

---

## 🍎 Sister Project: Apple IPSW & Firmware Restoration MCP Server

> **Looking for agentic Apple IPSW firmware downloads, TSS signing verification, and automated DFU/Recovery re-flashing?**
> 
> 👉 [**VladislavEkimtcov/ipsw-mcp-server**](https://github.com/VladislavEkimtcov/ipsw-mcp-server)
>
> Provides 11 specialized MCP tools for querying Apple TSS servers, converting regulatory A-numbers (`A2848` ➔ `iPhone16,1`), stream-downloading official Apple CDN restore images with HTTP Range resumption, and generating `idevicerestore` CLI pipelines.

---

## 📖 Master Command Reference

### Daily-Driver Device Revival & ROI
| Action | Command |
| :--- | :--- |
| **Windows 1-Click Revival (Batch)** | `.\revive.bat` |
| **Windows Guide** | See `windows_revival_guide.md` |
| **Unified Status (Host + Device + Backup)** | `python3 iphone_manager.py status` |
| **Storage & Live Market ROI Report** | `python3 iphone_manager.py roi` |
| **List Registered Market Quotes (Live)** | `python3 iphone_manager.py market list` |
| **Audit Pricing Staleness & Stimulate Agent**| `python3 iphone_manager.py market check` |
| **One-Touch Revival Pipeline (Default)** | `python3 iphone_manager.py revive` |

### Safety Gates, Backups & Credentials
| Action | Command |
| :--- | :--- |
| **Autonomous Sensory Backup & Verification** | `python3 iphone_manager.py backup --authoritative` |
| **Verify Backup Gate (Must report PASS)** | `python3 iphone_manager.py verify --connected` |
| **Audit Tokens & Credential Posture** | `python3 iphone_manager.py tokens audit` |
| **Extract & Seal Pre-Wipe Token Manifest** | `python3 iphone_manager.py tokens extract --out tokens_prewipe.json` |
| **Verify 100% Post-Restore Token Survival** | `python3 iphone_manager.py tokens verify --prewipe tokens_prewipe.json` |
| **Interactive Password Hunter (Web/CLI)** | `python3 iphone_manager.py passwords --cli` |
| **Wait for Reset Phone & Stage Restore** | `python3 iphone_manager.py restorer wait` |

### Forensics, Apps & Testing
| Action | Command |
| :--- | :--- |
| **Run MVT Spyware / Forensic Audit** | `python3 run_mvt_inspection.py` |
| **Run Heuristic Deep Forensic Inspection** | `python3 backup_deep_inspect.py --report mvt_reports/<UDID>_report` |
| **List Installed Backup Apps** | `python3 iphone_manager.py apps list` |
| **Filter Apps by Genre (e.g. Finance)** | `python3 iphone_manager.py apps list --genre Finance` |
| **Open App Store Page in Simulator Safari** | `python3 iphone_manager.py apps open-store "Duo Mobile"` |
| **Boot Xcode Simulator & Open Window** | `python3 iphone_manager.py sim boot "iPhone 16e"` |
| **Send Backup & Media to Xcode Simulator** | `python3 iphone_manager.py sim send` |
| **Run Token-Saver Diagnostic Audit** | `python3 token_saver.py audit README.md` |
| **Run Command with Token-Saver RTK Filter** | `python3 token_saver.py wrap -- <command>` |
| **Run Complete Test Suite** | `python3 -m unittest tests/test_suite.py` |

---

## 🔒 Security

Please read [SECURITY.md](SECURITY.md) for details on our zero-credential-capture philosophy, backup immutability, and vulnerability reporting.

---

## 🤝 Contributing

We welcome contributions! Please review [CONTRIBUTING.md](CONTRIBUTING.md). Note that incoming pull requests are code-reviewed by an autonomous AI agent alongside human maintainers.

---

## 📄 License

Distributed under the [MIT License](LICENSE).
