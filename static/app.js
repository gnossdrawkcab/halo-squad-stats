(() => {
  const searchInput = document.getElementById("session-search");
  const table = document.querySelector('[data-table="session"]');
  if (!searchInput || !table) {
    return;
  }

  const rows = Array.from(table.querySelectorAll("tbody tr"));

  const normalize = (value) => value.toLowerCase();

  searchInput.addEventListener("input", (event) => {
    const query = normalize(event.target.value.trim());
    rows.forEach((row) => {
      if (row.querySelector(".empty")) {
        row.style.display = "";
        return;
      }
      const text = normalize(row.textContent);
      row.style.display = text.includes(query) ? "" : "none";
    });
  });
})();

// Per-table horizontal pagination
function attachTableNav() {
  const buildNav = (wrap) => {
    const nav = document.createElement('div');
    nav.className = 'table-nav';
    const prev = document.createElement('button');
    prev.className = 'btn table-nav-btn';
    prev.textContent = '←';
    prev.addEventListener('click', () => {
      wrap.scrollLeft -= wrap.clientWidth * 0.8;
    });
    const next = document.createElement('button');
    next.className = 'btn table-nav-btn';
    next.textContent = '→';
    next.addEventListener('click', () => {
      wrap.scrollLeft += wrap.clientWidth * 0.8;
    });
    nav.appendChild(prev);
    nav.appendChild(next);
    wrap.parentNode.insertBefore(nav, wrap);
  };

  const evaluate = (wrap) => {
    if (wrap.classList.contains('no-table-nav')) return;
    const overflows = wrap.scrollWidth - wrap.clientWidth > 4;
    const existing = wrap.previousElementSibling?.classList.contains('table-nav')
      ? wrap.previousElementSibling : null;
    // Only show the ← → controls when the table actually overflows; otherwise
    // they read as broken pagination. Re-checked on resize below.
    if (overflows && !existing) {
      buildNav(wrap);
    } else if (!overflows && existing) {
      existing.remove();
    }
  };

  const evaluateAll = () =>
    document.querySelectorAll('.table-wrap, .table-scroll-container').forEach(evaluate);

  evaluateAll();
  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(evaluateAll, 150);
  });
}

// Client-side filtering for session/all matches table
function initSessionFilters() {
  const form = document.querySelector('.filters');
  if (!form) return;

  const selects = Array.from(form.querySelectorAll('select'));
  selects.forEach((sel) => sel.addEventListener('change', () => form.submit()));
}

// Generic table search filters
function initTableFilters() {
  document.querySelectorAll('[data-table-filter]').forEach((input) => {
    const tableName = input.dataset.tableFilter;
    if (!tableName) return;
    const table = document.querySelector(`[data-table="${tableName}"]`);
    if (!table) return;
    const rows = Array.from(table.querySelectorAll('tbody tr'));

    input.addEventListener('input', () => {
      const query = input.value.trim().toLowerCase();
      rows.forEach((row) => {
        if (row.querySelector('.empty')) {
          row.style.display = '';
          return;
        }
        const text = row.textContent.toLowerCase();
        row.style.display = text.includes(query) ? '' : 'none';
      });
    });
  });
}

// Theme Toggle
function toggleTheme() {
  const html = document.documentElement;
  const currentTheme = html.getAttribute('data-theme') || 'dark';
  const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
  html.setAttribute('data-theme', newTheme);
  localStorage.setItem('theme', newTheme);
  
  // Update toggle button emoji
  const toggle = document.querySelector('.theme-toggle');
  if (toggle) {
    toggle.textContent = newTheme === 'dark' ? '🌙' : '☀️';
  }
}

// Load saved theme on page load
(function() {
  const savedTheme = localStorage.getItem('theme') || 'dark';
  document.documentElement.setAttribute('data-theme', savedTheme);
  
  // Update toggle button on load
  document.addEventListener('DOMContentLoaded', () => {
    const toggle = document.querySelector('.theme-toggle');
    if (toggle) {
      toggle.textContent = savedTheme === 'dark' ? '🌙' : '☀️';
    }
  });
})();

// Export Dropdown
function toggleExport() {
  const menu = document.getElementById('exportMenu');
  if (menu) {
    menu.classList.toggle('show');
  }
}

