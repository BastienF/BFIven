#!/usr/bin/env python3
"""Push an ntfy notification when strong wind is forecast.

Meant to run hourly from cron. Only notifies when a wind episode starts,
gets worse or lasts longer than announced, and when it is over, so hourly
runs do not spam. Keeps the hourly wind history in SQLite so that each
alert can be compared with the episodes of the past 12 months.
"""

import argparse
import json
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 4
# Covers the hours the script does not run (quiet hours, outages)
FORECAST_PAST_DAYS = 1
HISTORY_COMPARISON_DAYS = 365
HTTP_TIMEOUT_SECONDS = 60
FRENCH_WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
FRENCH_MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def main():
    args = parse_args()
    config = json.loads(Path(args.config).read_text())
    timezone = ZoneInfo(config["timezone"])
    now = datetime.now(timezone).replace(tzinfo=None)

    if is_quiet_hour(now, config) and not (args.test or args.dry_run):
        return

    read_only = args.test or args.dry_run
    history = open_history(Path(config["history_file"]), read_only)
    forecast = fetch_hourly(FORECAST_URL, config, {"past_days": FORECAST_PAST_DAYS, "forecast_days": FORECAST_DAYS})
    if not read_only:
        backfill_if_needed(history, config, now)
        record_hours(history, [hour for hour in forecast if hour["time"] < current_hour(now)])

    episodes = upcoming_episodes(forecast, now, config)
    past_episodes = recent_past_episodes(history, config, now) if history else []

    if args.test:
        publish(config, test_notification(episodes, past_episodes, config, now), args.dry_run)
        return

    state_path = Path(config["state_file"])
    notifications, notified_episodes = reconcile(
        episodes, load_notified_episodes(state_path), past_episodes, config, now
    )
    for notification in notifications:
        publish(config, notification, args.dry_run)
        if not read_only:
            record_notification(history, notification, now)
    if not read_only:
        save_notified_episodes(state_path, notified_episodes)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", help="path to the JSON configuration file")
    parser.add_argument("--test", action="store_true", help="send a test notification with the current forecast")
    parser.add_argument("--dry-run", action="store_true", help="print the notification instead of sending it")
    return parser.parse_args()


def is_quiet_hour(now, config):
    start, end = config["quiet_hours_start"], config["quiet_hours_end"]
    if start > end:
        return now.hour >= start or now.hour < end
    return start <= now.hour < end


def current_hour(now):
    return now.replace(minute=0, second=0, microsecond=0)


def fetch_hourly(url, config, period):
    query = urllib.parse.urlencode({
        "latitude": config["latitude"],
        "longitude": config["longitude"],
        "hourly": "wind_speed_10m,wind_gusts_10m",
        "models": config["model"],
        "timezone": config["timezone"],
        "wind_speed_unit": "kmh",
        **period,
    })
    with urllib.request.urlopen(f"{url}?{query}", timeout=HTTP_TIMEOUT_SECONDS) as response:
        hourly = json.load(response)["hourly"]
    return [
        {"time": datetime.fromisoformat(time), "speed": speed, "gust": gust}
        for time, speed, gust in zip(hourly["time"], hourly["wind_speed_10m"], hourly["wind_gusts_10m"])
        if speed is not None and gust is not None
    ]


def open_history(path, read_only):
    if read_only:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True) if path.exists() else None
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS hourly_wind (time TEXT PRIMARY KEY, speed REAL NOT NULL, gust REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS sent_notifications "
            "(sent_at TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL, priority INTEGER NOT NULL)"
        )
    return connection


def comparison_period_start(now):
    return now - timedelta(days=HISTORY_COMPARISON_DAYS)


def covers_comparison_period(history, now):
    oldest = history.execute("SELECT MIN(time) FROM hourly_wind").fetchone()[0]
    return oldest is not None and datetime.fromisoformat(oldest) <= comparison_period_start(now) + timedelta(days=1)


