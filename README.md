# FastNodeSync CLI

Command-line client for bidirectional, near real-time Obsidian note sync with [**Fast Note Sync Service**](https://github.com/haierkeys/fast-note-sync-service). Use it on headless Linux servers (e.g. OpenClaw) with the same sync model as the Obsidian plugin.

## Documentation

**English (default)** · [简体中文](doc/README.zh-CN.md) · [日本語](doc/README.ja.md) · [한국어](doc/README.ko.md) · [繁體中文](doc/README.zh-TW.md)

Full guide (configuration, systemd, CLI, troubleshooting): **[doc/README.md](doc/README.md)**

## Quick start

```bash
pip install -r requirements.txt
cp config.yaml config.local.yaml   # edit api / token / vault
python -m fns_cli.main run -c config.local.yaml
```

## Related

- [Fast Note Sync Service](https://github.com/haierkeys/fast-note-sync-service) — server
- [obsidian-fast-note-sync](https://github.com/haierkeys/obsidian-fast-note-sync) — Obsidian plugin
