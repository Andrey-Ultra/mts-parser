import json
import os
import sys
import time

from loguru import logger

from data_parser import InvalidHtml, get_product_data
from page_fetcher import AbstractPageFetcher, CamoufoxPageFetcher
from proxy_repository import (
    PROXY_GIVER_URL, AbstractProxyRepository, ApiProxyRepository, FakeProxyRepository,
)
from task_manager_repository import (
    STM_URL, AbstractTaskManagerRepository, FakeTaskManagerRepository, SimpleTaskManagerRepository,
)

# ------------ CONFIG ------------

# prod — боевой режим (Docker, задаётся в Dockerfile): очередь и прокси настоящие.
# dev  — локальная отладка: фейковая очередь с одной задачей и один прокси.
APP_ENV = os.getenv("APP_ENV", "dev")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")  # DEBUG покажет и каждый пропущенный дубль

IDLE_SLEEP = 1    # очередь пуста
ERROR_SLEEP = 5   # очередь недоступна, нет прокси или задача упала


def setup_logging():
    logger.remove()
    # diagnose=False: иначе loguru печатает значения переменных в трейсбеке, а там логин и пароль прокси
    logger.add(sys.stderr, level=LOG_LEVEL, backtrace=True, diagnose=False)


def build_dependencies():
    if APP_ENV == "prod":
        return ApiProxyRepository(), SimpleTaskManagerRepository(), CamoufoxPageFetcher()
    return FakeProxyRepository(), FakeTaskManagerRepository(), CamoufoxPageFetcher()


# ------------ TASK ------------

def release_quietly(task_manager_repo: AbstractTaskManagerRepository, job) -> None:
    """Возвращает задачу в очередь. Сбой самой очереди не должен ронять воркер."""
    try:
        task_manager_repo.release(job.id)
        logger.info(f"[{job.id}] Задача возвращена в очередь")
    except Exception:
        logger.exception(f"[{job.id}] Не удалось вернуть задачу в очередь (вернётся сама по TASK_TIMEOUT)")


def handle(job, proxy: dict, page_fetcher: AbstractPageFetcher) -> list[dict]:
    """Загружает каталог и разбирает карточки. Бросает исключение, если что-то пошло не так."""
    logger.info(f"[{job.id}] Открываю {job.task} через прокси {proxy['server']}")

    started = time.monotonic()
    html = page_fetcher.fetch(job.task, proxy)
    logger.info(f"[{job.id}] Страница получена: {len(html) // 1024} КБ за {time.monotonic() - started:.0f} с")

    products = get_product_data(html)
    logger.debug(f"[{job.id}] Первые товары: {products[:3]}")
    return products


def run(
        proxy_repo: AbstractProxyRepository,
        task_manager_repo: AbstractTaskManagerRepository,
        page_fetcher: AbstractPageFetcher,
) -> None:
    while True:
        # ---------- берём задачу ----------
        try:
            job = task_manager_repo.take()
        except Exception as e:
            logger.warning(f"Очередь недоступна: {e}")
            time.sleep(ERROR_SLEEP)
            continue
        if job is None:
            time.sleep(IDLE_SLEEP)
            continue

        started = time.monotonic()
        logger.info(f"[{job.id}] Взял задачу {job.task}")

        # ---------- берём прокси ----------
        proxy = proxy_repo.get()
        if proxy is None:
            logger.warning(f"[{job.id}] Нет доступных прокси")
            release_quietly(task_manager_repo, job)
            time.sleep(ERROR_SLEEP)
            continue

        # ---------- загружаем и разбираем ----------
        try:
            products = handle(job, proxy, page_fetcher)
        except InvalidHtml as e:
            logger.warning(f"[{job.id}] Страница не прошла проверку: {e}")
            release_quietly(task_manager_repo, job)
            time.sleep(ERROR_SLEEP)
            continue
        except Exception:
            logger.exception(f"[{job.id}] Ошибка задачи {job.task}")
            release_quietly(task_manager_repo, job)
            time.sleep(ERROR_SLEEP)
            continue

        # ---------- отдаём результат ----------
        try:
            accepted = task_manager_repo.answer(job.id, json.dumps(products, ensure_ascii=False))
        except Exception:
            logger.exception(f"[{job.id}] Не удалось отправить ответ")
            release_quietly(task_manager_repo, job)
            time.sleep(ERROR_SLEEP)
            continue

        elapsed = time.monotonic() - started
        if accepted:
            logger.info(f"[{job.id}] Готово: {len(products)} товаров в наличии, всего {elapsed:.0f} с")
        else:
            logger.warning(f"[{job.id}] Ответ не принят: задачу отменили или истёк TASK_TIMEOUT")


def main():
    setup_logging()
    logger.info(f"Воркер запущен: APP_ENV={APP_ENV}, STM_URL={STM_URL}, PROXY_GIVER_URL={PROXY_GIVER_URL}")
    proxy_repo, task_manager_repo, page_fetcher = build_dependencies()
    try:
        run(proxy_repo, task_manager_repo, page_fetcher)
    except KeyboardInterrupt:
        logger.info("Воркер остановлен")


if __name__ == "__main__":
    main()
