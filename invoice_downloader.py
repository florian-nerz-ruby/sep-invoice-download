import csv
import io
import json
import os
import random
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter, Retry

from token_manager import TokenManager, load_env_config


ENVIRONMENTS = {
    "UAT": {
        "base_url": "https://eu1.api.uat.development.abovecloud.io",
        "channel_id": "0ace103a-23f5-4da4-9bd9-eb827559017d",
    },
    "PROD": {
        "base_url": "https://eu1.api.ep.shiji.world",
        "channel_id": "1b1bcc5b-73f7-43b6-ae7c-a4e1f32ce4bf",
    },
}

AVAILABLE_PROPERTIES = {
    "Ruby Rosi": "5f41a8ab-423d-40fc-92e4-2fe50d56603d",
    "Ruby Lilly": "c673ff2b-71cb-4d7d-9ec6-3d235ee7223f",
    "Ruby Ella": "ce933e98-a99b-4ce9-8cf6-2828fd551e59",
    "Ruby Luna": "2b66028f-bb1f-4c2a-80c7-94d95ecb1f87",
    "Ruby Lotti": "aff90788-0489-4063-8727-ad8da4d7adae",
    "Ruby Coco": "5947b9ba-0cb3-42f7-b6da-d50b1193d947",
    "Ruby Hanna": "dca8efe9-bfc0-4142-8270-7dcf7db3f1e0",
    "Ruby Leni": "74a38884-8165-4805-a56a-7bdb213ea6ef",
    "Ruby Louise": "305ab810-da0d-452d-a1ef-da406592c185",
    "Ruby Claire": "81c6a3a2-7559-40cf-8268-03ad05ba44ad",
    "Ruby Mimi": "e22e5e91-0dea-408f-a59e-44b706ac60f6",
    "Ruby Emma": "403c3ad5-0a36-4158-8ce6-0c1db8aed462",
    "Ruby Marie": "66ff2eec-38f1-4484-94e5-4af7cbc82f86",
    "Ruby Sofie": "7422e5f0-bb34-4e8d-8d55-11ffea0849e3",
    "Ruby Stella": "70a917b3-7439-49c6-aa25-4d56d015cffe",
    "Ruby Zoe": "f2ab77d5-9f6a-4419-bc8e-126183ca539e",
    "Ruby Lissi": "a81a9b0f-6ef2-4b23-aa69-9f747d2ad64c",
    "Ruby Lucy": "bd0ba381-4265-420c-a09f-f51fe7798c11",
    "Ruby Molly": "1896d2c5-dce4-4238-ae12-5fef5af3d894",
}

ProgressCallback = Callable[[str, Dict[str, Any]], None]


@dataclass
class DownloadConfig:
    environment: str
    properties: Dict[str, str]
    output_dir: Path
    cache_file: Path
    departure_date_from: str
    departure_date_to: str
    reservation_id: Optional[str] = None
    profile_id_filter: Optional[str] = None
    external_reference_system_ids: List[str] = field(default_factory=list)
    download_credit_notes: bool = False
    invoice_version: str = "copy"
    retry_cached_errors: bool = False
    max_reservations_per_property: Optional[int] = None
    max_pages_per_property: Optional[int] = None
    page_size: int = 50
    auth_overrides: Dict[str, str] = field(default_factory=dict)
    mode: str = "manual"
    csv_file: Optional[Path] = None
    report_file: Optional[Path] = None


@dataclass(frozen=True)
class CsvReservationRow:
    row_number: int
    main_id: str
    hotel_name: str
    guest_name: str
    arrival_date: date
    departure_date: Optional[date]
    booking_state: str


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sanitize_filename_part(value: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "-", value).strip()


def sanitize_csv_guest_filename_part(value: str) -> str:
    return re.sub(r"\s+", "-", sanitize_filename_part(value))


def unique_output_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    extension = candidate.suffix
    for suffix in range(2, 10_000):
        candidate = directory / f"{stem}_{suffix}{extension}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find an unused filename for '{filename}'.")


