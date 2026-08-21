# -*- coding: utf-8 -*-
"""Простой rate limiting для логина (в памяти, без Redis).

Защита от брутфорса пароля: после N неудачных попыток с одного IP
следующие попытки отклоняются на заданное время (окно).

Ограничения: счётчики живут в памяти процесса — при перезапуске сбрасываются,
и не работают при нескольких воркерах. Для одно-двухпроцессного приложения
на VDS этого достаточно.
"""
import time
from collections import defaultdict
from threading import Lock

_lock = Lock()
# ip -> list[timestamp] неудачных попыток
_failures: dict[str, list[float]] = defaultdict(list)
# ip -> время блокировки (до какого момента заблокирован)
_blocked_until: dict[str, float] = {}

MAX_ATTEMPTS = 5          # попыток за окно
WINDOW_SECONDS = 300      # окно 5 минут
BLOCK_SECONDS = 600       # блокировка на 10 минут


def is_blocked(ip: str) -> bool:
    """True, если IP временно заблокирован."""
    with _lock:
        until = _blocked_until.get(ip, 0)
        if until > time.time():
            return True
        if until:
            _blocked_until.pop(ip, None)
        return False


def register_failure(ip: str) -> None:
    """Фиксирует неудачную попытку входа."""
    now = time.time()
    with _lock:
        lst = _failures[ip]
        # оставляем только попытки в окне
        _failures[ip] = [t for t in lst if now - t < WINDOW_SECONDS]
        _failures[ip].append(now)
        if len(_failures[ip]) >= MAX_ATTEMPTS:
            _blocked_until[ip] = now + BLOCK_SECONDS
            _failures[ip] = []


def register_success(ip: str) -> None:
    """Сбрасывает счётчик после успешного входа."""
    with _lock:
        _failures.pop(ip, None)
        _blocked_until.pop(ip, None)


def client_ip(request) -> str:
    """IP клиента с учётом reverse-proxy (nginx)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
