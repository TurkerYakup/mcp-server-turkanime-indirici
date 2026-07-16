# TürkAnime İndirici — MCP Sunucusu

`turkanime-cli` ([KebabLord/turkanime-indirici](https://github.com/KebabLord/turkanime-indirici))
paketini saran, **stdio tabanlı bir MCP sunucusu**. Claude Desktop'a eklendiğinde,
"şu animenin şu bölümlerini indir" dediğinizde Claude arka planda indirip klasörler.

Bu sürüm **Selenium/webdriver kullanmaz**; Cloudflare bypass `curl_cffi` ile yapılır.
İndirme motoru **yt-dlp**'dir. Sadece kişisel kullanım içindir — mevcut kütüphaneyi sarar.

## Araçlar

| Araç | Ne yapar |
|------|----------|
| `search_anime(query)` | Anime arar → `[{slug, title}]` |
| `list_episodes(anime_slug)` | Bölümleri listeler → `{title, ozet, episodes:[{index, slug, title}]}` |
| `download_episodes(anime_slug, episodes, output_dir?, subfolder?, fansub?, max_resolution?, rename?)` | Seçili bölümleri **arka planda** indirir, hemen `job_id`'lerle döner |
| `download_season(anime_slug, output_dir?, subfolder?, fansub?, max_resolution?, rename?)` | **Tüm sezonu** (slug'ın tüm bölümlerini) asenkron indirir |
| `download_status(job_id?)` | İndirme durumunu sorgular |

`episodes` esnektir (index'ler `list_episodes`'daki 0-tabanlı `index`'tir):

- tek index: `3` veya `"3"`
- bölüm slug'ı: `"one-piece-3-bolum"`
- aralık: `"0-11"` (ilk 12 bölüm)
- virgüllü/karışık: `"0,1,2,5-8"`
- liste: `[0, 1, 2]`
- **tüm sezon**: `"all"` (ya da `download_season` aracı)

### İndirme klasörü ve düzeni

- **Kök klasör** kullanıcıya özeldir: config'de `env → TURKANIME_OUTPUT_DIR` ile
  belirlenir (örn. `D:\Anime` veya `...\Masaüstü\Anime`). `output_dir` parametresi
  verilmezse bu varsayılan kullanılır; ikisi de yoksa araç uyarır.
- Sunucu, kök klasörün içinde **anime/sezon için otomatik alt klasör** açar ve
  dosyayı oraya taşır:
  `<kök>/<Anime Başlığı>/<Anime Başlığı> - NNN.ext`
  (örn. `D:\Anime\Shingeki no Kyojin\Shingeki no Kyojin - 001.mp4`).
  Alt klasör adını `subfolder` parametresiyle elle de verebilirsin.
- `rename=False` verilirse taşıma/adlandırma yapılmaz (ham `anime_slug/bolum_slug.ext`).
- **Tüm sezon** indirmede tüm bölümler aynı anda kuyruğa girer; `TURKANIME_MAX_WORKERS`
  kadarı paralel iner, gerisi sırada bekler.

## Kurulum (Windows)

Python **3.10+** gerekir.

```powershell
cd C:\path\to\turkanime-mcp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> **pywin32 notu:** Windows'ta resmi `mcp` SDK'sinin stdio katmanı `pywintypes`
> modülüne ihtiyaç duyar; bu `pywin32` ile gelir ve `requirements.txt` içinde vardır.
> Kurulumdan sonra gerekirse: `python -m pywin32_postinstall -install`.

### ffmpeg (önerilir)

Bazı formatların (ayrı video/ses parçaları) birleştirilmesi için **ffmpeg** gerekir.

```powershell
winget install Gyan.FFmpeg
```

Kurulumdan sonra `ffmpeg`'in `PATH`'te olduğundan emin olun (`ffmpeg -version`).

## Çalıştırma / Test

```powershell
# Söz dizimi kontrolü
python -m py_compile turkanime_mcp.py

# Sunucuyu doğrudan çalıştır (stdio bekler; Ctrl+C ile çık)
python -m turkanime_mcp
```

Sunucu stdio üzerinden konuşur ve tüm log'ları **stderr**'e yazar (stdout MCP
protokolüne ayrılmıştır).

## Claude Desktop yapılandırması

`claude_desktop_config.json` dosyasına ekleyin
(`%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "turkanime": {
      "command": "C:\\Users\\<siz>\\AppData\\Local\\Programs\\Python\\Python311\\python.exe",
      "args": ["C:\\path\\to\\turkanime-mcp\\turkanime_mcp.py"],
      "env": {
        "TURKANIME_OUTPUT_DIR": "D:\\Anime",
        "TURKANIME_MAX_WORKERS": "3"
      }
    }
  }
}
```

> **Önemli:** `args` olarak modül adı (`-m turkanime_mcp`) yerine **script'in tam
> yolunu** verin. Claude Desktop `cwd`'yi güvenilir uygulamadığından `-m`
> `No module named turkanime_mcp` hatası verebilir.

`TURKANIME_OUTPUT_DIR` sizin indirme kök klasörünüzdür — istediğiniz sürücü/klasör
(örn. `D:\Anime`, `C:\İndirilenler`, masaüstünüz). Sunucu her anime için bunun
içinde ayrı bir alt klasör açar.

Kaydedip Claude Desktop'ı yeniden başlatın. Ardından örneğin:

> "One Piece'i ara, ilk 3 bölümü `D:\Anime` klasörüne indir."

## Ortam değişkenleri

| Değişken | Varsayılan | Açıklama |
|----------|-----------|----------|
| `TURKANIME_OUTPUT_DIR` | *(yok)* | Varsayılan indirme kök klasörü (kullanıcıya özel) |
| `TURKANIME_MAX_WORKERS` | `3` | Eşzamanlı indirme (thread havuzu) sayısı |
| `CURL_CA_BUNDLE` | *(otomatik)* | ASCII CA sertifika yolu (sunucu gerekirse kendi ayarlar) |

## Sorun giderme

**SSL hatası `curl: (77) error setting certificate verify locations`:**
Windows kullanıcı adınız ASCII-dışı karakter içeriyorsa (örn. `Türker Yakup`),
libcurl certifi'nin `cacert.pem`'ini açamaz. Sunucu bunu **otomatik** çözer:
sertifikayı ASCII bir yola (`%PROGRAMDATA%\turkanime-mcp\cacert.pem`) kopyalayıp
`CURL_CA_BUNDLE`'a işaret eder. Yine de sorun yaşarsanız config'e elle env ekleyin:

```json
"env": { "CURL_CA_BUNDLE": "C:\\ProgramData\\turkanime-mcp\\cacert.pem" }
```

## Notlar / Bilinen davranışlar

- `arama_yap`, `fetch_info`, `best_video` ağ çağrılarıdır; yavaş olabilirler.
- `best_video` hiçbir kaynak çalışmıyorsa `None` döner → ilgili işin durumu `error`
  ("Bu bölüm için çalışan kaynak bulunamadı.") olur.
- Site HTML/regex değişirse `turkanime-cli` kırılabilir; paketi güncelleyin
  (`pip install -U turkanime-cli`) — bu sunucu kurulu paketi import eder, vendorlamaz.
- İndirme `indir()` blocking olduğundan ayrı thread havuzunda çalışır; MCP ana
  thread'i asla kilitlenmez.
