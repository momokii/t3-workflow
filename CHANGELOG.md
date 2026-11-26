# Changelog

Format mengikuti [Keep a Changelog](https://keepachangelog.com/); versi mengikuti [SemVer](https://semver.org/lang/id/).

## [0.1.0] — 2026-11-26

Rilis pertama dari alur kerja tiket → thread → test → rilis.

### Added

- **Bridge Telegram** (`bridge/t3_bridge.py`): `/projects`, `/threads`, `/ctx`, `/send`, `/new`,
  `/tickets`, `/ticket`, `/pending`, `/approve`, `/reject`, `/id`, `/help`. Long polling,
  allowlist chat, audit JSONL. `/send` menerima id thread atau nama project yang hanya punya satu thread.
- **Tiket SQLite** (`tickets/`): state machine `backlog → ready → in-progress → testing → review → done`
  plus `blocked`, klaim atomik, override `--force` yang tercatat, laporan markdown otomatis saat selesai.
- **Profil model** di `~/.config/t3-workflow/config.json`: `fast`, `deep`, `cheap`, `build`
  (provider `pi` dan `opencode`); watcher tidak pernah memilih model sendiri.
- **Watcher** (`watcher.py`): tahap dev (mulai thread), tahap test (jalankan `test_cmd`, simpan bukti,
  `review` hanya bila exit code 0), tahap release (branch `ticket/T-xxxx-…`, commit, push, tautan compare,
  badan PR), batas konkurensi dan anggaran waktu.
- **Papan kanban** (`board/`): drag-and-drop plus kontrol pointer tunggal (WCAG 2.2 SC 2.5.7),
  persetujuan, tiket baru, pendaftaran project, adopsi thread T3 yang sudah ada, responsif, mode gelap,
  logo + favicon, laporan per tiket.
- **Layanan**: `t3-bridge`, `t3-watcher`, `t3-board` (tailnet), `t3-backup.timer` harian dengan retensi 7,
  serta `t3-alert@.service` untuk notifikasi kegagalan unit.
- **Dokumentasi**: `README.md`, `RESEARCH.md`, `AGENTS.md`, `REVIEW.md`.
- **Test**: 79 test (bridge, tiket, watcher, rilis, papan) + satu suite integrasi opt-in.

### Known limitations

- Repo belum punya remote; release stage sudah diimplementasikan dan diuji dengan remote bare lokal.
- PR belum dibuat via API (butuh `gh` atau token GitHub); tersedia tautan compare dan berkas badan PR.
- Deploy dan rollback belum ada.
