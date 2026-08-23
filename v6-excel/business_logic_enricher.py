"""
business_logic_enricher.py

Fills the "Business Logic (full logic)", "Additional Informations" and
"Category" columns of the Transformations (Tab-2) and Mapplet_Transformations
(Tab-4) catalogs that build_lineage.py / lineage_engine.py already produce,
using values looked up in a SEPARATE, user-supplied Excel workbook.

That workbook is expected to have ONE TAB PER TRANSFORMATION TYPE, split
between mapping-scoped tabs and their mapplet-scoped counterparts (the
mapplet tab is generally "Mapplet_<Type>", though a couple deviate - see
_TYPE_TAB_CANDIDATES):
    Mapping tabs:  Expression, Filter, Joiner, Lookup Procedure, Router,
                   Sequence, Sorter, Source Qualifier, Stored Procedure,
                   Transaction Control, Update Strategy, Aggregator, Rank,
                   Custom Transformation
    Mapplet tabs:  Mapplet_Expression, Mapplet_Filter, Mapplet_Joiner,
                   Mapplet_Lookup, Mapplet_Router, Mapplet_Sequne gen,
                   Mapplet_Sorter, Mapplet_Source Qualifier,
                   Mapplet_Stored Procedure, Mapplet_Transaction Control,
                   Mapplet_Update Strategy, Mapplet_Aggregator,
                   Mapplet_Rank, Mapplet_Custom Transformation
A Tab-2 (Transformations) row is only ever matched against a mapping tab,
and a Tab-4 (Mapplet_Transformations) row only ever against a mapplet tab -
mapplet_mode (see enrich_catalog_rows) controls which side is searched, so
e.g. a plain "Expression" lookup can never accidentally resolve to
"Mapplet_Expression" or vice versa.

Each tab is expected to carry (at least) these identifying columns, plus
whatever attribute columns are relevant to that transformation type:
    Session Name, Mapping Name (or Mapplet Name), Transformation Name,
    PORT_NAME

Matching is case/whitespace/punctuation-insensitive on column headers AND on
the identifying values, so small naming differences ("Session" vs
"Session Name", "Mapping_Name" vs "Mapping Name") don't cause a miss.

USAGE
-----
As a library (preferred - this is how build_lineage.py hooks in):

    from business_logic_enricher import load_source_workbook, enrich_catalog_rows

    workbook = load_source_workbook("business_logic_source.xlsx")
    enrich_catalog_rows(catalog_rows, workbook, mapping_key="_Mapping",
                         mapplet_mode=False, session_lookup=mapping_to_session)

As a standalone script (thin wrapper around build_lineage.build_lineage):

    python business_logic_enricher.py <json_path> <target_table> \
        <target_instance_name> <target_mapping> <business_logic_source.xlsx> \
        [--workflow NAME] [--out lineage] [--outdir OUTDIR]

This module does not change anything if it is never called - build_lineage.py
only invokes it when a --business-logic-excel path is supplied, so lineage
generation behaves exactly as before when that option is omitted.
"""

import re
import argparse
from collections import defaultdict

import pandas as pd


# --------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------

def _norm(s):
    """Case/space/punctuation-insensitive normalization used for BOTH column
    header matching and identifying-value matching (Session/Mapping/
    Mapplet/Transformation/PORT_NAME)."""
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _find_col(cols, aliases):
    normcols = {_norm(c): c for c in cols}
    for a in aliases:
        na = _norm(a)
        if na in normcols:
            return normcols[na]
    return None


# --------------------------------------------------------------------------
# Loading the user-supplied workbook
# --------------------------------------------------------------------------

def load_source_workbook(path):
    """Reads every tab of the user-supplied Excel into {sheet_name: DataFrame}."""
    return pd.read_excel(path, sheet_name=None, dtype=str)


