from flask import Blueprint, render_template, request

from app.services.workflow_service import require_repo
from app.services import graph_service
from app.analyzer import field_lineage_analyzer as fla
from app.analyzer import reportability_analyzer as ra

bp = Blueprint("reportability", __name__)


def _resolve_and_list(repo, mapping_name: str, table_name: str, instance_name: str) -> dict:
    """Shared resolution logic for both the POST submit and the GET
    re-listing (used by the lineage graph page's Back button so it returns
    to this same attribute list instead of a blank form). Returns a dict of
    template context: either {"error": ...} or the full "ready" listing."""
    ctx = {"mapping_name": mapping_name, "table_name": table_name, "instance_name": instance_name}

    if not mapping_name or not table_name:
        ctx["error"] = "Please enter a Mapping Name and a Table Name."
        return ctx

    if mapping_name not in repo.mappings:
        ctx["error"] = f"Mapping '{mapping_name}' was not found in this upload."
        return ctx

    matches = ra.find_matching_target_instances(repo, mapping_name, table_name)
    if not matches:
        ctx["error"] = f"Table '{table_name}' was not found as a Target Table in mapping '{mapping_name}'."
        return ctx

    if instance_name:
        chosen = next((i for i in matches if i.name == instance_name), None)
        if chosen is None:
            ctx["error"] = (f"'{instance_name}' is not a Transformation Instance of Table '{table_name}' "
                             f"in mapping '{mapping_name}'. Available instance name(s): "
                             + ", ".join(i.name for i in matches))
            return ctx
    elif len(matches) > 1:
        ctx["error"] = (f"Table '{table_name}' appears {len(matches)} times in mapping '{mapping_name}'. "
                         "Please re-submit with one of the following Instance Name(s): "
                         + ", ".join(i.name for i in matches))
        return ctx
    else:
        chosen = matches[0]

    session_name = fla.session_for_mapping(repo, mapping_name)
    attributes = ra.list_attributes(repo, mapping_name, chosen.name)

    ctx["instance_name"] = chosen.name
    ctx["session_name"] = session_name
    ctx["attributes"] = attributes
    ctx["ready"] = True
    return ctx


@bp.route("/reportability", methods=["GET"])
def reportability_page():
    repo = require_repo()
    mapping_name = (request.args.get("mapping") or "").strip()
    table_name = (request.args.get("table") or "").strip()
    instance_name = (request.args.get("instance") or "").strip()

    if not mapping_name and not table_name:
        # Plain landing on the feature: blank form, nothing to resolve yet.
        return render_template("reportability.html", repo=repo)

    # Reached via the lineage graph page's Back button: re-list the same
    # attributes rather than dropping the person back to a blank form.
    ctx = {"repo": repo}
    ctx.update(_resolve_and_list(repo, mapping_name, table_name, instance_name))
    return render_template("reportability.html", **ctx)


@bp.route("/reportability", methods=["POST"])
def reportability_submit():
    repo = require_repo()
    mapping_name = (request.form.get("mapping_name") or "").strip()
    table_name = (request.form.get("table_name") or "").strip()
    instance_name = (request.form.get("instance_name") or "").strip()

    ctx = {"repo": repo}
    ctx.update(_resolve_and_list(repo, mapping_name, table_name, instance_name))
    return render_template("reportability.html", **ctx)


@bp.route("/reportability/graph")
def reportability_graph():
    repo = require_repo()
    mapping_name = request.args.get("mapping", "")
    table_name = request.args.get("table", "")
    instance_name = request.args.get("instance", "")
    field_name = request.args.get("field", "")
    session_name = request.args.get("session") or None

    if mapping_name not in repo.mappings:
        return render_template("error.html", message=f"Mapping '{mapping_name}' not found."), 404

    graph_data = ra.build_attribute_lineage_graph(repo, mapping_name, session_name, instance_name, field_name)
    if graph_data is None:
        return render_template(
            "error.html",
            message=(f"Could not resolve Attribute '{field_name}' on Instance '{instance_name}' "
                      f"in mapping '{mapping_name}'.")
        ), 404

    # table_name is only needed so the Back button can return to this exact
    # attribute list (via the GET route above) instead of a blank form; fall
    # back to the resolved Target Table name if it wasn't passed through.
    table_name = table_name or graph_data["final_target_table"]

    # Split the lineage graph Mapping-wise: one panel per Mapping (in real
    # Source -> Target lineage order) with only that Mapping's own
    # components, instead of a single graph mixing every Mapping together.
    grouping = ra.group_by_mapping(repo, graph_data)
    graph_paths = graph_service.render_reportability_graph_by_mapping(graph_data, grouping)
    mapping_panels = [
        {
            "mapping": m,
            "graph_path": graph_paths[m],
            "node_count": grouping["panels"][m]["node_count"],
            "incoming": grouping["panels"][m]["incoming"],
            "outgoing": grouping["panels"][m]["outgoing"],
            "is_final": (m == graph_data["final_mapping"]),
        }
        for m in grouping["order"]
    ]

    return render_template("reportability_graph.html", graph_data=graph_data,
                            mapping_panels=mapping_panels,
                            field_name=field_name, mapping_name=mapping_name, table_name=table_name,
                            instance_name=instance_name)
