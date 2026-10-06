"""Per-binding source scan deadlines; wake-up hints never advance a cursor."""
import math


class ScanSchedule:
    def __init__(self, minimum_interval=10, poll_interval=600):
        self.minimum_interval = float(minimum_interval)
        self.poll_interval = float(poll_interval)
        if not math.isfinite(self.minimum_interval) or not 0 <= self.minimum_interval <= 600:
            raise ValueError('source_min_interval 必须在 0～600 秒之间')
        if not math.isfinite(self.poll_interval) or self.poll_interval <= 0:
            raise ValueError('poll_interval 必须是大于零的有限秒数')
        self.allowed_at = 0
        self.retry_at = 0
        self.due_at = 0

    def request(self, now):
        self.due_at = min(self.due_at, max(now, self.allowed_at, self.retry_at))

    def ready(self, now):
        return now >= self.due_at

    def begin(self, now):
        self.allowed_at = now + self.minimum_interval
        self.due_at = math.inf

    def finish(self, now, delivered):
        self.retry_at = 0
        self.due_at = max(self.allowed_at, now + (0 if delivered else self.poll_interval))

    def fail(self, now):
        self.retry_at = now + min(60, self.poll_interval)
        self.due_at = max(self.allowed_at, self.retry_at)
