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

class RetriesStrategy(Enum):
    """Identical to original docstrings"""
    EXPONENTIAL = auto()
    CONSTANT = auto()

class Searches:
    """
    Class to handle searches in MS Rewards.
    """
    maxRetries: Final[int] = CONFIG.get("retries").get("max")
    baseDelay: Final[float] = CONFIG.get("retries").get("base_delay_in_seconds")
    retriesStrategy = RetriesStrategy[CONFIG.get("retries").get("strategy")]

    def __init__(self, browser: Browser, num_additional_searches=2):
        self.browser = browser
        self.webdriver = browser.webdriver
        self.num_additional_searches = num_additional_searches

        dumbDbm = dbm.dumb.open((getProjectRoot() / "google_trends").__str__())
        self.googleTrendsShelf: shelve.Shelf = shelve.Shelf(dumbDbm)
        logging.debug(f"googleTrendsShelf.__dict__ = {self.googleTrendsShelf.__dict__}")
        logging.debug(f"google_trends = {list(self.googleTrendsShelf.items())}")
        
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
                self.googleTrendsShelf[trend] = None
            logging.debug(f"google_trends after load = {list(self.googleTrendsShelf.items())}")

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
        logging.info(f"[BING] Starting {self.browser.browserType.capitalize()} Edge Bing searches...")
        self.browser.utils.goToSearch()

        while True:
            desktopAndMobileRemaining = self.browser.getRemainingSearches(desktopAndMobile=True)
            logging.info(f"[BING] Remaining searches={desktopAndMobileRemaining}")

            if ((self.browser.browserType == "desktop" and desktopAndMobileRemaining.desktop == 0) or
                (self.browser.browserType == "mobile" and desktopAndMobileRemaining.mobile == 0)):
                break

            if not self.googleTrendsShelf or len(self.googleTrendsShelf) <= 1:  # Only has loadDate
                logging.debug("Refreshing trends cache...")
                trends = self.getGoogleTrends(desktopAndMobileRemaining.getTotal())
                shuffle(trends)
                for trend in trends:
                    self.googleTrendsShelf[trend] = None
                self.googleTrendsShelf[LOAD_DATE_KEY] = date.today()

            while len(self.googleTrendsShelf) > 1:  # More than just loadDate
                self.bingSearch()
                sleep(randint(10, 15))

        logging.info(f"[BING] Finished {self.browser.browserType.capitalize()} Edge Bing searches!")

    def bingSearch(self) -> None:
        availableTrends = [k for k in self.googleTrendsShelf.keys() if k != LOAD_DATE_KEY]
        if not availableTrends:
            logging.error("[BING] No trending keywords available.")
            return

        primaryKeyword = availableTrends[0]
        relatedKeywords = self.getRelatedTerms(primaryKeyword)

        logging.debug(f"Primary trend={primaryKeyword}")
        logging.debug(f"Fetched related keywords={relatedKeywords}")

        # Perform primary search
        self.browser.utils.goToSearch()
        searchbar = self.browser.utils.waitUntilClickable(By.ID, "sb_form_q", timeToWait=60)
        searchbar.clear()
        sleep(1)
        searchbar.send_keys(primaryKeyword)
        sleep(1)
        searchbar.submit()

        # Always remove the keyword after searching
        if primaryKeyword in self.googleTrendsShelf:
            del self.googleTrendsShelf[primaryKeyword]
            logging.debug(f"Removed used keyword: {primaryKeyword}")

        logging.info("[COOLDOWN] Applying cooldown after primary search")
        cooldown()

        # Additional searches
        for i in range(min(self.num_additional_searches, len(relatedKeywords))):
            relatedKeyword = relatedKeywords.pop(0)
            logging.debug(f"Related trendKeyword #{i+1}={relatedKeyword}")

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
        self.googleTrendsShelf.__exit__(None, None, None)