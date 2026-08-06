import os, json, subprocess, re
from datetime import datetime
from mcp.server.fastmcp import FastMCP

# 각 진단 도구는 별도 리포에서 pip 패키지(git+https, 버전 태그 고정)로 설치되어
# 콘솔 스크립트(entry point)로 PATH에 등록된다 — 소스 파일 경로에 의존하지 않는다.
# (리포별 pyproject.toml의 [project.scripts] 이름과 정확히 일치해야 함)
PG_TOOL  = "pg-diagnose"
AKS_TOOL = "aks-diagnose"
ADX_TOOL = "adx-diagnose"
EH_TOOL  = "eh-diagnose"
SVCMAP_TOOL = "svcmap-diagnose"
AGW_TOOL = "agw-diagnose"
WINDOWS_TOOL = "windows-diagnose"
LINUX_TOOL = "linux-diagnose"
MSSQL_TOOL = "mssql-diagnose"
MYSQL_TOOL = "mysql-diagnose"

mcp = FastMCP("diag-tools", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))

# 헬스체크: ACA/오케스트레이터가 컨테이너 생존을 확인하는 용도(인증·진단 로직 없음, /mcp와 무관).
# 설치된 mcp SDK 버전에 custom_route가 없을 수도 있어 방어적으로 등록한다 —
# 없으면 조용히 건너뛰고 /mcp 등 기존 동작은 그대로 유지된다.
if hasattr(mcp, "custom_route"):
    try:
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        @mcp.custom_route("/health", methods=["GET"])
        async def _health(request: Request) -> JSONResponse:  # noqa: ANN001
            return JSONResponse({"status": "ok"})
    except Exception:  # noqa: BLE001 — 헬스체크 등록 실패는 서버 기동을 막지 않는다
        pass

RID = re.compile(r"^/subscriptions/[0-9a-fA-F-]+/.+$")
NS  = re.compile(r"^[a-z0-9-]{1,63}$")
CLUSTER = re.compile(r"^https://[A-Za-z0-9.-]+\.kusto\.[A-Za-z0-9.]+/?$", re.IGNORECASE)


def _validate_iso_dt(value: str) -> str:
    """start_time/end_time이 실제 ISO 8601 형식인지 검증(그대로 argv에 전달되므로 조기 검증)."""
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"invalid ISO 8601 datetime: {value!r}")
    return value


# --prometheus-aad(Entra 토큰) 유출/SSRF 방지: Azure Monitor 관리형 Prometheus 엔드포인트로만 허용
PROM_URL = re.compile(r"^https://[A-Za-z0-9.-]+\.prometheus\.monitor\.azure\.com(/.*)?$", re.IGNORECASE)
# Log Analytics workspace GUID / OS 호스트명(컴퓨터명) 검증 (linux/windows os 진단용)
WORKSPACE_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
COMPUTER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


