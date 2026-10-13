"""Тесты JSON и XML: корректные данные и атаки на разборщик."""
from __future__ import annotations

import unittest

from server.fs.errors import BadFormatError
from server.fs.formats import MAX_DEPTH, build_xml, dump_json, parse_json, parse_xml


class JsonTests(unittest.TestCase):
    def test_туда_и_обратно(self) -> None:
        value = {"name": "Тест", "items": [1, 2.5, True, None], "nested": {"a": []}}
        self.assertEqual(parse_json(dump_json(value)), value)

    def test_только_данные_без_объектов(self) -> None:
        # Сколько бы ни старался файл, на выходе только словари, списки и
        # примитивы — в отличие от pickle, который создаёт объекты любых классов.
        value = parse_json('{"__class__": "os.system", "py/object": "subprocess.Popen"}')
        self.assertIsInstance(value, dict)
        self.assertEqual(set(map(type, value.values())), {str})

    def test_дубликат_ключа(self) -> None:
        with self.assertRaises(BadFormatError):
            parse_json('{"role": "user", "role": "admin"}')

    def test_nan_и_бесконечность(self) -> None:
        for text in ('{"x": NaN}', '[Infinity]', '[-Infinity]'):
            with self.subTest(text=text), self.assertRaises(BadFormatError):
                parse_json(text)
        with self.assertRaises(BadFormatError):
            dump_json({"x": float("nan")})

    def test_глубокая_вложенность(self) -> None:
        with self.assertRaises(BadFormatError):
            parse_json("[" * (MAX_DEPTH + 2) + "]" * (MAX_DEPTH + 2))
        with self.assertRaises(BadFormatError):
            parse_json("[" * 100_000 + "]" * 100_000)

    def test_вложенность_в_пределах(self) -> None:
        text = "[" * MAX_DEPTH + "]" * MAX_DEPTH
        self.assertIsInstance(parse_json(text), list)

    def test_некорректный_json(self) -> None:
        with self.assertRaises(BadFormatError) as ctx:
            parse_json('{"a": 1,}')
        self.assertIn("строка 1", ctx.exception.message)


class XmlTests(unittest.TestCase):
    def test_разбор(self) -> None:
        tree = parse_xml(
            '<?xml version="1.0"?><phones><phone name="iPhone 6"><price>40000</price>'
            "</phone></phones>"
        )
        self.assertEqual(tree["tag"], "phones")
        phone = tree["children"][0]
        self.assertEqual(phone["attrs"], {"name": "iPhone 6"})
        self.assertEqual(phone["children"][0]["text"], "40000")

    def test_xxe_файл_сервера(self) -> None:
        text = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE data [<!ENTITY secret SYSTEM "file:///etc/passwd">]>'
            "<data>&secret;</data>"
        )
        with self.assertRaises(BadFormatError):
            parse_xml(text)

    def test_xxe_запрос_в_сеть(self) -> None:
        text = '<!DOCTYPE d [<!ENTITY x SYSTEM "http://169.254.169.254/latest/">]><d>&x;</d>'
        with self.assertRaises(BadFormatError):
            parse_xml(text)

    def test_миллиард_смеющихся(self) -> None:
        entities = '<!ENTITY lol "lol">' + "".join(
            f'<!ENTITY lol{n} "{("&lol" + (str(n - 1) if n > 1 else "") + ";") * 10}">'
            for n in range(1, 10)
        )
        text = f"<!DOCTYPE lolz [{entities}]><lolz>&lol9;</lolz>"
        with self.assertRaises(BadFormatError):
            parse_xml(text)

    def test_внешняя_dtd(self) -> None:
        with self.assertRaises(BadFormatError):
            parse_xml('<!DOCTYPE d SYSTEM "http://example.com/evil.dtd"><d/>')

    def test_глубина_и_количество(self) -> None:
        deep = "<a>" * (MAX_DEPTH + 1) + "</a>" * (MAX_DEPTH + 1)
        with self.assertRaises(BadFormatError):
            parse_xml(deep)
        wide = "<r>" + "<i/>" * 20_000 + "</r>"
        with self.assertRaises(BadFormatError):
            parse_xml(wide)

    def test_некорректный_xml(self) -> None:
        for text in ("<a><b></a>", "", "просто текст"):
            with self.subTest(text=text), self.assertRaises(BadFormatError):
                parse_xml(text)

    def test_сборка_экранирует_данные(self) -> None:
        # Текст с разметкой должен остаться текстом, а не стать элементами.
        tree = {"tag": "note", "attrs": {"author": 'a" evil="1'}, "text": "</note><admin/>",
                "children": []}
        parsed = parse_xml(build_xml(tree))
        self.assertEqual(parsed["text"], "</note><admin/>")
        self.assertEqual(parsed["attrs"], {"author": 'a" evil="1'})
        self.assertEqual(parsed["children"], [])

    def test_сборка_отвергает_мусор(self) -> None:
        bad_trees = [
            {"tag": "1bad"},
            {"tag": "a b"},
            {"tag": "ok", "attrs": {"bad name": "x"}},
            {"tag": "ok", "text": 5},
            {"tag": "ok", "children": "nope"},
            {"tag": "ok", "extra": 1},
            {"tag": "ok", "text": "\x00"},
            "not a dict",
        ]
        for tree in bad_trees:
            with self.subTest(tree=tree), self.assertRaises(BadFormatError):
                build_xml(tree)

    def test_сборка_и_разбор_согласованы(self) -> None:
        tree = {
            "tag": "phones", "attrs": {}, "text": "",
            "children": [{"tag": "phone", "attrs": {"name": "Nokia"}, "text": "19500", "children": []}],
        }
        self.assertEqual(parse_xml(build_xml(tree)), tree)


if __name__ == "__main__":
    unittest.main()
