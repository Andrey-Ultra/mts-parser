import csv
import io
import itertools
import json
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import docker
import requests
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from stm import Client, STMError, TaskNotFound

STM_URL = os.getenv("STM_URL", "http://localhost:8080")
PROXY_GIVER_URL = os.getenv("PROXY_GIVER_URL", "http://localhost:9001")
WORKER_IMAGE = os.getenv("WORKER_IMAGE", "mts-parser-worker")
WORKER_NETWORK = os.getenv("WORKER_NETWORK", "mts-parser")
STATE_FILE = Path(os.getenv("DATA_DIR", "data")) / "tasks.json"
LIST_FILE = STATE_FILE.with_name("list.txt")  # текст поля со ссылками

WORKER_LABEL = "mts-parser.role"
PROXY_RE = re.compile(r"^[\d.]+:\d+:[^:\s]+:[^:\s]+$")
ACTIVE = ("in queue", "in work")

stm = Client(STM_URL)
lock = threading.Lock()
tasks: list[dict] = []  # {"url", "id" (id в stm), "status", "products"}
generation = 0  # растёт при каждой замене списка: опрос старого списка отбрасываем
queue_error: str | None = None


# ------------ STATE ------------

def save():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tasks, ensure_ascii=False))
    tmp.replace(STATE_FILE)


def check(task: dict) -> dict:
    """Свежее состояние задачи из stm. Готовый ответ stm отдаёт один раз, поэтому сразу сохраняем его."""
    try:
        status = stm.status(task["id"])
        if status != "complete":
            return {"status": status}
        return {"status": "done", "products": json.loads(stm.answer(task["id"]))}
    except TaskNotFound:          # stm забыл задачу: контейнер очереди перезапускали
        return {"status": "lost"}
    except (TypeError, ValueError):  # ответа нет или он не JSON
        return {"status": "error"}


def cancel_quietly(task_id: int):
    try:
        stm.cancel(task_id)
    except TaskNotFound:
        pass


def resubmit_active():
    """stm хранит задачи в памяти, после его перезапуска старые id ничего не значат
    (и могут совпасть с новыми). Снимаем старые id и ставим все незавершённые задачи заново."""
    active = [t for t in tasks if t["status"] in ACTIVE]
    for t in active:
        cancel_quietly(t["id"])
    for t in active:
        t.update(id=stm.submit(t["url"]), status="in queue")


def poll_tasks():
    global queue_error
    while True:
        with lock:
            gen, pending = generation, [t for t in tasks if t["status"] in ACTIVE]
        try:
            updates = [(t, check(t)) for t in pending]
            with lock:
                if gen == generation:
                    for t, update in updates:
                        if update["status"] != "lost":
                            t.update(update)
                    lost = any(u["status"] == "lost" for _, u in updates)
                    if lost:
                        resubmit_active()
                    if lost or any(t["status"] not in ACTIVE for t, _ in updates):
                        save()
            queue_error = None
        except STMError as e:
            queue_error = f"Очередь недоступна: {e}"
        time.sleep(2)


@asynccontextmanager
async def lifespan(_):
    if STATE_FILE.exists():
        tasks.extend(json.loads(STATE_FILE.read_text()))
    for t in tasks:  # «потерянные» из старых версий панели — пусть опрос поставит их заново
        if t["status"] == "lost":
            t["status"] = "in queue"
    threading.Thread(target=poll_tasks, daemon=True).start()
    yield


app = FastAPI(title="МТС parser", lifespan=lifespan)


class ProxyGiverError(Exception):
    pass


@app.exception_handler(ProxyGiverError)
async def proxy_giver_error(_, exc):
    return JSONResponse({"detail": f"proxy-giver недоступен: {exc}"}, status_code=502)


@app.exception_handler(STMError)
async def stm_error(_, exc):
    return JSONResponse({"detail": f"Очередь недоступна: {exc}"}, status_code=502)


@app.exception_handler(docker.errors.DockerException)
async def docker_error(_, exc):
    return JSONResponse({"detail": f"Docker: {exc}"}, status_code=502)


def unique_lines(text: str) -> list[str]:
    """Непустые строки без дублей, порядок сохраняется."""
    return list(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))


# ------------ PROXIES ------------

