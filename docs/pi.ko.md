# Pi에서 Sapper 사용하기

[English](pi.md) | 한국어

Pi로 로컬 프로젝트를 분석하고, Sapper 그래프에 작업 결과를 기록하고, 여러 Pi worker의 세션을 유지할 수 있습니다. 이 경로에는 tmux나 대회 서버 설정이 필요하지 않습니다. 기존 Claude 백엔드는 그대로 사용할 수 있습니다.

보안 점검에는 별도의 대상 승인이 필요합니다. 아래 예시는 로컬 문서 분석만 수행합니다.

## 1. 실행 환경 준비하기

macOS 또는 Linux에서 다음 버전을 사용합니다.

- Python 3.10 이상
- Node.js 22.19 이상
- Pi `@earendil-works/pi-coding-agent` 1.4.0

Pi가 없다면 설치합니다. 이미 설치했다면 먼저 `pi --version`으로 버전을 확인합니다. 이 통합은 Pi 1.4.0으로 검증했습니다.

```sh
npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.4.0
```

Pi 지원은 `feat/pi-rpc` 브랜치에 있습니다. 저장소를 받고 현재 터미널에서 CLI를 사용할 수 있게 합니다.

```sh
git clone --branch feat/pi-rpc https://github.com/sehoon123/sapper-v2.git
cd sapper-v2
export PATH="$PWD/bin:$PATH"
sapper --version
```

`sapper 1.0.0`이 표시되면 CLI가 준비된 상태입니다. PATH 설정은 현재 터미널에서만 유지됩니다. 새 터미널에서는 저장소의 `bin`을 다시 PATH에 추가하거나 `/absolute/path/to/sapper-v2/bin/sapper`를 사용합니다.

Pi에서 사용할 provider가 아직 없다면 `pi`를 실행한 뒤 `/login`으로 인증하고 `/model`로 모델을 선택합니다. API 키나 자체 gateway를 쓰는 경우에는 [Pi provider 문서](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md)를 따릅니다. Sapper는 인증 정보를 설정하거나 키를 교체하지 않습니다.

## 2. 작업할 프로젝트에 연결하기

Pi를 종료한 뒤, 분석할 프로젝트 디렉터리에서 실행합니다. `/path/to/your-project`는 실제 프로젝트 경로로 바꿉니다.

```sh
cd /path/to/your-project
sapper setup --backend pi --goal "로컬 프로젝트의 구조와 테스트를 분석한다"
sapper status
sapper context
```

처음 실행하면 `.sapper/`에 그래프를 만들고, 프로젝트의 `.pi/settings.json`에 Sapper 패키지 경로를 등록합니다. 기존 그래프가 있으면 유지합니다. 사용자 전역 Pi 설정, Claude 설정, `AGENTS.md`는 바꾸지 않습니다.

Pi를 실행하고 기본 신뢰 절차에서 패키지와 프로젝트 설정을 검토합니다. 관리형 RPC에서도 프로젝트 리소스를 사용하려면 Pi에 신뢰 결정을 저장합니다. Sapper는 승인이나 불신을 강제하지 않습니다. RPC에는 시작 시 신뢰 대화창이 없으므로, 아직 신뢰하지 않은 프로젝트 리소스는 Pi 기본 규칙에 따라 로드되지 않을 수 있습니다.

```sh
pi
```

Pi에 다음과 같이 요청할 수 있습니다.

```text
Sapper 그래프와 이 폴더의 README를 읽고 현재 목표와 다음 분석 단계를 설명해줘.
파일은 변경하지 말고 외부 주소에는 접속하지 마.
```

Sapper extension은 그래프 컨텍스트와 번들 CLI 경로를 제공합니다. 별도의 도구 허용 목록이나 shell 차단 정책은 없습니다. 일반 Pi와 마찬가지로 shell 실행, 파일 쓰기·수정, 사용자 도구를 사용할 수 있으며, 사용자가 설정한 도구 선택과 권한 extension은 그대로 적용됩니다. 아래 명령은 터미널 또는 shell을 사용할 수 있는 Pi에서 실행할 수 있습니다.

## 3. Pi로 그래프 작업 실행하기

Pi 대화창이 아닌 일반 터미널에서 실행합니다.

```sh
sapper dispatch reason --backend pi
sapper status
```

