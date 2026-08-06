[한국어](README.md) | **English**

# diag-tools MCP Server

A server that exposes the Azure diagnostic tool suite (`pg_diagnose` / `aks_diagnose` /
`adx_diagnose` / `eh_diagnose` / `agw_diagnose` / `svcmap_diagnose` / `windows_diagnose` /
`linux_diagnose` / `mssql_diagnose` / `mysql_diagnose`) as **MCP (Model Context Protocol) tools**. Azure SRE Agent or any MCP
client can call each diagnostic tool and receive **read-only** diagnostic results as JSON.

> [!NOTE]
> All diagnostics are read-only. They never modify resources, and result JSON is returned after secret/PII/prompt-injection filtering (`_clean`).

---

## Folder Structure

The MCP server anchors each tool's path to the **script location (`__file__`)** (independent of the working directory `cwd`). Each tool has its own folder + `requirements.txt`.

```
Install File/            # = BASE (parent folder of mcp_server.py)
├── mcp/
│   ├── mcp_server.py    # MCP server (tool registration)
│   └── README.md        # (this document)
├── pg/    pg_diagnose.py
├── aks/   aks_diagnose.py
├── adx/   adx_diagnose.py  + requirements.txt
└── eh/    eh_diagnose.py   + requirements.txt
```

`BASE = dirname(dirname(mcp_server.py))`, and each tool is referenced by the rule `BASE/<name>/<name>_diagnose.py`.

> [!IMPORTANT]
> There is no unified `requirements.txt`. To avoid conflicts and over-installation from independent per-tool dependencies, **each tool keeps its own `requirements.txt`**.

---

## Registered Tools

| MCP tool | Target | Key arguments | Internal invocation |
|---|---|---|---|
| `diagnose_postgres` | PostgreSQL Flexible Server | `host`, `user`, `dbname`, `resource_id`, `hours`, `auth_mode`(entra\|password) | `pg_diagnose.py --format json` |
| `diagnose_aks` | AKS cluster | `namespace`, `context`, `all_namespaces`, `prometheus_url`, `appinsights_id` | `aks_diagnose.py --format json` |
| `diagnose_adx` | Azure Data Explorer (Kusto) | `cluster`, `database`, `resource_id`, `region`, `hours` | `adx_diagnose.py --auth default --format json` |
| `diagnose_eventhub` | Azure Event Hubs | `resource_id`, `event_hub`, `region`, `window_minutes` | `eh_diagnose.py --azure-auth --eh-auth entra --format json` |
| `diagnose_service_map` | Workload service map | `appinsights_id`, `workspace_id`, `workload`, `window_minutes` | `svcmap_diagnose.py --format json` |
| `diagnose_appgateway` | Application Gateway | `resource_id`, `region`, `window_minutes`, `backend_health` | `agw_diagnose.py --azure-auth --format json` |
| `diagnose_windows_os` | Windows server (Azure VM/Arc, or on-prem/AWS/GCP) | `source`(azure-monitor\|direct), `computer`/`workspace_id` or `host`/`winrm_user`, `resource_id`, `hours`, `start_time`/`end_time` | `windows_diagnose.py --format json` |
| `diagnose_linux_os` | Linux server (Azure VM/Arc, or on-prem/AWS/GCP) | `source`(azure-monitor\|direct), `computer`/`workspace_id` or `host`/`ssh_user`, `resource_id`, `hours`, `start_time`/`end_time` | `linux_diagnose.py --format json` |
| `diagnose_mssql` | SQL Server (on-prem/IaaS/Azure SQL DB/MI) | `host`, `user`, `database`, `auth_mode`, `resource_id`, `region`, `hours` | `mssql_diagnose.py --format json` |
| `diagnose_mysql` | MySQL (on-prem/IaaS/Azure DB for MySQL) | `host`, `user`, `database`, `auth_mode`, `resource_id`, `region`, `hours` | `mysql_diagnose.py --format json` |

Each tool validates input (`RID`/`NS`/`CLUSTER`/`WORKSPACE_ID`/`COMPUTER` regex), then runs the
diagnostic via `subprocess.run([..., "--format", "json"])` and returns `json.loads(stdout)`.
Arguments are passed as an **argv list**, not a shell string.

### Output schema (Finding fields differ per tool)
- Common top level: `tool` / `target` / `health_score` / `findings[]`
- Severity values: **critical / warning / info / ok** (NOT high/medium/low)
- `pg` · `adx`: `findings[]` = severity / category / title / detail / recommendation
- `aks`: severity / component / title / detail / recommendation / **steps (list)**
- `eh`: its own schema (`checks[]` = category / severity / title / detail / **recommendation** / evidence) plus top-level `worst_severity` / **`health_score`** / **`severity_counts`** / **`summary`** (one-line NL) / **`recommended_actions[]`** (prioritized: severity / category / title / action)