def _run(tool: str, cmd: list, timeout: int) -> dict:
    """모든 진단 도구 공통 실행 래퍼 — 실패/타임아웃/비JSON을 구조화 에러로 통일 반환한다.
    SRE Agent가 도구마다 다른 방식(예외 vs JSON)으로 실패를 처리하지 않도록 응답 형식을 맞춘다."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"tool": tool, "error": "diagnose timed out", "timeout_seconds": timeout}
    except Exception as e:  # noqa: BLE001 — 실행 자체 실패(파일 없음 등)
        return {"tool": tool, "error": "diagnose could not start", "detail": str(e)[-500:]}
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return {"tool": tool, "error": "diagnose failed",
                "returncode": proc.returncode, "stderr": (proc.stderr or "")[-2000:]}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"tool": tool, "error": "invalid JSON output",
                "stdout": proc.stdout[-2000:], "stderr": (proc.stderr or "")[-1000:]}


@mcp.tool()
def diagnose_postgres(host: str, user: str, dbname: str = "postgres",
                      resource_id: str = "", hours: int = 24, auth_mode: str = "entra") -> dict:
    """Azure Database for PostgreSQL Flexible Server 진단(읽기 전용, 결과 JSON).
    host: <name>.postgres.database.azure.com. user: 접속 롤(Entra 사용자 또는 DB 계정) — 필수.
    resource_id 미지정 시 Azure Monitor 메트릭(CPU/메모리/IOPS/연결수) 수집 생략.

    auth_mode='entra'(기본): Entra ID 토큰 인증. auth_mode='password': DB 계정(네이티브 PostgreSQL
    로그인) 인증 — 이 경우 비밀번호는 이 도구의 인자로 전달되지 않으며, MCP를 구동하는 컨테이너에
    PGPASSWORD 환경변수가 미리 설정돼 있어야 한다(mssql/mysql의 auth_mode="sql"/"mysql"과 동일한
    설계 — DB 접속 계정과 Azure API 호출용 시스템 계정을 분리 운영할 수 있다).

    [자율 발견→재호출 루프] host(FQDN, 데이터 평면)만으로는 ARM resource_id(제어 평면)를
    직접 유도할 수 없다. resource_id 없이 호출해 결과 JSON의 최상위 `needs_input`에
    parameter="resource_id" 항목이 있으면, 그 안의 discovery_hint를 참고해 Resource
    Graph로 host의 서버명과 일치하는 Flexible Server 리소스를 찾은 뒤, 그 id를
    resource_id 인자로 이 도구를 재호출하면 Azure Monitor 메트릭까지 채워진다."""
    if not host or not user:
        raise ValueError("host 와 user 는 필수입니다")
    if auth_mode not in ("entra", "password"):
        raise ValueError("invalid auth_mode")
    if resource_id and not RID.match(resource_id):
        raise ValueError("invalid resource_id")
    cmd = [PG_TOOL, "--host", host, "--user", user, "--dbname", dbname,
           "--format", "json", "--hours", str(hours)]
    if auth_mode == "entra":
        cmd += ["--aad"]  # Entra 토큰
    if resource_id:
        cmd += ["--resource-id", resource_id]
    return _run("pg_diagnose", cmd, 180)


@mcp.tool()
def diagnose_aks(namespace: str = "default", context: str = "", all_namespaces: bool = False,
                 prometheus_url: str = "", appinsights_id: str = "") -> dict:
    """AKS 클러스터 진단(읽기 전용, 외부 kubeconfig/context).
    prometheus_url 미지정 시 Prometheus 지표(노드/파드 CPU·메모리·큐 랙) 생략,
    appinsights_id 미지정 시 분산 트레이싱 생략.

    [자율 발견→재호출 루프] kubeconfig context/namespace(데이터 평면)만으로는 연결된
    Azure Monitor 관리형 Prometheus/App Insights의 ARM 리소스(제어 평면)를 직접 유도할
    수 없다. 결과 JSON의 최상위 `needs_input`에 parameter="prometheus_url" 또는
    "appinsights_id" 항목이 있으면, 그 안의 discovery_hint를 참고해 (Resource Graph로
    context와 일치하는 AKS 클러스터를 찾고, 연결된 Azure Monitor Workspace/App
    Insights/Log Analytics workspace를 확인해) 값을 확정한 뒤, 그 값을 prometheus_url
    또는 appinsights_id 인자로 이 도구를 재호출하면 해당 섹션까지 채워진다."""
    if not NS.match(namespace):
        raise ValueError("invalid namespace")
    if prometheus_url and not PROM_URL.match(prometheus_url):
        raise ValueError("prometheus_url 은 Azure Monitor 관리형 Prometheus "
                         "엔드포인트(https://<...>.prometheus.monitor.azure.com)여야 합니다")
    cmd = [AKS_TOOL, "--namespace", namespace, "--format", "json"]
    if context:        cmd += ["--context", context]
    if all_namespaces: cmd += ["--all-namespaces"]
    if prometheus_url: cmd += ["--prometheus-url", prometheus_url, "--prometheus-aad"]
    if appinsights_id: cmd += ["--appinsights-id", appinsights_id]
    return _run("aks_diagnose", cmd, 240)

@mcp.tool()
def diagnose_adx(cluster: str, database: str = "", resource_id: str = "",
                 region: str = "", hours: int = 24) -> dict:
    """Azure Data Explorer(ADX/Kusto) 진단(읽기 전용, 결과 JSON).
    cluster: https://<name>.<region>.kusto.windows.net
    database 지정 시 .show queries/캐시/extents 분석, resource_id+region 지정 시 Azure Monitor 메트릭.

    [자율 발견→재호출 루프] cluster URI(쿼리 평면)만으로는 ARM resource_id(제어 평면)를
    직접 유도할 수 없다. resource_id 없이 호출해 결과 JSON의 최상위 `needs_input`에
    parameter="resource_id" 항목이 있으면, 그 안의 discovery_hint를 참고해 Resource
    Graph로 cluster URI의 클러스터명과 일치하는 Kusto 클러스터 리소스를 찾은 뒤, 그
    id를 resource_id 인자로(필요시 location을 region 인자로) 이 도구를 재호출하면
    Azure Monitor 메트릭까지 채워진다."""
    if not CLUSTER.match(cluster or ""):
        raise ValueError("invalid cluster URI")
    if resource_id and not RID.match(resource_id):
        raise ValueError("invalid resource_id")
    cmd = [ADX_TOOL, "--cluster", cluster,
           "--auth", "default", "--format", "json", "--hours", str(hours)]
    if database:    cmd += ["--database", database]
    if resource_id: cmd += ["--resource-id", resource_id]
    if region:      cmd += ["--region", region]
    return _run("adx_diagnose", cmd, 240)

@mcp.tool()
def diagnose_eventhub(resource_id: str, event_hub: str = "", region: str = "",
                      window_minutes: int = 60, checkpoint_store: str = "") -> dict:
    """Azure Event Hubs 진단(읽기 전용, 결과 JSON).
    resource_id: namespace ARM 리소스 ID. event_hub 미지정 시 모든 event hub를 개별 진단.
    region 미지정 시 ARM location에서 자동 유도. checkpoint_store 미지정 시 같은 RG의
    스토리지를 자동 탐색해 consumer lag까지 계산(권한: Storage Blob Data Reader).
    결과에는 summary/health_score/severity_counts/recommended_actions 포함.

    [자율 발견→재호출 루프] consumer lag는 컨슈머 앱의 BlobCheckpointStore(데이터 평면)에
    있어 ARM/Resource Graph만으로는 위치를 알 수 없다. 결과 JSON의 최상위 `needs_input`에
    parameter="checkpoint_store" 항목이 있으면, 그 안의 expected_blob_prefix/discovery_hint를
    참고해 (Resource Graph로 스토리지 후보 탐색 + 앱 설정/Key Vault의 연결 문자열 확인 또는
    컨테이너 접두사 탐침으로) 체크포인트 컨테이너 URL을 확정한 뒤, 그 값을 checkpoint_store
    인자로 이 도구를 재호출하면 정밀 consumer lag가 채워진다."""
    if not RID.match(resource_id or ""):
        raise ValueError("invalid resource_id")
    cmd = [EH_TOOL, "--resource-id", resource_id, "--azure-auth",
           "--eh-auth", "entra", "--format", "json", "--window-minutes", str(window_minutes)]
    if event_hub:
        cmd += ["--event-hub", event_hub]
    if region:
        cmd += ["--region", region]
    if checkpoint_store:
        cmd += ["--checkpoint-store", checkpoint_store]
    return _run("eh_diagnose", cmd, 300)

@mcp.tool()
def diagnose_service_map(appinsights_id: str = "", workspace_id: str = "",
                         workload: str = "", window_minutes: int = 60) -> dict:
    """워크로드 서비스 맵 진단(읽기 전용, 결과 JSON).
    어떤 서비스가 어떤 서비스와 연결돼 있는지(노드/엣지)와 연결별 통신 상태
    (호출수/실패율/지연/데이터량)를 수집한다.
    appinsights_id: Application Insights ARM 리소스 ID → 앱↔앱/앱→PaaS 의존성 자동 발견.
    workspace_id: Log Analytics workspace GUID → AppDependencies + VMConnection(네트워크 bytes).
    결과에는 nodes/edges + summary/health_score/severity_counts/recommended_actions 포함."""
    if appinsights_id and not RID.match(appinsights_id):
        raise ValueError("invalid appinsights_id")
    if not (appinsights_id or workspace_id):
        raise ValueError("appinsights_id 또는 workspace_id 중 하나는 필요합니다")
    cmd = [SVCMAP_TOOL, "--format", "json",
           "--window-minutes", str(window_minutes)]
    if appinsights_id:
        cmd += ["--appinsights-id", appinsights_id]
    if workspace_id:
        cmd += ["--workspace-id", workspace_id]
    if workload:
        cmd += ["--workload", workload]
    return _run("svcmap_diagnose", cmd, 300)

@mcp.tool()
def diagnose_appgateway(resource_id: str = "", region: str = "",
                        window_minutes: int = 60, backend_health: bool = True) -> dict:
    """Azure Application Gateway 데이터 플레인 진단(읽기 전용, 결과 JSON).
    resource_id: Application Gateway ARM 리소스 ID.
    백엔드 상태(실시간 프로브) + 실패요청/5xx/지연/용량 포화/연결을 수집.
    region 미지정 시 ARM location에서 자동 유도. backend_health=False면 실시간 프로브 생략.
    결과에는 summary/health_score/severity_counts/recommended_actions 포함."""
    if not RID.match(resource_id or ""):
        raise ValueError("invalid resource_id")
    cmd = [AGW_TOOL, "--resource-id", resource_id, "--azure-auth",
           "--format", "json", "--window-minutes", str(window_minutes)]
    if region:
        cmd += ["--region", region]
    if not backend_health:
        cmd += ["--no-backend-health"]
    return _run("agw_diagnose", cmd, 300)

@mcp.tool()
def diagnose_mssql(host: str, user: str, database: str = "master", auth_mode: str = "entra",
                   resource_id: str = "", region: str = "", hours: int = 24) -> dict:
    """SQL Server 진단(읽기 전용, 결과 JSON) — 온프레미스/IaaS/Azure SQL Database/
    Azure SQL Managed Instance 전부 지원(같은 바이너리, EngineEdition으로 자동 감지).
    host: SQL Server 호스트/FQDN. user: 로그인 이름(Entra UPN 또는 SQL 로그인) — 필수.
    auth_mode: 'entra'(기본, Entra 토큰) 또는 'sql'(SQL 인증 — 이 경우 컴테이너
    환경변수 MSSQL_DIAGNOSE_PASSWORD가 미리 설정돼 있어야 하며, MCP를 통해
    비밀번호를 전달하지 않는다 — DB 계정과 시스템 계정이 분리되어 서비스되는
    환경에서도 각자 독립적으로 인증할 수 있다). resource_id/region 지정 시(Azure SQL
    DB/MI인 경우) Azure Monitor 메트릭도 포함한다.

    [자율 발견→재호출 루프] host만으로는 Azure SQL DB/MI의 ARM resource_id를
    유도할 수 없다. 배포 형태가 Azure로 감지되었는데 resource_id 없이 호출하면
    결과 JSON의 최상위 `needs_input`에 안내가 채워진다. discovery_hint를 참고해
    값을 확정한 뒤 재호출하면 채워진다."""
    if not COMPUTER.match(host or ""):
        raise ValueError("invalid host")
    if auth_mode not in ("entra", "sql"):
        raise ValueError("invalid auth_mode")
    if resource_id and not RID.match(resource_id):
        raise ValueError("invalid resource_id")
    cmd = [MSSQL_TOOL, "--host", host, "--user", user, "--database", database,
          "--auth-mode", auth_mode, "--format", "json", "--hours", str(hours)]
    if resource_id:
        cmd += ["--resource-id", resource_id]
    if region:
        cmd += ["--region", region]
    return _run("mssql_diagnose", cmd, 180)

@mcp.tool()
def diagnose_mysql(host: str, user: str, database: str = "", auth_mode: str = "entra",
                   resource_id: str = "", region: str = "", hours: int = 24) -> dict:
    """MySQL 진단(읽기 전용, 결과 JSON) — 온프레미스/IaaS/Azure Database for MySQL
    Flexible Server 전부 지원(같은 바이너리).
    host: MySQL 호스트/FQDN. user: 로그인 이름(Entra 이름 또는 MySQL 사용자) — 필수.
    auth_mode: 'entra'(기본, Entra 토큰) 또는 'mysql'(네이티브 인증 — 이 경우
    컴테이너 환경변수 MYSQL_DIAGNOSE_PASSWORD가 미리 설정돼 있어야 하며, MCP를
    통해 비밀번호를 전달하지 않는다). resource_id/region 지정 시(Azure DB for
    MySQL Flexible Server인 경우) Azure Monitor 메트릭도 포함한다.

    [자율 발견→재호출 루프] host가 *.mysql.database.azure.com으로 보이는데
    resource_id 없이 호출하면 결과 JSON의 최상위 `needs_input`에 안내가 채워진다.
    discovery_hint를 참고해 값을 확정한 뒤 재호출하면 채워진다."""
    if not COMPUTER.match(host or ""):
        raise ValueError("invalid host")
    if auth_mode not in ("entra", "mysql"):
        raise ValueError("invalid auth_mode")
    if resource_id and not RID.match(resource_id):
        raise ValueError("invalid resource_id")
    cmd = [MYSQL_TOOL, "--host", host, "--user", user,
          "--auth-mode", auth_mode, "--format", "json", "--hours", str(hours)]
    if database:
        cmd += ["--database", database]
    if resource_id:
        cmd += ["--resource-id", resource_id]
    if region:
        cmd += ["--region", region]
    return _run("mysql_diagnose", cmd, 180)

@mcp.tool()
def diagnose_windows_os(computer: str = "", workspace_id: str = "", resource_id: str = "",
                        hours: int = 24, source: str = "azure-monitor",
                        host: str = "", winrm_user: str = "", skip_patch_check: bool = False,
                        skip_upgrade_check: bool = False,
                        start_time: str = "", end_time: str = "") -> dict:
    """Windows 서버 OS 진단(읽기 전용, 결과 JSON) — 두 가지 수집 방식 중 선택.

    source='azure-monitor'(기본): computer(Log Analytics 'Computer' 컬럼 값) + workspace_id로
    이미 수집된 Azure Monitor Agent 원격 측정을 조회. Azure VM/Arc-enabled server 전용.

    source='direct': host + winrm_user로 WinRM에 직접 접속해 실시간 수집. Azure Monitor를
    쓰지 않는 온프레미스/AWS/GCP 등 어떤 환경에서도 동작. 비밀번호는 이 도구의 인자로 전달되지
    않는다 — MCP를 구동하는 컨테이너에 WINDOWS_DIAGNOSE_WINRM_PASSWORD 환경변수가 미리
    설정돼 있어야 한다.

    resource_id 지정 시(두 방식 공통, Azure/Arc VM인 경우) VM 전원 상태/크기/OS 버전(제어 평면) 포함.

    [자율 발견→재호출 루프] source='azure-monitor'에서 computer만으로는 연결된 Log Analytics
    워크스페이스나 VM의 ARM resource_id를 직접 유도할 수 없다. workspace_id/resource_id 없이
    호출해 결과 JSON의 최상위 `needs_input`에 해당 parameter 항목이 있으면, 그 안의
    discovery_hint를 참고해 Resource Graph로 값을 확정한 뒤 재호출하면 해당 섹션까지 채워진다."""
    if source not in ("azure-monitor", "direct"):
        raise ValueError("invalid source")
    if resource_id and not RID.match(resource_id):
        raise ValueError("invalid resource_id")
    if source == "azure-monitor":
        if not COMPUTER.match(computer or ""):
            raise ValueError("invalid computer")
        if workspace_id and not WORKSPACE_ID.match(workspace_id):
            raise ValueError("invalid workspace_id")
        cmd = [WINDOWS_TOOL, "--computer", computer, "--format", "json", "--hours", str(hours)]
        if workspace_id:
            cmd += ["--workspace-id", workspace_id]
        if resource_id:
            cmd += ["--resource-id", resource_id]
        if start_time:
            cmd += ["--start-time", _validate_iso_dt(start_time)]
            if end_time:
                cmd += ["--end-time", _validate_iso_dt(end_time)]
        return _run("windows_diagnose", cmd, 180)
    if not COMPUTER.match(host or ""):
        raise ValueError("invalid host")
    if not winrm_user:
        raise ValueError("winrm_user is required when source='direct'")
    cmd = [WINDOWS_TOOL, "--source", "direct", "--host", host, "--winrm-user", winrm_user,
          "--format", "json", "--hours", str(hours)]
    if resource_id:
        cmd += ["--resource-id", resource_id]
    if skip_patch_check:
        cmd += ["--skip-patch-check"]
    if skip_upgrade_check:
        cmd += ["--skip-upgrade-check"]
    return _run("windows_diagnose", cmd, 240)

@mcp.tool()
def diagnose_linux_os(computer: str = "", workspace_id: str = "", resource_id: str = "",
                      hours: int = 24, source: str = "azure-monitor",
                      host: str = "", ssh_user: str = "", skip_patch_check: bool = False,
                      skip_upgrade_check: bool = False,
                      start_time: str = "", end_time: str = "") -> dict:
    """Linux 서버 OS 진단(읽기 전용, 결과 JSON) — 두 가지 수집 방식 중 선택.

    source='azure-monitor'(기본): computer(Log Analytics 'Computer' 컬럼 값) + workspace_id로
    이미 수집된 Azure Monitor Agent 원격 측정을 조회. Azure VM/Arc-enabled server 전용.

    source='direct': host + ssh_user로 SSH에 직접 접속해 실시간 수집. Azure Monitor를 쓰지
    않는 온프레미스/AWS/GCP 등 어떤 환경에서도 동작. 비밀번호는 이 도구의 인자로 전달되지
    않는다 — MCP를 구동하는 컨테이너에 LINUX_DIAGNOSE_SSH_PASSWORD 환경변수(또는 마운트된
    SSH 키)가 미리 준비돼 있어야 한다.

    resource_id 지정 시(두 방식 공통, Azure/Arc VM인 경우) VM 전원 상태/크기/OS 버전(제어 평면) 포함.

    [자율 발견→재호출 루프] source='azure-monitor'에서 computer만으로는 연결된 Log Analytics
    워크스페이스나 VM의 ARM resource_id를 직접 유도할 수 없다. workspace_id/resource_id 없이
    호출해 결과 JSON의 최상위 `needs_input`에 해당 parameter 항목이 있으면, 그 안의
    discovery_hint를 참고해 Resource Graph로 값을 확정한 뒤 재호출하면 해당 섹션까지 채워진다."""
    if source not in ("azure-monitor", "direct"):
        raise ValueError("invalid source")
    if resource_id and not RID.match(resource_id):
        raise ValueError("invalid resource_id")
    if source == "azure-monitor":
        if not COMPUTER.match(computer or ""):
            raise ValueError("invalid computer")
        if workspace_id and not WORKSPACE_ID.match(workspace_id):
            raise ValueError("invalid workspace_id")
        cmd = [LINUX_TOOL, "--computer", computer, "--format", "json", "--hours", str(hours)]
        if workspace_id:
            cmd += ["--workspace-id", workspace_id]
        if resource_id:
            cmd += ["--resource-id", resource_id]
        if start_time:
            cmd += ["--start-time", _validate_iso_dt(start_time)]
            if end_time:
                cmd += ["--end-time", _validate_iso_dt(end_time)]
        return _run("linux_diagnose", cmd, 180)
    if not COMPUTER.match(host or ""):
        raise ValueError("invalid host")
    if not ssh_user:
        raise ValueError("ssh_user is required when source='direct'")
    cmd = [LINUX_TOOL, "--source", "direct", "--host", host, "--ssh-user", ssh_user,
          "--format", "json", "--hours", str(hours)]
    if resource_id:
        cmd += ["--resource-id", resource_id]
    if skip_patch_check:
        cmd += ["--skip-patch-check"]
    if skip_upgrade_check:
        cmd += ["--skip-upgrade-check"]
    return _run("linux_diagnose", cmd, 240)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")   # 엔드포인트: /mcp
