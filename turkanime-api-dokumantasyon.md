# TürkAnime API (`turkanime_api`) — Geliştirici Dokümantasyonu

> Kaynak: [KebabLord/turkanime-indirici](https://github.com/KebabLord/turkanime-indirici)
> Paket: **`turkanime-cli`** — sürüm **10.0.4** — Python **>=3.10, <4**
> Bu doküman, deponun `master` dalındaki `turkanime_api/objects.py` ve `turkanime_api/bypass.py` kaynak kodundan doğrulanarak çıkarılmıştır. İmzalar birebir kaynaktan alınmıştır.

Bu belgenin amacı: `turkanime_api`'yi bir MCP sunucusundan **programatik** olarak (interaktif `turkanime` TUI'sini kullanmadan) çağırabilmek için gereken tüm sınıf/metod bilgisini vermek.

---

## 0. Genel bakış ve mimari

- Site: `https://turkanime.tv/` (Cloudflare arkasında).
- **Bypass artık Selenium/webdriver kullanmıyor.** Bunun yerine `curl_cffi` ile **Firefox TLS fingerprint impersonation** yapılıyor (`turkanime_api/bypass.py`). Yani webdriver, geckodriver, Chrome vs. **gerekmiyor** — sadece Python + bağımlılıklar yeterli.
- İndirme motoru: **yt-dlp** (curl-cffi eklentisiyle). Bazı player'lar için `impersonate` kullanılıyor.
- Video URL'leri şifreli geliyor; `pycryptodome` ile çözülüyor (`get_real_url`, `unmask_real_url`).

### Bağımlılıklar (`pyproject.toml`)
```
yt-dlp[default,curl-cffi] >= 2025.10.22
curl-cffi              >= 0.13
pycryptodome
appdirs
questionary
rich                   >= 13.0.0
easygui                >= 0.98.2
```

### Konsol komutu (MCP için KULLANMA)
`turkanime` komutu → `turkanime_api.cli.__main__:main`. Bu **interaktif TUI**'dir (questionary/easygui pencereleri açar), otomasyon için uygun değildir. MCP, aşağıdaki Python API'sini **doğrudan** çağırmalıdır.

---

## 1. Paket girişi

```python
# turkanime_api/__init__.py
from .objects import Anime, Bolum, Video
from .bypass import session
```

Kullanım:
```python
import turkanime_api as ta
ta.Anime, ta.Bolum, ta.Video
```

`bypass` modülü ayrıca şunları sağlar (gerektiğinde `from turkanime_api.bypass import fetch, get_real_url, unmask_real_url`):
- `fetch(path, headers={}, data=None)` — curl_cffi tabanlı GET/POST; Cloudflare'i geçer. İlk çağrıda `session`'ı kurar. `path` "/" ile başlıyorsa `BASE_URL`'e eklenir.
- `get_real_url(cipher)` — şifreli video path'ini çözer.
- `unmask_real_url(url, video=...)` — turkanime içi maskeli URL'leri açar.

Bu fonksiyonları MCP'de doğrudan çağırmana genelde **gerek yok**; sınıflar arka planda kullanıyor. Sadece hata ayıklama için faydalı.

---

## 2. `Anime` sınıfı

```python
class Anime:
    def __init__(self, slug, parse_fansubs=True): ...
```

Bir animeyi `slug`'ıyla temsil eder (slug = URL'deki tanımlayıcı, örn. `one-piece`).

### 2.1 Arama — `Anime.arama_yap(query)`  ⭐ (staticmethod)
```python
@staticmethod
def arama_yap(query):
    src = fetch("/arama", data={"arama": query})
    res = re.findall(r'/anime/([^"\'>]+)["\'] [^>]*?title=["\']([^"]+?) izle', src)
    results = [ (slug, unescape(isim_)) for slug, isim_ in res ]
    ...
```
- **Girdi:** arama metni (`str`).
- **Çıktı:** `[(slug, başlık), ...]` — tuple listesi. Örn. `[("one-piece", "One Piece"), ...]`.
- Aramanın giriş noktası budur. MCP'nin `search_anime` aracı bunu çağırmalı.

