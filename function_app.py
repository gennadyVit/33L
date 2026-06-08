import logging
import os
from datetime import datetime, timedelta, timezone
from io import BytesIO
from azure.communication.email import EmailClient

import azure.functions as func
import pandas as pd
import pytz
import requests
from azure.storage.blob import BlobServiceClient

app = func.FunctionApp()

LAT, LON = 42.3656, -71.0096  # Logan Airport
EASTERN = pytz.timezone("America/New_York")
FORECAST_DAYS = 3
ACTIVE_HOURS = range(7, 24)  # 7 AM through 11 PM (excludes 12 AM - 6 AM)


def likelihood_of_33l(deg: float):
    """Map wind direction (degrees) to runway 33L usage likelihood."""
    if 290 <= deg <= 360 or deg == 0:
        return "High", 100
    if 265 <= deg < 290:
        diff = abs(deg - 290)
        if diff <= 10:
            return "Possible", 80
        if diff <= 20:
            return "Possible", 65
        return "Possible", 50
    return "Unlikely", None
def fill_single_hour_gaps(df: pd.DataFrame) -> pd.DataFrame:
    """Upgrade isolated 'Unlikely' hours sandwiched between two noisy hours."""
    if df.empty:
        return df
    df = df.sort_values("forecast_time_local").reset_index(drop=True)
    noisy = {"High", "Possible"}
    for i in range(1, len(df) - 1):
        if df.loc[i, "likelihood"] != "Unlikely":
            continue
        prev_row, next_row = df.loc[i - 1], df.loc[i + 1]
        # Only fill if neighbors are within the same day and both noisy
        if prev_row["date"] != df.loc[i, "date"] or next_row["date"] != df.loc[i, "date"]:
            continue
        if prev_row["likelihood"] in noisy and next_row["likelihood"] in noisy:
            # Inherit the weaker of the two neighbors (conservative)
            inherited = "Possible" if "Possible" in (prev_row["likelihood"], next_row["likelihood"]) else "High"
            df.loc[i, "likelihood"] = inherited
            df.loc[i, "percentage_likelihood"] = min(
                prev_row["percentage_likelihood"] or 50,
                next_row["percentage_likelihood"] or 50,
            )
            df.loc[i, "noisy_window"] = True
    return df


def fetch_forecast(api_key: str):
    url = (
        "https://api.openweathermap.org/data/2.5/forecast"
        f"?lat={LAT}&lon={LON}&appid={api_key}&units=metric"
    )
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()["list"]


def build_dataframe(entries):
    """Convert 3-hour API entries into hourly rows, 3 days ahead, 7 AM-midnight."""
    run_time = datetime.now(timezone.utc)
    today_local = datetime.now(EASTERN).date()
    cutoff_date = today_local + timedelta(days=FORECAST_DAYS)

    rows = []
    for entry in entries:
        dt_utc = datetime.fromtimestamp(entry["dt"], tz=timezone.utc)
        dt_local = dt_utc.astimezone(EASTERN)

        if dt_local.date() > cutoff_date:
            continue

        wind_deg = entry["wind"]["deg"]
        wind_speed = entry["wind"].get("speed")
        usage, pct = likelihood_of_33l(wind_deg)

        # Each API entry covers 3 hours - emit one row per hour
        for hour_offset in range(3):
            hour_local = dt_local + timedelta(hours=hour_offset)
            if hour_local.hour not in ACTIVE_HOURS:
                continue
            if hour_local.date() > cutoff_date:
                continue

            rows.append({
                "forecast_run_utc": run_time,
                "forecast_time_local": hour_local.replace(tzinfo=None),
                "date": hour_local.strftime("%Y-%m-%d"),
                "day": hour_local.strftime("%A"),
                "hour_label": hour_local.strftime("%I:00 %p"),
                "hour_24": hour_local.hour,
                "wind_deg": wind_deg,
                "wind_speed_ms": wind_speed,
                "likelihood": usage,
                "percentage_likelihood": pct,
                "noisy_window": usage in ("Possible", "High"),
            })
    return pd.DataFrame(rows)


def write_to_blob(df: pd.DataFrame):
    conn_str = os.environ["AzureWebJobsStorage"]
    container = os.environ.get("BLOB_CONTAINER", "forecasts")
    service = BlobServiceClient.from_connection_string(conn_str)

    buf = BytesIO()
    df.to_parquet(buf, index=False)
    data = buf.getvalue()

    service.get_blob_client(container, "predictions_latest.parquet") \
        .upload_blob(data, overwrite=True)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M")
    service.get_blob_client(container, f"history/{stamp}.parquet") \
        .upload_blob(data, overwrite=True)


def send_email(df: pd.DataFrame) -> None:
    conn_str = os.environ["ACS_CONNECTION_STRING"]
    sender = os.environ["SENDER_EMAIL"]
    recipient = os.environ["RECIPIENT_EMAIL"]

    client = EmailClient.from_connection_string(conn_str)

    display_cols = [c for c in df.columns if c not in ('forecast_run_utc', 'noisy_window')]
    html = df[df['likelihood'].isin(['High', 'Possible'])][display_cols].to_html(index=False)

    message = {
        "senderAddress": sender,
        "recipients": {"to": [{"address": recipient}]},
        "content": {
            "subject": f"33L Forecast {datetime.now(EASTERN).strftime('%a %b %d')}",
            "html": f"<h2>33L Overhead Forecast</h2>{html}"
        }
    }

    poller = client.begin_send(message)
    result = poller.result()  # block until sent; raises on failure
    logging.info("Email sent, message id: %s", result.get("id"))


@app.timer_trigger(
    schedule="0 0 11 * * *",   # 11:00 UTC = 7 AM EDT / 6 AM EST
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def predict_overhead(timer: func.TimerRequest) -> None:
    logging.info("Forecast run starting")
    api_key = os.environ["OPENWEATHER_API_KEY"]
    entries = fetch_forecast(api_key)
    df = build_dataframe(entries)
    df = fill_single_hour_gaps(df)
    logging.info("Built dataframe with %d rows", len(df))
    write_to_blob(df)
    try:
        send_email(df)
    except Exception:
        logging.exception("Email send failed — forecast still written to blob")
    logging.info("Forecast run complete")