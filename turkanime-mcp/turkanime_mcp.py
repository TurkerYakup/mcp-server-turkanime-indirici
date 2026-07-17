"""TürkAnime İndirici MCP Sunucusu (stdio).

`turkanime-cli` (KebabLord/turkanime-indirici) paketini saran, Claude Desktop
için stdio tabanlı bir MCP sunucusu. Dört araç sunar:

    search_anime(query)                    -> anime ara
    list_episodes(anime_slug)              -> bölümleri listele
    download_episodes(anime_slug, ...)     -> arka planda indir (hemen döner)
    download_status(job_id=None)           -> indirme durumunu sorgula

Kurulu `turkanime_api` paketini import ederek kullanır; depoyu vendorlamaz.
Sadece kişisel kullanım içindir; sadece mevcut kütüphaneyi sarar.

Not: MCP protokolü stdout'u kullanır — bu yüzden tüm log'lar stderr'e yazılır.
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import shutil
import logging
import threading
from uuid import uuid4
from typing import Any, Optional, Union
from concurrent.futures import ThreadPoolExecutor

# --------------------------------------------------------------------------- #
# Loglama — MUTLAKA stderr'e. stdout MCP protokolüne ait, kirletilmemeli.
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] turkanime-mcp: %(message)s",
)
log = logging.getLogger("turkanime-mcp")


def _ensure_ca_bundle() -> None:
    """curl_cffi (libcurl) için ASCII bir CA sertifika yolu garantiler.

    Windows'ta kullanıcı adı Türkçe/ASCII-dışı karakter içeriyorsa
    (örn. 'C:\\Users\\Türker Yakup\\...'), libcurl certifi'nin cacert.pem
    dosyasını AÇAMAZ ve tüm ağ çağrıları SSL hatası (curl 77) verir.
    Çözüm: sertifikayı ASCII bir yola kopyala ve CURL_CA_BUNDLE'a işaret et.
    """
    try:
        if os.environ.get("CURL_CA_BUNDLE") and os.path.exists(os.environ["CURL_CA_BUNDLE"]):
            return
        import certifi
        ca = certifi.where()
        if ca.isascii() and os.path.exists(ca):
            os.environ.setdefault("CURL_CA_BUNDLE", ca)
            os.environ.setdefault("SSL_CERT_FILE", ca)
            return
        # ASCII-dışı yol: sertifikayı ASCII bir konuma kopyala
        import shutil
        candidates = [
            os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "turkanime-mcp"),
            os.path.dirname(os.path.abspath(__file__)),
        ]
        for base in candidates:
            try:
                os.makedirs(base, exist_ok=True)
                target = os.path.join(base, "cacert.pem")
                if not target.isascii():
                    continue
                if not os.path.exists(target) or os.path.getsize(target) != os.path.getsize(ca):
                    shutil.copy(ca, target)
                os.environ["CURL_CA_BUNDLE"] = target
                os.environ["SSL_CERT_FILE"] = target
                log.info("CA bundle ASCII yola ayarlandı: %s", target)
                return
            except Exception:
                continue
        log.warning("ASCII CA bundle konumu bulunamadı; SSL hataları olabilir.")
    except Exception as exc:  # pragma: no cover
        log.warning("CA bundle ayarlanamadı: %s", exc)


# turkanime_api / curl_cffi import edilmeden ÖNCE çalışmalı
_ensure_ca_bundle()

try:
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # pragma: no cover
    log.error("MCP SDK import edilemedi: %s", exc)
    log.error("Kurulum: pip install -r requirements.txt "
              "(Windows'ta 'pywin32' de gereklidir)")
    raise

# turkanime_api tembel (lazy) import edilir; böylece paket kurulu değilse bile
# sunucu ayağa kalkar ve araçlar anlamlı bir hata mesajı döndürür.
_ta = None
_ta_import_error: Optional[str] = None


def _get_ta():
    """Kurulu turkanime_api modülünü döndürür (tek seferlik, tembel import)."""
    global _ta, _ta_import_error
    if _ta is not None:
        return _ta
    if _ta_import_error is not None:
        raise RuntimeError(_ta_import_error)
    try:
        import turkanime_api as ta  # noqa: WPS433 (bilinçli lazy import)
        _ta = ta
        return _ta
    except Exception as exc:  # pragma: no cover
        _ta_import_error = (
            "turkanime_api paketi yüklenemedi (%s). "
            "Kurulum: pip install turkanime-cli" % exc
        )
        raise RuntimeError(_ta_import_error)


# --------------------------------------------------------------------------- #
# Global durum: iş havuzu, iş sözlüğü, anime önbelleği
# --------------------------------------------------------------------------- #
# İş havuzu boyutu (thread üst sınırı). Gerçek eşzamanlılık aşağıdaki dinamik
# kapı ile sınırlanır; her indirme çağrısı kendi paralel sayısını seçebilir.
_WORKER_POOL_SIZE = max(1, int(os.environ.get("TURKANIME_WORKER_POOL", "8")))
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKER_POOL_SIZE, thread_name_prefix="ta-dl")

# Eşzamanlı indirme VARSAYILANI: tool çağrısında `max_workers` verilmezse bu
# kullanılır. Varsayılan 1 = tek tek indir. Config'de TURKANIME_MAX_WORKERS ile
# değiştirilebilir (örn. "3").
_DEFAULT_PARALLEL = max(1, int(os.environ.get("TURKANIME_MAX_WORKERS", "1")))

# Dinamik eşzamanlılık kapısı: _parallel_limit çalışırken değişebilir; her
# indirme çağrısı bunu kendi max_workers'ına ayarlar. İndirme işleri slot alır.
_PARALLEL_COND = threading.Condition()
_parallel_limit = _DEFAULT_PARALLEL
_parallel_active = 0


def _set_parallel_limit(n: Optional[int]) -> int:
    """Eşzamanlı indirme sınırını ayarlar (1.._WORKER_POOL_SIZE). None → varsayılan."""
    global _parallel_limit
    target = _DEFAULT_PARALLEL if n is None else int(n)
    target = max(1, min(target, _WORKER_POOL_SIZE))
    with _PARALLEL_COND:
        _parallel_limit = target
        _PARALLEL_COND.notify_all()
    return target


def _acquire_slot(jid: str) -> bool:
    """Dinamik limite göre indirme slotu al; limit doluysa bekler.

    İptal edilirse slot almadan False döner (iş 'cancelled' işaretlenmeli).
    """
    global _parallel_active
    with _PARALLEL_COND:
        while _parallel_active >= _parallel_limit:
            if _is_cancelled(jid):
                return False
            _PARALLEL_COND.wait(timeout=0.5)
        _parallel_active += 1
        return True


def _release_slot() -> None:
    """İndirme slotunu bırak ve bekleyenleri uyandır."""
    global _parallel_active
    with _PARALLEL_COND:
        if _parallel_active > 0:
            _parallel_active -= 1
        _PARALLEL_COND.notify_all()

# Varsayılan indirme klasörü — kullanıcıya özeldir, Claude Desktop config'inde
# env -> TURKANIME_OUTPUT_DIR ile ayarlanır. Böylece kullanıcı "masaüstü/anime"
# ya da "D:\Anime" gibi kendi kök klasörünü belirler; her indirmede tekrar
# yazmasına gerek kalmaz.
_DEFAULT_OUTPUT_DIR = os.environ.get("TURKANIME_OUTPUT_DIR", "").strip()

# "Tüm bölümler" için kabul edilen anahtar sözcükler
_ALL_TOKENS = {"all", "hepsi", "tümü", "tumu", "tum", "*", "sezon", "season"}

# Bir bölüm için denenecek maksimum farklı kaynak (player) sayısı. İlk kaynağın
# akışı yarıda keserse (imzalı URL süresi dolması, CDN bağlantı kesmesi vb.),
# başarısız player hariç tutulup farklı bir kaynakla yeniden denenir. Her
# başarısız deneme bant genişliği harcadığından makul bir üst sınır tutulur.
_MAX_SOURCE_ATTEMPTS = max(1, int(os.environ.get("TURKANIME_SOURCE_ATTEMPTS", "3")))

# Bir dosyanın "gerçek bir bölüm" sayılması için gereken en küçük boyut. Bunun
# altındaki son dosyalar (0-byte kalıntılar, yarım bırakılmış parçalar) `partial`
# kabul edilir. verify_library ve skip_existing bu eşiği kullanır.
_MIN_VALID_BYTES = max(0, int(os.environ.get("TURKANIME_MIN_VALID_BYTES", str(1024 * 1024))))

# Bir kaynak akışı yarıda kestiğinde AYNI player'da kaldığı yerden devam etmek
# için yapılacak deneme sayısı. Her denemeden sonra `.part` büyümediyse devam
# etmenin faydası yok — partial temizlenip farklı kaynağa geçilir. 0 = resume
# kapalı (her zaman baştan indir).
_RESUME_ATTEMPTS = max(0, int(os.environ.get("TURKANIME_RESUME_ATTEMPTS", "1")))

# Arama denemesi sayısı ve backoff temeli (0.5s, 1.0s, ...). `arama_yap` site
# anlık takıldığında boş liste dönebildiğinden boş sonuç da yeniden denenir;
# upstream rate limit'e takılabildiği için deneme sayısı düşük tutulur.
_SEARCH_RETRIES = max(1, int(os.environ.get("TURKANIME_SEARCH_RETRIES", "2")))
_SEARCH_BACKOFF_SECS = max(0.0, float(os.environ.get("TURKANIME_SEARCH_BACKOFF", "0.5")))

# job_id -> iş bilgisi dict'i
JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()

# slug -> Anime nesnesi önbelleği (fetch_info çağrılmış halde)
_ANIME_CACHE: dict[str, Any] = {}
_ANIME_LOCK = threading.Lock()

# İşin tamamlandığı sayılan durumlar (temizleme/özet için).
# "interrupted": sunucu yeniden başlatıldığında yarıda kalmış işler bu duruma
# alınır — thread'leri artık yok, yani devam eden bir iş DEĞİL.
_DONE_STATUSES = {"finished", "error", "cancelled", "interrupted"}

# Restart sonrası "hâlâ iniyor" yalanını önlemek için: bu durumlardaki işler
# yüklenirken "interrupted" işaretlenir.
_INTERRUPTED_MSG = "Sunucu yeniden başlatıldı; iş yarıda kesildi."


# --------------------------------------------------------------------------- #
# Kalıcı durum: JOBS yalnızca RAM'de olduğundan restart'ta iş geçmişi kaybolurdu.
# jobs.json'a yazılır; açılışta geri yüklenir.
# --------------------------------------------------------------------------- #
def _default_state_dir() -> str:
    """Kalıcı durum klasörü: TURKANIME_STATE_DIR > %PROGRAMDATA% > ~/.turkanime-mcp."""
    env = os.environ.get("TURKANIME_STATE_DIR", "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    programdata = os.environ.get("PROGRAMDATA", "").strip()
    if programdata and os.path.isdir(programdata):
        return os.path.join(programdata, "turkanime-mcp")
    return os.path.join(os.path.expanduser("~"), ".turkanime-mcp")


_STATE_DIR = _default_state_dir()
_STATE_FILE = os.path.join(_STATE_DIR, "jobs.json")

# Disk'i dövmemek için debounce: `status` değişimi ANINDA yazılır, ara ilerleme
# (percent/speed) güncellemeleri en fazla bu aralıkta bir flush edilir.
_STATE_FLUSH_SECS = max(0.0, float(os.environ.get("TURKANIME_STATE_FLUSH_SECS", "1.0")))

# _STATE_LOCK bir YAPRAK kilittir: yalnızca dosya G/Ç'sini ve aşağıdaki iki
# sayacı korur. Bu kilit tutulurken ASLA _JOBS_LOCK alınmaz (deadlock önlemi).
_STATE_LOCK = threading.Lock()
_state_last_flush = 0.0   # _STATE_LOCK altında
_state_written_seq = 0    # _STATE_LOCK altında — eski snapshot'ın yenisini ezmesini önler
_state_seq = 0            # _JOBS_LOCK altında — her değişiklikte artar


def _bump_state_seq_locked() -> None:
    """Durum sıra numarasını artırır. ÇAĞIRAN _JOBS_LOCK'u tutuyor olmalı."""
    global _state_seq
    _state_seq += 1