---

## ⚙️ Prerequisites

**When using MCP server / Azure SRE Agent** (i.e. already deployed to ACA)
- Nothing to do on your side — just register the MCP connector (`mcpEndpoint`) with the SRE Agent. The server already runs on Azure Container Apps, all 6 diagnostic tools are already installed in the image, and Azure credentials are already configured via Managed Identity.

**When running this MCP server itself standalone locally** (for dev/testing)
- Python 3.10+
- All 6 diagnostic tools' `requirements.txt` installed into the same Python environment
- Azure credentials (`az login` or environment variables) set up locally

---

## ⚙️ Installation & Execution

**When using MCP server / Azure SRE Agent**, no installation is needed — the ACA deployment from `infra/main.bicep` is already running (see "Deployment" below for details).

**When running the MCP server standalone locally**:

## Running

```powershell
# 1) Install each tool's dependencies into the MCP runtime Python env (per-tool requirements)
pip install -r "..\adx\requirements.txt"
pip install -r "..\eh\requirements.txt"
# (install pg/aks dependencies into the same env too)

# 2) Start the server (streamable-http, default port 8000 → endpoint /mcp)
python mcp_server.py
```

- The port can be changed via the `PORT` environment variable (default `8000`).
- Diagnostics run via `sys.executable`, so Azure credentials (Managed Identity / `az login` /
  environment variables) must be available in the **same Python environment that runs the MCP
  server**.
- For RBAC, see each diagnostic tool's README (e.g., ADX metrics = cluster `Monitoring Reader`,
  Event Hubs data plane = `Azure Event Hubs Data Receiver`).
- To call `diagnose_postgres` with `auth_mode="password"` (a native DB account), the
  `PGPASSWORD` environment variable must already be set on the container/process running MCP —
  the password is never passed as an MCP tool argument. With `auth_mode="entra"` (default), this
  environment variable isn't needed.
- To call `diagnose_mssql`/`diagnose_mysql` with `auth_mode="sql"`/`"mysql"` (a native DB
  account), the `MSSQL_DIAGNOSE_PASSWORD`/`MYSQL_DIAGNOSE_PASSWORD` environment variable must
  already be set on the container/process running MCP — the password is never passed as an MCP
  tool argument (same "never accept plaintext as an argument" design as the CLI). With
  `auth_mode="entra"` (default), this environment variable isn't needed.
- To call `diagnose_windows_os`/`diagnose_linux_os` with `source="direct"` (connecting directly
  via WinRM/SSH without Azure Monitor), the `WINDOWS_DIAGNOSE_WINRM_PASSWORD`/
  `LINUX_DIAGNOSE_SSH_PASSWORD` environment variable (or, for Linux, a mounted SSH key) must
  already be set up on the container, and the password is never passed as an MCP tool argument.
  With `source="azure-monitor"` (default), this credential isn't needed.

### Container build (optional)
```
az acr build -r $ACR -t diag-mcp:v1 -f mcp/Dockerfile .   # from the BASE (Install File) root
```

---

## Deployment (CI/CD → ACR → ACA)

> [!IMPORTANT]
> **Where the source lives (GitHub) ≠ where it runs (the resident MCP server)**.
> GitHub is only involved **at deploy time**: build the image → push to ACR (**version-pinned**) →
> redeploy to ACA. GitHub is **not** in the runtime request loop (SRE Agent → `/mcp` call →
> diagnostic run → JSON). There's no clone/`pip install` on every request (this avoids latency,
> supply-chain, reproducibility, and auth issues).

```
GitHub repo (Install File/)
   │  git push (main) / tag v1.2.0
   ▼
GitHub Actions (.github/workflows/deploy-mcp.yml)
   │  az acr build  →  ACR: diag-mcp:<version> (version-pinned) + :latest
   ▼
diag-mcp runs resident in Azure Container Apps  ← diagnostic code is already baked into the image
   ▲   (read-only access to ADX/PG/AKS/EH via User-Assigned Managed Identity)
   │  MCP calls (/mcp, streamable-http) — on every request
SRE Agent  ←  triggered by user/incident
```

### 1. Provision infrastructure (Bicep)
`infra/main.bicep` — Log Analytics + Container Apps environment + **User-Assigned Managed
Identity** + Container App (port 8000, `/mcp`) + ACR Pull role.

