"""Тесты ZIP: упаковка, распаковка и атаки через архив."""
from __future__ import annotations

import io
import warnings
import zipfile

from server.fs import errors
from server.fs.archive import ZipLimits, create_archive, extract_archive, list_archive
from server.fs.manager import FileManager
from server.fs.sandbox import TEMP_PREFIX

from .support import TempDirTestCase

LIMITS = ZipLimits(max_entries=50, max_total_size=1024 * 1024, max_ratio=100)


class ArchiveTestCase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.files = FileManager(self.tmp / "storage", max_file_size=64 * 1024).for_user(1)
        self.root = self.files.sandbox.root

    def make_zip(self, name: str, entries: list[tuple[zipfile.ZipInfo | str, bytes]]) -> None:
        buffer = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # zipfile предупреждает о дубликатах
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for info, data in entries:
                    bundle.writestr(info, data)
        (self.root / name).write_bytes(buffer.getvalue())

    def assertNothingExtracted(self) -> None:
        leftovers = sorted(p.name for p in self.root.iterdir() if p.name != "evil.zip")
        self.assertEqual(leftovers, [])
        self.assertFalse((self.tmp / "evil.txt").exists())
        self.assertFalse((self.tmp / "storage" / "evil.txt").exists())


class RoundTripTests(ArchiveTestCase):
    def test_упаковка_и_распаковка(self) -> None:
        self.files.make_dir("docs")
        self.files.write_text("docs/a.txt", "альфа", overwrite=False)
        self.files.write_text("b.json", "{}", overwrite=False)

        written, count = create_archive(self.files, "pack.zip", ["docs", "b.json"], LIMITS, overwrite=False)
        self.assertEqual((written.location, count, written.created), ("pack.zip", 2, True))

        names = [entry["name"] for entry in list_archive(self.files, "pack.zip", LIMITS)]
        self.assertEqual(names, ["b.json", "docs/a.txt"])

        extracted = extract_archive(self.files, "pack.zip", "out", LIMITS)
        self.assertEqual(sorted(item.location for item in extracted), ["out/b.json", "out/docs/a.txt"])
        self.assertEqual(self.files.read_text("out/docs/a.txt")[1], "альфа")

    def test_архив_не_попадает_сам_в_себя(self) -> None:
        self.files.write_text("a.txt", "1", overwrite=False)
        create_archive(self.files, "all.zip", [""], LIMITS, overwrite=False)
        _, count = create_archive(self.files, "all.zip", [""], LIMITS, overwrite=True)
        self.assertEqual(count, 1)

    def test_существующий_архив_без_overwrite(self) -> None:
        self.files.write_text("a.txt", "1", overwrite=False)
        create_archive(self.files, "a.zip", ["a.txt"], LIMITS, overwrite=False)
        with self.assertRaises(errors.AlreadyExistsError):
            create_archive(self.files, "a.zip", ["a.txt"], LIMITS, overwrite=False)

    def test_имя_архива_с_расширением_zip(self) -> None:
        self.files.write_text("a.txt", "1", overwrite=False)
        with self.assertRaises(errors.BadPathError):
            create_archive(self.files, "a.exe", ["a.txt"], LIMITS, overwrite=False)

    def test_распаковка_только_в_новый_каталог(self) -> None:
        self.files.write_text("a.txt", "1", overwrite=False)
        create_archive(self.files, "a.zip", ["a.txt"], LIMITS, overwrite=False)
        self.files.make_dir("out")
        with self.assertRaises(errors.AlreadyExistsError):
            extract_archive(self.files, "a.zip", "out", LIMITS)
        with self.assertRaises(errors.BadPathError):
            extract_archive(self.files, "a.zip", "", LIMITS)

    def test_не_архив(self) -> None:
        (self.root / "fake.zip").write_text("это не архив", encoding="utf-8")
        with self.assertRaises(errors.BadArchiveError):
            extract_archive(self.files, "fake.zip", "out", LIMITS)

    def test_предел_при_упаковке(self) -> None:
        for index in range(6):
            self.files.write_text(f"f{index}.txt", "1", overwrite=False)
        small = ZipLimits(max_entries=5, max_total_size=1024, max_ratio=100)
        with self.assertRaises(errors.LimitExceededError):
            create_archive(self.files, "a.zip", [""], small, overwrite=False)


