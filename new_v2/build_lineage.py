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
    format_lineage_chain, build_transformation_catalog,
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
</style>
</head>
<body>
<header>
  <h1>$title</h1>
  <p>Tab 1: field-level lineage. Tab 2: transformation catalog. Click any hop in a
     Transformation Lineage chain to jump to its row in the catalog.</p>
</header>
<div class="tabs">
  <button class="tab-btn active" data-tab="lineage" onclick="showTab('lineage')">Lineage ($n_lineage rows)</button>
  <button class="tab-btn" data-tab="catalog" onclick="showTab('catalog')">Transformations ($n_catalog rows)</button>
</div>

<div id="lineage" class="panel active">
  <div class="toolbar">
    <input type="text" id="lineage-search" placeholder="Filter lineage rows..." oninput="renderLineage()">
    <span class="count" id="lineage-count"></span>
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
  </div>
  <div class="tbl-wrap"><table>
    <thead><tr id="catalog-head"></tr></thead>
    <tbody id="catalog-body"></tbody>
  </table></div>
</div>

<script>
const LINEAGE_COLS = $lineage_cols;
const CATALOG_COLS = $catalog_cols;
const lineageRows = $lineage_rows;
const catalogRows = $catalog_rows;
const PAGE_SIZE = 100;
let lineagePageNum = 0;

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

function hopCellHtml(chainStr){
  if(!chainStr) return '';
  const parts = chainStr.split(' -> ');
  return parts.map(p => {
    const m = p.match(/^(.*)\[(.*)\]$$/);
    const name = m ? m[1] : p;
    return '<span class="hop" data-name="' + escapeHtml(name) + '">' + escapeHtml(p) + '</span>';
  }).join('<span class="arrow">&rarr;</span>');
}

function filterRows(rows, cols, query){
  if(!query) return rows;
  const q = query.toLowerCase();
  return rows.filter(r => cols.some(c => String(r[c] ?? '').toLowerCase().includes(q)));
}

