# Shell 命令执行工具设计文档（BashTool Standardized）
版本：1.1.0（引入半沙箱增强）  
协议标准：Standard Envelope v1.0（详见 `docs/通用工具响应协议.md`）

## 1. 概述
BashTool 用于在**项目根目录沙箱**内执行 Shell 命令。它允许命令串联与受限 `cd`，并通过最小黑名单、路径校验以及**半沙箱增强（Half-Sandbox Enhancement）**机制，在不引入重度容器化（如 Docker）的前提下，最大程度防御大模型的幻觉误操作和资源滥用。

核心特性：
- 受限执行：默认在 project root 内执行，`cd` 仅允许在项目根内
- 混合安全屏障：正则黑名单 + Unix SetUID 降权 + Resource Limit 资源控制
- 可选文件隔离：支持 `use_temp_sandbox` 临时替身目录运行模式
- 标准化响应：统一 `status/data/text/stats/context` 结构
- 输出保护：超时与输出截断

## 2. 接口规范
### 2.1 工具定义
- Internal Class Name: `BashTool`
- Python Module: `tools/builtin/bash.py`（规划）
- Agent Exposed Name: **`Bash`**

### 2.2 输入参数（JSON Schema）
```json
{
  "name": "Bash",
  "description": "Execute a shell command within the project sandbox.",
  "parameters": {
    "type": "object",
    "properties": {
      "command": {
        "type": "string",
        "description": "The shell command to execute. Command chaining (&&, ||, ;) is allowed."
      },
      "directory": {
        "type": "string",
        "description": "Working directory relative to project root. Defaults to '.'.",
        "default": "."
      },
      "timeout_ms": {
        "type": "integer",
        "description": "Execution timeout in milliseconds. Default 120000, max 600000.",
        "default": 120000
      }
    },
    "required": ["command"]
  }
}
```

## 3. 输出结构（标准信封）
严格遵循《通用工具响应协议》，顶层字段仅允许：  
`status`, `data`, `text`, `stats`, `context`（且仅当 `status="error"` 时出现 `error` 字段）。

### 3.1 data 字段定义
- `stdout` (string): 标准输出（可能被截断）
- `stderr` (string): 标准错误输出（可能被截断）
- `exit_code` (int | null): 进程退出码（超时/被杀时可为 null）
- `signal` (string | null): 被信号终止时的信号名（如 `SIGKILL`）
- `truncated` (boolean): 输出是否截断
- `command` (string): 实际执行的命令（原样）
- `directory` (string): 实际执行目录（相对 project root）

### 3.2 context 字段定义
- `cwd` (string): **必填**，执行时工作目录（相对 project root）
- `params_input` (object): **必填**，原始参数
- `directory_resolved` (string): 解析后的规范化相对路径（POSIX）

### 3.3 成功示例
```json
{
  "status": "success",
  "data": {
    "stdout": "...",
    "stderr": "",
    "exit_code": 0,
    "signal": null,
    "truncated": false,
    "command": "pytest tests/unit",
    "directory": "."
  },
  "text": "Command succeeded: pytest tests/unit\n(Exit code 0. Took 1240ms)",
  "stats": {"time_ms": 1240, "stdout_bytes": 1200, "stderr_bytes": 0},
  "context": {"cwd": ".", "params_input": {"command": "pytest tests/unit"}, "directory_resolved": "."}
}
```

## 4. 状态机与逻辑判定
### 4.1 status = "success"
- 命令执行完成
- `exit_code == 0`
- 未截断、未超时

### 4.2 status = "partial"
满足任一情况：
- 退出码非 0（仍返回 stdout/stderr）
- 超时但已有部分输出

### 4.3 status = "error"
- 参数校验失败
- 目录非法或越权
- 命令被安全规则阻止
- 超时且无输出

## 5. 执行与安全约束（半沙箱防御体系）

为了防止模型由于幻觉生成死循环脚本、越权操作或破坏代码库，BashTool 实现了一套四层防御体系。

### 5.1 L1: 工作目录与 `cd` (Path Jail)
- `directory` 解析为 project root 内的相对路径
- 允许命令中使用 `cd`，但每一个 `cd` 目标路径必须位于 project root 内
- `cd` 支持相对路径与绝对路径，均需通过沙箱验证

### 5.2 L2: 命令黑名单与工具正交 (Semantic Block)
- **禁止交互命令**：以下命令或模式视为交互式，直接拒绝：
  - 编辑器/交互式工具：`vim`, `vi`, `nano`, `less`, `more`, `top`, `htop`, `watch`, `tmux`, `screen`
  - 交互式 Git：`git rebase -i`, `git add -i`
  - 需要登录或会话的命令：`ssh`, `scp`, `sftp`, `ftp`
