## turkanime-mcp v1.1.0 — AnimeDepo arşivine geçiş

> [!WARNING]
> TürkAnime 19.09.2026'da kapandı. Bu sürümle MCP sunucusu, upstream'in "B planı" olan
> **AnimeDepo metadata arşivini** kullanıyor. Yalnızca **Eylül 2026 öncesi** yüklenmiş
> bölümlere erişilebilir. Yeni bölüm gelmez. 3. parti sitelerden silinen videolar zamanla kaybolabilir.

### Değişiklikler
- **Upstream senkronu:** `kebablord/turkanime-indirici` → "TürkAnime Arşiv'e geçiş yap"
  (`manifest.json`: `force_fallback: true`)
- **Bölüm listeleme ve indirme artık AnimeDepo'dan:** `list_episodes`, `list_fansubs`,
  `download_*`, `verify_library`, `check_new_episodes` aramayla aynı kaynağı kullanır
  (önceden yalnızca arama AnimeDepo'ya düşüyordu, bölümler kapanan siteden isteniyordu)
- **Yeni env:** `TURKANIME_PROVIDER` = `animedepo` | `turkanime` (verilmezse manifest belirler)
- **`health_check`:** `turkanime.tv` kontrolünün yerini aktif sağlayıcıyı sınayan `kaynak` kontrolü aldı

### Yükseltme
```powershell
git pull
```
Bağımlılık değişikliği yok. Paket olarak kurduysanız (`pip install .`) `turkanime-mcp` klasöründe
tekrar `pip install .` çalıştırın. Ardından Claude Desktop'ı yeniden başlatın.