def _snapshot_jobs() -> tuple[dict, int]:
    """JOBS'un JSON'a yazılabilir kopyasını + sıra numarasını döndürür."""
    with _JOBS_LOCK:
        return {jid: dict(job) for jid, job in JOBS.items()}, _state_seq


def _write_state_file(jobs: dict, seq: int) -> None:
    """jobs.json'ı atomik yazar (temp + os.replace). Eski snapshot'ı atlar."""
    global _state_written_seq
    with _STATE_LOCK:
        if seq < _state_written_seq:
            return  # daha yeni bir snapshot zaten yazılmış
        payload = {"version": 1, "jobs": jobs}
        try:
            os.makedirs(_STATE_DIR, exist_ok=True)
            tmp = _STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, _STATE_FILE)
            _state_written_seq = seq
        except Exception as exc:  # pragma: no cover — durum kaydı indirmeyi düşürmemeli
            log.warning("İş durumu kaydedilemedi (%s): %s", _STATE_FILE, exc)


def _persist_jobs(force: bool = False) -> None:
    """İş durumunu diske yazar. force=False ise debounce aralığına uyar."""
    global _state_last_flush
    with _STATE_LOCK:
        now = time.monotonic()
        if not force and (now - _state_last_flush) < _STATE_FLUSH_SECS:
            return  # ara ilerleme güncellemesi — atlanabilir, kayıp önemsiz
        _state_last_flush = now
    jobs, seq = _snapshot_jobs()
    _write_state_file(jobs, seq)


def _load_state() -> int:
    """jobs.json'ı JOBS'a yükler; yarıda kalan işleri 'interrupted' işaretler.

    Restart sonrası thread'ler yok olduğundan `downloading`/`queued`/
    `kaynak_araniyor` gibi durumlar YALAN olur; bunlar interrupted'a çevrilir.
    Returns: yüklenen iş sayısı.
    """
    try:
        if not os.path.exists(_STATE_FILE):
            return 0
        with open(_STATE_FILE, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        saved = (payload or {}).get("jobs") or {}
    except Exception as exc:
        log.warning("İş durumu okunamadı (%s): %s", _STATE_FILE, exc)
        return 0

    kesilen = 0
    with _JOBS_LOCK:
        for jid, job in saved.items():
            if not isinstance(job, dict):
                continue
            if job.get("status") not in _DONE_STATUSES:
                job["status"] = "interrupted"
                job["error"] = job.get("error") or _INTERRUPTED_MSG
                kesilen += 1
            job["cancel"] = False  # thread yok; bayrak anlamsız
            job["restored"] = True
            JOBS[jid] = job
        yuklenen = len(saved)
    if yuklenen:
        log.info("Önceki oturumdan %d iş yüklendi (%d tanesi yarıda kesilmiş).",
                 yuklenen, kesilen)
    return yuklenen


class _Cancelled(Exception):
    """İptal edilen indirmeyi normal hatadan ayırmak için işaret exception'ı."""


def _is_cancelled(jid: str) -> bool:
    """İş için iptal istenmiş mi (thread-güvenli)."""
    with _JOBS_LOCK:
        job = JOBS.get(jid)
        return bool(job and job.get("cancel"))


# --------------------------------------------------------------------------- #
# Manifest: sağlayıcı/özellik kararları (yerel manifest.json'dan okunur).
# Upstream CLI bunu uzaktan çeker; MCP her aramada ağ çağrısı yapmamak için
# depodaki kopyayı okur. TURKANIME_MANIFEST ile başka bir yol verilebilir.
# --------------------------------------------------------------------------- #
_MANIFEST_CACHE: Optional[dict] = None
_MANIFEST_LOCK = threading.Lock()


def _manifest_candidates() -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    return [
        os.environ.get("TURKANIME_MANIFEST", "").strip(),
        os.path.join(here, "manifest.json"),
        os.path.join(os.path.dirname(here), "manifest.json"),  # depo kökü
    ]


def _manifest() -> dict:
    """Yerel manifest.json (tek seferlik okunur). Yoksa boş dict."""
    global _MANIFEST_CACHE
    with _MANIFEST_LOCK:
        if _MANIFEST_CACHE is not None:
            return _MANIFEST_CACHE
        data: dict = {}
        for path in _manifest_candidates():
            if not path or not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh) or {}
                log.info("Manifest okundu: %s", path)
                break
            except Exception as exc:
                log.warning("Manifest okunamadı (%s): %s", path, exc)
        _MANIFEST_CACHE = data
        return data


