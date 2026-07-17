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

# job_id -> iş bilgisi dict'i
JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()

# slug -> Anime nesnesi önbelleği (fetch_info çağrılmış halde)
_ANIME_CACHE: dict[str, Any] = {}
_ANIME_LOCK = threading.Lock()

# İşin tamamlandığı sayılan durumlar (temizleme/özet için)
_DONE_STATUSES = {"finished", "error", "cancelled"}


class _Cancelled(Exception):
    """İptal edilen indirmeyi normal hatadan ayırmak için işaret exception'ı."""


def _is_cancelled(jid: str) -> bool:
    """İş için iptal istenmiş mi (thread-güvenli)."""
    with _JOBS_LOCK:
        job = JOBS.get(jid)
        return bool(job and job.get("cancel"))


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
    """İş sözlüğünü thread-güvenli günceller."""
    with _JOBS_LOCK:
        if jid in JOBS:
            JOBS[jid].update(fields)


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

            # Kopan bağlantıya karşı biraz daha dayanıklılık.
            try:
                video.ydl_opts = {**video.ydl_opts, "retries": 15, "fragment_retries": 20}
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
            if _download_complete(output_base, anime_slug, bolum):
                _job_set(jid, status="finished", error=None)
                if rename:
                    _finalize_file(jid, output_base, season_folder, bolum, pos)
                log.info("[%s] tamamlandı (player=%s): %s", jid, player, bolum.slug)
                return

            # Eksik indirme: yarım dosyayı temizle, farklı kaynağı dene.
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
@mcp.tool()
def search_anime(query: str) -> list[dict]:
    """TürkAnime'de anime ara.

    Verilen metinle eşleşen animeleri döndürür. Sonuçtaki `slug` değeri,
    list_episodes ve download_episodes araçlarında kullanılır.

    Args:
        query: Aranacak anime adı (örn. "one piece").

    Returns:
        [{"slug": ..., "title": ...}, ...] biçiminde liste.
    """
    try:
        ta = _get_ta()
        results = ta.Anime.arama_yap(query)  # -> [(slug, title), ...]
        return [{"slug": slug, "title": title} for slug, title in results]
    except Exception as exc:
        log.exception("search_anime hatası")
        return [{"error": f"Arama başarısız: {exc}"}]


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

    jobs_out = []
    for bolum in secili:
        pos = idx_map.get(id(bolum), 0)
        jid = str(uuid4())
        with _JOBS_LOCK:
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
            output_base, season_folder, fansub, max_resolution, rename,
        )
        jobs_out.append({"job_id": jid, "anime": anime_title, "bolum": bolum.title})

    return {
        "output_dir": output_base,
        "target_dir": season_dir,
        "queued": len(jobs_out),
        "parallel": parallel,
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

    Returns:
        {"output_dir", "target_dir", "queued", "parallel", "jobs":[...], "job_ids":[...]}
    """
    try:
        return _start_downloads(
            anime_slug, episodes, output_dir, subfolder,
            fansub, max_resolution, rename, max_workers,
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

    Returns:
        {"output_dir", "target_dir", "queued", "parallel", "jobs":[...], "job_ids":[...]}
    """
    try:
        return _start_downloads(
            anime_slug, "all", output_dir, subfolder,
            fansub, max_resolution, rename, max_workers,
        )
    except Exception as exc:
        log.exception("download_season hatası")
        return {"error": f"Sezon indirme başlatılamadı: {exc}"}


@mcp.tool()
def download_status(job_id: Optional[str] = None) -> Union[dict, list]:
    """İndirme işlerinin durumunu sorgula.

    Args:
        job_id: Belirli bir işin id'si. None ise tüm işler döner.

    Returns:
        Tek iş için dict, tüm işler için liste. Alanlar:
        job_id, anime, bolum, status, percent, speed, eta, file, error.
    """
    fields = ("job_id", "anime", "bolum", "status", "percent",
              "speed", "eta", "file", "error", "player", "fansub", "target_dir")

    def _view(job: dict) -> dict:
        return {k: job.get(k) for k in fields}

    try:
        with _JOBS_LOCK:
            if job_id is not None:
                job = JOBS.get(job_id)
                if job is None:
                    return {"error": f"İş bulunamadı: {job_id}"}
                return _view(job)
            return [_view(j) for j in JOBS.values()]
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
    # ffmpeg bazı formatların birleştirilmesi için gereklidir; yoksa uyar.
    if shutil.which("ffmpeg") is None:
        log.warning("ffmpeg PATH'te bulunamadı — bazı bölümlerin ses/video "
                    "birleştirmesi başarısız olabilir. Kurulum: winget install Gyan.FFmpeg")
    mcp.run()  # varsayılan transport: stdio


if __name__ == "__main__":
    main()
