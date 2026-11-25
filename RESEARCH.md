# T3 workflow automation: research and design

<!-- markdownlint-disable MD013 -->

Riset dan desain untuk membuat alur kerja harian (T3 Code + Pi + opencode-go + notifikasi Telegram) lebih seamless dan terautomasi, termasuk kemungkinan kanban dengan watcher agent.

Ditulis 2026-11-25. Semua klaim bertanda sumber: `[cli]` dari output `--help` di mesin ini, `[t3]` dari perilaku tool T3 yang tersedia di harness, `[tg]` dari dokumentasi Bot API Telegram, `[gh]` dari dokumentasi GitHub, `[local]` dari pengukuran langsung, `[inference]` kesimpulan saya.

---

## 1. Ringkasan eksekutif

Tiga hal yang membuat "utopia" ini sebenarnya dekat:

1. **T3 sudah punya semua primitif yang dibutuhkan.** `t3 thread list/start/send/show` bekerja dari CLI terhadap server yang berjalan `[cli]`, dan `thread send` secara eksplisit mengantre ke thread yang sedang sibuk: *"A busy thread gets it as its next turn; nothing is interrupted"* `[cli]`.
2. **Kanal dua arah sudah ada.** Bot Telegram sudah terpasang, `getUpdates` mendukung long polling dengan `offset`/`timeout` `[tg]`, dan batas pengirimannya longgar untuk pemakaian pribadi: 1 pesan/detik per chat, 20/menit per grup, ±30/detik untuk broadcast `[tg]`.
3. **Yang belum ada adalah lapisan penengah**: penerjemah perintah, penyimpan status tiket, penjadwal watcher, dan penjaga gerbang persetujuan. Itu pekerjaan yang bisa dibangun bertahap tanpa menyentuh kernel, tanpa mengubah T3.

Yang **tidak** mungkin hari ini, dan perlu dinyatakan sejak awal: menjadikan deploy/push otomatis penuh tanpa manusia. Bukan karena teknis, tapi karena Anda sendiri punya aturan `DEV NEVER PUSH`, dan karena gerbang persetujuan adalah satu-satunya titik kendali bermakna `[inference]`.

---

## 2. Kondisi awal yang terukur

| Komponen | Fakta | Bukti |
|---|---|---|
| T3 server | v0.1.9, listen `0.0.0.0:3773`, service `t3code.service` aktif | `[local]` |
| CLI thread | `list`, `start`, `send`, `show`; `send` punya `--wait` dan `--json` | `[cli]` |
| Kode keluar `--wait` | `1` bila run gagal, `2` bila menunggu jawaban | `[cli]` |
| CLI project | hanya `add`, `remove`, `rename` — **tidak ada `list`** | `[cli]` |
| Daftar project | tersedia di `statev2.sqlite` (dibaca read-only oleh notifier) | `[local]` |
| Token akses | bearer session dengan scope (`orchestration:read/operate`, `filesystem:*`, dll.) | `[cli]` |
| Notifier | `t3-wa-notify` aktif; Telegram `enabled`, chatId dan token tersimpan mode 600 | `[local]` |
| Bot Telegram | `getMe` OK; belum ada webhook; update diambil via long polling | `[local]` + `[tg]` |
| Kanban | belum ada | `[local]` |

Implikasi langsung: **bridge tidak perlu API baru.** Ia cukup memanggil CLI yang sudah ada, dan memakai kredensial Telegram yang sudah tersimpan.

---

## 3. Ide 1 — Telegram dua arah ke thread yang sudah ada

### Yang mungkin, dengan bukti