def proxy_giver(method: str, path: str, **params) -> dict:
    try:
        response = requests.request(method, f"{PROXY_GIVER_URL}{path}", params=params, timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        raise ProxyGiverError(e) from e


@app.get("/api/proxies")
def get_proxies():
    return {"proxies": proxy_giver("GET", "/get-all-proxies")["proxies"]}


@app.put("/api/proxies")
def set_proxies(text: str = Body(..., embed=True)):
    new = unique_lines(text)
    bad = [p for p in new if not PROXY_RE.match(p)]
    if bad:
        raise HTTPException(400, "Нужен формат ip:port:login:password, ошибка в: " + ", ".join(bad))

    current = proxy_giver("GET", "/get-all-proxies")["proxies"]
    removed = [p for p in current if p not in new]
    added = [p for p in new if p not in current]
    for p in removed:
        proxy_giver("POST", "/remove-proxy", proxy=p)
    for p in added:
        proxy_giver("POST", "/add-proxy", proxy=p)
    return {"added": len(added), "removed": len(removed), **get_proxies()}


# ------------ TASKS ------------

@app.get("/api/tasks")
def get_tasks():
    with lock:
        items = [{
            "url": t["url"],
            "status": t["status"],
            "count": None if t["products"] is None else len(t["products"]),
        } for t in tasks]
    done = sum(t["status"] == "done" for t in items)
    return {"tasks": items, "done": done, "total": len(items), "queue_error": queue_error}


@app.post("/api/tasks")
def replace_tasks(text: str = Body(..., embed=True)):
    """Отменяет все прошлые задачи (и их результаты) и ставит новый список."""
    urls = unique_lines(text)
    bad = [u for u in urls if not u.startswith(("http://", "https://"))]
    if bad:
        raise HTTPException(400, "Это не ссылки: " + ", ".join(bad))

    global generation
    with lock:
        generation += 1
        try:
            for t in tasks:
                if t["status"] in ACTIVE:
                    cancel_quietly(t["id"])
            tasks.clear()
            for url in urls:
                tasks.append({"url": url, "id": stm.submit(url), "status": "in queue", "products": None})
        finally:
            save()
    return get_tasks()


@app.get("/api/list")
def get_list():
    return {"text": LIST_FILE.read_text() if LIST_FILE.exists() else ""}


@app.put("/api/list")
def save_list(text: str = Body(..., embed=True)):
    with lock:
        LIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LIST_FILE.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(LIST_FILE)
    return {"text": text}


@app.get("/api/results.csv")
def results_csv():
    """Снимок готовых результатов: можно качать в любой момент.

    Формат: разделитель «;», UTF-8 с BOM, без заголовка. Колонки:
    артикул; название; цена («Без комплекта», если комплект есть); цена «В комплекте» (0, если нет);
    старая зачёркнутая цена (0, если нет); ссылка на товар.
    """
    with lock:
        products = [p for t in tasks if t["status"] == "done" for p in t["products"]]
        complete = bool(tasks) and all(t["status"] == "done" for t in tasks)

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\n")
    writer.writerows(
        (
            p.get("product_id"),
            p.get("name"),
            p.get("price"),
            p.get("bundle_price", 0),
            p.get("old_price", 0),
            p.get("url"),
        )
        for p in products
    )
    name = f"mts-{datetime.now():%Y%m%d-%H%M}{'' if complete else '-partial'}.csv"
    return Response(
        buf.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ------------ WORKERS ------------

_docker = None


def dock():
    global _docker
    if _docker is None:
        _docker = docker.from_env()
    return _docker


def worker_containers():
    return dock().containers.list(all=True, filters={"label": f"{WORKER_LABEL}=worker"})


def get_worker(name: str):
    container = dock().containers.get(name)
    if container.labels.get(WORKER_LABEL) != "worker":
        raise HTTPException(404, f"{name} не воркер")
    return container


@app.get("/api/workers")
def list_workers():
    return sorted(({
        "name": c.name,
        "status": c.status,
        "created": c.attrs["Created"][:19] + "Z",
    } for c in worker_containers()), key=lambda w: w["name"])


@app.post("/api/workers")
def create_worker():
    try:
        dock().images.get(WORKER_IMAGE)
    except docker.errors.ImageNotFound:
        raise HTTPException(400, f"Нет образа {WORKER_IMAGE}, соберите его: docker compose build worker")

    names = {c.name for c in worker_containers()}
    name = next(f"mts-worker-{i}" for i in itertools.count(1) if f"mts-worker-{i}" not in names)
    dock().containers.run(
        WORKER_IMAGE,
        name=name,
        detach=True,
        labels={WORKER_LABEL: "worker"},
        network=WORKER_NETWORK,
        environment={"STM_URL": STM_URL, "PROXY_GIVER_URL": PROXY_GIVER_URL, "TZ": os.getenv("TZ", "UTC")},
        restart_policy={"Name": "unless-stopped"},
        shm_size="1g",  # Firefox падает на стандартных 64 МБ /dev/shm
    )
    return {"name": name}


@app.delete("/api/workers/{name}")
def delete_worker(name: str):
    get_worker(name).remove(force=True)
    return {"name": name}


@app.get("/api/workers/{name}/logs", response_class=PlainTextResponse)
def worker_logs(name: str, tail: int = 500):
    return get_worker(name).logs(tail=tail).decode("utf-8", "replace")


class NoCacheStaticFiles(StaticFiles):
    """Браузер всегда перепроверяет страницу, иначе после обновления панели висит старая версия."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/", NoCacheStaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
