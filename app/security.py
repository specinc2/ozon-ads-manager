"""Безопасность: пароли, шифрование API-ключей, сессии.

- Пароли хешируются PBKDF2-SHA256 (стандартная библиотека, без внешних
  зависимостей и проблем с версиями bcrypt).
- Ключи Ozon шифруются Fernet (cryptography); ключ шифрования берётся из
  ENCRYPTION_KEY или генерируется один раз и сохраняется рядом с БД.
- Сессии — подписанные HttpOnly cookie (itsdangerous).
"""
import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings, BASE_DIR

PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """Хеширует пароль в формате pbkdf2$iterations$salt$hash."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(plain: str, hashed: str) -> bool:
    try:
        scheme, iterations, salt, expected = hashed.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", plain.encode(), salt.encode(), int(iterations)
        ).hex()
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Шифрование API-ключей
# ---------------------------------------------------------------------------

_fernet: Fernet | None = None
_enc_key_path = BASE_DIR / ".encryption_key"


def _load_or_create_key() -> bytes:
    """Читает ключ из ENCRYPTION_KEY или из файла .encryption_key (создаёт при первом запуске)."""
    env_key = settings.encryption_key
    if env_key:
        return env_key.encode() if not env_key.endswith("=") else env_key.encode()
    if _enc_key_path.exists():
        return _enc_key_path.read_bytes().strip()
    key = Fernet.generate_key()
    _enc_key_path.write_bytes(key)
    return key


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        raw = _load_or_create_key()
        # Поддержка не-base64 ключей (дополняем при необходимости)
        try:
            _fernet = Fernet(raw)
        except (ValueError, base64.binascii.Error):
            b64 = base64.urlsafe_b64encode(raw).rstrip(b"=")
            _fernet = Fernet(b64)
    return _fernet


def encrypt_value(value: str) -> str:
    """Шифрует строку (Client-Id, Client-Secret, Api-Key) для хранения в БД."""
    if not value:
        return ""
    return get_fernet().encrypt(value.encode()).decode()


def decrypt_value(token: str) -> str:
    """Расшифровывает значение из БД. При повреждении возвращает пустую строку."""
    if not token:
        return ""
    try:
        return get_fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Сессии
# ---------------------------------------------------------------------------

_session_serializer: URLSafeTimedSerializer | None = None


def get_serializer() -> URLSafeTimedSerializer:
    global _session_serializer
    if _session_serializer is None:
        _session_serializer = URLSafeTimedSerializer(settings.secret_key, salt="ozon-ads-session")
    return _session_serializer


SESSION_TTL = 60 * 60 * 24 * 7  # 7 дней


def create_session_token(user_id: int) -> str:
    return get_serializer().dumps({"user_id": user_id})


def read_session_token(token: str) -> int | None:
    """Возвращает user_id или None, если токен недействителен/просрочен."""
    try:
        data = get_serializer().loads(token, max_age=SESSION_TTL)
        return data.get("user_id")
    except BadSignature:
        return None


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)