| Kemampuan | Mekanisme | Status |
|---|---|---|
| Lihat daftar project | baca `statev2.sqlite` (`projects`), sama seperti notifier | mungkin `[local]` |
| Lihat daftar thread per project | `t3 thread list --project <path> --json` | mungkin `[cli]` |
| Lihat konteks terakhir sebuah thread | `t3 thread show <id> --json --messages N` | mungkin `[cli]` |
| Kirim pesan ke thread yang ada | `t3 thread send <id> -` (pesan dari stdin) | mungkin `[cli]` |
| Membuat thread baru dari HP | `t3 thread start <project> - --title ... --provider ... --model ...` | mungkin `[cli]` |
| Menunggu hasil lalu membalas | `--wait` + `--json`; kode keluar 2 = butuh jawaban | mungkin `[cli]` |

### Batasan yang harus dihormati

1. **Thread yang sibuk tidak terinterupsi.** Pesan masuk menjadi turn berikutnya `[cli]`. Untuk "kirim sekarang juga", tidak ada jalan tanpa menghentikan run — dan itu fitur, bukan bug.
2. **Membalas pertanyaan persetujuan bukan lewat `thread send`.** Persetujuan di T3 adalah objek `pending request`, bukan pesan biasa `[t3]`. CLI tidak punya subcommand untuk menjawabnya `[cli]`. Dua jalan: (a) manusia menjawab di UI, atau (b) sebuah *control thread* yang punya tool MCP menjawabnya. Bridge Telegram sebaiknya **memberi tahu** dan menyertakan tautan thread, bukan berpura-pura bisa menjawab.
3. **Bot tidak bisa memulai percakapan.** Pengguna harus menekan Start lebih dulu; ini pernah terjadi di sesi sebelumnya `[local]`, dan sesuai aturan Bot API `[tg]`.
4. **Satu mode update.** Long polling dan webhook saling eksklusif `[tg]`. Pilihan long polling lebih sederhana karena tidak butuh domain, TLS, atau port publik.
5. **Update disimpan maksimal 24 jam** `[tg]`. Bridge yang mati lebih dari sehari kehilangan perintah lama — dan itu benar: perintah lama sebaiknya tidak dieksekusi terlambat `[inference]`.

### Risiko keamanan

Bot Telegram adalah **input tidak tepercaya**. Siapa pun yang tahu username bot bisa mengirim pesan; yang menyelamatkan hanya allowlist `chat_id` `[tg]`. Karena perintah bridge bisa menyuruh agent menulis kode, allowlist itu wajib, bukan opsional. Tambahan: perintah destruktif (`push`, hapus) tetap ditolak di lapisan bridge, dan pesan dari Telegram tidak boleh diteruskan mentah-mentah sebagai instruksi sistem `[inference]`.

---

## 4. Ide 2 — Kanban + watcher + testing + deploy

### Bentuk alur yang realistis

```
backlog → ready → in-progress → review/test → done
             │          │            │
             │          │            └─ watcher test: jalankan test suite, tulis bukti
             │          └─ watcher dev: t3 thread start/send + model pilihan
             └─ gerbang: hanya tiket dengan label ready yang diambil
```

### Di mana menyimpan tiket

| Opsi | Kelebihan | Kekurangan | Bukti |
|---|---|---|---|
| **GitHub Issues** | sudah ada untuk repo `momokii/*`; label, sub-issue, dependency, Projects; API + webhook | butuh token dengan scope repo; satu sumber kebenaran tersebar di banyak repo | `[gh]` |
| SQLite lokal + UI kecil | cepat, offline, kontrol penuh; cocok dengan pola `t3-wa-notify` | UI harus dibuat; tidak ada riwayat kolaboratif | `[inference]` |
| Markdown di repo (`tickets/*.md`) | sederhana, terlihat di diff, mudah di-review | tanpa query/status machine; rawan konflik | `[inference]` |

Rekomendasi: **GitHub Issues sebagai sumber tiket**, karena statusnya sudah punya label dan API-nya stabil `[gh]`, sementara watcher tetap lokal. Kalau ingin tanpa ketergantungan jaringan, mulai dari SQLite dan migrasi nanti `[inference]`.

### Watcher: tiga bentuk, dari paling aman

