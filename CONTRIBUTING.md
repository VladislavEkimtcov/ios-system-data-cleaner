# Contributing to `ios-system-data-cleaner`

First off, thank you for considering contributing! Whether you're fixing a bug, adding support for a new iOS or macOS version, optimizing token efficiency, or improving documentation, your help is appreciated.

---

## 🤖 The "My Agent Will Review Your Pull Requests" Workflow

Pull requests in this repository are triaged and code-reviewed by an **autonomous AI coding agent pair-programmer** alongside human maintainers.

### What the Agent Expects:
1. **Targeted, Atomic Commits:** Keep changes focused on a single issue or capability.
2. **Passing Test Suite:** All pull requests must pass the full test suite (`python3 -m unittest discover tests`).
3. **Zero Secrets & Zero PII:** Never commit actual device serial numbers, UDIDs, iCloud account identifiers, Wi-Fi credentials, or home directory absolute paths.
4. **Documentation & Docstring Integrity:** Preserve existing code documentation and add docstrings to any newly introduced public interfaces.
5. **Clear PR Description:** Explain **what** was changed, **why**, and provide sample output or test confirmation.

If your PR satisfies these criteria, our agent will review the diff, run the validation pipelines, provide constructive feedback if adjustments are needed, and stage it for merge.

---

## 🛠️ Local Development Setup

The core engine has **zero required external Python dependencies** (standard library
`plistlib`, `sqlite3`, `subprocess`, `http.server`, …). Python **3.10+** is required.

```bash
# 1. Clone the repository
git clone https://github.com/VladislavEkimtcov/ios-system-data-cleaner.git
cd ios-system-data-cleaner

# 2. (optional) copy the example environment file
cp .env.example .env

# 3. Run the test suite (hermetic: no device or network needed)
python3 -m unittest discover -s tests -p "test_*.py" -v
```

`pyproject.toml` configures only the test runner and `ruff`; it declares no runtime
dependencies. Runtime state is written to `~/.ios-system-data-cleaner/` (override with
`IOS_CLEANER_STATE_DIR`), so tests and local runs never touch the repo tree.

### Recommended Host Tools
For live physical device management:
- macOS Sonoma (14.0+)/Sequoia (15.0+), or Windows 10+ with official iTunes.
- `libimobiledevice` (`brew install libimobiledevice`).
- For proof-of-password / forensics: `pip3 install mvt` (`mvt-ios`) and/or `iOSbackup`.
- Optional: `pip install pillow` for simulator wallpaper decoding.

---

## 🧪 Running Tests & the PII Guard

Before submitting a pull request:

```bash
# Full test suite
python3 -m unittest discover -s tests -p "test_*.py" -v

# PII / secret guard — must print a clean result (CI runs this too)
python3 check_pii.py
```

`check_pii.py` scans tracked source and docs for device UDIDs, Wi-Fi MACs, absolute home
paths, and personal tokens. Keep synthetic values in `tests/` fixtures or `*.example.*`
files, or annotate an intentional line with `# pii-allow`.

---

## 🔒 Security & Privacy First

Please review our [SECURITY.md](SECURITY.md) before submitting code that touches device backups, encryption passwords, or system reset operations. All automation tools must adhere to the **Zero-Lockout Principle** and the **Coattail Philosophy** (stopping right before human points of no return).
