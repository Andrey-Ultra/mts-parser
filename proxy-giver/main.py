from fastapi import FastAPI
from collections import deque

app = FastAPI()
proxies: deque[str] = deque()

@app.post("/add-proxy")
async def add_proxy(proxy: str):
    if proxy in proxies:
        return {"status": "error", "message": f"Прокси {proxy} уже добавлен"}
    proxies.append(proxy)
    return {"status": f"Прокси {proxy} добавлен"}


@app.post("/remove-proxy")
async def remove_proxy(proxy: str):
    try:
        proxies.remove(proxy)
    except ValueError:
        return {"status": "error", "message": f"Прокси {proxy} не найден"}
    return {"status": "success"}


@app.post("/remove-all-proxies")
async def remove_all_proxies():
    count = len(proxies)
    proxies.clear()
    return {"status": "success", "message": f"Удалено прокси: {count}"}


@app.get("/get-random-proxy")
async def get_random_proxy():
    if not proxies:
        return {"status": "error"}

    proxy = proxies.popleft()
    proxies.append(proxy)

    return {"status": "success", "proxy": proxy}


@app.get("/get-all-proxies")
async def get_all_proxies():
    return {"count": len(proxies), "proxies": list(proxies)}