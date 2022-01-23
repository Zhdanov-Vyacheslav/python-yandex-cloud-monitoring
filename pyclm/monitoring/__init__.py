import os
import sys
import weakref
import time
import jwt
import requests
import datetime
from multiprocessing import Process, Queue


API_IAM = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
API_MONITORING = "https://monitoring.api.cloud.yandex.net/monitoring/v2/data/write"
IAM_EXP = 4*60*60
AUTH_TYPE = "Bearer"
_format = {
    "ts": "string",
    "labels": "object",
    "metrics": [
        {
            "name": "string",
            "labels": "object",
            "type": "string",
            "ts": "string",
            "value": "number",
            "timeseries": [
                {
                    "ts": "string",
                    "value": "number"
                }
            ]
        }
    ]
}


class Monitoring:
    def __init__(
        self, credentials: dict = None, group_id: str = "default", resource_type: str = None, resource_id: str = None,
            elements: int = 100, period: int = 10, workers: int = 0
    ):
        args = [
            credentials, group_id, resource_type or str(os.uname()[1]), resource_id or str(os.getpid()),
            elements if 0 < elements <= 100 else 100, period
        ]
        self._send = PM(*args, workers=workers) if workers > 0 else Ingestion(*args)

    def _metric(
        self, name: str, value, t:str = "DGAUGE", ts: datetime = None,
        labels: dict = None, timeseries: list = None
    ):
        result = {
            "name": name,
            "value": value,
            "type": t,
            "ts": ts.isoformat() if ts is not None else datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        if labels is not None:
            result["labels"] = labels
        if timeseries is not None and len(timeseries) > 1:
            result["timeseries"] = timeseries
        self._send(result)

    #  Числовой показатель. Задается дробным числом.
    def dgauge(self, name: str, value: float, ts: datetime = None, labels: dict = None, timeseries: list = None):
        self._metric(name, value, "DGAUGE", ts, labels, timeseries)

    #  Числовой показатель. Задается целым числом.
    def igauge(self, name: str, value: int, ts: datetime = None, labels: dict = None, timeseries: list = None):
        self._metric(name, value, "IGAUGE", ts, labels, timeseries)

    #  Счетчик.
    def counter(self, name: str, value: float, ts: datetime = None, labels: dict = None, timeseries: list = None):
        self._metric(name, value, "COUNTER", ts, labels, timeseries)

    #  Производная.
    def rate(self, name: str, value: float, ts: datetime = None, labels: dict = None, timeseries: list = None):
        self._metric(name, value, "RATE", ts, labels, timeseries)


class Chrono(object):
    def __init__(self, carry: Monitoring = None, name="elapsed", labels: dict = None, mul=10**9):
        self.client = carry
        self.name = name
        self.labels = labels.copy() if labels is not None else {}
        self.mul = mul

    def __enter__(self):
        self._time_ns = time.time_ns()
        self._process_time_ns = time.process_time_ns()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.client is not None:
            self.client.dgauge(
                "process_" + self.name, (time.process_time_ns() - self._process_time_ns) / self.mul, labels=self.labels
            )
            self.client.dgauge(self.name, (time.time_ns() - self._time_ns) / self.mul, labels=self.labels)


class PM:
    def __init__(self, *args, workers: int = 1):
        self.workers = []
        self.queue = Queue()
        for _ in range(workers):
            proc = Process(target=self.process, args=args, kwargs={"queue": self.queue})
            self.workers.append(proc)
            proc.start()
        self._finalizer = weakref.finalize(self, self.finalize, 0)

    def finalize(self, c: int = 0):
        for _ in self.workers:
            self.queue.put(c)
        for proc in self.workers:
            proc.join()

    def __call__(self, value: dict):
        self.queue.put(value)

    @staticmethod
    def process(*args, queue: Queue = None):
        sender = Ingestion(*args)
        while True:
            value = queue.get()
            if isinstance(value, dict):
                sender(value)
            else:
                break


class Ingestion:
    def __init__(self, credentials, group_id, resource_type, resource_id, elements, period):
        self.credentials = credentials
        self._exp = 0
        self._token = None
        self._required = "?cloudId={cloudId}&folderId={folderId}&service=custom".format(**credentials)
        self.elements = elements
        self.metrics = []
        self.period = period
        self.labels = {
            "group_id": str(group_id),
            "resource_type": str(resource_type),
            "resource_id": str(resource_id)
        }
        self.timer = time.time() + self.period
        self._finalizer = weakref.finalize(self, self.finalize, 0)

    def finalize(self, c: int = 0):
        if len(self.metrics) > c:
            self._write()

    @property
    def _payload(self):
        result = {
            "labels": self.labels,
            "metrics": self.metrics
        }
        return result

    def _write(self):
        response = requests.post(
            url=API_MONITORING + self._required,
            json=self._payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": " ".join([AUTH_TYPE, self.iam_token])
            }
        )
        if response.status_code == 200 and response.json()["writtenMetricsCount"] == len(self.metrics):
            self.metrics = []
            self.timer = time.time() + self.period
        return response

    def __call__(self, value: dict):
        self.metrics.append(value)
        if len(self.metrics) >= self.elements or self.timer < time.time():
            self._write()

    @property
    def iam_token(self):
        if self._exp < time.time():
            response = requests.post(
                url=API_IAM,
                json={'jwt': self.jwt},
                headers={'Content-Type': 'application/json'}
            )
            if response.status_code == 200:
                self._token = response.json().get("iamToken")
                self._exp = time.time() + IAM_EXP
        return self._token

    @property
    def jwt(self):
        now = int(time.time())
        key = self.credentials["service_account_key"]
        return jwt.encode(
            {
                'aud': API_IAM,
                'iss': key["service_account_id"],
                'iat': now,
                'exp': now + 360
            },
            key["private_key"],
            algorithm='PS256',
            headers={'kid': key["id"]}
        )