// Mobile navigation toggle
// Per-game ↔ per-minute toggle for combat-leader rate stats.
function setRate(btn, mode) {
  const wrap = btn.closest('.cl-rate-wrap');
  if (!wrap) return;
  const grid = wrap.querySelector('.cl-grid');
  if (grid) {
    grid.classList.toggle('rate-mode-min', mode === 'min');
    grid.classList.toggle('rate-mode-pg', mode !== 'min');
  }
  wrap.querySelectorAll('.rate-btn').forEach((b) => b.classList.toggle('active', b.dataset.rate === mode));
}

function toggleNav() {
  const body = document.body;
  if (!body) return;
  if (body.classList.contains('nav-open')) {
    closeNav();
  } else {
    openNav();
  }
}

function openNav() {
  const body = document.body;
  if (!body) return;
  body.classList.remove('nav-closing');
  body.classList.add('nav-open');
  const toggle = document.querySelector('.nav-toggle');
  if (toggle) {
    toggle.setAttribute('aria-expanded', 'true');
  }
}

function closeNav() {
  const body = document.body;
  if (!body || !body.classList.contains('nav-open')) return;
  const toggle = document.querySelector('.nav-toggle');
  if (toggle) {
    toggle.setAttribute('aria-expanded', 'false');
  }
  const nav = document.getElementById('siteNav');
  const isMobile = window.matchMedia('(max-width: 980px)').matches;
  // On mobile, play the slide-out animation, then tear down (back to display:none).
  if (nav && isMobile) {
    body.classList.add('nav-closing');
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      body.classList.remove('nav-open', 'nav-closing');
      nav.removeEventListener('animationend', finish);
    };
    nav.addEventListener('animationend', finish);
    setTimeout(finish, 320); // fallback if animationend never fires
  } else {
    body.classList.remove('nav-open', 'nav-closing');
  }
}

// Edge-swipe to open (drag right from the left edge) / swipe-left to close.
function initNavSwipe() {
  const EDGE = 32;     // px from the left edge that starts an open-gesture
  const THRESH = 55;   // px of horizontal travel to trigger
  let startX = null, startY = null, tracking = false;
  const isMobile = () => window.matchMedia('(max-width: 980px)').matches;

  document.addEventListener('touchstart', (e) => {
    if (!isMobile() || e.touches.length !== 1) { tracking = false; return; }
    const t = e.touches[0];
    startX = t.clientX; startY = t.clientY;
    const open = document.body.classList.contains('nav-open');
    tracking = open || startX <= EDGE;   // close-gesture anywhere when open; else edge only
  }, { passive: true });

  document.addEventListener('touchmove', (e) => {
    if (!tracking || startX === null || e.touches.length !== 1) return;
    const t = e.touches[0];
    const dx = t.clientX - startX;
    const dy = t.clientY - startY;
    if (Math.abs(dy) > Math.abs(dx) && Math.abs(dy) > 12) { tracking = false; return; } // vertical scroll
    const open = document.body.classList.contains('nav-open');
    if (!open && dx > THRESH) { openNav(); tracking = false; }
    else if (open && dx < -THRESH) { closeNav(); tracking = false; }
  }, { passive: true });

  const reset = () => { startX = startY = null; tracking = false; };
  document.addEventListener('touchend', reset, { passive: true });
  document.addEventListener('touchcancel', reset, { passive: true });
}

// Mobile: turn record-list tables into stacked label:value cards (CSS does the
// layout at <=600px; this just tags them + injects per-cell data-labels from the
// header row). True matrix tables are left as horizontal-scroll + sticky col.
// Mobile: collapse the wide matrix detail tables on the dashboard so the page
// isn't a mile long. They stay expanded on desktop (Pat's preference); on a
// phone the card views above already summarize them. Tap to expand.
function initMobileCollapse() {
  if (!window.matchMedia('(max-width: 600px)').matches) return;
  // Keep the report-card full-stat table expanded on mobile — Pat wants the
  // whole session table (every stat, every player) visible on the summary.
  document.querySelectorAll('details.ranked-details[open]:not(.rc-detail-table-wrap)').forEach((d) => d.removeAttribute('open'));
}

