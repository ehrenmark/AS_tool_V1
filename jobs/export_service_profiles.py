"""Export authenticated AirlineSim service profiles to a local JSON file."""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "service_profiles.json"
DEFAULT_USER_AGENT = "AS-tool service-profile exporter (+local development)"
REQUIRED_SETTINGS = ("AS_USERNAME", "AS_PASSWORD")
DEFAULT_LOGIN_URL = "https://airlinesim.aero/auth/login"
AUTH_API_URL = "https://sar.simulogics.games/api/sessions"


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE settings without replacing explicit environment values."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        separator = "=" if "=" in line else ":" if ":" in line else None
        if separator is None:
            continue
        key, value = line.split(separator, 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_settings() -> dict[str, str]:
    missing = [key for key in REQUIRED_SETTINGS if not os.getenv(key)]
    if missing:
        raise ValueError("Fehlende Einstellungen in .env: " + ", ".join(missing))
    gameworld = os.getenv("AS_GAMEWORLD") or os.getenv("GAMEWORLDNAME")
    service_profiles_url = os.getenv("AS_SERVICE_PROFILES_URL")
    if service_profiles_url and "your-game-world" in service_profiles_url:
        service_profiles_url = None
    if not service_profiles_url and gameworld:
        service_profiles_url = f"https://{gameworld}.airlinesim.aero/action/enterprise/serviceProfiles"
    if not service_profiles_url:
        raise ValueError("Fehlende Einstellung in .env: AS_GAMEWORLD oder AS_SERVICE_PROFILES_URL")
    return {
        "AS_USERNAME": os.environ["AS_USERNAME"],
        "AS_PASSWORD": os.environ["AS_PASSWORD"],
        "AS_LOGIN_URL": (
            os.getenv("AS_LOGIN_URL")
            if os.getenv("AS_LOGIN_URL") and "your-game-world" not in os.environ["AS_LOGIN_URL"]
            else DEFAULT_LOGIN_URL
        ),
        "AS_SERVICE_PROFILES_URL": service_profiles_url,
    }


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def normalize_airline_id(airline_id: str | int) -> str:
    normalized = str(airline_id).strip()
    if not normalized.isdecimal() or int(normalized) <= 0:
        raise ValueError("Die Airline-ID muss eine positive ganze Zahl sein")
    return normalized


def gameworld_base_url(management_url: str) -> str:
    parsed = urlsplit(management_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Ungültige AirlineSim-URL: {management_url}")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def select_airline(
    session: requests.Session,
    management_url: str,
    airline_id: str | int,
    timeout: int,
) -> None:
    """Switch the session's active enterprise before accessing a management page."""
    response = session.get(
        f"{gameworld_base_url(management_url)}/app/enterprise/dashboard",
        params={"select": normalize_airline_id(airline_id)},
        timeout=timeout,
    )
    response.raise_for_status()


def select_login_form(soup: BeautifulSoup) -> Tag:
    password_input = soup.find("input", {"type": "password"})
    if password_input is None:
        raise ValueError("Login-Formular mit Passwortfeld nicht gefunden")
    form = password_input.find_parent("form")
    if not isinstance(form, Tag):
        raise ValueError("Login-Formular nicht gefunden")
    return form


def build_login_payload(form: Tag, username: str, password: str) -> dict[str, str]:
    payload = {
        input_tag["name"]: input_tag.get("value", "")
        for input_tag in form.select("input[name]")
        if input_tag.get("type", "").lower() not in {"submit", "button", "reset", "file"}
    }
    password_input = form.find("input", {"type": "password"})
    if not isinstance(password_input, Tag) or not password_input.get("name"):
        raise ValueError("Passwortfeld im Login-Formular ohne Namen")
    payload[password_input["name"]] = password

    username_input = next(
        (
            input_tag
            for input_tag in form.select("input[name]")
            if input_tag.get("type", "").lower() in {"text", "email"}
            and any(term in input_tag["name"].lower() for term in ("user", "email", "login"))
        ),
        None,
    )
    if not isinstance(username_input, Tag) or not username_input.get("name"):
        raise ValueError("Benutzername-Feld im Login-Formular nicht gefunden")
    payload[username_input["name"]] = username
    return payload


def login(
    session: requests.Session,
    login_url: str,
    username: str,
    password: str,
    timeout: int,
    destination_url: str,
) -> None:
    if login_url.rstrip("/") == DEFAULT_LOGIN_URL:
        response = session.post(
            AUTH_API_URL,
            json={
                "method": "password",
                "brand": "as",
                "login": username,
                "password": password,
                "metadata": {},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        session_data = response.json()
        session_token = session_data.get("token")
        if not session_token:
            raise ValueError("AirlineSim-Login lieferte keinen Session-Token")
        session.cookies.set("as-sid", session_token, domain="airlinesim.aero", path="/")
        session.cookies.set("as-sid", session_token, domain=urlparse(destination_url).hostname, path="/")
        return
    response = session.get(login_url, params={"od": destination_url}, timeout=timeout)
    response.raise_for_status()
    form = select_login_form(BeautifulSoup(response.text, "html.parser"))
    action = urljoin(response.url, form.get("action") or response.url)
    response = session.post(action, data=build_login_payload(form, username, password), timeout=timeout)
    response.raise_for_status()


def table_records(table: Tag) -> list[dict[str, str]]:
    headers = [normalize_text(cell.get_text(" ", strip=True)) for cell in table.select("thead th")]
    rows = []
    for row in table.select("tbody tr") or table.select("tr"):
        cells = [normalize_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"], recursive=False)]
        if not cells or all(not cell for cell in cells):
            continue
        keys = headers if len(headers) == len(cells) else [f"column_{index + 1}" for index in range(len(cells))]
        if cells != headers:
            rows.append(dict(zip(keys, cells)))
    return rows


def extract_fields(container: Tag) -> dict[str, str]:
    fields = {}
    for row in container.select("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if len(cells) == 2 and cells[0].name == "th" and not row.find_parent("thead"):
            label = normalize_text(cells[0].get_text(" ", strip=True)).rstrip(":")
            value = normalize_text(cells[1].get_text(" ", strip=True))
            if label and value:
                fields[label] = value
    for term in container.select("dt"):
        definition = term.find_next_sibling("dd")
        label = normalize_text(term.get_text(" ", strip=True)).rstrip(":")
        if label and definition:
            fields[label] = normalize_text(definition.get_text(" ", strip=True))
    return fields


def profile_from_container(container: Tag, index: int) -> dict[str, Any]:
    title = container.select_one("h1, h2, h3, h4, h5, .profile-name, .name")
    name = normalize_text(title.get_text(" ", strip=True)) if title else f"service-profile-{index}"
    fields = extract_fields(container)
    records = [record for table in container.find_all("table") for record in table_records(table)]
    return {"name": name, "fields": fields, "records": records}


def detail_links(row: Tag, source_url: str) -> list[dict[str, str]]:
    links = []
    for anchor in row.select("a[href]"):
        label = normalize_text(anchor.get_text(" ", strip=True)).casefold()
        href = anchor["href"]
        if href and any(word in label for word in ("bearbeiten", "edit", "info")):
            links.append({"label": label, "url": urljoin(source_url, href)})
    return links


def extract_profile_summaries(html: str, source_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    profiles = []
    for row in soup.select("table tr"):
        links = detail_links(row, source_url)
        if not links:
            continue
        cells = [normalize_text(cell.get_text(" ", strip=True)) for cell in row.find_all("td", recursive=False)]
        if not cells:
            continue
        name = cells[0]
        if not name:
            continue
        minimum_route = next(
            (cell for cell in cells if "km" in cell.casefold() or "mi" in cell.casefold()),
            None,
        )
        profiles.append({
            "name": name,
            "minimum_route": minimum_route,
            "overview": {f"column_{index + 1}": value for index, value in enumerate(cells)},
            "detail_links": links,
        })
    if not profiles:
        raise ValueError("Keine Serviceprofile mit Bearbeiten- oder Info-Link gefunden")
    return profiles


def extract_detail_page(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    fields = extract_fields(soup)
    form_values = {}
    for control in soup.select("input[name], select[name], textarea[name]"):
        name = control["name"]
        if control.get("type", "").lower() in {"password", "hidden"} or "csrf" in name.casefold():
            continue
        if control.name == "select":
            selected = control.select_one("option[selected]") or control.select_one("option")
            value = normalize_text(selected.get_text(" ", strip=True)) if selected else ""
        elif control.name == "textarea":
            value = control.get_text()
        elif control.get("type", "").lower() in {"checkbox", "radio"}:
            if not control.has_attr("checked"):
                continue
            value = control.get("value", "on")
        else:
            value = control.get("value", "")
        form_values[name] = value
    tables = [table_records(table) for table in soup.find_all("table")]
    return {
        "title": normalize_text(soup.title.get_text(" ", strip=True)) if soup.title else None,
        "fields": fields,
        "form_values": form_values,
        "tables": tables,
    }


def extract_service_profiles(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    matches = soup.select("[data-service-profile], .service-profile, .service_profile")
    containers = [
        match for match in matches
        if not any(parent in matches for parent in match.parents if isinstance(parent, Tag))
    ]
    if containers:
        return [profile_from_container(container, index) for index, container in enumerate(containers, start=1)]

    # Some game versions present profiles as a single management table.
    records = [record for table in soup.find_all("table") for record in table_records(table)]
    if records:
        return [{"name": "service-profiles", "fields": {}, "records": records}]
    raise ValueError("Keine Serviceprofile auf der abgerufenen Seite gefunden")


def export_service_profiles(
    output_path: Path,
    *,
    username: str,
    password: str,
    login_url: str,
    service_profiles_url: str,
    timeout: int = 30,
    user_agent: str = DEFAULT_USER_AGENT,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    session = session or requests.Session()
    session.headers.update({"User-Agent": user_agent})
    login(session, login_url, username, password, timeout, service_profiles_url)
    response = session.get(service_profiles_url, timeout=timeout)
    response.raise_for_status()
    profiles = extract_profile_summaries(response.text, response.url)
    for profile in profiles:
        details = {}
        for link in profile["detail_links"]:
            detail_response = session.get(link["url"], timeout=timeout)
            detail_response.raise_for_status()
            details[link["label"]] = {
                "source_url": detail_response.url,
                **extract_detail_page(detail_response.text),
            }
        profile["details"] = details
    export = {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_url": response.url,
        "profiles": profiles,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(export, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return export


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Exportiert eingeloggte AirlineSim-Serviceprofile als JSON.")
    parser.add_argument("--output", default=os.getenv("AS_SERVICE_PROFILES_OUTPUT", str(DEFAULT_OUTPUT_PATH)))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    settings = get_settings()
    output_path = Path(args.output).expanduser().resolve()
    export = export_service_profiles(
        output_path,
        username=settings["AS_USERNAME"],
        password=settings["AS_PASSWORD"],
        login_url=settings["AS_LOGIN_URL"],
        service_profiles_url=settings["AS_SERVICE_PROFILES_URL"],
        timeout=args.timeout,
        user_agent=args.user_agent,
    )
    print(f"Fertig: {len(export['profiles'])} Serviceprofile exportiert nach {output_path}")


if __name__ == "__main__":
    main()