| Bentuk | Cara kerja | Risiko | Catatan |
|---|---|---|---|
| **Poller** (systemd timer, tiap 1–5 menit) | baca tiket `ready` → `t3 thread start`/`send` → catat id thread ke tiket | rendah; mudah dihentikan | paling mudah diaudit `[inference]` |
| **Agent watcher** (satu thread yang punya tool MCP) | thread itu yang memilih tiket, menjawab pending request, memindahkan status | sedang; butuh prompt yang ketat | satu-satunya cara menjawab persetujuan otomatis `[t3]` |
| **Event-driven** (webhook GitHub → bridge) | reaksi instan saat label berubah | butuh endpoint publik + tanda tangan | lebih kompleks, belum perlu `[inference]` |

### Gerbang yang tidak boleh dilewati

1. **Model dipilih per tiket**, bukan per watcher: `t3 thread start --provider ... --model ...` `[cli]`. Ini juga memenuhi aturan "satu model per thread" saat delegasi `[local]`.
2. **Test watcher tidak menilai kode sendiri.** Ia menjalankan perintah test yang sudah ditentukan di tiket dan menempelkan output. Penilaian "lulus/tidak" berbasis exit code, bukan opini model `[inference]`.
3. **Push/deploy tetap di tangan manusia** sampai ada dua hal: (a) aturan repo mengizinkannya, (b) ada jalur rollback yang terbukti. Sampai itu ada, watcher berhenti di "siap di-push" dan mengirim bukti `[inference]`.
4. **Persetujuan multi-kanal**: satu layanan persetujuan yang mengirim ke Telegram (dan WhatsApp bila hidup), menyimpan `request_id`, dan menerima balasan `approve/reject <id>` dari kanal mana pun. Balasan pertama menang; sisanya diabaikan `[inference]`.

### Yang membuat ini gagal di praktik

| Mode kegagalan | Pencegahan |
|---|---|
| Dua watcher mengambil tiket yang sama | klaim atomik: label `in-progress` + timestamp, atau `UPDATE ... WHERE status='ready'` di SQLite `[inference]` |
| Tiket "selesai" padahal test tidak jalan | definisi selesai per tiket wajib memuat perintah test dan bukti output `[inference]` |
| Thread menumpuk karena tiap tiket membuat thread baru | satu thread per tiket untuk kerja terisolasi; satu thread per project untuk kerja berkelanjutan `[inference]` |
| Biaya model meledak | batas per tiket (maks turn/waktu) dan pemilihan model eksplisit `[inference]` |
| Bot dibanjiri notifikasi | gabungkan per project, kirim ringkasan per 5 menit, bukan per event `[tg]` (batas 1 pesan/detik per chat) |

---

## 5. Arsitektur yang diusulkan

```
┌─────────────┐   long poll    ┌──────────────┐   subprocess   ┌────────────┐
│  Telegram   │ ◄────────────► │  t3-bridge   │ ─────────────► │  t3 CLI    │
│  (manusia)  │                │  (daemon)    │                │  (server)  │
└─────────────┘                └──────┬───────┘                └────────────┘
                                      │ baca/tulis
                                      ▼
                               ┌──────────────┐
                               │ state.sqlite │  offset update, allowlist, audit log
                               └──────────────┘

Fase 2:
┌──────────────┐   poll 1-5 m   ┌──────────────┐   t3 thread start/send
│ GitHub Issues│ ─────────────► │  watcher-dev │ ──────────────────────► thread
│  (tiket)     │ ◄───────────── │  (systemd)   │ ◄────── exit code + log
└──────────────┘   label/comment└──────────────┘
```

Komponen dan tanggung jawab:

