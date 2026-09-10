"""Import local AirlineSim cabin configuration exports into another game world."""

import argparse
import re
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs.export_cabins import extract_cabin_summaries, table_fields
from jobs.export_service_profiles import (
    DEFAULT_USER_AGENT,
    PROJECT_ROOT,
    get_settings,
    load_dotenv,
    login,
    normalize_text,
)
from jobs.import_service_profiles import find_form, submit_form


DEFAULT_INPUT_PATH = PROJECT_ROOT / "data" / "cabins.json"
CREATE_FORM_ACTION = "create~any"
EDITOR_FORM_ACTION = "editor~form"
VERIFICATION_ATTEMPTS = 3


def get_import_settings() -> dict[str, str]:
    settings = get_settings()
    gameworld = os.getenv("GAMEWORLDNAME_EXPORT_PROFILES")
    if not gameworld:
        raise ValueError("Fehlende Einstellung in .env: GAMEWORLDNAME_EXPORT_PROFILES")
    settings["AS_TARGET_CABINS_URL"] = f"https://{gameworld}.airlinesim.aero/app/seating"
    return settings


def load_configurations(input_path: Path) -> list[dict[str, Any]]:
    try:
        export = json.loads(input_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"Exportdatei nicht gefunden: {input_path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Ungültige JSON-Exportdatei: {input_path}") from error
    configurations = export.get("configurations")
    if not isinstance(configurations, list) or not configurations:
        raise ValueError("Die Exportdatei enthält keine Kabinenkonfigurationen")
    names = []
    for configuration in configurations:
        name = configuration.get("name") if isinstance(configuration, dict) else None
        aircraft_type = configuration.get("aircraft_type") if isinstance(configuration, dict) else None
        details = configuration.get("details") if isinstance(configuration, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Eine exportierte Kabinenkonfiguration hat keinen gültigen Namen")
        if not isinstance(aircraft_type, str) or not aircraft_type.strip():
            raise ValueError(f"Kabinenkonfiguration '{name}' hat keinen gültigen Flugzeugtyp")
        if not isinstance(details, dict) or not isinstance(details.get("form_values"), dict):
            raise ValueError(f"Kabinenkonfiguration '{name}' enthält keine importierbaren Detaildaten")
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError("Die Exportdatei enthält mehrere Kabinenkonfigurationen mit demselben Namen")
    return configurations


def cabin_map(html: str, source_url: str) -> dict[str, dict[str, Any]]:
    try:
        configurations = extract_cabin_summaries(html, source_url)
    except ValueError as error:
        if str(error) != "Keine Kabinenkonfigurationen mit Bearbeitungslink gefunden":
            raise
        configurations = []
    return {configuration["name"]: configuration for configuration in configurations}


def aircraft_type_options(form: Tag) -> dict[str, str]:
    select = next(
        (control for control in form.select("select[name]") if "type" in control["name"].casefold()),
        None,
    )
    if select is None:
        raise ValueError("Erstellungsformular enthält keine Flugzeugtyp-Auswahl")
    options = {
        normalize_text(option.get_text(" ", strip=True)): option.get("value", "")
        for option in select.select("option[value]")
        if normalize_text(option.get_text(" ", strip=True)) and option.get("value", "")
    }
    if not options:
        raise ValueError("Die Zielwelt enthält keine auswählbaren Flugzeugtypen")
    return options


def create_payload(form: Tag, name: str, aircraft_type: str) -> dict[str, str]:
    name_input = next(
        (control for control in form.select("input[name]") if "name" in control["name"].casefold()),
        None,
    )
    if name_input is None:
        raise ValueError("Erstellungsformular enthält kein Namensfeld")
    options = aircraft_type_options(form)
    target_type = options.get(normalize_text(aircraft_type))
    if target_type is None:
        raise ValueError(f"Flugzeugtyp '{aircraft_type}' ist in der Zielwelt nicht verfügbar")
    return {name_input["name"]: name, next(control["name"] for control in form.select("select[name]") if "type" in control["name"].casefold()): target_type}


def editor_payload(form: Tag, values: dict[str, Any], configuration_name: str) -> dict[str, str]:
    controls = form.select("input[name], select[name], textarea[name]")
    allowed_names = {control["name"] for control in controls}
    payload = {
        control["name"]: control.get("value", "")
        for control in controls
        if control.name == "input" and control.get("type", "").casefold() == "hidden"
    }
    exported_values = {
        name: str(value)
        for name, value in values.items()
        if name != "searchQuery" and name in allowed_names
    }
    if not exported_values:
        raise ValueError(f"Keine exportierten Formularwerte passen zu '{configuration_name}' in der Zielwelt")
    for name, value in exported_values.items():
        select = next((control for control in controls if control.name == "select" and control["name"] == name), None)
        if select is not None:
            choices = {option.get("value", "") for option in select.select("option[value]")}
            if value not in choices:
                raise ValueError(f"Wert '{value}' für '{name}' ist für '{configuration_name}' nicht verfügbar")
        inputs = [control for control in controls if control.name == "input" and control["name"] == name]
        choice_values = {
            control.get("value", "")
            for control in inputs
            if control.get("type", "").casefold() in {"radio", "checkbox"}
        }
        if choice_values and value not in choice_values:
            raise ValueError(f"Wert '{value}' für '{name}' ist für '{configuration_name}' nicht verfügbar")
        payload[name] = value
    return payload


def find_editor_form(soup: BeautifulSoup) -> Tag:
    """Find the editor form; Wicket adds component parameters to its action."""
    form = next(
        (form for form in soup.select("form[action]") if EDITOR_FORM_ACTION in form.get("action", "").casefold()),
        None,
    )
    if form is None:
        raise ValueError(f"AirlineSim-Formular '{EDITOR_FORM_ACTION}' nicht gefunden")
    return form


def selected_value(control: Tag) -> str:
    if control.name == "input" and control.get("type", "").casefold() in {"checkbox", "radio"}:
        return control.get("value", "") if control.has_attr("checked") else ""
    if control.name == "select":
        option = control.select_one("option[selected]") or control.select_one("option")
        return option.get("value", "") if option else ""
    if control.name == "textarea":
        return control.get_text()
    return control.get("value", "")


def cabin_class_code(label: str) -> str:
    match = re.search(r"\(([A-Za-z])\)", label)
    if match:
        return match.group(1).upper()
    normalized = normalize_text(label).casefold()
    codes = {"first class": "F", "business class": "C", "economy class": "Y"}
    for class_name, code in codes.items():
        if class_name in normalized:
            return code
    raise ValueError(f"Unbekannte Kabinenklasse '{label}'")


def required_cabin_classes(configuration: dict[str, Any]) -> list[str]:
    cabins = configuration["details"].get("cabins", [])
    if not isinstance(cabins, list):
        raise ValueError(f"Kabinenkonfiguration '{configuration['name']}' enthält ungültige Klassen")
    codes = []
    for cabin in cabins:
        label = cabin.get("class") if isinstance(cabin, dict) else None
        if not isinstance(label, str):
            raise ValueError(f"Kabinenkonfiguration '{configuration['name']}' enthält eine ungültige Klasse")
        code = cabin_class_code(label)
        if code not in codes:
            codes.append(code)
    return codes


def editor_class_codes(form: Tag) -> set[str]:
    return {
        cabin_class_code(heading.get_text(" ", strip=True))
        for heading in form.select(".cabin h3")
    }


def required_seat_types(configuration: dict[str, Any]) -> dict[str, str]:
    seat_types = {}
    for cabin in configuration["details"].get("cabins", []):
        if not isinstance(cabin, dict):
            continue
        fields = cabin.get("fields")
        label = cabin.get("class")
        if not isinstance(fields, dict) or not isinstance(label, str):
            continue
        seat_type = fields.get("Sitz")
        if isinstance(seat_type, str) and seat_type.strip():
            seat_types[cabin_class_code(label)] = re.sub(r"\s*\[.*\]\s*$", "", seat_type).strip()
    return seat_types


def editor_seat_types(form: Tag) -> dict[str, str]:
    seat_types = {}
    for cabin in form.select(".cabin"):
        heading = cabin.select_one("h3")
        table = cabin.select_one("table")
        if heading is None or table is None:
            continue
        seat_type = table_fields(table).get("Sitz")
        if seat_type:
            seat_types[cabin_class_code(heading.get_text(" ", strip=True))] = re.sub(r"\s*\[.*\]\s*$", "", seat_type).strip()
    return seat_types


def cabin_add_url(form: Tag, editor_url: str) -> str:
    document = form.find_parent("html") or form.parent or form
    for script in document.find_all("script"):
        match = re.search(r'"u"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*cabins~add-cabins[^"\\]*)"', script.get_text())
        if match:
            return urljoin(editor_url, match.group(1).replace("\\/", "/"))
    raise ValueError("Wicket-AJAX-URL zum Hinzufügen einer Kabinenklasse nicht gefunden")


def wicket_ajax_url(document: Tag, editor_url: str, component: str) -> str:
    pattern = re.compile(r'"u"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*' + re.escape(component) + r'[^"\\]*)"')
    root = document.find_parent("html") or document.parent or document
    for script in root.find_all("script"):
        match = pattern.search(script.get_text())
        if match:
            return urljoin(editor_url, match.group(1).replace("\\/", "/"))
    raise ValueError(f"Wicket-AJAX-URL für '{component}' nicht gefunden")


def select_cabin_seat(
    session: requests.Session, editor_url: str, form: Tag, index: int, seat_type: str, timeout: int,
) -> None:
    selector_url = wicket_ajax_url(form, editor_url, f"cabins-{index}-seat~select")
    headers = {"Wicket-Ajax": "true", "Wicket-Ajax-BaseURL": editor_url}
    response = session.post(selector_url, headers=headers, timeout=timeout)
    response.raise_for_status()
    component_match = re.search(r"<component\b[^>]*><!\[CDATA\[(.*?)\]\]></component>", response.text, re.DOTALL)
    if component_match is None:
        raise ValueError("AirlineSim-Sitzauswahl konnte nicht geöffnet werden")
    dialog = BeautifulSoup(component_match.group(1), "html.parser")
    choices = {
        normalize_text(row.select_one(".name").get_text(" ", strip=True)): link.get("id", "")
        for row in dialog.select("tr")
        if row.select_one(".name") is not None
        for link in [row.select_one("a[id]")]
        if link is not None
    }
    choice_id = choices.get(normalize_text(seat_type))
    if not choice_id:
        raise ValueError(f"Sitzart '{seat_type}' ist für die Zielkabine nicht verfügbar")
    choice_pattern = re.compile(
        r'Wicket\.Ajax\.ajax\(\{[^}]*"u"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*seats-\d+-select[^"\\]*)"[^}]*"c"\s*:\s*"'
        + re.escape(choice_id) + r'"'
    )
    choice_match = choice_pattern.search(response.text)
    if choice_match is None:
        raise ValueError(f"Wicket-AJAX-URL für Sitzart '{seat_type}' nicht gefunden")
    selection = session.post(
        urljoin(editor_url, "/app/" + re.sub(r"^(?:\.\./)+", "", choice_match.group(1)).replace("\\/", "/")), headers=headers, timeout=timeout,
    )
    selection.raise_for_status()


def apply_seat_types(
    session: requests.Session, editor_url: str, form: Tag, seat_types: dict[str, str], timeout: int,
) -> Tag:
    current_form = form
    for index, cabin in enumerate(current_form.select(".cabin")):
        heading = cabin.select_one("h3")
        if heading is None:
            continue
        code = cabin_class_code(heading.get_text(" ", strip=True))
        seat_type = seat_types.get(code)
        if not seat_type or normalize_text(editor_seat_types(current_form).get(code, "")) == normalize_text(seat_type):
            continue
        select_cabin_seat(session, editor_url, current_form, index, seat_type, timeout)
        _, current_form = load_editor(session, editor_url, timeout)
    return current_form


def add_missing_classes(
    session: requests.Session, editor_url: str, form: Tag, required_codes: list[str], timeout: int,
) -> Tag:
    current_form = form
    for code in required_codes:
        if code in editor_class_codes(current_form):
            continue
        add_url = cabin_add_url(current_form, editor_url)
        response = session.post(
            add_url,
            data={"editor:deck:cabins-add:cabins": code},
            headers={"Wicket-Ajax": "true", "Wicket-Ajax-BaseURL": editor_url},
            timeout=timeout,
        )
        response.raise_for_status()
        editor_response = session.get(editor_url, timeout=timeout)
        editor_response.raise_for_status()
        current_form = find_editor_form(BeautifulSoup(editor_response.text, "html.parser"))
    missing = set(required_codes) - editor_class_codes(current_form)
    if missing:
        raise ValueError(f"Kabinenklassen konnten nicht hinzugefügt werden: {', '.join(sorted(missing))}")
    return current_form


def editor_matches(form: Tag, values: dict[str, Any], required_codes: list[str], seat_types: dict[str, str]) -> bool:
    controls = {control["name"]: control for control in form.select("input[name], select[name], textarea[name]")}
    expected = {name: str(value) for name, value in values.items() if name != "searchQuery"}
    matching_expected = {name: value for name, value in expected.items() if name in controls}
    return (
        set(required_codes).issubset(editor_class_codes(form))
        and bool(matching_expected)
        and all(selected_value(controls[name]) == value for name, value in matching_expected.items())
        and all(normalize_text(editor_seat_types(form).get(code, "")) == normalize_text(seat_type) for code, seat_type in seat_types.items())
    )


def load_editor(session: requests.Session, editor_url: str, timeout: int) -> tuple[requests.Response, Tag]:
    response = session.get(editor_url, timeout=timeout)
    response.raise_for_status()
    return response, find_editor_form(BeautifulSoup(response.text, "html.parser"))


def editor_was_saved(
    session: requests.Session,
    editor_url: str,
    values: dict[str, Any],
    required_codes: list[str],
    seat_types: dict[str, str],
    timeout: int,
) -> bool:
    """Allow the Wicket editor a moment to expose a just-saved configuration."""
    for attempt in range(VERIFICATION_ATTEMPTS):
        _, form = load_editor(session, editor_url, timeout)
        if editor_matches(form, values, required_codes, seat_types):
            return True
        if attempt < VERIFICATION_ATTEMPTS - 1:
            time.sleep(1)
    return False


def import_cabins(
    input_path: Path,
    *,
    username: str,
    password: str,
    login_url: str,
    cabins_url: str,
    apply: bool = False,
    timeout: int = 30,
    user_agent: str = DEFAULT_USER_AGENT,
    session: requests.Session | None = None,
) -> dict[str, list[str]]:
    configurations = load_configurations(input_path)
    session = session or requests.Session()
    session.headers.update({"User-Agent": user_agent})
    login(session, login_url, username, password, timeout, cabins_url)
    overview = session.get(cabins_url, timeout=timeout)
    overview.raise_for_status()
    existing = cabin_map(overview.text, overview.url)
    create_form = find_form(BeautifulSoup(overview.text, "html.parser"), CREATE_FORM_ACTION)
    result = {
        "created": [], "updated_existing": [], "skipped_existing": [],
        "would_create": [], "would_update": [],
    }

    for configuration in configurations:
        name = configuration["name"]
        values = configuration["details"]["form_values"]
        required_codes = required_cabin_classes(configuration)
        seat_types = required_seat_types(configuration)
        if name in existing:
            editor_response, current_form = load_editor(session, existing[name]["editor_url"], timeout)
            if editor_matches(current_form, values, required_codes, seat_types):
                result["skipped_existing"].append(name)
                continue
            if not apply:
                result["would_update"].append(name)
                continue
            configured_form = add_missing_classes(
                session, editor_response.url, current_form, required_codes, timeout,
            )
            configured_form = apply_seat_types(session, editor_response.url, configured_form, seat_types, timeout)
            submit_form(session, editor_response.url, configured_form, editor_payload(configured_form, values, name), timeout)
            if not editor_was_saved(session, editor_response.url, values, required_codes, seat_types, timeout):
                raise ValueError(f"AirlineSim hat die Kabinenkonfiguration '{name}' nicht vollständig gespeichert")
            result["updated_existing"].append(name)
            continue
        payload = create_payload(create_form, name, configuration["aircraft_type"])
        if not apply:
            result["would_create"].append(name)
            continue
        editor_response = submit_form(session, overview.url, create_form, payload, timeout)
        editor_form = find_editor_form(BeautifulSoup(editor_response.text, "html.parser"))
        editor_form = add_missing_classes(session, editor_response.url, editor_form, required_codes, timeout)
        editor_form = apply_seat_types(session, editor_response.url, editor_form, seat_types, timeout)
        submit_form(
            session,
            editor_response.url,
            editor_form,
            editor_payload(editor_form, values, name),
            timeout,
        )
        if not editor_was_saved(session, editor_response.url, values, required_codes, seat_types, timeout):
            raise ValueError(f"AirlineSim hat die Kabinenkonfiguration '{name}' nicht vollständig gespeichert")
        current_overview = session.get(cabins_url, timeout=timeout)
        current_overview.raise_for_status()
        existing = cabin_map(current_overview.text, current_overview.url)
        if name not in existing:
            raise ValueError(f"AirlineSim hat die Kabinenkonfiguration '{name}' nicht gespeichert")
        result["created"].append(name)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Importiert AirlineSim-Kabinenkonfigurationen in eine Zielspielwelt.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT_PATH), help="Pfad zur Kabinen-Exportdatei")
    parser.add_argument("--apply", action="store_true", help="Kabinen tatsächlich anlegen und konfigurieren")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    settings = get_import_settings()
    result = import_cabins(
        Path(args.input).expanduser().resolve(),
        username=settings["AS_USERNAME"],
        password=settings["AS_PASSWORD"],
        login_url=settings["AS_LOGIN_URL"],
        cabins_url=settings["AS_TARGET_CABINS_URL"],
        apply=args.apply,
        timeout=args.timeout,
        user_agent=args.user_agent,
    )
    if args.apply:
        print(
            f"Fertig: {len(result['created'])} Kabinen erstellt, "
            f"{len(result['updated_existing'])} aktualisiert, "
            f"{len(result['skipped_existing'])} unverändert."
        )
    else:
        print(
            f"Vorschau: {len(result['would_create'])} Kabinen würden erstellt, "
            f"{len(result['would_update'])} würden aktualisiert, "
            f"{len(result['skipped_existing'])} sind unverändert. Für den Import --apply verwenden."
        )


if __name__ == "__main__":
    main()
