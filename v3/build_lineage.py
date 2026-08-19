import os
import sys
import re
import json
import argparse
import pandas as pd
from string import Template
from collections import defaultdict

from lineage_engine import (
    load_repo, get_folders, index_folder, index_mapping,
    build_session_order, MappingTracer,
    format_lineage_chain, format_lineage_chain_data, build_transformation_catalog,
    CATALOG_DISPLAY_COLS,
    build_mapplet_catalog, MAPPLET_CATALOG_DISPLAY_COLS,
)


_HOP_NAME_RE = re.compile(r"^(.*)\[(.*)\]$")


def _split_hops(chain):
    """'SQ[Source Qualifier] -> EXP[Expression] -> RTR[Router]' ->
       [{"label": "SQ[Source Qualifier]", "name": "SQ"}, ...]"""
    hops = []
    if not chain:
        return hops
    for part in chain.split(" -> "):
        m = _HOP_NAME_RE.match(part)
        hops.append({"label": part, "name": m.group(1) if m else part})
    return hops


def _safe_id(name):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(name))


_HTML_TEMPLATE = Template(r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>$title</title>
<style>
  :root{--bg:#0f1116;--panel:#171a21;--border:#2a2e38;--text:#e6e8ee;--muted:#9aa1b1;
        --accent:#5b8cff;--accent2:#33d69f;--hover:#1f2430;--highlight:#3a3212;}
  *{box-sizing:border-box;}
  body{margin:0;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:var(--bg);color:var(--text);}
  header{padding:18px 24px;border-bottom:1px solid var(--border);}
  header h1{margin:0 0 4px 0;font-size:18px;}
  header p{margin:0;color:var(--muted);font-size:13px;}
  .tabs{display:flex;gap:4px;padding:0 24px;border-bottom:1px solid var(--border);background:var(--panel);}
  .tab-btn{background:none;border:none;color:var(--muted);padding:12px 18px;cursor:pointer;font-size:14px;
           border-bottom:2px solid transparent;}
  .tab-btn.active{color:var(--text);border-bottom-color:var(--accent);}
  .tab-btn:hover{color:var(--text);}
  .panel{display:none;padding:16px 24px;}
  .panel.active{display:block;}
  .toolbar{display:flex;gap:10px;align-items:center;margin-bottom:10px;}
  input[type=text]{background:var(--panel);border:1px solid var(--border);color:var(--text);
                    padding:8px 10px;border-radius:6px;font-size:13px;width:320px;}
  .count{color:var(--muted);font-size:12px;}
  table{border-collapse:collapse;width:100%;font-size:12.5px;}
  th{text-align:left;background:var(--panel);color:var(--muted);padding:8px 10px;position:sticky;top:0;
     border-bottom:1px solid var(--border);white-space:nowrap;}
  td{padding:7px 10px;border-bottom:1px solid var(--border);vertical-align:top;}
  tr:hover td{background:var(--hover);}
  .tbl-wrap{max-height:70vh;overflow:auto;border:1px solid var(--border);border-radius:8px;}
  .hop{color:var(--accent);cursor:pointer;text-decoration:none;}
  .hop:hover{text-decoration:underline;color:var(--accent2);}
  .arrow{color:var(--muted);margin:0 4px;}
  .pager{display:flex;gap:8px;align-items:center;margin-top:10px;}
  .pager button{background:var(--panel);border:1px solid var(--border);color:var(--text);
                 padding:6px 10px;border-radius:6px;cursor:pointer;font-size:12px;}
  .pager button:disabled{opacity:.4;cursor:default;}
  tr.hit{animation:flash 1.6s ease-out;}
  @keyframes flash{0%{background:var(--highlight);}100%{background:transparent;}}
  .id-row{scroll-margin-top:80px;}
  .logic-cell{white-space:pre-wrap;font-family:monospace;}
  .th-inner{display:flex;align-items:center;justify-content:space-between;gap:6px;}
  .filter-btn{background:none;border:none;color:var(--muted);cursor:pointer;font-size:11px;
              padding:2px 5px;border-radius:4px;line-height:1;}
  .filter-btn:hover{background:var(--hover);color:var(--text);}
  .filter-btn.active{color:var(--accent2);}
  .filter-dropdown{position:absolute;background:var(--panel);border:1px solid var(--border);
                    border-radius:8px;padding:8px;z-index:100;max-height:320px;min-width:220px;
                    max-width:280px;overflow:auto;box-shadow:0 8px 24px rgba(0,0,0,.5);}
  .filter-dropdown input[type=text]{width:100%;margin-bottom:6px;padding:6px 8px;font-size:12px;}
  .filter-option{display:flex;align-items:center;padding:2px 2px;font-size:12px;}
  .filter-option label{display:flex;align-items:center;gap:6px;cursor:pointer;width:100%;
                        word-break:break-word;}
  .filter-option input[type=checkbox]{flex:none;}
  #filter-option-list{max-height:200px;overflow:auto;border-top:1px solid var(--border);
                       border-bottom:1px solid var(--border);margin:4px 0;padding:4px 0;}
  .filter-actions{display:flex;justify-content:flex-end;gap:6px;margin-top:8px;}
  .filter-actions button{background:var(--panel);border:1px solid var(--border);color:var(--text);
                          padding:5px 10px;border-radius:6px;cursor:pointer;font-size:11px;}
  .filter-actions button:hover{background:var(--hover);}
</style>
</head>
<body>
<header>
  <h1>$title</h1>
  <p>Tab 1: field-level lineage. Tab 2: transformation catalog. Tab 3: mapplet boundary-to-boundary
     field paths. Click any hop in a Transformation Lineage chain to jump to its row in the
     catalog (or, for a "[Mapplet]" hop, to its paths in Tab 3).</p>
</header>
<div class="tabs">
  <button class="tab-btn active" data-tab="lineage" onclick="showTab('lineage')">Lineage ($n_lineage rows)</button>
  <button class="tab-btn" data-tab="catalog" onclick="showTab('catalog')">Transformations ($n_catalog rows)</button>
$mapplets_tab_button
</div>

<div id="lineage" class="panel active">
  <div class="toolbar">
    <input type="text" id="lineage-search" placeholder="Filter lineage rows..." oninput="renderLineage()">
    <span class="count" id="lineage-count"></span>
    <span class="count">Click &#9662; on any column header to filter its values</span>
  </div>
  <div class="tbl-wrap"><table>
    <thead><tr id="lineage-head"></tr></thead>
    <tbody id="lineage-body"></tbody>
  </table></div>
  <div class="pager">
    <button onclick="lineagePage(-1)">&larr; Prev</button>
    <span id="lineage-pageinfo" class="count"></span>
    <button onclick="lineagePage(1)">Next &rarr;</button>
  </div>
</div>

<div id="catalog" class="panel">
  <div class="toolbar">
    <input type="text" id="catalog-search" placeholder="Filter transformations..." oninput="renderCatalog()">
    <span class="count" id="catalog-count"></span>
    <span class="count">Click &#9662; on any column header to filter its values</span>
  </div>
  <div class="tbl-wrap"><table>
    <thead><tr id="catalog-head"></tr></thead>
    <tbody id="catalog-body"></tbody>
  </table></div>
</div>

$mapplets_panel

<script>
const LINEAGE_COLS = $lineage_cols;
const CATALOG_COLS = $catalog_cols;
const MAPPLET_COLS = $mapplet_cols;
const lineageRows = $lineage_rows;
const catalogRows = $catalog_rows;
const mappletRows = $mapplet_rows;
const PAGE_SIZE = 100;
let lineagePageNum = 0;

// Exclude the raw JSON data column from HTML rendering
const DISPLAY_COLS = LINEAGE_COLS.filter(c => c !== 'Transformation Lineage Data');

function showTab(name){
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('active', b.dataset.tab===name));
  document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('active', p.id===name));
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderHead(rowEl, cols){
  rowEl.innerHTML = cols.map(c => '<th>' + escapeHtml(c) + '</th>').join('');
}