```powershell
az deployment group create -g <rg> `
  -f infra/main.bicep -p infra/main.bicepparam `
  -p acrName=<ACR name>
# Output: mcpEndpoint / identityPrincipalId / identityClientId
```

### 2. Grant least-privilege access (read-only)
Since all diagnostics are read-only, MI permissions are minimized to query/metrics-read as well.
Use the `identityPrincipalId` from the `infra` output:

```powershell
./infra/assign-roles.ps1 -PrincipalId <identityPrincipalId> `
  -MonitoringScope "/subscriptions/<sub>/resourceGroups/<rg>" `
  -EventHubNamespaceId "<eh-namespace-resource-id>" `
  -AksClusterId "<aks-resource-id>"
```

| Target | Azure RBAC (granted by the script) | Data plane (granted separately) |
|---|---|---|
| Common | `Reader`, `Monitoring Reader` | — |
| PostgreSQL | `Reader`/`Monitoring Reader` | Register the MI as an Entra user, then `GRANT pg_monitor` |
| ADX | `Reader`/`Monitoring Reader` | Database `Viewer` (or AllDatabasesViewer) |
| Event Hubs | `Azure Event Hubs Data Receiver` | — |
| AKS | `AKS Cluster User Role` | K8s RBAC `view` ClusterRole binding |

> [!NOTE]
> Data-plane permissions are granted inside each service, not via Azure RBAC (see the script's
> guidance output after running it).

### 3. CI/CD (GitHub Actions)
`.github/workflows/deploy-mcp.yml` — runs on a push to `main` or a `v*` tag. Uses **OIDC login**
(no secrets stored as credentials), `az acr build` (version-pinned tag = the release tag or
`sha-<short>`), and `az containerapp update`.

Repo secrets (Settings → Secrets → Actions):

| Secret | Purpose |
|---|---|
| `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_SUBSCRIPTION_ID` | App used for Azure OIDC login (federated credential) |
| `ACR_NAME` | ACR used to build/store the image |
| `ACA_NAME` / `ACA_RESOURCE_GROUP` | Target Container App for deployment |

> If you need a deployment approval gate, add protection rules to the workflow's
> `environment: production`.

### 4. Register the SRE Agent connector
Register the `infra` output `mcpEndpoint` (`https://<fqdn>/mcp`) as the SRE Agent's MCP connector.

> [!WARNING]
> `externalIngress=true` (the PoC default) exposes the MCP endpoint publicly. In production, keep
> `externalIngress=false` (internal) and put **authentication behind APIM/Private Endpoint**, or at
> minimum apply IP restrictions. Diagnostic output is filtered for secret/PII/prompt-injection via
> `_clean`, but access control for the endpoint itself must be secured separately.

### 4.1 Endpoint access control (optional, defaults preserve existing behavior)

The following parameters have been added to `main.bicep`. **All defaults match the previous
behavior** (no auth, no IP restriction), and are only turned on when needed. Before enabling,
also consider combining with `externalIngress=false` + a Private Endpoint.

| Parameter | Default | Behavior when enabled |
|---|---|---|
| `enableEntraAuth` | `false` | Turns on ACA Easy Auth (platform-level) in front of `/mcp`, blocking requests without a valid token with **401**. No `mcp_server.py` code changes needed. |
| `entraAuthClientId` | `''` | Required when `enableEntraAuth=true` — the client ID of the App Registration representing this MCP API. |
| `entraAuthTenantId` | The deployment subscription's tenant | The token-issuing tenant (leave as default unless this is a multi-tenant app). |
| `allowedIpRanges` | `[]` (unrestricted) | When populated (e.g., `['203.0.113.0/24']`), applies an IP allow-list to ingress; traffic outside the list is blocked automatically. |

```bicepparam
// main.bicepparam example — when moving to production
param enableEntraAuth = true
param entraAuthClientId = '<mcp-api-app-registration-client-id>'
param allowedIpRanges = ['<SRE-Agent-egress-CIDR>']
```

