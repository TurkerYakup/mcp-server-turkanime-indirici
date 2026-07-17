"""health_check, jellyfin adlandırma ve check_new_episodes için birim testler.

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


class _FakeBolum:
    def __init__(self, slug, title):
        self.slug = slug
        self.title = title


class _FakeAnime:
    def __init__(self, slug, title, bolumler):
        self.slug = slug
        self.title = title
        self.bolumler = bolumler


class NamingTest(unittest.TestCase):
    """naming="jellyfin" dosyayı S01E04 biçiminde adlandırmalı."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ta-mcp-naming-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.bolum = _FakeBolum("horimiya-4-bolum", "4. Bölüm")

        for ad, deger in (("_STATE_DIR", self.base),
                          ("_STATE_FILE", os.path.join(self.base, "jobs.json"))):
            self.addCleanup(setattr, tm, ad, getattr(tm, ad))
            setattr(tm, ad, deger)

        # yt-dlp'nin indirdiği ham dosyayı taklit et.
        ham_dir = os.path.join(self.base, "horimiya")
        os.makedirs(ham_dir)
        self.ham = os.path.join(ham_dir, "horimiya-4-bolum.mp4")
        with open(self.ham, "wb") as fh:
            fh.write(b"\0" * 100)

        with tm._JOBS_LOCK:
            tm.JOBS.clear()
            tm.JOBS["j"] = {"job_id": "j", "status": "finished", "file": self.ham}
        self.addCleanup(tm.JOBS.clear)

    def _sonuc_dosyasi(self):
        d = os.path.join(self.base, "Horimiya")
        return sorted(os.listdir(d))

    def test_default_adlandirma(self):
        tm._finalize_file("j", self.base, "Horimiya", self.bolum, 3)
        self.assertEqual(self._sonuc_dosyasi(), ["Horimiya - 004.mp4"])

    def test_jellyfin_adlandirma(self):
        tm._finalize_file("j", self.base, "Horimiya", self.bolum, 3,
                          naming="jellyfin", season_number=1)
        self.assertEqual(self._sonuc_dosyasi(), ["Horimiya - S01E04.mp4"])

    def test_jellyfin_sezon_numarasi(self):
        tm._finalize_file("j", self.base, "Horimiya", self.bolum, 3,
                          naming="jellyfin", season_number=2)
        self.assertEqual(self._sonuc_dosyasi(), ["Horimiya - S02E04.mp4"])

    def test_jellyfin_dosyasi_verify_tarafindan_taninir(self):
        """verify_library/skip_existing jellyfin adlandırmasını 'ok' saymalı."""
        # setUp'ın ham dosyasından etkilenmemek için ayrı bir kök kullan
        # (Windows'ta <base>/horimiya ile <base>/Horimiya AYNI klasördür).
        base = tempfile.mkdtemp(prefix="ta-mcp-naming2-")
        self.addCleanup(shutil.rmtree, base, True)
        buyuk = tm._MIN_VALID_BYTES + 1
        d = os.path.join(base, "Horimiya")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "Horimiya - S01E04.mp4"), "wb") as fh:
            fh.write(b"\0" * buyuk)
        anime = _FakeAnime("horimiya", "Horimiya",
                           [_FakeBolum(f"horimiya-{i}-bolum", f"{i}") for i in range(1, 5)])
        durumlar = {s["episode"]: s["status"]
                    for s in tm._library_states(anime, base, "Horimiya")}
        self.assertEqual(durumlar[4], "ok")
        self.assertEqual(durumlar[1], "missing")

    def test_jellyfin_yanlis_bolumu_eslestirmez(self):
        self.assertFalse(tm._file_belongs_to("Show - S01E11", "Show", "001", "x-1-bolum"))
        self.assertTrue(tm._file_belongs_to("Show - S01E11", "Show", "011", "x-11-bolum"))


class CheckNewEpisodesTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ta-mcp-new-")
        self.addCleanup(shutil.rmtree, self.base, True)

        for ad, deger in (("_STATE_DIR", self.base),
                          ("_STATE_FILE", os.path.join(self.base, "jobs.json"))):
            self.addCleanup(setattr, tm, ad, getattr(tm, ad))
            setattr(tm, ad, deger)

        with tm._SERIES_LOCK:
            tm._SERIES.clear()
        self.addCleanup(tm._SERIES.clear)
        with tm._JOBS_LOCK:
            tm.JOBS.clear()
        self.addCleanup(tm.JOBS.clear)

        self.addCleanup(setattr, tm, "_get_anime", tm._get_anime)
        self.addCleanup(setattr, tm, "_EXECUTOR", tm._EXECUTOR)

        class _Exec:
            def submit(self, fn, *a, **k):
                pass
        tm._EXECUTOR = _Exec()

    def _anime_kur(self, n):
        anime = _FakeAnime("horimiya", "Horimiya",
                           [_FakeBolum(f"horimiya-{i}-bolum", f"{i}. Bölüm")
                            for i in range(1, n + 1)])
        tm._get_anime = lambda slug, refresh=False: anime
        return anime

    def test_ilk_kontrol_yeni_bildirmez(self):
        """İlk çağrıda tüm sezon 'yeni' sanılmamalı; sadece temel kaydedilir."""
        self._anime_kur(12)
        r = _fn(tm.check_new_episodes)("horimiya")
        self.assertTrue(r["first_check"])
        self.assertEqual(r["new_episodes"], [])
        self.assertEqual(r["episode_count"], 12)
        self.assertIsNone(r["previous_count"])

    def test_yeni_bolum_tespit_edilir(self):
        self._anime_kur(12)
        _fn(tm.check_new_episodes)("horimiya")   # temel: 12
        self._anime_kur(14)
        r = _fn(tm.check_new_episodes)("horimiya")
        self.assertFalse(r["first_check"])
        self.assertEqual(r["previous_count"], 12)
        self.assertEqual([e["episode"] for e in r["new_episodes"]], [13, 14])
        self.assertEqual([e["index"] for e in r["new_episodes"]], [12, 13])

    def test_yeni_yoksa_bos(self):
        self._anime_kur(12)
        _fn(tm.check_new_episodes)("horimiya")
        r = _fn(tm.check_new_episodes)("horimiya")
        self.assertEqual(r["new_episodes"], [])
        self.assertIn("Yeni bölüm yok", r["note"])

    def test_auto_download_kuyruga_alir(self):
        self._anime_kur(12)
        _fn(tm.check_new_episodes)("horimiya")
        self._anime_kur(13)
        r = _fn(tm.check_new_episodes)("horimiya", auto_download=True,
                                       output_dir=self.base)
        self.assertTrue(r["downloaded"])
        self.assertEqual(len(r["queued_job_ids"]), 1)

    def test_auto_download_kapaliyken_indirmez(self):
        self._anime_kur(12)
        _fn(tm.check_new_episodes)("horimiya")
        self._anime_kur(13)
        r = _fn(tm.check_new_episodes)("horimiya")
        self.assertFalse(r["downloaded"])
        self.assertEqual(r["queued_job_ids"], [])

    def test_seri_durumu_kalici(self):
        """Restart sonrası 'en son kaç bölüm gördüm' hatırlanmalı."""
        self._anime_kur(12)
        _fn(tm.check_new_episodes)("horimiya")
        with tm._SERIES_LOCK:
            tm._SERIES.clear()
        tm._load_state()
        self.assertEqual(tm._series_get("horimiya")["episode_count"], 12)


class HealthCheckTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ta-mcp-health-")
        self.addCleanup(shutil.rmtree, self.base, True)
        for ad, deger in (("_STATE_DIR", self.base),
                          ("_DEFAULT_OUTPUT_DIR", self.base)):
            self.addCleanup(setattr, tm, ad, getattr(tm, ad))
            setattr(tm, ad, deger)
        self.addCleanup(setattr, tm, "_get_ta", tm._get_ta)

    def _isim_status(self, r):
        return {c["name"]: c["status"] for c in r["checks"]}

    def test_turkanime_api_yoksa_hata(self):
        def patlat():
            raise RuntimeError("paket yok")
        tm._get_ta = patlat
        r = _fn(tm.health_check)()
        d = self._isim_status(r)
        self.assertEqual(d["turkanime_api"], "hata")
        self.assertEqual(d["turkanime.tv"], "hata")
        self.assertEqual(r["overall"], "hata")

    def test_output_dir_yazilabilir(self):
        tm._get_ta = lambda: None
        r = _fn(tm.health_check)()
        self.assertEqual(self._isim_status(r)["output_dir"], "ok")

    def test_tum_kontroller_var(self):
        tm._get_ta = lambda: None
        r = _fn(tm.health_check)()
        self.assertEqual(
            set(self._isim_status(r)),
            {"turkanime_api", "turkanime.tv", "ffmpeg", "output_dir",
             "ca_bundle", "state_dir"})
        self.assertIn(r["overall"], ("ok", "uyarı", "hata"))


if __name__ == "__main__":
    unittest.main()