function hopCellHtml(chainStr, hopsData){
  if(!chainStr) return '';
  const parts = chainStr.split(' -> ');
  return parts.map((p, idx) => {
    const data = hopsData[idx] || {};
    const field = data.field || '';
    const mapping = data.mapping || '';
    const type = data.type || '';
    // Prefer the structured name from hopsData (always in sync with the
    // display string). Fallback only strips the "[Type]" suffix and any
    // ".field" suffix before it, for the rare case hopsData is missing.
    let name = data.name;
    if(!name){
      const bracket = p.match(/^(.*)\[(.*)\]$$/);
      const beforeBracket = bracket ? bracket[1] : p;
      name = field && beforeBracket.endsWith('.' + field)
        ? beforeBracket.slice(0, -(field.length + 1))
        : beforeBracket;
    }
    // Attaches Transformation Name + Field + Mapping (+ Type) so the Engine
    // knows exactly which Tab-2 row to highlight, even when the same
    // instance name is reused (with different logic) across different
    // mappings - and, for a "[Mapplet]" hop, routes to Tab-3 instead.
    return '<span class="hop" data-name="' + escapeHtml(name) + '" data-field="' + escapeHtml(field) +
           '" data-mapping="' + escapeHtml(mapping) + '" data-type="' + escapeHtml(type) +
           '">' + escapeHtml(p) + '</span>';
  }).join('<span class="arrow">&rarr;</span>');
}

function filterRows(rows, cols, query){
  if(!query) return rows;
  const q = query.toLowerCase();
  return rows.filter(r => cols.some(c => String(r[c] ?? '').toLowerCase().includes(q)));
}

// --- Per-column (Excel-style) filters, generalized across all three
// tables (Tab-1 Lineage, Tab-2 Transformations, Tab-3 Mapplets) ---
// tableKey -> { colName -> Set of allowed string values } (col absent = no filter)
const columnFilters = {lineage: {}, catalog: {}, mapplets: {}};
// tableKey -> {rows, cols, rerender, resetPage} - registered by each
// render*Head() call so the shared dropdown logic can drive any table.
const filterableTables = {};

function rowMatchesColumnFilters(tableKey, row){
  const filters = columnFilters[tableKey];
  for(const col in filters){
    const allowed = filters[col];
    if(!allowed) continue;
    if(!allowed.has(String(row[col] ?? ''))) return false;
  }
  return true;
}

function closeFilterDropdown(){
  const existing = document.getElementById('active-filter-dropdown');
  if(existing) existing.remove();
  document.removeEventListener('click', outsideFilterClickHandler);
}

function outsideFilterClickHandler(e){
  const dd = document.getElementById('active-filter-dropdown');
  if(dd && !dd.contains(e.target) && !e.target.closest('.filter-btn')) closeFilterDropdown();
}

