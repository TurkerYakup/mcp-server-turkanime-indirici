"""search_anime dayanıklılığı ve AnimeDepo fallback'i için birim testler.

Ağ gerektirmez: sağlayıcı çağrıları sahtelerle değiştirilir.

Çalıştırmak için:  python -m unittest discover -s turkanime-mcp/tests -t turkanime-mcp/tests
"""

import os
import sys
import copy
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import turkanime_mcp as tm  # noqa: E402

_MANIFEST = {
    "providers": {
        "turkanime": {"enabled": True, "min_client_version": "9.2.2", "priority": 20},
        "animedepo": {"enabled": True, "min_client_version": "10.0.0", "priority": 10},
    },
    "features": {"search": {"provider": "turkanime",
                            "fallback_provider": "animedepo",
                            "force_fallback": False}},
    "messages": {"turkanime_offline": "TürkAnime şu anda kullanılamıyor, AnimeDepo kullanılacak."},
}


class _FakeTa:
    """turkanime_api.Anime.arama_yap sahtesi."""

    def __init__(self, senaryo):
        self.senaryo = senaryo  # list: sonuç listesi ya da Exception
        self.calls = 0
        parent = self

        class Anime:
            @staticmethod
            def arama_yap(query):
                adim = parent.senaryo[min(parent.calls, len(parent.senaryo) - 1)]
                parent.calls += 1
                if isinstance(adim, Exception):
                    raise adim
                return adim

        self.Anime = Anime


class SearchTest(unittest.TestCase):
    def setUp(self):
        # Manifest'i sabitle (depodaki dosyaya bağımlı olma).
        # deepcopy şart: testler features/providers gibi İÇ dict'leri değiştiriyor;
        # sığ kopya paylaşılan sabiti kirletir ve testler birbirini bozar.
        tm._MANIFEST_CACHE = copy.deepcopy(_MANIFEST)
        self.addCleanup(setattr, tm, "_MANIFEST_CACHE", None)

        # Backoff'u sıfırla: testler beklemesin.
        eski_backoff = tm._SEARCH_BACKOFF_SECS
        tm._SEARCH_BACKOFF_SECS = 0.0
        self.addCleanup(setattr, tm, "_SEARCH_BACKOFF_SECS", eski_backoff)

        self.addCleanup(setattr, tm, "_get_ta", tm._get_ta)
        self.addCleanup(setattr, tm, "_search_animedepo", tm._search_animedepo)

    def _ta_kur(self, *senaryo):
        fake = _FakeTa(list(senaryo))
        tm._get_ta = lambda: fake
        return fake

    def _depo_kur(self, sonuc=None, hata=None):
        def sahte(query):
            if hata:
                raise hata
            return sonuc or []
        tm._search_animedepo = sahte

    def _ara(self, q="one piece"):
        fn = getattr(tm.search_anime, "fn", tm.search_anime)
        return fn(q)

    # --- başarı ---------------------------------------------------------
    def test_sonuc_bulununca_turkanime_doner(self):
        self._ta_kur([("one-piece", "One Piece")])
        r = self._ara()
        self.assertEqual(r["provider"], "turkanime")
        self.assertEqual(r["results"], [{"slug": "one-piece", "title": "One Piece"}])
        self.assertNotIn("error", r)

    def test_ilk_deneme_hata_ikinci_basarili(self):
        """Anlık hata retry ile toparlanmalı; fallback'e düşmemeli."""
        fake = self._ta_kur(RuntimeError("timeout"), [("one-piece", "One Piece")])
        r = self._ara()
        self.assertEqual(r["provider"], "turkanime")
        self.assertEqual(fake.calls, 2)

    def test_ilk_deneme_bos_ikinci_dolu(self):
        """Site anlık takılıp boş dönerse yeniden denenmeli."""
        fake = self._ta_kur([], [("one-piece", "One Piece")])
        r = self._ara()
        self.assertEqual(fake.calls, 2)
        self.assertEqual(len(r["results"]), 1)

    # --- eşleşme yok vs erişilemedi -------------------------------------
    def test_gercekten_bos_ise_not_doner(self):
        """Eşleşme yok: hata DEĞİL, boş results + note."""
        self._ta_kur([], [])
        self._depo_kur(sonuc=[])
        r = self._ara("zzzzz")
        self.assertEqual(r["results"], [])
        self.assertIn("Eşleşme yok", r["note"])
        self.assertNotIn("error", r)

    def test_erisilemezse_retryable_hata(self):
        self._ta_kur(RuntimeError("curl 77"), RuntimeError("curl 77"))
        self._depo_kur(hata=RuntimeError("gitlab down"))
        r = self._ara()
        self.assertTrue(r["retryable"])
        self.assertIn("ulaşılamadı", r["error"])

    def test_bos_query(self):
        r = self._ara("   ")
        self.assertEqual(r["results"], [])
        self.assertIn("boş", r["note"])

    # --- fallback -------------------------------------------------------
    def test_turkanime_hata_verince_animedepo_kullanilir(self):
        self._ta_kur(RuntimeError("down"), RuntimeError("down"))
        self._depo_kur(sonuc=[("one-piece", "One Piece")])
        r = self._ara()
        self.assertEqual(r["provider"], "animedepo")
        self.assertEqual(len(r["results"]), 1)
        self.assertIn("AnimeDepo", r["note"])

    def test_force_fallback_turkanimeyi_atlar(self):
        tm._MANIFEST_CACHE["features"]["search"]["force_fallback"] = True
        fake = self._ta_kur([("one-piece", "One Piece")])
        self._depo_kur(sonuc=[("one-piece", "One Piece")])
        r = self._ara()
        self.assertEqual(r["provider"], "animedepo")
        self.assertEqual(fake.calls, 0, "force_fallback iken TürkAnime denenmemeli")

    def test_turkanime_disabled_ise_animedepo(self):
        tm._MANIFEST_CACHE["providers"]["turkanime"]["enabled"] = False
        fake = self._ta_kur([("one-piece", "One Piece")])
        self._depo_kur(sonuc=[("one-piece", "One Piece")])
        r = self._ara()
        self.assertEqual(r["provider"], "animedepo")
        self.assertEqual(fake.calls, 0)

    def test_fallback_tanimsizsa_hata_doner(self):
        tm._MANIFEST_CACHE["features"]["search"].pop("fallback_provider")
        self._ta_kur(RuntimeError("down"), RuntimeError("down"))
        r = self._ara()
        self.assertTrue(r["retryable"])

    def test_animedepo_da_bos_ise_eslesme_yok(self):
        """TürkAnime boş + AnimeDepo boş = eşleşme yok (hata değil)."""
        self._ta_kur([], [])
        self._depo_kur(sonuc=[])
        r = self._ara("zzzz")
        self.assertNotIn("error", r)
        self.assertIn("Eşleşme yok", r["note"])

    def test_hicbir_saglayici_yoksa(self):
        tm._MANIFEST_CACHE["providers"]["turkanime"]["enabled"] = False
        tm._MANIFEST_CACHE["providers"]["animedepo"]["enabled"] = False
        r = self._ara()
        self.assertIn("sağlayıcı", r["error"])
        self.assertFalse(r["retryable"])