# Anchor keyword (matched against Transformation Type from the repo) ->
# candidate substrings to look for among the workbook's tab names, once the
# sheet name has had any leading "Mapplet" prefix stripped off (see
# _match_sheet_name). A couple of tab names deviate from the obvious
# "Mapplet_<Type>" pattern (e.g. Lookup Procedure's mapplet tab is just
# "Mapplet_Lookup", not "Mapplet_Lookup Procedure"; Sequence's mapplet tab
# is the typo'd "Mapplet_Sequne gen") - those alternate spellings are
# included as extra candidates so both still resolve correctly.
_TYPE_TAB_CANDIDATES = [
    ("SOURCE QUALIFIER", ["source qualifier"]),
    ("STORED PROCEDURE", ["stored procedure"]),
    ("TRANSACTION", ["transaction control", "transaction"]),
    ("UPDATE STRATEGY", ["update strategy"]),
    ("LOOKUP", ["lookup procedure", "lookup"]),
    ("AGGREGATOR", ["aggregator"]),
    ("EXPRESSION", ["expression"]),
    ("FILTER", ["filter"]),
    ("JOINER", ["joiner"]),
    ("ROUTER", ["router"]),
    ("SEQUENCE", ["sequence", "sequne gen", "sequence generator", "seq gen"]),
    ("SORTER", ["sorter"]),
    ("RANK", ["rank"]),
    ("CUSTOM", ["custom transformation", "custom"]),
]

_MAPPLET_PREFIX = _norm("Mapplet")


def _match_sheet_name(workbook, ttype, mapplet_mode):
    """Finds the workbook tab for this transformation type, honoring
    mapping-vs-mapplet scope so a mapping-mode lookup never lands on a
    "Mapplet_..." tab (and vice versa) - e.g. an EXPRESSION type search in
    mapplet_mode=True can only match "Mapplet_Expression", never the plain
    "Expression" tab, even though "expression" is a substring match for
    both once normalized."""
    t = (ttype or "").upper()
    for anchor, candidates in _TYPE_TAB_CANDIDATES:
        if anchor in t or (anchor == "SOURCE QUALIFIER" and t == "SQ"):
            for sheet_name in workbook.keys():
                sn = _norm(sheet_name)
                is_mapplet_sheet = sn.startswith(_MAPPLET_PREFIX)
                if is_mapplet_sheet != mapplet_mode:
                    continue
                sn_core = sn[len(_MAPPLET_PREFIX):] if is_mapplet_sheet else sn
                for cand in candidates:
                    nc = _norm(cand)
                    if nc in sn_core or sn_core in nc:
                        return sheet_name
    return None


# --------------------------------------------------------------------------
# Per-sheet lookup index: (session, mapping/mapplet, transformation, port)
# -> list of matched rows (normalized-header -> value dict)
# --------------------------------------------------------------------------

def _build_sheet_index(df):
    cols = list(df.columns)
    session_col = _find_col(cols, ["Session Name", "Session"])
    mapping_col = _find_col(cols, ["Mapping Name", "Mapping"])
    mapplet_col = _find_col(cols, ["Mapplet Name", "Mapplet"])
    trans_col = _find_col(cols, ["Transformation Name", "Transformation", "Instance Name"])
    port_col = _find_col(cols, ["PORT_NAME", "Port Name", "Port"])

    index = defaultdict(list)
    if not trans_col or not port_col:
        return index  # sheet doesn't even carry the minimum identifying columns

    def val(rowd, col):
        if not col:
            return ""
        v = rowd.get(col, "")
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        return str(v).strip()

    for _, r in df.iterrows():
        rowd = r.to_dict()
        session_v = _norm(val(rowd, session_col))
        mapping_v = _norm(val(rowd, mapping_col))
        mapplet_v = _norm(val(rowd, mapplet_col))
        trans_v = _norm(val(rowd, trans_col))
        port_v = _norm(val(rowd, port_col))
        if not trans_v or not port_v:
            continue

        normmap = {}
        for k, v in rowd.items():
            if v is None or (isinstance(v, float) and pd.isna(v)):
                continue
            sv = str(v).strip()
            if sv:
                normmap[_norm(k)] = sv

        scopes = {s for s in (mapping_v, mapplet_v) if s}
        if not scopes:
            # Sheet has no Mapping/Mapplet column at all - fall back to
            # matching on Transformation + Port alone as a last resort.
            index[("bare", trans_v, port_v)].append(normmap)
            continue
        for scope_v in scopes:
            index[("full", session_v, scope_v, trans_v, port_v)].append(normmap)
            index[("loose", scope_v, trans_v, port_v)].append(normmap)

    return index