### 2.2 Metadata — `fetch_info()`
```python
def fetch_info(self):
    """Anime detay sayfasını ayrıştır."""
    src = fetch(f'/anime/{self.slug}')
    ... self.info["Resim"], self.anime_id = twitmeta ...
```
- `self.info` (dict) ve `self.anime_id`'yi doldurur.
- `self.info` anahtarları **Türkçe**: `"Resim"` (kapak görseli URL), `"Puanı"` (float), `"Anime Türü"` (list), `"Özet"`, vb.
- **Önemli:** `get_bolum_listesi()` `self.anime_id`'ye ihtiyaç duyar; `anime_id` `fetch_info()` içinde set edilir. Bölüm listesi almadan önce `fetch_info()` çağrılmalı (veya `bolumler` property'si bunu tetikleyecek şekilde kullanılmalı — güvenli olması için önce `fetch_info()` çağır).

### 2.3 Özellikler
| Üye | Tip | Açıklama |
|-----|-----|----------|
| `slug` | str | Anime tanımlayıcı |
| `title` | str (property) | Görünen ad (`fetch_info` doldurur) |
| `anime_id` | int | Site içi ID (başlangıç 0, `fetch_info` set eder) |
| `info` | dict | Metadata (Türkçe anahtarlar) |
| `bolumler` | list[`Bolum`] (property) | Bölüm listesi (lazy — ilk erişimde kurulur) |

### 2.4 `bolumler` property
```python
@property
def bolumler(self):
    if not self._bolumler:
        for slug, title in self.get_bolum_listesi():
            self._bolumler.append(
                Bolum(slug=slug, title=title, anime=self,
                      parse_fansubs=self.parse_fansubs))
    return self._bolumler
```
- `Bolum` nesnelerinin listesini döndürür. İlk erişimde `get_bolum_listesi()` çağrılır.

### 2.5 `get_bolum_listesi()`
```python
def get_bolum_listesi(self):
    anime_id = self.anime_id
    src = fetch(f'/ajax/bolumler&animeId={anime_id}')
    return re.findall(r'\/video\/(.*?)\\?".*?title=\\?"(.*?)\\?" style=', src)
```
- **Çıktı:** `[(bolum_slug, bolum_title), ...]`.
- `anime_id` gerektirir → önce `fetch_info()`.

### 2.6 `get_anime_listesi()` (staticmethod)
Sitedeki tüm anime listesini döndürür (arama yerine tam liste gerekirse). MCP için genelde `arama_yap` yeterli.

---

## 3. `Bolum` sınıfı (bölüm/episode)

```python
class Bolum:
    def __init__(self, slug, anime=None, title=None, parse_fansubs=True): ...
```

### 3.1 Özellikler
| Üye | Tip | Açıklama |
|-----|-----|----------|
| `slug` | str | Bölüm tanımlayıcı |
| `title` | str (property) | Bölüm adı |
| `anime` | `Anime` (property) | Bağlı anime |
| `videos` | list[`Video`] (property) | Bu bölüm için tüm video kaynakları |
| `fansubs` | (property) | Fansub grup bilgisi |

### 3.2 `best_video(...)`  ⭐ — en iyi kaynağı seçer
```python
def best_video(self, by_res=True, by_fansub=None, default_res=600,
               callback=lambda x: None):
    ...
```
- `by_res=True`: çözünürlüğe göre seç (1080p bulursa hemen döndürür, yoksa çalışan en yüksek çözünürlüğü).
- `by_fansub`: tercih edilen fansub adı (öncelik verir). `None` ise fansub filtresi yok.
- `default_res`: çözünürlük tespit edilemezse varsayılan.
- `callback(dict)`: ilerleme/durum bildirimi. Dict alanları: `current`, `total`, `player`, `status` (örn. `"üstbilgi çekiliyor"`, `"çalışıyor"`, `"çalışmıyor"`, `"hiçbiri çalışmıyor"`).
- **Çıktı:** çalışan bir `Video` **veya** hiçbiri çalışmıyorsa `None`.
- İç mantık: player'ları `SUPPORTED` öncelik sırasına göre (ve `by_fansub` tercihine göre) sıralar, çalışanı bulup ≥1080p ise hemen, değilse en yüksek çözünürlüklü çalışanı döndürür.

---

## 4. `Video` sınıfı

```python
class Video:
    def __init__(self, bolum, path, player=None, fansub=None,
                 log_handler=LogHandler): ...
```

