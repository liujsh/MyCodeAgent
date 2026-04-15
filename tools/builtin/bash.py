"""Shell 命令执行工具 (Bash)

遵循《通用工具响应协议》，返回标准化结构。
在项目根目录沙箱内执行 Shell 命令，支持命令串联与受限 cd。
支持轻量级半沙箱化：资源限制、权限降权、临时文件系统隔离。
"""

import os
import platform
import re
import signal
import subprocess
import tempfile
import shutil
import time
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from prompts.tools_prompts.bash_prompt import bash_prompt
from ..base import Tool, ToolParameter, ToolStatus, ErrorCode
from core.env import load_env

load_env()

# 配置日志记录
logger = logging.getLogger(__name__)

# 平台兼容性：resource 模块仅在 Unix 可用
try:
    import resource
    HAS_RESOURCE = True
except ImportError:
    HAS_RESOURCE = False
    logger.debug("resource module not available (non-Unix platform)")


@dataclass
class BashSandboxConfig:
    """Bash 沙箱配置

    支持从环境变量加载配置，提供资源限制、权限降权和临时沙箱设置。
    """
    # 资源限制
    max_cpu_time_sec: int = 120  # 默认 120 秒 CPU 时间
    max_memory_mb: int = 512     # 默认 512MB 内存
    max_processes: int = 1024    # 最大进程数

    # 降权配置
    drop_privileges: bool = False
    target_uid: Optional[int] = None
    target_gid: Optional[int] = None

    # 临时沙箱
    use_temp_sandbox: bool = False
    temp_sandbox_mode: str = "copy"  # copy | symlink
    max_file_size_mb: int = 10

    @classmethod
    def from_env(cls) -> "BashSandboxConfig":
        """从环境变量加载配置"""
        # 资源限制
        max_cpu = int(os.environ.get("BASH_MAX_CPU_TIME", "120"))
        max_mem = int(os.environ.get("BASH_MAX_MEMORY_MB", "512"))
        max_proc = int(os.environ.get("BASH_MAX_PROCESSES", "1024"))

        # 降权配置
        drop_priv = os.environ.get("BASH_DROP_PRIVILEGES", "false").lower() == "true"
        uid_str = os.environ.get("BASH_SANDBOX_UID")
        gid_str = os.environ.get("BASH_SANDBOX_GID")
        target_uid = int(uid_str) if uid_str else None
        target_gid = int(gid_str) if gid_str else None

        # 临时沙箱
        use_temp = os.environ.get("BASH_USE_TEMP_SANDBOX", "false").lower() == "true"
        temp_mode = os.environ.get("BASH_TEMP_MODE", "copy")
        max_file_mb = int(os.environ.get("BASH_MAX_FILE_SIZE_MB", "10"))

        return cls(
            max_cpu_time_sec=max_cpu,
            max_memory_mb=max_mem,
            max_processes=max_proc,
            drop_privileges=drop_priv,
            target_uid=target_uid,
            target_gid=target_gid,
            use_temp_sandbox=use_temp,
            temp_sandbox_mode=temp_mode,
            max_file_size_mb=max_file_mb,
        )


@dataclass
class ExecutionResult:
    """命令执行结果"""
    stdout: str
    stderr: str
    exit_code: Optional[int]
    signal_name: Optional[str] = None
    timed_out: bool = False
    resource_limited: bool = False  # 是否因资源限制被终止
    resource_limit_message: Optional[str] = None