function openFilterDropdown(tableKey, col, btnEl){
  const openDd = document.getElementById('active-filter-dropdown');
  const wasOpenForThisCol = openDd && openDd.dataset.table === tableKey && openDd.dataset.col === col;
  closeFilterDropdown();
  if(wasOpenForThisCol) return; // second click on same button just closes it

  const table = filterableTables[tableKey];
  const filters = columnFilters[tableKey];

  // Distinct values computed from the full dataset (not the currently
  // filtered view), same as Excel's column filter list.
  const values = Array.from(new Set(table.rows.map(r => String(r[col] ?? '')))).sort((a, b) => a.localeCompare(b));
  const currentSel = filters[col] || new Set(values);

  const dropdown = document.createElement('div');
  dropdown.className = 'filter-dropdown';
  dropdown.id = 'active-filter-dropdown';
  dropdown.dataset.table = tableKey;
  dropdown.dataset.col = col;

  const rect = btnEl.getBoundingClientRect();
  dropdown.style.top = (rect.bottom + window.scrollY + 4) + 'px';
  dropdown.style.left = Math.max(8, rect.right + window.scrollX - 260) + 'px';

  const search = document.createElement('input');
  search.type = 'text';
  search.placeholder = 'Search values...';
  dropdown.appendChild(search);

  const selectAllWrap = document.createElement('div');
  selectAllWrap.className = 'filter-option';
  const selectAllCb = document.createElement('input');
  selectAllCb.type = 'checkbox';
  selectAllCb.checked = currentSel.size === values.length;
  const selectAllLabel = document.createElement('label');
  selectAllLabel.appendChild(selectAllCb);
  selectAllLabel.appendChild(document.createTextNode('(Select All)'));
  selectAllWrap.appendChild(selectAllLabel);
  dropdown.appendChild(selectAllWrap);

  const listWrap = document.createElement('div');
  listWrap.id = 'filter-option-list';
  const checkboxes = [];
  values.forEach(v => {
    const opt = document.createElement('div');
    opt.className = 'filter-option';
    opt.dataset.value = v.toLowerCase();
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = v;
    cb.checked = currentSel.has(v);
    checkboxes.push(cb);
    const label = document.createElement('label');
    label.appendChild(cb);
    label.appendChild(document.createTextNode(v === '' ? '(Blanks)' : v));
    opt.appendChild(label);
    listWrap.appendChild(opt);
    cb.addEventListener('change', () => {
      selectAllCb.checked = checkboxes.every(c => c.checked);
    });
  });
  dropdown.appendChild(listWrap);

  search.addEventListener('input', () => {
    const q = search.value.toLowerCase();
    listWrap.querySelectorAll('.filter-option').forEach(opt => {
      opt.style.display = opt.dataset.value.includes(q) ? '' : 'none';
    });
  });

  selectAllCb.addEventListener('change', () => {
    const visibleCbs = Array.from(listWrap.querySelectorAll('.filter-option'))
      .filter(opt => opt.style.display !== 'none')
      .map(opt => opt.querySelector('input[type=checkbox]'));
    visibleCbs.forEach(cb => { cb.checked = selectAllCb.checked; });
  });

  const actions = document.createElement('div');
  actions.className = 'filter-actions';
  const clearBtn = document.createElement('button');
  clearBtn.type = 'button';
  clearBtn.textContent = 'Clear';
  clearBtn.addEventListener('click', () => {
    delete filters[col];
    btnEl.classList.remove('active');
    closeFilterDropdown();
    if(table.resetPage) table.resetPage();
    table.rerender();
  });
  const okBtn = document.createElement('button');
  okBtn.type = 'button';
  okBtn.textContent = 'OK';
  okBtn.addEventListener('click', () => {
    const selected = new Set(checkboxes.filter(cb => cb.checked).map(cb => cb.value));
    if(selected.size === values.length){
      delete filters[col];
      btnEl.classList.remove('active');
    } else {
      filters[col] = selected;
      btnEl.classList.add('active');
    }
    closeFilterDropdown();
    if(table.resetPage) table.resetPage();
    table.rerender();
  });
  actions.appendChild(clearBtn);
  actions.appendChild(okBtn);
  dropdown.appendChild(actions);

  document.body.appendChild(dropdown);
  search.focus();
  setTimeout(() => document.addEventListener('click', outsideFilterClickHandler), 0);
}

function renderHeadWithFilters(tableKey, headRowEl, cols, rows, rerender, resetPage){
  filterableTables[tableKey] = {rows, rerender, resetPage};
  const filters = columnFilters[tableKey];
  headRowEl.innerHTML = cols.map(c => {
    const active = filters[c] ? ' active' : '';
    return '<th><div class="th-inner"><span class="th-label">' + escapeHtml(c) +
           '</span><button type="button" class="filter-btn' + active + '" data-col="' +
           escapeHtml(c) + '" title="Filter ' + escapeHtml(c) + '">&#9662;</button></div></th>';
  }).join('');
  headRowEl.querySelectorAll('.filter-btn').forEach(btn => {
    btn.addEventListener('click', function(e){
      e.stopPropagation();
      openFilterDropdown(tableKey, btn.dataset.col, btn);
    });
  });
}

function lineageRowMatchesColumnFilters(row){
  return rowMatchesColumnFilters('lineage', row);
}

function renderLineageHead(){
  renderHeadWithFilters('lineage', document.getElementById('lineage-head'), DISPLAY_COLS,
                         lineageRows, renderLineage, () => { lineagePageNum = 0; });
}

