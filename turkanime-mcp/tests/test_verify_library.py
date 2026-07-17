"""verify_library'nin bölüm-durum mantığı için birim testler.

Ağ ya da turkanime_api gerektirmez: sahte (fake) Anime/Bolum nesneleriyle
gerçek bir geçici klasör üzerinde `_library_states` çalıştırılır.

Çalıştırmak için:  python -m unittest discover -s turkanime-mcp/tests
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


def _write(path, size):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)


class LibraryStatesTest(unittest.TestCase):
    """Gerçek dosya düzenleri üzerinde ok/partial/missing sınıflandırması."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="ta-mcp-test-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.big = tm._MIN_VALID_BYTES + 1
        bolumler = [_FakeBolum(f"horimiya-{i}-bolum", f"{i}. Bölüm")
                    for i in range(1, 5)]
        self.anime = _FakeAnime("horimiya", "Horimiya", bolumler)

    def _states(self):
        return {s["episode"]: s["status"]
                for s in tm._library_states(self.anime, self.base, "Horimiya")}

    def test_bos_klasor_hepsi_missing(self):
        self.assertEqual(self._states(), {1: "missing", 2: "missing",
                                          3: "missing", 4: "missing"})

    def test_finalize_adlandirmasi_ok(self):
        _write(os.path.join(self.base, "Horimiya", "Horimiya - 001.mp4"), self.big)
        self.assertEqual(self._states()[1], "ok")

    def test_slug_adlandirmasi_ok(self):
        """Yeniden adlandırılmamış (rename=False) eski indirmeler de tanınmalı."""
        _write(os.path.join(self.base, "horimiya", "horimiya-2-bolum.mkv"), self.big)
        self.assertEqual(self._states()[2], "ok")

    def test_part_dosyasi_partial(self):
        _write(os.path.join(self.base, "horimiya", "horimiya-3-bolum.mp4.part"), self.big)
        self.assertEqual(self._states()[3], "partial")

    def test_esik_alti_dosya_partial(self):
        _write(os.path.join(self.base, "Horimiya", "Horimiya - 004.mp4"), 10)
        self.assertEqual(self._states()[4], "partial")

    def test_ytdl_ve_frag_dosyalari_partial(self):
        _write(os.path.join(self.base, "horimiya", "horimiya-1-bolum.mp4.ytdl"), 500)
        _write(os.path.join(self.base, "horimiya", "horimiya-2-bolum.mp4.part-Frag7"), 500)
        states = self._states()
        self.assertEqual(states[1], "partial")
        self.assertEqual(states[2], "partial")

    def test_tam_dosya_ve_part_birlikte_partial(self):
        """Bugünkü gerçek durum: 'Horimiya - 001.mp4' + slug tabanlı '.part'.

        Windows'ta 'Horimiya' ve 'horimiya' AYNI klasördür; yarım dosya varsa
        bölüm 'ok' değil 'partial' sayılmalı.
        """
        _write(os.path.join(self.base, "Horimiya", "Horimiya - 001.mp4"), self.big)
        _write(os.path.join(self.base, "Horimiya", "horimiya-1-bolum.mp4.part"), self.big)
        self.assertEqual(self._states()[1], "partial")

    def test_cakisma_eki_olan_dosya_ok(self):
        _write(os.path.join(self.base, "Horimiya", "Horimiya - 002 (2).mp4"), self.big)
        self.assertEqual(self._states()[2], "ok")

    def test_yt_dlp_format_eki_ok(self):
        _write(os.path.join(self.base, "horimiya", "horimiya-3-bolum.f137.mp4"), self.big)
        self.assertEqual(self._states()[3], "ok")

    def test_benzer_numaralar_karismaz(self):
        """'Horimiya - 001' öneki 'Horimiya - 0011' dosyasını yakalamamalı."""
        bolumler = [_FakeBolum("show-1-bolum", "1"), _FakeBolum("show-11-bolum", "11")]
        anime = _FakeAnime("show", "Show", bolumler)
        _write(os.path.join(self.base, "Show", "Show - 011.mp4"), self.big)
        states = {s["episode"]: s["status"]
                  for s in tm._library_states(anime, self.base, "Show")}
        self.assertEqual(states, {1: "missing", 11: "ok"})

    def test_alt_kume_taramasi_gercek_index_verir(self):
        """bolumler alt kümesi verilse de index tam listedeki sıradır."""
        secili = [self.anime.bolumler[2]]
        states = tm._library_states(self.anime, self.base, "Horimiya", secili)
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["index"], 2)
        self.assertEqual(states[0]["episode"], 3)


class TempSuffixTest(unittest.TestCase):
    def test_strip(self):
        self.assertEqual(tm._strip_temp_suffix("a.mp4.part"), ("a.mp4", True))
        self.assertEqual(tm._strip_temp_suffix("a.mp4.ytdl"), ("a.mp4", True))
        self.assertEqual(tm._strip_temp_suffix("a.mp4.part-Frag12"), ("a.mp4", True))
        self.assertEqual(tm._strip_temp_suffix("a.mp4"), ("a.mp4", False))
        self.assertEqual(tm._strip_temp_suffix("a.partial.mp4"), ("a.partial.mp4", False))


if __name__ == "__main__":
    unittest.main()