def _emit(callback: ProgressCallback, message: str, **details: Any) -> None:
    callback(message, details)


def _auth_config(config: DownloadConfig) -> Dict[str, str]:
    try:
        auth = load_env_config(config.environment)
    except RuntimeError:
        auth = {"ENV_NAME": config.environment}
    auth.update({key: value for key, value in config.auth_overrides.items() if value})
    required = ("TOKEN_URL", "API_VALIDATE_URL", "USERNAME", "PASSWORD", "CLIENT_ID", "CLIENT_SECRET", "TENANT_NAME")
    missing = [key for key in required if not auth.get(key)]
    if missing:
        prefix = config.environment + "_"
        raise RuntimeError(
            "Authentication is incomplete. Set the matching environment variables "
            f"or provide the missing values in the form: {', '.join(prefix + key for key in missing)}."
        )
    return auth


def make_session(config: DownloadConfig) -> requests.Session:
    session = requests.Session()
    retries = Retry(total=6, connect=6, read=6, backoff_factor=0.5,
                    status_forcelist=(500, 502, 503, 504), allowed_methods=frozenset(["GET"]),
                    raise_on_status=False)
    session.mount("https://", HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10))
    token_manager = TokenManager(_auth_config(config))
    session.token_manager = token_manager  # type: ignore[attr-defined]
    session.headers.update({"Content-Type": "application/json", "Accept": "application/json",
                            "Authorization": f"Bearer {token_manager.get_access_token()}"})
    return session