def _client_version() -> Optional[tuple]:
    """Kurulu turkanime-cli sürümü (min_client_version kontrolü için)."""
    try:
        from turkanime_api.cli.version import __version__
        return tuple(int(i) for i in str(__version__).split("."))
    except Exception:
        pass
    try:
        import importlib.metadata as md
        return tuple(int(i) for i in md.version("turkanime-cli").split("."))
    except Exception:
        return None


def _provider_allowed(name: str) -> bool:
    """Manifest'e göre sağlayıcı kullanılabilir mi (enabled + min_client_version)."""
    conf = (_manifest().get("providers") or {}).get(name)
    if conf is None:
        return True  # manifest/sağlayıcı tanımlı değilse engelleme
    if not conf.get("enabled", True):
        return False
    gereken = str(conf.get("min_client_version", "") or "").lower().lstrip("v")
    if not gereken:
        return True
    mevcut = _client_version()
    if mevcut is None:
        return True  # sürüm okunamıyorsa engelleme (best-effort)
    try:
        return mevcut >= tuple(int(i) for i in gereken.split("."))
    except Exception:
        return True


def _search_feature() -> dict:
    return (_manifest().get("features") or {}).get("search") or {}


mcp = FastMCP("turkanime")


# --------------------------------------------------------------------------- #
# Yardımcılar
# --------------------------------------------------------------------------- #
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|]')


def _sanitize(name: str) -> str:
    """Windows'ta geçersiz karakterleri temizler."""
    name = _INVALID_CHARS.sub("", name or "")
    name = name.strip().rstrip(".")
    return name or "anime"


def _job_set(jid: str, **fields: Any) -> None:
    """İş sözlüğünü thread-güvenli günceller ve durumu kalıcılaştırır.

    `status` değişimi anında flush edilir (restart sonrası doğru durum için);
    salt ilerleme (percent/speed/eta) güncellemeleri debounce'a tabidir.
    """
    global _state_seq
    with _JOBS_LOCK:
        job = JOBS.get(jid)
        if job is None:
            return
        status_degisti = "status" in fields and fields["status"] != job.get("status")
        job.update(fields)
        _state_seq += 1
    _persist_jobs(force=status_degisti)


def _get_anime(slug: str, refresh: bool = False):
    """Anime nesnesini kurar, fetch_info() çağırır ve önbelleğe alır.

    fetch_info() şarttır: aksi halde anime_id boş kalır ve bölüm listesi
    gelmez (bkz. dokümantasyon §7.1).

    refresh=True ise önbellek atlanır ve site yeniden çekilir (yeni yayınlanan
    bölümleri görmek için — aksi halde yeniden başlatana kadar eski liste döner).
    """
    slug = (slug or "").strip()
    if not slug:
        raise ValueError("anime_slug boş olamaz.")
    if not refresh:
        with _ANIME_LOCK:
            if slug in _ANIME_CACHE:
                return _ANIME_CACHE[slug]
    ta = _get_ta()
    anime = ta.Anime(slug)
    anime.fetch_info()  # info + anime_id doldurur; bölüm listesi için ŞART
    with _ANIME_LOCK:
        _ANIME_CACHE[slug] = anime
    return anime


def _resolve_base_dir(output_dir: Optional[str]) -> str:
    """İndirme kök klasörünü çözer: parametre > config varsayılanı.

    Ne output_dir ne de TURKANIME_OUTPUT_DIR verilmişse anlamlı bir hata verir.
    """
    base = (output_dir or "").strip() or _DEFAULT_OUTPUT_DIR
    if not base:
        raise ValueError(
            "İndirme klasörü belirtilmemiş. Ya `output_dir` parametresi verin ya da "
            "Claude Desktop config'inde bu sunucuya "
            "env -> \"TURKANIME_OUTPUT_DIR\": \"<kök klasör>\" ekleyin."
        )
    return os.path.abspath(os.path.expanduser(base))


def _episode_number(bolum: Any, fallback: int) -> str:
    """Bölüm slug/başlığından okunur bir bölüm numarası çıkarır (3 haneli)."""
    text = f"{getattr(bolum, 'slug', '') or ''} {getattr(bolum, 'title', '') or ''}".lower()
    m = re.search(r"(\d+)\D*bolum", text) or re.search(r"(\d+)", text)
    num = int(m.group(1)) if m else (fallback + 1)
    return f"{num:03d}"


def _resolve_bolumler(anime: Any, episodes: Union[int, str, list]) -> list:
    """`episodes` parametresini Bolum nesnelerine çözer.

    Kabul edilen biçimler (index'ler list_episodes'daki 0-tabanlı `index`
    ile aynıdır):
      - tek index (int/str):      3  ya da  "3"
      - bölüm slug'ı:             "one-piece-3-bolum"
      - aralık string'i:          "0-11"   (ilk 12 bölüm)
      - virgüllü liste:           "0,1,2,5-8"
      - liste:                    [0, 1, "one-piece-3-bolum"]
    """
    bolumler = anime.bolumler  # list[Bolum]; ilk erişimde get_bolum_listesi() çağrılır
    n = len(bolumler)
    if n == 0:
        raise RuntimeError("Bu anime için bölüm bulunamadı.")

    # "all"/"hepsi"/"tümü" -> tüm bölümler (tüm sezonu indir)
    if isinstance(episodes, str) and episodes.strip().lower() in _ALL_TOKENS:
        return list(bolumler)

    slug_map = {b.slug: b for b in bolumler}

    # Girdiyi düz token listesine indirge
    if isinstance(episodes, (int,)):
        tokens: list = [episodes]
    elif isinstance(episodes, str):
        tokens = [t.strip() for t in episodes.split(",") if t.strip()]
    elif isinstance(episodes, (list, tuple)):
        tokens = list(episodes)
    else:
        raise ValueError("episodes tipi geçersiz (int/str/list bekleniyor).")

    selected: list = []
    seen: set = set()

    def _add_index(i: int) -> None:
        if 0 <= i < n:
            b = bolumler[i]
            if id(b) not in seen:
                seen.add(id(b))
                selected.append(b)
        else:
            raise ValueError(f"Bölüm index'i aralık dışı: {i} (0..{n - 1}).")

    def _add_slug(s: str) -> None:
        b = slug_map.get(s)
        if b is None:
            raise ValueError(f"Bölüm bulunamadı: '{s}'.")
        if id(b) not in seen:
            seen.add(id(b))
            selected.append(b)

    for tok in tokens:
        if isinstance(tok, int):
            _add_index(tok)
            continue
        tok = str(tok).strip()
        if not tok:
            continue
        parts = tok.split("-")
        # "X-Y" aralığı SADECE iki taraf da tam sayıysa (slug'lar da '-' içerir)
        if len(parts) == 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
            start, end = int(parts[0]), int(parts[1])
            if start > end:
                start, end = end, start
            for i in range(start, end + 1):
                _add_index(i)
        elif tok.isdigit():
            _add_index(int(tok))
        else:
            _add_slug(tok)

    if not selected:
        raise ValueError("Seçili bölüm yok. episodes parametresini kontrol edin.")
    return selected


def _finalize_file(
    jid: str, output_base: str, season_folder: str, bolum: Any, pos: int
) -> None:
    """İndirme bitince dosyayı okunur alt klasöre TAŞIR + yeniden adlandırır.

    Hedef: `<output_base>/<season_folder>/<season_folder> - <NNN>.<ext>`.
    yt-dlp önce dosyayı `<output_base>/<anime_slug>/<bolum_slug>.<ext>` altına
    indirir; burada onu düzenli sezon klasörüne taşıyıp boşalan geçici
    `anime_slug` klasörünü temizleriz. Best-effort: hata olursa işi düşürmez.
    """
    try:
        with _JOBS_LOCK:
            current = JOBS.get(jid, {}).get("file")
        if not current or not os.path.exists(current):
            return
        ext = os.path.splitext(current)[1] or ".mp4"
        num = _episode_number(bolum, pos)
        season_dir = os.path.join(output_base, season_folder)
        os.makedirs(season_dir, exist_ok=True)

        target = os.path.join(season_dir, f"{season_folder} - {num}{ext}")
        # Çakışma olursa üzerine YAZMA; " (2)" gibi ek ver.
        if os.path.abspath(target) != os.path.abspath(current) and os.path.exists(target):
            stem, e = os.path.splitext(target)
            k = 2
            while os.path.exists(f"{stem} ({k}){e}"):
                k += 1
            target = f"{stem} ({k}){e}"

        if os.path.abspath(target) != os.path.abspath(current):
            os.replace(current, target)  # aynı sürücüde çalışır
            _job_set(jid, file=target)

        # Boşaldıysa geçici anime_slug klasörünü kaldır (dolu ise sessizce geç).
        try:
            os.rmdir(os.path.dirname(current))
        except OSError:
            pass
    except Exception as exc:  # pragma: no cover
        log.warning("[%s] dosya taşınamadı/adlandırılamadı: %s", jid, exc)


