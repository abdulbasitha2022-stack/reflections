"""Create a PBIR page with native visuals for a generated MySQL view.

This module edits a local Power BI Project (PBIP) on disk. It adds a TMDL table
that imports the named MySQL view, registers the table in model.tmdl, and adds a
new PBIR page containing a native chart plus a details table.

Power BI Desktop must reload external project changes and refresh the model before
new imported rows can be displayed. Always work on a saved, backed-up project.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable

log = logging.getLogger("chatbot.powerbi_project")


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SUPPORTED_VISUAL_TYPES = {"lineChart", "clusteredColumnChart", "barChart", "pieChart", "donutChart", "tableEx"}
VIEW_RE = re.compile(r"^vw_[A-Za-z0-9_]+$")


class PowerBIProjectError(RuntimeError):
    """A safe, user-facing problem while modifying a Power BI project."""


def _uuid_hex(length: int = 20) -> str:
    return uuid.uuid4().hex[:length]


def _json_read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PowerBIProjectError(f"Required Power BI project file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PowerBIProjectError(f"Invalid JSON in {path}: {exc}") from exc


def _json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _valid_identifier(name: str, kind: str) -> str:
    if not isinstance(name, str) or not IDENTIFIER_RE.fullmatch(name):
        raise PowerBIProjectError(
            f"Unsupported {kind} '{name}'. Generated view columns must use simple SQL identifiers "
            "(letters, digits and underscores, starting with a letter or underscore)."
        )
    return name


def _powerbi_type(mysql_type: str) -> tuple[str, str | None]:
    """Map SHOW COLUMNS MySQL types into a practical Power BI/TMDL type."""
    t = (mysql_type or "").lower().strip()
    base = re.sub(r"\(.*\)", "", t).strip()
    if base in {"date", "datetime", "timestamp", "time"}:
        # Time-only values aren't ideal in reports, but Power BI's datetime type is the closest.
        return "dateTime", "General Date"
    if base in {"tinyint", "smallint", "mediumint", "int", "integer", "bigint", "year", "bit"}:
        return "int64", "0"
    if base in {"decimal", "numeric", "float", "double", "real"}:
        return "double", None
    if base in {"bool", "boolean"}:
        return "boolean", None
    return "string", None


def _is_numeric(column: dict) -> bool:
    return _powerbi_type(column.get("type", ""))[0] in {"int64", "double"}


def _is_temporal(column: dict) -> bool:
    t = (column.get("type") or "").lower().strip()
    base = re.sub(r"\(.*\)", "", t).strip()
    if base in {"date", "datetime", "timestamp"}:
        return True
    # Groq may return a month key like '2026-10' as VARCHAR. It still works as a categorical axis.
    name = column.get("name", "").lower()
    return any(token in name for token in ("month", "date", "period", "timestamp", "year", "week"))


def _choose_fields(columns: list[dict]) -> tuple[str | None, str | None, str]:
    """Choose dimension, measure, and native chart type from output schema."""
    numeric = [column for column in columns if _is_numeric(column)]
    if not numeric:
        return None, None, "tableEx"

    temporal = [column for column in columns if _is_temporal(column)]
    dimension_candidates = [column for column in columns if column not in numeric]
    # Temporal columns take precedence for time-series reports.
    if temporal:
        dim = temporal[0]
        measure = _pick_measure(numeric)
        return dim["name"], measure["name"], "lineChart"

    # Prefer descriptive string fields over IDs and long technical key fields.
    categories = [c for c in dimension_candidates if _powerbi_type(c.get("type", ""))[0] == "string"]
    categories = [c for c in categories if not c["name"].lower().endswith("_id")]
    if not categories:
        categories = dimension_candidates
    if categories:
        dim = categories[0]
        measure = _pick_measure(numeric)
        return dim["name"], measure["name"], "clusteredColumnChart"

    # No dimension: still provide a native table visual of the result.
    return None, numeric[0]["name"], "tableEx"


def _pick_measure(numeric_columns: list[dict]) -> dict:
    priorities = ("_usd", "revenue", "refund", "amount", "count", "rate", "_pct", "total")
    return sorted(
        numeric_columns,
        key=lambda c: (
            -max((len(p) for p in priorities if p in c["name"].lower()), default=0),
            c["name"].lower(),
        ),
    )[0]


def _select_requested_visual(columns: list[dict], requested_visual_type: str | None) -> tuple[str | None, str | None, str]:
    """Honor an explicit visual choice when the result has a usable category and measure."""
    dimension, measure, automatic_type = _choose_fields(columns)
    requested = (requested_visual_type or "auto").strip()
    if requested == "auto":
        return dimension, measure, automatic_type
    if requested not in SUPPORTED_VISUAL_TYPES:
        raise PowerBIProjectError(
            f"Unsupported visual type '{requested}'. Choose auto, lineChart, clusteredColumnChart, "
            "barChart, pieChart, donutChart, or tableEx."
        )
    if requested == "tableEx":
        return dimension, measure, "tableEx"
    if dimension and measure:
        return dimension, measure, requested
    # Do not invent chart mappings when the query lacks a category or numeric measure.
    return dimension, measure, "tableEx"


def _projection_column(entity: str, field_name: str, active: bool = True) -> dict:
    result = {
        "field": {
            "Column": {
                "Expression": {"SourceRef": {"Entity": entity}},
                "Property": field_name,
            }
        },
        "queryRef": f"{entity}.{field_name}",
        "nativeQueryRef": field_name,
    }
    if active:
        result["active"] = True
    return result


def _projection_sum(entity: str, field_name: str) -> dict:
    return {
        "field": {
            "Aggregation": {
                "Expression": {
                    "Column": {
                        "Expression": {"SourceRef": {"Entity": entity}},
                        "Property": field_name,
                    }
                },
                "Function": 0,
            }
        },
        "queryRef": f"Sum({entity}.{field_name})",
        "nativeQueryRef": f"Sum of {field_name}",
    }


def _chart_visual_json(entity: str, dimension: str, measure: str, visual_type: str,
                       visual_id: str, x: int, y: int, width: int, height: int,
                       sort_direction: str) -> dict:
    category = _projection_column(entity, dimension)
    measure_projection = _projection_sum(entity, measure)
    sort_field = category["field"] if visual_type == "lineChart" else measure_projection["field"]
    visual_query = {
        "queryState": {
            "Category": {"projections": [category]},
            "Y": {"projections": [measure_projection]},
        },
        "sortDefinition": {
            "sort": [{"field": sort_field, "direction": sort_direction}],
            "isDefaultSort": True,
        },
    }
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.13.0/schema.json",
        "name": visual_id,
        "position": {"x": x, "y": y, "z": 0, "height": height, "width": width, "tabOrder": 0},
        "visual": {"visualType": visual_type, "query": visual_query, "drillFilterOtherVisuals": True},
    }


def _table_visual_json(entity: str, columns: list[dict], visual_id: str,
                       x: int, y: int, width: int, height: int, tab_order: int = 1) -> dict:
    projections = [_projection_column(entity, c["name"]) for c in columns]
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.13.0/schema.json",
        "name": visual_id,
        "position": {"x": x, "y": y, "z": 1, "height": height, "width": width, "tabOrder": tab_order},
        "visual": {
            "visualType": "tableEx",
            "query": {"queryState": {"Values": {"projections": projections}}},
            "drillFilterOtherVisuals": True,
        },
    }


def _tmdl_ref(name: str) -> str:
    if IDENTIFIER_RE.fullmatch(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def _table_tmdl(table_entity: str, view_name: str, columns: list[dict], server: str, database: str) -> str:
    safe_var = "q_" + re.sub(r"[^A-Za-z0-9_]", "_", view_name)
    # Power Query M string escaping uses doubled quotes.
    server_m = server.replace('"', '""')
    database_m = database.replace('"', '""')
    lines = [f"table '{table_entity}'", f"\tlineageTag: {uuid.uuid4()}", ""]
    for col in columns:
        name = col["name"]
        dtype, fmt = _powerbi_type(col.get("type", ""))
        lines.extend([
            f"\tcolumn {_tmdl_ref(name)}",
            f"\t\tdataType: {dtype}",
        ])
        if fmt:
            lines.append(f"\t\tformatString: {fmt}")
        lines.extend([
            f"\t\tlineageTag: {uuid.uuid4()}",
            f"\t\tsummarizeBy: {'sum' if dtype in {'int64', 'double'} else 'none'}",
            f"\t\tsourceColumn: {_tmdl_ref(name)}",
            "",
        ])
        if dtype == "double":
            lines.append("\t\tannotation PBI_FormatHint = {\"isGeneralNumber\":true}")
            lines.append("")
    lines.extend([
        f"\tpartition '{table_entity}' = m",
        "\t\tmode: import",
        "\t\tsource =",
        "\t\t\t\tlet",
        f'\t\t\t\t\tSource = MySQL.Database("{server_m}", "{database_m}", [ReturnSingleDatabase=true]),',
        f'\t\t\t\t\t{safe_var} = Source{{[Schema="{database_m}",Item="{view_name}"]}}[Data]',
        "\t\t\t\tin",
        f"\t\t\t\t\t{safe_var}",
        "",
        "\tannotation PBI_ResultType = Table",
        "",
    ])
    return "\n".join(lines)


def _update_model_file(model_file: Path, table_entity: str) -> None:
    text = model_file.read_text(encoding="utf-8")
    ref_line = f"ref table '{table_entity}'"
    if ref_line not in text:
        marker = "ref cultureInfo en-IN"
        if marker in text:
            text = text.replace(marker, ref_line + "\n" + marker, 1)
        else:
            text = text.rstrip() + "\n" + ref_line + "\n"

    match = re.search(r"(?m)^annotation PBI_QueryOrder = (\[.*\])\s*$", text)
    if match:
        try:
            order = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise PowerBIProjectError(f"Cannot update PBI_QueryOrder in {model_file}: {exc}") from exc
        if table_entity not in order:
            order.append(table_entity)
            text = text[:match.start(1)] + json.dumps(order, ensure_ascii=False, separators=(",", ":")) + text[match.end(1):]
    else:
        # Query order is an optional annotation. Add one if the project lacks it.
        text = text.replace("annotation __PBI_TimeIntelligenceEnabled", f"annotation PBI_QueryOrder = {json.dumps([table_entity])}\n\nannotation __PBI_TimeIntelligenceEnabled", 1)
    model_file.write_text(text, encoding="utf-8")


def _safe_display_name(title: str, fallback: str) -> str:
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    title = re.sub(r"^(please\s+)?(show|display|create|make|give me)\s+", "", title, flags=re.I)
    title = title[:60].strip(" .-_")
    return title or fallback


def _backup_files(paths: Iterable[Path], parent: Path) -> Path:
    backup_dir = parent / ".powerbi_generated_backups" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_dir.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if path.exists():
            # Include path name and original basename; each source file copied once.
            dest = backup_dir / f"{path.parent.name}__{path.name}"
            shutil.copy2(path, dest)
    return backup_dir


def generate_native_report_page(
    *,
    report_dir: str | Path,
    model_dir: str | Path,
    view_name: str,
    title: str,
    columns: list[dict],
    mysql_server: str = "localhost:3306",
    database: str = "billing",
    include_detail_table: bool = True,
    requested_visual_type: str | None = "auto",
    reuse_existing_table: bool = False,
    request_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Add a table for ``view_name`` to the TMDL model and a new page with native visuals.

    ``columns`` must look like ``[{"name": "refund_reason", "type": "varchar(50)"}, ...]``.
    With ``reuse_existing_table=True`` (used when the user only asks for a different chart type)
    the view's table must already exist in the model; only a new page is added.

    This function edits project files, but does not open Power BI Desktop or refresh the imported data.
    """
    report_dir = Path(report_dir).expanduser().resolve()
    model_dir = Path(model_dir).expanduser().resolve()
    if not VIEW_RE.fullmatch(view_name):
        raise PowerBIProjectError("view_name must be a generated vw_ name containing only letters, digits and underscores")
    if not report_dir.is_dir() or not model_dir.is_dir():
        raise PowerBIProjectError("Both report_dir and model_dir must point to existing Power BI project folders")

    report_def = report_dir / "definition"
    pages_dir = report_def / "pages"
    pages_meta_file = pages_dir / "pages.json"
    model_def = model_dir / "definition"
    model_file = model_def / "model.tmdl"
    tables_dir = model_def / "tables"
    if not pages_dir.is_dir() or not tables_dir.is_dir() or not model_file.is_file():
        raise PowerBIProjectError("Project folders are missing PBIR pages or TMDL model definitions")

    # Confirm the report's relative semantic-model reference resolves to the model being edited.
    definition_pbir = _json_read(report_dir / "definition.pbir")
    by_path = definition_pbir.get("datasetReference", {}).get("byPath", {}).get("path")
    if by_path:
        resolved_model = (report_dir / by_path).resolve()
        if resolved_model != model_dir:
            raise PowerBIProjectError(
                f"Report definition.pbir points to '{resolved_model}', not the supplied model folder '{model_dir}'."
            )

    if not columns:
        raise PowerBIProjectError(f"MySQL view '{view_name}' returned no columns")
    normalized_cols = []
    seen = set()
    for raw in columns:
        name = _valid_identifier(str(raw.get("name", "")), "column name")
        if name in seen:
            raise PowerBIProjectError(f"Duplicate column name '{name}' in generated view")
        seen.add(name)
        normalized_cols.append({"name": name, "type": str(raw.get("type", "varchar(255)"))})

    table_entity = f"billing {view_name}"
    table_file = tables_dir / f"{table_entity}.tmdl"
    table_exists = table_file.exists() or f"ref table '{table_entity}'" in model_file.read_text(encoding="utf-8")
    if reuse_existing_table and not table_file.exists():
        raise PowerBIProjectError(f"Cannot reuse '{view_name}': its table is not in the semantic model")
    if table_exists and not reuse_existing_table:
        raise PowerBIProjectError(f"The semantic model already contains generated view '{view_name}'")

    pages_meta = _json_read(pages_meta_file)
    page_order = list(pages_meta.get("pageOrder", []))
    # Pick the existing report dimensions and active schema version as templates.
    first_page = None
    page_width, page_height = 1920, 1080
    page_schema = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json"
    for page_id in page_order:
        candidate = pages_dir / page_id / "page.json"
        if candidate.exists():
            first_page = _json_read(candidate)
            page_width = int(first_page.get("width", page_width))
            page_height = int(first_page.get("height", page_height))
            page_schema = first_page.get("$schema", page_schema)
            break
    if first_page is None:
        raise PowerBIProjectError("No existing report page definitions were found")

    display_name = _safe_display_name(title, f"Report {view_name}")
    existing_names = set()
    for page_id in page_order:
        path = pages_dir / page_id / "page.json"
        if path.exists():
            existing_names.add(_json_read(path).get("displayName", ""))
    base_display = display_name
    suffix = 2
    while display_name in existing_names:
        tail = f" ({suffix})"
        display_name = base_display[: max(1, 60 - len(tail))] + tail
        suffix += 1

    page_id = _uuid_hex(20)
    while (pages_dir / page_id).exists():
        page_id = _uuid_hex(20)
    page_folder = pages_dir / page_id
    visuals_dir = page_folder / "visuals"

    dimension, measure, chart_type = _select_requested_visual(normalized_cols, requested_visual_type)
    visual_id = _uuid_hex(20)
    visual_files = []
    if dimension and measure and chart_type in SUPPORTED_VISUAL_TYPES - {"tableEx"}:
        sort_direction = "Ascending" if chart_type == "lineChart" else "Descending"
        chart_json = _chart_visual_json(
            table_entity, dimension, measure, chart_type, visual_id,
            x=30, y=25, width=page_width - 60,
            height=max(160, int((page_height - 170) * 0.58)) if include_detail_table else page_height - 170,
            sort_direction=sort_direction,
        )
        chart_path = visuals_dir / visual_id / "visual.json"
        _json_write(chart_path, chart_json)
        visual_files.append(str(chart_path))
        if include_detail_table:
            table_id = _uuid_hex(20)
            table_y = 25 + (max(160, int((page_height - 170) * 0.58)) if include_detail_table else 0) + 15
            table_json = _table_visual_json(
                table_entity, normalized_cols, table_id,
                x=30, y=table_y, width=page_width - 60,
                height=max(100, page_height - table_y - 130), tab_order=1,
            )
            table_path = visuals_dir / table_id / "visual.json"
            _json_write(table_path, table_json)
            visual_files.append(str(table_path))
    else:
        # For a result without a usable dimension+measure pair, produce a detail table rather
        # than inventing a potentially misleading chart.
        table_json = _table_visual_json(
            table_entity, normalized_cols, visual_id,
            x=30, y=25, width=page_width - 60, height=page_height - 170,
        )
        table_path = visuals_dir / visual_id / "visual.json"
        _json_write(table_path, table_json)
        visual_files.append(str(table_path))

    # Place a dedicated instance of the chatbot custom visual on the generated report page.
    # It runs in feedback-only mode and submits feedback against this report's request_id.
    if request_id:
        feedback_id = _uuid_hex(20)
        fb_width  = min(420, page_width - 60)
        fb_height = 130
        fb_x      = page_width  - fb_width  - 30   # right-aligned
        fb_y      = page_height - fb_height - 30    # bottom-aligned
        feedback_json = {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.13.0/schema.json",
            "name": feedback_id,
            "position": {"x": fb_x, "y": fb_y, "z": 100,
                         "height": fb_height, "width": fb_width, "tabOrder": 10},
            "visual": {
                "visualType": "billingChatbot79170476B0CA440F9F0C35FF45CF4C13",
                # No queryState/projections — feedback mode is driven entirely by
                # the objects below.  Removing the field binding means Power BI
                # won't show a field-name header and the visual doesn't require
                # a data field to be dragged in.
                "objects": {"feedback": [{"properties": {
                    "mode":      {"expr": {"Literal": {"Value": "'feedback'"}}},
                    "requestId": {"expr": {"Literal": {"Value": "'" + request_id.replace("'", "''") + "'"}}},
                    "sessionId": {"expr": {"Literal": {"Value": "'" + (session_id or "").replace("'", "''") + "'"}}}
                }}]},
                "drillFilterOtherVisuals": True
            }
        }
        feedback_path = visuals_dir / feedback_id / "visual.json"
        _json_write(feedback_path, feedback_json)
        visual_files.append(str(feedback_path))

    page_json = {
        "$schema": page_schema,
        "name": page_id,
        "displayName": display_name,
        "displayOption": "FitToPage",
        "height": page_height,
        "width": page_width,
    }
    page_file = page_folder / "page.json"
    _json_write(page_file, page_json)

    # Backup current metadata before editing it. Generated files are new and don't need backups.
    backup_dir = _backup_files([model_file, pages_meta_file], report_dir.parent)
    try:
        if not reuse_existing_table:
            table_text = _table_tmdl(table_entity, view_name, normalized_cols, mysql_server, database)
            table_file.write_text(table_text, encoding="utf-8")
            _update_model_file(model_file, table_entity)
        if page_id not in page_order:
            page_order.append(page_id)
        pages_meta["pageOrder"] = page_order
        # Keep the user's current page active (normally the chatbot page) so the completion
        # message remains visible. The new report is still added to pageOrder and can be opened by name.
        _json_write(pages_meta_file, pages_meta)
    except Exception:
        log.exception("Power BI project update failed; attempting to roll back generated files")
        # Best-effort rollback of model metadata; generated page/table are removed to avoid partial output.
        try:
            shutil.rmtree(page_folder, ignore_errors=True)
            if not reuse_existing_table:
                table_file.unlink(missing_ok=True)
            for backup in backup_dir.iterdir():
                if backup.name.endswith("__model.tmdl"):
                    shutil.copy2(backup, model_file)
                elif backup.name.endswith("__pages.json"):
                    shutil.copy2(backup, pages_meta_file)
        finally:
            raise

    return {
        "status": "generated_pending_reload_and_refresh",
        "view_name": view_name,
        "model_table": table_entity,
        "page_id": page_id,
        "page_name": display_name,
        "visual_type": chart_type,
        "requested_visual_type": requested_visual_type or "auto",
        "dimension_field": dimension,
        "measure_field": measure,
        "visual_files": visual_files,
        "feedback_visual_added": bool(request_id),
        "table_definition": str(table_file),
        "backup_dir": str(backup_dir),
        "needs_powerbi_reload": True,
        "needs_model_refresh": not reuse_existing_table,
        "reused_table": reuse_existing_table,
    }