def request_with_throttle(session: requests.Session, method: str, url: str, *, callback: ProgressCallback,
                          headers: Optional[Dict[str, str]] = None, timeout: int = 30, **kwargs: Any) -> requests.Response:
    response = session.request(method, url, headers=headers or {}, timeout=timeout, **kwargs)
    if response.status_code == 401:
        token_manager = getattr(session, "token_manager", None)
        if token_manager:
            session.headers.update({"Authorization": f"Bearer {token_manager.refresh_access_token()}"})
            response = session.request(method, url, headers=headers or {}, timeout=timeout, **kwargs)
    retries = 0
    while response.status_code == 429 and retries < 8:
        raw_wait = response.headers.get("Retry-After", "10").strip()
        if raw_wait.isdigit():
            wait_seconds = int(raw_wait)
        else:
            try:
                retry_at = parsedate_to_datetime(raw_wait)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                wait_seconds = max(1, int((retry_at - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError):
                wait_seconds = 10
        wait_seconds = min(wait_seconds, 120) * (1 + random.uniform(0, 0.25))
        _emit(callback, f"Rate limited. Retrying in {wait_seconds:.1f}s.")
        time.sleep(wait_seconds)
        response = session.request(method, url, headers=headers or {}, timeout=timeout, **kwargs)
        if response.status_code == 401:
            token_manager = getattr(session, "token_manager", None)
            if token_manager:
                session.headers.update({"Authorization": f"Bearer {token_manager.refresh_access_token()}"})
                response = session.request(method, url, headers=headers or {}, timeout=timeout, **kwargs)
        retries += 1
    return response


def load_invoice_cache(cache_file: Path) -> Dict[str, Dict[str, Any]]:
    if not cache_file.exists():
        return {}
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(key): {"status": "downloaded"} for key in data}
        if isinstance(data, dict) and isinstance(data.get("keys"), list):
            return {str(key): {"status": "downloaded"} for key in data["keys"]}
        return {str(key): value if isinstance(value, dict) else {"status": str(value)}
                for key, value in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not load cache '{cache_file}': {exc}") from exc


def save_invoice_cache(cache_file: Path, cache: Dict[str, Dict[str, Any]]) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = cache_file.with_suffix(cache_file.suffix + ".tmp")
    temporary_file.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary_file.replace(cache_file)


def normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value.strip())
    without_diacritics = "".join(char for char in normalized if not unicodedata.combining(char))
    simplified = without_diacritics.casefold().replace("ae", "a").replace("oe", "o").replace("ue", "u")
    return re.sub(r"[^a-z]", "", simplified)


def _parse_csv_date(value: str, field_name: str, row_number: int) -> date:
    for date_format in ("%Y-%m-%d", "%d-%m-%y"):
        try:
            return datetime.strptime(value.strip(), date_format).date()
        except ValueError:
            pass
    raise ValueError(f"CSV row {row_number}: {field_name} must be YYYY-MM-DD or DD-MM-YY.")


def load_csv_reservation_rows(csv_file: Path) -> List[CsvReservationRow]:
    required_headers = {"MAIN_ID", "HOTEL_NAME", "GUEST_NAME", "ARRIVAL_DATE", "DEPARTURE_DATE", "BOOKING_STATE"}
    try:
        try:
            content = csv_file.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            content = csv_file.read_text(encoding="cp1252")
        with io.StringIO(content, newline="") as source_file:
            reader = csv.DictReader(source_file)
            headers = set(reader.fieldnames or [])
            missing_headers = required_headers - headers
            if missing_headers:
                raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing_headers))}.")
            rows: List[CsvReservationRow] = []
            for row_number, row in enumerate(reader, start=2):
                if not any((value or "").strip() for value in row.values()):
                    continue
                hotel_name = (row.get("HOTEL_NAME") or "").strip()
                guest_name = (row.get("GUEST_NAME") or "").strip()
                arrival_value = (row.get("ARRIVAL_DATE") or "").strip()
                if not hotel_name or not guest_name or not arrival_value:
                    raise ValueError(f"CSV row {row_number}: HOTEL_NAME, GUEST_NAME, and ARRIVAL_DATE are required.")
                departure_value = (row.get("DEPARTURE_DATE") or "").strip()
                rows.append(CsvReservationRow(
                    row_number=row_number,
                    main_id=(row.get("MAIN_ID") or "").strip(),
                    hotel_name=hotel_name,
                    guest_name=guest_name,
                    arrival_date=_parse_csv_date(arrival_value, "ARRIVAL_DATE", row_number),
                    departure_date=_parse_csv_date(departure_value, "DEPARTURE_DATE", row_number) if departure_value else None,
                    booking_state=(row.get("BOOKING_STATE") or "").strip(),
                ))
    except OSError as exc:
        raise RuntimeError(f"Could not read CSV '{csv_file}': {exc}") from exc
    if not rows:
        raise ValueError("CSV contains no reservation rows.")
    return rows


def save_csv_report(report_file: Path, report: Dict[str, Any]) -> None:
    """Durably replace the report so each resolved CSV row survives a later failure."""
    report_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = report_file.with_suffix(report_file.suffix + ".tmp")
    temporary_file.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary_file.replace(report_file)


def _csv_report_key(row: CsvReservationRow) -> str:
    return row.main_id or f"csv-row-{row.row_number}"


def _new_csv_report(rows: List[CsvReservationRow], csv_file: Path) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": "csv_import",
        "source_file": csv_file.name,
        "started_at": now_utc_iso(),
        "completed_at": None,
        "run_error": None,
        "rows": {
            _csv_report_key(row): {
                "status": "pending",
                "outcome": "pending",
                "row_number": row.row_number,
                "main_id": row.main_id,
                "hotel_name": row.hotel_name,
                "guest_name": row.guest_name,
                "normalized_guest_name": normalize_name(row.guest_name),
                "arrival_date": row.arrival_date.isoformat(),
                "departure_date": row.departure_date.isoformat() if row.departure_date else None,
                "booking_state": row.booking_state,
                "documents": [],
                "last_updated_at": now_utc_iso(),
            }
            for row in rows
        },
    }


def _set_csv_row_outcome(report: Dict[str, Any], row: CsvReservationRow, status: str, outcome: str,
                         **details: Any) -> None:
    record = report["rows"][_csv_report_key(row)]
    record.update(details)
    record["status"] = status
    record["outcome"] = outcome
    record["last_updated_at"] = now_utc_iso()