> [!NOTE]
> After deploying with `enableEntraAuth=true`, the SRE Agent's MCP connector must also be
> configured to acquire a token for `entraAuthClientId` (or that API's `api://<clientId>` scope)
> and call with `Authorization: Bearer <token>`. Otherwise, even valid calls will be blocked with
> 401 — verify in a non-production environment first before applying.

### 4.2 Health check
If the installed `mcp` SDK supports `custom_route`, `mcp_server.py` additionally exposes
`GET /health` (no auth required, `{"status":"ok"}`). On unsupported versions, it's silently
skipped and existing behavior (e.g., `/mcp`) is unaffected.

---

## Adding a New Diagnostic Tool

Diagnostic tools keep getting added. When attaching a new tool (`<name>_diagnose.py`), follow these 4 steps.

### 1. Place the folder + files
```
Install File/<name>/
├── <name>_diagnose.py
└── requirements.txt
```

### 2. Support JSON output in the diagnostic (required)
Output **pure JSON only** to `stdout` (send logs/notes to `stderr`). Like pg/adx/eh, pass it through secret/PII/prompt-injection sanitization.

The pattern adds roughly 5 spots to the diagnostic source:

| # | Location (anchor) | What |
|---|-------------------|------|
| 0 | `from dataclasses import dataclass, field` | append `, asdict` if needed |
| 1 | after `demo: bool = False` in `class Config:` | add `format: str = "html"` field |
| 2 | right above `def parse_args(...)` | add `emit_json()` + `_clean()` functions |
| 3 | after argparse `--demo` | add `--format {html,json}` (or `{table,json}`) argument |
| 4 | `return Config(... demo=a.demo)` | append `, format=a.format` |
| 5 | after `score = health_score(...)` in `main()` (before snapshot/HTML) | `if cfg.format=="json": emit_json(...); return 0` |

```python
import re, sys, json
from dataclasses import asdict

_SECRET = re.compile(r'(?i)(password|pwd|secret|connection ?string|accountkey|sas|token|apikey)\s*[=:]\s*\S+')
_RRN    = re.compile(r'\b\d{6}-\d{7}\b')
_EMAIL  = re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.-]+\b')
_INJECT = re.compile(r'(?i)(ignore (all|previous)|system prompt|<\s*important\s*>|assistant\s*:|tool_call)')

def _clean(v):
    if isinstance(v, str):
        v = _SECRET.sub(r'\1=***', v); v = _RRN.sub('[PII]', v)
        v = _EMAIL.sub('[PII]', v);    v = _INJECT.sub('[filtered]', v)
        return v
    if isinstance(v, dict):  return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):  return [_clean(x) for x in v]   # recursively mask lists (e.g. aks steps)
    return v

def emit_json(findings, score, target, tool="<name>_diagnose"):
    payload = {"tool": tool, "target": target, "health_score": score,
               "findings": [{k: _clean(val) for k, val in asdict(f).items()} for f in findings]}
    json.dump(payload, sys.stdout, ensure_ascii=False)
```

Add `--format {html|json}` (or `{table|json}`) to the CLI, and when `json`, call `emit_json(...)` and return immediately.

### 3. Add two parts to `mcp_server.py`
One path constant + one thin `@mcp.tool()` wrapper:

```python
# path (BASE/<name>/<name>_diagnose.py)
XXX_TOOL = os.path.join(BASE, "<name>", "<name>_diagnose.py")

@mcp.tool()
def diagnose_<name>(resource_id: str = "", ...) -> dict:
    """<target> diagnosis (read-only, JSON result)."""
    if resource_id and not RID.match(resource_id):     # input validation
        raise ValueError("invalid resource_id")
    cmd = [sys.executable, XXX_TOOL, "--format", "json", ...]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=240)
    return json.loads(out.stdout)
```

- Validation regexes at the top of the server: `RID` (resource id), `NS` (namespace), `CLUSTER` (ADX cluster URI). Add a new pattern for a new type.
- Tune `timeout` to the target scale (default 180–240s).

### 4. Install dependencies + verify
```powershell
pip install -r "..\<name>\requirements.txt"
python ..\<name>\<name>_diagnose.py --demo --format json | python -m json.tool
# → check tool/target/health_score/findings keys
```

---

## Security Principles
- **Read-only**: only query/`.show`/ARM read/Azure Monitor read calls. Never modifies resources.
- **Output sanitization**: all JSON output is filtered for secret/PII/prompt-injection via `_clean`.
- **Input validation**: the MCP wrapper validates resource_id/namespace/cluster via regex before execution.
- **Argument-injection prevention**: user input is passed as an **argv list**, not a shell string (`subprocess.run([...])`).

---

## Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `json.loads` fails | The diagnostic mixed non-JSON text into stdout → logs must go to `stderr` |
| `FileNotFoundError` (tool path) | Check the location/spelling of `BASE/<name>/<name>_diagnose.py` |
| `ModuleNotFoundError` | The tool's `requirements.txt` is not installed in the MCP runtime Python env |

---

## License

This project is licensed under the [MIT License](LICENSE).
