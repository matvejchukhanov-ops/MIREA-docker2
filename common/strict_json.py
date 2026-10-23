"""Строгий разбор JSON, общий для сервера и клиента.

Стандартный json.loads снисходителен к двум вещам, которых нет в стандарте
или которые в нём не определены:

* дубликаты ключей — {"role": "user", "role": "admin"}. json.loads молча
  оставляет последнее значение, другие разборщики берут первое. Проверка в
  одном месте видит одно, применение в другом — другое;
* NaN и Infinity — не входят в стандарт JSON, ломают сравнения и сторонние
  разборщики.

Правило должно действовать на каждом входе. Если строго разбирать только
файлы на диске, а запросы из сети — обычным json.loads, дубликат пройдёт
через запрос и окажется в файле уже «чистым»: {"role": "admin"}.
"""
from __future__ import annotations

import json
from typing import Any


class JSONRuleError(ValueError):
    """JSON синтаксически верен, но нарушает правила строгого разбора."""


def loads(text: str) -> Any:
    """Разобрать JSON, отвергая дубликаты ключей, NaN и Infinity.

    Синтаксические ошибки — json.JSONDecodeError, нарушения правил —
    JSONRuleError. Слишком глубокая вложенность — RecursionError, как и у
    json.loads: перехватывает вызывающий, он знает, какую ошибку отдать.
    """
    return json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JSONRuleError(f"Ключ {key!r} повторяется в одном объекте")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise JSONRuleError(f"Значение {name} не входит в стандарт JSON")
