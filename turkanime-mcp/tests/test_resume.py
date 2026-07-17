"""Yarım indirmeyi devam ettirme (.part resume) mantığı için birim testler.

Ağ gerektirmez: sahte bir Video nesnesinin `indir()` çağrıları senaryoya göre
gerçek dosyalar yazar, `_download_task` bunun üzerinde çalıştırılır.

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
    slug = "horimiya-1-bolum"
    title = "1. Bölüm"


class _FakeVideo:
    """indir() çağrıldıkça `senaryo` listesindeki adımı uygular."""

    def __init__(self, player, senaryo):
        self.player = player
        self.fansub = "TestSub"
        self.resolution = 1080
        self.is_supported = True
        self.is_working = True
        self.ydl_opts = {}
        self.senaryo = senaryo
        self.calls = 0

    def indir(self, callback=None, output=""):
        adim = self.senaryo[min(self.calls, len(self.senaryo) - 1)]
        self.calls += 1
        adim(output)


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ta-mcp-resume-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.folder = os.path.join(self.base, "horimiya")
        self.bolum = _FakeBolum()
        self.big = tm._MIN_VALID_BYTES + 1

        # Durum yazımını geçici klasöre yönlendir (gerçek %PROGRAMDATA%'ya değil).
        eski_file = tm._STATE_FILE
        tm._STATE_FILE = os.path.join(self.base, "jobs.json")
        self.addCleanup(setattr, tm, "_STATE_FILE", eski_file)

        eski_dir = tm._STATE_DIR
        tm._STATE_DIR = self.base
        self.addCleanup(setattr, tm, "_STATE_DIR", eski_dir)

        self._eski_pick = tm._pick_video
        self.addCleanup(setattr, tm, "_pick_video", self._eski_pick)

        with tm._JOBS_LOCK:
            tm.JOBS.clear()
        self.addCleanup(self._temizle)

    def _temizle(self):
        with tm._JOBS_LOCK:
            tm.JOBS.clear()

    # --- senaryo adımları ---------------------------------------------------
    def _yaz(self, ad, boyut):
        os.makedirs(self.folder, exist_ok=True)
        with open(os.path.join(self.folder, ad), "wb") as fh:
            fh.write(b"\0" * boyut)

    def _part_yaz(self, boyut):
        return lambda output: self._yaz("horimiya-1-bolum.mp4.part", boyut)

    def _tamamla(self):
        def adim(output):
            part = os.path.join(self.folder, "horimiya-1-bolum.mp4.part")
            if os.path.exists(part):
                os.remove(part)
            self._yaz("horimiya-1-bolum.mp4", self.big)
        return adim

    def _hicbir_sey(self):
        return lambda output: None

    # --- yardımcılar --------------------------------------------------------
    def _videolari_kur(self, *videolar):
        """_pick_video'yu sırayla videoları veren (exclude'a saygılı) sahteyle değiştir."""
        def sahte(bolum, by_res, by_fansub, exclude, callback):
            for v in videolar:
                if v.player not in exclude:
                    return v
            return None
        tm._pick_video = sahte

    def _is_kur(self, jid="j1"):
        with tm._JOBS_LOCK:
            tm.JOBS[jid] = {"job_id": jid, "status": "queued", "cancel": False}
        return jid

    def _calistir(self, jid, resume=True):
        tm._download_task(jid, "horimiya", "Horimiya", self.bolum, 0,
                          self.base, "Horimiya", None, True, False, resume)

    def _status(self, jid):
        with tm._JOBS_LOCK:
            return tm.JOBS[jid]["status"]

    # --- testler ------------------------------------------------------------
    def test_resume_ile_tamamlanir(self):
        """İlk deneme yarım kalır; aynı player'da devam edip tamamlanır."""
        v = _FakeVideo("PLAYER_A", [self._part_yaz(500), self._tamamla()])
        self._videolari_kur(v)
        jid = self._is_kur()
        self._calistir(jid, resume=True)
        self.assertEqual(self._status(jid), "finished")
        self.assertEqual(v.calls, 2, "aynı player'da resume denenmeliydi")
        self.assertTrue(v.ydl_opts.get("continuedl"))

    def test_resume_ilerlemezse_kaynak_degisir(self):
        """.part büyümüyorsa partial silinip farklı kaynağa geçilmeli."""
        a = _FakeVideo("PLAYER_A", [self._part_yaz(500), self._hicbir_sey()])
        b = _FakeVideo("PLAYER_B", [self._tamamla()])
        self._videolari_kur(a, b)
        jid = self._is_kur()
        self._calistir(jid, resume=True)
        self.assertEqual(self._status(jid), "finished")
        self.assertEqual(a.calls, 2, "A: 1 indirme + 1 resume denemesi")
        self.assertEqual(b.calls, 1, "B kaynağına geçilmeliydi")

    def test_resume_kapaliyken_denenmez(self):
        a = _FakeVideo("PLAYER_A", [self._part_yaz(500)])
        b = _FakeVideo("PLAYER_B", [self._tamamla()])
        self._videolari_kur(a, b)
        jid = self._is_kur()
        self._calistir(jid, resume=False)
        self.assertEqual(self._status(jid), "finished")
        self.assertEqual(a.calls, 1, "resume=False iken devam denenmemeli")
        self.assertFalse(a.ydl_opts.get("continuedl"))

    def test_kaynak_degisince_yarim_dosya_silinir(self):
        """Farklı player = farklı URL; yarım veri KULLANILMAMALI (bozuk dosya riski)."""
        gorulen = {}

        def b_adim(output):
            part = os.path.join(self.folder, "horimiya-1-bolum.mp4.part")
            gorulen["part_var"] = os.path.exists(part)
            self._tamamla()(output)

        a = _FakeVideo("PLAYER_A", [self._part_yaz(500), self._hicbir_sey()])
        b = _FakeVideo("PLAYER_B", [b_adim])
        self._videolari_kur(a, b)
        jid = self._is_kur()
        self._calistir(jid, resume=True)
        self.assertFalse(gorulen["part_var"],
                         "B kaynağı A'nın .part dosyasını devralmamalı")

    def test_onceki_oturumdan_kalan_part_silinir(self):
        """İş başlarken bilinmeyen kaynaktan kalmış .part temizlenmeli."""
        self._yaz("horimiya-1-bolum.mp4.part", 12345)
        gorulen = {}

        def adim(output):
            part = os.path.join(self.folder, "horimiya-1-bolum.mp4.part")
            gorulen["part_var"] = os.path.exists(part)
            self._tamamla()(output)

        v = _FakeVideo("PLAYER_A", [adim])
        self._videolari_kur(v)
        jid = self._is_kur()
        self._calistir(jid, resume=True)
        self.assertFalse(gorulen["part_var"],
                         "önceki oturumun .part dosyası devralınmamalı")
        self.assertEqual(self._status(jid), "finished")

    def test_hic_kaynak_yoksa_hata(self):
        self._videolari_kur()
        jid = self._is_kur()
        self._calistir(jid)
        self.assertEqual(self._status(jid), "error")

    def test_tum_kaynaklar_basarisizsa_hata(self):
        """Asla yanlış 'finished' bildirilmemeli."""
        a = _FakeVideo("PLAYER_A", [self._part_yaz(500), self._hicbir_sey()])
        b = _FakeVideo("PLAYER_B", [self._part_yaz(500), self._hicbir_sey()])
        self._videolari_kur(a, b)
        jid = self._is_kur()
        self._calistir(jid, resume=True)
        self.assertEqual(self._status(jid), "error")
        with tm._JOBS_LOCK:
            self.assertIn("PLAYER_A", tm.JOBS[jid]["error"])


class PartialSizeTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ta-mcp-psize-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.folder = os.path.join(self.base, "horimiya")
        os.makedirs(self.folder)
        self.bolum = _FakeBolum()

    def test_yok_ise_sifir(self):
        self.assertEqual(tm._partial_size(self.base, "horimiya", self.bolum), 0)

    def test_part_ve_frag_toplanir(self):
        for ad, boyut in [("horimiya-1-bolum.mp4.part", 100),
                          ("horimiya-1-bolum.mp4.part-Frag3", 50)]:
            with open(os.path.join(self.folder, ad), "wb") as fh:
                fh.write(b"\0" * boyut)
        self.assertEqual(tm._partial_size(self.base, "horimiya", self.bolum), 150)

    def test_tam_dosya_sayilmaz(self):
        with open(os.path.join(self.folder, "horimiya-1-bolum.mp4"), "wb") as fh:
            fh.write(b"\0" * 999)
        self.assertEqual(tm._partial_size(self.base, "horimiya", self.bolum), 0)


if __name__ == "__main__":
    unittest.main()