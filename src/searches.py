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
    EXPONENTIAL = auto()
    CONSTANT = auto()

class Searches:
    maxRetries: Final[int] = CONFIG.get("retries").get("max")
    baseDelay: Final[float] = CONFIG.get("retries").get("base_delay_in_seconds")
    retriesStrategy = RetriesStrategy[CONFIG.get("retries").get("strategy")]

    def __init__(self, browser: Browser, num_additional_searches=2):
        self.browser = browser
        self.webdriver = browser.webdriver
        self.num_additional_searches = num_additional_searches

        dumbDbm = dbm.dumb.open((getProjectRoot() / "google_trends").__str__())
        self.googleTrendsShelf: shelve.Shelf = shelve.Shelf(dumbDbm)
        
        loadDate: date | None = self.googleTrendsShelf.get(LOAD_DATE_KEY)
        if loadDate is None or loadDate < date.today():
            self.refreshTrends()

    def refreshTrends(self):
        """Refresh stored trends when outdated."""
        self.googleTrendsShelf.clear()
        self.googleTrendsShelf[LOAD_DATE_KEY] = date.today()
        trends = self.getGoogleTrends(self.browser.getRemainingSearches(desktopAndMobile=True).getTotal())
        shuffle(trends)
        for trend in trends:
            self.googleTrendsShelf[trend] = None

    def getGoogleTrends(self, wordsCount: int) -> list[str]:
        """Fetch trending keywords."""
        try:
            trends = Trends().trending_now(geo=self.browser.localeGeo)[:wordsCount]
            return [t.keyword.lower() for t in trends]
        except Exception as e:
            logging.error(f"Error fetching trends: {e}")
            return []

    def getRelatedTerms(self, term: str) -> list[str]:
        """Retrieve related terms using Bing autocomplete API."""
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
        """Perform Bing searches with exact count tracking."""
        logging.info(f"[BING] Starting searches for {self.browser.browserType.capitalize()} Edge...")

        # Fetch initial search limits
        remainingSearches = self.browser.getRemainingSearches(desktopAndMobile=True)
        targetSearches = remainingSearches.getTotal()
        performedSearches = 0

        if targetSearches == 0:
            logging.info("[BING] No searches needed.")
            return

        self.browser.utils.goToSearch()

        while performedSearches < targetSearches:
            if len(self.googleTrendsShelf) <= 1:  # Only has loadDate
                self.refreshTrends()

            primaryKeyword = next((k for k in self.googleTrendsShelf.keys() if k != LOAD_DATE_KEY), None)
            if not primaryKeyword:
                logging.error("[BING] No keywords available for search.")
                break

            relatedKeywords = self.getRelatedTerms(primaryKeyword)

            # Perform primary search
            self.bingSearch(primaryKeyword)
            performedSearches += 1
            del self.googleTrendsShelf[primaryKeyword]
            
            # Perform related searches within limit
            for _ in range(min(self.num_additional_searches, len(relatedKeywords))):
                if performedSearches >= targetSearches:
                    break
                relatedKeyword = relatedKeywords.pop(0)
                self.bingSearch(relatedKeyword)
                performedSearches += 1

        logging.info(f"[BING] Finished {self.browser.browserType.capitalize()} Edge searches!")

    def bingSearch(self, keyword: str) -> None:
        """Execute a single Bing search."""
        try:
            self.browser.utils.goToSearch()
            searchbar = self.browser.utils.waitUntilClickable(By.ID, "sb_form_q", timeToWait=60)
            searchbar.clear()
            sleep(1)
            searchbar.send_keys(keyword)
            sleep(1)
            searchbar.submit()

            logging.info(f"[COOLDOWN] Applying cooldown after searching: {keyword}")
            cooldown()
        except Exception as e:
            logging.error(f"Error searching {keyword}: {e}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.googleTrendsShelf.__exit__(None, None, None)