def _partial_size(output_base: str, anime_slug: str, bolum: Any) -> int:
    """Bölümün yarım (.part/.ytdl) dosyalarının toplam boyutu (bayt).

    Resume'un işe yarayıp yaramadığını ölçmek için: iki deneme arasında bu değer
    artmıyorsa devam etmenin faydası yoktur, kaynak değiştirmek gerekir.
    """
    folder = os.path.join(output_base, anime_slug)
    if not os.path.isdir(folder):
        return 0
    prefix = getattr(bolum, "slug", "") or ""
    total = 0
    try:
        for name in os.listdir(folder):
            if prefix and not name.startswith(prefix):
                continue
            if name.endswith(".part") or name.endswith(".ytdl") or ".part-" in name:
                try:
                    total += os.path.getsize(os.path.join(folder, name))
                except OSError:
                    pass
    except OSError:
        return 0
    return total


def _cleanup_partials(output_base: str, anime_slug: str, bolum: Any) -> None:
    """İptal sonrası yarım kalan .part / fragment dosyalarını temizler (best-effort)."""
    try:
        folder = os.path.join(output_base, anime_slug)
        if not os.path.isdir(folder):
            return
        prefix = getattr(bolum, "slug", "")
        for name in os.listdir(folder):
            if prefix and name.startswith(prefix):
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass
        try:
            os.rmdir(folder)  # boşsa kaldır
        except OSError:
            pass
    except Exception:  # pragma: no cover
        pass


# --------------------------------------------------------------------------- #
# Kütüphane tarama (verify_library / skip_existing ortak mantığı)
# --------------------------------------------------------------------------- #
# yt-dlp'nin yarım dosya son ekleri: "<ad>.part", "<ad>.ytdl", "<ad>.part-Frag12"
_TEMP_SUFFIX_RE = re.compile(r"^(?P<base>.+?)\.(?:part|ytdl)(?:-Frag\d+)?$", re.IGNORECASE)


def _strip_temp_suffix(name: str) -> tuple[str, bool]:
    """('X.mp4.part') -> ('X.mp4', True); ('X.mp4') -> ('X.mp4', False)."""
    m = _TEMP_SUFFIX_RE.match(name)
    if m:
        return m.group("base"), True
    return name, False


def _scan_dirs(dirs: list[str]) -> list[tuple[str, str]]:
    """Verilen klasörleri tarayıp (klasör, dosya_adı) çiftleri döndürür.

    Aynı klasör iki kez taranmaz. Windows'ta dosya sistemi büyük/küçük harf
    duyarsız olduğundan `<kök>/Horimiya` ile `<kök>/horimiya` AYNI klasördür;
    normcase ile tekilleştirilir (aksi halde her dosya iki kez sayılırdı).
    """
    seen: set = set()
    out: list[tuple[str, str]] = []
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        key = os.path.normcase(os.path.abspath(d))
        if key in seen:
            continue
        seen.add(key)
        try:
            for name in os.listdir(d):
                out.append((d, name))
        except OSError:
            continue
    return out


def _file_belongs_to(stem: str, season_folder: str, num: str, bolum_slug: str) -> bool:
    """Uzantısız dosya adı bu bölüme mi ait?

    İki adlandırma da tanınır:
      - `_finalize_file` çıktısı:  "<season_folder> - NNN"  (ve " (2)" çakışma eki)
      - ham/yeniden adlandırılmamış: "<bolum_slug>" (yt-dlp format eki alabilir:
        "<bolum_slug>.f137")
    """
    if bolum_slug and (stem == bolum_slug or stem.startswith(bolum_slug + ".")):
        return True
    prefix = f"{season_folder} - {num}"
    return stem == prefix or stem.startswith(prefix + " (")


def _episode_status(files: list[tuple[str, str]], season_folder: str,
                    num: str, bolum_slug: str) -> tuple[str, list[str]]:
    """Bir bölümün disk durumunu belirler -> ("ok"|"partial"|"missing", [dosyalar]).

    - partial: `.part`/`.ytdl` var YA DA son dosya `_MIN_VALID_BYTES` altında.
    - ok: eşikten büyük bir son dosya var ve yarım dosya yok.
    - missing: eşleşen hiçbir dosya yok.
    """
    matched: list[str] = []
    has_partial = False
    has_final = False
    for folder, name in files:
        base, is_temp = _strip_temp_suffix(name)
        stem = os.path.splitext(base)[0]
        if not _file_belongs_to(stem, season_folder, num, bolum_slug):
            continue
        path = os.path.join(folder, name)
        matched.append(path)
        if is_temp:
            has_partial = True
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size >= _MIN_VALID_BYTES:
            has_final = True
        else:
            has_partial = True
    if has_partial:
        return "partial", matched
    if has_final:
        return "ok", matched
    return "missing", matched


def _library_states(anime: Any, output_base: str, season_folder: str,
                    bolumler: Optional[list] = None) -> list[dict]:
    """Bölümlerin disk durumunu çıkarır (verify_library + skip_existing ortak).

    `bolumler` None ise animenin tüm bölümleri taranır; verilirse yalnızca o
    alt küme. `index` daima animenin TAM listesindeki 0-tabanlı sırasıdır —
    böylece `_episode_number` fallback'i ve indirme çağrıları tutarlı kalır.

    Hem düzenlenmiş sezon klasörü hem de yt-dlp'nin ham indirme klasörü
    (`<output_base>/<anime_slug>`) taranır; kullanıcının eski manuel/yarım
    indirmeleri farklı adlandırılmış olabilir.
    """
    tumu = anime.bolumler
    idx_map = {id(b): i for i, b in enumerate(tumu)}
    hedef = tumu if bolumler is None else bolumler
    files = _scan_dirs([
        os.path.join(output_base, season_folder),
        os.path.join(output_base, getattr(anime, "slug", "") or ""),
    ])
    out: list[dict] = []
    for b in hedef:
        pos = idx_map.get(id(b), 0)
        num = _episode_number(b, pos)
        status, matched = _episode_status(files, season_folder, num,
                                          getattr(b, "slug", "") or "")
        out.append({
            "index": pos,
            "episode": int(num),
            "slug": getattr(b, "slug", None),
            "title": getattr(b, "title", None),
            "status": status,
            "files": matched,
        })
    return out


_SUPPORTED_CACHE: Optional[list] = None


def _supported_players() -> list:
    """turkanime_api'nin desteklenen player öncelik listesi (tembel yüklenir)."""
    global _SUPPORTED_CACHE
    if _SUPPORTED_CACHE is None:
        try:
            from turkanime_api.objects import SUPPORTED
            _SUPPORTED_CACHE = list(SUPPORTED)
        except Exception:  # pragma: no cover
            _SUPPORTED_CACHE = []
    return _SUPPORTED_CACHE


def _download_complete(output_base: str, anime_slug: str, bolum: Any) -> bool:
    """İndirme GERÇEKTEN tamamlandı mı diye diske bakar.

    Neden gerekli: upstream `indir()` yt-dlp'yi `ignoreerrors='only_download'`
    ile çağırır; indirme ortada kesilse bile hatasız döner. Bu yüzden akışın
    dönmesine güvenemeyiz — tamamlanma = bölüm slug'ıyla başlayan bir SON dosya
    var VE ortada `.part`/`.ytdl` (yarım) dosya YOK.
    """
    folder = os.path.join(output_base, anime_slug)
    if not os.path.isdir(folder):
        return False
    prefix = getattr(bolum, "slug", "") or ""
    has_final = False
    for name in os.listdir(folder):
        if prefix and not name.startswith(prefix):
            continue
        if name.endswith(".part") or name.endswith(".ytdl") or ".part-" in name:
            return False  # yarım dosya var → tamamlanmamış
        has_final = True
    return has_final


