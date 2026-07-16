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

Bu sunucu, TürkAnime'nin Python API'sini (`turkanime_api`) Claude Desktop'a **4 araç + 1 tüm-sezon aracı**
olarak açar. Kullanıcı sohbette isteğini yazar, Claude uygun aracı çağırır:

> "Attack on Titan'ı bul, 1. sezonun ilk 3 bölümünü indir."
>
> "Bocchi the Rock'ın **tüm sezonunu** indir."
>
> "İndirmelerin durumu ne?"

- ⚙️ **Selenium/webdriver gerektirmez** — Cloudflare bypass `curl_cffi` (Firefox TLS impersonation) ile.
- 🎞️ İndirme motoru **yt-dlp**; şifreli video URL'leri `pycryptodome` ile çözülür.
- 🧵 İndirmeler **arka planda, paralel** (thread havuzu) sürer; MCP ana thread'i kilitlenmez.
- 🗂️ Her anime için **otomatik düzenli klasör** oluşturur.
- 🪟 **Windows** için tasarlandı; ASCII-dışı kullanıcı adlarındaki SSL sorununu otomatik çözer.

---

## Araçlar

| Araç | Ne yapar | Döner |
|------|----------|-------|
| `search_anime(query)` | Anime arar | `[{slug, title}]` |
| `list_episodes(anime_slug, refresh?)` | Bölümleri + özeti listeler (`refresh` yeni bölümler için) | `{title, ozet, episodes:[{index, slug, title}]}` |
| `list_fansubs(anime_slug, episode?)` | Bölümün mevcut fansub gruplarını listeler | `{anime, bolum, fansubs:[…]}` |
| `download_episodes(anime_slug, episodes, …)` | Seçili bölümleri **arka planda** indirir | `{job_ids, target_dir, …}` |
| `download_season(anime_slug, …)` | **Tüm sezonu** asenkron indirir | `{job_ids, target_dir, …}` |
| `download_status(job_id?)` | İndirme durumunu sorgular | `[{status, percent, speed, eta, file, …}]` |
| `cancel_download(job_id)` | Bir indirmeyi iptal eder (yarım dosyaları temizler) | `{job_id, status, message}` |
| `cancel_all()` | Devam eden/kuyruktaki tüm indirmeleri iptal eder | `{cancelling, job_ids}` |
| `clear_finished_jobs()` | Biten/hatalı/iptal işleri listeden temizler | `{removed, remaining}` |

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
- **Tüm sezon** indirmede bütün bölümler aynı anda kuyruğa girer; `TURKANIME_MAX_WORKERS`
  kadarı paralel iner, gerisi sırada bekler.

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
| `TURKANIME_MAX_WORKERS` | `3` | Eşzamanlı indirme (thread havuzu) sayısı |
| `CURL_CA_BUNDLE` | *(otomatik)* | ASCII CA sertifika yolu (sunucu gerekirse kendi ayarlar) |

---

## Sorun giderme

**`No module named turkanime_mcp`** → Config'de `-m turkanime_mcp` yerine script'in tam yolunu verin.

**SSL hatası `curl: (77) error setting certificate verify locations`** → Windows kullanıcı adınız
ASCII-dışı karakter içeriyorsa (örn. `Türker Yakup`), libcurl certifi'nin `cacert.pem`'ini açamaz.
Sunucu bunu **otomatik** çözer: sertifikayı `%PROGRAMDATA%\turkanime-mcp\cacert.pem`'e kopyalayıp
`CURL_CA_BUNDLE`'a işaret eder. Yine de sorun olursa config'e elle ekleyin:
`"env": { "CURL_CA_BUNDLE": "C:\\ProgramData\\turkanime-mcp\\cacert.pem" }`

**`best_video` kaynak bulamıyor** → Bölümün ilgili işi `error` ("çalışan kaynak bulunamadı") olur;
başka bölüm/fansub deneyin. Site HTML/regex değişirse `pip install -U turkanime-cli` ile güncelleyin.

**Log nerede?** Sunucu tüm log'ları **stderr**'e yazar (stdout MCP protokolüne ait). Claude Desktop:
`%APPDATA%\Claude\logs\mcp-server-turkanime.log`.

---

## Mimari (kısa)

```
Claude Desktop  ──stdio──▶  turkanime_mcp.py (FastMCP)
                               │
                               ├─ search_anime  ─▶ turkanime_api.Anime.arama_yap
                               ├─ list_episodes ─▶ Anime.fetch_info + bolumler
                               └─ download_*    ─▶ ThreadPoolExecutor
                                                    └─ Bolum.best_video ─▶ Video.indir (yt-dlp)
                                                         └─ finalize: düzenli klasöre taşı + adlandır
```

- İş durumu paylaşımlı bir `JOBS` sözlüğünde tutulur; `download_status` bunu okur.
- `Anime` nesneleri slug bazında önbelleğe alınır (tekrar `fetch_info` maliyetini önler).

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
