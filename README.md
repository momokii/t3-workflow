# t3-workflow

<img src="board/static/logo.svg" alt="" width="56" height="56">

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

## Layanan yang berjalan

| Unit | Isi | Bind / irama |
|---|---|---|
| `t3-bridge.service` | Telegram ⇄ T3 | long polling |
| `t3-watcher.service` | dev + test stage | tiap `watcher.intervalSeconds` (120 s) |
| `t3-board.service` | papan kanban | `100.124.184.116:8787` |

Menjeda tanpa mematikan service: set `watcher.enabled=false` di
`~/.config/t3-workflow/config.json`, lalu `systemctl --user restart t3-watcher`.
Menghentikan total: `systemctl --user disable --now t3-watcher`.

## Alur lengkap

```
backlog ──(anda)──► ready ──(watcher dev)──► in-progress ──(anda/model)──► testing
                                                      ▲                        │
                                                      │            (watcher test: test_cmd)
                                                      │                        │
                                                      └──── gagal ◄────────────┤
                                                                               │ lulus
                                                                               ▼
                                                     done ◄──(anda)── review ◄─┘
                                                       │
                                                laporan otomatis
                                       ~/.local/state/t3-workflow/reports/T-xxxx.md
```

- Tiket dengan `requires_approval` menunggu keputusan; bridge mengirim pesan dan
  menerima `/approve <id>` atau `/reject <id>`. Balasan kedua diabaikan.
- `in-progress → testing` bisa dilakukan model atau Anda (papan punya "Paksa pindah…").
- `testing` selalu dijalankan watcher: exit code 0 → `review` dengan bukti; selain itu
  kembali `in-progress` dengan kutipan kegagalan.

## Papan kanban

Board berjalan sebagai service user di **tailnet**, bukan di localhost:

```
http://100.124.184.116:8787
```

| Item | Nilai |
|---|---|
| Service | `t3-board.service` (enabled, active) |
| Bind | `100.124.184.116:8787` (hanya interface Tailscale) |
| Token tulis | `~/.config/t3-workflow/board.env` (mode 600), dibaca lewat `EnvironmentFile` |
| Halaman | menyuntikkan token ke `<meta name="board-token">` supaya UI bisa POST |

Menjalankan manual:

```bash
python3 board/server.py --host 100.124.184.116 --port 8787 --token "$(grep -o '[0-9a-f]\{32\}' ~/.config/t3-workflow/board.env)"
systemctl --user restart t3-board
```

POST tanpa token ditolak `401`. Kontrol akses sebenarnya adalah keanggotaan tailnet:
hanya perangkat di tailnet yang bisa memuat halaman ini.

Halaman menyediakan drag-and-drop **dan** kontrol "Pindahkan" per kartu, karena
WCAG 2.2 SC 2.5.7 mewajibkan alternatif pointer tunggal untuk setiap aksi drag.
Ada juga "Paksa pindah…" untuk override manusia; setiap override dicatat sebagai
event `forced` supaya tampak di laporan.

## Laporan per tiket

```bash
python3 tickets/cli.py report T-0001            # cetak markdown
python3 tickets/cli.py report T-0001 --out reports/T-0001.md
curl http://127.0.0.1:8787/api/report/T-0001     # dari papan
```

Laporan memuat status, profil model, thread, perintah test, catatan bukti, dan
seluruh riwayat tiket.

## Batas dan pemeliharaan

```bash
# batas konkurensi + anggaran waktu (menit) ada di config
python3 -c "import json,pathlib;print(json.loads((pathlib.Path.home()/'.config/t3-workflow/config.json').read_text()).get('limits'))"

# backup database tiket + laporan
python3 tickets/cli.py backup --out ~/.local/state/t3-workflow/backups
```

- `watcher.maxConcurrent` — jumlah tiket `in-progress` yang boleh berjalan bersamaan.
- `limits.maxInProgressMinutes` — tiket yang melewatinya dipindah ke `blocked` beserta
  menit terukurnya, supaya slot tidak tertahan thread yang macet. `0` mematikan.
- Notifikasi: bridge mengirim satu pesan saat tiket masuk `review`, `done`, atau `blocked`.

## Status audit

Setiap perintah dicatat di `~/.local/state/t3-bridge/audit.jsonl`; log di `bridge.log`; offset update di `state.json`.