def _pick_video(bolum: Any, by_res: bool, by_fansub: Optional[str],
                exclude_players: set, callback) -> Any:
    """`best_video` benzeri kaynak seçimi; ama `exclude_players` içindeki
    player'ları atlar. Böylece yarıda kesen bir kaynağı retry'da eleyebiliriz.

    best_video her fansub için `SUPPORTED.index(player)`e göre sıraladığından
    hep aynı "en iyi" player'ı seçer; bu fonksiyon başarısız player'ı dışlayarak
    gerçek bir alternatif dener. Uygun video yoksa None döner.
    """
    supported = _supported_players()

    def _prio(v: Any):
        # BETA player'lar (ör. ALUCARD(BETA)) ~%70'te akışı kesme eğiliminde;
        # kararlı kaynaklar önce denensin, BETA yalnızca başka seçenek yoksa.
        is_beta = "(BETA)" in (v.player or "").upper()
        try:
            idx = supported.index(v.player)
        except ValueError:
            idx = len(supported)
        return (is_beta, bool(by_fansub) and v.fansub != by_fansub, idx)

    vids = [v for v in bolum.videos
            if getattr(v, "is_supported", False) and v.player not in exclude_players]
    vids = sorted(vids, key=_prio)

    working: list = []
    total = len(vids)
    for i, vid in enumerate(vids, start=1):
        callback({"current": i, "total": total, "player": vid.player,
                  "status": "üstbilgi çekiliyor"})
        try:
            if not vid.is_working:
                continue
        except Exception:
            continue
        working.append(vid)
        res = vid.resolution or 600
        if not by_res or res >= 1080:
            return vid
    if not working:
        return None
    return max(working, key=lambda x: x.resolution or 600)


def _download_task(
    jid: str,
    anime_slug: str,
    anime_title: str,
    bolum: Any,
    pos: int,
    output_base: str,
    season_folder: str,
    fansub: Optional[str],
    max_resolution: bool,
    rename: bool,
    resume: bool = True,
) -> None:
    """Tek bir bölümü indiren arka plan işi (thread havuzunda çalışır)."""
    slot_held = False
    try:
        # Kuyrukta beklerken iptal edildiyse hiç başlama.
        if _is_cancelled(jid):
            _job_set(jid, status="cancelled")
            log.info("[%s] başlamadan iptal edildi: %s", jid, bolum.slug)
            return

        # Eşzamanlılık kapısı: dinamik paralel limite göre sıraya gir.
        # (Slot alınana kadar status "queued" kalır.)
        if not _acquire_slot(jid):
            _job_set(jid, status="cancelled")
            _cleanup_partials(output_base, anime_slug, bolum)
            log.info("[%s] slot beklerken iptal edildi: %s", jid, bolum.slug)
            return
        slot_held = True

        def bv_callback(d: dict) -> None:
            # kaynak seçme ilerleme/durum bildirimi
            _job_set(
                jid,
                source_status=d.get("status"),
                source_current=d.get("current"),
                source_total=d.get("total"),
                player=d.get("player"),
            )

        def hook(d: dict) -> None:
            # İptal istendiyse hook'tan exception fırlat → yt-dlp indirmeyi durdurur.
            if _is_cancelled(jid):
                raise _Cancelled()
            # yt-dlp progress_hooks callback'i
            _job_set(
                jid,
                status=d.get("status"),
                percent=(d.get("_percent_str") or "").strip() or None,
                speed=(d.get("_speed_str") or "").strip() or None,
                eta=d.get("eta"),
                file=d.get("filename"),
            )

        # ÖNCEKİ oturumdan/işten kalmış yarım dosyaları sil. yt-dlp'nin
        # `continuedl` varsayılanı True olduğundan, farklı bir player'dan kalmış
        # bir `.part` sessizce "devam" ettirilir ve BOZUK dosya üretirdi (outtmpl
        # bölüm bazlı, kaynak bazlı değil). Resume yalnızca aynı player'da,
        # aşağıdaki döngü içinde güvenlidir.
        _cleanup_partials(output_base, anime_slug, bolum)

        # Kaynak akışı yarıda kesilebildiği için birden fazla player denenir;
        # başarısız olan player sonraki denemede hariç tutulur.
        tried_players: list = []
        exclude: set = set()
        last_error: Optional[str] = None

        for attempt in range(1, _MAX_SOURCE_ATTEMPTS + 1):
            if _is_cancelled(jid):
                _job_set(jid, status="cancelled")
                _cleanup_partials(output_base, anime_slug, bolum)
                log.info("[%s] iptal edildi: %s", jid, bolum.slug)
                return

            _job_set(jid, status="kaynak_araniyor")
            try:
                video = _pick_video(bolum, max_resolution, fansub, exclude, bv_callback)
            except Exception as exc:
                last_error = f"kaynak seçilemedi: {exc}"
                log.warning("[%s] %s", jid, last_error)
                break

            if video is None:
                last_error = "Çalışan (kalan) kaynak bulunamadı."
                log.info("[%s] deneme %d: çalışan kaynak yok: %s", jid, attempt, bolum.slug)
                break

            player = getattr(video, "player", None)
            tried_players.append(player or "?")
            exclude.add(player)  # bu deneme başarısız olursa aynı player'ı tekrar seçme
            _job_set(
                jid,
                player=player,
                fansub=getattr(video, "fansub", None),
                percent=None, speed=None, eta=None, error=None,
            )

            # Kopan bağlantıya karşı biraz daha dayanıklılık + kaldığı yerden
            # devam (`continuedl`). `part=True` yarım veriyi .part dosyasında
            # tutar; resume ancak bu dosya korunursa mümkün.
            try:
                video.ydl_opts = {
                    **video.ydl_opts,
                    "retries": 15,
                    "fragment_retries": 20,
                    "continuedl": bool(resume),
                    "part": True,
                }
            except Exception:  # pragma: no cover
                pass

            _job_set(jid, status="downloading")
            log.info("[%s] deneme %d/%d, player=%s: indiriliyor %s",
                     jid, attempt, _MAX_SOURCE_ATTEMPTS, player, bolum.slug)
            # indir() blocking'tir; output TABAN klasördür.
            # Dosya: <output_base>/<anime_slug>/<bolum_slug>.<ext> olarak iner.
            video.indir(callback=hook, output=output_base)

            # yt-dlp ignoreerrors yüzünden yarıda kesilse de hatasız döner:
            # diske bakıp GERÇEKTEN bitti mi kontrol et.
            tamamlandi = _download_complete(output_base, anime_slug, bolum)

            # Eksik kaldıysa: kaynağı değiştirmeden önce AYNI player'da kaldığı
            # yerden devam etmeyi dene (bant genişliği israfını önler). Farklı
            # player = farklı URL olduğundan resume yalnızca burada anlamlıdır.
            r = 0
            while (not tamamlandi and resume and r < _RESUME_ATTEMPTS
                   and not _is_cancelled(jid)):
                r += 1
                onceki = _partial_size(output_base, anime_slug, bolum)
                if onceki <= 0:
                    break  # devam edilecek yarım dosya yok
                _job_set(jid, status="downloading",
                         source_status="kaldığı yerden devam ediliyor")
                log.info("[%s] resume %d/%d (player=%s, %d bayt mevcut): %s",
                         jid, r, _RESUME_ATTEMPTS, player, onceki, bolum.slug)
                video.indir(callback=hook, output=output_base)
                tamamlandi = _download_complete(output_base, anime_slug, bolum)
                if tamamlandi:
                    break
                if _partial_size(output_base, anime_slug, bolum) <= onceki:
                    log.info("[%s] resume ilerlemedi; kaynak değiştirilecek: %s",
                             jid, bolum.slug)
                    break

            if tamamlandi:
                _job_set(jid, status="finished", error=None)
                if rename:
                    _finalize_file(jid, output_base, season_folder, bolum, pos)
                log.info("[%s] tamamlandı (player=%s): %s", jid, player, bolum.slug)
                return

            # Eksik indirme: yarım dosyayı temizle, farklı kaynağı dene.
            # (Farklı kaynağın URL'si başka olacağından yarım veri kullanılamaz.)
            last_error = f"'{player}' kaynağı akışı yarıda kesti (eksik indirme)."
            log.warning("[%s] deneme %d eksik kaldı (player=%s), sıradaki kaynak denenecek: %s",
                        jid, attempt, player, bolum.slug)
            _cleanup_partials(output_base, anime_slug, bolum)

        # Tüm denemeler tükendi → dürüstçe hata bildir (asla yanlış "finished").
        msg = "İndirme tamamlanamadı"
        if tried_players:
            msg += f" (denenen kaynaklar: {', '.join(tried_players)})"
        if last_error:
            msg += f". {last_error}"
        _job_set(jid, status="error", error=msg)
        log.error("[%s] %s: %s", jid, msg, bolum.slug)

    except _Cancelled:
        _job_set(jid, status="cancelled", error=None)
        _cleanup_partials(output_base, anime_slug, bolum)
        log.info("[%s] iptal edildi: %s", jid, bolum.slug)
    except Exception as exc:  # geniş yakalama — thread'i sessizce düşürmemek için
        # İptal, best_video gibi yerlerde farklı bir exception olarak da gelebilir.
        if _is_cancelled(jid):
            _job_set(jid, status="cancelled", error=None)
            _cleanup_partials(output_base, anime_slug, bolum)
            log.info("[%s] iptal edildi (araması sırasında): %s", jid, bolum.slug)
        else:
            _job_set(jid, status="error", error=str(exc))
            _cleanup_partials(output_base, anime_slug, bolum)
            log.exception("[%s] indirme hatası: %s", jid, exc)
    finally:
        if slot_held:
            _release_slot()