class BashTool(Tool):
    """Shell 命令执行工具，支持命令串联与沙箱限制"""

    # 默认超时时间（毫秒）
    DEFAULT_TIMEOUT_MS = 120000
    
    # 最大超时时间（毫秒）
    MAX_TIMEOUT_MS = 600000

    # 交互式命令黑名单（直接拒绝）
    INTERACTIVE_COMMANDS: Set[str] = {
        "vim", "vi", "nano", "less", "more", "top", "htop",
        "watch", "tmux", "screen", "ssh", "scp", "sftp", "ftp",
    }
    
    # 破坏性系统命令黑名单
    DESTRUCTIVE_COMMANDS: Set[str] = {
        "mkfs", "fdisk", "dd", "shutdown", "reboot", "poweroff", "halt",
    }
    
    # 权限提升命令黑名单
    PRIVILEGE_COMMANDS: Set[str] = {
        "sudo", "su", "doas",
    }
    
    # 读/搜/列类 Shell 命令黑名单（应使用相应工具）
    READ_SEARCH_COMMANDS: Set[str] = {
        "ls", "cat", "head", "tail", "grep", "find", "rg",
    }

    def __init__(
        self,
        name: str = "Bash",
        project_root: Optional[Path] = None,
        working_dir: Optional[Path] = None,
        sandbox_config: Optional[BashSandboxConfig] = None,
    ):
        """
        初始化 Shell 命令执行工具

        Args:
            name: 工具名称，默认为 "Bash"
            project_root: 项目根目录，用于沙箱限制
            working_dir: 工作目录，用于解析相对路径
            sandbox_config: 沙箱配置，默认从环境变量加载
        """
        if project_root is None:
            raise ValueError("project_root must be provided by the framework")

        super().__init__(
            name=name,
            description=bash_prompt,
            project_root=project_root,
            working_dir=working_dir if working_dir else project_root,
        )

        # 保存项目根目录
        self._root = self._project_root

        # 是否允许网络工具（默认禁用）
        self._allow_network = os.environ.get("BASH_ALLOW_NETWORK", "false").lower() == "true"

        # 加载沙箱配置
        self._sandbox_config = sandbox_config or BashSandboxConfig.from_env()

        # 日志记录配置
        logger.debug(f"BashTool initialized with sandbox config: {self._sandbox_config}")

    def run(self, parameters: Dict[str, Any]) -> str:
        """
        执行 Shell 命令

        Args:
            parameters: 包含以下键的字典：
                - command: 要执行的命令（必填）
                - directory: 工作目录（相对项目根目录，默认为 "."）
                - timeout_ms: 超时时间（毫秒，默认 120000，最大 600000）

        Returns:
            JSON 格式的响应字符串（遵循《通用工具响应协议》）
        """
        start_time = time.monotonic()
        params_input = dict(parameters)
        
        # 提取参数
        command = parameters.get("command")
        directory = parameters.get("directory", ".")
        timeout_ms = parameters.get("timeout_ms", self.DEFAULT_TIMEOUT_MS)

        # =====================================================================
        # 参数校验
        # =====================================================================
        
        # command 必填
        if not command:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Missing required parameter 'command'.",
                params_input=params_input,
            )
        
        # command 必须是字符串
        if not isinstance(command, str):
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message="Parameter 'command' must be a string.",
                params_input=params_input,
            )
        
        # timeout_ms 校验
        if not isinstance(timeout_ms, int) or timeout_ms < 1 or timeout_ms > self.MAX_TIMEOUT_MS:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message=f"timeout_ms must be an integer between 1 and {self.MAX_TIMEOUT_MS}.",
                params_input=params_input,
            )

        # =====================================================================
        # 安全检查：命令黑名单
        # =====================================================================
        
        safety_result = self._check_command_safety(command)
        if safety_result is not None:
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message=safety_result,
                params_input=params_input,
            )

        # =====================================================================
        # 目录解析与沙箱校验
        # =====================================================================
        
        try:
            # 解析目录路径
            dir_path = Path(directory)
            if dir_path.is_absolute():
                target_dir = dir_path.resolve()
            else:
                target_dir = (self._root / dir_path).resolve()
            
            # 沙箱检查
            target_dir.relative_to(self._root)
            directory_resolved = str(target_dir.relative_to(self._root))
            if not directory_resolved:
                directory_resolved = "."
        except ValueError:
            return self.create_error_response(
                error_code=ErrorCode.ACCESS_DENIED,
                message="Access denied. Path must be within project root.",
                params_input=params_input,
            )
        except OSError as e:
            return self.create_error_response(
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"Path resolution failed: {e}",
                params_input=params_input,
            )
        
        # 检查目录是否存在
        if not target_dir.exists():
            return self.create_error_response(
                error_code=ErrorCode.NOT_FOUND,
                message=f"Directory '{directory}' does not exist.",
                params_input=params_input,
            )
        
        # 检查是否为目录
        if not target_dir.is_dir():
            return self.create_error_response(
                error_code=ErrorCode.INVALID_PARAM,
                message=f"'{directory}' is not a directory.",
                params_input=params_input,
            )

        # =====================================================================
        # 检查命令中的 cd 路径
        # =====================================================================
        
        cd_check_result = self._check_cd_paths(command, target_dir)
        if cd_check_result is not None:
            return self.create_error_response(
                error_code=ErrorCode.ACCESS_DENIED,
                message=cd_check_result,
                params_input=params_input,
            )

        # =====================================================================
        # 执行命令
        # =====================================================================

        # 设置环境变量
        env = os.environ.copy()
        env["MYCODEAGENT"] = "1"

        # 转换超时时间为秒
        timeout_sec = timeout_ms / 1000.0

        stdout = ""
        stderr = ""
        exit_code = None
        signal_name = None
        timed_out = False
        resource_limited = False
        resource_limit_message = None

        try:
            # 使用临时沙箱上下文管理器
            with self._temp_sandbox(target_dir) as sandbox_dir:
                result = self._execute_command(
                    command=command,
                    target_dir=sandbox_dir,
                    timeout_sec=timeout_sec,
                    env=env,
                )
                stdout = result.stdout
                stderr = result.stderr
                exit_code = result.exit_code
                signal_name = result.signal_name
                timed_out = result.timed_out
                resource_limited = result.resource_limited
                resource_limit_message = result.resource_limit_message

                # 如果是临时沙箱模式，记录日志
                if self._sandbox_config.use_temp_sandbox:
                    logger.debug(f"Command executed in temp sandbox: {sandbox_dir}")

        except PermissionError:
            time_ms = int((time.monotonic() - start_time) * 1000)
            return self.create_error_response(
                error_code=ErrorCode.PERMISSION_DENIED,
                message="Permission denied executing command.",
                params_input=params_input,
                time_ms=time_ms,
            )
        except Exception as e:
            time_ms = int((time.monotonic() - start_time) * 1000)
            return self.create_error_response(
                error_code=ErrorCode.EXECUTION_ERROR,
                message=f"Command failed: {e}",
                params_input=params_input,
                time_ms=time_ms,
            )

        # =====================================================================
        # 构建响应
        # =====================================================================
        
        time_ms = int((time.monotonic() - start_time) * 1000)
        
        # 构建 data 字段
        data: Dict[str, Any] = {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "signal": signal_name,
            "truncated": False,  # MVP 阶段不截断
            "command": command,
            "directory": directory_resolved,
        }
        
        # 构建 stats 字段
        extra_stats = {
            "stdout_bytes": len(stdout.encode("utf-8")),
            "stderr_bytes": len(stderr.encode("utf-8")),
        }
        
        # 构建 context 字段
        extra_context = {
            "directory_resolved": directory_resolved,
            "cwd": directory_resolved,
        }
        
        # 构建 text 字段
        if resource_limited:
            # 资源限制导致的失败 -> error
            limit_msg = resource_limit_message or "Command exceeded resource limit"
            return self.create_error_response(
                error_code=ErrorCode.EXECUTION_ERROR,
                message=f"{limit_msg}. Command was terminated due to resource constraints.",
                params_input=params_input,
                time_ms=time_ms,
                extra_context={
                    "directory_resolved": directory_resolved,
                    "cwd": directory_resolved,
                    "signal": signal_name,
                },
            )

        if timed_out:
            if stdout or stderr:
                # 超时但有部分输出 -> partial
                text_lines = [
                    f"Command timed out: {command}",
                    f"(Timeout after {timeout_ms}ms)",
                ]
                if stdout:
                    text_lines.append(f"\n--- STDOUT ({len(stdout.encode('utf-8'))} bytes) ---")
                    text_lines.append(stdout[:1000] + ("..." if len(stdout) > 1000 else ""))
                if stderr:
                    text_lines.append(f"\n--- STDERR ({len(stderr.encode('utf-8'))} bytes) ---")
                    text_lines.append(stderr[:1000] + ("..." if len(stderr) > 1000 else ""))
                text = "\n".join(text_lines)
                
                return self.create_partial_response(
                    data=data,
                    text=text,
                    params_input=params_input,
                    time_ms=time_ms,
                    extra_stats=extra_stats,
                    extra_context=extra_context,
                )
            else:
                # 超时且无输出 -> error
                return self.create_error_response(
                    error_code=ErrorCode.TIMEOUT,
                    message="Command timed out with no output.",
                    params_input=params_input,
                    time_ms=time_ms,
                )
        
        # 判断状态
        if exit_code == 0:
            # 成功
            text_lines = [
                f"Command succeeded: {command}",
                f"(Exit code 0. Took {time_ms}ms)",
            ]
            if stdout:
                text_lines.append(f"\n--- STDOUT ({len(stdout.encode('utf-8'))} bytes) ---")
                text_lines.append(stdout[:2000] + ("..." if len(stdout) > 2000 else ""))
            if stderr:
                text_lines.append(f"\n--- STDERR ({len(stderr.encode('utf-8'))} bytes) ---")
                text_lines.append(stderr[:1000] + ("..." if len(stderr) > 1000 else ""))
            text = "\n".join(text_lines)
            
            return self.create_success_response(
                data=data,
                text=text,
                params_input=params_input,
                time_ms=time_ms,
                extra_stats=extra_stats,
                extra_context=extra_context,
            )
        else:
            # 非零退出码 -> partial
            text_lines = [
                f"Command failed: {command}",
                f"(Exit code {exit_code}. Took {time_ms}ms)",
            ]
            if stdout:
                text_lines.append(f"\n--- STDOUT ({len(stdout.encode('utf-8'))} bytes) ---")
                text_lines.append(stdout[:2000] + ("..." if len(stdout) > 2000 else ""))
            if stderr:
                text_lines.append(f"\n--- STDERR ({len(stderr.encode('utf-8'))} bytes) ---")
                text_lines.append(stderr[:2000] + ("..." if len(stderr) > 2000 else ""))
            text = "\n".join(text_lines)
            
            return self.create_partial_response(
                data=data,
                text=text,
                params_input=params_input,
                time_ms=time_ms,
                extra_stats=extra_stats,
                extra_context=extra_context,
            )

    def _check_command_safety(self, command: str) -> Optional[str]:
        """
        检查命令安全性
        
        Args:
            command: 要检查的命令
            
        Returns:
            如果命令不安全，返回错误消息；否则返回 None
        """
        # 提取命令中的所有"词"（简单分词）
        # 注意：这是一个简化的检查，可能无法捕获所有变体
        # Strip quoted strings to reduce false positives (e.g. echo "ls")
        command_for_scan = re.sub(r'(["\']).*?\1', ' ', command)
        words = re.findall(r'\b\w+\b', command_for_scan.lower())
        
        # 检查交互式命令
        for word in words:
            if word in self.INTERACTIVE_COMMANDS:
                return f"Command blocked by safety rules. Interactive command '{word}' is not allowed."
        
        # 检查交互式 git 命令
        if "git" in words:
            if "rebase" in words and ("-i" in command or "--interactive" in command):
                return "Command blocked by safety rules. Interactive 'git rebase -i' is not allowed."
            if "add" in words and ("-i" in command or "--interactive" in command):
                return "Command blocked by safety rules. Interactive 'git add -i' is not allowed."
        
        # 检查破坏性命令
        for word in words:
            if word in self.DESTRUCTIVE_COMMANDS:
                return f"Command blocked by safety rules. Destructive command '{word}' is not allowed."
        
        # 检查权限提升命令
        for word in words:
            if word in self.PRIVILEGE_COMMANDS:
                return f"Command blocked by safety rules. Privilege escalation command '{word}' is not allowed."
        
        # 检查危险的 rm 命令
        if "rm" in words:
            # 检查 rm -rf / 或 rm -rf /*
            if re.search(r'\brm\s+(-[rf]+\s+)*(/|/\*)\s*$', command):
                return "Command blocked by safety rules. Destructive 'rm -rf /' is not allowed."
            if re.search(r'\brm\s+.*-[rf]*\s+.*(/|/\*)', command):
                # 更宽松的检查
                if "/ " in command or "/*" in command:
                    return "Command blocked by safety rules. Destructive 'rm' on root is not allowed."
        
        # 检查远程脚本执行
        remote_exec_patterns = [
            r'\bcurl\s+.*\|\s*bash',
            r'\bwget\s+.*\|\s*bash',
            r'\bbash\s+<\s*\(\s*curl',
            r'\bbash\s+<\s*\(\s*wget',
            r'\bcurl\s+.*\|\s*sh',
            r'\bwget\s+.*\|\s*sh',
        ]
        for pattern in remote_exec_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return "Command blocked by safety rules. Remote script execution is not allowed."
        
        # 检查网络工具（默认禁用）
        if not self._allow_network:
            if "curl" in words or "wget" in words:
                return "Command blocked by safety rules. Network tools (curl/wget) are disabled. Set BASH_ALLOW_NETWORK=true to enable."
        
        # 检查读/搜/列类命令
        for word in words:
            if word in self.READ_SEARCH_COMMANDS:
                tool_suggestion = {
                    "ls": "LS",
                    "cat": "Read",
                    "head": "Read",
                    "tail": "Read",
                    "grep": "Grep",
                    "find": "Glob",
                    "rg": "Grep",
                }.get(word, "the appropriate tool")
                return f"Command blocked by safety rules. Use {tool_suggestion} instead of '{word}'."
        
        return None

    def _check_cd_paths(self, command: str, base_dir: Path) -> Optional[str]:
        """
        检查命令中的 cd 路径是否在项目根目录内
        
        Args:
            command: 要检查的命令
            base_dir: 当前工作目录
            
        Returns:
            如果 cd 路径越界，返回错误消息；否则返回 None
        """
        # 匹配 cd 命令及其目标路径
        cd_patterns = [
            r'\bcd\s+([^\s;&|]+)',  # cd path
            r'\bcd\s+"([^"]+)"',     # cd "path with spaces"
            r"\bcd\s+'([^']+)'",     # cd 'path with spaces'
        ]
        
        for pattern in cd_patterns:
            for match in re.finditer(pattern, command):
                cd_target = match.group(1)
                
                # 解析 cd 目标路径
                try:
                    if cd_target.startswith("/"):
                        # 绝对路径
                        resolved = Path(cd_target).resolve()
                    else:
                        # 相对路径（相对于当前工作目录）
                        resolved = (base_dir / cd_target).resolve()
                    
                    # 检查是否在项目根目录内
                    resolved.relative_to(self._root)
                except ValueError:
                    return f"Access denied. 'cd {cd_target}' would go outside project root."
                except OSError:
                    # 路径解析失败，继续检查其他 cd
                    pass

        return None

    # =====================================================================
    # 安全沙箱核心方法
    # =====================================================================

    def _set_resource_limits(self, cpu_limit_sec: int, memory_limit_mb: int) -> None:
        """
        在子进程 fork 后、exec 前设置资源限制

        仅在 Unix 平台有效（通过 preexec_fn 调用）

        Args:
            cpu_limit_sec: 最大 CPU 时间（秒）
            memory_limit_mb: 最大内存限制（MB）
        """
        if not HAS_RESOURCE:
            return

        try:
            # CPU 时间限制（软/硬限制都设为相同值）
            if cpu_limit_sec > 0:
                resource.setrlimit(
                    resource.RLIMIT_CPU,
                    (cpu_limit_sec, cpu_limit_sec)
                )

            # 内存限制（地址空间）- 转换为字节
            if memory_limit_mb > 0:
                memory_bytes = memory_limit_mb * 1024 * 1024
                resource.setrlimit(
                    resource.RLIMIT_AS,
                    (memory_bytes, memory_bytes)
                )

            # 限制进程数，防止 fork bomb
            max_procs = self._sandbox_config.max_processes
            if max_procs > 0:
                resource.setrlimit(
                    resource.RLIMIT_NPROC,
                    (max_procs, max_procs)
                )

            logger.debug(
                f"Resource limits set: CPU={cpu_limit_sec}s, "
                f"Memory={memory_limit_mb}MB, Procs={max_procs}"
            )

        except (ValueError, OSError) as e:
            # 记录但继续执行（资源限制失败不应阻止命令执行）
            logger.warning(f"Failed to set resource limits: {e}")

    def _drop_privileges(self) -> None:
        """
        尝试降低执行权限到指定用户

        仅在 Unix 平台且当前有 root 权限时有效（通过 preexec_fn 调用）
        """
        if platform.system() == "Windows":
            return

        if not self._sandbox_config.drop_privileges:
            return

        try:
            target_uid = self._sandbox_config.target_uid
            target_gid = self._sandbox_config.target_gid

            if target_uid is None:
                # 默认尝试 nobody 用户 (UID 65534 或 99)
                import pwd
                try:
                    nobody = pwd.getpwnam("nobody")
                    target_uid = nobody.pw_uid
                    target_gid = nobody.pw_gid
                except KeyError:
                    target_uid = 65534
                    target_gid = 65534

            # 先设置 GID，再设置 UID（顺序很重要）
            if target_gid is not None:
                os.setgid(target_gid)
                logger.debug(f"Dropped GID to {target_gid}")

            if target_uid is not None:
                os.setuid(target_uid)
                logger.debug(f"Dropped UID to {target_uid}")

        except (ValueError, PermissionError, OSError) as e:
            # 权限不足是预期情况，记录但不失败
            logger.debug(f"Could not drop privileges: {e}")

    def _preexec_setup(self, cpu_limit: int, memory_limit: int) -> None:
        """
        子进程启动前的设置函数

        整合资源限制和权限降权（Unix only）

        Args:
            cpu_limit: CPU 时间限制（秒）
            memory_limit: 内存限制（MB）
        """
        self._set_resource_limits(cpu_limit, memory_limit)
        self._drop_privileges()

    @contextmanager
    def _temp_sandbox(self, target_dir: Path):
        """
        创建临时沙箱目录，将 target_dir 镜像到临时位置

        Args:
            target_dir: 目标工作目录

        Yields:
            Path: 临时沙箱中的对应目录路径
        """
        if not self._sandbox_config.use_temp_sandbox:
            yield target_dir
            return

        temp_base = tempfile.mkdtemp(prefix="bash_sandbox_")
        temp_target = Path(temp_base) / "workspace"

        try:
            # 创建目录结构
            temp_target.mkdir(parents=True, exist_ok=True)

            # 根据模式复制/链接文件
            mode = self._sandbox_config.temp_sandbox_mode
            if mode == "copy":
                self._copy_to_sandbox(target_dir, temp_target)
            elif mode == "symlink":
                self._symlink_to_sandbox(target_dir, temp_target)
            else:
                # 默认复制
                self._copy_to_sandbox(target_dir, temp_target)

            logger.debug(f"Temp sandbox created at {temp_target} (mode={mode})")
            yield temp_target

        finally:
            # 清理临时目录
            try:
                shutil.rmtree(temp_base, ignore_errors=True)
                logger.debug(f"Temp sandbox cleaned up: {temp_base}")
            except Exception as e:
                logger.warning(f"Failed to cleanup temp sandbox: {e}")

    def _copy_to_sandbox(self, source: Path, dest: Path) -> None:
        """
        复制文件到沙箱（选择性复制，避免复制大文件）

        Args:
            source: 源目录
            dest: 目标目录
        """
        max_file_size = self._sandbox_config.max_file_size_mb * 1024 * 1024

        try:
            for item in source.rglob("*"):
                if item.is_file():
                    # 跳过过大文件
                    try:
                        file_size = item.stat().st_size
                        if file_size > max_file_size:
                            logger.debug(f"Skipping large file: {item} ({file_size} bytes)")
                            continue
                    except (OSError, IOError):
                        continue

                    # 计算相对路径
                    try:
                        rel_path = item.relative_to(source)
                        dest_file = dest / rel_path
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, dest_file)
                    except (OSError, IOError) as e:
                        logger.debug(f"Failed to copy {item}: {e}")
                        continue

            # 复制空目录
            for item in source.rglob("*"):
                if item.is_dir():
                    try:
                        rel_path = item.relative_to(source)
                        (dest / rel_path).mkdir(parents=True, exist_ok=True)
                    except (OSError, IOError):
                        continue

        except Exception as e:
            logger.warning(f"Error during sandbox copy: {e}")

    def _symlink_to_sandbox(self, source: Path, dest: Path) -> None:
        """
        使用符号链接将文件链接到沙箱

        Args:
            source: 源目录
            dest: 目标目录
        """
        try:
            for item in source.rglob("*"):
                if item.is_file():
                    try:
                        rel_path = item.relative_to(source)
                        dest_file = dest / rel_path
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        os.symlink(item, dest_file)
                    except (OSError, IOError) as e:
                        logger.debug(f"Failed to symlink {item}: {e}")
                        continue

            # 创建目录
            for item in source.rglob("*"):
                if item.is_dir():
                    try:
                        rel_path = item.relative_to(source)
                        (dest / rel_path).mkdir(parents=True, exist_ok=True)
                    except (OSError, IOError):
                        continue

        except Exception as e:
            logger.warning(f"Error during sandbox symlink: {e}")

    def _execute_command(
        self,
        command: str,
        target_dir: Path,
        timeout_sec: float,
        env: Dict[str, str],
    ) -> ExecutionResult:
        """
        在沙箱环境中执行命令

        整合资源限制、权限降权等功能

        Args:
            command: 要执行的命令
            target_dir: 工作目录
            timeout_sec: 超时时间（秒）
            env: 环境变量字典

        Returns:
            ExecutionResult: 执行结果
        """
        stdout = ""
        stderr = ""
        exit_code = None
        signal_name = None
        timed_out = False
        resource_limited = False
        resource_limit_message = None

        # 准备 preexec_fn（仅在 Unix 非 Windows）
        preexec_fn = None
        if HAS_RESOURCE and platform.system() != "Windows":
            cpu_limit = min(
                int(timeout_sec),
                self._sandbox_config.max_cpu_time_sec
            )
            memory_limit = self._sandbox_config.max_memory_mb

            # 使用闭包传递参数
            def preexec_setup():
                self._preexec_setup(cpu_limit, memory_limit)

            preexec_fn = preexec_setup

        try:
            logger.debug(f"Executing command: {command[:100]}... in {target_dir}")

            result = subprocess.run(
                command,
                shell=True,
                cwd=str(target_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                preexec_fn=preexec_fn,
            )
            stdout = result.stdout
            stderr = result.stderr
            exit_code = result.returncode

        except subprocess.TimeoutExpired as e:
            timed_out = True
            stdout = e.stdout or ""
            stderr = e.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            logger.debug(f"Command timed out after {timeout_sec}s")

        except subprocess.CalledProcessError as e:
            stdout = e.stdout or ""
            stderr = e.stderr or ""
            exit_code = e.returncode

            # 检查是否是资源限制导致的失败
            if exit_code == -signal.SIGXCPU:
                resource_limited = True
                signal_name = "SIGXCPU"
                resource_limit_message = "Command exceeded CPU time limit"
                logger.warning(f"Command hit CPU limit: {command[:100]}")
            elif exit_code == -signal.SIGSEGV or exit_code == -signal.SIGKILL:
                # 可能是内存限制导致的
                resource_limited = True
                signal_name = "SIGSEGV" if exit_code == -signal.SIGSEGV else "SIGKILL"
                resource_limit_message = "Command may have exceeded memory limit"
                logger.warning(f"Command hit memory limit (possible): {command[:100]}")

        except PermissionError:
            logger.error(f"Permission denied executing command: {command[:100]}")
            raise

        except Exception as e:
            logger.error(f"Command execution failed: {e}")
            raise

        return ExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            signal_name=signal_name,
            timed_out=timed_out,
            resource_limited=resource_limited,
            resource_limit_message=resource_limit_message,
        )

    def get_parameters(self) -> List[ToolParameter]:
        """
        获取工具参数定义
        
        Returns:
            工具参数列表
        """
        return [
            ToolParameter(
                name="command",
                type="string",
                description="The shell command to execute. Command chaining (&&, ||, ;) is allowed.",
                required=True,
            ),
            ToolParameter(
                name="directory",
                type="string",
                description="Working directory relative to project root. Defaults to '.'.",
                required=False,
                default=".",
            ),
            ToolParameter(
                name="timeout_ms",
                type="integer",
                description=f"Execution timeout in milliseconds. Default {self.DEFAULT_TIMEOUT_MS}, max {self.MAX_TIMEOUT_MS}.",
                required=False,
                default=self.DEFAULT_TIMEOUT_MS,
            ),
        ]
