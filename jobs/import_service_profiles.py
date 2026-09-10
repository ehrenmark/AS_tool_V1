"""Import local AirlineSim service-profile exports into another game world."""

import argparse
import json
import os
import sys
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
    extract_profile_summaries,
    get_settings,
    load_dotenv,
    login,
)


DEFAULT_INPUT_PATH = PROJECT_ROOT / "data" / "service_profiles.json"
CREATE_FORM_ACTION = "servicenewprofile"
CHANGE_FORM_ACTION = "servicechange"


def get_import_settings() -> dict[str, str]:
    settings = get_settings()
    gameworld = os.getenv("GAMEWORLDNAME_EXPORT_PROFILES")
    if not gameworld:
        raise ValueError("Fehlende Einstellung in .env: GAMEWORLDNAME_EXPORT_PROFILES")
    settings["AS_TARGET_SERVICE_PROFILES_URL"] = (
        f"https://{gameworld}.airlinesim.aero/action/enterprise/serviceProfiles"
    )
    return settings


def load_profiles(input_path: Path) -> list[dict[str, Any]]:
    try:
        export = json.loads(input_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"Exportdatei nicht gefunden: {input_path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Ungültige JSON-Exportdatei: {input_path}") from error
    profiles = export.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("Die Exportdatei enthält keine Serviceprofile")
    names = []
    for profile in profiles:
        name = profile.get("name") if isinstance(profile, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Ein exportiertes Serviceprofil hat keinen gültigen Namen")
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError("Die Exportdatei enthält mehrere Profile mit demselben Namen")
    return profiles


def find_form(soup: BeautifulSoup, action_name: str) -> Tag:
    for form in soup.select("form[action]"):
        if form.get("action", "").rstrip("/").casefold().endswith(action_name):
            return form
    raise ValueError(f"AirlineSim-Formular '{action_name}' nicht gefunden")


def submit_form(
    session: requests.Session,
    response_url: str,
    form: Tag,
    payload: dict[str, str],
    timeout: int,
) -> requests.Response:
    action = urljoin(response_url, form.get("action") or response_url)
    method = form.get("method", "get").casefold()
    if method == "post":
        response = session.post(action, data=payload, timeout=timeout)
    elif method == "get":
        response = session.get(action, params=payload, timeout=timeout)
    else:
        raise ValueError(f"Nicht unterstützte Formularmethode: {method}")
    response.raise_for_status()
    return response


def saved_form_values(profile: dict[str, Any]) -> dict[str, str]:
    details = profile.get("details")
    if not isinstance(details, dict):
        raise ValueError(f"Profil '{profile['name']}' enthält keine Detaildaten")
    for detail in details.values():
        values = detail.get("form_values") if isinstance(detail, dict) else None
        if isinstance(values, dict) and values:
            return {name: str(value) for name, value in values.items() if name != "query"}
    raise ValueError(f"Profil '{profile['name']}' enthält keine importierbaren Formularwerte")


def change_payload(form: Tag, values: dict[str, str], profile_name: str) -> dict[str, str]:
    controls = form.select("input[name], select[name], textarea[name]")
    allowed_names = {control["name"] for control in controls}
    payload = {
        control["name"]: control.get("value", "")
        for control in controls
        if control.name == "input" and control.get("type", "").casefold() == "hidden"
    }
    if "id" not in payload:
        raise ValueError(f"Bearbeitungsformular für '{profile_name}' enthält keine Profil-ID")
    for name, value in values.items():
        if name not in allowed_names:
            continue
        matching_controls = [control for control in controls if control["name"] == name]
        valid_choices = {
            control.get("value", "") for control in matching_controls
            if control.name == "input" and control.get("type", "").casefold() in {"radio", "checkbox"}
        }
        if valid_choices and value not in valid_choices:
            raise ValueError(f"Wert '{value}' für '{name}' ist für '{profile_name}' nicht verfügbar")
        payload[name] = value
    if not allowed_names.intersection(values):
        raise ValueError(f"Keine exportierten Formularwerte passen zu '{profile_name}' in der Zielwelt")
    return payload


def service_profile_map(html: str, source_url: str) -> dict[str, dict[str, Any]]:
    return {profile["name"]: profile for profile in extract_profile_summaries(html, source_url)}


def import_service_profiles(
    input_path: Path,
    *,
    username: str,
    password: str,
    login_url: str,
    service_profiles_url: str,
    apply: bool = False,
    timeout: int = 30,
    user_agent: str = DEFAULT_USER_AGENT,
    session: requests.Session | None = None,
) -> dict[str, list[str]]:
    profiles = load_profiles(input_path)
    session = session or requests.Session()
    session.headers.update({"User-Agent": user_agent})
    login(session, login_url, username, password, timeout, service_profiles_url)
    overview = session.get(service_profiles_url, timeout=timeout)
    overview.raise_for_status()
    existing = service_profile_map(overview.text, overview.url)
    result = {"created": [], "skipped_existing": [], "would_create": []}
    create_form = find_form(BeautifulSoup(overview.text, "html.parser"), CREATE_FORM_ACTION)

    for profile in profiles:
        name = profile["name"]
        if name in existing:
            result["skipped_existing"].append(name)
            continue
        if not apply:
            result["would_create"].append(name)
            continue
        submit_form(session, overview.url, create_form, {"name": name}, timeout)
        current_overview = session.get(service_profiles_url, timeout=timeout)
        current_overview.raise_for_status()
        existing = service_profile_map(current_overview.text, current_overview.url)
        created_profile = existing.get(name)
        if created_profile is None:
            raise ValueError(f"AirlineSim hat das Profil '{name}' nicht angelegt")
        detail_url = next(
            (link["url"] for link in created_profile["detail_links"] if "bearbeiten" in link["label"] or "edit" in link["label"]),
            None,
        )
        if detail_url is None:
            raise ValueError(f"Kein Bearbeitungslink für das neue Profil '{name}' gefunden")
        detail_response = session.get(detail_url, timeout=timeout)
        detail_response.raise_for_status()
        change_form = find_form(BeautifulSoup(detail_response.text, "html.parser"), CHANGE_FORM_ACTION)
        submit_form(
            session,
            detail_response.url,
            change_form,
            change_payload(change_form, saved_form_values(profile), name),
            timeout,
        )
        result["created"].append(name)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Importiert AirlineSim-Serviceprofile in eine Zielspielwelt.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT_PATH), help="Pfad zur Serviceprofil-Exportdatei")
    parser.add_argument("--apply", action="store_true", help="Profile tatsächlich anlegen und konfigurieren")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    settings = get_import_settings()
    result = import_service_profiles(
        Path(args.input).expanduser().resolve(),
        username=settings["AS_USERNAME"],
        password=settings["AS_PASSWORD"],
        login_url=settings["AS_LOGIN_URL"],
        service_profiles_url=settings["AS_TARGET_SERVICE_PROFILES_URL"],
        apply=args.apply,
        timeout=args.timeout,
        user_agent=args.user_agent,
    )
    if args.apply:
        print(f"Fertig: {len(result['created'])} Profile erstellt, {len(result['skipped_existing'])} bereits vorhanden.")
    else:
        print(
            f"Vorschau: {len(result['would_create'])} Profile würden erstellt, "
            f"{len(result['skipped_existing'])} sind bereits vorhanden. Für den Import --apply verwenden."
        )


if __name__ == "__main__":
    main()