def backfill_if_needed(history, config, now):
    """Load the past year until the history covers it; on failure the next run retries, alerts go on meanwhile."""
    if covers_comparison_period(history, now):
        return
    period = {
        "start_date": comparison_period_start(now).date().isoformat(),
        "end_date": (now - timedelta(days=1)).date().isoformat(),
    }
    try:
        hours = fetch_hourly(HISTORICAL_FORECAST_URL, config, period)
    except OSError as error:
        print(f"{now.isoformat(timespec='seconds')} history backfill failed: {error}", file=sys.stderr)
        return
    record_hours(history, hours)
    print(f"{now.isoformat(timespec='seconds')} history backfilled with {len(hours)} hours")


def record_hours(history, hours):
    with history:
        history.executemany(
            "INSERT INTO hourly_wind (time, speed, gust) VALUES (?, ?, ?) "
            "ON CONFLICT (time) DO UPDATE SET speed = excluded.speed, gust = excluded.gust",
            [(hour["time"].isoformat(), hour["speed"], hour["gust"]) for hour in hours],
        )


def record_notification(history, notification, now):
    with history:
        history.execute(
            "INSERT INTO sent_notifications (sent_at, title, message, priority) VALUES (?, ?, ?, ?)",
            (now.isoformat(timespec="seconds"), notification["title"], notification["message"],
             notification["priority"]),
        )


def recent_past_episodes(history, config, now):
    """Episodes of the past 12 months, cut with the same rules as the forecast ones.
    The episode in progress is left out so that it is not compared with itself.
    Empty while the history does not cover the whole period, to avoid misleading rankings."""
    if not covers_comparison_period(history, now):
        return []
    rows = history.execute(
        "SELECT time, speed, gust FROM hourly_wind WHERE time >= ? ORDER BY time",
        (comparison_period_start(now).isoformat(),),
    )
    hours = [{"time": datetime.fromisoformat(time), "speed": speed, "gust": gust} for time, speed, gust in rows]
    episodes = split_episodes(hours, config["levels"], config["episode_gap_hours"])
    finished_before = current_hour(now) - timedelta(hours=config["episode_gap_hours"])
    return [episode for episode in episodes if episode["end"] < finished_before]


def upcoming_episodes(forecast, now, config):
    """Episodes starting within the horizon, measured over the whole forecast so that
    an episode does not look like it worsens as the horizon slides over it."""
    horizon_end = now + timedelta(hours=config["horizon_hours"])
    upcoming = [hour for hour in forecast if hour["time"] >= current_hour(now)]
    episodes = split_episodes(upcoming, config["levels"], config["episode_gap_hours"])
    return [episode for episode in episodes if episode["start"] < horizon_end]


def level_of(hour, levels):
    reached = 0
    for number, level in enumerate(levels, start=1):
        if hour["speed"] >= level["speed"] or hour["gust"] >= level["gust"]:
            reached = number
    return reached


def split_episodes(hours, levels, gap_hours):
    """Group windy hours into episodes; more than gap_hours between two windy hours starts a new episode."""
    episodes = []
    for hour in hours:
        level = level_of(hour, levels)
        if level == 0:
            continue
        if episodes and hour["time"] - episodes[-1]["end"] <= timedelta(hours=gap_hours):
            episode = episodes[-1]
        else:
            episode = {"start": hour["time"], "level": 0, "max_speed": 0, "strongest_gust": hour}
            episodes.append(episode)
        episode["end"] = hour["time"]
        episode["level"] = max(episode["level"], level)
        episode["max_speed"] = max(episode["max_speed"], hour["speed"])
        if hour["gust"] > episode["strongest_gust"]["gust"]:
            episode["strongest_gust"] = hour
    return episodes


def reconcile(episodes, notified_episodes, past_episodes, config, now):
    """Notify forecast episodes that are new, worse, or longer than announced; return notifications and the new state."""
    gap = timedelta(hours=config["episode_gap_hours"])
    extension_tolerance = timedelta(hours=config["extension_tolerance_hours"])
    notifications = []
    still_notified = []
    for episode in episodes:
        known = next(
            (notified for notified in notified_episodes
             if notified["start"] - gap <= episode["end"] and episode["start"] <= notified["end"] + gap),
            None,
        )
        if known is None:
            title_prefix = "Vent"
        elif episode["level"] > known["level"]:
            title_prefix = "Vent en hausse"
        elif episode["end"] > known["announced_end"] + extension_tolerance:
            title_prefix = "Vent prolongé"
        else:
            title_prefix = None
        if title_prefix:
            notifications.append(wind_notification(episode, past_episodes, config, now, title_prefix))
        still_notified.append({
            "start": min(episode["start"], known["start"]) if known else episode["start"],
            "end": episode["end"],
            "announced_end": episode["end"] if title_prefix else known["announced_end"],
            "level": max(episode["level"], known["level"]) if known else episode["level"],
        })
    if notified_episodes and not episodes:
        notifications.append({
            "title": "Vent : fin d'alerte",
            "message": f"Plus de vent fort prévu dans les {config['horizon_hours']} prochaines heures.",
            "priority": 2,
            "tags": ["white_check_mark"],
        })
    return notifications, still_notified


