# --- START OF FILE news_filter.py ---

import logging
import requests
import xml.etree.ElementTree as ET
import threading
from datetime import datetime, timedelta, timezone
from typing import Tuple
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


class NewsFilter:
    def __init__(self, block_before_mins: int = 15, block_after_mins: int = 30):
        self.block_before = timedelta(minutes=block_before_mins)
        self.block_after = timedelta(minutes=block_after_mins)
        self.events = []
        self.last_fetch = None
        self._lock = threading.RLock()
        self.url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

    def _fetch_calendar(self):
        with self._lock:
            now = datetime.now(timezone.utc)
            # Only fetch once every 4 hours to avoid IP bans from ForexFactory
            if self.last_fetch and (now - self.last_fetch).total_seconds() < 14400:
                return

            try:
                log.info("[NewsFilter] Fetching weekly macroeconomic calendar...")
                response = requests.get(self.url, timeout=10)
                response.raise_for_status()
                root = ET.fromstring(response.content)

                parsed_events = []
                for event in root.findall("event"):
                    impact = event.find("impact").text.strip() if event.find("impact") is not None else ""

                    # We ONLY care about High Impact (Red Folder) events
                    if impact != "High":
                        continue

                    currency = event.find("country").text.strip().upper() if event.find("country") is not None else ""
                    date_str = event.find("date").text.strip() if event.find("date") is not None else ""
                    time_str = event.find("time").text.strip() if event.find("time") is not None else ""
                    title = event.find("title").text.strip() if event.find("title") is not None else ""

                    if "All Day" in time_str or "Tentative" in time_str:
                        continue # Skip un-timed events to prevent freezing the bot all day

                    # Parse ForexFactory Time (US Eastern Time) and convert to UTC
                    try:
                        dt_str = f"{date_str} {time_str}"
                        raw_dt = datetime.strptime(dt_str, "%m-%d-%Y %I:%M%p")

                        # FIX: Use the real US Eastern zone (handles EST/EDT automatically).
                        # Previously used the MACHINE's local DST, which is wrong on
                        # non-US servers (e.g. WAT has no DST) and shifted events 1h off.
                        est_tz = ZoneInfo("America/New_York")
                        localized_dt = raw_dt.replace(tzinfo=est_tz)
                        utc_dt = localized_dt.astimezone(timezone.utc)

                        parsed_events.append({
                            "currency": currency,
                            "time": utc_dt,
                            "title": title
                        })
                    except Exception as parse_err:
                        continue

                self.events = parsed_events
                self.last_fetch = now
                log.info(f"[NewsFilter] Loaded {len(self.events)} High-Impact events for the week.")
            except Exception as e:
                log.error(f"[NewsFilter] Failed to fetch calendar: {e}")

    def _get_symbol_currencies(self, symbol: str) -> list:
        """Breaks down a ticker (EURUSDm) into its base currencies (EUR, USD)."""
        clean_sym = symbol.upper().replace("M", "").replace("m", "")

        # Forex (e.g., EURUSD)
        if len(clean_sym) == 6:
            return [clean_sym[:3], clean_sym[3:]]

        # Crypto/Metals generally trade against USD
        if "BTC" in clean_sym or "ETH" in clean_sym or "XAU" in clean_sym or "XAG" in clean_sym:
            return ["USD"]

        # Indices
        if "US30" in clean_sym or "US500" in clean_sym or "USTEC" in clean_sym:
            return ["USD"]
        if "DE30" in clean_sym: return ["EUR"]
        if "UK100" in clean_sym: return ["GBP"]
        if "JP225" in clean_sym: return ["JPY"]
        if "AUS200" in clean_sym: return ["AUD"]

        return []

    def is_safe_to_trade(self, symbol: str) -> Tuple[bool, str]:
        """Checks if the symbol is within the blast radius of an upcoming news event.
        Always fails OPEN: if the calendar cannot be fetched, trading is allowed
        (the filter must never freeze the bot)."""
        try:
            self._fetch_calendar()
        except Exception as e:
            log.warning(f"[NewsFilter] Calendar fetch raised ({e}) — trading allowed (fail-open).")
            return True, ""

        now = datetime.now(timezone.utc)
        currencies = self._get_symbol_currencies(symbol)

        if not currencies:
            return True, ""

        with self._lock:
            for event in self.events:
                if event["currency"] in currencies:
                    event_time = event["time"]
                    danger_start = event_time - self.block_before
                    danger_end = event_time + self.block_after

                    if danger_start <= now <= danger_end:
                        mins_left = int((danger_end - now).total_seconds() / 60)
                        return False, f"HIGH IMPACT NEWS: {event['title']} ({event['currency']}). Safe in {mins_left}m."

        return True, ""
# --- END OF FILE news_filter.py ---