# --------------------------------------------------------------------------- #
# MCP Araçları
# --------------------------------------------------------------------------- #
def _search_turkanime(query: str) -> tuple[list, Optional[Exception]]:
    """TürkAnime'de arar; kısa retry/backoff uygular. -> (sonuçlar, son_hata).

    Site anlık takıldığında `arama_yap` istisna atmak yerine BOŞ liste de
    dönebildiğinden boş sonuç da (bir kez) yeniden denenir. Rate limit riskine
    karşı deneme sayısı düşük tutulur (TURKANIME_SEARCH_RETRIES).
    """
    ta = _get_ta()
    son_hata: Optional[Exception] = None
    for deneme in range(1, _SEARCH_RETRIES + 1):
        try:
            sonuc = list(ta.Anime.arama_yap(query) or [])
            son_hata = None
            if sonuc:
                return sonuc, None
            log.info("search_anime deneme %d/%d: boş sonuç", deneme, _SEARCH_RETRIES)
        except Exception as exc:
            son_hata = exc
            log.warning("search_anime deneme %d/%d başarısız: %s",
                        deneme, _SEARCH_RETRIES, exc)
        if deneme < _SEARCH_RETRIES:
            time.sleep(_SEARCH_BACKOFF_SECS * deneme)
    return [], son_hata


def _search_animedepo(query: str) -> list:
    """AnimeDepo fallback araması.

    Upstream `animedepo.Anime.arama_yap()` NotImplementedError atıyor; bu yüzden
    çalışan `get_anime_listesi()` (dizin.json) üzerinden yerel alt-dize eşleşmesi
    yapılır.
    """
    from turkanime_api import animedepo
    mf = _manifest()
    if mf.get("animedepo_url"):
        animedepo.BASE_URL = mf["animedepo_url"]
    q = (query or "").strip().casefold()
    return [(slug, title) for slug, title in (animedepo.Anime.get_anime_listesi() or [])
            if q in (title or "").casefold()]


@mcp.tool()
def search_anime(query: str) -> dict:
    """TürkAnime'de anime ara (erişilemezse AnimeDepo'ya düşer).

    Sonuçtaki `slug` değeri list_episodes ve download_episodes araçlarında
    kullanılır. Kısa bir retry/backoff uygulanır; TürkAnime erişilemezse ve
    manifest'te fallback tanımlıysa AnimeDepo dizininde aranır.

    "Eşleşme yok" ile "siteye ulaşılamadı" AYRI raporlanır: erişim hatasında
    `error` + `retryable: true` döner (tekrar denemek mantıklı), eşleşme
    olmadığında boş `results` + `note` döner.

    Args:
        query: Aranacak anime adı (örn. "one piece").

    Returns:
        Başarı: {"provider": "turkanime"|"animedepo", "results": [{"slug","title"}, ...]}
        Eşleşme yok: {"provider": ..., "results": [], "note": "Eşleşme yok"}
        Erişilemedi: {"error": "...", "retryable": true}
    """
    try:
        if not (query or "").strip():
            return {"provider": None, "results": [], "note": "Arama metni boş."}

        feature = _search_feature()
        fallback_adi = feature.get("fallback_provider")
        zorla = bool(feature.get("force_fallback"))
        ta_kullan = _provider_allowed("turkanime") and not zorla

        sonuc: list = []
        hata: Optional[Exception] = None
        if ta_kullan:
            sonuc, hata = _search_turkanime(query)
            if sonuc:
                return {"provider": "turkanime",
                        "results": [{"slug": s, "title": t} for s, t in sonuc]}

        # Buraya gelindiyse: TürkAnime kapalı / zorla fallback / boş / hatalı.
        fb_uygun = bool(fallback_adi) and _provider_allowed(fallback_adi)
        if fb_uygun and fallback_adi == "animedepo":
            try:
                fb_sonuc = _search_animedepo(query)
            except Exception as fb_exc:
                log.warning("AnimeDepo fallback başarısız: %s", fb_exc)
                if hata is not None:
                    return {"error": f"TürkAnime'ye ulaşılamadı ({hata}); "
                                     f"AnimeDepo fallback da başarısız ({fb_exc}).",
                            "retryable": True}
            else:
                if fb_sonuc:
                    mesaj = (_manifest().get("messages") or {}).get("turkanime_offline")
                    return {
                        "provider": "animedepo",
                        "results": [{"slug": s, "title": t} for s, t in fb_sonuc],
                        "note": mesaj or "TürkAnime kullanılamadı, AnimeDepo kullanıldı.",
                    }
                if hata is None:
                    return {"provider": "animedepo", "results": [],
                            "note": "Eşleşme yok (TürkAnime ve AnimeDepo)."}

        if hata is not None:
            return {"error": f"TürkAnime'ye ulaşılamadı: {hata}", "retryable": True}
        if not ta_kullan and not fb_uygun:
            return {"error": "Kullanılabilir arama sağlayıcısı yok (manifest).",
                    "retryable": False}
        return {"provider": "turkanime" if ta_kullan else fallback_adi,
                "results": [], "note": "Eşleşme yok"}
    except Exception as exc:
        log.exception("search_anime hatası")
        return {"error": f"Arama başarısız: {exc}", "retryable": False}


@mcp.tool()
def list_episodes(anime_slug: str, refresh: bool = False) -> dict:
    """Bir animenin bölümlerini ve temel bilgisini listele.

    `index` değerleri 0-tabanlıdır ve download_episodes'un `episodes`
    parametresinde aynen kullanılabilir.

    Args:
        anime_slug: search_anime'den gelen anime slug'ı (örn. "one-piece").
        refresh: True ise önbelleği atlayıp siteyi yeniden çeker (yeni yayınlanan
            bölümleri görmek için).

    Returns:
        {"title", "ozet", "episodes": [{"index", "slug", "title"}, ...]}
    """
    try:
        anime = _get_anime(anime_slug, refresh=refresh)
        info = getattr(anime, "info", {}) or {}
        episodes = [
            {"index": i, "slug": b.slug, "title": b.title}
            for i, b in enumerate(anime.bolumler)
        ]
        return {
            "title": getattr(anime, "title", anime_slug),
            "ozet": info.get("Özet"),
            "episode_count": len(episodes),
            "episodes": episodes,
        }
    except Exception as exc:
        log.exception("list_episodes hatası")
        return {"error": f"Bölümler alınamadı: {exc}"}


