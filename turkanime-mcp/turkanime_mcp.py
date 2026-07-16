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
_MAX_WORKERS = int(os.environ.get("TURKANIME_MAX_WORKERS", "3"))
_EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="ta-dl")

# Varsayılan indirme klasörü — kullanıcıya özeldir, Claude Desktop config'inde
# env -> TURKANIME_OUTPUT_DIR ile ayarlanır. Böylece kullanıcı "masaüstü/anime"
# ya da "D:\Anime" gibi kendi kök klasörünü belirler; her indirmede tekrar
# yazmasına gerek kalmaz.
_DEFAULT_OUTPUT_DIR = os.environ.get("TURKANIME_OUTPUT_DIR", "").strip()

# "Tüm bölümler" için kabul edilen anahtar sözcükler
_ALL_TOKENS = {"all", "hepsi", "tümü", "tumu", "tum", "*", "sezon", "season"}

# job_id -> iş bilgisi dict'i
JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()

# slug -> Anime nesnesi önbelleği (fetch_info çağrılmış halde)
_ANIME_CACHE: dict[str, Any] = {}
_ANIME_LOCK = threading.Lock()

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


def _get_anime(slug: str):
    """Anime nesnesini kurar, fetch_info() çağırır ve önbelleğe alır.

    fetch_info() şarttır: aksi halde anime_id boş kalır ve bölüm listesi
    gelmez (bkz. dokümantasyon §7.1).
    """
    slug = (slug or "").strip()
    if not slug:
        raise ValueError("anime_slug boş olamaz.")
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
    try:
        _job_set(jid, status="kaynak_araniyor")

        def bv_callback(d: dict) -> None:
            # best_video ilerleme/durum bildirimi
            _job_set(
                jid,
                source_status=d.get("status"),
                source_current=d.get("current"),
                source_total=d.get("total"),
                player=d.get("player"),
            )

        video = bolum.best_video(
            by_res=max_resolution,
            by_fansub=fansub,
            callback=bv_callback,
        )
        if video is None:
            _job_set(jid, status="error", error="Bu bölüm için çalışan kaynak bulunamadı.")
            log.info("[%s] çalışan kaynak yok: %s", jid, bolum.slug)
            return

        _job_set(
            jid,
            player=getattr(video, "player", None),
            fansub=getattr(video, "fansub", None),
        )

        def hook(d: dict) -> None:
            # yt-dlp progress_hooks callback'i
            _job_set(
                jid,
                status=d.get("status"),
                percent=(d.get("_percent_str") or "").strip() or None,
                speed=(d.get("_speed_str") or "").strip() or None,
                eta=d.get("eta"),
                file=d.get("filename"),
            )

        _job_set(jid, status="downloading")
        # indir() blocking'tir; output TABAN klasördür.
        # Dosya: <output_base>/<anime_slug>/<bolum_slug>.<ext> olarak iner.
        video.indir(callback=hook, output=output_base)

        _job_set(jid, status="finished")
        if rename:
            _finalize_file(jid, output_base, season_folder, bolum, pos)
        log.info("[%s] tamamlandı: %s", jid, bolum.slug)

    except Exception as exc:  # geniş yakalama — thread'i sessizce düşürmemek için
        _job_set(jid, status="error", error=str(exc))
        log.exception("[%s] indirme hatası: %s", jid, exc)


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
def list_episodes(anime_slug: str) -> dict:
    """Bir animenin bölümlerini ve temel bilgisini listele.

    `index` değerleri 0-tabanlıdır ve download_episodes'un `episodes`
    parametresinde aynen kullanılabilir.

    Args:
        anime_slug: search_anime'den gelen anime slug'ı (örn. "one-piece").

    Returns:
        {"title", "ozet", "episodes": [{"index", "slug", "title"}, ...]}
    """
    try:
        anime = _get_anime(anime_slug)
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
) -> dict:
    """İndirme işlerini kuran çekirdek mantık (araçlar bunu çağırır)."""
    anime = _get_anime(anime_slug)
    anime_title = getattr(anime, "title", anime_slug)
    secili = _resolve_bolumler(anime, episodes)

    output_base = _resolve_base_dir(output_dir)
    os.makedirs(output_base, exist_ok=True)
    # Sezon/anime için otomatik alt klasör (kullanıcı `subfolder` ile ezebilir).
    season_folder = _sanitize(subfolder or anime_title or anime_slug)
    season_dir = os.path.join(output_base, season_folder)

    jobs_out = []
    for pos, bolum in enumerate(secili):
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

    Returns:
        {"output_dir", "target_dir", "queued", "jobs":[...], "job_ids":[...]}
    """
    try:
        return _start_downloads(
            anime_slug, episodes, output_dir, subfolder,
            fansub, max_resolution, rename,
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
) -> dict:
    """Bir animenin (sezonun) TÜM bölümlerini ARKA PLANDA, asenkron indir.

    TürkAnime'de her sezon ayrı bir slug'tır (örn. "shingeki-no-kyojin",
    "shingeki-no-kyojin-season-2"). Bu araç o slug'ın tüm bölümlerini
    aynı anda kuyruğa alır; TURKANIME_MAX_WORKERS kadarı paralel iner.
    Tümü `<output_dir>/<Anime Başlığı>/` altında toplanır.

    Args:
        anime_slug: Sezon slug'ı.
        output_dir: Kök klasör. Verilmezse config'deki TURKANIME_OUTPUT_DIR.
        subfolder: Sezon alt klasörü adı (verilmezse başlıktan türetilir).
        fansub: Tercih edilen fansub grubu (None = filtre yok).
        max_resolution: True ise en yüksek çözünürlük.
        rename: True ise düzenli adlandırma + alt klasöre taşıma.

    Returns:
        {"output_dir", "target_dir", "queued", "jobs":[...], "job_ids":[...]}
    """
    try:
        return _start_downloads(
            anime_slug, "all", output_dir, subfolder,
            fansub, max_resolution, rename,
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


def main() -> None:
    """stdio üzerinden MCP sunucusunu çalıştır."""
    log.info("turkanime-mcp başlatılıyor (max_workers=%d)", _MAX_WORKERS)
    mcp.run()  # varsayılan transport: stdio


if __name__ == "__main__":
    main()
