"""Тест списка дисков: структура ответа на той системе, где запущен тест."""
from __future__ import annotations

import unittest

from server.fs.drives import _unescape, list_drives

FIELDS = {
    "name", "type", "filesystem", "label", "ready", "total_size", "total_free", "available_free",
}


class DrivesTests(unittest.TestCase):
    def test_хотя_бы_один_диск(self) -> None:
        drives = list_drives()
        self.assertGreater(len(drives), 0)
        for drive in drives:
            self.assertEqual(set(drive), FIELDS)

    def test_размеры_согласованы(self) -> None:
        for drive in list_drives():
            if not drive["ready"] or drive["total_size"] is None:
                continue
            with self.subTest(drive=drive["name"]):
                self.assertGreater(drive["total_size"], 0)
                self.assertLessEqual(drive["total_free"], drive["total_size"])
                # Доступное пользователю не больше свободного вообще.
                self.assertLessEqual(drive["available_free"], drive["total_free"])

    def test_экранирование_proc_mounts(self) -> None:
        self.assertEqual(_unescape("/media/My\\040Disk"), "/media/My Disk")


if __name__ == "__main__":
    unittest.main()
