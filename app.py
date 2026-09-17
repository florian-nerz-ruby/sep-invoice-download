import base64
import json
import re
import threading
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

from invoice_downloader import AVAILABLE_PROPERTIES, DownloadConfig, run


PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = PROJECT_ROOT / "runtime"
MAX_CSV_BYTES = 15 * 1024 * 1024


class JobManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread = None
        self.state: Dict[str, Any] = {"status": "idle", "logs": [], "totals": {}, "error": None}

    def _progress(self, message: str, details: Dict[str, Any]) -> None:
        with self.lock:
            self.state["logs"] = (self.state["logs"] + [{"message": message}])[-250:]
            self.state["totals"].update({
                key: value for key, value in details.items()
                if key in {"downloaded", "skipped", "cached", "reservations_found", "processed"}
            })
            if details.get("property_name"):
                self.state["property_name"] = details["property_name"]

    def start(self, config: DownloadConfig) -> None:
        with self.lock:
            if self.state["status"] == "running":
                raise RuntimeError("A download is already running.")
            self.state = {
                "status": "running", "logs": [], "totals": {}, "error": None,
                "property_name": None, "report_path": str(config.report_file) if config.report_file else None,
            }
        self.thread = threading.Thread(target=self._run, args=(config,), daemon=True)
        self.thread.start()

    def _run(self, config: DownloadConfig) -> None:
        try:
            totals = run(config, self._progress)
            with self.lock:
                self.state["status"] = "completed"
                self.state["totals"] = totals
        except Exception as exc:
            with self.lock:
                self.state["status"] = "failed"
                self.state["error"] = str(exc)
                self.state["logs"].append({"message": f"Run failed: {exc}"})

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            snapshot = dict(self.state)
        report_path = snapshot.get("report_path")
        snapshot["report_available"] = bool(report_path and Path(report_path).is_file())
        return snapshot

    def current_report(self) -> Path | None:
        with self.lock:
            report_path = self.state.get("report_path")
        if report_path:
            path = Path(report_path)
            if path.is_file() and path.is_relative_to(RUNTIME_ROOT):
                return path
        return None


JOBS = JobManager()


def optional_integer(value: Any, name: str):
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a whole number.") from exc
    if number < 1:
        raise ValueError(f"{name} must be at least 1.")
    return number


def safe_name(value: Any, fallback: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or fallback)).strip(".-")
    if not name:
        raise ValueError("Name must contain letters or numbers.")
    return name


def write_uploaded_csv(payload: Dict[str, Any]) -> Path:
    encoded_content = payload.get("csv_bytes_base64")
    if not isinstance(encoded_content, str) or not encoded_content:
        raise ValueError("Choose a CSV file before starting the CSV import.")
    try:
        content = base64.b64decode(encoded_content, validate=True)
    except ValueError as exc:
        raise ValueError("CSV upload is not valid file data.") from exc
    if len(content) > MAX_CSV_BYTES:
        raise ValueError("CSV files must be 15 MB or smaller.")
    filename = safe_name(payload.get("csv_filename"), "reservations.csv")
    if not filename.lower().endswith(".csv"):
        filename += ".csv"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = RUNTIME_ROOT / "imports" / f"{stamp}-{filename}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = destination.with_suffix(destination.suffix + ".tmp")
    temporary_file.write_bytes(content)
    temporary_file.replace(destination)
    return destination


