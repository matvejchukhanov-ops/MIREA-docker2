"""Тесты фрейминга.

Проверяется главное свойство: результат не зависит от того, как поток байт
поделён между чтениями. Именно этого TCP не гарантирует, и именно поэтому
фрейминг вообще нужен.

Запуск:
    python -m unittest discover
"""
from __future__ import annotations

import unittest

from common.framing import FrameReader, FrameTooLongError, encode_frame


class EncodeFrameTests(unittest.TestCase):
    def test_добавляет_разделитель(self) -> None:
        self.assertEqual(encode_frame(b"hello"), b"hello\n")

    def test_пустая_нагрузка_допустима(self) -> None:
        self.assertEqual(encode_frame(b""), b"\n")

    def test_разделитель_внутри_нагрузки_запрещён(self) -> None:
        # Такой кадр было бы невозможно разобрать обратно.
        with self.assertRaises(ValueError):
            encode_frame("две\nстроки".encode("utf-8"))


class FrameReaderTests(unittest.TestCase):
    def test_один_кадр_целиком(self) -> None:
        reader = FrameReader()
        self.assertEqual(reader.feed(b"hello\n"), [b"hello"])
        self.assertEqual(reader.pending, 0)

    def test_несколько_кадров_за_одно_чтение(self) -> None:
        # Ровно тот случай, который ломал эхо-сервер первого этапа:
        # два send() клиента пришли одним recv().
        reader = FrameReader()
        self.assertEqual(reader.feed(b"AAA\nBBB\n"), [b"AAA", b"BBB"])

    def test_кадр_разрезанный_между_чтениями(self) -> None:
        reader = FrameReader()
        self.assertEqual(reader.feed(b"hel"), [])
        self.assertEqual(reader.pending, 3)
        self.assertEqual(reader.feed(b"lo"), [])
        self.assertEqual(reader.feed(b"\n"), [b"hello"])

    def test_разрез_посреди_стыка_кадров(self) -> None:
        # Самый неприятный вариант: чтение оборвалось внутри второго сообщения.
        reader = FrameReader()
        self.assertEqual(reader.feed(b"first\nsec"), [b"first"])
        self.assertEqual(reader.feed(b"ond\n"), [b"second"])

    def test_побайтовая_подача(self) -> None:
        # Предельный случай дробления: каждый байт отдельным чтением.
        reader = FrameReader()
        collected: list[bytes] = []
        for byte in b"2+2\n3*3\n":
            collected.extend(reader.feed(bytes([byte])))
        self.assertEqual(collected, [b"2+2", b"3*3"])

    def test_многобайтный_символ_разрезан_пополам(self) -> None:
        # Кириллическая «п» занимает два байта в UTF-8. Фрейминг работает
        # с байтами и не должен пытаться их декодировать — иначе разрез
        # внутри символа развалил бы разбор.
        reader = FrameReader()
        self.assertEqual(reader.feed(b"\xd0"), [])
        frames = reader.feed(b"\xbf\n")
        self.assertEqual(frames, [b"\xd0\xbf"])
        self.assertEqual(frames[0].decode("utf-8"), "п")

    def test_пустой_кадр(self) -> None:
        reader = FrameReader()
        self.assertEqual(reader.feed(b"\n"), [b""])

    def test_возврат_каретки_отбрасывается(self) -> None:
        # telnet-клиент Windows завершает строки двумя символами.
        reader = FrameReader()
        self.assertEqual(reader.feed(b"ping\r\n"), [b"ping"])

    def test_возврат_каретки_внутри_кадра_сохраняется(self) -> None:
        # Отбрасывается только тот, что стоит прямо перед разделителем.
        reader = FrameReader()
        self.assertEqual(reader.feed(b"a\rb\n"), [b"a\rb"])

    def test_пустое_чтение_ничего_не_меняет(self) -> None:
        reader = FrameReader()
        reader.feed(b"abc")
        self.assertEqual(reader.feed(b""), [])
        self.assertEqual(reader.pending, 3)

    def test_reset_очищает_буфер(self) -> None:
        reader = FrameReader()
        reader.feed("хвост без разделителя".encode("utf-8"))
        reader.reset()
        self.assertEqual(reader.pending, 0)


class FrameSizeLimitTests(unittest.TestCase):
    """Лимит защищает от потока без разделителей, съедающего память."""

    def test_кадр_ровно_по_границе_проходит(self) -> None:
        # Лимит считается вместе с разделителем: 7 байт + перевод строки = 8.
        reader = FrameReader(max_frame_size=8)
        self.assertEqual(reader.feed(b"1234567\n"), [b"1234567"])

    def test_кадр_на_байт_длиннее_отвергается(self) -> None:
        reader = FrameReader(max_frame_size=8)
        with self.assertRaises(FrameTooLongError):
            reader.feed(b"12345678\n")

    def test_поток_без_разделителя_отвергается(self) -> None:
        # Разделителя нет вовсе, а место кончилось — валидного кадра тут уже
        # не получится, ждать дальше бессмысленно.
        reader = FrameReader(max_frame_size=8)
        with self.assertRaises(FrameTooLongError):
            reader.feed(b"12345678")

    def test_переполнение_накапливается_между_чтениями(self) -> None:
        reader = FrameReader(max_frame_size=8)
        self.assertEqual(reader.feed(b"1234"), [])
        with self.assertRaises(FrameTooLongError):
            reader.feed(b"5678")

    def test_ошибка_называет_лимит(self) -> None:
        reader = FrameReader(max_frame_size=16)
        with self.assertRaises(FrameTooLongError) as ctx:
            reader.feed(b"x" * 20)
        self.assertEqual(ctx.exception.limit, 16)

    def test_слишком_маленький_лимит_запрещён(self) -> None:
        with self.assertRaises(ValueError):
            FrameReader(max_frame_size=1)


if __name__ == "__main__":
    unittest.main()