`reason`은 현재 그래프를 바탕으로 다음 작업을 제안합니다. 모델을 명시하려면 `pi --list-models`로 확인한 정확한 `provider/model-id`를 `--model`에 전달합니다. 예를 들어 `sapper dispatch reason --backend pi --model provider/model-id`처럼 사용합니다. 알 수 없거나 모호한 모델 이름은 다른 모델로 대체하지 않고 실패합니다.

`status`에 표시된 작업 내용을 검토합니다. 다음 예시의 `i001`을 실행할 실제 intent ID로 바꿉니다. 확인이 필요한 작업은 `confirm`으로 승인한 뒤 실행합니다.

```sh
sapper confirm i001
sapper dispatch i001 --backend pi --claim
sapper dispatch verify --intent i001 --backend pi
sapper status
```

최종 결과는 기존 payload 검증과 intent 확인·점유 규칙을 통과한 뒤 Sapper 그래프에 반영됩니다. 제한된 횟수로 자동 진행하려면 다음을 실행합니다.

```sh
sapper auto --backend pi --max-steps 2 --max-explore 1
```

`setup --backend pi`는 이후 명령의 백엔드 기본값을 바꾸지 않습니다. `dispatch`와 `auto`에는 계속 `--backend pi`를 지정합니다. `worker` 명령은 Pi 전용입니다.

## 4. 상주 worker 사용하기

한 프로젝트 디렉터리에서 supervisor를 시작합니다. 아래 예시는 worker를 최대 2개로 제한하고 실행 기한을 30분으로 설정합니다.

```sh
sapper worker up --max-workers 2 --seconds 1800
sapper worker spawn --cwd .
sapper worker list
```

`spawn`이 반환한 `id`를 사용합니다. 아래 `w-0123456789abcdef`는 예시이므로 실제 ID로 바꿉니다.

```sh
sapper worker message w-0123456789abcdef "로컬 README를 읽고 핵심 구조를 요약해줘. 파일은 변경하지 마."
sapper worker peek w-0123456789abcdef
sapper worker board
```

`peek`의 `status`가 `completed`이면 `result`에서 응답을 확인합니다. 실행 중에는 새 메시지를 받지 않습니다. 자유 형식 메시지는 텍스트 결과를 반환하며, 결과를 자동으로 그래프 사실로 기록하지는 않습니다.

그래프 작업을 맡기려면 기존 작업이 끝난 뒤 검토·승인한 intent를 전달합니다.

```sh
sapper worker message w-0123456789abcdef --intent i001
```

worker는 해당 intent를 자기 ID로 점유하고, 결과를 검증한 뒤 그래프에 기록합니다. 작업이 끝나면 worker 하나 또는 supervisor 전체를 중지합니다.

```sh
sapper worker stop w-0123456789abcdef
sapper worker down
```

세션 파일이 저장된 worker는 supervisor가 실행 중일 때 `sapper worker resume ID`로 재개할 수 있습니다. 같은 세션을 다시 열지만 중단된 작업을 자동 재실행하지 않습니다. 그래프와 실제 변경 사항을 확인한 뒤 새 메시지를 보냅니다.

