"""Kod incelemesinde bulunan hatalar için regresyon testleri.

Her test, düzeltilmeden ÖNCE başarısız olan somut bir senaryoyu sabitler.

Çalıştırmak için:  python -m unittest discover -s turkanime-mcp/tests -t turkanime-mcp/tests
"""

import os
import sys
import copy
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import turkanime_mcp as tm  # noqa: E402


def _fn(tool):
    return getattr(tool, "fn", tool)


class _B:
    def __init__(self, slug, title):
        self.slug, self.title = slug, title


class _A:
    def __init__(self, slug, title, bolumler):
        self.slug, self.title, self.bolumler = slug, title, bolumler


class CleanupVeriKaybiTest(unittest.TestCase):
    """only_partial=True TAMAMLANMIŞ dosyaya dokunmamalı (veri kaybı regresyonu)."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ta-mcp-reg1-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.folder = os.path.join(self.base, "horimiya")
        os.makedirs(self.folder)
        self.bolum = _B("horimiya-1-bolum", "1. Bölüm")

    def _yaz(self, ad, boyut=5_000_000):
        yol = os.path.join(self.folder, ad)
        with open(yol, "wb") as fh:
            fh.write(b"\0" * boyut)
        return yol

    def test_only_partial_tamamlanmis_dosyayi_silmez(self):
        """rename=False düzeninde tam dosya adı da slug ile başlar — SİLİNMEMELİ."""
        tam = self._yaz("horimiya-1-bolum.mp4")
        tm._cleanup_partials(self.base, "horimiya", self.bolum, only_partial=True)
        self.assertTrue(os.path.exists(tam), "tamamlanmış dosya silindi (veri kaybı)")

    def test_only_partial_yarim_dosyalari_siler(self):
        part = self._yaz("horimiya-1-bolum.mp4.part", 1000)
        ytdl = self._yaz("horimiya-1-bolum.mp4.ytdl", 10)
        tam = self._yaz("horimiya-1-bolum.mp4")
        tm._cleanup_partials(self.base, "horimiya", self.bolum, only_partial=True)
        self.assertFalse(os.path.exists(part))
        self.assertFalse(os.path.exists(ytdl))
        self.assertTrue(os.path.exists(tam))

    def test_iptal_temizligi_hepsini_siler(self):
        """only_partial=False (iptal/hata) eski davranışı korur."""
        tam = self._yaz("horimiya-1-bolum.mp4")
        tm._cleanup_partials(self.base, "horimiya", self.bolum)
        self.assertFalse(os.path.exists(tam))


class AramaHataAyrimiTest(unittest.TestCase):
    """Sağlayıcıya ulaşılamaması ASLA 'Eşleşme yok' diye raporlanmamalı."""

    def setUp(self):
        tm._MANIFEST_CACHE = copy.deepcopy({
            "providers": {"turkanime": {"enabled": True}, "animedepo": {"enabled": True}},
            "features": {"search": {"fallback_provider": "animedepo",
                                    "force_fallback": False}},
        })
        self.addCleanup(setattr, tm, "_MANIFEST_CACHE", None)
        eski = tm._SEARCH_BACKOFF_SECS
        tm._SEARCH_BACKOFF_SECS = 0.0
        self.addCleanup(setattr, tm, "_SEARCH_BACKOFF_SECS", eski)
        self.addCleanup(setattr, tm, "_search_animedepo", tm._search_animedepo)
        self.addCleanup(setattr, tm, "_get_ta", tm._get_ta)

    def _depo_patlat(self):
        def sahte(q):
            raise RuntimeError("gitlab erisilemedi")
        tm._search_animedepo = sahte

    def test_force_fallback_ve_depo_erisilemez_hata_doner(self):
        """force_fallback=True iken TürkAnime hiç denenmez; depo patlarsa HATA."""
        tm._MANIFEST_CACHE["features"]["search"]["force_fallback"] = True
        self._depo_patlat()
        r = _fn(tm.search_anime)("one piece")
        self.assertIn("error", r, f"'Eşleşme yok' yanılgısı: {r}")
        self.assertTrue(r["retryable"])

    def test_turkanime_disabled_ve_depo_erisilemez_hata_doner(self):
        tm._MANIFEST_CACHE["providers"]["turkanime"]["enabled"] = False
        self._depo_patlat()
        r = _fn(tm.search_anime)("one piece")
        self.assertIn("error", r, f"'Eşleşme yok' yanılgısı: {r}")
        self.assertTrue(r["retryable"])

    def test_iki_saglayici_da_erisilemez_ikisini_de_bildirir(self):
        class F:
            class Anime:
                @staticmethod
                def arama_yap(q):
                    raise RuntimeError("turkanime down")
        tm._get_ta = lambda: F()
        self._depo_patlat()
        r = _fn(tm.search_anime)("one piece")
        self.assertIn("TürkAnime", r["error"])
        self.assertIn("AnimeDepo", r["error"])
        self.assertTrue(r["retryable"])


class ParalelLimitTest(unittest.TestCase):
    """retry_job/verify_library devam eden indirmelerin hızını düşürmemeli."""

    def setUp(self):
        self.addCleanup(tm._set_parallel_limit, None)

    def test_keep_parallel_limiti_korur(self):
        tm._set_parallel_limit(6)
        self.assertEqual(tm._current_parallel_limit(), 6)

        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        anime = _A("horimiya", "Horimiya", [_B("horimiya-1-bolum", "1")])
        self.addCleanup(setattr, tm, "_get_anime", tm._get_anime)
        tm._get_anime = lambda s, refresh=False: anime
        self.addCleanup(setattr, tm, "_EXECUTOR", tm._EXECUTOR)

        class E:
            def submit(self, fn, *a, **k):
                pass
        tm._EXECUTOR = E()
        self.addCleanup(tm.JOBS.clear)

        for ad, deger in (("_STATE_DIR", base),
                          ("_STATE_FILE", os.path.join(base, "jobs.json"))):
            self.addCleanup(setattr, tm, ad, getattr(tm, ad))
            setattr(tm, ad, deger)

        r = tm._start_downloads("horimiya", 0, base, None, None, True, True,
                                tm._KEEP_PARALLEL)
        self.assertEqual(tm._current_parallel_limit(), 6, "limit sıfırlandı")
        self.assertEqual(r["parallel"], 6)

    def test_none_varsayilana_sifirlar(self):
        """download_* için belgelenmiş davranış korunmalı."""
        tm._set_parallel_limit(6)
        tm._set_parallel_limit(None)
        self.assertEqual(tm._current_parallel_limit(), tm._DEFAULT_PARALLEL)


class HookFinishedTest(unittest.TestCase):
    """yt-dlp'nin 'finished'i işi finished YAPMAMALI (yanlış 'bitti' regresyonu)."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ta-mcp-reg4-")
        self.addCleanup(shutil.rmtree, self.base, True)
        for ad, deger in (("_STATE_DIR", self.base),
                          ("_STATE_FILE", os.path.join(self.base, "jobs.json"))):
            self.addCleanup(setattr, tm, ad, getattr(tm, ad))
            setattr(tm, ad, deger)
        self.addCleanup(setattr, tm, "_pick_video", tm._pick_video)
        with tm._JOBS_LOCK:
            tm.JOBS.clear()
        self.addCleanup(tm.JOBS.clear)

    def test_eksik_indirme_finished_yazilmaz(self):
        """indir() 'finished' hook'u atar ama dosya eksik → iş error olmalı."""
        class V:
            player, fansub, resolution = "P_A", "S", 1080
            is_supported = is_working = True
            ydl_opts: dict = {}

            def indir(self, callback=None, output=""):
                # yt-dlp gercekte boyle bir hook atar
                callback({"status": "finished", "_percent_str": " 100.0%",
                          "filename": "x.mp4"})
                # ...ama diske tam dosya YAZILMADI (ignoreerrors)

        tm._pick_video = lambda *a, **k: V()
        with tm._JOBS_LOCK:
            tm.JOBS["j"] = {"job_id": "j", "status": "queued", "cancel": False}

        tm._download_task("j", "horimiya", "Horimiya",
                          _B("horimiya-1-bolum", "1"), 0, self.base, "Horimiya",
                          None, True, False, False)

        with tm._JOBS_LOCK:
            self.assertEqual(tm.JOBS["j"]["status"], "error",
                             "eksik indirme 'finished' gösterildi")
            self.assertEqual(tm.JOBS["j"]["dl_status"], "finished")

    def test_kalici_durumda_da_finished_yok(self):
        """Restart sonrası eksik indirme 'finished' değil 'interrupted' olmalı."""
        with tm._JOBS_LOCK:
            tm.JOBS["j"] = {"job_id": "j", "status": "queued", "cancel": False}
        tm._job_set("j", status="downloading", dl_status="finished")
        tm._persist_jobs(force=True)
        with tm._JOBS_LOCK:
            tm.JOBS.clear()
        tm._load_state()
        self.assertEqual(tm.JOBS["j"]["status"], "interrupted")