def _lookup(index, session_v, scope_v, trans_v, port_v):
    rows = index.get(("full", session_v, scope_v, trans_v, port_v))
    if rows:
        return rows
    rows = index.get(("loose", scope_v, trans_v, port_v))
    if rows:
        return rows
    rows = index.get(("bare", trans_v, port_v))
    if rows:
        return rows
    return []


# --------------------------------------------------------------------------
# Per-transformation-type Business Logic / Additional Informations rules
# --------------------------------------------------------------------------

def _attr(matched_rows, *aliases):
    """First non-blank value for any of `aliases`, searched across every
    matched Excel row (there can be more than one, e.g. Stored Procedure's
    tall Property Name/Property Value rows)."""
    keys = [_norm(a) for a in aliases]
    for normmap in matched_rows:
        for k in keys:
            v = normmap.get(k)
            if v:
                return v
    return ""


def _compute_business_fields(ttype, matched_rows):
    """Returns (business_logic, additional_informations) per the spec, given
    the Excel row(s) matched for this (session, mapping/mapplet,
    transformation, port). Returns ("", "") when nothing was found in the
    workbook for this row (row's existing value, if any, is left alone by
    the caller)."""
    if not matched_rows:
        return "", ""

    t = (ttype or "").upper()

    def attr(*names):
        return _attr(matched_rows, *names)

    if "EXPRESSION" in t and "AGGREGATOR" not in t:
        # Per spec: "Expression" header under the Expression /
        # Mapplet_Expression tab.
        return attr("Expression"), ""

    if "AGGREGATOR" in t:
        # Per spec: "expression" header under the Aggregator /
        # Mapplet_Aggregator tab.
        return attr("Expression", "Aggregator Expression"), ""

    if "FILTER" in t:
        return attr("Filter Condition"), ""

    if "JOINER" in t:
        return attr("Join Condition"), attr("Join Type")

    if "LOOKUP" in t:
        cond = attr("Lookup Condition")
        sql = attr("Lookup Sql Override", "Lookup Sql Overide")
        table = attr("Lookup Table Name")
        parts = []
        if cond:
            parts.append(f"Lookup Condition: {cond}")
        if sql:
            parts.append(f"Lookup Sql Override: {sql}")
        if table:
            parts.append(f"Lookup Table Name: {table}")
        return "\n".join(parts), attr("Connection Information")

    if "ROUTER" in t:
        grp = attr("Group Name")
        cond = attr("Group Filter Condition", "Filter Condition", "Condition")
        parts = []
        if grp:
            parts.append(f"Group Name: {grp}")
        if cond:
            parts.append(f"Group Filter Condition: {cond}")
        return "\n".join(parts), ""

    if "SEQUENCE" in t:
        biz = attr("Current Value")
        addl_parts = []
        for label in ("Start Value", "Increment Value", "End Value"):
            v = attr(label)
            if v:
                addl_parts.append(f"{label}: {v}")
        return biz, "; ".join(addl_parts)

    if "SORTER" in t:
        return attr("Sort Direction"), attr("Transformation Scope")

    if "SOURCE QUALIFIER" in t or t == "SQ":
        biz = attr("SQL Query", "Sql Override")
        addl_parts = []
        uj = attr("User Defined Join")
        sf = attr("Source Filter")
        assoc = attr("Associated Source Definitions", "Associated Source Instance", "Source Table Name")
        if uj:
            addl_parts.append(f"User Defined Join: {uj}")
        if sf:
            addl_parts.append(f"Source Filter: {sf}")
        if assoc:
            addl_parts.append(f"Associated source definitions: {assoc}")
        return biz, "; ".join(addl_parts)

    if "STORED PROCEDURE" in t:
        # This tab is laid out "tall": one row per (Property_name,
        # Property_VALUE) pair rather than one column per property. Per
        # spec, Business Logic is the Property_VALUE of the row whose
        # Property_name equals "Stored Procedure Name"; every OTHER
        # Property_name/Property_VALUE pair (Connection information, Stored
        # procedure type, execution order, etc.) goes into Additional
        # Informations.
        pname_key = _norm("Property_name")
        pval_key = _norm("Property_VALUE")
        biz = ""
        pairs, seen = [], set()
        for normmap in matched_rows:
            pname = normmap.get(pname_key, "")
            pval = normmap.get(pval_key, "")
            if not pname:
                continue
            if _norm(pname) == _norm("Stored Procedure Name"):
                if not biz:
                    biz = pval
                continue
            pair = f"{pname}: {pval}" if pval else pname
            if pair not in seen:
                seen.add(pair)
                pairs.append(pair)
        return biz, "; ".join(pairs)

    if "TRANSACTION" in t:
        return attr("Transaction Control Condition"), ""

    if "UPDATE STRATEGY" in t:
        return attr("Update Strategy Expression"), ""

    if "RANK" in t:
        top_bottom = attr("Top/Bottom", "Top Bottom", "Rank")
        num_ranks = attr("Number of Ranks", "Number Of Ranks")
        biz = f"Rank={top_bottom}, number of Ranks={num_ranks}" if (top_bottom or num_ranks) else ""
        addl = attr("Case Sensitive String Comparison", "Case-Sensitive String Comparison")
        return biz, addl

    if "CUSTOM" in t:
        grp_port = attr("Group_source_port", "Group Source Port")
        grp_type = attr("GORUP_TYPE", "Group_Type", "Group Type")
        ext_val = attr("Extension_Value", "Extension Value")
        biz_parts = []
        if grp_port:
            biz_parts.append(f"Group_source_port: {grp_port}")
        if grp_type:
            biz_parts.append(f"GROUP_TYPE: {grp_type}")
        if ext_val:
            biz_parts.append(f"Extension_Value: {ext_val}")
        biz = "; ".join(biz_parts)

        ext_name = attr("Extension_Name", "Extension Name")
        ext_domain = attr("Extension_domain_name", "Extension Domain Name")
        addl_parts = []
        if ext_name:
            addl_parts.append(f"Extension_Name: {ext_name}")
        if ext_domain:
            addl_parts.append(f"Extension_domain_name: {ext_domain}")
        addl = "; ".join(addl_parts)
        return biz, addl

    return "", ""


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def enrich_catalog_rows(rows, workbook, *, mapping_key="_Mapping", mapplet_mode=False,
                         session_lookup=None):
    """Mutates `rows` (a list of dicts, as returned by
    lineage_engine.build_transformation_catalog / build_mapplet_transformation_catalog)
    in place, filling "Business Logic", "Additional Informations" and
    "Category" - Excel first, XML gap-fill:

      1. Look up this row's (session, mapping/mapplet, transformation, port)
         in the matching tab of the user-supplied `workbook`. If that tab
         has a non-blank value, use it - Source is stamped "Excel (<tab>)".
      2. Otherwise, fall back to the XML-derived text lineage_engine already
         computed at catalog-build time (row["_XML_Business_Logic"] /
         row["_XML_Additional_Info"]) - Source is stamped "XML - gap fill".
      3. If NEITHER source has anything for this row, Business Logic stays
         blank and Source is stamped "XML - lineage" (XML only confirmed
         this port/instance exists and how it connects - no logic text was
         available from either source).

    mapping_key: bookkeeping column on each row holding the mapping (Tab-2,
                 "_Mapping") or mapplet (Tab-4, "_Mapplet") scope name.
    mapplet_mode: True for Tab-4 rows - a mapplet definition isn't itself
                 run by any one session, so the Session Name key is skipped
                 for these and matching falls back to
                 (Mapplet Name, Transformation Name, PORT_NAME).
    session_lookup: {mapping_name: session_name} - only used when
                 mapplet_mode is False, to resolve the Session Name key.
    Returns `rows` (same object, for convenience).
    """
    if not rows:
        return rows

    sheet_index_cache = {}

    def get_index(ttype):
        if not workbook:
            return None, None
        sheet_name = _match_sheet_name(workbook, ttype, mapplet_mode)
        if not sheet_name:
            return None, None
        if sheet_name not in sheet_index_cache:
            sheet_index_cache[sheet_name] = _build_sheet_index(workbook[sheet_name])
        return sheet_name, sheet_index_cache[sheet_name]

    for row in rows:
        ttype = row.get("Transformation Type", "")
        sheet_name, index = get_index(ttype)

        matched_rows = []
        if index is not None:
            scope_val = row.get(mapping_key, "")
            scope_v = _norm(scope_val)
            trans_v = _norm(row.get("Transformation Name", ""))
            port_v = _norm(row.get("PORT_NAME", ""))
            session_v = ""
            if not mapplet_mode and session_lookup:
                session_v = _norm(session_lookup.get(scope_val, ""))
            matched_rows = _lookup(index, session_v, scope_v, trans_v, port_v)

        biz, addl = _compute_business_fields(ttype, matched_rows) if matched_rows else ("", "")

        if biz:
            # Excel had a usable value for this row - it wins outright,
            # even if Additional Informations came back blank alongside it.
            row["Business Logic"] = biz
            if addl:
                row["Additional Informations"] = addl
            row["_Logic_Source"] = f"Excel ({sheet_name})"
        else:
            xml_biz = row.get("_XML_Business_Logic", "")
            xml_addl = row.get("_XML_Additional_Info", "")
            if xml_biz:
                row["Business Logic"] = xml_biz
                if xml_addl:
                    row["Additional Informations"] = xml_addl
                row["_Logic_Source"] = "XML - gap fill"
            else:
                row["_Logic_Source"] = "XML - lineage"

        row["Category"] = "Involves Derivation" if row.get("_Own_Expression") else "Direct Pass through"

    return rows


# --------------------------------------------------------------------------
# Standalone CLI: thin wrapper around build_lineage.build_lineage
# --------------------------------------------------------------------------

if __name__ == "__main__":
    from build_lineage import build_lineage

    ap = argparse.ArgumentParser(
        description="Generate the lineage report and enrich Business Logic / "
                     "Additional Informations / Category from a business-logic source Excel."
    )
    ap.add_argument("json_path")
    ap.add_argument("target_table", help="Target table name.")
    ap.add_argument("target_instance_name",
                     help="Target INSTANCE name (mostly the sink/last transformation) in "
                          "which the target table is used.")
    ap.add_argument("target_mapping", help="Mapping name where the target table is present.")
    ap.add_argument("business_logic_excel",
                     help="Path to the user-supplied workbook (one tab per transformation type).")
    ap.add_argument("--workflow", default=None)
    ap.add_argument("--out", default="lineage", help="Output filename prefix")
    ap.add_argument("--outdir", default=None,
                     help="Directory to write outputs into. Created automatically if missing.")
    args = ap.parse_args()

    build_lineage(args.json_path, args.target_table, args.target_instance_name, args.target_mapping,
                   args.workflow, args.out, args.outdir, business_logic_excel=args.business_logic_excel)
