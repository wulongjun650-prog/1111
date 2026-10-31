from __future__ import annotations

import locale
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


DAILY_TASK_NAME = "GGAdsProtocolManager Daily Start"
LOGON_TASK_NAME = "GGAdsProtocolManager Logon Start"
DAILY_APPEAL_TASK_NAME = "GGAdsProtocolManager Daily Appeal Draft"
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class TaskStatus:
    exists: bool
    command: str = ""
    start_time: str = ""
    detail: str = ""


def validate_time(value: str) -> str:
    value = value.strip()
    if not TIME_PATTERN.fullmatch(value):
        raise ValueError("启动时间必须使用 HH:MM 格式，例如 09:30 或 21:05")
    return value


def application_command() -> str:
    """Return the command that Task Scheduler should launch."""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    launcher = Path(__file__).resolve().parents[1] / "launcher.py"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    executable = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{executable.resolve()}" "{launcher}"'


def _run_schtasks(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        ["schtasks.exe", *arguments],
        capture_output=True,
        text=True,
        encoding=locale.getpreferredencoding(False),
        errors="replace",
        creationflags=flags,
        check=False,
    )


def _failure(result: subprocess.CompletedProcess[str], action: str) -> RuntimeError:
    detail = (result.stderr or result.stdout or "未知错误").strip()
    return RuntimeError(f"Windows 任务计划{action}失败：{detail}")


def create_daily_task(start_time: str) -> None:
    start_time = validate_time(start_time)
    result = _run_schtasks([
        "/Create", "/TN", DAILY_TASK_NAME, "/TR", application_command(),
        "/SC", "DAILY", "/ST", start_time, "/RL", "LIMITED", "/F",
    ])
    if result.returncode:
        raise _failure(result, "创建每日启动任务")


def create_logon_task() -> None:
    result = _run_schtasks([
        "/Create", "/TN", LOGON_TASK_NAME, "/TR", application_command(),
        "/SC", "ONLOGON", "/RL", "LIMITED", "/F",
    ])
    if result.returncode:
        raise _failure(result, "创建登录启动任务")


def create_daily_appeal_task(start_time: str) -> None:
    start_time = validate_time(start_time)
    command = application_command() + " --daily-appeal"
    result = _run_schtasks([
        "/Create", "/TN", DAILY_APPEAL_TASK_NAME, "/TR", command,
        "/SC", "DAILY", "/ST", start_time, "/RL", "LIMITED", "/F",
    ])
    if result.returncode:
        raise _failure(result, "创建每日账户申诉任务")


def delete_task(name: str) -> bool:
    result = _run_schtasks(["/Delete", "/TN", name, "/F"])
    if result.returncode:
        # A missing task is already in the desired disabled state.
        if not query_task(name).exists:
            return False
        raise _failure(result, "删除任务")
    return True


def query_task(name: str) -> TaskStatus:
    result = _run_schtasks(["/Query", "/TN", name, "/XML"])
    if result.returncode:
        raw_detail = (result.stderr or result.stdout or "").strip()
        normalized = raw_detail.casefold()
        missing_markers = (
            "cannot find the file",
            "cannot find the path",
            "not found",
            "找不到指定的文件",
            "找不到指定的路径",
            "系统找不到",
        )
        detail = (
            "尚未创建"
            if not raw_detail or any(marker in normalized for marker in missing_markers)
            else f"状态读取失败：{raw_detail}"
        )
        return TaskStatus(False, detail=detail)
    command = ""
    start_time = ""
    detail = "已启用"
    try:
        root = ET.fromstring((result.stdout or "").lstrip("\ufeff\n\r "))
        command_node = root.find(".//{*}Actions/{*}Exec/{*}Command")
        arguments_node = root.find(".//{*}Actions/{*}Exec/{*}Arguments")
        boundary_node = root.find(".//{*}Triggers/*/{*}StartBoundary")
        enabled_node = root.find(".//{*}Settings/{*}Enabled")
        command = (command_node.text or "").strip() if command_node is not None else ""
        arguments = (arguments_node.text or "").strip() if arguments_node is not None else ""
        if arguments:
            command = f"{command} {arguments}".strip()
        boundary = (boundary_node.text or "").strip() if boundary_node is not None else ""
        match = re.search(r"T(\d{2}:\d{2})", boundary)
        start_time = match.group(1) if match else ""
        if enabled_node is not None and (enabled_node.text or "").strip().casefold() == "false":
            detail = "任务存在，但已被 Windows 禁用"
    except ET.ParseError:
        detail = "任务存在；Windows 返回内容无法解析"
    return TaskStatus(True, command=command, start_time=start_time, detail=detail)


def schedule_status() -> dict[str, TaskStatus]:
    return {
        "daily": query_task(DAILY_TASK_NAME),
        "logon": query_task(LOGON_TASK_NAME),
        "appeal": query_task(DAILY_APPEAL_TASK_NAME),
    }