function initResponsiveTables() {
  const STACK = new Set([
    'maps', 'modes', 'player-maps', 'player-modes', 'trends', 'sos',
    'snapshots', 'snap-maps', 'snap-csr', 'snap-30day', 'sessions',
    'session-compare', 'role-heatmap', 'recap-players', 'recap-notable',
    'notable-games', 'momentum', 'map-veto', 'lineups-2', 'lineups-3', 'lineups-4',
    'consistency', 'clutch-index', 'change-summary', 'highlights',
  ]);
  document.querySelectorAll('table.data-table').forEach((t) => {
    const name = t.getAttribute('data-table');
    const wantCards = t.classList.contains('cards') || (name && STACK.has(name));
    if (!wantCards || t.classList.contains('heatmap-table')) return; // never stack a matrix
    t.classList.add('cards');
    const wrap = t.closest('.table-wrap');
    if (wrap) wrap.classList.add('cards-wrap');
    const ths = Array.from(t.querySelectorAll('thead th')).map((th) => th.textContent.trim());
    if (!ths.length) return;
    t.querySelectorAll('tbody tr').forEach((tr) => {
      const tds = tr.querySelectorAll('td');
      if (tds.length !== ths.length) return; // skip group/empty/colspan rows
      tds.forEach((td, i) => {
        if (!td.hasAttribute('data-label')) td.setAttribute('data-label', ths[i] || '');
      });
    });
  });
}

function initMobileNavGroups() {
  document.querySelectorAll('.site-nav .nav-link').forEach((link) => {
    link.addEventListener('click', () => {
      if (!window.matchMedia('(max-width: 980px)').matches) return;
      closeNav();
    });
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeNav();
    }
  });
}

function initStickyFilters() {
  document.querySelectorAll('form.filters').forEach((form) => {
    form.classList.add('sticky');
  });
}

function initFilterChips() {
  document.querySelectorAll('form.filters').forEach((form) => {
    const buildChips = () => {
      let chipContainer = form.nextElementSibling;
      if (!chipContainer || !chipContainer.classList.contains('filter-chips')) {
        chipContainer = document.createElement('div');
        chipContainer.className = 'filter-chips';
        form.insertAdjacentElement('afterend', chipContainer);
      }
      chipContainer.innerHTML = '';

      const inputs = Array.from(form.querySelectorAll('select, input'));
      const chips = [];

      inputs.forEach((input) => {
        const value = String(input.value || '').trim();
        if (!value || value === 'all') return;
        const label = input.closest('label');
        const labelText = label ? (label.childNodes[0]?.textContent || label.textContent || '').trim() : input.name;
        const defaultOption = input.tagName === 'SELECT' ? input.querySelector('option')?.value : '';
        const resetValue = input.tagName === 'SELECT' ? (defaultOption ?? '') : '';

        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'filter-chip';
        chip.textContent = `${labelText}: ${value}`;
        chip.addEventListener('click', () => {
          if (input.tagName === 'SELECT') {
            input.value = resetValue;
          } else {
            input.value = '';
          }
          form.submit();
        });
        chips.push(chip);
      });

      chips.forEach((chip) => chipContainer.appendChild(chip));

      if (chips.length) {
        const clear = document.createElement('button');
        clear.type = 'button';
        clear.className = 'filter-clear';
        clear.textContent = 'Clear Filters';
        clear.addEventListener('click', () => {
          inputs.forEach((input) => {
            if (input.tagName === 'SELECT') {
              const first = input.querySelector('option');
              if (first) input.value = first.value;
            } else {
              input.value = '';
            }
          });
          form.submit();
        });
        chipContainer.appendChild(clear);
      }
    };

    form.addEventListener('change', buildChips);
    buildChips();
  });
}

