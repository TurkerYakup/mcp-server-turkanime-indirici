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
- 🧵 İndirmeler **arka planda** sürer; kaç bölümün **paralel** ineceğini seçebilirsin (`max_workers`, varsayılan tek tek).
- 🔁 **Güvenilir tamamlanma:** yarıda kesilen indirme "bitti" sanılmaz — diske bakılıp doğrulanır ve
  otomatik olarak **farklı bir kaynak** denenir. Kararsız **BETA** kaynakları (ör. `ALUCARD(BETA)`) en sona atılır.
- 🗂️ Her anime için **otomatik düzenli klasör** oluşturur.
- 🪟 **Windows** için tasarlandı; ASCII-dışı kullanıcı adlarındaki SSL sorununu otomatik çözer.

---

## Araçlar

| Araç | Ne yapar | Döner |
|------|----------|-------|
| `search_anime(query)` | Anime arar | `[{slug, title}]` |
| `list_episodes(anime_slug, refresh?)` | Bölümleri + özeti listeler (`refresh` yeni bölümler için) | `{title, ozet, episodes:[{index, slug, title}]}` |
| `list_fansubs(anime_slug, episode?)` | Bölümün mevcut fansub gruplarını listeler | `{anime, bolum, fansubs:[…]}` |
| `download_episodes(anime_slug, episodes, …, max_workers?)` | Seçili bölümleri **arka planda** indirir (`max_workers` ile paralel sayısı) | `{job_ids, target_dir, parallel, …}` |
| `download_season(anime_slug, …, max_workers?)` | **Tüm sezonu** asenkron indirir (`max_workers` ile paralel sayısı) | `{job_ids, target_dir, parallel, …}` |
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
- **Tüm sezon** indirmede bütün bölümler kuyruğa girer; aynı anda kaç tanesinin ineceğini
  `max_workers` belirler (verilmezse varsayılan **1 = tek tek**; `TURKANIME_MAX_WORKERS` ile
  varsayılanı değiştirebilirsin). Gerisi sırada bekler.

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
| `TURKANIME_WORKER_POOL` | `8` | Thread havuzu üst sınırı (`max_workers` bunu aşamaz) |
| `CURL_CA_BUNDLE` | *(otomatik)* | ASCII CA sertifika yolu (sunucu gerekirse kendi ayarlar) |

---

## Sorun giderme

**`No module named turkanime_mcp`** → Config'de `-m turkanime_mcp` yerine script'in tam yolunu verin.

**SSL hatası `curl: (77) error setting certificate verify locations`** → Windows kullanıcı adınız
ASCII-dışı karakter içeriyorsa (örn. `Türker Yakup`), libcurl certifi'nin `cacert.pem`'ini açamaz.
Sunucu bunu **otomatik** çözer: sertifikayı `%PROGRAMDATA%\turkanime-mcp\cacert.pem`'e kopyalayıp
`CURL_CA_BUNDLE`'a işaret eder. Yine de sorun olursa config'e elle ekleyin:
`"env": { "CURL_CA_BUNDLE": "C:\\ProgramData\\turkanime-mcp\\cacert.pem" }`

**Kaynak bulunamıyor / indirme tamamlanmıyor** → Bir kaynak akışı yarıda keserse iş **otomatik
olarak sıradaki kaynağı** dener (`TURKANIME_SOURCE_ATTEMPTS` kadar). Hepsi başarısız olursa iş
`error` olur ve denenen kaynaklar mesajda listelenir. Site HTML/regex değişirse
`pip install -U turkanime-cli` ile güncelleyin.

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
                               ├─ search_anime  ─▶ turkanime_api.Anime.arama_yap
                               ├─ list_episodes ─▶ Anime.fetch_info + bolumler
                               └─ download_*    ─▶ ThreadPoolExecutor + paralel kapı (max_workers)
                                                    └─ kaynak seç (BETA'lar sona) ─▶ Video.indir (yt-dlp)
                                                         ├─ tamamlanma diskten doğrulanır
                                                         ├─ eksikse → farklı kaynakla retry
                                                         └─ finalize: düzenli klasöre taşı + adlandır
```

- İş durumu paylaşımlı bir `JOBS` sözlüğünde tutulur; `download_status` bunu okur.
- `Anime` nesneleri slug bazında önbelleğe alınır (tekrar `fetch_info` maliyetini önler).
- Eşzamanlılık **dinamik bir kapı** ile sınırlanır; her indirme çağrısı kendi `max_workers`'ını seçer.
- Kaynak seçiminde kararsız **BETA** player'lar en sona atılır; yt-dlp `ignoreerrors` yüzünden
  yarıda kesilen indirmeyi "bitti" sanmamak için tamamlanma **dosya sisteminden** doğrulanır.

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
