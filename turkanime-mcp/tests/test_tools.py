"""skip_existing, get_batch_summary ve retry_job için birim testler.

Ağ gerektirmez: _get_anime ve _EXECUTOR sahtelerle değiştirilir.

Çalıştırmak için:  python -m unittest discover -s turkanime-mcp/tests -t turkanime-mcp/tests
"""

import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import turkanime_mcp as tm  # noqa: E402


class _FakeBolum:
    def __init__(self, slug, title):
        self.slug = slug
        self.title = title


class _FakeAnime:
    def __init__(self, slug, title, bolumler):
        self.slug = slug
        self.title = title
        self.bolumler = bolumler


class _FakeExecutor:
    """submit()'i yutar — testte gerçek indirme thread'i başlatılmasın."""

    def __init__(self):
        self.submits = []

    def submit(self, fn, *args, **kwargs):
        self.submits.append(args)


def _fn(tool):
    return getattr(tool, "fn", tool)


class _Base(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ta-mcp-tools-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.big = tm._MIN_VALID_BYTES + 1

        # Durum yazımını geçici klasöre yönlendir.
        for ad, deger in (("_STATE_DIR", self.base),
                          ("_STATE_FILE", os.path.join(self.base, "jobs.json"))):
            self.addCleanup(setattr, tm, ad, getattr(tm, ad))
            setattr(tm, ad, deger)

        self.anime = _FakeAnime("horimiya", "Horimiya",
                                [_FakeBolum(f"horimiya-{i}-bolum", f"{i}. Bölüm")
                                 for i in range(1, 5)])
        self.addCleanup(setattr, tm, "_get_anime", tm._get_anime)
        tm._get_anime = lambda slug, refresh=False: self.anime

        self.executor = _FakeExecutor()
        self.addCleanup(setattr, tm, "_EXECUTOR", tm._EXECUTOR)
        tm._EXECUTOR = self.executor

        with tm._JOBS_LOCK:
            tm.JOBS.clear()
        self.addCleanup(self._temizle)

    def _temizle(self):
        with tm._JOBS_LOCK:
            tm.JOBS.clear()

    def _yaz(self, klasor, ad, boyut):
        yol = os.path.join(self.base, klasor)
        os.makedirs(yol, exist_ok=True)
        with open(os.path.join(yol, ad), "wb") as fh:
            fh.write(b"\0" * boyut)


class SkipExistingTest(_Base):
    def test_varsayilan_kapali_hepsi_kuyruga(self):
        self._yaz("Horimiya", "Horimiya - 001.mp4", self.big)
        r = _fn(tm.download_season)("horimiya", output_dir=self.base)
        self.assertEqual(r["queued"], 4)
        self.assertEqual(r["skipped"], [])

    def test_tam_bolumler_atlanir(self):
        self._yaz("Horimiya", "Horimiya - 001.mp4", self.big)
        self._yaz("Horimiya", "Horimiya - 002.mp4", self.big)
        r = _fn(tm.download_season)("horimiya", output_dir=self.base,
                                    skip_existing=True)
        self.assertEqual(r["skipped"], [1, 2])
        self.assertEqual(r["queued"], 2)

    def test_yarim_bolum_atlanmaz(self):
        """partial olan bölüm 'var' sayılmamalı — yeniden indirilmeli."""
        self._yaz("Horimiya", "Horimiya - 003.mp4.part", self.big)
        r = _fn(tm.download_season)("horimiya", output_dir=self.base,
                                    skip_existing=True)
        self.assertEqual(r["skipped"], [])
        self.assertEqual(r["queued"], 4)

    def test_hepsi_varsa_hicbiri_kuyruga_girmez(self):
        for i in range(1, 5):
            self._yaz("Horimiya", f"Horimiya - {i:03d}.mp4", self.big)
        r = _fn(tm.download_season)("horimiya", output_dir=self.base,
                                    skip_existing=True)
        self.assertEqual(r["queued"], 0)
        self.assertEqual(r["skipped"], [1, 2, 3, 4])

    def test_download_episodes_ile_de_calisir(self):
        self._yaz("Horimiya", "Horimiya - 001.mp4", self.big)
        r = _fn(tm.download_episodes)("horimiya", "0-1", output_dir=self.base,
                                      skip_existing=True)
        self.assertEqual(r["skipped"], [1])
        self.assertEqual(r["queued"], 1)


class BatchSummaryTest(_Base):
    def _isler(self, *durumlar):
        with tm._JOBS_LOCK:
            for i, (status, ek) in enumerate(durumlar):
                tm.JOBS[f"j{i}"] = {"job_id": f"j{i}", "status": status,
                                    "bolum": f"{i}. Bölüm", **ek}

    def test_sayimlar(self):
        self._isler(("finished", {}), ("downloading", {"percent": " 50.0%"}),
                    ("queued", {}), ("error", {"error": "olmadı"}),
                    ("cancelled", {}), ("interrupted", {}))
        r = _fn(tm.get_batch_summary)()
        self.assertEqual(r["total"], 6)
        self.assertEqual(r["finished"], 1)
        self.assertEqual(r["downloading"], 1)
        self.assertEqual(r["queued"], 1)
        self.assertEqual(r["error"], 1)
        self.assertEqual(r["cancelled"], 1)
        self.assertEqual(r["interrupted"], 1)

    def test_ortalama_yuzde(self):
        # finished=100, downloading=50, queued=0 -> 50.0
        self._isler(("finished", {}), ("downloading", {"percent": " 50.0%"}),
                    ("queued", {}))
        self.assertEqual(_fn(tm.get_batch_summary)()["average_percent"], 50.0)

    def test_eta_inen_islerin_en_buyugu(self):
        self._isler(("downloading", {"eta": 30}), ("downloading", {"eta": 120}),
                    ("queued", {"eta": 999}))
        self.assertEqual(_fn(tm.get_batch_summary)()["eta_seconds"], 120)

    def test_hata_listesi(self):
        self._isler(("error", {"error": "kaynak yok"}), ("finished", {}))
        hatalar = _fn(tm.get_batch_summary)()["errors"]
        self.assertEqual(len(hatalar), 1)
        self.assertEqual(hatalar[0]["error"], "kaynak yok")
        self.assertEqual(hatalar[0]["bolum"], "0. Bölüm")

    def test_job_ids_filtresi(self):
        self._isler(("finished", {}), ("error", {"error": "x"}))
        r = _fn(tm.get_batch_summary)(job_ids=["j0"])
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["finished"], 1)

    def test_bilinmeyen_job_id_raporlanir(self):
        self._isler(("finished", {}))
        r = _fn(tm.get_batch_summary)(job_ids=["j0", "yok"])
        self.assertEqual(r["job_ids_unknown"], ["yok"])
        self.assertEqual(r["total"], 1)

    def test_bos(self):
        r = _fn(tm.get_batch_summary)()
        self.assertEqual(r["total"], 0)
        self.assertIsNone(r["average_percent"])
        self.assertIsNone(r["eta_seconds"])


