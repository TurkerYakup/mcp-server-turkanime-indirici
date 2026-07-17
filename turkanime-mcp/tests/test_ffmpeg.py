"""ffmpeg çözümü (PATH / TURKANIME_FFMPEG / imageio-ffmpeg) için birim testler.

Çalıştırmak için:  python -m unittest discover -s turkanime-mcp/tests -t turkanime-mcp/tests
"""

import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import turkanime_mcp as tm  # noqa: E402


def _fn(tool):
    return getattr(tool, "fn", tool)


class ResolveFfmpegTest(unittest.TestCase):
    def setUp(self):
        # Önbelleği sıfırla: her test kendi senaryosunu kursun.
        tm._FFMPEG_CACHE = None
        self.addCleanup(setattr, tm, "_FFMPEG_CACHE", None)
        self.addCleanup(setattr, shutil, "which", shutil.which)
        self.addCleanup(os.environ.pop, "TURKANIME_FFMPEG", None)
        os.environ.pop("TURKANIME_FFMPEG", None)

        self.tmp = tempfile.mkdtemp(prefix="ta-mcp-ff-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sahte_exe = os.path.join(self.tmp, "ffmpeg.exe")
        with open(self.sahte_exe, "wb") as fh:
            fh.write(b"MZ")

    def _path_yok(self):
        shutil.which = lambda ad, *a, **k: None

    def _path_var(self, yol="C:\\bin\\ffmpeg.exe"):
        shutil.which = lambda ad, *a, **k: yol if ad == "ffmpeg" else None

    def test_path_tercih_edilir(self):
        self._path_var("C:\\bin\\ffmpeg.exe")
        yol, kaynak = tm._resolve_ffmpeg()
        self.assertEqual(kaynak, "path")
        self.assertEqual(yol, "C:\\bin\\ffmpeg.exe")

    def test_env_path_ten_once_gelir(self):
        self._path_var("C:\\bin\\ffmpeg.exe")
        os.environ["TURKANIME_FFMPEG"] = self.sahte_exe
        yol, kaynak = tm._resolve_ffmpeg()
        self.assertEqual(kaynak, "env")
        self.assertEqual(yol, self.sahte_exe)

    def test_env_dosya_yoksa_yok_sayilir(self):
        self._path_var("C:\\bin\\ffmpeg.exe")
        os.environ["TURKANIME_FFMPEG"] = os.path.join(self.tmp, "olmayan.exe")
        _yol, kaynak = tm._resolve_ffmpeg()
        self.assertEqual(kaynak, "path", "var olmayan env yolu PATH'i ezmemeli")

    def test_path_yoksa_imageio_yedegi(self):
        self._path_yok()
        yol, kaynak = tm._resolve_ffmpeg()
        self.assertEqual(kaynak, "imageio")
        self.assertTrue(os.path.isfile(yol))

    def test_onbellek(self):
        self._path_var("C:\\bin\\ffmpeg.exe")
        tm._resolve_ffmpeg()
        # ikinci çağrıda which patlasa bile önbellekten dönmeli
        def patlat(*a, **k):
            raise AssertionError("which yeniden çağrıldı (önbellek çalışmıyor)")
        shutil.which = patlat
        self.assertEqual(tm._resolve_ffmpeg()[1], "path")


class FfmpegYdlOptsTest(unittest.TestCase):
    """yt-dlp'ye yalnızca gerektiğinde ffmpeg_location verilmeli."""

    def setUp(self):
        tm._FFMPEG_CACHE = None
        self.addCleanup(setattr, tm, "_FFMPEG_CACHE", None)

    def test_path_ise_karisilmaz(self):
        """PATH'te varsa yt-dlp kendi bulur (ffprobe'u da) — ezmeyelim."""
        tm._FFMPEG_CACHE = ("C:\\bin\\ffmpeg.exe", "path")
        self.assertEqual(tm._ffmpeg_ydl_opts(), {})

    def test_imageio_ise_konum_verilir(self):
        tm._FFMPEG_CACHE = ("C:\\pkg\\ffmpeg-win.exe", "imageio")
        self.assertEqual(tm._ffmpeg_ydl_opts(),
                         {"ffmpeg_location": "C:\\pkg\\ffmpeg-win.exe"})

    def test_env_ise_konum_verilir(self):
        tm._FFMPEG_CACHE = ("D:\\ff\\ffmpeg.exe", "env")
        self.assertEqual(tm._ffmpeg_ydl_opts(),
                         {"ffmpeg_location": "D:\\ff\\ffmpeg.exe"})

    def test_yoksa_bos(self):
        tm._FFMPEG_CACHE = (None, "yok")
        self.assertEqual(tm._ffmpeg_ydl_opts(), {})


class HealthCheckFfmpegTest(unittest.TestCase):
    def setUp(self):
        tm._FFMPEG_CACHE = None
        self.addCleanup(setattr, tm, "_FFMPEG_CACHE", None)
        self.addCleanup(setattr, tm, "_get_ta", tm._get_ta)
        tm._get_ta = lambda: None

    def _ffmpeg_satiri(self):
        r = _fn(tm.health_check)()
        return next(c for c in r["checks"] if c["name"] == "ffmpeg")

    def test_imageio_yedegi_ok_ama_aciklamali(self):
        tm._FFMPEG_CACHE = ("C:\\pkg\\ffmpeg.exe", "imageio")
        c = self._ffmpeg_satiri()
        self.assertEqual(c["status"], "ok")
        self.assertIn("imageio-ffmpeg", c["detail"])

    def test_hic_yoksa_uyari(self):
        tm._FFMPEG_CACHE = (None, "yok")
        c = self._ffmpeg_satiri()
        self.assertEqual(c["status"], "uyarı")
        self.assertIn("winget", c["detail"])

    def test_path_ok(self):
        tm._FFMPEG_CACHE = ("C:\\bin\\ffmpeg.exe", "path")
        c = self._ffmpeg_satiri()
        self.assertEqual(c["status"], "ok")
        self.assertIn("PATH", c["detail"])


class GercekOrtamTest(unittest.TestCase):
    """Bu makinede ffmpeg gerçekten çözülüyor mu (entegrasyon)."""

    def test_ffmpeg_bulunuyor(self):
        tm._FFMPEG_CACHE = None
        self.addCleanup(setattr, tm, "_FFMPEG_CACHE", None)
        yol, kaynak = tm._resolve_ffmpeg()
        self.assertNotEqual(kaynak, "yok", "ffmpeg hiçbir kaynaktan bulunamadı")
        self.assertTrue(yol)


if __name__ == "__main__":
    unittest.main()