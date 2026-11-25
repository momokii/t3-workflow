# t3-workflow

Otomasi alur kerja T3 Code: bridge Telegram dua arah, dan rencana kanban + watcher.

| Berkas | Isi |
|---|---|
| `RESEARCH.md` | riset dan desain: apa yang mungkin, batasannya, rencana bertahap |
| `bridge/t3_bridge.py` | daemon bridge Telegram ⇄ T3 (fase 1, sudah jalan) |
| `bridge/tests/` | 17 test: unit + integrasi dengan Telegram tiruan |
| `~/.config/t3-bridge/config.json` | konfigurasi (token diwarisi dari notifier, mode 600) |
| `~/.config/systemd/user/t3-bridge.service` | unit user service (terpasang, belum di-enable) |

## Perintah yang tersedia

| Perintah | Fungsi |
|---|---|
| `/help` | daftar perintah |
| `/id` | chat id (untuk allowlist) |
| `/projects` | daftar project T3 |
| `/threads <project>` | thread terbaru di project itu |
| `/ctx <thread> [n]` | judul, status, model, tautan, dan n pesan terakhir |
| `/send <thread> <teks>` | kirim pesan; thread sibuk menerimanya sebagai turn berikutnya |
| `/new <project> <teks>` | mulai thread baru, judul dari baris pertama |

## Menjalankan

```bash
# uji konfigurasi tanpa menyentuh Telegram
python3 bridge/t3_bridge.py --selftest

# jalankan satu perintah secara lokal, cetak balasan, tidak mengirim
python3 bridge/t3_bridge.py --once "/projects"

# daemon (foreground)
python3 bridge/t3_bridge.py

# sebagai service
systemctl --user daemon-reload
systemctl --user enable --now t3-bridge.service
journalctl --user -u t3-bridge -f
```

## Batasan yang disengaja

1. **Persetujuan (approval) tidak dijawab dari Telegram.** T3 menyimpannya sebagai `pending request`, dan CLI tidak punya subcommand untuk itu. Bridge hanya memberi tahu; jawaban tetap di UI T3.
2. **Thread sibuk tidak diinterupsi.** `t3 thread send` mengantre sebagai turn berikutnya — sesuai perilaku CLI.
3. **Allowlist wajib.** Chat di luar `telegram.allowChatIds` tidak dilayani dan dicatat di audit.
4. **Tanpa push.** Bridge tidak pernah menjalankan `git push` atau operasi destruktif.
5. **Satu mode update.** Long polling; webhook tidak dipakai, jadi tidak ada port publik.

## Konfigurasi

`~/.config/t3-bridge/config.json` (mode 600):

```json
{
  "telegram": { "botToken": "", "allowChatIds": ["<chat-id>"] },
  "t3": { "cli": "/home/kelanach/.local/bin/t3", "dbPath": "/home/kelanach/.t3/userdata/statev2.sqlite" },
  "bridge": { "dryRun": false, "allowWrites": true, "pollTimeout": 25 }
}
```

- `botToken` kosong → diwarisi dari `~/.config/t3-wa-notify/config.json`.
- `dryRun: true` → perintah tulis hanya dicetak, tidak dijalankan.
- `allowWrites: false` → `/send` dan `/new` ditolak.

## Tiket dan watcher

```bash
python3 tickets/cli.py init
python3 tickets/cli.py profiles
python3 tickets/cli.py add "Judul" --project t3-workflow --profile fast --test "python3 -m unittest discover -s tests"
python3 tickets/cli.py list
python3 tickets/cli.py move T-0001 ready
python3 watcher.py --once --dry-run     # cetak rencana, tidak mengubah apa pun
python3 watcher.py --once               # klaim atomik + mulai thread
```

Status: `backlog → ready → in-progress → testing → review → done`, plus `blocked`.
Transisi lain ditolak. Profil model ada di `~/.config/t3-workflow/config.json`
dan hanya dibaca oleh watcher — watcher tidak pernah memilih model sendiri.

## Status audit

Setiap perintah dicatat di `~/.local/state/t3-bridge/audit.jsonl`; log di `bridge.log`; offset update di `state.json`.