class AttackTests(ArchiveTestCase):
    def assertRejected(self, entries: list[tuple[zipfile.ZipInfo | str, bytes]],
                       limits: ZipLimits = LIMITS) -> None:
        self.make_zip("evil.zip", entries)
        with self.assertRaises((errors.UnsafeArchiveError, errors.BadArchiveError)):
            extract_archive(self.files, "evil.zip", "out", limits)
        self.assertNothingExtracted()

    def test_zip_slip_родительский_каталог(self) -> None:
        self.assertRejected([("ok.txt", b"1"), ("../../evil.txt", b"pwned")])

    def test_zip_slip_абсолютный_путь(self) -> None:
        self.assertRejected([("/tmp/evil.txt", b"pwned")])

    def test_zip_slip_буква_диска(self) -> None:
        self.assertRejected([("C:/Windows/evil.txt", b"pwned")])

    def test_zip_slip_обратные_слеши(self) -> None:
        self.assertRejected([("..\\..\\evil.txt", b"pwned")])

    def test_zip_бомба_по_степени_сжатия(self) -> None:
        # Мегабайт нулей сжимается примерно в тысячу раз.
        self.assertRejected([("zeros.bin", b"\x00" * 1024 * 1024)])

    def test_zip_бомба_по_суммарному_размеру(self) -> None:
        limits = ZipLimits(max_entries=50, max_total_size=1000, max_ratio=10_000)
        self.assertRejected([(f"f{n}.txt", bytes(range(256)) * 2) for n in range(3)], limits)

    def test_слишком_много_записей(self) -> None:
        limits = ZipLimits(max_entries=5, max_total_size=1024 * 1024, max_ratio=100)
        self.assertRejected([(f"f{n}.txt", b"x") for n in range(6)], limits)

    def test_заголовок_врёт_о_размере(self) -> None:
        # Объявленный размер — то, что написал создатель архива. Подменим его
        # в заголовках и убедимся, что распаковка не поверила.
        data = bytes(range(256)) * 40
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("liar.bin", data)
        raw = bytearray(buffer.getvalue())
        real = len(data).to_bytes(4, "little")
        fake = (10).to_bytes(4, "little")
        patched = bytes(raw).replace(real, fake)
        self.assertNotEqual(bytes(raw), patched)
        (self.root / "evil.zip").write_bytes(patched)
        with self.assertRaises((errors.UnsafeArchiveError, errors.BadArchiveError)):
            extract_archive(self.files, "evil.zip", "out", LIMITS)
        self.assertNothingExtracted()

    def test_символическая_ссылка(self) -> None:
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (0o120777 << 16)
        self.assertRejected([(link, b"/etc"), ("link/passwd", b"x")])

    def test_зашифрованная_запись(self) -> None:
        # zipfile не пишет зашифрованных архивов, поэтому флаг шифрования
        # ставится вручную в локальном заголовке и в центральном каталоге.
        self.make_zip("evil.zip", [("secret.txt", b"x")])
        raw = bytearray((self.root / "evil.zip").read_bytes())
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            position = raw.find(signature) + offset
            raw[position] |= 0x1
        (self.root / "evil.zip").write_bytes(bytes(raw))
        with self.assertRaises(errors.UnsafeArchiveError):
            extract_archive(self.files, "evil.zip", "out", LIMITS)
        self.assertNothingExtracted()

    def test_дубликаты_и_регистр(self) -> None:
        self.assertRejected([("a.txt", b"1"), ("A.TXT", b"2")])

    def test_файл_и_каталог_с_одним_именем(self) -> None:
        self.assertRejected([("a", b"1"), ("a/b.txt", b"2")])

    def test_зарезервированные_имена_windows(self) -> None:
        self.assertRejected([("con.txt", b"1")])

    def test_после_отказа_нет_временных_каталогов(self) -> None:
        self.make_zip("evil.zip", [("ok.txt", b"1"), ("../evil.txt", b"2")])
        with self.assertRaises(errors.UnsafeArchiveError):
            extract_archive(self.files, "evil.zip", "out", LIMITS)
        self.assertFalse(any(p.name.startswith(TEMP_PREFIX) for p in self.root.iterdir()))