function renderLineage(){
  const q = document.getElementById('lineage-search').value;
  let filtered = lineageRows.filter(lineageRowMatchesColumnFilters);
  filtered = filterRows(filtered, LINEAGE_COLS, q);
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  if(lineagePageNum >= totalPages) lineagePageNum = totalPages - 1;
  if(lineagePageNum < 0) lineagePageNum = 0;
  const start = lineagePageNum * PAGE_SIZE;
  const pageRows = filtered.slice(start, start + PAGE_SIZE);

  document.getElementById('lineage-body').innerHTML = pageRows.map(r => {
    return '<tr>' + DISPLAY_COLS.map(c => {
      if(c === 'Transformation Lineage') {
         let hopsData = [];
         try { hopsData = JSON.parse(r['Transformation Lineage Data'] || '[]'); } catch(e){}
         return '<td>' + hopCellHtml(r[c], hopsData) + '</td>';
      }
      return '<td>' + escapeHtml(r[c] ?? '') + '</td>';
    }).join('') + '</tr>';
  }).join('');
  document.getElementById('lineage-count').textContent = filtered.length + ' matching row(s)';
  document.getElementById('lineage-pageinfo').textContent = 'Page ' + (lineagePageNum+1) + ' / ' + totalPages;
}

function lineagePage(delta){
  lineagePageNum += delta;
  renderLineage();
}

function renderCatalog(){
  const q = document.getElementById('catalog-search').value;
  let filtered = catalogRows.filter(r => rowMatchesColumnFilters('catalog', r));
  filtered = filterRows(filtered, CATALOG_COLS, q);
  renderHeadWithFilters('catalog', document.getElementById('catalog-head'), CATALOG_COLS,
                         catalogRows, renderCatalog, null);
  document.getElementById('catalog-body').innerHTML = filtered.map(r => {
    const rid = 'tf-' + r.__id;
    return '<tr id="' + rid + '" class="id-row">' + CATALOG_COLS.map(c => {
      if(c === 'Business Logic') {
        return '<td class="logic-cell">' + escapeHtml(r[c] ?? '') + '</td>';
      }
      return '<td>' + escapeHtml(r[c] ?? '') + '</td>';
    }).join('') + '</tr>';
  }).join('');
  document.getElementById('catalog-count').textContent = filtered.length + ' matching row(s)';
}

function renderMapplets(){
  // Tab-3 only exists in the DOM when the repository actually contains
  // Mapplets (improvement-5) - no-op otherwise so init/search calls never
  // throw on a missing element.
  const searchEl = document.getElementById('mapplet-search');
  if(!searchEl) return;
  const q = searchEl.value;
  let filtered = mappletRows.filter(r => rowMatchesColumnFilters('mapplets', r));
  filtered = filterRows(filtered, MAPPLET_COLS, q);
  renderHeadWithFilters('mapplets', document.getElementById('mapplet-head'), MAPPLET_COLS,
                         mappletRows, renderMapplets, null);
  document.getElementById('mapplet-body').innerHTML = filtered.map(r => {
    const rid = 'mp-' + r.__id;
    return '<tr id="' + rid + '" class="id-row">' + MAPPLET_COLS.map(c => {
      if(c === 'Transformation_lineage') {
        return '<td class="logic-cell">' + escapeHtml(r[c] ?? '') + '</td>';
      }
      return '<td>' + escapeHtml(r[c] ?? '') + '</td>';
    }).join('') + '</tr>';
  }).join('');
  document.getElementById('mapplet-count').textContent = filtered.length + ' matching row(s)';
}

function jumpToMapplet(name, field, mapping){
  if(!document.getElementById('mapplets')) return; // no Mapplets tab in this report
  showTab('mapplets');
  document.getElementById('mapplet-search').value = '';
  renderMapplets();

  // Best match: exact Mapplet INSTANCE + Mapping + Output field (handles
  // the same mapplet definition being dropped in more than once, and the
  // same output field being fed by more than one input field).
  let matches = mappletRows.filter(r =>
    r._Mapplet_Instance === name && r.Mapping_Name === mapping &&
    (!field || r['Output Transformation Field'] === field));

  // Fallback: instance + mapping only
  if(!matches.length){
    matches = mappletRows.filter(r => r._Mapplet_Instance === name && r.Mapping_Name === mapping);
  }
  // Last resort: instance name alone
  if(!matches.length){
    matches = mappletRows.filter(r => r._Mapplet_Instance === name);
  }

  const target = matches[0];
  const rid = target ? 'mp-' + target.__id : null;
  setTimeout(() => {
    let el = rid && document.getElementById(rid);
    if(!el){
      document.getElementById('mapplet-search').value = name;
      renderMapplets();
      el = rid && document.getElementById(rid);
    }
    if(el){
      el.scrollIntoView({behavior:'smooth', block:'center'});
      el.classList.remove('hit'); void el.offsetWidth; el.classList.add('hit');
    }
  }, 30);
}

function jumpToTransform(name, field, mapping){
  showTab('catalog');
  document.getElementById('catalog-search').value = '';
  renderCatalog();

  // Best match: exact Transformation + Port + Mapping (handles the same
  // instance name being reused with different logic in different mappings).
  let target = catalogRows.find(r => r['Transformation Name'] === name && r._Port === field && r._Mapping === mapping);

  // Fallback: Transformation + Port, ignoring mapping
  if(!target) {
    target = catalogRows.find(r => r['Transformation Name'] === name && r._Port === field);
  }

  // Last resort: name alone
  if(!target) {
     target = catalogRows.find(r => r['Transformation Name'] === name);
  }
  
  const rid = target ? 'tf-' + target.__id : null;
  setTimeout(() => {
    let el = rid && document.getElementById(rid);
    if(!el){
      document.getElementById('catalog-search').value = name;
      renderCatalog();
      el = rid && document.getElementById(rid);
    }
    if(el){
      el.scrollIntoView({behavior:'smooth', block:'center'});
      el.classList.remove('hit'); void el.offsetWidth; el.classList.add('hit');
    }
  }, 30);
}

