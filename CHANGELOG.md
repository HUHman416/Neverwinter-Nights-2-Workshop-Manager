# Changelog

## 0.2.0 - 2026-09-11

- Cache mod file lists against Steam per-item manifest metadata; rescan changed items.
- Skip content comparisons and copies for unchanged files using recorded size/timestamps.
- Full Verify for manual edits; mod-level scanning progress.
- Automatic release checks, verified AppImage downloads, and optional restart.
- Per-file winner buttons, prefer-one-mod-for-all-conflicts, and reset to priority.
- GUI smoke testing in CI.

## 0.1.0 - 2026-09-11

- Initial Bazzite-friendly AppImage application.
- Steam library and Proton-prefix detection.
- Reversible Workshop synchronization with original-file backups.
- Per-mod enable/disable controls and explicit conflict priority.
- Dry-run preview, conflict browser, backup status, and desktop integration.
- Legacy `nwn2-workshop-sync` state import.
