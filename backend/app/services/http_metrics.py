"""有容量限制且线程安全的进程计数器，不保留请求内容或请求标识。"""
from copy import deepcopy
from threading import Lock

BUCKETS = (100, 500, 1000, 5000, 30000)
METHODS = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'}


class HttpMetrics:
    def __init__(self):
        self._lock = Lock()
        self._active = self._peak = self._total = self._errors = self._interrupted = 0
        self._routes = {}

    def begin(self):
        with self._lock:
            self._active += 1
            self._peak = max(self._peak, self._active)

    def finish(self, method, route, status, duration_ms, outcome):
        method = method if method in METHODS else 'OTHER'
        key = f'{method} {route}'[:200]
        with self._lock:
            self._active -= 1
            self._total += 1
            self._errors += int(status >= 500)
            self._interrupted += int(outcome != 'completed')
            if key not in self._routes and len(self._routes) >= 128:
                key = 'other'
            row = self._routes.setdefault(key, dict(requests=0, server_errors=0, interrupted=0,
                status_classes={}, duration_ms_sum=0, duration_ms_max=0, duration_buckets=[0]*6))
            row['requests'] += 1
            row['server_errors'] += int(status >= 500)
            row['interrupted'] += int(outcome != 'completed')
            status_class = f'{status // 100}xx' if 100 <= status <= 599 else 'other'
            row['status_classes'][status_class] = row['status_classes'].get(status_class, 0) + 1
            duration_ms = max(0, duration_ms)
            row['duration_ms_sum'] += duration_ms
            row['duration_ms_max'] = max(row['duration_ms_max'], duration_ms)
            bucket = next((i for i, bound in enumerate(BUCKETS) if duration_ms <= bound), 5)
            row['duration_buckets'][bucket] += 1

    def snapshot(self):
        with self._lock:
            return dict(scope='current_process_since_start', active=self._active, peak_active=self._peak,
                requests=self._total, server_errors=self._errors, interrupted=self._interrupted,
                duration_bucket_upper_ms=[*BUCKETS, None], routes=deepcopy(self._routes),
                note='Duration includes streaming lifetime; buckets are non-cumulative. Not cluster capacity.')


http_metrics = HttpMetrics()