function renderLineage(){
  const q = document.getElementById('lineage-search').value;
  const filtered = filterRows(lineageRows, LINEAGE_COLS, q);
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  if(lineagePageNum >= totalPages) lineagePageNum = totalPages - 1;
  if(lineagePageNum < 0) lineagePageNum = 0;
  const start = lineagePageNum * PAGE_SIZE;
  const pageRows = filtered.slice(start, start + PAGE_SIZE);

  renderHead(document.getElementById('lineage-head'), LINEAGE_COLS);
  document.getElementById('lineage-body').innerHTML = pageRows.map(r => {
    return '<tr>' + LINEAGE_COLS.map(c => {
      if(c === 'Transformation Lineage') return '<td>' + hopCellHtml(r[c]) + '</td>';
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
  const filtered = filterRows(catalogRows, CATALOG_COLS, q);
  renderHead(document.getElementById('catalog-head'), CATALOG_COLS);
  document.getElementById('catalog-body').innerHTML = filtered.map(r => {
    const rid = 'tf-' + r.__id;
    return '<tr id="' + rid + '" class="id-row">' + CATALOG_COLS.map(c =>
      '<td>' + escapeHtml(r[c] ?? '') + '</td>'
    ).join('') + '</tr>';
  }).join('');
  document.getElementById('catalog-count').textContent = filtered.length + ' matching row(s)';
}

function jumpToTransform(name){
  showTab('catalog');
  document.getElementById('catalog-search').value = '';
  renderCatalog();
  const target = catalogRows.find(r => r['Transformation Name'] === name);
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
  if(hop) jumpToTransform(hop.dataset.name);
});

renderLineage();
renderCatalog();
</script>
</body>
</html>
""")


def write_html_report(df_lineage, df_catalog, out_path, title="Lineage Report"):
    cat = df_catalog.copy()
    cat["__id"] = cat["Transformation Name"].map(_safe_id)

    html = _HTML_TEMPLATE.substitute(
        title=title,
        n_lineage=len(df_lineage),
        n_catalog=len(df_catalog),
        lineage_cols=json.dumps(list(df_lineage.columns)),
        catalog_cols=json.dumps(["Transformation Name", "Transformation Type",
                                  "Business Logic", "Ports"]),
        lineage_rows=json.dumps(df_lineage.to_dict(orient="records")),
        catalog_rows=json.dumps(cat.to_dict(orient="records")),
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

    # index: which session (in scope) WRITES a given (table, field) -> list of
    # (order, session, mapping, instance_names). instance_names is a sorted
    # tuple of every INSTANCE (within that session's mapping) that writes/reads
    # this table+field -- if it has more than one entry, that table+field is
    # ambiguous (multiple instances in the same mapping touch the same
    # physical table/field) and every candidate is surfaced rather than
    # silently picking one.
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
                            # more than one instance in the predecessor mapping
                            # writes this same table+field -> surface all of
                            # them instead of guessing which one is "the" source
                            prev_instance = ", ".join(p_insts)

                    next_session = next_mapping = next_order = next_table = next_field = ""
                    next_instance = ""
                    cands = [r for r in readers_of.get((target_table, field), []) if r[0] > o]
                    if cands:
                        n_o, n_s, n_mp, n_insts = cands[0]  # closest successor
                        next_session, next_mapping, next_order = n_s, n_mp, n_o
                        next_table, next_field = target_table, field
                        next_instance = ", ".join(n_insts)

                    # how many TARGET instances in *this* mapping write to this
                    # same (target_table, field) -- flags same-table/multi-instance
                    # ambiguity happening within the current mapping itself
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
    col_order = [
        "Edge_ID", "Current_Session_name", "Current_Mapping_name", "Mapping_Execution_Order",
        "Source_Instance_Name", "Source Table", "Source Field Name",
        "Transformation Lineage",
        "Target_Instance_Name", "Target Table", "Target Field",
        "Target_Table_Multi_Instance", "Target_Table_Instance_Names",
        "Prev_session_name", "Prev_mapping_name", "Prev_Mapping_Execution_Order",
        "Prev_Target_table_name", "Prev_Target_table_attribute", "Prev_Target_Instance_Name",
        "next_session_name", "next_mapping_name", "Next_Mapping_Execution_Order",
        "next_Source_table_name", "next_Source_table_attribute", "next_Source_Instance_Name",
    ]
    df_out = df[col_order].drop_duplicates().reset_index(drop=True)
    df_out["Edge_ID"] = range(1, len(df_out) + 1)

    # Tab-2: transformation catalog for the whole uploaded JSON (folder-wide,
    # not scoped to the anchor run) - dedup by transformation name.
    df_catalog = pd.DataFrame(build_transformation_catalog(folder_idx))

    # Resolve the output directory. `/mnt/user-data/outputs` only exists inside
    # Claude's sandbox -- on a normal machine (Windows, Mac, Linux) it doesn't
    # exist, which is exactly what caused the OSError. Instead:
    #   1. use --outdir if the user passed one
    #   2. otherwise use that sandbox path IF it's actually present
    #   3. otherwise fall back to an "output" folder next to this script
    # and always create the directory if it's missing.
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

    df_out.to_csv(csv_path, index=False)
    try:
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            df_out.to_excel(writer, sheet_name="Lineage", index=False)
            df_catalog.to_excel(writer, sheet_name="Transformations", index=False)
    except Exception as e:
        print("xlsx export skipped:", e)

    try:
        write_html_report(df_out, df_catalog, html_path,
                           title=f"Lineage: {target_instance_name}")
    except Exception as e:
        print("html export skipped:", e)

    print(f"\nRows produced: {len(df_out)}")
    print(f"Transformations catalogued: {len(df_catalog)}")
    print(f"Sessions in scope (1..{anchor_order}): {len(in_scope)}")
    print(f"Wrote: {csv_path}")
    print(f"Wrote: {xlsx_path}")
    print(f"Wrote: {html_path}")
    return df_out, df_catalog


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("target_instance_name")
    ap.add_argument("--workflow", default=None)
    ap.add_argument("--out", default="lineage", help="Output filename prefix (e.g. 'lineage' -> lineage.csv/.xlsx/.html)")
    ap.add_argument("--outdir", default=None,
                     help="Directory to write outputs into. Defaults to an 'output' "
                          "folder next to this script (or /mnt/user-data/outputs if that "
                          "sandbox path exists). Created automatically if missing.")
    args = ap.parse_args()
    build_lineage(args.json_path, args.target_instance_name, args.workflow, args.out, args.outdir)
