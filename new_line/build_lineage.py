import sys
import json
import argparse
import pandas as pd
from collections import defaultdict

from lineage_engine import (
    load_repo, get_folders, index_folder, index_mapping,
    build_session_order, MappingTracer,
)


def session_read_write_sets(folder_idx, mapping_name):
    """For a mapping, return:
       writes: set of (target_table, target_field)
       reads:  set of (source_table, source_field)
    Purely from INSTANCE/TARGETFIELD/SOURCEFIELD definitions (fast, no tracing).
    """
    mapping_node = folder_idx["mappings"].get(mapping_name)
    if mapping_node is None:
        return set(), set()
    m = index_mapping(mapping_node)
    writes, reads = set(), set()
    for inst_name, inst in m["instances"].items():
        if inst["type"] == "TARGET":
            tdef = folder_idx["targets"].get(inst["transformation_name"])
            if tdef:
                for fld in tdef["fields"]:
                    writes.add((tdef["table"], fld))
        elif inst["type"] == "SOURCE":
            sdef = folder_idx["sources"].get(inst["transformation_name"])
            if sdef:
                for fld in sdef["fields"]:
                    reads.add((sdef["table"], fld))
    return writes, reads


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


def build_lineage(json_path, target_instance_name, workflow_name=None, out_prefix="lineage"):
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

    # index: which session (in scope) WRITES a given (table, field) -> list of (order, session, mapping)
    writers_of = defaultdict(list)
    readers_of = defaultdict(list)
    for o, s, mp in in_scope:
        writes, reads = rw_cache[s]
        for tf in writes:
            writers_of[tf].append((o, s, mp))
        for tf in reads:
            readers_of[tf].append((o, s, mp))
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
                    if src_table and src_field:
                        cands = [w for w in writers_of.get((src_table, src_field), []) if w[0] < o]
                        if cands:
                            p_o, p_s, p_mp = cands[-1]  # closest predecessor
                            prev_session, prev_mapping, prev_order = p_s, p_mp, p_o
                            prev_table, prev_field = src_table, src_field

                    next_session = next_mapping = next_order = next_table = next_field = ""
                    cands = [r for r in readers_of.get((target_table, field), []) if r[0] > o]
                    if cands:
                        n_o, n_s, n_mp = cands[0]  # closest successor
                        next_session, next_mapping, next_order = n_s, n_mp, n_o
                        next_table, next_field = target_table, field

                    edge_id += 1
                    rows.append({
                        "Edge_ID": edge_id,
                        "Current_Session_name": session_name,
                        "Current_Mapping_name": mapping_name,
                        "Mapping_Execution_Order": o,
                        "Source_Instance_Name": leaf.get("source_instance") or "",
                        "Source Table": src_table or "",
                        "Source Field Name": src_field or "",
                        "Transformation (1 to N column)": " -> ".join(leaf["path"]),
                        "Target_Instance_Name": inst_name,
                        "Target Table": target_table,
                        "Target Field": field,
                        "Prev_session_name": prev_session,
                        "Prev_mapping_name": prev_mapping,
                        "Prev_Mapping_Execution_Order": prev_order,
                        "Prev_Target_table_name": prev_table,
                        "Prev_Target_table_attribute": prev_field,
                        "next_session_name": next_session,
                        "next_mapping_name": next_mapping,
                        "Next_Mapping_Execution_Order": next_order,
                        "next_Source_table_name": next_table,
                        "next_Source_table_attribute": next_field,
                    })

    df = pd.DataFrame(rows)
    col_order = [
        "Edge_ID", "Current_Session_name", "Current_Mapping_name", "Mapping_Execution_Order",
        "Source_Instance_Name", "Source Table", "Source Field Name",
        "Transformation (1 to N column)",
        "Target_Instance_Name", "Target Table", "Target Field",
        "Prev_session_name", "Prev_mapping_name", "Prev_Mapping_Execution_Order",
        "Prev_Target_table_name", "Prev_Target_table_attribute",
        "next_session_name", "next_mapping_name", "Next_Mapping_Execution_Order",
        "next_Source_table_name", "next_Source_table_attribute",
    ]
    df_out = df[col_order].drop_duplicates().reset_index(drop=True)
    df_out["Edge_ID"] = range(1, len(df_out) + 1)

    csv_path = f"/mnt/user-data/outputs/{out_prefix}.csv"
    xlsx_path = f"/mnt/user-data/outputs/{out_prefix}.xlsx"
    df_out.to_csv(csv_path, index=False)
    try:
        df_out.to_excel(xlsx_path, index=False)
    except Exception as e:
        print("xlsx export skipped:", e)

    print(f"\nRows produced: {len(df_out)}")
    print(f"Sessions in scope (1..{anchor_order}): {len(in_scope)}")
    print(f"Wrote: {csv_path}")
    return df_out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("json_path")
    ap.add_argument("target_instance_name")
    ap.add_argument("--workflow", default=None)
    ap.add_argument("--out", default="lineage")
    args = ap.parse_args()
    build_lineage(args.json_path, args.target_instance_name, args.workflow, args.out)
