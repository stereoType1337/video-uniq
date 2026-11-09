from __future__ import annotations


class AppError(Exception):
    """Base for all domain exceptions."""


class UserNotFoundError(AppError):
    def __init__(self, tg_id: int) -> None:
        super().__init__(f"User {tg_id} not found")
        self.tg_id = tg_id


class InsufficientBalanceError(AppError):
    def __init__(self, tg_id: int, required: int, balance: int) -> None:
        super().__init__(f"User {tg_id} needs {required} credits, has {balance}")
        self.tg_id = tg_id
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