| Komponen | Tanggung jawab | Tidak boleh |
|---|---|---|
| `t3-bridge` | terima perintah Telegram, validasi, panggil CLI, balas ringkas | menafsirkan pesan sebagai instruksi sistem, menjalankan push |
| `state.sqlite` | offset update, allowlist chat, audit perintah | menyimpan token |
| `watcher-dev` | ambil tiket `ready`, jalankan thread, catat id thread | menilai hasil test |
| `watcher-test` | jalankan perintah test, tempel output, pindahkan status | mengubah kode |
| `approval` | kirim permintaan ke kanal, tunggu balasan pertama, catat | menyetujui sendiri |

---

## 6. Rencana bertahap dengan kriteria selesai

| Fase | Isi | Kriteria selesai | Status |
|---|---|---|---|
| **1. Bridge baca** | `/help`, `/projects`, `/threads`, `/ctx` | perintah dijawab dari data nyata; tidak ada penulisan ke T3 | **selesai** — `bridge/t3_bridge.py`, diuji dengan T3 sungguhan |
| **1b. Bridge tulis** | `/send`, `/new` | pesan benar-benar masuk ke thread tujuan; id thread divalidasi; audit tercatat | **selesai** — 17 test lulus, jalur tulis diuji sampai ke CLI nyata (kasus negatif) |
| **2. Approval routing** | kirim permintaan persetujuan ke Telegram, balasan `approve/reject` | satu permintaan, satu balasan, idempoten; percobaan kedua diabaikan | desain siap, belum dibangun |
| **3. Tiket** | GitHub Issues + label status, atau SQLite | tiket bisa dibuat dari Telegram, status terlihat | desain siap |
| **4. Watcher dev** | poller → `t3 thread start/send`, catat id thread ke tiket | satu tiket = satu thread, tanpa duplikasi | desain siap |
| **5. Watcher test** | jalankan test, tempel bukti, pindah status | status hanya berubah bila exit code 0 | desain siap |
| **6. Rilis** | changelog + bukti + PR, push manual | PR berisi changelog dan bukti test | desain siap, menunggu keputusan aturan push |

---

## 7. Keputusan yang menunggu Anda

1. **Sumber tiket**: GitHub Issues atau SQLite lokal?
2. **Batas otonomi**: watcher boleh `thread start` sendiri, atau selalu membuat draft yang Anda setujui?
3. **Push**: tetap manual, atau nanti didelegasikan ke watcher setelah ada rollback teruji?
4. **Kanal persetujuan**: Telegram saja, atau Telegram + WhatsApp saat sesi WA hidup?

---

## 8. Verifikasi riset ini

| Klaim | Cara diperiksa | Hasil |
|---|---|---|
| CLI punya `thread send/start/list/show` | `t3 thread --help` | terlihat di output |
| `send` mengantre, tidak menginterupsi | teks bantuan `t3 thread send --help` | dikutip apa adanya |
| Kode keluar `--wait` 1/2 | teks bantuan `--wait` | dikutip apa adanya |
| CLI tidak punya `project list` | `t3 project --help` | hanya add/remove/rename |
| Bot bisa long polling, bukan webhook | `getUpdates` di mesin ini mengembalikan update; webhook kosong | `[local]` + `[tg]` |
| Batas kirim Telegram | FAQ resmi | 1/detik per chat, 20/menit per grup, ±30/detik broadcast |
| Update hilang setelah 24 jam | halaman Bot API | dikutip |
| GitHub Issues punya label/sub-issue/dependency | docs GitHub | dikutip |
| Token & chat id tersedia untuk dipakai ulang | config notifier | ada, mode 600 |
| Nama tabel project | `projection_projects` di `statev2.sqlite` | dikonfirmasi; tabel `projects` tidak ada |
| Bentuk JSON `thread show` | `{"thread": …, "messages": […]}` | dikonfirmasi; formatter awal salah membaca `items` dan sudah diperbaiki |
| Bridge fase 1 berjalan | `--selftest`, `--once`, 17 test | 10 project terbaca; `/threads kelanach` menampilkan 2 thread; jalur tulis mencapai CLI |
