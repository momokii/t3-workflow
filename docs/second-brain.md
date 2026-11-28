# Second Brain view

The board has a second destination, **Second Brain** (`/#brain`), showing the `~/brain` vault as a note
index and an interactive graph. It is read-only, token-guarded, and has no build step.

The view is deliberately a thin client over the vault's public CLI contract (`docs/contract.md` in the
vault): `brain list`, `brain graph` and `brain get`. The board does not parse Markdown, frontmatter or
wikilinks, and never writes to the vault. One link definition therefore serves both Obsidian and the
board.

## Setup

The menu is optional. Without a vault and a CLI that implements the contract below, the board still
runs; **Second Brain** shows a clear error and the rest of the queue is unaffected.

### Reference implementation

The vault is produced by the separate [`t3-brain`](https://github.com/momokii/t3-brain) repository. T3
Code must be installed and running; it uses the same T3 server and ticket database:

```bash
git clone https://github.com/momokii/t3-brain.git "$HOME/brain"
cd "$HOME/brain"
make check
make sync
test -x "$HOME/brain/bin/brain" && echo "brain CLI: OK"
```

Install and initialize `t3-workflow` first; `t3-brain` reads T3 and the ticket database. `make sync`
writes Markdown notes and never writes back to T3. The vault is plain files; open `~/brain` in Obsidian
with **Open folder as vault**. The default Obsidian vault name is
`brain`; the board reads the folder name from the running board, so a vault under a different folder
name uses that name for the `obsidian://` link.

### Required structure

```
~/brain/
├── Home.md                     # optional entry note; any root note is fine
├── 00 Inbox/                   # human capture
├── 10 Projects/                # generated indexes, threads, tickets and reports
│   └── <project>/*.md
├── 20 Areas/                   # durable human notes
├── 30 Resources/               # durable human notes
├── 40 Archive/                 # archive
├── 90 Meta/                    # schema, status and conventions
└── bin/brain                   # executable CLI
```

`t3-brain` creates the generated folders. If you bring your own vault, it needs:

- a folder of `.md` files, readable by the board user;
- an executable CLI at `BRAIN_BIN` (default `~/brain/bin/brain`);
- notes with optional flat frontmatter: `type`, `title`, `project`, `generated`;
- `[[wikilinks]]` with unique basenames, or path-qualified links;
- no symlinked note paths (the board rejects them).

Notes are the only graph nodes. Attachments, tags, unresolved links and generated folders are not
special node types in this version. The board never edits the vault and never calls `brain put`.

### CLI contract

Any implementation works if these three commands print JSON:

```bash
brain list --json
brain graph --json
brain get '<relative path>.md' --json
```

The board consumes the exact shapes documented on this page; it does not parse Markdown or resolve
wikilinks itself. A broken link is fixed in the vault tool, once, and both Obsidian and the board
agree. The CLI is invoked with an argument vector, `shell=False`, a minimal environment and a
10-second timeout.

### Enable it in the board

Put the paths in the board's environment file so the systemd unit receives them; use absolute paths,
not `~` or shell expressions:

```bash
cat >> "$HOME/.config/t3-workflow/board.env" <<'EOF'
BRAIN_VAULT=/home/you/brain
BRAIN_BIN=/home/you/brain/bin/brain
EOF
chmod 600 "$HOME/.config/t3-workflow/board.env"
systemctl --user restart t3-board
```

`BRAIN_VAULT` and `BRAIN_BIN` can also be passed as `--brain-vault` / `--brain-bin` when running the
board by hand. An empty value falls back to `~/brain` and `~/brain/bin/brain`.

### Verify

```bash
set -a
. "$HOME/.config/t3-workflow/board.env"
set +a

# 401: note bodies are private
curl -s -o /dev/null -w '%{http_code}\n' \
  "http://127.0.0.1:8787/api/brain/notes"

# 200: the same count the CLI reports
curl -fsS -H "X-Board-Token: $T3_BOARD_TOKEN" \
  "http://127.0.0.1:8787/api/brain/notes" | python3 -c \
  'import json,sys; print(len(json.load(sys.stdin)), "notes")'

BRAIN_VAULT="$HOME/brain" "$HOME/brain/bin/brain" list --json | python3 -c \
  'import json,sys; print(len(json.load(sys.stdin)), "notes")'

# No writes from the board
git -C "$HOME/brain" status --porcelain
```

The two counts must match. `400` for traversal, `404` for a missing note and `503` for an unavailable
CLI are the expected error boundaries.

### Docker

The board image does not mount a vault by default. If using compose, mount the vault and CLI read-only
at the same absolute paths and add the two variables to `board.env`; see
[docker.md](docker.md#second-brain-optional).

### Cannot enable it?

Check, in this order:

1. `test -x "$BRAIN_BIN"` as the service user;
2. `test -r "$BRAIN_VAULT"` and `BRAIN_VAULT/Home.md`;
3. `BRAIN_VAULT=... "$BRAIN_BIN" list --json` exits `0`;
4. the board was restarted after editing `board.env`;
5. `/api/brain/notes` with the token returns `200`, not `401` or `503`.

The board host must be able to read the vault; on a remote machine that means the vault lives there,
not on the browser's machine.

## Data flow

```
browser ── X-Board-Token ──▶ board/server.py ── argv, shell=False ──▶ brain CLI ──▶ ~/brain (read)
                                  │
                                  └─ 10 s cache for list + graph, ?refresh=1 bypasses it
```

| Route | Source | Shape |
|---|---|---|
| `GET /api/brain/notes` | `brain list --json` | array of `{path, basename, type, title, project, zone, generated, updated, modified, size}` |
| `GET /api/brain/graph` | `brain graph --json` | `{version, nodes[], edges[], unresolved[], stats}` |
| `GET /api/brain/note?path=…` | `brain get <path> --json` | `{path, basename, frontmatter, body}` |

All three require `X-Board-Token`, including note bodies. The defaults are `~/brain` and
`~/brain/bin/brain`; override with `BRAIN_VAULT` / `BRAIN_BIN`, or with `--brain-vault` /
`--brain-bin`. Failures map to a one-line `503`; a missing note is `404`.

## Path handling

`path` is untrusted input. Before calling the CLI the board rejects:

- empty paths, absolute paths, `..` or `.` segments, NUL bytes, and non-`.md` suffixes;
- a path that resolves outside the vault;
- a path with any symlink component.

The returned note keeps its vault-relative path; the local absolute path is never sent to the browser.
The CLI runs with an argument vector, `shell=False`, a 10-second timeout, and `BRAIN_VAULT` in its
environment.

## Interface

- **Desktop** (≥ 1000 px): list left, graph right. **Mobile**: List / Graph tabs, list first; the graph
  tab scrolls the graph into view.
- **List**: search, zone/type/project filters, orphan filter, sortable columns, count, empty and error
  states. It is the keyboard-accessible text alternative to the canvas graph, as WCAG 2.2 SC 1.1.1
  requires.
- **Graph**: Cytoscape.js 3.35.0 (MIT, vendored at `board/static/vendor/cytoscape.min.js`) with a cose
  layout; node size by degree; colour by project inside `10 Projects` and by zone elsewhere, or by
  note type via the Colour control; pan, wheel and button zoom, Fit, Reset view, Re-layout, labels and
  arrows toggles, local graph depth 1–2, and a legend. Hover highlights neighbours; click selects and opens the note.
- **Note detail**: frontmatter, raw Markdown in a read-only `<pre>`, outlinks and backlinks computed
  from the loaded graph, Copy Markdown, and an `obsidian://open` convenience link. Note content is
  inserted with `textContent`, never `innerHTML`.

The list and graph share one filter state. Neither view polls; Refresh is explicit. The list remains
usable when the graph library fails to load.

## Limits

- Notes and `[[wikilinks]]` only; attachments and tags are not graph nodes.
- **Open in Obsidian** uses the vault folder name (default `brain`) as the Obsidian vault name.
- No editing, no tag nodes, no server-side full-text search, no clustering for very large vaults.
- The board host must be able to execute the brain CLI and read the vault.
- `git -C ~/brain status` must stay clean with respect to this feature: the view never writes.

## Verification

`tests/test_board.py` covers token `401` on all three routes, passthrough shape, note detail, traversal
and symlink rejection, missing-note `404`, CLI `503`, the static navigation/mounts, and the vendored
library. Existing board tests remain unchanged. Manual checks use a fixture vault, never the live
`~/brain` for assertions.