def wind_notification(episode, past_episodes, config, now, title_prefix="Vent"):
    levels = config["levels"]
    level = levels[episode["level"] - 1]
    gust = episode["strongest_gust"]
    lines = [
        f"Rafales jusqu'à {round(gust['gust'])} km/h {describe_time(gust['time'], now)}, "
        f"vent moyen jusqu'à {round(episode['max_speed'])} km/h.",
        f"Épisode : {describe_time(episode['start'], now)} → {describe_time(episode['end'], now)}.",
    ]
    if past_episodes:
        lines.append(compare_with_past(episode, past_episodes))
    lines.append(level["advice"])
    return {
        "title": f"{title_prefix} : {level['name']} (niveau {episode['level']}/{len(levels)})",
        "message": "\n".join(lines),
        "priority": level["priority"],
        "tags": level["tags"],
    }


def compare_with_past(episode, past_episodes):
    """Rank the episode by its strongest gust among the episodes of the past 12 months."""
    gust = episode["strongest_gust"]["gust"]
    record = max(past_episodes, key=lambda past: past["strongest_gust"]["gust"])["strongest_gust"]
    record_text = f"{round(record['gust'])} km/h le {record['time'].day} {FRENCH_MONTHS[record['time'].month - 1]}"
    rank = 1 + sum(1 for past in past_episodes if past["strongest_gust"]["gust"] > gust)
    if rank == 1:
        return f"Le plus fort des 12 derniers mois (record précédent : {record_text})."
    return f"{rank}ᵉ plus fort sur {len(past_episodes) + 1} épisodes en 12 mois (record : {record_text})."


def test_notification(episodes, past_episodes, config, now):
    if episodes:
        strongest = max(episodes, key=lambda episode: episode["level"])
        notification = wind_notification(strongest, past_episodes, config, now)
        notification["title"] = f"[Test] {notification['title']}"
        return notification
    return {
        "title": "[Test] Alerte vent opérationnelle",
        "message": f"Pas de vent fort prévu dans les {config['horizon_hours']} prochaines heures.",
        "priority": 2,
        "tags": ["white_check_mark"],
    }


def describe_time(time, now):
    days_ahead = (time.date() - now.date()).days
    if days_ahead == 0:
        day = "aujourd'hui"
    elif days_ahead == 1:
        day = "demain"
    else:
        day = FRENCH_WEEKDAYS[time.weekday()]
    return f"{day} {time.hour}h"


STATE_TIME_FIELDS = ("start", "end", "announced_end")


def load_notified_episodes(state_path):
    if not state_path.exists():
        return []
    episodes = json.loads(state_path.read_text()).get("notified_episodes", [])
    return [
        {**episode, **{field: datetime.fromisoformat(episode[field]) for field in STATE_TIME_FIELDS}}
        for episode in episodes
    ]


def save_notified_episodes(state_path, episodes):
    serializable = [
        {**episode, **{field: episode[field].isoformat() for field in STATE_TIME_FIELDS}}
        for episode in episodes
    ]
    state_path.write_text(json.dumps({"notified_episodes": serializable}))


def publish(config, notification, dry_run):
    payload = {"topic": config["ntfy_topic"], "click": config["click_url"], **notification}
    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    request = urllib.request.Request(
        config["ntfy_url"],
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS).close()
    sent_at = datetime.now(ZoneInfo(config["timezone"])).replace(tzinfo=None)
    print(f"{sent_at.isoformat(timespec='seconds')} sent: {notification['title']}")


if __name__ == "__main__":
    sys.exit(main())
