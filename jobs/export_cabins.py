"""Export authenticated AirlineSim cabin configurations to a local JSON file."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs.export_service_profiles import (
    DEFAULT_USER_AGENT,
    PROJECT_ROOT,
    get_settings,
    load_dotenv,
    login,
    normalize_text,
    table_records,
)

DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "cabins.json"


def seating_url(service_profiles_url: str) -> str:
    return service_profiles_url.split("/action/", 1)[0].rstrip("/") + "/app/seating"


def selected_value(control: Tag) -> str:
    if control.name == "input" and control.get("type", "").casefold() in {"checkbox", "radio"}:
        return control.get("value", "") if control.has_attr("checked") else ""
    if control.name == "select":
        option = control.select_one("option[selected]") or control.select_one("option")
        return option.get("value", "") if option else ""
    if control.name == "textarea":
        return control.get_text()
    return control.get("value", "")


def table_fields(table: Tag) -> dict[str, str]:
    fields = {}
    for row in table.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        for index in range(0, len(cells) - 1, 2):
            if cells[index].name != "th":
                continue
            label = normalize_text(cells[index].get_text(" ", strip=True)).rstrip(":")
            value = normalize_text(cells[index + 1].get_text(" ", strip=True))
            if label and value:
                fields[label] = value
    return fields


def extract_cabin_editor(html: str, source_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    editor_form = next((form for form in soup.select("form[action]") if "editor~form" in form.get("action", "")), None)
    if editor_form is None:
        raise ValueError("AirlineSim-Kabineneditorformular nicht gefunden")
    cabins = []
    for container in editor_form.select(".cabin"):
        table = container.select_one("table")
        heading = container.select_one("h3")
        if table is None or heading is None:
            continue
        cabins.append({
            "class": normalize_text(heading.get_text(" ", strip=True)),
            "fields": table_fields(table),
            "form_values": {
                control["name"]: selected_value(control)
                for control in container.select("input[name], select[name], textarea[name]")
                if control.get("type", "").casefold() not in {"hidden", "password"}
            },
        })
    if not cabins:
        raise ValueError("Keine Kabinenklassen im AirlineSim-Kabineneditor gefunden")
    summary_table = editor_form.select_one("table")
    return {
        "source_url": source_url,
        "summary": table_fields(summary_table) if summary_table else {},
        "form_values": {
            control["name"]: selected_value(control)
            for control in editor_form.select("input[name], select[name], textarea[name]")
            if control.get("type", "").casefold() not in {"hidden", "password"}
            and control["name"] != "searchQuery"
        },
        "cabins": cabins,
        "tables": [table_records(table) for table in editor_form.select("table")],
    }


def extract_cabin_summaries(html: str, source_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    configurations = []
    aircraft_type = None
    for row in soup.select("table tr"):
        if "subhead" in row.get("class", []):
            aircraft_type = normalize_text(row.get_text(" ", strip=True)).split("(", 1)[0].strip()
            continue
        edit_link = next((anchor for anchor in row.select("a[href]") if normalize_text(anchor.get_text(" ", strip=True)).casefold() == "edit"), None)
        if edit_link is None:
            continue
        cells = [normalize_text(cell.get_text(" ", strip=True)) for cell in row.find_all("td", recursive=False)]
        if cells and cells[0]:
            configurations.append({
                "name": cells[0],
                "aircraft_type": aircraft_type,
                "overview": {f"column_{index + 1}": value for index, value in enumerate(cells)},
                "editor_url": urljoin(source_url, edit_link["href"]),
            })
    if not configurations:
        raise ValueError("Keine Kabinenkonfigurationen mit Bearbeitungslink gefunden")
    return configurations


def export_cabins(output_path: Path, *, username: str, password: str, login_url: str, cabins_url: str,
                  timeout: int = 30, user_agent: str = DEFAULT_USER_AGENT,
                  session: requests.Session | None = None) -> dict[str, Any]:
    session = session or requests.Session()
    session.headers.update({"User-Agent": user_agent})
    login(session, login_url, username, password, timeout, cabins_url)
    response = session.get(cabins_url, timeout=timeout)
    response.raise_for_status()
    configurations = extract_cabin_summaries(response.text, response.url)
    for configuration in configurations:
        editor_response = session.get(configuration["editor_url"], timeout=timeout)
        editor_response.raise_for_status()
        configuration["details"] = extract_cabin_editor(editor_response.text, editor_response.url)
    export = {"exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "source_url": response.url, "configurations": configurations}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(export, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return export


def main() -> None:
    parser = argparse.ArgumentParser(description="Exportiert eingeloggte AirlineSim-Kabinen als JSON.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    settings = get_settings()
    export = export_cabins(Path(args.output).expanduser().resolve(), username=settings["AS_USERNAME"],
                           password=settings["AS_PASSWORD"], login_url=settings["AS_LOGIN_URL"],
                           cabins_url=seating_url(settings["AS_SERVICE_PROFILES_URL"]), timeout=args.timeout)
    print(f"Fertig: {len(export['configurations'])} Kabinenkonfigurationen exportiert nach {args.output}")


if __name__ == "__main__":
    main()