실행 기한과 사용자가 정한 양의 정수 worker 수 제한은 최초 시작 시 저장됩니다. 별도의 32개 상한은 없습니다. supervisor를 재시작해도 초기화되지 않습니다. 새 예산은 별도 프로젝트 디렉터리에서 시작합니다. 복구가 거부되거나 `unresolved` 상태가 나오면 [영문 복구 안내](pi.md#recover-a-campaign)를 확인합니다.

기존 registry는 supervisor 잠금 안에서 폐기된 `settings.policy` 키만 제거합니다. 그 외 필드, 세션, generation, claim, 실행 기한과 worker 수는 유지됩니다(기존 재시작 상태 전이는 그대로 적용). 알 수 없는 실행 설정은 파일을 덮어쓰지 않고 실패합니다. 복구를 위해 세션을 삭제할 필요는 없습니다.

## 5. Pi 대화에서 worker를 직접 조율하기

일반 Pi의 shell 기능을 사용하며 Sapper 전용 opt-in은 필요하지 않습니다. 사용자 도구 설정과 권한 extension은 계속 적용됩니다. Pi는 OS 샌드박스가 아니며 사용 가능한 파일·인증 정보·네트워크에 접근하거나 프로세스를 실행할 수 있습니다. 필요에 따라 별도 OS 사용자, 컨테이너 또는 VM과 적절한 파일·네트워크 범위를 사용합니다.

```sh
pi
```

Pi에 다음과 같이 요청합니다.

```text
이 프로젝트의 로컬 코드와 문서를 분석해줘.
Sapper worker를 최대 2개, 기한 15분으로 시작해.
worker에게 README 분석과 테스트 구조 분석을 나눠 맡겨.
외부 주소에 접속하지 말고 파일을 변경하지 마.
각 결과를 확인해 종합하고, 끝나면 worker down으로 정리해.
```

관리형 Pi는 사용자 extension, skill, prompt template, theme, context/instruction(`APPEND_SYSTEM.md` 포함), MCP, provider 환경 변수와 Pi 기본 프로젝트 신뢰 결정을 그대로 사용합니다. Sapper는 offline, telemetry, version 설정을 강제로 바꾸지 않습니다. explore용 추가 지침도 기본 prompt 리소스를 대체하지 않고 덧붙입니다.

공유 옵션 `--skip-permissions`는 Pi에서 호환성을 위한 **no-op**입니다. Pi에는 대응하는 core 도구 승인 생략 옵션이 없으며, 이 옵션으로 신뢰 결정이나 사용자 권한 extension의 대화창을 승인하지 않습니다. Claude에서는 기존 `--dangerously-skip-permissions` 전달을 유지합니다. 이전 `--pi-policy`, `SAPPER_PI_POLICY`, policy 명령은 제거되었으므로 스크립트에서 폐기된 옵션을 지웁니다.

관리형 RPC에는 대화형 승인 중계가 없습니다. 로드된 extension이 `select`, `confirm`, `input`, `editor`를 요청하면 취소 응답을 보내고 **명확한 오류로 실행을 실패 처리**합니다. 시작 단계에도 동일하며, 취소를 성공으로 처리하거나 extension을 몰래 비활성화하지 않습니다. 알림은 표시되지 않을 수 있고 Pi RPC 자체의 custom TUI 기능도 제한됩니다. 상호작용이 필요하면 대화형 Pi를 쓰거나 해당 extension이 지원하는 비대화형 동작을 직접 설정합니다.

## 6. 프로젝트 연결 해제하기

먼저 worker를 종료한 뒤 패키지 등록을 제거합니다.

```sh
sapper worker down
sapper teardown --backend pi
```

supervisor를 시작한 적이 없다면 첫 명령은 생략합니다. `teardown`은 Sapper가 추가한 패키지 등록만 제거합니다. 그래프와 runtime 세션은 보존합니다. 데이터 삭제 옵션인 `--purge`는 백업 없이 사용하지 않습니다.

## 지원 범위 확인하기

- Pi에서 `dispatch`, `auto`, `worker`를 사용할 수 있습니다.
- 기존 `bench`, `sweep`, `command`, tmux `fleet`, `docker/commander`는 Pi 백엔드가 아직 구현되지 않았습니다. 도구 권한 제한이 아니며 Claude 경로는 그대로입니다.
- 무인 전략 commander, 실행 중 `steer/follow_up`, 대화형 승인 중계는 구현하지 않았습니다.
- 정확한 모델 ID를 사용하며 Claude식 alias/fallback/키 교체는 하지 않습니다. Pi auto 실행 오류는 명확하게 실패 처리합니다.
- 임의의 세션 전환·fork는 지원하지 않습니다. 정확한 소유 세션 복구, stale generation 거부, 이전 PID의 종료 확인과 protocol/context 크기 제한은 데이터·수명주기 불변 조건입니다.
- Windows는 지원하지 않습니다. macOS의 supervisor 경로는 Unix socket 길이 제한 때문에 짧게 유지해야 합니다.
- `.sapper-runtime/`에는 모델 입력·출력과 세션이 남을 수 있습니다. Git에 올리거나 다른 사용자에게 공유하지 않습니다.

외부 모델 없이 검증하려면 저장소에서 실행합니다.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 python3 scripts/verify_pi.py
git diff --check
```

verifier는 설치된 Pi 1.4.0과 localhost provider, 임시 HOME·설정·프로젝트만 사용합니다. 기본 bash/write/edit 및 사용자 도구, CLI, 사용자 리소스와 도구 선택, 신뢰 설정, 대화창 실패, public dispatch/worker, registry migration과 exact-session 복구·정리를 확인합니다. 테스트 격리 옵션은 production launcher에 적용하지 않습니다. 컨테이너 실행은 [영문 Pi 가이드](pi.md)를 참고합니다.
