import abc
import os
from dataclasses import dataclass

from stm import Worker

STM_URL = os.getenv("STM_URL", "http://localhost:8080")

class AbstractTaskManagerRepository(abc.ABC):
    @abc.abstractmethod
    def take(self): ...

    @abc.abstractmethod
    def release(self, task_id: int) -> bool: ...

    @abc.abstractmethod
    def answer(self, task_id: int, answer: str ) -> bool: ...


@dataclass
class Job:
    def __init__(self, id: int, task: str) -> None:
        self.id = id
        self.task = task




class FakeTaskManagerRepository(AbstractTaskManagerRepository):
    def __init__(self):
        self.count = 0

    def take(self):
        if self.count == 0:
            self.count = 1
            return Job(1, "https://shop.mts.ru/catalog/smartfony/")
        else:
            exit(0)


    def release(self, task_id: int) -> bool:
        return True

    def answer(self, task_id: int, answer: str) -> bool:
        return True

class SimpleTaskManagerRepository(AbstractTaskManagerRepository):
    def __init__(self):
        self.worker = Worker(STM_URL)

    def take(self):
        return self.worker.take()

    def release(self, task_id: int) -> bool:
        return self.worker.release(task_id)

    def answer(self, task_id: int, answer: str) -> bool:
        return self.worker.answer(task_id, answer)