- **最小黑名单（破坏性命令）**：
  - 破坏性系统命令：`mkfs`, `fdisk`, `dd`, `shutdown`, `reboot`, `poweroff`, `halt`
  - 破坏性删除：`rm -rf /`, `rm -rf /*`
  - 权限提升：`sudo`, `su`, `doas`
  - 远程脚本执行：`curl | bash`, `wget | bash`, `bash <(curl ...)`
  - 网络工具：`curl`, `wget`（默认禁用；可通过环境变量 `BASH_ALLOW_NETWORK=true` 开启）
- **禁止使用“读/搜/列”类 Shell 命令**：
  - 为了保持工具职责清晰，以下命令不允许使用：`ls`, `cat`, `head`, `tail`, `grep`, `find`, `rg`
  - 应使用相应工具：`LS / Read / Grep / Glob`。

### 5.3 L3: Unix 资源与权限边界 (Resource & Privilege Limits)
在 `subprocess.run` 调用期间，通过 `preexec_fn` 注入底层约束（限 Unix/类 Unix 系统）：
- **防死循环 (RLIMIT_CPU)**：设定进程最大 CPU 时间（CPU time），防止进程捕获信号而拒绝挂起。
- **防内存炸雷 (RLIMIT_AS)**：设置进程地址空间上限，防止大模型运行恶意耗存脚本（如 fork 炸弹变体）。
- **权限降级 (SetUID Drop)**：如果系统配置允许（如环境变量传入 `SANDBOX_UID`），进程 fork 后、目标命令 exec 前，主动调用 `os.setuid()` 放弃当前高权限，降权运行目标脚本。

### 5.4 L4: 替身软隔离 (Virtual Workspace - 规划实现)
- 支持开启 `use_temp_sandbox=True` 模式（Dry-Run Sandbox）。
- 该模式下，底层透明地将原本的 `target_dir` 拷贝或硬链接重组至 `/tmp/...` 等临时挂载点中。
- 大模型执行修改、构建测试后，如果验证失败或熔断，产生的破损代码直接随临时文件夹销毁，零污染主 Repo。

### 5.5 输出截断与环境清理
- Bash 的原始输出会在写入 history 前经过 **ObservationTruncator** 统一截断（上限 50KB/2000行）。
- 截断阈值与方向由环境变量控制（见 `docs/工具输出截断设计文档.md`）。
- 若触发截断，工具响应会被包装为框架级 `partial`（保持协议一致）。
- 自动注入 `MYCODEAGENT=1` 环境变量（脚本可据此禁用交互提示等行为）。
- 继承父进程环境变量（如 `PATH`, `HOME`）。

## 6. 错误处理规范
| 场景 | error.code | error.message / text 建议 |
| --- | --- | --- |
| command 缺失 | INVALID_PARAM | "Missing required parameter 'command'." |
| timeout_ms 非法 | INVALID_PARAM | "timeout_ms must be an integer between 1 and 600000." |
| directory 不存在 | NOT_FOUND | "Directory '{path}' does not exist." |
| directory 非目录 | INVALID_PARAM | "'{path}' is not a directory." |
| 越权访问 | ACCESS_DENIED | "Access denied. Path must be within project root." |
| 命令被阻止 | INVALID_PARAM | "Command blocked by safety rules." |
| 系统权限不足 | PERMISSION_DENIED | "Permission denied executing command." |
| 超时无输出 | TIMEOUT | "Command timed out with no output." |
| 其它执行异常 | EXECUTION_ERROR | "Command failed: {details}" |

## 7. text 字段规范
统一格式：
```
Command {succeeded|failed}: <command>
(Exit code X. Took Tms)
[Optional warnings about truncation/timeout]

<stdout/stderr 摘要...>
```

（未来扩展）当截断时：
```
[Truncated: Output exceeded limit. Narrow command or redirect to file.]
```

### 示例（成功）
```
Command succeeded: pytest tests/unit
(Exit code 0. Took 1240ms)

--- STDOUT (1200 bytes) ---
===== test session starts =====
collected 42 items
tests/unit/test_api.py::test_get ✓
...
===== 42 passed in 1.2s =====
```

### 示例（失败）
```
Command failed: npm test
(Exit code 1. Took 3450ms)

--- STDERR (850 bytes) ---
Error: Cannot find module 'jest'
    at Function.Module._resolveFilename ...
```

## 8. MVP 限制与后续扩展
- 不支持交互式命令（后续可引入 pty）
- 不支持后台任务（后续可增加 `background=true`）
- 黑名单为最小集合（可扩展为规则引擎）
