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

class RetriesStrategy(Enum):
    """Identical to original docstrings"""
    EXPONENTIAL = auto()
    CONSTANT = auto()

class Searches:
    """
    Class to handle searches in MS Rewards.
    Version 2.3 - Optimized logging sequence
    """
    maxRetries: Final[int] = CONFIG.get("retries").get("max")
    baseDelay: Final[float] = CONFIG.get("retries").get("base_delay_in_seconds")
    retriesStrategy = RetriesStrategy[CONFIG.get("retries").get("strategy")]

    def __init__(self, browser: Browser, num_additional_searches=2):
        self.browser = browser
        self.webdriver = browser.webdriver
        self.num_additional_searches = num_additional_searches

        # Device-specific shelf
        dumbDbm = dbm.dumb.open((getProjectRoot() / "google_trends").__str__())
        self.googleTrendsShelf: shelve.Shelf = shelve.Shelf(dumbDbm)
        
        # Global keyword tracker
        globalDbm = dbm.dumb.open((getProjectRoot() / GLOBAL_KEYWORDS_DB).__str__())
        self.usedKeywordsShelf: shelve.Shelf = shelve.Shelf(globalDbm)
        
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
        """Version 2.3 - Exact counting with cross-device deduplication"""
        logging.info(f"[BING] Starting {self.browser.browserType.capitalize()} Edge Bing searches...")
        self.browser.utils.goToSearch()

        while True:
            remaining = self.browser.getRemainingSearches(desktopAndMobile=True)
            logging.info(f"[BING] Remaining searches={remaining}")
            
            if ((self.browser.browserType == "desktop" and remaining.desktop <= 0) or
                (self.browser.browserType == "mobile" and remaining.mobile <= 0)):
                break
                
            needed_searches = remaining.desktop if self.browser.browserType == "desktop" else remaining.mobile
            
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
                logging.debug(f"TRENDS LOADED: {list(self.googleTrendsShelf.keys())}")

            for _ in range(needed_searches):
                if ((self.browser.browserType == "desktop" and remaining.desktop <= 0) or
                    (self.browser.browserType == "mobile" and remaining.mobile <= 0)):
                    break
                    
                self.bingSearch()
                sleep(randint(10, 15))
                
                remaining = self.browser.getRemainingSearches(desktopAndMobile=True)
                logging.info(f"[BING] Updated remaining searches={remaining}")

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

        # 4. NEW: Show related terms summary AFTER primary search but BEFORE additional searches
        logging.debug(
            f"RELATED TERMS SUMMARY: Found {len(relatedKeywords)} terms - "
            f"{relatedKeywords[:10]}"  # First 10 terms only
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
        self.googleTrendsShelf.close()
        self.usedKeywordsShelf.close()