def _start_downloads(
    anime_slug: str,
    episodes: Union[int, str, list],
    output_dir: Optional[str],
    subfolder: Optional[str],
    fansub: Optional[str],
    max_resolution: bool,
    rename: bool,
    max_workers: Optional[int] = None,
    resume: bool = True,
) -> dict:
    """İndirme işlerini kuran çekirdek mantık (araçlar bunu çağırır)."""
    # Eşzamanlı indirme sınırını bu çağrıya göre ayarla (None → varsayılan=1).
    parallel = _set_parallel_limit(max_workers)

    anime = _get_anime(anime_slug)
    anime_title = getattr(anime, "title", anime_slug)
    secili = _resolve_bolumler(anime, episodes)

    output_base = _resolve_base_dir(output_dir)
    os.makedirs(output_base, exist_ok=True)
    # Sezon/anime için otomatik alt klasör (kullanıcı `subfolder` ile ezebilir).
    season_folder = _sanitize(subfolder or anime_title or anime_slug)
    season_dir = os.path.join(output_base, season_folder)

    # `pos`, animenin TAM listesindeki 0-tabanlı index'tir (seçim içindeki sıra
    # değil). `_episode_number` slug'da rakam bulamazsa buna düşer; verify_library
    # ile aynı numarayı üretmesi için gerçek index şart.
    idx_map = {id(b): i for i, b in enumerate(anime.bolumler)}

    global _state_seq
    jobs_out = []
    for bolum in secili:
        pos = idx_map.get(id(bolum), 0)
        jid = str(uuid4())
        with _JOBS_LOCK:
            _state_seq += 1
            JOBS[jid] = {
                "job_id": jid,
                "anime": anime_title,
                "anime_slug": anime_slug,
                "bolum": bolum.title,
                "bolum_slug": bolum.slug,
                "target_dir": season_dir,
                "status": "queued",
                "percent": None,
                "speed": None,
                "eta": None,
                "file": None,
                "error": None,
                "cancel": False,
            }
        _EXECUTOR.submit(
            _download_task,
            jid, anime_slug, anime_title, bolum, pos,
            output_base, season_folder, fansub, max_resolution, rename, resume,
        )
        jobs_out.append({"job_id": jid, "anime": anime_title, "bolum": bolum.title})

    _persist_jobs(force=True)
    return {
        "output_dir": output_base,
        "target_dir": season_dir,
        "queued": len(jobs_out),
        "parallel": parallel,
        "resume": bool(resume),
        "jobs": jobs_out,
        "job_ids": [j["job_id"] for j in jobs_out],
    }


@mcp.tool()
def download_episodes(
    anime_slug: str,
    episodes: Union[int, str, list],
    output_dir: Optional[str] = None,
    subfolder: Optional[str] = None,
    fansub: Optional[str] = None,
    max_resolution: bool = True,
    rename: bool = True,
    max_workers: Optional[int] = None,
    resume: bool = True,
) -> dict:
    """Bir animenin belirtilen bölümlerini ARKA PLANDA indir.

    Bu araç HEMEN döner; gerçek indirme iş havuzunda (asenkron, paralel) sürer.
    İlerlemeyi download_status ile job_id üzerinden takip et.

    `episodes` esnektir (index'ler list_episodes'daki 0-tabanlı index'tir):
        - tek index:        3            veya "3"
        - bölüm slug'ı:     "one-piece-3-bolum"
        - aralık:           "0-11"       (ilk 12 bölüm)
        - virgüllü/karışık: "0,1,2,5-8"
        - liste:            [0, 1, 2]
        - tüm sezon:        "all" (ya da download_season aracını kullan)

    Dosyalar `<output_dir>/<Anime Başlığı>/<Anime Başlığı> - NNN.ext` biçiminde
    düzenli bir alt klasöre iner.

    Args:
        anime_slug: Anime slug'ı.
        episodes: İndirilecek bölüm(ler) — yukarıdaki biçimlerden biri.
        output_dir: Kök indirme klasörü. Verilmezse config'deki
            TURKANIME_OUTPUT_DIR kullanılır (kullanıcıya özel varsayılan).
        subfolder: Sezon/anime alt klasörü adı. Verilmezse anime başlığından
            otomatik türetilir (örn. "Shingeki no Kyojin").
        fansub: Tercih edilen fansub grubu (None = filtre yok).
        max_resolution: True ise en yüksek çözünürlüğü tercih eder.
        rename: True ise dosyayı düzenli alt klasöre taşıyıp "<Başlık> - NNN.ext"
            biçiminde adlandırır.
        max_workers: Aynı anda kaç bölümün paralel ineceği. Verilmezse varsayılan
            1 (tek tek). Örn. 3 verirsen 3 bölüm aynı anda iner. Bu değer sunucu
            genelindeki eşzamanlılık sınırını ayarlar (son çağrı geçerlidir).
        resume: True (varsayılan) ise bir kaynak akışı yarıda keserse aynı
            kaynakta kaldığı yerden devam etmeye çalışır; ilerleme olmazsa yarım
            dosyayı silip farklı bir kaynağa geçer. False = her seferinde baştan.

    Returns:
        {"output_dir", "target_dir", "queued", "parallel", "resume",
         "jobs":[...], "job_ids":[...]}
    """
    try:
        return _start_downloads(
            anime_slug, episodes, output_dir, subfolder,
            fansub, max_resolution, rename, max_workers, resume,
        )
    except Exception as exc:
        log.exception("download_episodes hatası")
        return {"error": f"İndirme başlatılamadı: {exc}"}


@mcp.tool()
def download_season(
    anime_slug: str,
    output_dir: Optional[str] = None,
    subfolder: Optional[str] = None,
    fansub: Optional[str] = None,
    max_resolution: bool = True,
    rename: bool = True,
    max_workers: Optional[int] = None,
    resume: bool = True,
) -> dict:
    """Bir animenin (sezonun) TÜM bölümlerini ARKA PLANDA, asenkron indir.

    TürkAnime'de her sezon ayrı bir slug'tır (örn. "shingeki-no-kyojin",
    "shingeki-no-kyojin-season-2"). Bu araç o slug'ın tüm bölümlerini
    kuyruğa alır; `max_workers` kadarı paralel iner (verilmezse tek tek).
    Tümü `<output_dir>/<Anime Başlığı>/` altında toplanır.

    Args:
        anime_slug: Sezon slug'ı.
        output_dir: Kök klasör. Verilmezse config'deki TURKANIME_OUTPUT_DIR.
        subfolder: Sezon alt klasörü adı (verilmezse başlıktan türetilir).
        fansub: Tercih edilen fansub grubu (None = filtre yok).
        max_resolution: True ise en yüksek çözünürlük.
        rename: True ise düzenli adlandırma + alt klasöre taşıma.
        max_workers: Aynı anda kaç bölümün paralel ineceği. Verilmezse varsayılan
            1 (tek tek). Örn. 3 → 3 bölüm aynı anda.
        resume: True (varsayılan) ise yarıda kesilen indirmeyi aynı kaynakta
            kaldığı yerden devam ettirmeye çalışır (bkz. download_episodes).

    Returns:
        {"output_dir", "target_dir", "queued", "parallel", "resume",
         "jobs":[...], "job_ids":[...]}
    """
    try:
        return _start_downloads(
            anime_slug, "all", output_dir, subfolder,
            fansub, max_resolution, rename, max_workers, resume,
        )
    except Exception as exc:
        log.exception("download_season hatası")
        return {"error": f"Sezon indirme başlatılamadı: {exc}"}


@mcp.tool()
def download_status(job_id: Optional[str] = None,
                    include_history: bool = True) -> Union[dict, list]:
    """İndirme işlerinin durumunu sorgula.

    İş durumu diske kaydedilir; sunucu yeniden başlasa bile önceki oturumun
    işleri burada görünür. Restart'ta yarıda kalan işler `interrupted` olur
    (asla yanlışlıkla "hâlâ iniyor" gösterilmez); `retry_job` ile tekrar
    kuyruğa alınabilirler.

    Args:
        job_id: Belirli bir işin id'si. None ise tüm işler döner.
        include_history: True (varsayılan) ise biten/hatalı/iptal/kesilen işler
            de listelenir. False ise yalnızca AKTİF işler (queued/downloading/
            kaynak_araniyor) döner. `job_id` verildiyse bu parametre yok sayılır.

    Returns:
        Tek iş için dict, tüm işler için liste. Alanlar:
        job_id, anime, bolum, status, percent, speed, eta, file, error,
        player, fansub, target_dir, restored, retried_as.
    """
    fields = ("job_id", "anime", "bolum", "status", "percent",
              "speed", "eta", "file", "error", "player", "fansub", "target_dir",
              "restored", "retried_as")

    def _view(job: dict) -> dict:
        return {k: job.get(k) for k in fields}

    try:
        with _JOBS_LOCK:
            if job_id is not None:
                job = JOBS.get(job_id)
                if job is None:
                    return {"error": f"İş bulunamadı: {job_id}"}
                return _view(job)
            return [_view(j) for j in JOBS.values()
                    if include_history or j.get("status") not in _DONE_STATUSES]
    except Exception as exc:
        log.exception("download_status hatası")
        return {"error": f"Durum alınamadı: {exc}"}