document.getElementById('lineage-body').addEventListener('click', function(e){
  const hop = e.target.closest('.hop');
  if(!hop) return;
  if(hop.dataset.type === 'Mapplet'){
    jumpToMapplet(hop.dataset.name, hop.dataset.field, hop.dataset.mapping);
  } else {
    jumpToTransform(hop.dataset.name, hop.dataset.field, hop.dataset.mapping);
  }
});

renderLineageHead();
renderLineage();
renderCatalog();
renderMapplets();
</script>
</body>
</html>
""")


_MAPPLETS_TAB_BUTTON_TMPL = (
    '<button class="tab-btn" data-tab="mapplets" onclick="showTab(\'mapplets\')">'
    'Mapplets ({n} rows)</button>'
)

_MAPPLETS_PANEL_TMPL = """<div id="mapplets" class="panel">
  <div class="toolbar">
    <input type="text" id="mapplet-search" placeholder="Filter mapplets..." oninput="renderMapplets()">
    <span class="count" id="mapplet-count"></span>
    <span class="count">Click &#9662; on any column header to filter its values</span>
  </div>
  <div class="tbl-wrap"><table>
    <thead><tr id="mapplet-head"></tr></thead>
    <tbody id="mapplet-body"></tbody>
  </table></div>
</div>"""


def write_html_report(df_lineage, df_catalog, df_mapplet, out_path, title="Lineage Report",
                       has_mapplets=False):
    # Tab-3 (Mapplets) markup is only emitted at all when the repository
    # actually contains Mapplets reachable from this report (improvement-5).
    # When absent, both placeholders resolve to "" so no tab button, no
    # panel, and (via renderMapplets()'s own element-existence guard) no JS
    # error at init time either.
    mapplets_tab_button = (
        _MAPPLETS_TAB_BUTTON_TMPL.format(n=len(df_mapplet)) if has_mapplets else ""
    )
    mapplets_panel = _MAPPLETS_PANEL_TMPL if has_mapplets else ""

    cat = df_catalog.copy()
    # Composite key unique to Transformation Name + Mapping + Port, so that
    # the SAME instance name reused (with different logic) across different
    # mappings gets its own row id instead of colliding.
    cat["__id"] = (
        cat["Transformation Name"].astype(str) + "_" +
        cat["_Mapping"].astype(str) + "_" +
        cat["_Port"].astype(str)
    ).apply(_safe_id)

    mpl = df_mapplet.copy()
    # Composite key unique to Mapping + Mapplet INSTANCE + Output field +
    # Input field, so a mapplet used more than once (different instance
    # names) or reached via more than one input field for the same output
    # field each land on their own row id.
    if "_Mapplet_Instance" not in mpl.columns:
        mpl["_Mapplet_Instance"] = ""
    mpl["__id"] = (
        mpl["Mapping_Name"].astype(str) + "_" +
        mpl["_Mapplet_Instance"].astype(str) + "_" +
        mpl["Output Transformation Field"].astype(str) + "_" +
        mpl["Input Transformation Field"].astype(str)
    ).apply(_safe_id)

    html = _HTML_TEMPLATE.substitute(
        title=title,
        n_lineage=len(df_lineage),
        n_catalog=len(df_catalog),
        n_mapplet=len(df_mapplet),
        mapplets_tab_button=mapplets_tab_button,
        mapplets_panel=mapplets_panel,
        lineage_cols=json.dumps(list(df_lineage.columns)),
        catalog_cols=json.dumps(CATALOG_DISPLAY_COLS),
        mapplet_cols=json.dumps(MAPPLET_CATALOG_DISPLAY_COLS),
        lineage_rows=json.dumps(df_lineage.to_dict(orient="records")),
        catalog_rows=json.dumps(cat.to_dict(orient="records")),
        mapplet_rows=json.dumps(mpl.to_dict(orient="records")),
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


def session_read_write_sets(folder_idx, mapping_name):
    """For a mapping, return:
       writes: {(target_table, target_field): set(instance_names)}
       reads:  {(source_table, source_field): set(instance_names)}
    Purely from INSTANCE/TARGETFIELD/SOURCEFIELD definitions (fast, no tracing).

    Keyed by instance name (not just table/field) so that when the SAME
    physical table is written or read by more than one INSTANCE within a
    single mapping, all contributing instance names are preserved instead
    of being silently collapsed into one anonymous table/field pair.
    """
    mapping_node = folder_idx["mappings"].get(mapping_name)
    if mapping_node is None:
        return {}, {}
    m = index_mapping(mapping_node)
    writes, reads = defaultdict(set), defaultdict(set)
    for inst_name, inst in m["instances"].items():
        if inst["type"] == "TARGET":
            tdef = folder_idx["targets"].get(inst["transformation_name"])
            if tdef:
                for fld in tdef["fields"]:
                    writes[(tdef["table"], fld)].add(inst_name)
        elif inst["type"] == "SOURCE":
            sdef = folder_idx["sources"].get(inst["transformation_name"])
            if sdef:
                for fld in sdef["fields"]:
                    reads[(sdef["table"], fld)].add(inst_name)
    return dict(writes), dict(reads)


def compute_connectors(df, anchor_session, anchor_mapping, anchor_order, anchor_table):
    """Populate a 'Connectors' column on df.

    Starting rows: every row that represents a field write into the
    user-mentioned final target (matched on the exact anchor write event -
    Current_Session_name/Current_Mapping_name/Mapping_Execution_Order/
    Target Table - so we land on the specific target write the user asked
    for, not just any earlier row that happens to share the table name).

    For each such starting row, its own Target Field becomes a label. That
    label is stamped onto the row itself, then propagated backwards along
    the Prev_session_name / Prev_mapping_name / Prev_Mapping_Execution_Order/
    Prev_Target_table_name / Prev_Target_table_attribute pointers: those five
    values identify the predecessor row(s) (rows whose OWN
    Current_Session_name/Current_Mapping_name/Mapping_Execution_Order/
    Target Table/Target Field match them). The label is stamped on every
    matching predecessor row, and the walk continues from there, repeating
    until a row has no predecessor (Prev_session_name is blank).

    Where a single upstream row feeds more than one final target field, its
    Connectors value accumulates every such field label, comma-separated.
    """
    # key -> list of row indices, where key describes a row via the fields
    # that identify it as a Prev_* target of some other row.
    key_to_rows = defaultdict(list)
    for idx, row in df.iterrows():
        key = (row["Current_Session_name"], row["Current_Mapping_name"],
               row["Mapping_Execution_Order"], row["Target Table"], row["Target Field"])
        key_to_rows[key].append(idx)

    connectors = defaultdict(dict)  # idx -> {field_label: None} (ordered set)

    start_mask = (
        (df["Current_Session_name"] == anchor_session) &
        (df["Current_Mapping_name"] == anchor_mapping) &
        (df["Mapping_Execution_Order"] == anchor_order) &
        (df["Target Table"] == anchor_table)
    )
    start_rows = df.index[start_mask].tolist()

    for start_idx in start_rows:
        field_label = df.at[start_idx, "Target Field"]
        if not field_label:
            continue

        visited = set()
        frontier = [start_idx]
        while frontier:
            next_frontier = []
            for idx in frontier:
                if (idx, field_label) in visited:
                    continue
                visited.add((idx, field_label))
                connectors[idx][field_label] = None

                row = df.loc[idx]
                prev_session = row["Prev_session_name"]
                if not prev_session:
                    continue  # no predecessor - end of this chain

                prev_key = (
                    prev_session, row["Prev_mapping_name"],
                    row["Prev_Mapping_Execution_Order"],
                    row["Prev_Target_table_name"], row["Prev_Target_table_attribute"],
                )
                for p_idx in key_to_rows.get(prev_key, []):
                    if (p_idx, field_label) not in visited:
                        next_frontier.append(p_idx)
            frontier = next_frontier

    df["Connectors"] = [
        ", ".join(connectors[i].keys()) if i in connectors else ""
        for i in df.index
    ]
    return df


def find_target_instance(folder_idx, target_instance_name):
    """Search all mappings for a TARGET-type INSTANCE with this name.
    Returns list of (mapping_name, target_def_name)."""
    matches = []
    for mname, mnode in folder_idx["mappings"].items():
        for ch in mnode["children"]:
            if ch["tag"] == "INSTANCE":
                a = ch["attributes"]
                if a.get("TYPE") == "TARGET" and a.get("NAME") == target_instance_name:
                    matches.append((mname, a.get("TRANSFORMATION_NAME")))
    return matches


def build_lineage(json_path, target_instance_name, workflow_name=None, out_prefix="lineage",
                   out_dir=None):
    data = load_repo(json_path)
    folders = get_folders(data)
    if not folders:
        raise SystemExit("No FOLDER found in repository.")

    # use the first folder (extend to loop over all folders if needed)
    folder = folders[0]
    folder_idx = index_folder(folder)

    workflows = folder_idx["workflows"]
    if workflow_name:
        wf_nodes = {workflow_name: workflows[workflow_name]}
    else:
        wf_nodes = workflows

    # combine session order across all workflows in this folder (each gets
    # its own local order numbering merged into one global sequence, in the
    # order the workflows appear)
    session_to_mapping = {}
    session_order = {}
    session_workflow = {}
    counter_offset = 0
    for wf_name, wf_node in wf_nodes.items():
        s2m, sorder, olist = build_session_order(wf_node)
        for s, o in sorder.items():
            session_order[s] = o + counter_offset
            session_to_mapping[s] = s2m[s]
            session_workflow[s] = wf_name
        counter_offset += len(sorder)

    # locate target instance
    matches = find_target_instance(folder_idx, target_instance_name)
    if not matches:
        raise SystemExit(f"Target instance '{target_instance_name}' not found in any mapping.")

    candidate_orders = []
    for mname, tdefname in matches:
        sessions_using = [s for s, mp in session_to_mapping.items() if mp == mname]
        for s in sessions_using:
            candidate_orders.append((session_order[s], s, mname, tdefname))

    if not candidate_orders:
        raise SystemExit(
            f"Target instance '{target_instance_name}' found in mapping(s) "
            f"{[m for m, _ in matches]} but none of those mappings are run by any session."
        )

    candidate_orders.sort()
    anchor_order, anchor_session, anchor_mapping, anchor_tdefname = candidate_orders[0]

    print(f"Target instance '{target_instance_name}' -> mapping '{anchor_mapping}', "
          f"session '{anchor_session}' (execution order {anchor_order})")
    if len(candidate_orders) > 1:
        print("  Note: multiple sessions run this mapping/instance:")
        for o, s, mp, td in candidate_orders:
            print(f"    order={o} session={s} mapping={mp}")

    # sessions in scope: 1 .. anchor_order
    in_scope = sorted([(o, s, mp) for s, mp in session_to_mapping.items()
                        for o in [session_order[s]] if o <= anchor_order])

    # precompute read/write sets for every session IN SCOPE (needed for
    # cross-session prev/next stitching)
    rw_cache = {}
    for o, s, mp in in_scope:
        rw_cache[s] = session_read_write_sets(folder_idx, mp)

    writers_of = defaultdict(list)
    readers_of = defaultdict(list)
    for o, s, mp in in_scope:
        writes, reads = rw_cache[s]
        for tf, inst_names in writes.items():
            writers_of[tf].append((o, s, mp, tuple(sorted(inst_names))))
        for tf, inst_names in reads.items():
            readers_of[tf].append((o, s, mp, tuple(sorted(inst_names))))
    for k in writers_of:
        writers_of[k].sort()
    for k in readers_of:
        readers_of[k].sort()

    rows = []
    edge_id = 0

    for o, session_name, mapping_name in in_scope:
        mapping_node = folder_idx["mappings"].get(mapping_name)
        if mapping_node is None:
            continue
        tracer = MappingTracer(folder_idx, mapping_name)

        # every TARGET instance in this mapping
        for inst_name, inst in tracer.m["instances"].items():
            if inst["type"] != "TARGET":
                continue
            tdef = folder_idx["targets"].get(inst["transformation_name"])
            if not tdef:
                continue
            target_table = tdef["table"]

            for field in tdef["fields"]:
                leaves = tracer.trace_field(inst_name, field)
                # de-duplicate identical leaves (same source+path) for this field
                seen = set()
                for leaf in leaves:
                    dedup_key = (leaf.get("source_instance"), leaf.get("source_table"),
                                 leaf.get("source_field"), tuple(leaf["path"]))
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)

                    src_table = leaf.get("source_table")
                    src_field = leaf.get("source_field")

                    prev_session = prev_mapping = prev_order = prev_table = prev_field = ""
                    prev_instance = ""
                    if src_table and src_field:
                        cands = [w for w in writers_of.get((src_table, src_field), []) if w[0] < o]
                        if cands:
                            p_o, p_s, p_mp, p_insts = cands[-1]  # closest predecessor
                            prev_session, prev_mapping, prev_order = p_s, p_mp, p_o
                            prev_table, prev_field = src_table, src_field
                            prev_instance = ", ".join(p_insts)

                    next_session = next_mapping = next_order = next_table = next_field = ""
                    next_instance = ""
                    cands = [r for r in readers_of.get((target_table, field), []) if r[0] > o]
                    if cands:
                        n_o, n_s, n_mp, n_insts = cands[0]  # closest successor
                        next_session, next_mapping, next_order = n_s, n_mp, n_o
                        next_table, next_field = target_table, field
                        next_instance = ", ".join(n_insts)

                    same_table_instances = sorted(
                        {n for o2, s2, mp2, insts in writers_of.get((target_table, field), [])
                         if mp2 == mapping_name for n in insts}
                    )
                    target_instance_ambiguous = "Yes" if len(same_table_instances) > 1 else "No"

                    edge_id += 1
                    rows.append({
                        "Edge_ID": edge_id,
                        "Current_Session_name": session_name,
                        "Current_Mapping_name": mapping_name,
                        "Mapping_Execution_Order": o,
                        "Source_Instance_Name": leaf.get("source_instance") or "",
                        "Source Table": src_table or "",
                        "Source Field Name": src_field or "",
                        "Transformation Lineage": format_lineage_chain(leaf["path"]),
                        "Transformation Lineage Data": format_lineage_chain_data(leaf["path"], mapping_name), # Feeds HTML clicks
                        "Target_Instance_Name": inst_name,
                        "Target Table": target_table,
                        "Target Field": field,
                        "Target_Table_Multi_Instance": target_instance_ambiguous,
                        "Target_Table_Instance_Names": ", ".join(same_table_instances),
                        "Prev_session_name": prev_session,
                        "Prev_mapping_name": prev_mapping,
                        "Prev_Mapping_Execution_Order": prev_order,
                        "Prev_Target_table_name": prev_table,
                        "Prev_Target_table_attribute": prev_field,
                        "Prev_Target_Instance_Name": prev_instance,
                        "next_session_name": next_session,
                        "next_mapping_name": next_mapping,
                        "Next_Mapping_Execution_Order": next_order,
                        "next_Source_table_name": next_table,
                        "next_Source_table_attribute": next_field,
                        "next_Source_Instance_Name": next_instance,
                    })

    df = pd.DataFrame(rows)
    # Included Transformation Lineage Data to feed JS, but user will only physically see `Transformation Lineage` strings.
    col_order = [
        "Edge_ID", "Current_Session_name", "Current_Mapping_name", "Mapping_Execution_Order",
        "Source_Instance_Name", "Source Table", "Source Field Name",
        "Transformation Lineage", "Transformation Lineage Data",
        "Target_Instance_Name", "Target Table", "Target Field",
        "Target_Table_Multi_Instance", "Target_Table_Instance_Names",
        "Prev_session_name", "Prev_mapping_name", "Prev_Mapping_Execution_Order",
        "Prev_Target_table_name", "Prev_Target_table_attribute", "Prev_Target_Instance_Name",
        "next_session_name", "next_mapping_name", "Next_Mapping_Execution_Order",
        "next_Source_table_name", "next_Source_table_attribute", "next_Source_Instance_Name",
    ]
    df_out = df[col_order].drop_duplicates().reset_index(drop=True)
    df_out["Edge_ID"] = range(1, len(df_out) + 1)

    # Tab-1 last column: Connectors - traces every field of the user-mentioned
    # final target backwards through its predecessor chain, stamping the
    # final target field's name onto every upstream row that feeds it.
    anchor_table = folder_idx["targets"].get(anchor_tdefname, {}).get("table", "")
    df_out = compute_connectors(df_out, anchor_session, anchor_mapping, anchor_order, anchor_table)

    # Tab-2: field-level transformation catalog
    # df_catalog carries hidden _Mapping/_Port bookkeeping columns (needed
    # for unambiguous HTML click-to-navigate); df_catalog_display is the
    # user-facing 7-column view used for Excel/CSV.
    #
    # The SAME instance name legitimately recurs, with IDENTICAL logic,
    # across many different mappings (a generic "EXP_DATA" pass-through
    # port, say) - the engine-level dedup keys on the bookkeeping
    # _Mapping/_Port columns too, so those still come through as distinct
    # rows there. From the user's point of view looking at the exported
    # table, though, two rows that are identical in every VISIBLE column
    # are just noise (improvement-3), so drop_duplicates on the display
    # columns alone, keeping the first (bookkeeping-bearing) occurrence for
    # HTML click-to-navigate.
    df_catalog_full = pd.DataFrame(build_transformation_catalog(folder_idx))
    df_catalog = df_catalog_full.drop_duplicates(subset=CATALOG_DISPLAY_COLS).reset_index(drop=True)
    df_catalog_display = df_catalog[CATALOG_DISPLAY_COLS]

    # Tab-3: mapplet boundary-to-boundary field lineage (see requirement
    # item (d)). df_mapplet carries a hidden _Mapplet_Instance bookkeeping
    # column (mirrors df_catalog's _Mapping/_Port pattern) so the HTML
    # report can jump straight from a Tab-1 "...[Mapplet]" hop to its
    # matching Tab-3 row(s); df_mapplet_display is the user-facing 9-column
    # view used for Excel/CSV. Only populated when the repository actually
    # contains any Mapplets reachable from this report (improvement-5) -
    # an empty mapplet_rows means Tab-3 is entirely omitted downstream.
    mapplet_rows = build_mapplet_catalog(folder_idx)
    if mapplet_rows:
        df_mapplet_full = pd.DataFrame(mapplet_rows)
        df_mapplet = df_mapplet_full.drop_duplicates(subset=MAPPLET_CATALOG_DISPLAY_COLS).reset_index(drop=True)
    else:
        df_mapplet = pd.DataFrame(columns=MAPPLET_CATALOG_DISPLAY_COLS + ["_Mapplet_Instance"])
    df_mapplet_display = df_mapplet[MAPPLET_CATALOG_DISPLAY_COLS]
    has_mapplets = len(df_mapplet) > 0

    if out_dir:
        resolved_out_dir = out_dir
    elif os.path.isdir("/mnt/user-data/outputs"):
        resolved_out_dir = "/mnt/user-data/outputs"
    else:
        resolved_out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

    os.makedirs(resolved_out_dir, exist_ok=True)

    csv_path = os.path.join(resolved_out_dir, f"{out_prefix}.csv")
    xlsx_path = os.path.join(resolved_out_dir, f"{out_prefix}.xlsx")
    html_path = os.path.join(resolved_out_dir, f"{out_prefix}.html")

    # To avoid displaying raw JSON data within Excel, we drop `Transformation Lineage Data` locally
    df_excel_out = df_out.drop(columns=["Transformation Lineage Data"])
    
    df_excel_out.to_csv(csv_path, index=False)
    try:
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            df_excel_out.to_excel(writer, sheet_name="Lineage", index=False)
            df_catalog_display.to_excel(writer, sheet_name="Transformations", index=False)
            if has_mapplets:
                df_mapplet_display.to_excel(writer, sheet_name="Mapplets", index=False)
    except Exception as e:
        print("xlsx export skipped:", e)

    html_written = False
    try:
        write_html_report(df_out, df_catalog, df_mapplet, html_path,
                           title=f"Lineage: {target_instance_name}", has_mapplets=has_mapplets)
        html_written = True
    except Exception as e:
        print("html export skipped:", e)

    print(f"\nRows produced: {len(df_out)}")
    print(f"Transformations catalogued (Port-level, deduplicated): {len(df_catalog)}")
    print(f"Mapplet field paths catalogued (deduplicated): {len(df_mapplet)}")
    print(f"Sessions in scope (1..{anchor_order}): {len(in_scope)}")
    print(f"Wrote: {csv_path}")
    print(f"Wrote: {xlsx_path}")
    if html_written:
        print(f"Wrote: {html_path}")
    return df_out, df_catalog, df_mapplet


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("target_instance_name")
    ap.add_argument("--workflow", default=None)
    ap.add_argument("--out", default="lineage", help="Output filename prefix")
    ap.add_argument("--outdir", default=None,
                     help="Directory to write outputs into. Created automatically if missing.")
    args = ap.parse_args()
    build_lineage(args.json_path, args.target_instance_name, args.workflow, args.out, args.outdir)