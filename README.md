<div align="center">

# 🧩 TürkAnime İndirici — MCP Sunucusu

**Claude Desktop'tan doğal dille anime ara, listele ve indir.**

`turkanime-cli` paketini saran, **stdio tabanlı** bir Model Context Protocol (MCP) sunucusu.
Claude'a "şu animenin şu bölümlerini indir" dediğinde arka planda arar, en iyi kaynağı bulur,
indirir ve düzenli klasörler.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?style=flat-square)
![MCP](https://img.shields.io/badge/MCP-stdio-8A2BE2?style=flat-square)

</div>

---

## Ne yapar?

Bu sunucu, TürkAnime'nin Python API'sini (`turkanime_api`) Claude Desktop'a **14 araç**
olarak açar. Kullanıcı sohbette isteğini yazar, Claude uygun aracı çağırır:

> "Attack on Titan'ı bul, 1. sezonun ilk 3 bölümünü indir."
>
> "Bocchi the Rock'ın **tüm sezonunu** indir."
>
> "Horimiya klasörümü **kontrol et**, eksik bölüm varsa tamamla."
>
> "İndirmelerin durumu ne?"

- ⚙️ **Selenium/webdriver gerektirmez** — Cloudflare bypass `curl_cffi` (Firefox TLS impersonation) ile.
- 🎞️ İndirme motoru **yt-dlp**; şifreli video URL'leri `pycryptodome` ile çözülür.
- 🧵 İndirmeler **arka planda** sürer; kaç bölümün **paralel** ineceğini seçebilirsin (`max_workers`, varsayılan tek tek).
- 🔁 **Güvenilir tamamlanma:** yarıda kesilen indirme "bitti" sanılmaz — diske bakılıp doğrulanır,
  önce **kaldığı yerden devam** edilir, olmazsa **farklı bir kaynak** denenir. Kararsız **BETA**
  kaynakları (ör. `ALUCARD(BETA)`) en sona atılır.
- 💾 **İş durumu kalıcı:** sunucu yeniden başlasa da geçmiş korunur; yarıda kalanlar `interrupted`
  işaretlenir (asla yanlış "hâlâ iniyor") ve `retry_job` ile kurtarılabilir.
- 🔎 **Kütüphane doğrulama:** `verify_library` eksik/yarım bölümleri bulur, istersen onarır.
- 🗂️ Her anime için **otomatik düzenli klasör**; istersen **Jellyfin/Plex** uyumlu adlandırma.
- 🪟 **Windows** için tasarlandı; ASCII-dışı kullanıcı adlarındaki SSL sorununu otomatik çözer.

---

## Araçlar

### Arama / listeleme

| Araç | Ne yapar | Döner |
|------|----------|-------|
| `search_anime(query)` | Anime arar (retry'lı; TürkAnime erişilemezse **AnimeDepo**'ya düşer) | `{provider, results:[{slug, title}]}` |
| `list_episodes(anime_slug, refresh?)` | Bölümleri + özeti listeler (`refresh` yeni bölümler için) | `{title, ozet, episodes:[{index, slug, title}]}` |
| `list_fansubs(anime_slug, episode?)` | Bölümün mevcut fansub gruplarını listeler | `{anime, bolum, fansubs:[…]}` |

### İndirme

| Araç | Ne yapar | Döner |
|------|----------|-------|
| `download_episodes(anime_slug, episodes, …)` | Seçili bölümleri **arka planda** indirir | `{job_ids, target_dir, parallel, skipped, …}` |
| `download_season(anime_slug, …)` | **Tüm sezonu** asenkron indirir | `{job_ids, target_dir, parallel, skipped, …}` |
| `verify_library(anime_slug, …, repair?)` | Klasörü tarar: hangi bölüm **eksik/yarım**? `repair=True` ile onarır | `{ok:[…], partial:[…], missing:[…], queued_job_ids}` |
| `check_new_episodes(anime_slug, auto_download?)` | **Yeni bölüm** yayınlanmış mı? İstersen hemen indirir | `{new_episodes:[…], previous_count, queued_job_ids}` |

Ortak indirme parametreleri: `output_dir`, `subfolder`, `fansub`, `max_resolution`, `rename`,
`max_workers`, `resume`, `skip_existing`, `naming`, `season_number`.

### İş yönetimi

| Araç | Ne yapar | Döner |
|------|----------|-------|
| `download_status(job_id?, include_history?)` | İndirme durumunu sorgular (`include_history=False` → sadece aktifler) | `[{status, percent, speed, eta, file, …}]` |
| `get_batch_summary(job_ids?)` | **Toplu** ilerleme özeti (tek tek pollamadan) | `{total, finished, downloading, average_percent, eta_seconds, errors}` |
| `retry_job(job_id)` | `error`/`interrupted` bir işi orijinal parametreleriyle yeniden dener | `{job_id, retry_of, bolum, status}` |
| `cancel_download(job_id)` | Bir indirmeyi iptal eder (yarım dosyaları temizler) | `{job_id, status, message}` |
| `cancel_all()` | Devam eden/kuyruktaki tüm indirmeleri iptal eder | `{cancelling, job_ids}` |
| `clear_finished_jobs()` | Biten/hatalı/iptal/kesilen işleri listeden temizler | `{removed, remaining}` |

### Teşhis

| Araç | Ne yapar | Döner |
|------|----------|-------|
| `health_check()` | TürkAnime erişimi, `ffmpeg`, indirme klasörü, CA bundle, durum klasörü | `{overall, checks:[{name, status, detail}]}` |

> **⚠️ Kırıcı değişiklik:** `search_anime` artık düz liste yerine **dict** döner
> (`{"provider": …, "results": […]}`). Böylece "eşleşme yok" (`results: []` + `note`) ile
> "siteye ulaşılamadı" (`error` + `retryable: true`) ayırt edilebilir. Diğer araçların
> dönüş şeması değişmedi; yeni parametrelerin hepsi varsayılanlıdır.

---

## Sık kullanılan akışlar

**Sezonu tamamla / güncelle** — var olanı tekrar indirmeden:

```
download_season("horimiya", skip_existing=True)
```

**Kütüphaneyi denetle** (hiçbir şey indirmeden rapor al):

```
verify_library("horimiya")
→ {"total_episodes": 13, "ok": [1,2,3], "partial": [12], "missing": [4,5,6,7,8,9,10], …}
```

**Eksikleri onar:**

```
verify_library("horimiya", repair=True)
```

**Simulcast takibi** (zamanlanmış göreve bağlanabilir):

```
check_new_episodes("frieren", auto_download=True)
```

**Jellyfin/Plex kütüphanesi için:**

```
download_season("horimiya", naming="jellyfin", season_number=1)
→ Horimiya\Horimiya - S01E04.mp4
```

### `episodes` parametresi esnektir

`index` değerleri `list_episodes`'daki **0-tabanlı** `index` ile aynıdır:

| Biçim | Örnek | Anlamı |
|-------|-------|--------|
| tek index | `3` / `"3"` | 4. sıradaki bölüm |
| bölüm slug'ı | `"one-piece-3-bolum"` | slug ile tek bölüm |
| aralık | `"0-11"` | ilk 12 bölüm |
| virgüllü/karışık | `"0,1,2,5-8"` | seçili bölümler |
| liste | `[0, 1, 2]` | seçili bölümler |
| tüm sezon | `"all"` | tüm bölümler (ya da `download_season`) |

---

## İndirme klasörü ve düzeni

- **Kök klasör kullanıcıya özeldir.** Config'de `env → TURKANIME_OUTPUT_DIR` ile belirlenir
  (örn. `D:\Anime`, `C:\İndirilenler`, masaüstünüz). Araç çağrısında `output_dir` verilmezse bu
  varsayılan kullanılır; ikisi de yoksa araç nazikçe uyarır.
- Sunucu, kök klasörün içinde her anime için **otomatik alt klasör** açıp dosyayı oraya taşır:

  ```
  <kök>/<Anime Başlığı>/<Anime Başlığı> - NNN.ext
  # örn:  D:\Anime\Shingeki no Kyojin\Shingeki no Kyojin - 001.mp4
  ```

- Alt klasör adını `subfolder` parametresiyle elle de verebilirsin.
- `rename=False` verilirse ham düzen kalır: `<kök>/<anime_slug>/<bolum_slug>.ext`.
- `naming="jellyfin"` verilirse medya sunucusu uyumlu adlandırma kullanılır:

  ```
  <kök>/<Anime Başlığı>/<Anime Başlığı> - S01E04.ext
  ```

  Sezon numarasını `season_number` ile verirsin (TürkAnime'de her sezon ayrı bir slug olduğu için
  otomatik çıkarılamaz).
- **Tüm sezon** indirmede bütün bölümler kuyruğa girer; aynı anda kaç tanesinin ineceğini
  `max_workers` belirler (verilmezse varsayılan **1 = tek tek**; `TURKANIME_MAX_WORKERS` ile
  varsayılanı değiştirebilirsin). Gerisi sırada bekler.

`verify_library` ve `skip_existing` **üç adlandırmayı da** tanır (`<Başlık> - NNN.ext`,
`<Başlık> - S01E04.ext` ve ham `<bolum_slug>.*`), yani eski manuel indirmelerin de doğru okunur.

---

## İş durumu kalıcıdır

İş kayıtları `<durum klasörü>/jobs.json` dosyasına yazılır (atomik: temp + `os.replace`):

- **Ne zaman yazılır:** iş oluşturulduğunda, `status` değiştiğinde (anında) ve ara ilerleme
  güncellemelerinde (`TURKANIME_STATE_FLUSH_SECS` ile debounce'lu — disk dövülmez).
- **Açılışta:** önceki oturumun işleri geri yüklenir. Çalışır *görünen* işler (`downloading`,
  `queued`, `kaynak_araniyor`) **`interrupted`** işaretlenir — thread'leri artık olmadığından
  yanlışlıkla "hâlâ iniyor" gösterilmez.
- `retry_job(job_id)` ile kesilen işler orijinal parametreleriyle yeniden kuyruğa alınır.
- Durum klasörü: `TURKANIME_STATE_DIR` → yoksa `%PROGRAMDATA%\turkanime-mcp` → o da yoksa
  `~/.turkanime-mcp`.

---

## Kurulum (Windows)

Python **3.10+** gerekir.

```powershell
git clone https://github.com/TurkerYakup/mcp-server-turkanime-indirici.git
cd mcp-server-turkanime-indirici\turkanime-mcp
pip install -r requirements.txt
```

İsterseniz paket olarak da kurabilirsiniz (bir `turkanime-mcp` komutu oluşturur):

```powershell
pip install .
```

> **pywin32 notu:** Windows'ta resmi `mcp` SDK'sinin stdio katmanı `pywintypes` modülüne
> ihtiyaç duyar; bu `pywin32` ile gelir ve `requirements.txt` içindedir. Gerekirse:
> `python -m pywin32_postinstall -install`.

### ffmpeg (önerilir)

Bazı formatların (ayrı video/ses parçaları) birleştirilmesi için:

```powershell
winget install Gyan.FFmpeg
```

`ffmpeg`'in `PATH`'te olduğundan emin olun (`ffmpeg -version`).

---

## Claude Desktop yapılandırması

`%APPDATA%\Claude\claude_desktop_config.json` dosyasına ekleyin:

```json
{
  "mcpServers": {
    "turkanime": {
      "command": "C:\\Users\\<siz>\\AppData\\Local\\Programs\\Python\\Python311\\python.exe",
      "args": ["C:\\path\\to\\mcp-server-turkanime-indirici\\turkanime-mcp\\turkanime_mcp.py"],
      "env": {
        "TURKANIME_OUTPUT_DIR": "D:\\Anime",
        "TURKANIME_MAX_WORKERS": "3"
      }
    }
  }
}
```

> **Önemli:** `args` olarak modül adı (`-m turkanime_mcp`) yerine **script'in tam yolunu** verin.
> Claude Desktop `cwd`'yi güvenilir uygulamadığından `-m` `No module named turkanime_mcp` hatası verebilir.

Kaydedip Claude Desktop'ı **tamamen kapatıp yeniden açın** (tepsiden Quit). Ardından örneğin:

> "One Piece'i ara, ilk 3 bölümü indir."

---

## Ortam değişkenleri

| Değişken | Varsayılan | Açıklama |
|----------|-----------|----------|
| `TURKANIME_OUTPUT_DIR` | *(yok)* | Varsayılan indirme kök klasörü (kullanıcıya özel) |
| `TURKANIME_MAX_WORKERS` | `1` | Eşzamanlı indirme **varsayılanı** (tool'da `max_workers` verilmezse bu kullanılır) |
| `TURKANIME_SOURCE_ATTEMPTS` | `3` | Bir bölüm için denenecek farklı kaynak (player) sayısı; `1` = sadece ilk kaynak, retry yok |
| `TURKANIME_RESUME_ATTEMPTS` | `1` | Aynı kaynakta "kaldığı yerden devam" deneme sayısı; `0` = resume kapalı |
| `TURKANIME_WORKER_POOL` | `8` | Thread havuzu üst sınırı (`max_workers` bunu aşamaz) |
| `TURKANIME_STATE_DIR` | `%PROGRAMDATA%\turkanime-mcp` | İş durumunun (`jobs.json`) yazılacağı klasör; yoksa `~/.turkanime-mcp` |
| `TURKANIME_STATE_FLUSH_SECS` | `1.0` | Ara ilerleme güncellemelerinin diske yazılma aralığı (debounce). `status` değişimi her hâlükârda anında yazılır |
| `TURKANIME_MIN_VALID_BYTES` | `1048576` (1 MB) | `verify_library`/`skip_existing` için "geçerli bölüm" eşiği; altındaki son dosyalar `partial` sayılır |
| `TURKANIME_SEARCH_RETRIES` | `2` | `search_anime` deneme sayısı (boş sonuç da yeniden denenir) |
| `TURKANIME_SEARCH_BACKOFF` | `0.5` | Arama denemeleri arası bekleme temeli (saniye): 0.5s, 1.0s, … |
| `TURKANIME_MANIFEST` | *(otomatik)* | `manifest.json` yolu; verilmezse depodaki kopya okunur |
| `CURL_CA_BUNDLE` | *(otomatik)* | ASCII CA sertifika yolu (sunucu gerekirse kendi ayarlar) |

---

## Sorun giderme

> **İlk adım:** Claude'a *"health_check çalıştır"* deyin. Erişim, `ffmpeg`, indirme klasörü,
> CA bundle ve durum klasörünü tek seferde kontrol edip `ok`/`uyarı`/`hata` raporlar.

**`No module named turkanime_mcp`** → Config'de `-m turkanime_mcp` yerine script'in tam yolunu verin.

**SSL hatası `curl: (77) error setting certificate verify locations`** → Windows kullanıcı adınız
ASCII-dışı karakter içeriyorsa (örn. `Türker Yakup`), libcurl certifi'nin `cacert.pem`'ini açamaz.
Sunucu bunu **otomatik** çözer: sertifikayı `%PROGRAMDATA%\turkanime-mcp\cacert.pem`'e kopyalayıp
`CURL_CA_BUNDLE`'a işaret eder. Yine de sorun olursa config'e elle ekleyin:
`"env": { "CURL_CA_BUNDLE": "C:\\ProgramData\\turkanime-mcp\\cacert.pem" }`

**Kaynak bulunamıyor / indirme tamamlanmıyor** → Bir kaynak akışı yarıda keserse iş önce **aynı
kaynakta kaldığı yerden devam** etmeyi dener (`TURKANIME_RESUME_ATTEMPTS`); ilerleme olmazsa yarım
dosyayı silip **sıradaki kaynağı** dener (`TURKANIME_SOURCE_ATTEMPTS` kadar). Hepsi başarısız olursa
iş `error` olur ve denenen kaynaklar mesajda listelenir — sonra `retry_job(job_id)` ile yeniden
deneyebilirsin. Site HTML/regex değişirse `pip install -U turkanime-cli` ile güncelleyin.

**Arama boş dönüyor** → `search_anime` artık "eşleşme yok" ile "siteye ulaşılamadı"yı ayırır.
`{"error": …, "retryable": true}` görüyorsan site/bağlantı sorunudur (tekrar dene);
`{"results": [], "note": "Eşleşme yok"}` ise gerçekten sonuç yoktur. TürkAnime erişilemezse
`manifest.json`'daki tanıma göre **AnimeDepo**'ya düşülür (`provider` alanı hangisinin
kullanıldığını söyler).

**Bölümler eksik/yarım kalmış** → `verify_library("<slug>")` ile durumu görün
(`ok`/`partial`/`missing`), `repair=True` ile onarın. Eşiği `TURKANIME_MIN_VALID_BYTES` ayarlar.

**Restart sonrası işler kayboluyor** → Kaybolmaz: `jobs.json`'a yazılır. `download_status()`
önceki oturumun işlerini de gösterir; yarıda kalanlar `interrupted`'tır ve `retry_job` ile
kurtarılabilir. Kalıcılık çalışmıyorsa `health_check`'te `state_dir` uyarısına bakın.

**İndirme çok yavaş** → Kaynaklar stream başına hız sınırlayabilir (~200 KiB/s). İnternetin
destekliyorsa `max_workers`'ı artırarak (ör. `3`–`6`) toplam hızı yükseltebilirsin; ya da
`max_resolution=false` ile daha küçük/daha hızlı (SD) dosya indirebilirsin.

**Log nerede?** Sunucu tüm log'ları **stderr**'e yazar (stdout MCP protokolüne ait). Claude Desktop:
`%APPDATA%\Claude\logs\mcp-server-turkanime.log`.

---

## Mimari (kısa)

```
Claude Desktop  ──stdio──▶  turkanime_mcp.py (FastMCP)
                               │
                               ├─ search_anime    ─▶ Anime.arama_yap (retry/backoff)
                               │                     └─ boş/hata → AnimeDepo (manifest'e göre)
                               ├─ list_episodes   ─▶ Anime.fetch_info + bolumler
                               ├─ verify_library  ─▶ klasör tarama (ok/partial/missing)
                               │                     └─ repair → _start_downloads
                               └─ download_*      ─▶ ThreadPoolExecutor + paralel kapı (max_workers)
                                                      └─ kaynak seç (BETA'lar sona) ─▶ Video.indir (yt-dlp)
                                                           ├─ tamamlanma diskten doğrulanır
                                                           ├─ eksikse → aynı kaynakta resume
                                                           ├─ ilerlemezse → farklı kaynakla retry
                                                           └─ finalize: düzenli klasöre taşı + adlandır
                               │
                               └─ JOBS (RAM) ──debounce──▶ jobs.json (kalıcı; açılışta geri yüklenir)
```

- İş durumu paylaşımlı bir `JOBS` sözlüğünde tutulur; `download_status` bunu okur ve
  `jobs.json`'a kalıcılaştırılır (açılışta geri yüklenir, yarıda kalanlar `interrupted`).
- `Anime` nesneleri slug bazında önbelleğe alınır (tekrar `fetch_info` maliyetini önler).
- Eşzamanlılık **dinamik bir kapı** ile sınırlanır; her indirme çağrısı kendi `max_workers`'ını seçer.
- Kaynak seçiminde kararsız **BETA** player'lar en sona atılır; yt-dlp `ignoreerrors` yüzünden
  yarıda kesilen indirmeyi "bitti" sanmamak için tamamlanma **dosya sisteminden** doğrulanır.
- **Resume yalnızca aynı player'da** yapılır: farklı player = farklı URL olduğundan yarım veri
  devralınırsa bozuk dosya oluşur. Bu yüzden iş başlarken ve kaynak değişiminde `.part` temizlenir.
- **Kilit düzeni:** `JOBS` → `_JOBS_LOCK`, anime önbelleği → `_ANIME_LOCK`, seri hafızası →
  `_SERIES_LOCK`, durum dosyası G/Ç → `_STATE_LOCK` (yaprak kilit; tutulurken başka kilit alınmaz).

---

## Testler

Ağ ya da `turkanime_api` gerektirmeyen birim testler (stdlib `unittest`, ek bağımlılık yok):

```powershell
python -m unittest discover -s turkanime-mcp/tests -t turkanime-mcp/tests
```

Kapsam: `verify_library` tarama mantığı (adlandırma biçimleri, Windows büyük/küçük harf
duyarsızlığı, eşik), durum kalıcılığı (`interrupted` işaretleme, bozuk dosya), resume akışı,
arama fallback'i, `skip_existing`, `get_batch_summary`, `retry_job`, jellyfin adlandırma ve
`check_new_episodes`.

---

## Kısıtlar / Notlar

- Yalnızca **kişisel kullanım** içindir; mevcut `turkanime_api` kütüphanesini sarar, kimlik
  doğrulama/paywall aşımı vb. **eklemez**.
- İnteraktif `turkanime` TUI'si otomasyona uygun olmadığından kullanılmaz; Python API doğrudan çağrılır.
- `arama_yap`, `fetch_info`, `best_video` ağ çağrılarıdır; yavaş olabilirler.

---

## Teşekkür

Bu MCP sunucusu, [**KebabLord/turkanime-indirici**](https://github.com/KebabLord/turkanime-indirici)
(`turkanime-cli`) projesinin üzerine kuruludur; tüm indirme/bypass mantığı o pakete aittir.
Alt paket **CC BY-NC-ND 4.0** lisanslıdır (ticari olmayan kullanım) — bkz. [`LICENSE`](LICENSE) ve
[`DISCLAIMER.md`](DISCLAIMER.md). Bu depo, o projeyi saran bir MCP arayüzü ekler.