### 4.1 Özellikler
| Üye | Tip | Açıklama |
|-----|-----|----------|
| `player` | str | Kaynak/host adı (örn. `SIBNET`, `GDRIVE`) |
| `fansub` | str | Fansub grubu |
| `is_supported` | bool | `player in SUPPORTED` |
| `url` | str (property) | Çözülmüş **doğrudan** video URL'si (şifre çözme burada) |
| `info` | dict (property) | yt-dlp `extract_info` çıktısı |
| `resolution` | int (property) | Yükseklik (px), tespit edilemezse 0 |
| `is_working` | bool (property) | Video gerçekten oynatılabiliyor/çekilebiliyor mu |

> `url`, `info`, `is_working`, `resolution` erişimi **ağ isteği + yt-dlp çağrısı** tetikler (yavaş olabilir). `best_video` bunları zaten deneyerek çalışan videoyu seçer; MCP genelde `best_video` sonucunu doğrudan indirmeli.

### 4.2 `indir(...)`  ⭐ — indirme
```python
def indir(self, callback=None, output=""):
    assert self.is_working, "Video çalışmıyor."
    seri_slug = self.bolum.anime.slug if self.bolum.anime else ""
    output = join(output, seri_slug, self.bolum.slug)
    opts = self.ydl_opts.copy()
    if callback:
        opts['progress_hooks'] = [callback]
    opts['outtmpl'] = {'default': output + r'.%(ext)s'}
    ...
    with YoutubeDL(opts) as ydl:
        ydl.download_with_info_file(tmp.name)
```
- **`output` = TABAN klasör.** Gerçek dosya yolu **otomatik** olarak şöyle kurulur:
  ```
  <output>/<anime_slug>/<bolum_slug>.<ext>
  ```
  Yani `output=r"C:\İndirilenler"` verirsen dosya `C:\İndirilenler\one-piece\one-piece-1-bolum.mp4` gibi iner. **Uzantı (ext) yt-dlp tarafından belirlenir** (indirme bitene kadar kesin bilinmez, genelde `.mp4`).
- `callback`: yt-dlp `progress_hooks` fonksiyonu. Her çağrıda bir dict alır: `status` (`"downloading"`/`"finished"`), `downloaded_bytes`, `total_bytes` (veya `total_bytes_estimate`), `speed`, `eta`, `filename` vb.
- **Blocking'tir** — bitene kadar döner. Arka plan için ayrı thread/executor'da çalıştır (bkz. §6).
- Ön koşul: `self.is_working` True olmalı (aksi halde `AssertionError`). `best_video`'dan gelen video zaten çalışıyordur.

### 4.3 `oynat(...)` — (MCP için gereksiz)
```python
def oynat(self, dakika_hatirla=False, izlerken_kaydet=False, mpv_opts=[]): ...
```
mpv ile oynatır. İndirme MCP'si için gerekmez.

---

## 5. Sabitler ve config

### 5.1 `SUPPORTED` (player öncelik sırası)
`best_video` bu sıraya göre tercih yapar (üstteki daha öncelikli):
```python
SUPPORTED = [
    "YADISK", "ALUCARD(BETA)", "GDRIVE", "MAIL", "PIXELDRAIN",
    "AMATERASU(BETA)", "HDVID", "ODNOKLASSNIKI", "DAILYMOTION",
    "SIBNET", "VK", "VIDMOLY", "YOURUPLOAD", "SENDVID", "MYVI", "UQLOAD",
]
```

### 5.2 CLI config (referans — MCP kendi ayarını tutmalı)
Resmi TUI `appdirs` ile config tutar. İlgili anahtarlar: `"indirilenler"` (indirme klasörü), `"paralel indirme sayisi"` (eşzamanlı indirme), `"max resolution"`. MCP bu config'e bağlı kalmamalı; kendi `output_dir`'ini parametre olarak almalı.

### 5.3 CLI'nin toplu/paralel indirme deseni (referans)
```python
paralel = dosya.ayarlar.get("paralel indirme sayisi")
with cf.ThreadPoolExecutor(max_workers=paralel) as executor:
    for bolum in bolumler:
        futures.append(executor.submit(indirme_task_cli, bolum, board, dosya, sub))
    cf.wait(futures)

# task içinde:
best_video = bolum.best_video(
    by_res=dosya.ayarlar["max resolution"],
    by_fansub=sub,
    callback=vid_cli.callback)
best_video.indir(...)
```
MCP de aynı deseni (ThreadPoolExecutor + `best_video` → `indir`) kullanmalı.

---

## 6. MCP için tam kullanım örneği (uçtan uca)