def _property_headers(property_id: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    return {**(extra or {}), "AC-Property-ID": property_id}


def get_reservations_with_folios(session: requests.Session, config: DownloadConfig, property_id: str,
                                 callback: ProgressCallback) -> List[Dict[str, Any]]:
    environment = ENVIRONMENTS[config.environment]
    filters = [f"channelId=={environment['channel_id']}",
               f"(departureDate=ge={config.departure_date_from};departureDate=le={config.departure_date_to})"]
    if config.reservation_id:
        filters.append(f"id=={config.reservation_id}")
    results: List[Dict[str, Any]] = []
    page_number = 1
    while True:
        url = (f"{environment['base_url']}/api-gateway/aggregator/v1/reservations?sort=createdAt&extend=Folios"
               f"&filter={';'.join(filters)}&pageSize={config.page_size}&pageNumber={page_number}")
        response = request_with_throttle(session, "GET", url, callback=callback,
                                         headers=_property_headers(property_id))
        if response.status_code >= 400:
            raise RuntimeError(f"Reservation request failed with HTTP {response.status_code}: {(response.text or '')[:300]}")
        payload = response.json() or {}
        page_results = payload.get("results") or []
        results.extend(page_results)
        _emit(callback, f"Fetched page {page_number}: {len(page_results)} reservation(s).", reservations_found=len(results))
        if config.reservation_id or not (payload.get("paging") or {}).get("next"):
            break
        if config.max_pages_per_property and page_number >= config.max_pages_per_property:
            _emit(callback, f"Stopped after configured page limit ({config.max_pages_per_property}).")
            break
        page_number += 1
    return results


def get_csv_window_reservations(session: requests.Session, config: DownloadConfig, property_id: str,
                                window_start: date, window_end: date,
                                callback: ProgressCallback) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Fetch a three-day arrival window and the guests embedded beside its results."""
    environment = ENVIRONMENTS[config.environment]
    filter_value = f"arrivalDate=ge={window_start.isoformat()};arrivalDate=le={window_end.isoformat()}"
    reservations: List[Dict[str, Any]] = []
    individuals_by_id: Dict[str, Dict[str, Any]] = {}
    page_number = 1
    while True:
        url = (
            f"{environment['base_url']}/api-gateway/aggregator/v1/reservations"
            f"?filter=({filter_value})&extend=Folios,Guests&pageSize={config.page_size}"
        )
        if page_number > 1:
            url += f"&pageNumber={page_number}"
        response = request_with_throttle(session, "GET", url, callback=callback,
                                         headers=_property_headers(property_id))
        if response.status_code >= 400:
            raise RuntimeError(
                f"CSV reservation request failed with HTTP {response.status_code}: {(response.text or '')[:300]}"
            )
        payload = response.json() or {}
        page_results = payload.get("results") or []
        reservations.extend(page_results)
        embedded = payload.get("_embedded") or {}
        for individual in embedded.get("individuals") or []:
            individual_id = individual.get("id")
            if individual_id:
                individuals_by_id[str(individual_id)] = individual
        _emit(callback, f"Fetched {window_start.isoformat()} to {window_end.isoformat()} page {page_number}: "
                        f"{len(page_results)} reservation(s).", reservations_found=len(reservations))
        if not (payload.get("paging") or {}).get("next"):
            break
        if config.max_pages_per_property and page_number >= config.max_pages_per_property:
            _emit(callback, f"Stopped after configured page limit ({config.max_pages_per_property}).")
            break
        page_number += 1
    return reservations, list(individuals_by_id.values())


def pick_download_folios(reservation: Dict[str, Any], config: DownloadConfig) -> List[Tuple[str, Dict[str, Any]]]:
    folios = (((reservation.get("_embedded") or {}).get("folios") or {}).get("results") or [])
    invoices: List[Dict[str, Any]] = []
    credit_notes: List[Dict[str, Any]] = []
    for folio in folios:
        if ((folio.get("folioStatusCode") or {}).get("code")) != "CO":
            continue
        if config.profile_id_filter and folio.get("profileId") != config.profile_id_filter:
            continue
        if folio.get("voidedFolioWindowId"):
            if config.download_credit_notes:
                credit_notes.append(folio)
        else:
            invoices.append(folio)
    return ([('invoice', invoices[0])] if invoices else []) + [('creditnote', item) for item in credit_notes]


def get_preferred_external_reference(reservation: Dict[str, Any], system_ids: List[str]) -> Optional[str]:
    references = {str(item.get("systemId")): item.get("number") for item in reservation.get("externalIds") or []
                  if item.get("systemId") and item.get("number")}
    return next((references[system_id] for system_id in system_ids if system_id in references), None)


def get_invoice_content(session: requests.Session, config: DownloadConfig, property_id: str, account_id: str,
                        folio_id: str, callback: ProgressCallback) -> bytes:
    base_url = ENVIRONMENTS[config.environment]["base_url"]
    if config.invoice_version not in {"copy", "original"}:
        raise ValueError("Invoice version must be copy or original.")
    content_path = "copy/content" if config.invoice_version == "copy" else "final/content"
    url = f"{base_url}/api-gateway/cashiering/v1/accounts/{account_id}/folios/{folio_id}/correspondences/invoice/{content_path}"
    response = request_with_throttle(session, "GET", url, callback=callback,
                                     headers=_property_headers(property_id, {"Accept": "application/pdf"}), timeout=60)
    if response.status_code >= 400:
        try:
            error = response.json() or {}
            details = error.get("details") or [{}]
            message = details[0].get("message") or error.get("message") or response.text[:300]
        except ValueError:
            message = response.text[:300]
        raise RuntimeError(f"Invoice download failed with HTTP {response.status_code}: {message}")
    return response.content


def cache_key(property_id: str, doc_type: str, invoice_number: Optional[str], confirmation_number: Optional[str], folio_id: Optional[str]) -> str:
    return "::".join([property_id, doc_type, invoice_number or "", confirmation_number or "", folio_id or ""])


def _group_csv_rows(rows: List[CsvReservationRow], property_lookup: Dict[str, Tuple[str, str]]) -> Dict[Tuple[str, str], List[List[CsvReservationRow]]]:
    by_property: Dict[Tuple[str, str], List[CsvReservationRow]] = {}
    for row in rows:
        resolved_property = property_lookup.get(normalize_name(row.hotel_name))
        if resolved_property:
            by_property.setdefault(resolved_property, []).append(row)
    grouped: Dict[Tuple[str, str], List[List[CsvReservationRow]]] = {}
    for property_key, property_rows in by_property.items():
        windows: List[List[CsvReservationRow]] = []
        for row in sorted(property_rows, key=lambda item: (item.arrival_date, item.row_number)):
            if not windows or (row.arrival_date - windows[-1][0].arrival_date).days > 2:
                windows.append([row])
            else:
                windows[-1].append(row)
        grouped[property_key] = windows
    return grouped


def _individual_ids_by_name(individuals: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    matches: Dict[str, List[str]] = {}
    for individual in individuals:
        details = individual.get("details") or {}
        individual_id = individual.get("id")
        full_name = " ".join(part for part in (details.get("firstName"), details.get("lastName")) if part)
        normalized = normalize_name(full_name)
        if individual_id and normalized:
            matches.setdefault(normalized, []).append(str(individual_id))
    return matches


def run_csv_import(config: DownloadConfig, callback: ProgressCallback) -> Dict[str, int]:
    if config.environment not in ENVIRONMENTS:
        raise ValueError("Environment must be UAT or PROD.")
    if not config.csv_file or not config.report_file:
        raise ValueError("CSV import requires an uploaded CSV and a report file.")

    rows = load_csv_reservation_rows(config.csv_file)
    report = _new_csv_report(rows, config.csv_file)
    save_csv_report(config.report_file, report)
    totals = {"downloaded": 0, "skipped": 0, "cached": 0, "reservations_found": 0, "processed": 0}
    property_lookup = {normalize_name(name): (name, property_id) for name, property_id in AVAILABLE_PROPERTIES.items()}
    cache = load_invoice_cache(config.cache_file)

    try:
        _emit(callback, f"Starting CSV import for {len(rows)} row(s).", **totals)
        for row in rows:
            if normalize_name(row.hotel_name) not in property_lookup:
                totals["skipped"] += 1
                _set_csv_row_outcome(report, row, "skipped", "unknown_property", error_message=f"No SEP property matches '{row.hotel_name}'.")
                save_csv_report(config.report_file, report)

        session = make_session(config)
        for (property_name, property_id), windows in _group_csv_rows(rows, property_lookup).items():
            for window_rows in windows:
                window_start = window_rows[0].arrival_date
                window_end = window_rows[-1].arrival_date
                _emit(callback, f"{property_name}: fetching CSV arrival window {window_start} to {window_end}.",
                      property_name=property_name, **totals)
                try:
                    reservations, individuals = get_csv_window_reservations(
                        session, config, property_id, window_start, window_end, callback
                    )
                except Exception as exc:
                    for row in window_rows:
                        totals["skipped"] += 1
                        _set_csv_row_outcome(report, row, "error", "reservation_query_failed", property_id=property_id,
                                             error_message=str(exc))
                        save_csv_report(config.report_file, report)
                    _emit(callback, f"{property_name}: window query failed: {exc}", property_name=property_name, **totals)
                    continue

                totals["reservations_found"] += len(reservations)
                individual_ids_by_name = _individual_ids_by_name(individuals)
                for row in window_rows:
                    totals["processed"] += 1
                    normalized_guest = normalize_name(row.guest_name)
                    matching_profile_ids = individual_ids_by_name.get(normalized_guest, [])
                    if not matching_profile_ids:
                        totals["skipped"] += 1
                        _set_csv_row_outcome(report, row, "skipped", "individual_not_found", property_id=property_id)
                        save_csv_report(config.report_file, report)
                        continue
                    candidates = [
                        reservation for reservation in reservations
                        if (reservation.get("arrivalDate") or "")[:10] == row.arrival_date.isoformat()
                        and str(reservation.get("profileId") or "") in matching_profile_ids
                    ]
                    if not candidates:
                        totals["skipped"] += 1
                        _set_csv_row_outcome(report, row, "skipped", "reservation_not_found", property_id=property_id,
                                             matching_profile_ids=matching_profile_ids)
                        save_csv_report(config.report_file, report)
                        continue
                    if len(candidates) > 1:
                        totals["skipped"] += 1
                        _set_csv_row_outcome(
                            report, row, "skipped", "ambiguous_reservation", property_id=property_id,
                            matching_profile_ids=matching_profile_ids,
                            candidate_reservation_ids=[candidate.get("id") for candidate in candidates],
                        )
                        save_csv_report(config.report_file, report)
                        continue

                    reservation = candidates[0]
                    reservation_id = reservation.get("id")
                    profile_id = reservation.get("profileId")
                    account_id = reservation.get("accountId")
                    if not account_id:
                        totals["skipped"] += 1
                        _set_csv_row_outcome(report, row, "skipped", "missing_account_id", property_id=property_id,
                                             reservation_id=reservation_id, profile_id=profile_id)
                        save_csv_report(config.report_file, report)
                        continue
                    targets = pick_download_folios(reservation, config)
                    if not targets:
                        totals["skipped"] += 1
                        _set_csv_row_outcome(report, row, "skipped", "no_downloadable_folio", property_id=property_id,
                                             reservation_id=reservation_id, profile_id=profile_id)
                        save_csv_report(config.report_file, report)
                        continue

                    confirmation = reservation.get("confirmationNumber")
                    external_reference = get_preferred_external_reference(reservation, config.external_reference_system_ids)
                    departure = (reservation.get("departureDate") or "unknown")[:10]
                    documents: List[Dict[str, Any]] = []
                    for doc_type, folio in targets:
                        folio_id = folio.get("id")
                        invoice_number = folio.get("invoiceNumber")
                        document: Dict[str, Any] = {
                            "doc_type": doc_type,
                            "invoice_version": config.invoice_version,
                            "property_id": property_id,
                            "reservation_id": reservation_id,
                            "profile_id": profile_id,
                            "folio_id": folio_id,
                            "invoice_number": invoice_number,
                            "confirmation_number": confirmation,
                            "external_reference": external_reference,
                            "departure_date": departure,
                        }
                        if not folio_id:
                            document.update(status="skipped", outcome="missing_folio_id")
                            documents.append(document)
                            totals["skipped"] += 1
                            continue
                        key = cache_key(property_id, doc_type, invoice_number, confirmation, folio_id)
                        existing = cache.get(key)
                        if existing and (existing.get("status") == "downloaded" or not config.retry_cached_errors):
                            outcome = "cache_hit" if existing.get("status") == "downloaded" else "cached_error"
                            document.update(status="cached", outcome=outcome, cache_record=existing)
                            documents.append(document)
                            totals["cached"] += 1
                            continue
                        try:
                            content = get_invoice_content(session, config, property_id, account_id, folio_id, callback)
                            filename = (
                                f"{sanitize_filename_part(row.main_id or 'csv-row-' + str(row.row_number))}_"
                                f"{sanitize_filename_part(invoice_number or 'folio_' + str(folio.get('number') or 'unknown'))}_"
                                f"Arrival-{row.arrival_date.isoformat()}_{sanitize_csv_guest_filename_part(row.guest_name)}.pdf"
                            )
                            output_dir = config.output_dir / sanitize_filename_part(property_name)
                            output_dir.mkdir(parents=True, exist_ok=True)
                            output_path = unique_output_path(output_dir, filename)
                            output_path.write_bytes(content)
                            cache[key] = {**document, "invoice_version": config.invoice_version, "status": "downloaded", "file_path": str(output_path),
                                          "last_attempt_at": now_utc_iso()}
                            save_invoice_cache(config.cache_file, cache)
                            document.update(status="downloaded", outcome="downloaded", file_path=str(output_path))
                            documents.append(document)
                            totals["downloaded"] += 1
                        except Exception as exc:
                            cache[key] = {**document, "status": "error", "error_message": str(exc),
                                          "last_attempt_at": now_utc_iso()}
                            save_invoice_cache(config.cache_file, cache)
                            document.update(status="error", outcome="download_failed", error_message=str(exc))
                            documents.append(document)
                            totals["skipped"] += 1

                    outcomes = {document["outcome"] for document in documents}
                    if "downloaded" in outcomes:
                        status, outcome = "completed", "downloaded"
                    elif outcomes == {"cache_hit"}:
                        status, outcome = "completed", "cached"
                    elif "download_failed" in outcomes:
                        status, outcome = "error", "download_failed"
                    else:
                        status, outcome = "skipped", next(iter(outcomes))
                    _set_csv_row_outcome(report, row, status, outcome, property_id=property_id,
                                         reservation_id=reservation_id, profile_id=profile_id, documents=documents)
                    save_csv_report(config.report_file, report)
                    _emit(callback, f"{property_name}: CSV row {row.row_number} {outcome}.",
                          property_name=property_name, **totals)
        _emit(callback, "CSV import completed.", **totals)
        return totals
    except Exception as exc:
        report["run_error"] = str(exc)
        raise
    finally:
        report["completed_at"] = now_utc_iso()
        save_csv_report(config.report_file, report)


def run(config: DownloadConfig, callback: ProgressCallback) -> Dict[str, int]:
    if config.mode == "csv":
        return run_csv_import(config, callback)
    if config.environment not in ENVIRONMENTS:
        raise ValueError("Environment must be UAT or PROD.")
    if not config.properties:
        raise ValueError("Choose at least one property.")
    if config.departure_date_from > config.departure_date_to:
        raise ValueError("Departure date from must not be after departure date to.")
    config.output_dir.mkdir(parents=True, exist_ok=True)
    cache = load_invoice_cache(config.cache_file)
    totals = {"downloaded": 0, "skipped": 0, "cached": 0, "reservations_found": 0, "processed": 0}
    _emit(callback, f"Starting {config.environment} run for {len(config.properties)} property/properties.", **totals)
    session = make_session(config)

    for property_name, property_id in config.properties.items():
        _emit(callback, f"Fetching reservations for {property_name}.", property_name=property_name, **totals)
        try:
            reservations = get_reservations_with_folios(session, config, property_id, callback)
        except Exception as exc:
            totals["skipped"] += 1
            _emit(callback, f"Could not fetch {property_name}: {exc}", property_name=property_name, **totals)
            continue
        totals["reservations_found"] += len(reservations)
        _emit(callback, f"{property_name}: {len(reservations)} reservation(s) found.", property_name=property_name, **totals)
        property_dir = config.output_dir / sanitize_filename_part(property_name)
        property_dir.mkdir(parents=True, exist_ok=True)

        for index, reservation in enumerate(reservations, start=1):
            if config.max_reservations_per_property and index > config.max_reservations_per_property:
                _emit(callback, f"{property_name}: reached reservation limit.", property_name=property_name, **totals)
                break
            totals["processed"] += 1
            account_id = reservation.get("accountId")
            targets = pick_download_folios(reservation, config)
            if not account_id or not targets:
                totals["skipped"] += 1
                _emit(callback, f"{property_name}: skipped reservation {index} (no account or downloadable folio).", property_name=property_name, **totals)
                continue
            confirmation = reservation.get("confirmationNumber")
            external_reference = get_preferred_external_reference(reservation, config.external_reference_system_ids)
            departure = (reservation.get("departureDate") or "unknown")[:10]
            for doc_type, folio in targets:
                folio_id = folio.get("id")
                invoice_number = folio.get("invoiceNumber")
                key = cache_key(property_id, doc_type, invoice_number, confirmation, folio_id)
                existing = cache.get(key)
                if existing and (existing.get("status") == "downloaded" or not config.retry_cached_errors):
                    totals["cached"] += 1
                    _emit(callback, f"{property_name}: cache hit for {doc_type} ({index}/{len(reservations)}).", property_name=property_name, **totals)
                    continue
                if not folio_id:
                    totals["skipped"] += 1
                    _emit(callback, f"{property_name}: skipped folio without an id.", property_name=property_name, **totals)
                    continue
                try:
                    content = get_invoice_content(session, config, property_id, account_id, folio_id, callback)
                    filename = (f"{'invoice' if doc_type == 'invoice' else 'creditnote'}_"
                                f"{sanitize_filename_part(invoice_number or 'folio_' + str(folio.get('number') or 'unknown'))}_"
                                f"{sanitize_filename_part(external_reference or 'no_external_ref')}_"
                                f"Departure-{sanitize_filename_part(departure)}_{int(time.time())}.pdf")
                    output_path = property_dir / filename
                    output_path.write_bytes(content)
                    cache[key] = {"status": "downloaded", "file_path": str(output_path), "last_attempt_at": now_utc_iso()}
                    totals["downloaded"] += 1
                    _emit(callback, f"{property_name}: downloaded {doc_type} ({index}/{len(reservations)}).", property_name=property_name, **totals)
                except Exception as exc:
                    cache[key] = {"status": "error", "error_message": str(exc), "last_attempt_at": now_utc_iso()}
                    totals["skipped"] += 1
                    _emit(callback, f"{property_name}: {doc_type} failed ({index}/{len(reservations)}): {exc}", property_name=property_name, **totals)
                save_invoice_cache(config.cache_file, cache)

    save_invoice_cache(config.cache_file, cache)
    _emit(callback, "Run completed.", **totals)
    return totals
