# turkanime-mcp

MCP sunucusunun kaynak kodu burada: [`turkanime_mcp.py`](turkanime_mcp.py).

Kurulum, Claude Desktop yapılandırması, araçlar, ortam değişkenleri ve sorun giderme için
**depo kök dizinindeki [README](../README.md)**'ye bakın.

Hızlı başlangıç:

```powershell
pip install -r requirements.txt
python turkanime_mcp.py   # stdio bekler; Claude Desktop üzerinden kullanılır
```

Testler (ağ/`turkanime_api` gerektirmez, ek bağımlılık yok — depo kökünden çalıştırın):

```powershell
python -m unittest discover -s turkanime-mcp/tests -t turkanime-mcp/tests
```
