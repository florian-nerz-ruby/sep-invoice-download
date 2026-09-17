# SEP Invoice Download

A local browser interface for the SEP invoice downloader. It keeps the original
SEP requests, cache behavior, and PDF naming conventions, while allowing each
run to be configured and observed in the browser.

## Run it

1. Install the dependency: `python -m pip install -r requirements.txt`
2. Set the auth environment variables for the environment being used, for
   example `PROD_TOKEN_URL`, `PROD_USERNAME`, and `PROD_PASSWORD`.
3. Start the app: `python app.py`
4. Open `http://127.0.0.1:8765`.

The form can temporarily override authentication values for a run. They are not
written to the project or to the invoice cache.

## CSV import mode

Choose **CSV import** in the local interface and upload a CSV with these exact
columns: `MAIN_ID`, `HOTEL_NAME`, `GUEST_NAME`, `ARRIVAL_DATE`,
`DEPARTURE_DATE`, and `BOOKING_STATE`. Dates may be `YYYY-MM-DD` or `DD-MM-YY`.

The importer groups each property's arrival dates into windows of up to three
calendar dates. It requests `extend=Folios,Guests`, normalizes the CSV guest
name and the returned individual name, then requires both an exact arrival-date
match and `reservation.profileId == individual.id` before downloading a folio.

It creates `runtime/reports/<cache-name>-csv-report.json` before making SEP
requests and atomically updates it after every CSV row. The report retains the
source row, match identifiers, document/cache data, and every outcome,
including ambiguous or failed rows. It is available through the dashboard while
the run is in progress and after it ends.

Downloads and invoice caches are created under `runtime/`, which is ignored by
Git. Token caches continue to live in the user-level `.ruby_api_tokens` folder.

Only one download can run at a time. The status panel refreshes every second
with live counters and the latest 250 log messages.
