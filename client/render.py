"""Вывод ответов сервера человеку.

Клиент знает, какую команду отправил, поэтому оформляет ответ по ней.
Незнакомая форма ответа выводится как JSON: клиент не должен падать
от того, что сервер стал присылать больше полей.
"""
from __future__ import annotations

import json
from typing import Any

OPERATION_NAMES = {"create": "создание", "modify": "изменение", "delete": "удаление"}


def render(request: dict[str, Any], data: Any) -> str:
    if not isinstance(data, dict):
        return f"Неожиданный ответ сервера: {data!r}"

    status = data.get("status")
    if status == "error":
        return f"Ошибка [{data.get('code')}]: {data.get('message')}"
    if status != "ok":
        # Поле status обязано быть всегда — его отсутствие означает,
        # что мы разговариваем не с тем сервером.
        return f"Ответ без поля status: {data!r}"

    result = data.get("result")
    renderer = _RENDERERS.get(str(request.get("cmd")))
    if renderer is None:
        return _as_json(result)
    try:
        return renderer(result)
    except (KeyError, TypeError, ValueError):
        return _as_json(result)


def human_size(size: int | None) -> str:
    if size is None:
        return "—"
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{int(value)} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def _as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _user(result: dict[str, Any]) -> str:
    return f"Вы вошли как {result['username']}"


def _ls(result: dict[str, Any]) -> str:
    entries = result["entries"]
    header = f"/{result['path']}"
    if not entries:
        return f"{header}\n  (пусто)"
    lines = [header]
    for entry in entries:
        marker = {"dir": "[кат]", "link": "[ссл]"}.get(entry["type"], "     ")
        size = "" if entry["type"] == "dir" else human_size(entry["size"])
        lines.append(f"  {marker} {size:>10}  {entry['modified']}  {entry['name']}")
    if result.get("truncated"):
        lines.append("  … показаны не все записи")
    return "\n".join(lines)


def _info(result: dict[str, Any]) -> str:
    kinds = {"file": "файл", "dir": "каталог", "link": "ссылка"}
    lines = [
        f"Путь:       /{result['path']}",
        f"Тип:        {kinds.get(result['type'], result['type'])}",
    ]
    if result.get("size") is not None:
        lines.append(f"Размер:     {human_size(result['size'])} ({result['size']} байт)")
    lines.append(f"Создан:     {result['created']} UTC")
    lines.append(f"Изменён:    {result['modified']} UTC")
    record = result.get("record")
    if record:
        lines.append(f"В базе:     запись №{record['id']}, создана {record['created_at']} UTC")
    elif result["type"] == "file":
        lines.append("В базе:     нет записи (файл появился в обход сервера)")
    return "\n".join(lines)


def _written(result: dict[str, Any]) -> str:
    verb = "Создан" if result["created"] else "Перезаписан"
    text = f"{verb} /{result['path']} ({human_size(result['size'])})"
    if "files" in result:
        text += f", файлов в архиве: {result['files']}"
    return text


def _xml_tree(node: dict[str, Any], depth: int = 0) -> list[str]:
    attrs = "".join(f' {name}="{value}"' for name, value in node["attrs"].items())
    line = f"{'  ' * depth}<{node['tag']}{attrs}>"
    if node["text"]:
        line += f" {node['text']}"
    lines = [line]
    for child in node["children"]:
        lines.extend(_xml_tree(child, depth + 1))
    return lines


def _drives(result: list[dict[str, Any]]) -> str:
    lines = []
    for drive in result:
        label = f" «{drive['label']}»" if drive.get("label") else ""
        lines.append(f"{drive['name']}{label}")
        lines.append(f"  тип: {drive['type']}, файловая система: {drive['filesystem'] or '—'}")
        if drive["ready"]:
            lines.append(
                f"  объём: {human_size(drive['total_size'])},"
                f" свободно: {human_size(drive['total_free'])},"
                f" доступно вам: {human_size(drive['available_free'])}"
            )
        else:
            lines.append("  не готов (нет носителя или нет доступа)")
    return "\n".join(lines) or "(дисков не найдено)"


def _zip_list(result: dict[str, Any]) -> str:
    lines = [f"{result['archive']}:"]
    for entry in result["entries"]:
        if entry["is_dir"]:
            lines.append(f"  {'':>10}  {'':>10}  {entry['name']}")
        else:
            lines.append(
                f"  {human_size(entry['size']):>10}  {human_size(entry['compressed']):>10}"
                f"  {entry['name']}"
            )
    return "\n".join(lines)


def _unzip(result: dict[str, Any]) -> str:
    lines = [f"Распаковано в /{result['destination']}: файлов {len(result['files'])}"]
    lines.extend(f"  /{item['path']} ({human_size(item['size'])})" for item in result["files"])
    return "\n".join(lines)


def _log(result: list[dict[str, Any]]) -> str:
    if not result:
        return "(журнал пуст)"
    lines = []
    for entry in reversed(result):
        mark = "ok " if entry["success"] else "ОШ "
        target = f" {entry['target']}" if entry.get("target") else ""
        details = f"  [{entry['details']}]" if entry.get("details") else ""
        lines.append(f"  {entry['timestamp'][:19]}  {mark} {entry['action']}{target}{details}")
    return "\n".join(lines)


def _ops(result: list[dict[str, Any]]) -> str:
    if not result:
        return "(операций нет)"
    return "\n".join(
        f"  {entry['timestamp'][:19]}  {OPERATION_NAMES.get(entry['operation_type'], entry['operation_type']):<10}"
        f"  /{entry['location']}"
        for entry in reversed(result)
    )


_RENDERERS = {
    "register": _user,
    "login": _user,
    "whoami": lambda result: f"Вы: {result['username']}",
    "logout": lambda result: "Вы вышли",
    "ping": str,
    "quit": lambda result: "До свидания",
    "drives": _drives,
    "ls": _ls,
    "info": _info,
    "read": lambda result: result["content"],
    "write": _written,
    "json_write": _written,
    "xml_write": _written,
    "zip": _written,
    "delete": lambda result: f"Удалён /{result['path']}",
    "mkdir": lambda result: f"Создан каталог /{result['path']}",
    "rmdir": lambda result: f"Удалён каталог /{result['path']}",
    "json_read": lambda result: _as_json(result["data"]),
    "xml_read": lambda result: "\n".join(_xml_tree(result["tree"])),
    "zip_list": _zip_list,
    "unzip": _unzip,
    "log": _log,
    "ops": _ops,
}
