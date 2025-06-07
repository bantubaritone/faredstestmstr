import dbm.dumb
import json
import logging
import shelve
from datetime import date
from enum import Enum, auto
from random import randint, shuffle
from time import sleep
from typing import Final

from selenium.webdriver.common.by import By
from trendspy import Trends
import requests

from src.browser import Browser
from src.utils import CONFIG, getProjectRoot, cooldown, COUNTRY

LOAD_DATE_KEY = "loadDate"
GLOBAL_KEYWORDS_DB = "used_keywords"
GLOBAL_LOAD_DATE_KEY = "globalLoadDate"  # NEW in v2.4
SEARCH_PROGRESS_KEY = "searchProgress"

class RetriesStrategy(Enum):
    """Identical to original docstrings"""
    EXPONENTIAL = auto()
    CONSTANT = auto()

class Searches:
    """
    Class to handle searches in MS Rewards.
    Version 2.4 - Added global daily reset
    """
    maxRetries: Final[int] = CONFIG.get("retries").get("max")
    baseDelay: Final[float] = CONFIG.get("retries").get("base_delay_in_seconds")
    retriesStrategy = RetriesStrategy[CONFIG.get("retries").get("strategy")]

    def __init__(
        self,
        browser: Browser,
        num_additional_searches=2,
        custom_search_limits=None,
        use_custom_limits=True
    ):
        """
        :param custom_search_limits: Dict to force search counts (e.g., {"desktop": 10, "mobile": 5})
        :param use_custom_limits: If True, uses custom_search_limits; otherwise auto-detects remaining searches.
        """
        if custom_search_limits is not None:
            # 1. Dictionary check
            if not isinstance(custom_search_limits, dict):
                raise ValueError("custom_search_limits must be a dictionary")
            # 2. Key existence check
            if not all(k in custom_search_limits for k in ["desktop", "mobile"]):
                raise ValueError("custom_search_limits must contain 'desktop' and 'mobile' keys")
            # 3. Value type check (NEW)
            if not all(isinstance(v, int) for v in custom_search_limits.values()):
                raise ValueError("Search limits must be integers")
            # 4. Positive integer validation
            if not all(isinstance(v, int) and v >= 0 for v in custom_search_limits.values()):
                raise ValueError("Search limits must be positive integers")
            if any(v == 0 for v in custom_search_limits.values()):
                logging.warning("Zero search limits will skip all searches for that device type")
        self.browser = browser
        self.webdriver = browser.webdriver
        self.num_additional_searches = num_additional_searches
        self.custom_search_limits = custom_search_limits or {"desktop": 0, "mobile": 2}
        self.use_custom_limits = use_custom_limits

        # Device-specific shelf (UNCHANGED)
        dumbDbm = dbm.dumb.open((getProjectRoot() / "google_trends").__str__())
        self.googleTrendsShelf: shelve.Shelf = shelve.Shelf(dumbDbm)
    
        # Global keyword tracker (UNCHANGED)
        globalDbm = dbm.dumb.open((getProjectRoot() / GLOBAL_KEYWORDS_DB).__str__())
        self.usedKeywordsShelf: shelve.Shelf = shelve.Shelf(globalDbm)
    
        # NEW: Progress tracking initialization (only for custom mode)
        if self.use_custom_limits and self.custom_search_limits is not None:
            self.search_progress = self.usedKeywordsShelf.get("searchProgress", {"desktop": 0, "mobile": 0})
        else:
            self.search_progress = {"desktop": 0, "mobile": 0}  # Dummy object for auto mode
    
        # GLOBAL RESET LOGIC (Modified to preserve existing behavior)
        global_load_date = self.usedKeywordsShelf.get(GLOBAL_LOAD_DATE_KEY)
        if global_load_date is None or global_load_date < date.today():
            self.usedKeywordsShelf.clear()
            self.usedKeywordsShelf[GLOBAL_LOAD_DATE_KEY] = date.today()
            # NEW: Reset progress only for custom mode
            if self.use_custom_limits:
                self.search_progress = {"desktop": 0, "mobile": 0}
                self.usedKeywordsShelf["searchProgress"] = self.search_progress

        # EXISTING LOCAL RESET (COMPLETELY UNCHANGED)
        loadDate: date | None = None
        if LOAD_DATE_KEY in self.googleTrendsShelf:
            loadDate = self.googleTrendsShelf[LOAD_DATE_KEY]

        if loadDate is None or loadDate < date.today():
            self.googleTrendsShelf.clear()
            self.googleTrendsShelf[LOAD_DATE_KEY] = date.today()
            trends = self.getGoogleTrends(
                self.browser.getRemainingSearches(desktopAndMobile=True).getTotal()
            )
            shuffle(trends)
            for trend in trends:
                if trend.lower() not in self.usedKeywordsShelf:
                    self.googleTrendsShelf[trend] = None
            logging.debug(f"TRENDS LOADED: {list(self.googleTrendsShelf.keys())}")

    # EVERYTHING BELOW THIS LINE IS IDENTICAL TO ORIGINAL v2.4
    def getGoogleTrends(self, wordsCount: int) -> list[str]:
        """Fetch trends using trendspy"""
        logging.debug("Fetching trends via trendspy...")
        try:
            trends = Trends().trending_now(geo=self.browser.localeGeo)[:wordsCount]
            return [t.keyword.lower() for t in trends]
        except Exception as e:
            logging.error(f"Error fetching trends: {e}")
            return []

    def extract_json_from_response(self, text: str):
        """Maintained for backward compatibility"""
        logging.debug("Extracting JSON from API response")
        for line in text.splitlines():
            trimmed = line.strip()
            if trimmed.startswith('[') and trimmed.endswith(']'):
                try:
                    intermediate = json.loads(trimmed)
                    data = json.loads(intermediate[0][2])
                    logging.debug("JSON extraction successful")
                    return data[1]
                except Exception as e:
                    logging.warning(f"Error parsing JSON: {e}")
                    continue
        logging.error("No valid JSON found in response")
        return None

    def getRelatedTerms(self, term: str) -> list[str]:
        """Fetch related terms from Bing's autocomplete API"""
        try:
            response = requests.get(
                f"https://api.bing.com/osjson.aspx?query={term}",
                headers={"User-agent": self.browser.userAgent},
            )
            response.raise_for_status()
            relatedTerms = response.json()[1]
            uniqueTerms = list(dict.fromkeys(relatedTerms))
            return [t for t in uniqueTerms if t.lower() != term.lower()]
        except requests.RequestException as e:
            logging.error(f"Error fetching related terms for {term}: {e}")
            return []

    def bingSearches(self) -> None:
        """Version 2.4.2 - Custom/Auto search modes with fixed counter updates"""
        logging.info(f"[BING] Starting {self.browser.browserType.capitalize()} Edge Bing searches...")
        self.browser.utils.goToSearch()

        # NEW: Initialize progress tracking only for custom mode
        if self.use_custom_limits and self.custom_search_limits:
            self.search_progress = self.usedKeywordsShelf.get("searchProgress", {"desktop": 0, "mobile": 0})
            # Adjust initial counts based on progress
            self.custom_search_limits["desktop"] = max(0, self.custom_search_limits["desktop"] - self.search_progress["desktop"])
            self.custom_search_limits["mobile"] = max(0, self.custom_search_limits["mobile"] - self.search_progress["mobile"])

        while True:
            # --- SWITCH LOGIC ---
            if self.use_custom_limits and self.custom_search_limits:
                remaining_desktop = self.custom_search_limits.get("desktop", 0)
                remaining_mobile = self.custom_search_limits.get("mobile", 0)
            else:
                remaining = self.browser.getRemainingSearches(desktopAndMobile=True)
                remaining_desktop, remaining_mobile = remaining.desktop, remaining.mobile

            # Unified logging format
            mode = "CUSTOM" if (self.use_custom_limits and self.custom_search_limits) else "AUTO"
            # NEW: Enhanced logging for custom mode
            if self.use_custom_limits and self.custom_search_limits:
                logging.info(f"[MODE:{mode}] Desktop: {remaining_desktop} (done: {self.search_progress['desktop']}), Mobile: {remaining_mobile} (done: {self.search_progress['mobile']})")
            else:
                logging.info(f"[MODE:{mode}] Desktop: {remaining_desktop}, Mobile: {remaining_mobile}")

            # Unified exit condition
            current_remaining = remaining_desktop if self.browser.browserType == "desktop" else remaining_mobile
            if current_remaining <= 0:
                break

            logging.info(f"[BING] Remaining searches: Desktop={remaining_desktop}, Mobile={remaining_mobile}")

            # Calculate needed searches (FIXED: removed duplicate calculation)
            if self.use_custom_limits and self.custom_search_limits:
                needed_searches = (
                    self.custom_search_limits["desktop"] 
                    if self.browser.browserType == "desktop" 
                    else self.custom_search_limits["mobile"]
                )
            else:
                needed_searches = (
                    remaining.desktop if self.browser.browserType == "desktop" 
                    else remaining.mobile
                )
    
            if (len(self.googleTrendsShelf) <= 1 or
                len([k for k in self.googleTrendsShelf.keys() if k != LOAD_DATE_KEY]) < needed_searches):
                logging.debug("Refreshing trends cache...")
                trends = self.getGoogleTrends(needed_searches + 5)
                shuffle(trends)
                for trend in trends:
                    if trend.lower() not in self.usedKeywordsShelf:
                        self.googleTrendsShelf[trend] = None
                self.googleTrendsShelf[LOAD_DATE_KEY] = date.today()
    
                logging.debug(
                    f"BUFFER STATUS: Needed={needed_searches}, "
                    f"Loaded={len(trends)}, "
                    f"Now in shelf={len(self.googleTrendsShelf)-1}"
                )

            for _ in range(needed_searches):
                current_remaining = remaining_desktop if self.browser.browserType == "desktop" else remaining_mobile
                if current_remaining <= 0:
                    break
        
                self.bingSearch()
                sleep(randint(10, 15))

                # FIXED INDENTATION AND LOGIC:
                if self.use_custom_limits and self.custom_search_limits:
                    if self.browser.browserType == "desktop":
                        self.custom_search_limits["desktop"] = max(0, self.custom_search_limits["desktop"] - 1)
                        remaining_desktop = self.custom_search_limits["desktop"]
                        # NEW: Update progress tracking
                        self.search_progress["desktop"] += 1
                    else:
                        self.custom_search_limits["mobile"] = max(0, self.custom_search_limits["mobile"] - 1)
                        remaining_mobile = self.custom_search_limits["mobile"]
                        # NEW: Update progress tracking
                        self.search_progress["mobile"] += 1
                    # NEW: Save progress after each search
                    self.usedKeywordsShelf["searchProgress"] = self.search_progress
                    self.usedKeywordsShelf.sync()
                    logging.debug(f"[MODE:{mode}] Counters - Desktop: {remaining_desktop}, Mobile: {remaining_mobile}")
                else:
                    remaining = self.browser.getRemainingSearches(desktopAndMobile=True)

        logging.info(f"[BING] Finished {self.browser.browserType.capitalize()} Edge Bing searches!")

    def bingSearch(self) -> None:
        availableTrends = [
            k for k in self.googleTrendsShelf.keys() 
            if k != LOAD_DATE_KEY and k.lower() not in self.usedKeywordsShelf
        ]
        if not availableTrends:
            logging.error("[BING] No unused trending keywords available globally.")
            return

        primaryKeyword = availableTrends[0]
        relatedKeywords = self.getRelatedTerms(primaryKeyword)

        logging.debug(f"PRIMARY KEYWORD: {primaryKeyword}, REMAINING TRENDS: {len(self.googleTrendsShelf)-1}")
        logging.debug(f"GLOBAL USAGE COUNT: {len(self.usedKeywordsShelf)}")

        # 1. Perform primary search first
        self.browser.utils.goToSearch()
        searchbar = self.browser.utils.waitUntilClickable(By.ID, "sb_form_q", timeToWait=60)
        searchbar.clear()
        sleep(1)
        searchbar.send_keys(primaryKeyword)
        sleep(1)
        searchbar.submit()

        # 2. Mark as used globally
        self.usedKeywordsShelf[primaryKeyword.lower()] = None
        logging.debug(f"MARKED AS USED GLOBALLY: {primaryKeyword}")

        # 3. Original local deletion
        if primaryKeyword in self.googleTrendsShelf:
            del self.googleTrendsShelf[primaryKeyword]
            logging.debug(f"POST-DELETION SHELF: {list(self.googleTrendsShelf.keys())}")

        logging.info("[COOLDOWN] Applying cooldown after primary search")
        cooldown()

        # 4. Show related terms summary
        logging.debug(
            f"RELATED TERMS SUMMARY: Found {len(relatedKeywords)} terms - "
            f"{relatedKeywords[:10]}"
        )

        # 5. Additional searches
        for i in range(min(self.num_additional_searches, len(relatedKeywords))):
            relatedKeyword = relatedKeywords.pop(0)
            logging.debug(f"Searching related keyword #{i+1}: {relatedKeyword}")
            try:
                self.browser.utils.goToSearch()
                searchbar = self.browser.utils.waitUntilClickable(By.ID, "sb_form_q", timeToWait=60)
                searchbar.clear()
                sleep(1)
                searchbar.send_keys(relatedKeyword)
                sleep(1)
                searchbar.submit()

                logging.info(f"[COOLDOWN] Applying cooldown after related search #{i+1}")
                cooldown()
            except Exception as e:
                logging.error(f"Error searching {relatedKeyword}: {e}")

        logging.info(f"[BING] Completed search cycle for trend: {primaryKeyword}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Ensure progress is saved when exiting"""
        # NEW: Only save progress if in custom mode
        if (hasattr(self, 'use_custom_limits') and 
            self.use_custom_limits and 
            hasattr(self, 'search_progress')):
            self.usedKeywordsShelf["searchProgress"] = self.search_progress
            self.usedKeywordsShelf.sync()  # Force immediate save
    
        # Original closing logic remains unchanged
        self.googleTrendsShelf.close()
        self.usedKeywordsShelf.close()