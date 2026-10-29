import abc
import os
import re
from loguru import logger

import requests

PROXY_GIVER_URL = os.getenv("PROXY_GIVER_URL", "http://localhost:9001")


def parse_proxy(proxy_str: str) -> dict:
    pattern = r"([\d\.]+):(\d+):([^:]+):([^:\s]+)"
    match = re.search(pattern, proxy_str.strip())

    ip, port, username, password = match.groups()

    return {
        "server": f"{ip}:{port}",
        "username": username,
        "password": password
    }


class AbstractProxyRepository(abc.ABC):
    @abc.abstractmethod
    def get(self): ...


class FakeProxyRepository(AbstractProxyRepository):
    def get(self):
        return parse_proxy("")


class ApiProxyRepository(AbstractProxyRepository):
    def get(self):
        try:
            response = requests.get(f"{PROXY_GIVER_URL}/get-random-proxy", timeout=5)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"Не удалось связаться с proxy-giver: {e}")
            return None

        if data.get("status") != "success":
            logger.warning("Список прокси пуст, добавьте прокси в панели")
            return None
        return parse_proxy(data["proxy"])
