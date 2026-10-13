"""Сериализация и десериализация JSON и XML по принципу минимального доверия.

Файл, присланный клиентом или лежащий на диске, — недоверенные данные.
Разбор не должен ни исполнять код, ни ходить в сеть, ни есть память без
предела.

JSON
----
Небезопасная десериализация — это форматы, которые при чтении создают
объекты произвольных классов (pickle в Python, BinaryFormatter в .NET):
подобранный файл вызывает при разборе нужный атакующему метод. Модуль json
порождает только словари, списки, строки, числа и логические значения —
класс объекта из файла не выбирается. Сверх этого здесь отвергаются:

* дубликаты ключей — {"role": "user", "role": "admin"}: разные разборщики
  берут разное значение, и проверка в одном месте видит одно, а применение
  в другом — другое;
* NaN и Infinity — не входят в стандарт JSON, ломают сравнения и сторонние
  разборщики;
* глубокая вложенность — [[[[...]]]] на тысячи уровней исчерпывает стек.

XML
---
Стандартный разборщик понимает DTD, а через неё:

* XXE (внешние сущности) — <!ENTITY x SYSTEM "file:///etc/passwd"> подставляет
  в документ содержимое файла сервера или результат запроса в сеть;
* «миллиард смеющихся» — вложенные сущности, где каждая разворачивается в
  десять предыдущих: килобайт текста становится гигабайтами в памяти.

Документу с данными DTD не нужна вовсе, поэтому здесь любой <!DOCTYPE> —
повод отказать. Разбор идёт напрямую через expat с обработчиком, который
прерывает работу на первом же объявлении типа документа или сущности,
до того как что-либо будет развёрнуто.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any
from xml.parsers import expat

from .errors import BadFormatError

MAX_DEPTH = 32
MAX_XML_ELEMENTS = 10_000

# --- JSON ---


def parse_json(text: str) -> Any:
    """Текст -> значение JSON (только примитивы, списки и словари)."""
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise BadFormatError(
            f"Некорректный JSON: {exc.msg} (строка {exc.lineno}, столбец {exc.colno})"
        ) from exc
    except RecursionError as exc:
        raise BadFormatError(f"Вложенность JSON больше {MAX_DEPTH} уровней") from exc
    check_json_value(value)
    return value


def dump_json(value: Any) -> str:
    """Значение -> текст JSON для записи в файл."""
    check_json_value(value)
    # allow_nan=False — то же правило при записи, что и при чтении:
    # файл, который мы сами записали, должен читаться нами же.
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def check_json_value(value: Any, depth: int = 0) -> None:
    """Проверить глубину и типы значения, пришедшего не из json.loads."""
    if depth > MAX_DEPTH:
        raise BadFormatError(f"Вложенность JSON больше {MAX_DEPTH} уровней")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise BadFormatError("Ключи объекта JSON должны быть строками")
            check_json_value(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            check_json_value(item, depth + 1)
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise BadFormatError("NaN и бесконечность в JSON недопустимы")
    elif value is not None and not isinstance(value, (str, int, bool)):
        raise BadFormatError(f"Тип {type(value).__name__} в JSON недопустим")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BadFormatError(f"Ключ {key!r} повторяется в одном объекте")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise BadFormatError(f"Значение {name} не входит в стандарт JSON")


# --- XML ---

# Имя элемента или атрибута XML (упрощённо, без пространств имён с URI).
_XML_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*(:[A-Za-z_][A-Za-z0-9_.\-]*)?")

# Управляющие символы, которых не может быть в XML 1.0. ElementTree записал бы
# их как есть, и получился бы файл, который не прочитает ни один разборщик.
_XML_INVALID_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


class _Forbidden(Exception):
    """Внутренний сигнал прервать разбор."""


def parse_xml(text: str) -> dict[str, Any]:
    """Текст XML -> дерево {tag, attrs, text, children}.

    Дерево из словарей, а не ElementTree: его можно сразу отдать клиенту
    в JSON, и в нём нет ничего, кроме данных.
    """
    parser = expat.ParserCreate()
    # Не загружать внешние DTD и параметрические сущности ни при каких условиях.
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    root: dict[str, Any] | None = None
    stack: list[dict[str, Any]] = []
    texts: list[list[str]] = []
    count = 0

    def forbid(*_: Any) -> None:
        raise _Forbidden("Документ содержит DOCTYPE или объявление сущности — DTD запрещена")

    def start(tag: str, attrs: dict[str, str]) -> None:
        nonlocal root, count
        count += 1
        if count > MAX_XML_ELEMENTS:
            raise _Forbidden(f"В документе больше {MAX_XML_ELEMENTS} элементов")
        if len(stack) >= MAX_DEPTH:
            raise _Forbidden(f"Вложенность XML больше {MAX_DEPTH} уровней")
        node: dict[str, Any] = {"tag": tag, "attrs": dict(attrs), "text": "", "children": []}
        if stack:
            stack[-1]["children"].append(node)
        else:
            root = node
        stack.append(node)
        texts.append([])

    def end(tag: str) -> None:
        node = stack.pop()
        node["text"] = "".join(texts.pop()).strip()

    def characters(data: str) -> None:
        if texts:
            texts[-1].append(data)

    parser.StartDoctypeDeclHandler = forbid
    parser.EntityDeclHandler = forbid
    parser.UnparsedEntityDeclHandler = forbid
    parser.ExternalEntityRefHandler = forbid
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters

    try:
        parser.Parse(text, True)
    except _Forbidden as exc:
        raise BadFormatError(str(exc)) from exc
    except expat.ExpatError as exc:
        raise BadFormatError(
            f"Некорректный XML: {expat.ErrorString(exc.code)} "
            f"(строка {exc.lineno}, столбец {exc.offset + 1})"
        ) from exc

    if root is None:
        raise BadFormatError("В документе нет корневого элемента")
    return root


def build_xml(tree: Any) -> str:
    """Дерево {tag, attrs, text, children} -> текст XML.

    Документ собирается через ElementTree, а не склейкой строк: значения
    атрибутов и текст экранируются библиотекой, и строка вида
    "</a><admin/>" остаётся текстом, а не превращается в разметку.
    """
    element = _to_element(tree, 0)
    ET.indent(element)
    return ET.tostring(element, encoding="unicode", xml_declaration=True) + "\n"


def _to_element(node: Any, depth: int) -> ET.Element:
    if depth >= MAX_DEPTH:
        raise BadFormatError(f"Вложенность XML больше {MAX_DEPTH} уровней")
    if not isinstance(node, dict):
        raise BadFormatError("Узел XML должен быть объектом {tag, attrs, text, children}")
    unknown = set(node) - {"tag", "attrs", "text", "children"}
    if unknown:
        raise BadFormatError(f"Лишние поля узла XML: {', '.join(sorted(unknown))}")

    tag = node.get("tag")
    if not isinstance(tag, str) or not _XML_NAME.fullmatch(tag):
        raise BadFormatError(f"Недопустимое имя элемента: {tag!r}")

    attrs = node.get("attrs", {})
    if not isinstance(attrs, dict):
        raise BadFormatError("attrs должен быть объектом")
    for name, value in attrs.items():
        if not _XML_NAME.fullmatch(name) or not isinstance(value, str):
            raise BadFormatError(f"Недопустимый атрибут {name!r}")
        if _XML_INVALID_CHARS.search(value):
            raise BadFormatError(f"Атрибут {name!r} содержит символы, недопустимые в XML")

    text = node.get("text", "")
    if not isinstance(text, str):
        raise BadFormatError("text должен быть строкой")
    if _XML_INVALID_CHARS.search(text):
        raise BadFormatError(f"Текст элемента {tag!r} содержит символы, недопустимые в XML")

    children = node.get("children", [])
    if not isinstance(children, list):
        raise BadFormatError("children должен быть списком")

    element = ET.Element(tag, attrs)
    element.text = text or None
    for child in children:
        element.append(_to_element(child, depth + 1))
    return element