class YeniBolumSlugTest(unittest.TestCase):
    """Yeni bölüm tespiti slug'a göre olmalı (sayıya göre değil)."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ta-mcp-reg5-")
        self.addCleanup(shutil.rmtree, self.base, True)
        for ad, deger in (("_STATE_DIR", self.base),
                          ("_STATE_FILE", os.path.join(self.base, "jobs.json"))):
            self.addCleanup(setattr, tm, ad, getattr(tm, ad))
            setattr(tm, ad, deger)
        with tm._SERIES_LOCK:
            tm._SERIES.clear()
        self.addCleanup(tm._SERIES.clear)
        self.addCleanup(setattr, tm, "_get_anime", tm._get_anime)

    def _kur(self, sluglar):
        anime = _A("x", "X", [_B(s, s) for s in sluglar])
        tm._get_anime = lambda s, refresh=False: anime

    def test_araya_bolum_eklenirse_dogru_bulunur(self):
        """Bir bölüm silinip iki yeni eklenirse İKİSİ de yeni sayılmalı."""
        self._kur(["x-1-bolum", "x-2-bolum", "x-3-bolum"])
        _fn(tm.check_new_episodes)("x")                      # temel
        self._kur(["x-1-bolum", "x-3-bolum", "x-4-bolum", "x-5-bolum"])
        r = _fn(tm.check_new_episodes)("x")
        self.assertEqual(sorted(e["slug"] for e in r["new_episodes"]),
                         ["x-4-bolum", "x-5-bolum"])

    def test_basa_ova_eklenirse_dogru_bulunur(self):
        self._kur(["x-1-bolum", "x-2-bolum"])
        _fn(tm.check_new_episodes)("x")
        self._kur(["x-ova-bolum", "x-1-bolum", "x-2-bolum"])
        r = _fn(tm.check_new_episodes)("x")
        self.assertEqual([e["slug"] for e in r["new_episodes"]], ["x-ova-bolum"])

    def test_degisiklik_yoksa_bos(self):
        self._kur(["x-1-bolum", "x-2-bolum"])
        _fn(tm.check_new_episodes)("x")
        r = _fn(tm.check_new_episodes)("x")
        self.assertEqual(r["new_episodes"], [])

    def test_eski_durum_dosyasi_sayiya_duser(self):
        """slugs kaydı olmayan eski state ile geriye uyumlu çalışmalı."""
        with tm._SERIES_LOCK:
            tm._SERIES["x"] = {"episode_count": 2}       # slugs YOK
        self._kur(["x-1-bolum", "x-2-bolum", "x-3-bolum"])
        r = _fn(tm.check_new_episodes)("x")
        self.assertEqual([e["slug"] for e in r["new_episodes"]], ["x-3-bolum"])


if __name__ == "__main__":
    unittest.main()