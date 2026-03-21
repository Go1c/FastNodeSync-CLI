# FastNodeSync CLI

[简体中文](README.zh-CN.md) | [English](README.md) | [日本語](README.ja.md) | [한국어](README.ko.md) | [繁體中文](README.zh-TW.md)

Obsidian 노트의 양방향·준실시간 동기화를 위한 CLI 클라이언트입니다. [Fast Note Sync Service](https://github.com/haierkeys/fast-note-sync-service)와 함께 사용하며, GUI 없는 Linux 서버(OpenClaw 등)에서 Obsidian 데스크톱/모바일 플러그인과 유사한 동기화를 제공합니다.

## 기능

- **양방향 실시간 동기화**: 로컬 변경은 서버로 푸시, 원격(Obsidian 등) 변경은 로컬로 풀
- **전체 콘텐츠**: `.md` 노트, 첨부(이미지, HTML, Canvas 등), `.obsidian/` 설정
- **자동 재연결**: 연결 끊김 시 지수 백오프, 복구 후 증분 동기화
- **루프 방지**: 서버에서 받아 쓴 파일이 즉시 다시 업로드되지 않도록 처리
- **증분 동기화**: `lastSyncTime` 기준으로 변경분만 동기화

## 프로젝트 구조

```
FastNodeSync-CLI/
├── doc/                   # 문서(다국어 README)
├── fns_cli/               # Python 패키지
├── tests/                 # 스모크 테스트 (unittest)
├── .github/workflows/     # GitHub Actions CI
├── config.yaml            # 설정 예시
└── requirements.txt       # 의존성
```

## 개발 및 CI

로컬에서 스모크 테스트(stdlib만 사용):

```bash
# 저장소 루트에서
export PYTHONPATH=.   # Windows: set PYTHONPATH=.
python -m unittest discover -s tests -v
```

`main` 브랜치에 push 또는 PR 시 GitHub Actions가 의존성 설치, `compileall`, `fns_cli.main --help`, unittest를 실행합니다.

## 배포

### 1. 요구 사항

- Python 3.10+

### 2. 의존성 설치

```bash
pip install -r requirements.txt
```

### 3. 설정

`config.yaml` 편집:

```yaml
server:
  api: "https://your-server-address"   # Fast Note Sync Service URL
  token: "your_api_token"                # 관리 패널에서 발급한 API 토큰
  vault: "notes"                         # Vault 이름(Obsidian 플러그인과 동일해야 함)

sync:
  watch_path: "./vault"                  # 로컬 Vault 경로(상대/절대)
  sync_notes: true
  sync_files: true
  sync_config: true
  exclude_patterns:
    - ".git/**"
    - ".trash/**"
    - "*.tmp"
  file_chunk_size: 524288

client:
  reconnect_max_retries: 15
  reconnect_base_delay: 3
  heartbeat_interval: 30

logging:
  level: "INFO"
  file: ""
```

**토큰 발급 방법**

1. 브라우저에서 Fast Note Sync Service 관리 화면 열기(예: `https://your-server-address`)
2. 로그인
3. **"Copy API Config"** 클릭
4. JSON에서 `api`, `apiToken`, `vault`를 복사해 `config.yaml`에 반영

환경 변수(파일보다 우선하지 않음 — `config.py` 로직 참고):

```bash
export FNS_API="https://your-server-address"
export FNS_TOKEN="your_api_token"
```

### 4. 실행

```bash
python -m fns_cli.main run -c config.yaml
```

#### 임시 백그라운드(운영 환경에는 비권장)

```bash
nohup python -m fns_cli.main run -c config.yaml > fns.log 2>&1 &
screen -dmS fns python -m fns_cli.main run -c config.yaml
```

---

## 데몬 및 부팅 시 자동 시작(systemd, 권장)

Linux에서는 **systemd**로 **크래시 시 자동 재시작**, **재부팅 후 자동 시작**, **journalctl 로그**를 한 번에 관리할 수 있습니다.

가정:

- 설치 경로: `/opt/FastNodeSync-CLI`
- 설정 파일: `/opt/FastNodeSync-CLI/config.yaml`
- 실행 사용자: `your_user` (**root 사용 금지**)
- Python: `/usr/bin/python3` (`which python3`로 확인)

유닛 파일 생성:

```bash
sudo nano /etc/systemd/system/fns-cli.service
```

예시:

```ini
[Unit]
Description=FastNodeSync CLI - Obsidian vault sync
Documentation=https://github.com/Go1c/FastNodeSync-CLI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=your_user
Group=your_user
WorkingDirectory=/opt/FastNodeSync-CLI
Environment=PYTHONUNBUFFERED=1
# 선택: 비밀은 별도 파일(chmod 600)
# EnvironmentFile=/opt/FastNodeSync-CLI/.env
ExecStart=/usr/bin/python3 -m fns_cli.main run -c /opt/FastNodeSync-CLI/config.yaml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

활성화 및 시작:

```bash
sudo systemctl daemon-reload
sudo systemctl enable fns-cli
sudo systemctl start fns-cli
sudo systemctl status fns-cli
```

자주 쓰는 명령:

```bash
sudo systemctl stop fns-cli
sudo systemctl restart fns-cli
journalctl -u fns-cli -f
journalctl -u fns-cli --since today
```

**참고**

- `enable`은 **재부팅 후 자동 시작**을 등록합니다. `After=network-online.target`은 네트워크 준비 전에 WebSocket이 올라가는 것을 줄입니다.
- `your_user`가 `watch_path`(Vault 디렉터리)에 읽기/쓰기 권한이 있는지 확인하세요.
- 서버(업스트림) 배포는 [fast-note-sync-service](https://github.com/haierkeys/fast-note-sync-service) 문서(Docker, 원클릭 설치 등)를 참고하세요.

---

## CLI 명령

| 명령 | 설명 |
|------|------|
| `run` | 상시 실행: 초기 동기화 + 로컬 감시 + 원격 수신 |
| `sync` | 전체 양방향 동기화 1회 후 종료 |
| `pull` | 원격→로컬 풀만 수행 후 종료 |
| `push` | 로컬→서버 푸시 후 종료 |
| `status` | 설정 및 동기화 상태 표시 |

모든 명령은 `-c` / `--config`로 설정 파일을 지정할 수 있으며 기본값은 `config.yaml`입니다.

```bash
python -m fns_cli.main run -c config.yaml
python -m fns_cli.main sync -c config.yaml
python -m fns_cli.main pull -c config.yaml
python -m fns_cli.main push -c config.yaml
python -m fns_cli.main status -c config.yaml
```

## 동기화 동작

### `run` 흐름

```
1. WebSocket 연결 → 인증
2. 증분 풀(NoteSync + FileSync)
3. 로컬 Vault에 watchdog 시작
4. 지속 양방향 동기화(원격→로컬, 로컬→서버→다른 클라이언트)
5. 끊김 시 재연결·증분 보정
```

### 상태 파일

진행 상태는 `vault/.fns_state.json`에 저장됩니다(자동 관리). 재시작 후에도 마지막 시점부터 증분 동기화합니다.

### 주의

- `vault` 이름은 Obsidian 플러그인 설정과 반드시 일치해야 합니다.
- 첫 `run` 또는 `pull`은 전체 다운로드가 될 수 있으며, 이후는 증분입니다.
- 여러 기기에서 동시 편집 시 서버에 마지막으로 기록된 버전이 우선합니다(서버 측 충돌 처리).
- `.fns_state.json`은 서버로 업로드되지 않습니다.

## 관련 프로젝트

- [Fast Note Sync Service](https://github.com/haierkeys/fast-note-sync-service) — 백엔드 서버
- [obsidian-fast-note-sync](https://github.com/haierkeys/obsidian-fast-note-sync) — Obsidian 플러그인
