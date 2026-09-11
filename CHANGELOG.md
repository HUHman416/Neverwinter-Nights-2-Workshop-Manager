# Changelog

## 0.3.1 - 2026-09-11

- Fix updater certificate discovery on Fedora/Bazzite with explicit system trust paths.
- Bundle Certifi's public root certificates for systems without a readable CA bundle.
- Preserve certificate and hostname verification and explicit SSL trust overrides.
- Verify a real HTTPS update check from the packaged AppImage in CI.

## 0.3.0 - 2026-09-11

- Restore the host library environment when launching external applications.
- Native Steam `-applaunch`, Flatpak Steam fallback, browser fallback for Workshop pages,
  and visible failure messages with a launch log.
- Separate identical duplicates, priority overlaps, and custom winners; show pending/applied state.
- Apply Priority Winners opens the file-change preview with sync confirmation.
- Inspect every provider's file preview, source folder, and overlap win/loss counts.
- Cache overlap comparisons; show file counts and elapsed time while scanning/comparing.

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