function initPlayerHoverCard() {
  if (!window.matchMedia('(hover: hover)').matches) return;
  const data = window.playerHoverData || {};
  const targets = Array.from(document.querySelectorAll('.player-name'));
  if (!targets.length) return;

  const card = document.createElement('div');
  card.className = 'player-hover-card';
  document.body.appendChild(card);

  let hideTimer = null;

  const hideCard = () => {
    card.classList.remove('visible');
  };

  const showCard = (target) => {
    const name = (target.dataset.player || target.textContent || '').trim();
    if (!name) return;
    const info = data[name.toLowerCase()];
    if (!info) return;

    card.textContent = '';
    const makeRow = (label, value) => {
      const row = document.createElement('div');
      row.className = 'hover-row';
      const lbl = document.createElement('span');
      lbl.className = 'hover-label';
      lbl.textContent = label;
      const val = document.createElement('span');
      val.textContent = value;
      row.appendChild(lbl);
      row.appendChild(val);
      return row;
    };
    const nameDiv = document.createElement('div');
    nameDiv.className = 'hover-name';
    nameDiv.textContent = info.player;
    card.appendChild(nameDiv);
    card.appendChild(makeRow('Win %', info.win_pct));
    card.appendChild(makeRow('KDA', info.kda));
    card.appendChild(makeRow('CSR', info.csr));
    card.appendChild(makeRow('Last', info.last_match));

    card.style.left = '0px';
    card.style.top = '0px';
    card.classList.add('visible');

    const rect = target.getBoundingClientRect();
    const cardRect = card.getBoundingClientRect();
    const padding = 12;
    let left = rect.left + rect.width / 2 - cardRect.width / 2;
    left = Math.max(padding, Math.min(left, window.innerWidth - cardRect.width - padding));

    let top = rect.top - cardRect.height - 12;
    if (top < padding) {
      top = rect.bottom + 12;
    }
    card.style.left = `${left}px`;
    card.style.top = `${top}px`;
  };

  targets.forEach((target) => {
    target.addEventListener('mouseenter', () => {
      clearTimeout(hideTimer);
      showCard(target);
    });
    target.addEventListener('mouseleave', () => {
      hideTimer = setTimeout(hideCard, 120);
    });
  });
}

function updateCsrOnlineStatus() {
  const dots = document.querySelectorAll('[data-last-match]');
  if (!dots.length) return;
  const now = Date.now();
  dots.forEach((dot) => {
    const raw = dot.getAttribute('data-last-match');
    if (!raw) return;
    const ts = Date.parse(raw);
    if (Number.isNaN(ts)) return;
    const diffMinutes = (now - ts) / 60000;
    const isOnline = diffMinutes <= 20;
    dot.classList.toggle('online', isOnline);
    const rounded = Math.max(0, Math.round(diffMinutes));
    const statusLabel = isOnline ? 'Online' : 'Offline';
    dot.setAttribute('title', `${statusLabel} (last game ${rounded}m ago)`);
  });
}

function initCsrOnlineStatus() {
  updateCsrOnlineStatus();
  setInterval(updateCsrOnlineStatus, 60000);
}

// Close export dropdown when clicking outside
document.addEventListener('click', (e) => {
  const dropdown = document.querySelector('.export-dropdown');
  const menu = document.getElementById('exportMenu');
  if (dropdown && menu && !dropdown.contains(e.target)) {
    menu.classList.remove('show');
  }
});

// Keyboard navigation for horizontally scrollable tables
document.addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  const wrap = Array.from(document.querySelectorAll('.table-wrap')).find((el) => {
    const rect = el.getBoundingClientRect();
    return rect.top < window.innerHeight && rect.bottom > 0;
  });
  if (!wrap) return;
  const scrollAmount = wrap.clientWidth * 0.8;
  wrap.scrollLeft = wrap.scrollLeft + (e.key === 'ArrowLeft' ? -scrollAmount : scrollAmount);
});

function parseSortDate(text) {
  if (!text) return null;
  const hasDate = /\d{4}-\d{2}-\d{2}/.test(text) || /\d{1,2}\/\d{1,2}\/\d{2,4}/.test(text);
  if (!hasDate) return null;
  const parsed = Date.parse(text);
  return Number.isNaN(parsed) ? null : parsed;
}

function parseSortTime(text) {
  if (!text) return null;
  const trimmed = text.trim();
  const daysMatch = trimmed.match(/^(\d+)\s+days?\s+(\d{1,2}):(\d{2}):(\d{2}(?:\.\d+)?)$/);
  if (daysMatch) {
    const days = Number(daysMatch[1]);
    const hours = Number(daysMatch[2]);
    const minutes = Number(daysMatch[3]);
    const seconds = Number(daysMatch[4]);
    return (((days * 24 + hours) * 60 + minutes) * 60) + seconds;
  }

  const hmsMatch = trimmed.match(/^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$/);
  if (hmsMatch) {
    const hours = Number(hmsMatch[1]);
    const minutes = Number(hmsMatch[2]);
    const seconds = Number(hmsMatch[3]);
    return ((hours * 60 + minutes) * 60) + seconds;
  }

  const msMatch = trimmed.match(/^(\d+):(\d{2}(?:\.\d+)?)$/);
  if (msMatch) {
    const minutes = Number(msMatch[1]);
    const seconds = Number(msMatch[2]);
    return minutes * 60 + seconds;
  }

  return null;
}

