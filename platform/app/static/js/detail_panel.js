// Listens for node-click messages posted from the PyVis iframe (see
// graph_builder._inject_click_bridge) and populates the right-side detail
// panel via the JSON endpoints (/session/<name>, /transformation/<name>).

function openDetailPanel() {
  document.getElementById("detailPanel").classList.add("open");
}
function closeDetailPanel() {
  document.getElementById("detailPanel").classList.remove("open");
}

function renderKeyValueList(container, obj) {
  var html = "<dl class='row mb-0'>";
  Object.keys(obj).forEach(function (k) {
    var v = obj[k];
    if (Array.isArray(v)) v = v.length ? v.join(", ") : "(none)";
    html += "<dt class='col-5 text-muted small'>" + k + "</dt><dd class='col-7'>" + (v || "(none)") + "</dd>";
  });
  html += "</dl>";
  container.innerHTML = html;
}

function loadSessionPanel(sessionName) {
  fetch("/session/" + encodeURIComponent(sessionName))
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.error) return;
      document.getElementById("detailPanelTitle").textContent = "Session: " + data.session_name;
      renderKeyValueList(document.getElementById("detailPanelBody"), {
        "Session Name": data.session_name,
        "Mapping Name": data.mapping_name,
        "Tables Used": data.tables_used,
        "Transformations Used": data.transformations_used,
      });
      openDetailPanel();
    });
}

function loadTransformationPanel(name, mappingName) {
  var url = "/transformation/" + encodeURIComponent(name);
  if (mappingName) url += "?mapping=" + encodeURIComponent(mappingName);
  fetch(url)
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.error) return;
      document.getElementById("detailPanelTitle").textContent = "Transformation: " + data.name;
      var portNames = function (ports) { return ports.map(function (p) { return p.name; }); };
      var exprText = data.expressions.map(function (e) { return e.port + " = " + e.expression; }).join(" | ") || "(none)";
      var attrText = Object.keys(data.attributes).map(function (k) { return k + "=" + data.attributes[k]; }).join(", ") || "(none)";
      renderKeyValueList(document.getElementById("detailPanelBody"), {
        "Type": data.type,
        "Business Logic": data.business_logic,
        "Implementation Details": data.implementation_details,
        "Input Ports": portNames(data.input_ports),
        "Output Ports": portNames(data.output_ports),
        "Variable Ports": portNames(data.variable_ports),
        "Expressions": exprText,
        "Attributes": attrText,
      });
      openDetailPanel();
    });
}

// Reportability lineage graph: node metadata is embedded directly in the page
// as window.REPORTABILITY_NODES (see reportability_graph.html), so no extra
// round-trip is needed -- just look the clicked node id up and render the
// fields required for that node's kind (per the Reportability click-detail spec).
function loadReportabilityPanel(nodeId) {
  var nodes = window.REPORTABILITY_NODES || {};
  var n = nodes[nodeId];
  if (!n) return;
  var title, fields;

  if (n.kind === "TARGET_FINAL") {
    // Ultimate last node: only the field name.
    title = "Target Field";
    fields = { "Field Name": n.field };
  } else if (n.kind === "TARGET_INTERMEDIATE") {
    // A previous session's Target Table, itself feeding a later session.
    title = "Previous Session Target: " + (n.table || n.instance);
    fields = {
      "Next Session Name": n.next_session,
      "Next Mapping Name": n.next_mapping,
      "Session Source Table": n.next_source_table,
    };
  } else if (n.kind === "SOURCE") {
    title = "Source: " + (n.table || n.instance);
    fields = {
      "Source Qualifier": n.source_qualifier,
      "Last Session Name": n.final_session,
      "Target Table Name": n.final_target_table,
    };
  } else if (n.kind === "MAPPLET") {
    title = "Mapplet: " + n.instance;
    fields = {
      "Mapping": n.mapping,
      "Session": n.session,
      "Field": n.field,
      "Note": n.logic,
    };
  } else {
    // Ordinary transformation hop.
    title = "Transformation: " + n.instance;
    fields = {
      "Mapping": n.mapping,
      "Session": n.session,
    };
    if (n.mapplet) {
      fields["Inside Mapplet"] = n.mapplet;
    }
    fields["Type"] = n.ttype;
    fields["Classification"] = n.passthrough ? "Direct pass-through" : "Logic applied";
    fields["Logic"] = n.logic;
  }

  document.getElementById("detailPanelTitle").textContent = title;
  renderKeyValueList(document.getElementById("detailPanelBody"), fields);
  openDetailPanel();
}

window.addEventListener("message", function (event) {
  if (!event.data || event.data.type !== "node-click") return;
  var nodeId = event.data.id;
  var mode = document.body.getAttribute("data-panel-mode");
  if (mode === "session") {
    loadSessionPanel(nodeId);
  } else if (mode === "transformation") {
    var mappingName = document.body.getAttribute("data-mapping-name") || "";
    var instMap = window.MAPPING_INSTANCES || {};
    var inst = instMap[nodeId];
    if (inst && inst.type === "TRANSFORMATION") {
      loadTransformationPanel(inst.ref_name, mappingName);
    }
    // SOURCE/TARGET nodes intentionally have no detail panel per spec.
  } else if (mode === "reportability") {
    loadReportabilityPanel(nodeId);
  }
});
