# Second Brain view

The board has a second destination, **Second Brain** (`/#brain`), showing the `~/brain` vault as a note
index and an interactive graph. It is read-only, token-guarded, and has no build step.

The view is deliberately a thin client over the vault's public CLI contract (`docs/contract.md` in the
vault): `brain list`, `brain graph` and `brain get`. The board does not parse Markdown, frontmatter or
wikilinks, and never writes to the vault. One link definition therefore serves both Obsidian and the
board.

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
  layout; node size by degree, colour by project inside `10 Projects` and by zone elsewhere; pan, wheel
  and button zoom, Fit, Reset view, Re-layout, labels and arrows toggles, local graph depth 1–2, and a
  legend. Hover highlights neighbours; click selects and opens the note.
- **Note detail**: frontmatter, raw Markdown in a read-only `<pre>`, outlinks and backlinks computed
  from the loaded graph, Copy Markdown, and an `obsidian://open` convenience link. Note content is
  inserted with `textContent`, never `innerHTML`.

The list and graph share one filter state. Neither view polls; Refresh is explicit. The list remains
usable when the graph library fails to load.

## Limits

- Notes and `[[wikilinks]]` only; attachments and tags are not graph nodes.
- No editing, no tag nodes, no server-side full-text search, no clustering for very large vaults.
- The board host must be able to execute the brain CLI and read the vault.
- `git -C ~/brain status` must stay clean with respect to this feature: the view never writes.

## Verification

`tests/test_board.py` covers token `401` on all three routes, passthrough shape, note detail, traversal
and symlink rejection, missing-note `404`, CLI `503`, the static navigation/mounts, and the vendored
library. Existing board tests remain unchanged. Manual checks use a fixture vault, never the live
`~/brain` for assertions.
