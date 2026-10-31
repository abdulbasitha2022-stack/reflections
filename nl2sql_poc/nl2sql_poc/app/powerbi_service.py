"""Power BI project generation + Desktop automation, behind one small service object.

Order of operations for a new page:
  1. build_page():    save pending Desktop edits (prepare), then write the PBIR page / TMDL table
  2. reload_refresh(): ask Desktop to reload the project and refresh the new table

Step 2 makes Power BI Desktop re-create every visual (including the chatbot), so the API runs it
AFTER the chat reply has been stored and sent. The chatbot restores its conversation afterwards.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .powerbi_project_generator import PowerBIProjectError, generate_native_report_page

log = logging.getLogger("chatbot.powerbi")


class PowerBIService:
    def __init__(self, cfg: dict, base_dir, store):
        self.cfg = cfg.get("powerbi", {}) or {}
        self.automation_cfg = self.cfg.get("desktop_automation", {}) or {}
        self.base = Path(base_dir)
        self.store = store
        self.lock = threading.Lock()      # one Power BI project edit / reload at a time

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    @property
    def automation_enabled(self) -> bool:
        return self.enabled and bool(self.automation_cfg.get("enabled", False))

    @property
    def reload_delay(self) -> float:
        return float(self.automation_cfg.get("reload_delay_seconds", 2))

    # ------------------------------------------------------------------ Desktop helper
    def run_desktop_automation(self, action, table_name=None):
        started = time.perf_counter()
        timeout_seconds = int(self.automation_cfg.get("timeout_seconds", 180))
        script_path = self.base / "app" / "powerbi_desktop_automation.ps1"
        if not script_path.is_file():
            raise PowerBIProjectError(f"Power BI Desktop automation script not found: {script_path}")
        command = [os.getenv("POWERSHELL_EXE", "powershell.exe"), "-NoProfile",
                   "-ExecutionPolicy", "Bypass", "-File", str(script_path),
                   "-Action", action, "-ReportDir", str(self.cfg["report_dir"]),
                   "-TimeoutSeconds", str(timeout_seconds)]
        if table_name:
            command.extend(["-TableName", str(table_name)])
        try:
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                       errors="replace", timeout=timeout_seconds + 30, check=False)
        except subprocess.TimeoutExpired as exc:
            log.exception("Power BI Desktop automation timed out action=%s", action)
            raise PowerBIProjectError(
                f"Power BI Desktop automation timed out during '{action}'. "
                "Check Power BI Desktop and the model refresh status before retrying.") from exc
        except OSError as exc:
            log.exception("Could not launch Power BI Desktop automation action=%s", action)
            raise PowerBIProjectError(
                f"Could not launch PowerShell for Power BI Desktop automation: {exc}") from exc

        lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
        result = None
        if lines:
            try:
                result = json.loads(lines[-1])
            except json.JSONDecodeError:
                result = None
        if completed.returncode != 0 or not isinstance(result, dict) or not result.get("success"):
            details = ""
            if isinstance(result, dict):
                details = str(result.get("error") or result.get("message") or "")
            if not details:
                details = (completed.stderr or completed.stdout or "No diagnostic output was returned.").strip()[-2500:]
            log.error("Power BI Desktop automation failed action=%s return_code=%d",
                      action, completed.returncode)
            raise PowerBIProjectError(f"Power BI Desktop automation '{action}' failed: {details}")
        log.info("Power BI Desktop automation completed action=%s elapsed_ms=%d",
                 action, round((time.perf_counter() - started) * 1000))
        return result

    # ------------------------------------------------------------------ page generation
    def build_page(self, view_name, title, requested_visual_type=None, reuse_existing_table=False, request_id=None, session_id=None):
        """Write the page (and table) into the PBIP project. Does NOT reload Desktop."""
        if not self.enabled:
            return {"status": "disabled",
                    "message": "Power BI project generation is disabled in config_billing.yaml."}
        report_dir, model_dir = self.cfg.get("report_dir"), self.cfg.get("model_dir")
        if not report_dir or not model_dir:
            raise PowerBIProjectError(
                "powerbi.enabled is true but report_dir/model_dir are missing from config_billing.yaml.")
        if not re.fullmatch(r"vw_[A-Za-z0-9_]+", view_name):
            raise PowerBIProjectError("Generated view name has an unexpected format.")
        log.info("Building Power BI page request_id=%s view_name=%s", request_id, view_name)
        with self.lock:
            if self.automation_enabled:
                # Save pending UI edits (e.g. deleted pages) before the generator writes to disk.
                self.run_desktop_automation("prepare")
            columns = self.store.view_columns(view_name)
            generated = generate_native_report_page(
                report_dir=report_dir, model_dir=model_dir, view_name=view_name, title=title,
                columns=columns, mysql_server=self.store.server, database=self.store.database,
                include_detail_table=bool(self.cfg.get("include_detail_table", True)),
                requested_visual_type=requested_visual_type,
                reuse_existing_table=reuse_existing_table, request_id=request_id, session_id=session_id)
            log.info("Power BI page built request_id=%s view_name=%s", request_id, view_name)
            return generated

    def reload_refresh(self, generated: dict) -> dict:
        log.info("Reloading Power BI model table=%s", generated["model_table"])
        with self.lock:
            return self.run_desktop_automation("reload_refresh", table_name=generated["model_table"])
