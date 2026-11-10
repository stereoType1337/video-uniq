from __future__ import annotations


class AppError(Exception):
    """Base for all domain exceptions."""


class UserNotFoundError(AppError):
    def __init__(self, tg_id: int) -> None:
        super().__init__(f"User {tg_id} not found")
        self.tg_id = tg_id


class InsufficientBalanceError(AppError):
    def __init__(self, user_id: int, required: int, balance: int) -> None:
        super().__init__(f"User {user_id} needs {required} credits, has {balance}")
        self.user_id = user_id
        self.required = required
        self.balance = balance


class FileTooLargeError(AppError):
    def __init__(self, size_mb: float, limit_mb: int) -> None:
        super().__init__(f"File is {size_mb:.1f} MB, limit is {limit_mb} MB")
        self.size_mb = size_mb
        self.limit_mb = limit_mb


class RenderError(AppError):
    def __init__(self, copy_index: int, stderr_tail: str) -> None:
        super().__init__(f"Render of copy {copy_index} failed")
        self.copy_index = copy_index
        self.stderr_tail = stderr_tail


class FingerprintCollisionError(AppError):
    def __init__(self, copy_index: int, distance: int) -> None:
        super().__init__(f"Copy {copy_index} too close to an existing copy (distance {distance})")
        self.copy_index = copy_index
        self.distance = distance


class ProbeError(AppError):
    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"Cannot probe {path}: {reason}")
        self.path = path
        self.reason = reason


class JobNotFoundError(AppError):
    def __init__(self, job_id: int) -> None:
        super().__init__(f"Job {job_id} not found")
        self.job_id = job_id


class TooManyActiveJobsError(AppError):
    def __init__(self, user_id: int, limit: int) -> None:
        super().__init__(f"User {user_id} already has {limit} active jobs")
        self.user_id = user_id
        self.limit = limit


class SourceDownloadError(AppError):
    def __init__(self, file_id: str, reason: str) -> None:
        super().__init__(f"Cannot download {file_id}: {reason}")
        self.file_id = file_id
        self.reason = reason


class PaymentNotFoundError(AppError):
    def __init__(self, payment_id: int) -> None:
        super().__init__(f"Payment {payment_id} not found")
        self.payment_id = payment_id


class PaymentAmountMismatchError(AppError):
    def __init__(self, payment_id: int, expected: int, received: int) -> None:
        super().__init__(f"Payment {payment_id}: expected {expected}, received {received}")
        self.payment_id = payment_id
        self.expected = expected
        self.received = received
