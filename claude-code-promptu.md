# Claude Code Promptu — TürkAnime İndirici MCP Sunucusu

> Bu dosyanın tamamını Claude Code'a yapıştır. Yanına `turkanime-api-dokumantasyon.md` dosyasını da aynı klasöre koy (prompt ona atıf yapıyor).

---

## GÖREV

`turkanime-cli` paketini (KebabLord/turkanime-indirici) saran, **stdio tabanlı bir MCP sunucusu** yaz. Bu sunucu Claude Desktop'a eklenecek ve kullanıcı "şu animenin şu bölümlerini indir" dediğinde, Claude arka planda indirip klasörleyecek. Hedef platform: **Windows**.

## ADIM 0 — Önce depoyu indir ve API'yi kendi gözünle doğrula

1. Repoyu klonla ve incele:
   ```
   git clone https://github.com/KebabLord/turkanime-indirici.git
   ```
   Klonlanamazsa `pip download turkanime-cli --no-deps --no-binary :all:` ile ya da kurup site-packages içinden oku.
2. Şu dosyaları OKU ve API'yi teyit et (imzalar değişmiş olabilir, koda güven):
   - `turkanime_api/objects.py` → `Anime`, `Bolum`, `Video` sınıfları
   - `turkanime_api/bypass.py` → `fetch`, `get_real_url`
   - `turkanime_api/__init__.py`, `pyproject.toml`
3. Yanındaki **`turkanime-api-dokumantasyon.md`** dosyasını da oku — API'nin çıkarılmış referansı ve tuzaklar orada. Kod ile doküman çelişirse **KODU esas al** ve MCP'yi ona göre yaz.

> Not: Bu sürüm Selenium/webdriver KULLANMIYOR; bypass `curl_cffi` ile yapılıyor. Webdriver kurulumu ekleme.

## ADIM 1 — Projeyi kur

- Ayrı bir klasör: `turkanime-mcp/`
- Bağımlılıklar: `mcp` (resmi Python MCP SDK) + `turkanime-cli` (yt-dlp, curl-cffi, pycryptodome dahil gelir).
- `requirements.txt` ve kısa `README.md` (Windows kurulum + Claude Desktop config) üret.
- Python 3.10+.

## ADIM 2 — MCP araçlarını uygula

Kurulu paketi **import ederek** kullan (`import turkanime_api as ta`), depoyu kopyalama. Şu 4 aracı sun:

### `search_anime(query: str)`
- `ta.Anime.arama_yap(query)` çağır → `[(slug, title)]`.
- Dönüş: `[{"slug": ..., "title": ...}]`.

### `list_episodes(anime_slug: str)`
- `a = ta.Anime(anime_slug)`, sonra **mutlaka** `a.fetch_info()` (yoksa `anime_id` boş, bölümler gelmez).
- `a.bolumler` → `list[Bolum]`.
- Dönüş: `{"title": a.title, "ozet": a.info.get("Özet"), "episodes": [{"index": i, "slug": b.slug, "title": b.title}]}`.

### `download_episodes(anime_slug, episodes, output_dir, fansub=None, max_resolution=True)`
- `episodes` esnek olsun: tek index/slug, liste, veya `"1-12"` aralığı string'i. Bunu bölüm listesine çöz.
- **Arka planda** indir: modül seviyesinde bir `ThreadPoolExecutor` ve `JOBS: dict[str, dict]` tut. Her bölüm için `uuid4` job_id üret.
- Her iş şunu yapsın:
  ```python
  video = bolum.best_video(by_res=max_resolution, by_fansub=fansub, callback=<durum callback>)
  if video is None:
      JOBS[jid].update(status="error", error="çalışan kaynak yok"); return
  def hook(d):  # yt-dlp progress_hooks
      JOBS[jid].update(status=d.get("status"),
                       percent=d.get("_percent_str"),
                       speed=d.get("_speed_str"),
                       eta=d.get("eta"),
                       file=d.get("filename"))
  video.indir(callback=hook, output=output_dir)
  # indir() dosyayı: output_dir/<anime_slug>/<bolum_slug>.<ext> olarak yazar
  JOBS[jid].update(status="finished")
  ```
- `download_episodes` **HEMEN** dönmeli: `{"job_ids": [...], "output_dir": ...}`. Gerçek indirme thread havuzunda sürer.
- `indir()` blocking'tir; asla ana MCP thread'inde çağırma.

### `download_status(job_id: str = None)`
- `job_id` verilmişse o işi, verilmemişse tüm işleri döndür.
- Alanlar: `job_id, anime, bolum, status, percent, speed, eta, file, error`.

## ADIM 3 — Klasörleme

- `indir(output=...)` tabanın altına otomatik `anime_slug/bolum_slug.ext` açar.
- Kullanıcı "klasörü ben veririm, düzeni sen belirle" istiyor: `output_dir`'i olduğu gibi geçir. İndirme bitince (`finished` hook'unda `filename` gerçek yolu verir) isteğe bağlı olarak `anime_slug` klasörünü okunur başlığa (`anime.title`) yeniden adlandır ve dosyayı `Anime Adı - 001.ext` gibi düzenle. Yeniden adlandırmayı `finished` sonrası yap; uzantı önceden bilinmez.
- Windows yol ayracına ve geçersiz karakterlere (`\ / : * ? " < > |`) dikkat; başlıkları temizle.

## ADIM 4 — Sağlamlık

- Her araçta try/except; hataları düzgün mesajla döndür (traceback sızdırma).
- Ağ çağrıları yavaş: `arama_yap`, `fetch_info`, `best_video` uzun sürebilir — makul davran, MCP'yi kilitleme.
- `best_video` `None` dönebilir → nazik hata.
- Windows'ta ffmpeg önerisini README'ye yaz (bazı formatların birleşmesi için).
- Sunucuyu stdio ile çalıştır (`mcp.server` / FastMCP). Log'ları stderr'e yaz, stdout'u kirletme (MCP protokolü stdout kullanır).

## ADIM 5 — Test ve teslim

- `python -m py_compile` ile sözdizimini doğrula.
- `import turkanime_api` çalışıyor mu kontrol et.
- Gerçek bir aramayla (örn. "one piece") `search_anime` ve `list_episodes`'u dene; en az bir bölümde `best_video`'nun video döndürdüğünü doğrula (indirmeyi tamamlamak şart değil, kaynak bulunuyor mu bak).
- Claude Desktop `claude_desktop_config.json` için örnek blok üret:
  ```json
  {
    "mcpServers": {
      "turkanime": {
        "command": "python",
        "args": ["-m", "turkanime_mcp"],
        "cwd": "C:\\path\\to\\turkanime-mcp"
      }
    }
  }
  ```
  (Gerçek modül/dosya adına göre uyarlanacak.)

## KISITLAR
- Selenium/webdriver EKLEME (gerekmiyor).
- Depoyu vendorlama; kurulu `turkanime-cli` paketini import et.
- Sadece kişisel kullanım; MCP kimlik doğrulama/paywall aşımı vб. eklemesin — yalnızca mevcut kütüphaneyi sarar.
- Kod ve yorumlar Türkçe olabilir; araç açıklamaları (tool descriptions) net Türkçe/İngilizce olsun ki Claude doğru çağırsın.

## ÇIKTI
Çalışan bir `turkanime-mcp/` klasörü: MCP sunucu kodu, `requirements.txt`, `README.md` (Windows kurulum + config), ve yukarıdaki 4 araç.
