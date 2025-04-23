import dbm.dumb
import logging
import shelve
from time import sleep
from random import randint

from selenium.webdriver.common.by import By
from trendspy import Trends

from src.browser import Browser
from src.utils import CONFIG, getProjectRoot, cooldown, COUNTRY


class Searches:
    """
    Class to handle searches in MS Rewards.
    """

    def __init__(self, browser: Browser, num_additional_searches=2):
        self.browser = browser
        self.webdriver = browser.webdriver
        self.num_additional_searches = num_additional_searches  # Customizable additional searches

        dumbDbm = dbm.dumb.open((getProjectRoot() / "google_trends").__str__())
        self.googleTrendsShelf: shelve.Shelf = shelve.Shelf(dumbDbm)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.googleTrendsShelf.__exit__(None, None, None)

    def bingSearches(self) -> None:
        # Function to perform Bing searches
        logging.info(f"[BING] Starting {self.browser.browserType.capitalize()} Edge Bing searches...")

        self.browser.utils.goToSearch()

        while True:
            desktopAndMobileRemaining = self.browser.getRemainingSearches(desktopAndMobile=True)
            logging.info(f"[BING] Remaining searches={desktopAndMobileRemaining}")

            if (
                (self.browser.browserType == "desktop" and desktopAndMobileRemaining.desktop == 0) or
                (self.browser.browserType == "mobile" and desktopAndMobileRemaining.mobile == 0)
            ):
                break

            if desktopAndMobileRemaining.getTotal() > len(self.googleTrendsShelf):
                logging.debug(f"google_trends before load = {list(self.googleTrendsShelf.items())}")
                trends = Trends().trending_now(geo=COUNTRY)[:desktopAndMobileRemaining.getTotal()]
                for trend in trends:
                    self.googleTrendsShelf[trend.keyword] = trend
                logging.debug(f"google_trends after load = {list(self.googleTrendsShelf.items())}")

            # Search all available trends sequentially
            while self.googleTrendsShelf:
                self.bingSearch()
                sleep(randint(10, 15))

        logging.info(f"[BING] Finished {self.browser.browserType.capitalize()} Edge Bing searches!")

    def bingSearch(self) -> None:
        # Function to perform a single Bing search (Primary + Additional)
        pointsBefore = self.browser.utils.getAccountPoints()

        if not self.googleTrendsShelf:
            logging.error("[BING] No trending keywords available.")
            return

        trend = list(self.googleTrendsShelf.keys())[0]
        trendKeywords = self.googleTrendsShelf[trend].trend_keywords
        logging.debug(f"Primary trend={trend}")
        logging.debug(f"Related trendKeywords={trendKeywords}")

        # Perform primary search
        self.browser.utils.goToSearch()
        searchbar = self.browser.utils.waitUntilClickable(By.ID, "sb_form_q", timeToWait=60)
        searchbar.clear()
        primaryKeyword = trend
        logging.debug(f"Primary trendKeyword={primaryKeyword}")
        sleep(1)
        searchbar.send_keys(primaryKeyword)
        sleep(1)
        searchbar.submit()

        pointsAfter = self.browser.utils.getAccountPoints()
        if pointsBefore < pointsAfter:
            del self.googleTrendsShelf[trend]  # Remove searched primary keyword

        logging.info("[COOLDOWN] Applying cooldown after primary search")
        cooldown()  # Cooldown after primary search

        # Perform additional searches using **related trendKeywords**
        for i in range(min(self.num_additional_searches, len(trendKeywords))):
            relatedKeyword = trendKeywords.pop(0)
            logging.debug(f"Related trendKeyword #{i+1}={relatedKeyword}")

            try:
                self.browser.utils.goToSearch()  # Refresh before searching
                searchbar = self.browser.utils.waitUntilClickable(By.ID, "sb_form_q", timeToWait=60)
                searchbar.clear()
                sleep(1)
                searchbar.send_keys(relatedKeyword)
                sleep(1)
                searchbar.submit()

                logging.info(f"[COOLDOWN] Applying cooldown after related search #{i+1}")
                cooldown()  # Cooldown after every additional search

            except Exception as e:
                logging.error(f"Error searching {relatedKeyword}: {e}")

        logging.info(f"[BING] Completed search cycle for trend: {trend}")