@mcp.tool()
def cancel_download(job_id: str) -> dict:
    """Devam eden ya da kuyruktaki bir indirmeyi iptal et.

    İndirme sürüyorsa yt-dlp bir sonraki ilerleme adımında durdurulur ve yarım
    dosyalar temizlenir. Zaten bitmiş/hatalı işlerde bir şey yapılmaz.

    Args:
        job_id: İptal edilecek işin id'si.

    Returns:
        {"job_id", "status", "message"}
    """
    try:
        with _JOBS_LOCK:
            job = JOBS.get(job_id)
            if job is None:
                return {"error": f"İş bulunamadı: {job_id}"}
            status = job.get("status")
            if status in _DONE_STATUSES:
                return {"job_id": job_id, "status": status,
                        "message": "İş zaten tamamlanmış; iptal edilemez."}
            job["cancel"] = True
            _bump_state_seq_locked()
        _persist_jobs(force=True)
        return {"job_id": job_id, "status": "cancelling",
                "message": "İptal istendi; kısa süre içinde duracak."}
    except Exception as exc:
        log.exception("cancel_download hatası")
        return {"error": f"İptal edilemedi: {exc}"}


@mcp.tool()
def cancel_all() -> dict:
    """Devam eden ve kuyrukta bekleyen TÜM indirmeleri iptal et.

    Returns:
        {"cancelling": <adet>, "job_ids": [...]}
    """
    try:
        affected = []
        with _JOBS_LOCK:
            for jid, job in JOBS.items():
                if job.get("status") not in _DONE_STATUSES:
                    job["cancel"] = True
                    affected.append(jid)
            if affected:
                _bump_state_seq_locked()
        if affected:
            _persist_jobs(force=True)
        return {"cancelling": len(affected), "job_ids": affected}
    except Exception as exc:
        log.exception("cancel_all hatası")
        return {"error": f"İptal edilemedi: {exc}"}


@mcp.tool()
def clear_finished_jobs() -> dict:
    """Tamamlanmış (finished/error/cancelled) işleri iş listesinden temizle.

    Devam eden/kuyruktaki işlere dokunmaz. Uzun oturumlarda iş listesinin
    şişmesini önler.

    Returns:
        {"removed": <adet>, "remaining": <adet>}
    """
    try:
        with _JOBS_LOCK:
            done = [jid for jid, job in JOBS.items()
                    if job.get("status") in _DONE_STATUSES]
            for jid in done:
                del JOBS[jid]
            remaining = len(JOBS)
            if done:
                _bump_state_seq_locked()
        if done:
            _persist_jobs(force=True)
        return {"removed": len(done), "remaining": remaining}
    except Exception as exc:
        log.exception("clear_finished_jobs hatası")
        return {"error": f"Temizlenemedi: {exc}"}


@mcp.tool()
def list_fansubs(anime_slug: str, episode: Union[int, str] = 0) -> dict:
    """Bir bölüm için mevcut fansub gruplarını listele.

    download_episodes/download_season'daki `fansub` parametresine bu adlardan
    birini verebilirsin.

    Args:
        anime_slug: Anime slug'ı.
        episode: Bölüm (0-tabanlı index ya da bölüm slug'ı). Varsayılan ilk bölüm.

    Returns:
        {"anime", "bolum", "fansubs": [<ad>, ...]}
    """
    try:
        anime = _get_anime(anime_slug)
        secili = _resolve_bolumler(anime, episode)
        bolum = secili[0]
        fansubs = list(getattr(bolum, "fansubs", []) or [])
        return {
            "anime": getattr(anime, "title", anime_slug),
            "bolum": bolum.title,
            "fansubs": fansubs,
        }
    except Exception as exc:
        log.exception("list_fansubs hatası")
        return {"error": f"Fansublar alınamadı: {exc}"}


@mcp.tool()
def verify_library(
    anime_slug: str,
    output_dir: Optional[str] = None,
    subfolder: Optional[str] = None,
    repair: bool = False,
    fansub: Optional[str] = None,
    max_resolution: bool = True,
    max_workers: Optional[int] = None,
) -> dict:
    """Bir animenin indirme klasörünü doğrula: hangi bölümler eksik/yarım?

    Klasörü tarar ve her bölümü üç durumdan birine ayırır:
      - `ok`: eşikten (varsayılan 1 MB) büyük son dosya var, yarım dosya yok.
      - `partial`: `.part`/`.ytdl` ya da eşik altı/0-byte dosya var.
      - `missing`: hiç dosya yok.

    Hem düzenli `"<Başlık> - NNN.ext"` adlandırmasını hem de ham
    `"<bolum_slug>.*"` adlandırmasını tanır (eski manuel indirmeler için).

    `repair=True` verilirse eksik + yarım bölümleri ARKA PLANDA yeniden indirmeye
    alır; ilerlemeyi download_status ile takip et.

    Args:
        anime_slug: Anime slug'ı.
        output_dir: Kök klasör. Verilmezse config'deki TURKANIME_OUTPUT_DIR.
        subfolder: Sezon alt klasörü (verilmezse anime başlığından türetilir).
        repair: True ise eksik/yarım bölümleri kuyruğa alır. False = sadece rapor.
        fansub: Onarım indirmelerinde tercih edilen fansub (None = filtre yok).
        max_resolution: Onarım indirmelerinde en yüksek çözünürlüğü tercih et.
        max_workers: Onarım indirmelerinde paralel iş sayısı.

    Returns:
        {"anime", "target_dir", "total_episodes", "ok":[...], "partial":[...],
         "missing":[...], "repaired", "queued_job_ids":[...]}
        Bölüm numaraları 1-tabanlıdır (dosya adındaki numarayla aynı).
    """
    try:
        anime = _get_anime(anime_slug)
        anime_title = getattr(anime, "title", anime_slug)
        output_base = _resolve_base_dir(output_dir)
        season_folder = _sanitize(subfolder or anime_title or anime_slug)
        season_dir = os.path.join(output_base, season_folder)

        states = _library_states(anime, output_base, season_folder)
        result: dict[str, Any] = {
            "anime": anime_title,
            "target_dir": season_dir,
            "total_episodes": len(states),
            "ok": sorted(s["episode"] for s in states if s["status"] == "ok"),
            "partial": sorted(s["episode"] for s in states if s["status"] == "partial"),
            "missing": sorted(s["episode"] for s in states if s["status"] == "missing"),
            "repaired": False,
            "queued_job_ids": [],
        }
        if not repair:
            return result

        bozuk = [s["index"] for s in states if s["status"] in ("partial", "missing")]
        result["repaired"] = True
        if not bozuk:
            result["note"] = "Onarılacak bölüm yok; kütüphane eksiksiz."
            return result

        started = _start_downloads(
            anime_slug, bozuk, output_dir, subfolder,
            fansub, max_resolution, True, max_workers,
        )
        result["queued_job_ids"] = started.get("job_ids", [])
        result["queued"] = started.get("queued", 0)
        return result
    except Exception as exc:
        log.exception("verify_library hatası")
        return {"error": f"Kütüphane doğrulanamadı: {exc}"}


def main() -> None:
    """stdio üzerinden MCP sunucusunu çalıştır."""
    log.info("turkanime-mcp başlatılıyor (varsayılan paralel=%d, havuz=%d, "
             "kaynak deneme=%d)", _DEFAULT_PARALLEL, _WORKER_POOL_SIZE,
             _MAX_SOURCE_ATTEMPTS)
    if _DEFAULT_OUTPUT_DIR:
        log.info("Varsayılan indirme klasörü: %s", _DEFAULT_OUTPUT_DIR)
    # Önceki oturumun iş geçmişini geri yükle (yarıda kalanlar interrupted olur).
    log.info("Durum dosyası: %s", _STATE_FILE)
    _load_state()
    # ffmpeg bazı formatların birleştirilmesi için gereklidir; yoksa uyar.
    if shutil.which("ffmpeg") is None:
        log.warning("ffmpeg PATH'te bulunamadı — bazı bölümlerin ses/video "
                    "birleştirmesi başarısız olabilir. Kurulum: winget install Gyan.FFmpeg")
    mcp.run()  # varsayılan transport: stdio


if __name__ == "__main__":
    main()
