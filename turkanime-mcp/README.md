# turkanime-mcp

MCP sunucusunun kaynak kodu burada: [`turkanime_mcp.py`](turkanime_mcp.py).

Kurulum, Claude Desktop yapılandırması, araçlar, ortam değişkenleri ve sorun giderme için
**depo kök dizinindeki [README](../README.md)**'ye bakın.

## Hızlı Başlangıç

**Seçenek 1: Direktly kurulum (önerilen)**

```powershell
cd turkanime-mcp
pip install -r requirements.txt
python turkanime_mcp.py   # stdio bekler; Claude Desktop üzerinden kullanılır
```

**Seçenek 2: Paket olarak kurulum** (ayrı bir `turkanime-mcp` komutu oluşturur):

```powershell
pip install .
turkanime-mcp   # MCP sunucusu başlar
```

Her iki durumda da **tüm bağımlılıklar otomatik yüklenir** — `turkanime-cli`, `ffmpeg` vb. tek seferde.

Testler (ağ/`turkanime_api` gerektirmez, ek bağımlılık yok — depo kökünden çalıştırın):

```powershell
python -m unittest discover -s turkanime-mcp/tests -t turkanime-mcp/tests
```
