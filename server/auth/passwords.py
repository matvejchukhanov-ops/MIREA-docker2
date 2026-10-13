"""Хеширование паролей и правила для логина и пароля.

Алгоритм — scrypt из стандартной библиотеки. Быстрые хеши вроде SHA-256 для
паролей не годятся: видеокарта перебирает миллиарды SHA-256 в секунду, и
утёкшая база вскрывается по словарю за часы. scrypt намеренно медленный
и требует много памяти, что делает перебор на видеокартах дорогим.

Соль — 16 случайных байт на каждый пароль. Без неё одинаковые пароли дают
одинаковые хеши, и одна заранее посчитанная таблица вскрывает всю базу.

Формат хранения: scrypt$n$r$p$соль$хеш (соль и хеш — base64). Параметры
пишутся рядом с хешем, чтобы их можно было усилить позже, не ломая
проверку старых паролей.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

# Пределы параметров при разборе хеша. Хеш читается из базы, но доверять
# ей вслепую не стоит: подменённый n = 2**30 занял бы всю память сервера.
_MAX_N = 2**20

PASSWORD_MIN_LENGTH = 8
# Верхний предел нужен не для «безопасности пароля», а для сервера:
# хеширование мегабайтного пароля — бесплатная для атакующего нагрузка.
PASSWORD_MAX_LENGTH = 128

USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_.\-]{3,32}")


class PasswordPolicyError(ValueError):
    """Пароль не соответствует требованиям."""


class UsernamePolicyError(ValueError):
    """Логин не соответствует требованиям."""


def validate_username(username: str) -> None:
    # fullmatch, а не match: иначе «admin\n» прошёл бы проверку.
    if not USERNAME_PATTERN.fullmatch(username):
        raise UsernamePolicyError(
            "Логин — от 3 до 32 символов: латинские буквы, цифры, точка, дефис, подчёркивание"
        )


def validate_password(password: str, username: str) -> None:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise PasswordPolicyError(f"Пароль короче {PASSWORD_MIN_LENGTH} символов")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise PasswordPolicyError(f"Пароль длиннее {PASSWORD_MAX_LENGTH} символов")
    if password.casefold() == username.casefold():
        raise PasswordPolicyError("Пароль не должен совпадать с логином")
    if password.isdigit() or password.isalpha():
        raise PasswordPolicyError("Пароль должен содержать и буквы, и цифры или другие символы")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return "$".join(
        ("scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P), _b64(salt), _b64(key))
    )


def verify_password(password: str, stored: str) -> bool:
    """Проверить пароль. Испорченный хеш — это «не подходит», а не исключение."""
    try:
        scheme, n_raw, r_raw, p_raw, salt_raw, key_raw = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        if not (2 <= n <= _MAX_N and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 16):
            return False
        salt = base64.b64decode(salt_raw, validate=True)
        expected = base64.b64decode(key_raw, validate=True)
    except ValueError:
        return False
    if not 16 <= len(expected) <= 64 or not salt:
        return False

    actual = _derive(password, salt, n, r, p, dklen=len(expected))
    # compare_digest сравнивает за время, не зависящее от того, на каком
    # байте нашлось расхождение. Обычное == выходит на первом несовпадении,
    # и по времени ответа можно было бы подбирать хеш побайтно.
    return hmac.compare_digest(actual, expected)


def _derive(password: str, salt: bytes, n: int, r: int, p: int, dklen: int = KEY_BYTES) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=dklen,
        # Память под scrypt ~ 128 * n * r байт; для n = 2**20 нужен запас.
        maxmem=256 * 1024 * 1024,
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# Хеш для проверки, когда пользователя не существует. Без него ответ на
# неверный логин приходил бы мгновенно, а на неверный пароль — с задержкой
# на scrypt, и по времени ответа можно было бы выяснить, какие логины есть.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