```python
import turkanime_api as ta

# 1) ARAMA
sonuclar = ta.Anime.arama_yap("one piece")   # -> [(slug, baslik), ...]

# 2) ANIME SEÇ + METADATA
anime = ta.Anime(sonuclar[0][0])              # slug ile
anime.fetch_info()                            # info + anime_id doldurur
print(anime.title, anime.info.get("Özet"))

# 3) BÖLÜMLERİ LİSTELE
for i, bolum in enumerate(anime.bolumler):    # -> list[Bolum]
    print(i, bolum.slug, bolum.title)

# 4) BİR BÖLÜMÜ İNDİR
bolum = anime.bolumler[0]

def ilerleme(d):
    if d.get("status") == "downloading":
        print(d.get("_percent_str"), d.get("_speed_str"))
    elif d.get("status") == "finished":
        print("bitti:", d.get("filename"))

video = bolum.best_video(by_res=True, by_fansub=None)  # -> Video | None
if video:
    video.indir(callback=ilerleme, output=r"C:\İndirilenler")
    # Dosya: C:\İndirilenler\<anime_slug>\<bolum_slug>.<ext>
else:
    print("Bu bölüm için çalışan kaynak bulunamadı.")
```

---

## 7. MCP tasarımında dikkat edilecekler (tuzaklar)

1. **`fetch_info()` şart.** `anime_id` set edilmeden `bolumler`/`get_bolum_listesi` boş döner. Anime nesnesi kurar kurmaz `fetch_info()` çağır.
2. **Klasör yapısı zaten iç içe.** `indir(output=...)` verilen tabanın altına `anime_slug/bolum_slug.ext` açar. Kullanıcı "klasörü ben seçeyim, düzeni Claude belirlesin" dediyse: taban klasörü parametre al, indirdikten sonra istersen `anime_slug` klasörünü okunur başlığa (`anime.title`) veya bölümleri `Sezon`/numaraya göre **yeniden adlandır/taşı** (yt-dlp bitince dosya yolunu `finished` hook'undaki `filename`'den öğrenirsin).
3. **Uzantı önceden bilinmez.** Çıktı yolunu `finished` hook'undaki `filename` ile kesinleştir; kendi kaydını buna göre tut.
4. **`indir` blocking.** Arka plan indirme için `concurrent.futures.ThreadPoolExecutor` kullan, her işe bir `job_id` ver, ilerlemeyi `progress_hooks` callback'inde paylaşımlı bir dict'e yaz; `download_status` aracı bu dict'i okusun.
5. **`best_video` `None` dönebilir.** Hiçbir kaynak çalışmıyorsa nazikçe hata döndür.
6. **Yavaş çağrılar.** `arama_yap`, `fetch_info`, `bolumler`, `best_video` hepsi ağ ister; MCP araçlarında makul timeout ve hata yakalama koy.
7. **Kırılganlık.** Site HTML/regex değişirse `objects.py` bozulabilir; MCP'yi `turkanime-cli` güncellemelerine karşı esnek tut (paketi güncelleyince çalışsın; kendi kopyanı forklamak yerine kurulu paketi import et).
8. **Bağımlılık.** yt-dlp'nin `curl-cffi` eklentisi kurulu olmalı (`pip install "turkanime-cli"` bunları getirir). Windows'ta ayrıca ffmpeg önerilir (bazı formatların birleştirilmesi için).

---

## 8. Önerilen MCP arayüzü (araç sözleşmesi)

| Araç | Girdi | Çıktı | Altında ne çağrılır |
|------|-------|-------|---------------------|
| `search_anime` | `query: str` | `[{slug, title}]` | `Anime.arama_yap(query)` |
| `list_episodes` | `anime_slug: str` | `{title, info, episodes:[{index, slug, title}]}` | `Anime(slug)`, `fetch_info()`, `bolumler` |
| `download_episodes` | `anime_slug: str`, `episodes: (slug/index listesi veya "1-12" aralığı)`, `output_dir: str`, `fansub?: str`, `max_resolution?: bool` | `{job_ids: [...]}` (arka planda başlar) | `best_video(...)` + `indir(...)` (ThreadPoolExecutor) |
| `download_status` | `job_id?: str` | `[{job_id, anime, bolum, status, percent, speed, eta, file, error}]` | paylaşımlı ilerleme dict'i |

`episodes` parametresi hem tek bölüm, hem liste, hem `"5-10"` aralığı kabul etmeli. `download_episodes` çağrısı **hemen** `job_id`'lerle dönmeli; gerçek indirme thread havuzunda sürmeli.