def config_from_payload(payload: Dict[str, Any]) -> DownloadConfig:
    environment = str(payload.get("environment", "PROD")).upper()
    mode = str(payload.get("mode") or "manual")
    invoice_version = str(payload.get("invoice_version") or "copy")
    if invoice_version not in {"copy", "original"}:
        raise ValueError("Invoice version must be copy or original.")
    cache_name = safe_name(payload.get("cache_name"), "invoice-download-cache")
    if not cache_name.endswith(".json"):
        cache_name += ".json"
    output_name = safe_name(payload.get("output_name"), "invoices")
    auth = {key: str(value).strip() for key, value in (payload.get("auth") or {}).items() if value}
    auth = {key: value for key, value in auth.items() if key in {
        "TOKEN_URL", "API_VALIDATE_URL", "USERNAME", "PASSWORD", "CLIENT_ID", "CLIENT_SECRET", "TENANT_NAME"
    }}
    shared = {
        "environment": environment,
        "output_dir": RUNTIME_ROOT / "downloads" / output_name,
        "cache_file": RUNTIME_ROOT / "caches" / cache_name,
        "external_reference_system_ids": [
            item.strip() for item in str(payload.get("external_reference_system_ids") or "").split(",") if item.strip()
        ],
        "download_credit_notes": bool(payload.get("download_credit_notes")),
        "invoice_version": invoice_version,
        "retry_cached_errors": bool(payload.get("retry_cached_errors")),
        "max_pages_per_property": optional_integer(payload.get("max_pages_per_property"), "Page limit"),
        "page_size": optional_integer(payload.get("page_size") or 50, "Page size") or 50,
        "auth_overrides": auth,
        "mode": mode,
    }
    if mode == "csv":
        csv_file = write_uploaded_csv(payload)
        return DownloadConfig(
            properties={}, departure_date_from=date.today().isoformat(), departure_date_to=date.today().isoformat(),
            csv_file=csv_file,
            report_file=RUNTIME_ROOT / "reports" / f"{Path(cache_name).stem}-csv-report.json",
            **shared,
        )
    if mode != "manual":
        raise ValueError("Mode must be manual or csv.")
    selected = payload.get("properties") or []
    properties = {name: AVAILABLE_PROPERTIES[name] for name in selected if name in AVAILABLE_PROPERTIES}
    dates = payload.get("dates") or {}
    return DownloadConfig(
        properties=properties,
        departure_date_from=str(dates.get("from") or date.today().isoformat()),
        departure_date_to=str(dates.get("to") or date.today().isoformat()),
        reservation_id=str(payload.get("reservation_id") or "").strip() or None,
        profile_id_filter=str(payload.get("profile_id_filter") or "").strip() or None,
        max_reservations_per_property=optional_integer(payload.get("max_reservations_per_property"), "Reservation limit"),
        **shared,
    )


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SEP Invoice Download</title><style>
:root{font-family:Inter,"Segoe UI",sans-serif;color:#152238;background:#f3f6f8}*{box-sizing:border-box}body{margin:0}.top{background:#0d3b4a;color:#fff;padding:25px max(24px,calc((100% - 1160px)/2))}.top h1{font-size:23px;margin:0}.top p{margin:5px 0 0;color:#b8d0d7}.layout{max-width:1160px;margin:26px auto;padding:0 20px;display:grid;grid-template-columns:minmax(0,1.25fr) minmax(310px,.75fr);gap:22px}.panel{background:#fff;border:1px solid #d7e0e3;border-radius:8px;padding:22px}h2{font-size:16px;margin:0 0 16px}h3{font-size:13px;margin:22px 0 10px;color:#49616a;text-transform:uppercase;letter-spacing:.06em}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.wide{grid-column:1/-1}label{display:grid;gap:6px;font-size:13px;font-weight:600;color:#344b53}input,select{min-width:0;border:1px solid #b8c8cc;border-radius:5px;padding:9px;font:inherit;font-size:14px;background:#fff}input:focus,select:focus{outline:2px solid #64a4ae;outline-offset:1px}.mode{display:flex;border:1px solid #b8c8cc;border-radius:6px;overflow:hidden;width:max-content;margin-bottom:18px}.mode label{display:block;font-size:14px;font-weight:600;color:#344b53;cursor:pointer}.mode input{position:absolute;opacity:0}.mode span{display:block;padding:9px 15px}.mode input:checked+span{background:#0d3b4a;color:#fff}.checks{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;background:#f5f8f9;padding:12px;border-radius:5px}.checks label,.toggles label{display:flex;align-items:center;gap:7px;font-weight:400}.checks input,.toggles input{min-width:auto}.toggles{display:flex;gap:20px;flex-wrap:wrap;font-size:13px}.advanced{border-top:1px solid #e4eaec;margin-top:20px;padding-top:4px}.advanced summary{cursor:pointer;padding:12px 0;font-weight:600;font-size:14px}.actions{display:flex;justify-content:flex-end;margin-top:22px}button{border:0;border-radius:5px;background:#e75943;color:#fff;font:inherit;font-weight:700;padding:11px 17px;cursor:pointer}button:disabled{background:#9baab0;cursor:not-allowed}.status{display:flex;align-items:center;gap:9px;font-weight:700;text-transform:capitalize}.dot{width:10px;height:10px;border-radius:50%;background:#9baab0}.running .dot{background:#e89c38}.completed .dot{background:#2d9a73}.failed .dot{background:#d6514b}.metrics{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin:20px 0}.metric{background:#f5f8f9;border:1px solid #e1e8ea;padding:13px;border-radius:5px}.metric strong{font-size:25px;display:block}.metric span{font-size:12px;color:#52666d}.current,.report{font-size:13px;color:#52666d;min-height:20px}.report a{color:#176e7c;font-weight:700}.log{background:#11252c;color:#dbe8e9;margin-top:15px;padding:12px;height:390px;overflow:auto;border-radius:5px;font:12px/1.55 Consolas,monospace;white-space:pre-wrap}.error{color:#c6433e;font-size:13px;margin-top:12px}.hidden{display:none}@media(max-width:780px){.layout{grid-template-columns:1fr}.checks{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.top{padding:20px}.metric strong{font-size:22px}}</style></head><body>
<header class="top"><h1>SEP Invoice Download</h1><p>Run and monitor invoice exports from one local screen.</p></header><main class="layout"><section class="panel"><h2>Run settings</h2><form id="run-form">
<div class="mode" aria-label="Run mode"><label><input type="radio" name="mode" value="manual" checked><span>Manual selection</span></label><label><input type="radio" name="mode" value="csv"><span>CSV import</span></label></div>
<div class="grid"><label>Environment<select name="environment"><option value="PROD">Production</option><option value="UAT">UAT</option></select></label><label>Invoice version<select name="invoice_version"><option value="copy">Copy</option><option value="original">Original</option></select></label><label>Cache name<input name="cache_name" value="hotelbeds-invoices" required></label><label>Output folder name<input name="output_name" value="invoices" required></label><label id="external-reference-field">External-reference system IDs<input name="external_reference_system_ids" value="dda65710-4f28-4c45-99cf-3bc8e7251f89, 657561a4-74fe-4ce6-aaaf-1d13ee284a8e"></label><label>Page size<input name="page_size" type="number" min="1" value="50" required></label><label>Pages/property limit<input name="max_pages_per_property" type="number" min="1"></label>
<div id="csv-fields" class="wide hidden"><label>Reservation CSV<input name="csv_file" type="file" accept=".csv,text/csv"></label></div>
<div id="manual-fields" class="wide grid"><label>Departure date from<input name="date_from" type="date" required></label><label>Departure date to<input name="date_to" type="date" required></label><label>Reservation ID (optional)<input name="reservation_id"></label><label>Profile ID filter (optional)<input name="profile_id_filter"></label><label>Reservations/property limit<input name="max_reservations_per_property" type="number" min="1"></label><div class="wide"><h3>Properties</h3><div class="checks" id="properties"></div></div></div>
<div class="wide toggles"><label><input name="download_credit_notes" type="checkbox"> Download credit notes</label><label><input name="retry_cached_errors" type="checkbox"> Retry cached errors</label></div></div>
<details class="advanced"><summary>Authentication overrides</summary><div class="grid"><label>Token URL<input name="TOKEN_URL"></label><label>Validate URL<input name="API_VALIDATE_URL"></label><label>Username<input name="USERNAME"></label><label>Password<input name="PASSWORD" type="password"></label><label>Client ID<input name="CLIENT_ID"></label><label>Client secret<input name="CLIENT_SECRET" type="password"></label><label>Tenant name<input name="TENANT_NAME"></label></div></details><div class="actions"><button id="start" type="submit">Start download</button></div></form></section>
<aside class="panel"><div class="status idle" id="status"><span class="dot"></span><span>Idle</span></div><div class="metrics"><div class="metric"><strong id="found">0</strong><span>Reservations found</span></div><div class="metric"><strong id="processed">0</strong><span>CSV rows/reservations processed</span></div><div class="metric"><strong id="downloaded">0</strong><span>PDFs downloaded</span></div><div class="metric"><strong id="cached">0</strong><span>Cache skips</span></div><div class="metric"><strong id="skipped">0</strong><span>Other skips/errors</span></div></div><div class="current" id="current"></div><div class="report" id="report"></div><div class="error" id="error"></div><div class="log" id="log">Ready.</div></aside></main>
<script>
const properties='Ruby Rosi,Ruby Lilly,Ruby Ella,Ruby Luna,Ruby Lotti,Ruby Coco,Ruby Hanna,Ruby Leni,Ruby Louise,Ruby Claire,Ruby Mimi,Ruby Emma,Ruby Marie,Ruby Sofie,Ruby Stella,Ruby Zoe,Ruby Lissi,Ruby Lucy,Ruby Molly'.split(',');
const form=document.querySelector('#run-form'),propBox=document.querySelector('#properties'),manualFields=document.querySelector('#manual-fields'),csvFields=document.querySelector('#csv-fields'),externalReferenceField=document.querySelector('#external-reference-field'),start=document.querySelector('#start'),current=document.querySelector('#current'),error=document.querySelector('#error'),log=document.querySelector('#log'),report=document.querySelector('#report');
const metric={found:document.querySelector('#found'),processed:document.querySelector('#processed'),downloaded:document.querySelector('#downloaded'),cached:document.querySelector('#cached'),skipped:document.querySelector('#skipped')};
propBox.innerHTML=properties.map(function(name){return '<label><input type="checkbox" name="property" value="'+name+'" '+(name==='Ruby Lissi'?'checked':'')+'>'+name+'</label>'}).join('');
const today=new Date().toISOString().slice(0,10);form.elements.date_from.value=today;form.elements.date_to.value=today;
function mode(){const csv=form.elements.mode.value==='csv';manualFields.classList.toggle('hidden',csv);csvFields.classList.toggle('hidden',!csv);externalReferenceField.classList.toggle('hidden',csv);form.elements.date_from.required=!csv;form.elements.date_to.required=!csv;form.elements.csv_file.required=csv}
form.querySelectorAll('[name=mode]').forEach(function(input){input.addEventListener('change',mode)});mode();
function refresh(){fetch('/api/state').then(function(response){return response.json()}).then(function(state){const status=document.querySelector('#status'),totals=state.totals||{};status.className='status '+state.status;status.lastElementChild.textContent=state.status;metric.found.textContent=totals.reservations_found||0;metric.processed.textContent=totals.processed||0;metric.downloaded.textContent=totals.downloaded||0;metric.cached.textContent=totals.cached||0;metric.skipped.textContent=totals.skipped||0;current.textContent=state.property_name?'Current property: '+state.property_name:'';error.textContent=state.error||'';report.innerHTML=state.report_available?'<a href="/api/report">Download current CSV report</a>':'';log.textContent=(state.logs||[]).map(function(item){return item.message}).join('\n')||'Ready.';log.scrollTop=log.scrollHeight;start.disabled=state.status==='running'}).catch(function(){})}
setInterval(refresh,1000);refresh();
async function encodeCsv(file){const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(let offset=0;offset<bytes.length;offset+=32768){binary+=String.fromCharCode.apply(null,bytes.subarray(offset,offset+32768))}return btoa(binary)}
form.addEventListener('submit',async function(event){event.preventDefault();error.textContent='';const csv=form.elements.mode.value==='csv'?form.elements.csv_file.files[0]:null;if(form.elements.mode.value==='csv'&&!csv){error.textContent='Choose a CSV file.';return}const auth={};['TOKEN_URL','API_VALIDATE_URL','USERNAME','PASSWORD','CLIENT_ID','CLIENT_SECRET','TENANT_NAME'].forEach(function(key){auth[key]=form.elements[key].value});const body={mode:form.elements.mode.value,environment:form.elements.environment.value,invoice_version:form.elements.invoice_version.value,cache_name:form.elements.cache_name.value,output_name:form.elements.output_name.value,dates:{from:form.elements.date_from.value,to:form.elements.date_to.value},properties:[...form.querySelectorAll('[name=property]:checked')].map(function(input){return input.value}),reservation_id:form.elements.reservation_id.value,profile_id_filter:form.elements.profile_id_filter.value,external_reference_system_ids:form.elements.external_reference_system_ids.value,max_reservations_per_property:form.elements.max_reservations_per_property.value,max_pages_per_property:form.elements.max_pages_per_property.value,page_size:form.elements.page_size.value,download_credit_notes:form.elements.download_credit_notes.checked,retry_cached_errors:form.elements.retry_cached_errors.checked,auth:auth};if(csv){body.csv_filename=csv.name;body.csv_bytes_base64=await encodeCsv(csv)}const response=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!response.ok){const result=await response.json();error.textContent=result.error||'Could not start run.'}refresh()});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload: Dict[str, Any], status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/api/state":
            self._send_json(JOBS.snapshot())
            return
        if self.path == "/api/report":
            report = JOBS.current_report()
            if not report:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = report.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{report.name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/":
            data = PAGE.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path != "/api/runs":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_CSV_BYTES * 2:
                raise ValueError("Request is too large.")
            payload = json.loads(self.rfile.read(length))
            JOBS.start(config_from_payload(payload))
            self._send_json({"ok": True}, HTTPStatus.ACCEPTED)
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args: Any) -> None:
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("SEP Invoice Download is available at http://127.0.0.1:8765")
    server.serve_forever()
