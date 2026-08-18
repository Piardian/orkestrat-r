# GitHub repo oluşturma yeteneği — tek seferlik kurulum

Amaç: ChatGPT'den `yeni repo oluştur` dediğinizde, yerel GitHub MCP aracının sizin GitHub hesabınızda yeni repository oluşturabilmesi.

## Gerekenler

- Windows + Python 3.11+
- GitHub hesabı: `Piardian`
- ChatGPT full MCP write desteği için Business veya Enterprise/Edu workspace
- OpenAI Platform'da Secure MCP Tunnel erişimi

## 1) GitHub tarafını kur

PowerShell'de `agent-core` klasörüne girin ve:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_github_mcp_windows.ps1
```

Script tarayıcıda GitHub Fine-grained token sayfasını açar. Token ayarları:

- Resource owner: `Piardian`
- Repository access: `All repositories`
- Repository permissions → `Administration`: `Read and write`

Token proje içine yazılmaz. Windows DPAPI ile `%LOCALAPPDATA%\AjanOrdusu\secrets` altında, yalnızca sizin Windows hesabınızın çözebileceği biçimde saklanır.

## 2) OpenAI Secure MCP Tunnel kur

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_openai_tunnel_windows.ps1
```

Script OpenAI Platform Tunnels ve API Keys sayfalarını açar. Sizden:

- `tunnel_id` (`tunnel_...`)
- tunnel-client runtime API key

ister. Sonra resmi `openai/tunnel-client` son Windows sürümünü indirir ve bridge'i başlatır.

## 3) ChatGPT'ye uygulamayı ekle

ChatGPT web'de Developer Mode açık olmalı.

- Settings / Workspace Settings → Apps → Create
- Connection: `Tunnel`
- Oluşturduğunuz tunnel'i seçin
- `Scan Tools`
- Şu iki araç görünmeli:
  - `github_whoami`
  - `create_repository`
- Create

İlk test: `github_whoami` hesabı `Piardian` dönmeli.

Sonra: `deneme-agent-repo` adlı private repo oluştur.

## 4) Windows açılınca otomatik çalışsın (önerilir)

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\enable_github_bridge_autostart.ps1
```

Durum kontrolü:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\github_bridge_status.ps1
```

## Güvenlik

- Repo oluşturma varsayılan olarak `private`.
- Public repo oluşturma ayrıca kapalıdır (`GITHUB_MCP_ALLOW_PUBLIC=false`).
- Repo silme aracı yoktur.
- GitHub tokenı ve OpenAI tunnel key proje/GitHub içine yazılmaz.
- MCP yalnızca `127.0.0.1` üzerinde dinler; Secure MCP Tunnel outbound HTTPS ile OpenAI'ye bağlanır.
