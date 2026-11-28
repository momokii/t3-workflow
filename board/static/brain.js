/* Second Brain: a read-only view over the brain vault.
 *
 * The only data source is the brain CLI through /api/brain/*; this file never parses
 * wikilinks and never renders note Markdown as HTML. The note list is the text
 * alternative to the canvas graph (WCAG 2.2 SC 1.1.1), not a fallback.
 */
(() => {
  'use strict';

  const view = document.getElementById('brain-view');
  if (!view) return;

  const TOKEN = (document.querySelector('meta[name="board-token"]') || {}).content || '';
  const $ = (id) => document.getElementById(id);
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const ZONE_COLORS = ['#4f46e5', '#0f766e', '#b45309', '#b91c1c', '#7c3aed', '#0369a1', '#4d7c0f', '#9d174d',
                       '#0e7490', '#a16207', '#4338ca', '#be123c'];

  const state = {
    notes: [],
    graph: { nodes: [], edges: [], unresolved: [], stats: {} },
    loaded: false,
    loading: false,
    selected: '',
    sort: { key: 'title', dir: 1 },
    cy: null,
    lastTrigger: null,
    noteSeq: 0,
  };

  const search = $('brain-search');
  const zoneSel = $('brain-zone');
  const typeSel = $('brain-type');
  const projectSel = $('brain-project');
  const orphansSel = $('brain-orphans');
  const localToggle = $('brain-local');
  const depthSel = $('brain-depth');
  const labelsToggle = $('brain-labels');
  const arrowsToggle = $('brain-arrows');
  const statusEl = $('brain-status');
  const table = document.querySelector('.brain-table');
  const tableWrap = document.querySelector('.brain-table-wrap');
  const listEmpty = $('brain-list-empty');
  const listEmptyText = $('brain-empty-text');
  const retryBtn = $('brain-retry');
  const graphEmpty = $('brain-graph-empty');
  const graphEmptyText = $('brain-graph-empty-text');
  const graphRetry = $('brain-graph-retry');
  const graphEl = $('brain-graph');
  const summaryEl = $('brain-summary');
  const graphSummaryEl = $('brain-graph-summary');
  const zoomEl = $('brain-zoom');
  const noteDialog = $('dlg-note');

  function toast(message, isError = false) {
    const box = document.getElementById('toast');
    if (!box) return;
    const item = document.createElement('div');
    item.className = 'item' + (isError ? ' err' : '');
    item.textContent = message;
    box.appendChild(item);
    setTimeout(() => item.remove(), isError ? 6000 : 2600);
  }

  async function api(path) {
    const headers = TOKEN ? { 'X-Board-Token': TOKEN } : {};
    const res = await fetch(path, { headers, cache: 'no-store' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  function setStatus(message, isError = false) {
    statusEl.textContent = message || '';
    statusEl.classList.toggle('is-error', Boolean(isError));
  }

  function displayZone() {
    const stored = localStorage.getItem('boardTimeZone');
    if (!stored || stored === 'local') return undefined;
    return stored;
  }

  function dateValue(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatDate(value) {
    const date = dateValue(value);
    if (!date) return value ? String(value) : '—';
    try {
      return new Intl.DateTimeFormat(undefined, {
        month: 'short', day: 'numeric', timeZone: displayZone(),
      }).format(date);
    } catch (_) {
      return date.toLocaleDateString();
    }
  }

  function formatDateFull(value) {
    const date = dateValue(value);
    if (!date) return value ? String(value) : '—';
    try {
      return new Intl.DateTimeFormat(undefined, {
        dateStyle: 'medium', timeStyle: 'short', timeZone: displayZone(),
      }).format(date);
    } catch (_) {
      return date.toLocaleString();
    }
  }

  function formatSize(bytes) {
    const n = Number(bytes);
    if (!Number.isFinite(n) || n < 0) return '—';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  }

  function groupFor(node) {
    // `10 Projects` is one zone holding every project's generated notes; colouring the
    // zone would paint 340 of 363 nodes the same colour. Split it by project instead.
    if (node.zone === '10 Projects') {
      return node.project ? `Project · ${node.project}` : '10 Projects · unassigned';
    }
    return node.zone || 'root';
  }

  function colorForGroup(group) {
    let hash = 0;
    const text = String(group || 'root');
    for (let i = 0; i < text.length; i += 1) hash = ((hash << 5) - hash + text.charCodeAt(i)) | 0;
    return ZONE_COLORS[Math.abs(hash) % ZONE_COLORS.length];
  }

  function noteLabel(path) {
    const node = state.graph.nodes.find((n) => n.id === path);
    if (node) return node.label || path;
    const note = state.notes.find((n) => n.path === path);
    return note ? note.title || note.basename || path : path;
  }

  function degreeOf(path) {
    const node = state.graph.nodes.find((n) => n.id === path);
    return node ? Number(node.degree) || 0 : 0;
  }

  function selectedProject() {
    return projectSel.value === '__none__' ? '' : projectSel.value;
  }

  function filteredNotes() {
    const needle = search.value.trim().toLowerCase();
    const zone = zoneSel.value;
    const type = typeSel.value;
    const project = selectedProject();
    const projectFilter = projectSel.value;
    const orphanMode = orphansSel.value;
    return state.notes.filter((note) => {
      if (zone && note.zone !== zone) return false;
      if (type && note.type !== type) return false;
      if (projectFilter === '__none__' && note.project) return false;
      if (projectFilter !== '__none__' && project && note.project !== project) return false;
      if (orphanMode !== 'all') {
        const orphan = degreeOf(note.path) === 0;
        if (orphanMode === 'hide' && orphan) return false;
        if (orphanMode === 'only' && !orphan) return false;
      }
      if (!needle) return true;
      const haystack = [note.title, note.basename, note.path, note.project, note.zone, note.type]
        .join(' ').toLowerCase();
      return haystack.includes(needle);
    });
  }

  function sortedNotes(notes) {
    const { key, dir } = state.sort;
    return notes.slice().sort((a, b) => {
      if (key === 'size') return ((Number(a.size) || 0) - (Number(b.size) || 0)) * dir;
      if (key === 'modified') {
        const av = a.modified ? Date.parse(a.modified) : 0;
        const bv = b.modified ? Date.parse(b.modified) : 0;
        return (av - bv) * dir;
      }
      return String(a[key] || '').localeCompare(String(b[key] || ''), undefined, { sensitivity: 'base' }) * dir;
    });
  }

  function fillSelect(select, values, allLabel) {
    const previous = select.value;
    select.replaceChildren(new Option(allLabel, ''));
    values.forEach((value) => select.appendChild(new Option(value, value)));
    if (values.includes(previous)) select.value = previous;
  }

  function renderFilters() {
    const zones = [...new Set(state.notes.map((n) => n.zone).filter(Boolean))].sort();
    const types = [...new Set(state.notes.map((n) => n.type).filter(Boolean))].sort();
    const projects = [...new Set(state.notes.map((n) => n.project).filter(Boolean))].sort();
    fillSelect(zoneSel, zones, 'All zones');
    fillSelect(typeSel, types, 'All types');
    const previousProject = projectSel.value;
    projectSel.replaceChildren(new Option('All projects', ''));
    projects.forEach((value) => projectSel.appendChild(new Option(value, value)));
    projectSel.appendChild(new Option('No project', '__none__'));
    if (previousProject && (projects.includes(previousProject) || previousProject === '__none__')) {
      projectSel.value = previousProject;
    }
  }

  function renderSortIndicators() {
    document.querySelectorAll('.brain-sort').forEach((button) => {
      const th = button.closest('th');
      if (!th) return;
      if (button.dataset.sort === state.sort.key) {
        th.setAttribute('aria-sort', state.sort.dir === 1 ? 'ascending' : 'descending');
      } else {
        th.removeAttribute('aria-sort');
      }
    });
  }

  function renderList() {
    const rows = sortedNotes(filteredNotes());
    const tbody = $('brain-rows');
    tbody.replaceChildren();
    $('brain-list-count').textContent = String(rows.length);
    tableWrap.hidden = rows.length === 0;
    listEmpty.hidden = rows.length !== 0;

    if (!rows.length) {
      if (!state.notes.length) {
        listEmptyText.textContent = state.loaded ? 'This vault has no notes yet.' : 'Loading notes…';
      } else {
        listEmptyText.textContent = 'No notes match these filters.';
      }
      retryBtn.hidden = state.loaded;
      return;
    }
    retryBtn.hidden = false;

    rows.forEach((note) => {
      const tr = document.createElement('tr');
      tr.dataset.path = note.path;
      tr.title = note.path;

      const titleCell = document.createElement('td');
      const titleButton = document.createElement('button');
      titleButton.type = 'button';
      titleButton.className = 'brain-note-title';
      titleButton.textContent = note.title || note.basename || note.path;
      titleButton.setAttribute('aria-label', `Open note ${note.title || note.basename || note.path}`);
      titleButton.addEventListener('click', () => openNote(note.path, titleButton));
      titleCell.appendChild(titleButton);
      tr.appendChild(titleCell);

      const cells = [
        [note.type || '—', note.type || ''],
        [note.project || '—', note.project || ''],
        [note.zone || '—', note.zone || ''],
        [formatDate(note.modified), formatDateFull(note.modified)],
        [formatSize(note.size), `${Number(note.size) || 0} bytes`],
      ];
      cells.forEach(([value, title], index) => {
        const td = document.createElement('td');
        td.textContent = value;
        td.title = title;
        if (index === 3) td.className = 'muted';
        tr.appendChild(td);
      });

      tr.addEventListener('click', (event) => {
        if (event.target.closest('button')) return;
        openNote(note.path, tr);
      });
      tbody.appendChild(tr);
    });
    renderSortIndicators();
  }

  // ---------------------------------------------------------------- graph

  function graphStyle() {
    const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const labelColor = dark ? '#d7deea' : '#394352';
    const edgeColor = dark ? '#3b4657' : '#c7d0dc';
    return [
      {
        selector: 'node',
        style: {
          'background-color': 'data(color)',
          width: 'mapData(degree, 0, 20, 22, 58)',
          height: 'mapData(degree, 0, 20, 22, 58)',
          label: 'data(label)',
          color: labelColor,
          'font-size': 9,
          'font-weight': 500,
          'text-wrap': 'ellipsis',
          'text-max-width': 92,
          'text-valign': 'bottom',
          'text-margin-y': 3,
          'text-background-color': dark ? '#161a21' : '#ffffff',
          'text-background-opacity': 0.66,
          'text-background-padding': '2px',
          'border-width': 1,
          'border-color': dark ? '#0f1115' : '#ffffff',
          'min-zoomed-font-size': 7,
        },
      },
      { selector: 'node.brain-dim', style: { opacity: 0.18 } },
      { selector: 'node.brain-near', style: { 'border-width': 3, 'border-color': '#8b8cf9' } },
      { selector: 'node:selected', style: { 'border-width': 4, 'border-color': '#8b8cf9' } },
      {
        selector: 'edge',
        style: {
          width: 1,
          'line-color': edgeColor,
          'curve-style': 'bezier',
          opacity: 0.75,
          'target-arrow-color': edgeColor,
        },
      },
      { selector: 'edge.brain-dim', style: { opacity: 0.06 } },
    ];
  }

  function graphElements() {
    const elements = [];
    state.graph.nodes.forEach((node) => {
      elements.push({
        data: {
          id: node.id,
          label: node.label || node.id,
          type: node.type || '',
          project: node.project || '',
          zone: node.zone || 'root',
          group: groupFor(node),
          degree: Number(node.degree) || 0,
          color: colorForGroup(groupFor(node)),
        },
      });
    });
    const seen = new Set();
    state.graph.edges.forEach((edge, index) => {
      const key = `${edge.source}\u0000${edge.target}`;
      if (seen.has(key)) return;
      seen.add(key);
      elements.push({
        data: {
          id: `link-${index}`,
          source: edge.source,
          target: edge.target,
        },
      });
    });
    return elements;
  }

  function ensureGraph() {
    if (state.cy) return state.cy;
    if (!window.cytoscape) {
      showGraphMessage('The graph library did not load. The note list still works.', true);
      return null;
    }
    try {
      state.cy = window.cytoscape({
        container: graphEl,
        elements: [],
        style: graphStyle(),
        layout: { name: 'preset' },
        wheelSensitivity: 0.25,
        minZoom: 0.08,
        maxZoom: 3,
        boxSelectionEnabled: false,
      });
    } catch (err) {
      showGraphMessage(`The graph could not start: ${err.message}`, true);
      return null;
    }
    const cy = state.cy;
    cy.on('mouseover', 'node', (event) => {
      const node = event.target;
      cy.elements().addClass('brain-dim');
      node.closedNeighborhood().removeClass('brain-dim').addClass('brain-near');
    });
    cy.on('mouseout', 'node', () => cy.elements().removeClass('brain-dim brain-near'));
    cy.on('tap', 'node', (event) => {
      const path = event.target.id();
      openNote(path, graphEl);
    });
    cy.on('tap', (event) => {
      if (event.target !== cy) return;
      cy.nodes().unselect();
      state.selected = '';
      applyGraphFilter();
    });
    cy.on('zoom', () => {
      zoomEl.textContent = `${Math.round(cy.zoom() * 100)}%`;
    });
    if (window.ResizeObserver) {
      const observer = new ResizeObserver(() => cy.resize());
      observer.observe(graphEl);
    }
    return cy;
  }

  function graphLayout(fit) {
    const cy = state.cy;
    if (!cy) return;
    const layout = cy.layout({
      name: 'cose',
      animate: !reducedMotion,
      animationDuration: 450,
      randomize: true,
      nodeRepulsion: 42000,
      idealEdgeLength: 88,
      edgeElasticity: 90,
      gravity: 0.18,
      nodeOverlap: 8,
      numIter: 900,
      padding: 36,
      fit: false,
    });
    layout.one('layoutstop', () => {
      applyGraphFilter();
      if (fit) fitGraph();
    });
    layout.run();
  }

  function renderGraph(refit = true) {
    const cy = ensureGraph();
    if (!cy) return;
    hideGraphMessage();
    cy.elements().remove();
    cy.add(graphElements());
    applyGraphToggles();
    if (state.selected) {
      const node = cy.getElementById(state.selected);
      if (node.length) node.select();
    }
    if (!state.graph.nodes.length) {
      showGraphMessage('This vault has no notes yet.', false);
      graphSummaryEl.textContent = '0 notes · 0 links. The list is the keyboard-accessible alternative.';
    } else if (refit) {
      applyGraphFilter();
      graphLayout(true);
    } else {
      applyGraphFilter();
    }
    renderLegend();
    $('brain-graph').setAttribute(
      'aria-label',
      `Note graph with ${state.graph.nodes.length} notes and ${state.graph.edges.length} links. ` +
      'Use the note list for the keyboard-accessible view.'
    );
  }

  function renderLegend() {
    let legend = document.getElementById('brain-legend');
    if (!legend) {
      legend = document.createElement('div');
      legend.id = 'brain-legend';
      legend.className = 'brain-legend';
      legend.setAttribute('aria-label', 'Graph colours by zone');
      const pane = $('brain-graph-pane');
      pane.appendChild(legend);
    }
    legend.replaceChildren();
    const counts = new Map();
    state.graph.nodes.forEach((node) => {
      const group = groupFor(node);
      counts.set(group, (counts.get(group) || 0) + 1);
    });
    [...counts.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 6)
      .forEach(([zone, count]) => {
        const item = document.createElement('span');
        const dot = document.createElement('i');
        dot.style.background = colorForGroup(zone);
        item.appendChild(dot);
        item.appendChild(document.createTextNode(`${zone} (${count})`));
        legend.appendChild(item);
      });
    if (counts.size > 6) {
      const more = document.createElement('span');
      more.textContent = `+${counts.size - 6} more zones`;
      legend.appendChild(more);
    }
  }

  function applyGraphToggles() {
    const cy = state.cy;
    if (!cy) return;
    const labels = labelsToggle.checked;
    const arrows = arrowsToggle.checked;
    cy.nodes().forEach((node) => node.style('label', labels ? node.data('label') : ''));
    cy.edges().forEach((edge) => edge.style('target-arrow-shape', arrows ? 'triangle' : 'none'));
  }

  function localSet(start, depth) {
    const adjacency = new Map();
    state.graph.edges.forEach((edge) => {
      if (!adjacency.has(edge.source)) adjacency.set(edge.source, new Set());
      if (!adjacency.has(edge.target)) adjacency.set(edge.target, new Set());
      adjacency.get(edge.source).add(edge.target);
      adjacency.get(edge.target).add(edge.source);
    });
    const allowed = new Set([start]);
    let frontier = [start];
    for (let level = 0; level < depth; level += 1) {
      const next = [];
      frontier.forEach((path) => {
        (adjacency.get(path) || new Set()).forEach((neighbour) => {
          if (allowed.has(neighbour)) return;
          allowed.add(neighbour);
          next.push(neighbour);
        });
      });
      frontier = next;
    }
    return allowed;
  }

  function graphFilterSet() {
    const visible = new Set(filteredNotes().map((note) => note.path));
    if (localToggle.checked) {
      if (!state.selected) return { visible, localMissing: true };
      const allowed = localSet(state.selected, Number(depthSel.value) || 1);
      return { visible: new Set([...visible].filter((path) => allowed.has(path))), localMissing: false };
    }
    return { visible, localMissing: false };
  }

  function applyGraphFilter() {
    const cy = state.cy;
    if (!cy) return;
    const { visible, localMissing } = graphFilterSet();
    cy.batch(() => {
      cy.nodes().forEach((node) => node.style('display', visible.has(node.id()) ? 'element' : 'none'));
      cy.edges().forEach((edge) => {
        const show = visible.has(edge.data('source')) && visible.has(edge.data('target'));
        edge.style('display', show ? 'element' : 'none');
      });
    });
    const visibleEdges = cy.edges().filter((edge) => edge.style('display') !== 'none').length;
    const local = localToggle.checked && !localMissing;
    const summary = `${visible.size} of ${state.graph.nodes.length} notes · ${visibleEdges} links` +
      (local ? ` · local depth ${depthSel.value}` : '');
    graphSummaryEl.textContent = `${summary}. The list is the keyboard-accessible alternative.`;

    if (localMissing) {
      showGraphMessage('Select a note first, or turn off Local graph.', false);
      return;
    }
    if (!visible.size) {
      showGraphMessage(state.graph.nodes.length
        ? 'No notes match these filters.'
        : 'This vault has no notes yet.', false);
      return;
    }
    hideGraphMessage();
  }

  function showGraphMessage(message, canRetry) {
    graphEmpty.hidden = false;
    graphEmptyText.textContent = message;
    graphRetry.hidden = !canRetry;
  }

  function hideGraphMessage() {
    graphEmpty.hidden = true;
  }

  function fitGraph() {
    const cy = state.cy;
    if (!cy) return;
    const shown = cy.elements().filter((element) => element.style('display') !== 'none');
    if (shown.length) cy.fit(shown, 36);
    else cy.fit(undefined, 36);
  }

  // ---------------------------------------------------------------- note detail

  function renderLinkList(list, paths, emptyText) {
    list.replaceChildren();
    const unique = [...new Set(paths)].filter(Boolean);
    if (!unique.length) {
      const li = document.createElement('li');
      li.className = 'none';
      li.textContent = emptyText;
      list.appendChild(li);
      return;
    }
    unique.forEach((path) => {
      const li = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = noteLabel(path);
      button.title = path;
      button.addEventListener('click', () => openNote(path, button));
      li.appendChild(button);
      list.appendChild(li);
    });
  }

  function renderFrontmatter(frontmatter) {
    const dl = $('note-frontmatter');
    dl.replaceChildren();
    const entries = Object.entries(frontmatter || {});
    if (!entries.length) {
      const dt = document.createElement('dt');
      dt.textContent = '—';
      const dd = document.createElement('dd');
      dd.textContent = 'No frontmatter';
      dl.append(dt, dd);
      return;
    }
    entries.forEach(([key, value]) => {
      const dt = document.createElement('dt');
      dt.textContent = key;
      const dd = document.createElement('dd');
      if (Array.isArray(value)) dd.textContent = value.join(', ');
      else if (value && typeof value === 'object') dd.textContent = JSON.stringify(value);
      else dd.textContent = value === null || value === undefined ? '—' : String(value);
      dl.append(dt, dd);
    });
  }

  async function openNote(path, trigger) {
    if (!path) return;
    state.selected = path;
    state.lastTrigger = trigger || document.activeElement;
    const cy = state.cy;
    if (cy) {
      cy.nodes().unselect();
      const node = cy.getElementById(path);
      if (node && node.length) node.select();
    }
    if (localToggle.checked) applyGraphFilter();

    const row = document.querySelector(`tr[data-path="${CSS.escape(path)}"]`);
    if (row && trigger !== row) row.scrollIntoView({ block: 'nearest' });

    const sequence = ++state.noteSeq;
    $('note-kicker').textContent = 'Second Brain · note';
    $('note-title').textContent = noteLabel(path);
    $('note-meta').textContent = path;
    $('note-body').textContent = 'Loading note…';
    $('note-frontmatter').replaceChildren();
    renderLinkList($('note-outlinks'), [], 'None');
    renderLinkList($('note-backlinks'), [], 'None');
    $('note-obsidian').href = `obsidian://open?vault=brain&file=${encodeURIComponent(path.replace(/\.md$/i, ''))}`;
    $('note-copy').disabled = true;
    if (!noteDialog.open) noteDialog.showModal();
    $('note-close').focus();

    try {
      const note = await api(`/api/brain/note?path=${encodeURIComponent(path)}`);
      if (sequence !== state.noteSeq) return;
      const title = (note.frontmatter && note.frontmatter.title) || note.basename || note.title || path;
      $('note-title').textContent = String(title);
      $('note-kicker').textContent = 'Second Brain · note';
      const meta = [note.frontmatter && note.frontmatter.type, note.frontmatter && note.frontmatter.zone]
        .filter(Boolean).join(' · ');
      $('note-meta').textContent = meta ? `${meta} · ${note.path || path}` : (note.path || path);
      $('note-body').textContent = note.body || '';
      renderFrontmatter(note.frontmatter);
      const outlinks = state.graph.edges.filter((edge) => edge.source === path).map((edge) => edge.target);
      const backlinks = state.graph.edges.filter((edge) => edge.target === path).map((edge) => edge.source);
      renderLinkList($('note-outlinks'), outlinks, 'No outlinks');
      renderLinkList($('note-backlinks'), backlinks, 'No backlinks');
      $('note-copy').disabled = false;
      $('note-copy').onclick = async () => {
        try {
          await copyText(note.body || '');
          toast('Markdown copied');
        } catch (err) {
          toast(`Copy failed: ${err.message}`, true);
        }
      };
    } catch (err) {
      if (sequence !== state.noteSeq) return;
      $('note-body').textContent = `Could not load this note: ${err.message}`;
      $('note-copy').disabled = true;
    }
  }

  async function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand('copy');
    } finally {
      area.remove();
    }
  }

  // ---------------------------------------------------------------- load

  function showListError(message) {
    tableWrap.hidden = true;
    listEmpty.hidden = false;
    listEmptyText.textContent = message;
    retryBtn.hidden = false;
  }

  function updateSummary() {
    const stats = state.graph.stats || {};
    const nodes = Number(stats.nodes ?? state.graph.nodes.length);
    const edges = Number(stats.edges ?? state.graph.edges.length);
    const unresolved = Number(stats.unresolved ?? (state.graph.unresolved || []).length);
    const parts = [`${nodes} notes`, `${edges} links`];
    if (unresolved) parts.push(`${unresolved} unresolved`);
    summaryEl.textContent = `${parts.join(' · ')}. Notes and wikilinks only; attachments and tags are not nodes.`;
    graphSummaryEl.textContent = `${nodes} notes · ${edges} links. The list is the keyboard-accessible alternative.`;
  }

  async function loadBrain(refresh = false) {
    if (state.loading) return;
    state.loading = true;
    setStatus('Loading notes and graph…');
    $('brain-refresh').disabled = true;
    if (!state.loaded) {
      tableWrap.hidden = true;
      listEmpty.hidden = false;
      listEmptyText.textContent = 'Loading notes…';
      retryBtn.hidden = true;
      showGraphMessage('Loading graph…', false);
    }
    const suffix = refresh ? '?refresh=1' : '';
    try {
      const [notes, graph] = await Promise.all([
        api(`/api/brain/notes${suffix}`),
        api(`/api/brain/graph${suffix}`),
      ]);
      if (!Array.isArray(notes)) throw new Error('the notes response is not a list');
      if (!graph || !Array.isArray(graph.nodes) || !Array.isArray(graph.edges)) {
        throw new Error('the graph response is malformed');
      }
      state.notes = notes;
      state.graph = {
        nodes: graph.nodes,
        edges: graph.edges,
        unresolved: Array.isArray(graph.unresolved) ? graph.unresolved : [],
        stats: graph.stats || {},
      };
      state.loaded = true;
      renderFilters();
      renderList();
      updateSummary();
      if (!state.selected || !notes.some((note) => note.path === state.selected)) state.selected = '';
      renderGraph(true);
      setStatus(`Loaded ${notes.length} notes.`);
    } catch (err) {
      const message = `Could not load the vault: ${err.message}`;
      setStatus(message, true);
      if (!state.loaded) {
        showListError(message);
        showGraphMessage(message, true);
      } else {
        toast(message, true);
      }
    } finally {
      state.loading = false;
      $('brain-refresh').disabled = false;
    }
  }

  // ---------------------------------------------------------------- routing and events

  function setMobilePane(pane) {
    view.dataset.mobilePane = pane;
    $('brain-tab-list').setAttribute('aria-selected', String(pane === 'list'));
    $('brain-tab-graph').setAttribute('aria-selected', String(pane === 'graph'));
    if (pane === 'graph' && state.loaded) {
      const cy = ensureGraph();
      if (cy) {
        requestAnimationFrame(() => {
          cy.resize();
          if (!cy.elements().length) renderGraph(true);
          else fitGraph();
          $('brain-graph-pane').scrollIntoView({
            behavior: reducedMotion ? 'auto' : 'smooth', block: 'start',
          });
        });
      }
    } else if (pane === 'list') {
      $('brain-list-pane').scrollIntoView({
        behavior: reducedMotion ? 'auto' : 'smooth', block: 'start',
      });
    }
  }

  function applyRoute() {
    const brain = location.hash === '#brain';
    view.hidden = !brain;
    document.body.classList.toggle('route-brain', brain);
    const boardNav = $('nav-board');
    const brainNav = $('nav-brain');
    if (brain) {
      boardNav.removeAttribute('aria-current');
      brainNav.setAttribute('aria-current', 'page');
      if (!state.loaded && !state.loading) loadBrain();
      if (state.loaded && state.cy) {
        requestAnimationFrame(() => {
          state.cy.resize();
          fitGraph();
        });
      }
    } else {
      brainNav.removeAttribute('aria-current');
      boardNav.setAttribute('aria-current', 'page');
    }
  }

  search.addEventListener('input', () => {
    window.clearTimeout(search._timer);
    search._timer = window.setTimeout(() => {
      renderList();
      applyGraphFilter();
    }, 150);
  });
  [zoneSel, typeSel, projectSel, orphansSel].forEach((select) => {
    select.addEventListener('change', () => {
      renderList();
      applyGraphFilter();
    });
  });
  document.querySelectorAll('.brain-sort').forEach((button) => {
    button.addEventListener('click', () => {
      const key = button.dataset.sort;
      if (state.sort.key === key) state.sort.dir *= -1;
      else state.sort = { key, dir: key === 'modified' || key === 'size' ? -1 : 1 };
      renderList();
    });
  });
  $('brain-refresh').addEventListener('click', () => loadBrain(true));
  $('brain-retry').addEventListener('click', () => loadBrain(true));
  graphRetry.addEventListener('click', () => loadBrain(true));
  $('brain-tab-list').addEventListener('click', () => setMobilePane('list'));
  $('brain-tab-graph').addEventListener('click', () => setMobilePane('graph'));
  const tabs = [$('brain-tab-list'), $('brain-tab-graph')];
  tabs.forEach((tab, index) => {
    tab.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const nextIndex = event.key === 'Home' ? 0
        : event.key === 'End' ? tabs.length - 1
        : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      setMobilePane(nextIndex === 0 ? 'list' : 'graph');
      tabs[nextIndex].focus();
    });
  });
  $('brain-list-graph').addEventListener('click', () => setMobilePane('graph'));
  $('brain-graph-list').addEventListener('click', () => setMobilePane('list'));
  labelsToggle.addEventListener('change', applyGraphToggles);
  arrowsToggle.addEventListener('change', applyGraphToggles);
  localToggle.addEventListener('change', () => {
    if (localToggle.checked && !state.selected) {
      setStatus('Select a note to use the local graph.');
    }
    applyGraphFilter();
  });
  depthSel.addEventListener('change', applyGraphFilter);
  $('brain-fit').addEventListener('click', fitGraph);
  const zoomBy = (factor) => {
    const cy = state.cy;
    if (!cy) return;
    const level = Math.min(3, Math.max(0.08, cy.zoom() * factor));
    const box = graphEl.getBoundingClientRect();
    cy.zoom({ level, renderedPosition: { x: box.width / 2, y: box.height / 2 } });
    zoomEl.textContent = `${Math.round(cy.zoom() * 100)}%`;
  };
  $('brain-zoom-in').addEventListener('click', () => zoomBy(1.35));
  $('brain-zoom-out').addEventListener('click', () => zoomBy(1 / 1.35));
  $('brain-reset').addEventListener('click', () => {
    if (!state.cy) return;
    state.cy.zoom(1);
    state.cy.center();
    zoomEl.textContent = '100%';
  });
  $('brain-relayout').addEventListener('click', () => {
    applyGraphToggles();
    graphLayout(true);
    setStatus('Laying out the graph…');
  });
  $('note-close').addEventListener('click', () => noteDialog.close());
  noteDialog.addEventListener('close', () => {
    if (state.lastTrigger && document.contains(state.lastTrigger)) {
      state.lastTrigger.focus();
      return;
    }
    const row = state.selected ? document.querySelector(`tr[data-path="${CSS.escape(state.selected)}"] .brain-note-title`) : null;
    (row || $('brain-refresh')).focus();
  });
  window.addEventListener('hashchange', applyRoute);
  window.addEventListener('resize', () => {
    if (state.cy) state.cy.resize();
  });
  if (window.matchMedia) {
    const scheme = window.matchMedia('(prefers-color-scheme: dark)');
    if (scheme.addEventListener) {
      scheme.addEventListener('change', () => {
        if (state.cy) state.cy.style(graphStyle());
      });
    }
  }

  applyRoute();
})();