class RetryJobTest(_Base):
    def _is_olustur(self):
        r = _fn(tm.download_episodes)("horimiya", 0, output_dir=self.base)
        return r["job_ids"][0]

    def test_hatali_is_yeniden_kuyruga_alinir(self):
        jid = self._is_olustur()
        tm._job_set(jid, status="error", error="kaynak yok")
        r = _fn(tm.retry_job)(jid)
        self.assertEqual(r["retry_of"], jid)
        self.assertNotEqual(r["job_id"], jid)
        self.assertEqual(r["status"], "queued")
        with tm._JOBS_LOCK:
            self.assertEqual(tm.JOBS[jid]["retried_as"], r["job_id"])
            self.assertEqual(tm.JOBS[r["job_id"]]["bolum_slug"], "horimiya-1-bolum")

    def test_interrupted_is_yeniden_denenebilir(self):
        """Restart sonrası kesilen işler retry_job ile kurtarılabilmeli."""
        jid = self._is_olustur()
        tm._job_set(jid, status="interrupted")
        r = _fn(tm.retry_job)(jid)
        self.assertEqual(r["retry_of"], jid)

    def test_biten_ise_dokunmaz(self):
        jid = self._is_olustur()
        tm._job_set(jid, status="finished")
        r = _fn(tm.retry_job)(jid)
        self.assertNotIn("retry_of", r)
        self.assertEqual(r["status"], "finished")

    def test_suren_ise_dokunmaz(self):
        jid = self._is_olustur()
        tm._job_set(jid, status="downloading")
        r = _fn(tm.retry_job)(jid)
        self.assertNotIn("retry_of", r)

    def test_bilinmeyen_is(self):
        self.assertIn("error", _fn(tm.retry_job)("yok-boyle-bir-is"))

    def test_orijinal_parametreler_korunur(self):
        r0 = _fn(tm.download_episodes)("horimiya", 0, output_dir=self.base,
                                       fansub="TestSub", max_resolution=False,
                                       resume=False)
        jid = r0["job_ids"][0]
        tm._job_set(jid, status="error")
        yeni = _fn(tm.retry_job)(jid)["job_id"]
        with tm._JOBS_LOCK:
            params = tm.JOBS[yeni]["params"]
        self.assertEqual(params["fansub"], "TestSub")
        self.assertFalse(params["max_resolution"])
        self.assertFalse(params["resume"])

    def test_params_yoksa_anlamli_hata(self):
        """Bu özellikten önce oluşmuş (kalıcı durumdan gelen) işler."""
        with tm._JOBS_LOCK:
            tm.JOBS["eski"] = {"job_id": "eski", "status": "error"}
        self.assertIn("parametreleri yok", _fn(tm.retry_job)("eski")["error"])


if __name__ == "__main__":
    unittest.main()