function parseSortNumber(text) {
  if (!text) return null;
  const cleaned = text.replace(/,/g, '').replace(/%$/, '');
  if (!cleaned) return null;
  const value = Number(cleaned);
  return Number.isNaN(value) ? null : value;
}

const GRADE_RANK = {
  'S': 14, 'A+': 13, 'A': 12, 'A-': 11, 'B+': 10, 'B': 9, 'B-': 8,
  'C+': 7, 'C': 6, 'C-': 5, 'D+': 4, 'D': 3, 'D-': 2, 'F': 1,
};
function getSortValue(cell) {
  if (!cell) return { type: 'empty', value: null };
  // Grade cells sort by tier rank (S above A above B…), not alphabetically.
  if (!cell.getAttribute('data-sort')) {
    const g = cell.querySelector('.inline-grade, .grade-badge');
    if (g) {
      const rank = GRADE_RANK[g.textContent.trim().toUpperCase()];
      if (rank) return { type: 'number', value: rank };
    }
  }
  const raw = (cell.getAttribute('data-sort') || cell.getAttribute('data-value') || cell.textContent || '').trim();
  if (!raw || raw === '-') return { type: 'empty', value: null };

  const dateValue = parseSortDate(raw);
  if (dateValue !== null) return { type: 'number', value: dateValue };

  const timeValue = parseSortTime(raw);
  if (timeValue !== null) return { type: 'number', value: timeValue };

  const numberValue = parseSortNumber(raw);
  if (numberValue !== null) return { type: 'number', value: numberValue };

  return { type: 'text', value: raw.toLowerCase() };
}

function compareSortValues(a, b, direction) {
  const aEmpty = a.type === 'empty';
  const bEmpty = b.type === 'empty';
  if (aEmpty && bEmpty) return 0;
  if (aEmpty) return 1;
  if (bEmpty) return -1;

  if (a.type === 'number' && b.type === 'number') {
    return direction === 'asc' ? a.value - b.value : b.value - a.value;
  }
  if (a.type === 'number' && b.type !== 'number') return direction === 'asc' ? -1 : 1;
  if (a.type !== 'number' && b.type === 'number') return direction === 'asc' ? 1 : -1;

  const aText = a.value || '';
  const bText = b.value || '';
  return direction === 'asc' ? aText.localeCompare(bText) : bText.localeCompare(aText);
}

function heatmapColor(score) {
  const clamped = Math.max(0, Math.min(1, score));
  if (clamped >= 0.5) {
    const t = (clamped - 0.5) * 2;
    const r = Math.round(65 + (22 - 65) * t);
    const g = Math.round(82 + (163 - 82) * t);
    const b = Math.round(52 + (74 - 52) * t);
    return `rgba(${r}, ${g}, ${b}, ${0.18 + 0.28 * t})`;
  }
  const t = (0.5 - clamped) * 2;
  const r = Math.round(65 + (127 - 65) * t);
  const g = Math.round(82 + (29 - 82) * t);
  const b = Math.round(52 + (29 - 52) * t);
  return `rgba(${r}, ${g}, ${b}, ${0.18 + 0.30 * t})`;
}

function heatmapTextColor(score) {
  if (score >= 0.72) return '#b7f7c7';
  if (score <= 0.28) return '#fecaca';
  return '';
}

function shouldInvertHeatColumn(headerText) {
  const h = (headerText || '').toLowerCase();
  return /\b(deaths?|d\/g|d$|loss(?:es)?|l$|dmg-|damage taken|taken|against|allowed|betrayals?|suicides?|turnovers?|gap against)\b/.test(h);
}

function heatmapRawValue(cell) {
  if (!cell) return null;
  const explicit = cell.getAttribute('data-val') || cell.getAttribute('data-value') || cell.getAttribute('data-sort');
  if (explicit !== null && explicit !== '') return parseSortNumber(String(explicit).trim());
  const grade = cell.querySelector('.inline-grade, .grade-badge, .sb-grade');
  if (grade) {
    const rank = GRADE_RANK[(grade.textContent || '').trim().toUpperCase()];
    if (rank) return rank;
  }
  return parseSortNumber((cell.textContent || '').trim());
}