class ProviderAllowedTest(unittest.TestCase):
    def setUp(self):
        # deepcopy şart: testler features/providers gibi İÇ dict'leri değiştiriyor;
        # sığ kopya paylaşılan sabiti kirletir ve testler birbirini bozar.
        tm._MANIFEST_CACHE = copy.deepcopy(_MANIFEST)
        self.addCleanup(setattr, tm, "_MANIFEST_CACHE", None)
        self.addCleanup(setattr, tm, "_client_version", tm._client_version)

    def test_min_surum_saglanmiyorsa_kapali(self):
        tm._client_version = lambda: (9, 0, 0)
        self.assertFalse(tm._provider_allowed("animedepo"))  # 10.0.0 ister
        self.assertFalse(tm._provider_allowed("turkanime"))  # 9.2.2 ister

    def test_min_surum_saglaniyorsa_acik(self):
        tm._client_version = lambda: (10, 0, 4)
        self.assertTrue(tm._provider_allowed("animedepo"))
        self.assertTrue(tm._provider_allowed("turkanime"))

    def test_surum_okunamazsa_engellemez(self):
        tm._client_version = lambda: None
        self.assertTrue(tm._provider_allowed("animedepo"))

    def test_tanimsiz_saglayici_engellenmez(self):
        self.assertTrue(tm._provider_allowed("bilinmeyen"))


class ManifestTest(unittest.TestCase):
    def test_depodaki_manifest_okunur(self):
        """Depodaki gerçek manifest.json bulunmalı ve fallback tanımlı olmalı."""
        tm._MANIFEST_CACHE = None
        self.addCleanup(setattr, tm, "_MANIFEST_CACHE", None)
        mf = tm._manifest()
        self.assertEqual(
            (mf.get("features") or {}).get("search", {}).get("fallback_provider"),
            "animedepo")


if __name__ == "__main__":
    unittest.main()