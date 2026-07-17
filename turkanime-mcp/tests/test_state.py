"""İş durumu kalıcılığı (jobs.json) için birim testler.

Ağ ya da turkanime_api gerektirmez. TURKANIME_STATE_DIR geçici bir klasöre
yönlendirilir; modül bu değişkeni import anında okuduğu için testler modül
seviyesindeki yol sabitlerini doğrudan yamalar.

Çalıştırmak için:  python -m unittest discover -s turkanime-mcp/tests -t turkanime-mcp/tests
"""

import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import turkanime_mcp as tm  # noqa: E402


class StatePersistTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ta-mcp-state-")
        self.addCleanup(shutil.rmtree, self.dir, True)

        # Modül sabitlerini geçici klasöre yamala (import anında okunmuşlardı).
        self._eski_dir, self._eski_file = tm._STATE_DIR, tm._STATE_FILE
        tm._STATE_DIR = self.dir
        tm._STATE_FILE = os.path.join(self.dir, "jobs.json")
        self.addCleanup(self._geri_al)

        # Her test temiz bir JOBS ile başlasın.
        with tm._JOBS_LOCK:
            tm.JOBS.clear()
        self.addCleanup(self._jobs_temizle)

        # Debounce'u kapat: testler ara güncellemelerin de yazılmasını bekler.
        self._eski_flush = tm._STATE_FLUSH_SECS
        tm._STATE_FLUSH_SECS = 0.0
        tm._state_last_flush = 0.0
        tm._state_written_seq = 0

    def _geri_al(self):
        tm._STATE_DIR, tm._STATE_FILE = self._eski_dir, self._eski_file
        tm._STATE_FLUSH_SECS = self._eski_flush

    def _jobs_temizle(self):
        with tm._JOBS_LOCK:
            tm.JOBS.clear()

    def _is_ekle(self, jid, status, **ek):
        with tm._JOBS_LOCK:
            tm.JOBS[jid] = {"job_id": jid, "status": status, "anime": "Horimiya",
                            "bolum": "1. Bölüm", "cancel": False, **ek}
            tm._bump_state_seq_locked()
        tm._persist_jobs(force=True)

    def test_yazip_geri_yukleme(self):
        self._is_ekle("j1", "finished")
        self.assertTrue(os.path.exists(tm._STATE_FILE))
        self._jobs_temizle()
        self.assertEqual(tm._load_state(), 1)
        self.assertEqual(tm.JOBS["j1"]["status"], "finished")
        self.assertTrue(tm.JOBS["j1"]["restored"])

    def test_yarim_isler_interrupted_olur(self):
        """Restart sonrası ASLA yanlış 'downloading' gösterilmemeli."""
        for jid, status in [("d", "downloading"), ("q", "queued"),
                            ("s", "kaynak_araniyor")]:
            self._is_ekle(jid, status)
        self._jobs_temizle()
        tm._load_state()
        for jid in ("d", "q", "s"):
            self.assertEqual(tm.JOBS[jid]["status"], "interrupted", jid)
            self.assertEqual(tm.JOBS[jid]["error"], tm._INTERRUPTED_MSG)

    def test_biten_isler_korunur(self):
        for jid, status in [("f", "finished"), ("e", "error"), ("c", "cancelled")]:
            self._is_ekle(jid, status)
        self._jobs_temizle()
        tm._load_state()
        self.assertEqual(tm.JOBS["f"]["status"], "finished")
        self.assertEqual(tm.JOBS["e"]["status"], "error")
        self.assertEqual(tm.JOBS["c"]["status"], "cancelled")

    def test_cancel_bayragi_temizlenir(self):
        """Yüklenen işin thread'i yok; bekleyen iptal bayrağı anlamsızdır."""
        self._is_ekle("j", "downloading", cancel=True)
        self._jobs_temizle()
        tm._load_state()
        self.assertFalse(tm.JOBS["j"]["cancel"])

    def test_job_set_status_degisimini_yazar(self):
        self._is_ekle("j", "queued")
        tm._job_set("j", status="downloading", percent=" 10.0%")
        self._jobs_temizle()
        tm._load_state()
        # downloading -> yükleyince interrupted olur, ama percent korunmuş olmalı
        self.assertEqual(tm.JOBS["j"]["percent"], " 10.0%")
        self.assertEqual(tm.JOBS["j"]["status"], "interrupted")

    def test_bozuk_dosya_sunucuyu_dusurmez(self):
        with open(tm._STATE_FILE, "w", encoding="utf-8") as fh:
            fh.write("{bu gecerli json degil")
        self.assertEqual(tm._load_state(), 0)

    def test_dosya_yoksa_sifir_doner(self):
        self.assertEqual(tm._load_state(), 0)

    def test_eski_snapshot_yeniyi_ezmez(self):
        """Yarış durumunda geç kalan eski snapshot yazılmamalı."""
        self._is_ekle("j", "finished")
        tm._write_state_file({"j": {"job_id": "j", "status": "eski"}}, seq=0)
        self._jobs_temizle()
        tm._load_state()
        self.assertEqual(tm.JOBS["j"]["status"], "finished")


class DownloadStatusFilterTest(unittest.TestCase):
    def setUp(self):
        with tm._JOBS_LOCK:
            tm.JOBS.clear()
            tm.JOBS["a"] = {"job_id": "a", "status": "downloading"}
            tm.JOBS["b"] = {"job_id": "b", "status": "finished"}
            tm.JOBS["c"] = {"job_id": "c", "status": "interrupted"}
        self.addCleanup(tm.JOBS.clear)

    def _fn(self):
        # FastMCP dekoratörü fonksiyonu sarabilir; alttaki fonksiyonu çağır.
        return getattr(tm.download_status, "fn", tm.download_status)

    def test_include_history_true_hepsini_verir(self):
        self.assertEqual(len(self._fn()(include_history=True)), 3)

    def test_include_history_false_sadece_aktif(self):
        aktif = self._fn()(include_history=False)
        self.assertEqual([j["job_id"] for j in aktif], ["a"])

    def test_job_id_verilince_filtre_yok_sayilir(self):
        job = self._fn()(job_id="b", include_history=False)
        self.assertEqual(job["status"], "finished")


if __name__ == "__main__":
    unittest.main()