function applyGlobalTableHeatmaps() {
  document.querySelectorAll('table.data-table').forEach((table) => {
    if (table.classList.contains('heatmap-table') || table.dataset.heatmap === 'off') return;
    const headers = Array.from(table.querySelectorAll('thead th'));
    const bodyRows = Array.from(table.querySelectorAll('tbody tr'))
      .filter((row) => !row.classList.contains('match-details') && !row.querySelector('.empty'));
    if (!headers.length || bodyRows.length < 2) return;

    headers.forEach((header, colIdx) => {
      const headerText = header.textContent || '';
      const cells = [];
      bodyRows.forEach((row) => {
        const cell = row.cells[colIdx];
        if (!cell || cell.colSpan > 1) return;
        const value = heatmapRawValue(cell);
        if (value === null || !Number.isFinite(value)) return;
        cells.push({
          cell,
          value,
          invert: cell.hasAttribute('data-heat-invert') || shouldInvertHeatColumn(headerText),
        });
      });
      if (cells.length < 2) return;
      const values = cells.map((item) => item.value);
      const lo = Math.min(...values);
      const hi = Math.max(...values);
      if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi === lo) return;

      cells.forEach(({ cell, value, invert }) => {
        let score = (value - lo) / (hi - lo);
        if (invert) score = 1 - score;
        cell.style.backgroundColor = heatmapColor(score);
        const textColor = heatmapTextColor(score);
        if (textColor) cell.style.color = textColor;
        cell.dataset.heatScore = score.toFixed(3);
      });
    });
  });
}

function buildRowGroups(tbody) {
  const groups = [];
  const emptyRows = [];
  const detailRows = new Map();

  tbody.querySelectorAll('tr.match-details').forEach((row) => {
    if (row.id) {
      detailRows.set(row.id, row);
    }
  });

  tbody.querySelectorAll('tr').forEach((row) => {
    if (row.classList.contains('match-details')) return;
    if (row.querySelector('.empty')) {
      emptyRows.push(row);
      return;
    }
    const groupRows = [row];
    const matchId = row.dataset.matchId;
    if (matchId) {
      const detail = detailRows.get(`details-${matchId}`);
      if (detail) {
        groupRows.push(detail);
      }
    }
    groups.push({ row, rows: groupRows });
  });

  return { groups, emptyRows };
}

function initTimelineSelector() {
  const timelineButtons = document.querySelectorAll('.timeline-btn');
  if (!timelineButtons.length) {
    return;
  }

  const timelineTbodys = document.querySelectorAll('.timeline-tbody');

  timelineButtons.forEach(button => {
    button.addEventListener('click', () => {
      const timeline = button.dataset.timeline;

      timelineButtons.forEach(btn => btn.classList.remove('active'));
      button.classList.add('active');

      timelineTbodys.forEach(tbody => {
        if (tbody.id === `timeline-${timeline}`) {
          tbody.style.display = '';
        } else {
          tbody.style.display = 'none';
        }
      });
    });
  });
}

// Table sorting (click header to sort) + attach table nav
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.data-table th').forEach((header) => {
    header.style.cursor = 'pointer';
    header.addEventListener('click', () => {
      const table = header.closest('table');
      const tbody = table?.querySelector('tbody');
      if (!table || !tbody) return;

      const columnIndex = header.cellIndex;
      const currentDir = header.classList.contains('sort-asc')
        ? 'asc'
        : header.classList.contains('sort-desc')
          ? 'desc'
          : null;
      const nextDir = currentDir === 'asc' ? 'desc' : 'asc';

      table.querySelectorAll('th').forEach((th) => {
        th.classList.remove('sort-asc', 'sort-desc');
      });
      header.classList.add(nextDir === 'asc' ? 'sort-asc' : 'sort-desc');

      const { groups, emptyRows } = buildRowGroups(tbody);
      groups.sort((a, b) => {
        const aValue = getSortValue(a.row.cells[columnIndex]);
        const bValue = getSortValue(b.row.cells[columnIndex]);
        return compareSortValues(aValue, bValue, nextDir);
      });

      tbody.innerHTML = '';
      groups.forEach((group) => {
        group.rows.forEach((row) => tbody.appendChild(row));
      });
      emptyRows.forEach((row) => tbody.appendChild(row));
      applyGlobalTableHeatmaps();
    });
  });

  attachTableNav();
  initSessionFilters();
  initTableFilters();
  initMobileNavGroups();
  initNavSwipe();
  initResponsiveTables();
  initMobileCollapse();
  initStickyFilters();
  initFilterChips();
  initPlayerHoverCard();
  initCsrOnlineStatus();
  initTimelineSelector();
  applyGlobalTableHeatmaps();
});

