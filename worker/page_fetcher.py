import abc
import os
import random
import sys
import time

from camoufox import Camoufox
from loguru import logger
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

MTS_LOCATION_CODE = os.getenv("MTS_LOCATION_CODE", "33000200100000000000000000")

# кнопка «Показать ещё» под каталогом
MORE_BUTTON = "button.pagination-new__more-button"


class AbstractPageFetcher(abc.ABC):
    @abc.abstractmethod
    def fetch(self, url: str, proxy_config: dict) -> str:
        """Возвращает HTML страницы после полной подгрузки каталога."""


def click_more(page) -> bool:
    """Нажимает «Показать ещё», если кнопка есть и активна. Возвращает True, если нажала."""
    button = page.locator(MORE_BUTTON).first
    try:
        if not button.is_visible() or not button.is_enabled():
            return False
        button.scroll_into_view_if_needed(timeout=3000)
        time.sleep(random.uniform(0.2, 0.6))
        button.click(timeout=5000)
        return True
    except (PlaywrightTimeout, PlaywrightError) as e:
        logger.debug(f"Не удалось нажать «Показать ещё»: {e}")
        return False


def load_all(page, timeout=900, max_idle=15):
    """
    Подгружает весь каталог: жмёт «Показать ещё», ждёт, крутит колесо вниз.
    Останавливается, когда кнопки нет, а высота страницы max_idle шагов подряд не растёт.
    """
    page.bring_to_front()
    best = page.evaluate("document.body.scrollHeight")
    idle = 0
    clicks = 0
    deadline = time.time() + timeout

    while idle < max_idle:
        if time.time() > deadline:
            raise TimeoutError(f"Подгрузка каталога не завершилась за {timeout} сек")

        if click_more(page):
            clicks += 1
            # ждём, пока высота вырастет (подгрузились новые карточки)
            try:
                page.wait_for_function(
                    "h => document.body.scrollHeight > h + 15", arg=best, timeout=10000
                )
            except PlaywrightTimeout:
                idle += 1  # нажали, но ничего не добавилось
                continue
            time.sleep(random.uniform(0.8, 1.5))

        page.mouse.wheel(0, random.randint(150, 350))
        time.sleep(random.uniform(0.05, 0.15))

        height = page.evaluate("document.body.scrollHeight")
        if height > best + 15:
            best, idle = height, 0
            logger.info(f"Высота страницы: {best} (нажатий «Показать ещё»: {clicks})")
        else:
            idle += 1

    logger.info(f"Каталог загружен, нажатий «Показать ещё»: {clicks}")


class CamoufoxPageFetcher(AbstractPageFetcher):
    def fetch(self, url: str, proxy_config: dict) -> str:
        proxy_ip = proxy_config["server"].split("://")[-1].split(":")[0]
        with Camoufox(
                # в Docker экрана нет: Camoufox сам поднимает Xvfb (он есть в образе).
                # Virtual display бывает только на Linux, на маке для отладки открываем окно.
                headless="virtual" if sys.platform.startswith("linux") else False,
                proxy=proxy_config,
                geoip=proxy_ip,  # IP известен заранее: Camoufox не ходит за ним через прокси
                humanize=True,
                firefox_user_prefs={
                    "permissions.default.image": 2,
                    # не душим таймеры и не усыпляем документ, даже если вкладка не в фокусе
                    "dom.timeout.enable_budget_timer_throttling": False,
                    "dom.min_background_timeout_value": 4,
                    "dom.suspend_inactive.enabled": False,
                }
        ) as browser:
            context = browser.new_context()
            context.add_cookies([{
                "name": "MTS_LOCATION_CODE",
                "value": MTS_LOCATION_CODE,
                "domain": ".mts.ru",
                "path": "/",
            }])
            page = context.new_page()

            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # ждём реальный каталог (кнопка есть, если товаров больше одной страницы)
            try:
                page.wait_for_selector(MORE_BUTTON, timeout=15000)
            except PlaywrightTimeout:
                logger.warning("Кнопка «Показать ещё» не появилась: возможно, одна страница или заглушка")

            load_all(page)
            return page.content()