// Realtime stats freshness: poll a tiny DB row-count endpoint and refresh only
// when new match rows land. /live provides a partial-refresh hook so Twitch
// embeds keep playing; other pages reload into the fresh server render.
(function initRealtimeStatsRefresh() {
  if (window.haloDisableGlobalRefresh) return;
  if (window.location.search.includes('embed=1')) return;
  let knownCount = Number(window.haloInitialRowCount || 0);
  if (!knownCount) return;
  const pollMs = Math.max(1000, Number(window.haloRealtimePollSeconds || 2.5) * 1000);
  let stopped = false;
  let inFlight = false;

  function refreshForNewStats(nextCount) {
    knownCount = nextCount;
    if (typeof window.haloRefreshStats === 'function') {
      window.haloRefreshStats();
      window.setTimeout(check, pollMs);
      return;
    }
    window.location.reload();
  }

  async function check() {
    if (stopped || inFlight) return;
    inFlight = true;
    try {
      const url = '/api/site-version?since=' + encodeURIComponent(knownCount) + '&_=' + Date.now();
      const res = await fetch(url, { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      const nextCount = Number(data && data.count);
      if (nextCount && nextCount !== knownCount) {
        refreshForNewStats(nextCount);
        return;
      }
    } catch (_) {
      // Keep the watcher quiet on transient deploy/network hiccups.
    } finally {
      inFlight = false;
    }
    if (!stopped) window.setTimeout(check, pollMs);
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) check();
  });
  window.setTimeout(check, pollMs);
})();

/* ── Grade hover tooltip ──────────────────────────────────────────
   Native title= on grade badges is slow/unreliable and the big report-card
   badge had none. Show a styled tooltip on hover (desktop) and tap (mobile)
   for any grade element that carries a title. */
(function gradeTips() {
  const SEL = '.inline-grade, .grade-badge, .sb-grade';
  let pop = null;
  function ensure() {
    if (!pop) { pop = document.createElement('div'); pop.className = 'grade-tip-pop'; document.body.appendChild(pop); }
    return pop;
  }
  const LETTER_TIP = {
    S: 'S — elite (top ~10%)', A: 'A — excellent (top ~25%)',
    B: 'B — solid, middle of the pack', C: 'C — below average',
    D: 'D — well below average', F: 'F — bottom tier',
  };
  function genericTip(el) {
    const g = (el.textContent || '').trim().charAt(0).toUpperCase();
    const base = LETTER_TIP[g];
    return base ? base + '. See “How grades work”.' : '';
  }
  function tipText(el) {
    if (el.dataset.tip) return el.dataset.tip;
    let t = el.getAttribute('title');
    if (t) el.removeAttribute('title'); // steal native title to avoid double tooltip
    if (!t) {
      const anc = el.closest('[title]'); // tip may live on a parent (e.g. report-card cell)
      if (anc && anc !== el) t = anc.getAttribute('title');
    }
    if (!t) t = genericTip(el); // last resort: explain the letter so NO grade is ever bare
    el.dataset.tip = t || '';
    return el.dataset.tip;
  }
  function show(el) {
    const txt = tipText(el);
    if (!txt) return;
    const p = ensure();
    p.textContent = txt;
    p.style.display = 'block';
    const r = el.getBoundingClientRect();
    const pw = p.offsetWidth, ph = p.offsetHeight;
    let left = r.left + r.width / 2 - pw / 2;
    left = Math.max(8, Math.min(window.innerWidth - pw - 8, left));
    let top = r.top - ph - 8;
    if (top < 8) top = r.bottom + 8; // flip below if no room above
    p.style.left = left + 'px';
    p.style.top = top + 'px';
  }
  function hide() { if (pop) pop.style.display = 'none'; }
  document.addEventListener('mouseover', e => { const el = e.target.closest(SEL); if (el) show(el); });
  document.addEventListener('mouseout', e => { if (e.target.closest(SEL)) hide(); });
  document.addEventListener('click', e => { const el = e.target.closest(SEL); if (el) { e.stopPropagation(); show(el); } else hide(); });
  window.addEventListener('scroll', hide, { passive: true });
})();
