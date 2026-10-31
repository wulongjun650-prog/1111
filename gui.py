from __future__ import annotations

import copy
import csv
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageOps, ImageTk, UnidentifiedImageError

from . import __version__
from .bitbrowser import (
    health as bitbrowser_health,
    list_profiles as list_bitbrowser_profiles,
    load_api_base as load_bitbrowser_api_base,
    load_api_token as load_bitbrowser_api_token,
    save_local_api as save_bitbrowser_local_api,
)
from .browser_provider import (
    ADSPOWER,
    BITBROWSER,
    normalize_browser_type,
    query_profile_info as query_browser_profile_info,
    split_window_key,
    window_key,
)
from .ads import AdCatalog, AdManager, display_ad_hierarchy
from .adspower import query_profile_info
from .appeals import (
    APPEAL_BASIS_CORRECTED,
    APPEAL_BASIS_ERROR,
    APPEAL_BASIS_LABELS,
    APPEAL_LOGIN_REQUIRED,
    APPEAL_MODE_AUTO,
    APPEAL_MODE_MANUAL,
    APPEAL_OFFICIAL_PENDING,
    APPEAL_SUBMIT_FAILED,
    APPEAL_SUBMITTED,
    APPEAL_USER_ACTION_REQUIRED,
    APPEAL_VERIFICATION_REQUIRED,
    DEFAULT_APPEAL_TEMPLATE,
    MAX_APPEALS_PER_BATCH,
    OFFICIAL_REASON_SOURCE,
    REASON_READ,
    appeal_submission_is_protected,
    appeal_automation_due,
    effective_appeal_history_by_key,
    build_appeal_text,
    latest_suspension_reasons,
    load_appeal_automation,
    load_appeal_history,
    load_appeal_template,
    read_suspension_reasons,
    run_daily_appeal_workflow,
    run_one_click_appeals,
    save_appeal_automation,
    save_appeal_template,
    submit_appeals,
    update_appeal_status,
    validate_appeal_template,
    validate_appeal_time,
)
from .app_categories import ALL_APP_CATEGORY_IDS, APP_CATEGORIES
from .campaign_control import (
    CampaignAction,
    CampaignDecision,
    CampaignFacts,
    evaluate_daily_switch,
    execute_write_plans,
    write_plans_for_decisions,
)
from .ga4 import BUSINESS_OBJECTIVES, BUSINESS_SIZES, INDUSTRIES, GA4Runner
from .config import (
    DEFAULT_CONFIG,
    DEFAULT_OBJECT_NAMES,
    Assignment,
    account_cache_metadata,
    account_cache_window_metadata,
    account_cache_wanted,
    config_path,
    data_dir,
    format_cid,
    global_account_scope,
    load_account_cache,
    load_config,
    load_ui_state,
    load_warmup_template,
    normalize_cid,
    normalize_window_metadata,
    parse_assignments,
    resolve_object_names,
    save_config,
    save_account_cache,
    save_run_report,
    save_ui_state,
    save_warmup_template,
    update_account_cache_window_metadata,
    validate_config,
)
from .creative_library import (
    DEFAULT_CREATIVE_LIBRARY,
    creative_library_path,
    delete_plan,
    export_creative_template,
    import_image_files,
    import_creative_template,
    load_creative_library,
    put_plan,
    save_creative_library,
)
from .creative_thumbnails import CreativeThumbnail, THUMBNAIL_SIZE, load_creative_thumbnail
from .entity_copy import (
    COPY_AD,
    COPY_AD_GROUP,
    COPY_CAMPAIGN,
    EntityCopyManager,
)
from .image_upload import (
    ROLE_LABELS,
    classify_image_roles,
    validate_role_image,
)
from .protocol import (
    AccountPage,
    CAMPAIGN_STATUS_ENABLED,
    CAMPAIGN_STATUS_PAUSED,
    ProtocolRunner,
    RemoteManager,
    parse_money,
)
from .reporting import ReportPeriod, parse_report_period, parse_report_range
from .placements import PlacementManager
from . import scheduler
from .currency_rates import convert_micros, fetch_rate
from .portable_copy import SUPPORTED_CHANNELS
from .portable_copy_workflow import validate_snapshot, validate_snapshot_files
from .snapshot_copy import (
    SnapshotExporter,
    SnapshotImporter,
    batch_recreate,
    compose_campaign_name,
)
from .targeting_copy import summarize_targeting
from .warmup import (
    DEFAULT_DAILY_BUDGET,
    SOURCE_CAMPAIGN_NAME,
    WarmupManager,
    build_warmup_template_dry_run,
    validate_daily_budget,
)
from .website_analysis import WEBSITE_CATEGORIES, analyze_websites, import_website_file


TYPE_LABELS = {
    "app": "排除列表·应用类别",
    "web": "排除列表·网站",
    "xq": "自定义受众·兴趣/购买意向",
    "ss": "自定义受众·Google 搜索兴趣",
}


def import_window_record(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize one read-only BitBrowser list row for local UI state."""
    profile_id = str(value.get("profile_id") or value.get("id") or "").strip()
    if not profile_id:
        raise ValueError("比特浏览器窗口 ID 不能为空")
    serial_number = str(value.get("serial_number") or value.get("seq") or "").strip()
    name = str(value.get("name") or "").strip()
    running = bool(value.get("running"))
    return {
        "browser_type": BITBROWSER,
        "profile_id": profile_id,
        "serial_number": serial_number,
        "window_number": serial_number,
        "name": name,
        "window_name": name,
        "remark": str(value.get("remark") or "").strip(),
        "running": running,
        "running_status": "运行中" if running else "已停止",
    }


def filter_importable_windows(
    profiles: list[dict[str, Any]],
    existing_keys: set[str],
    search: str = "",
) -> list[dict[str, Any]]:
    """Return normalized BitBrowser rows with composite duplicate state."""
    query = str(search or "").strip().casefold()
    rows: list[dict[str, Any]] = []
    for value in profiles:
        row = import_window_record(value)
        if query and query not in " ".join(
            (row["name"], row["serial_number"], row["remark"])
        ).casefold():
            continue
        row["already_imported"] = window_key(
            row["browser_type"], row["profile_id"]
        ) in existing_keys
        rows.append(row)
    return rows


def execution_object_label(key: str, label: str, selected: bool) -> str:
    """Use an unambiguous tick instead of the theme's cross indicator."""
    marker = "✓" if selected else "□"
    return f"{marker} {key} · {label}"


COPY_KIND_BY_LABEL = {"广告系列": COPY_CAMPAIGN, "广告组": COPY_AD_GROUP, "广告": COPY_AD}
ACTION_STATUS_LABELS = {
    "empty": "内容为空，已跳过",
    "unchanged": "已完整存在，无需修改",
    "would_create": "预检：将创建",
    "would_add": "预检：将补充",
    "added": "已补充",
    "exists": "同名对象已存在，已跳过",
    "created": "已创建",
    "skipped": "已跳过",
}
UNASSIGNED_PLAN = "（未分配，不执行）"
CONTENT_EXAMPLES = {
    "web": "例如：example.com、sub.example.org；引用本地内容库：@屏蔽网站",
    "xq": "自定义受众：具有这些兴趣或购买意向的用户；例如 home improvement；引用：@家装兴趣",
    "ss": "自定义受众：曾在 Google 搜索这些字词的用户；例如 kitchen remodel；引用：@家装搜索",
}

# One shared visual language for the whole desktop app.  Blue means navigation/read,
# purple means a write action, and green/amber/red are reserved for result states.
COLOR_BG = "#EDF1F7"
COLOR_SURFACE = "#FFFFFF"
COLOR_SURFACE_ALT = "#F6F8FB"
COLOR_BORDER = "#DCE3EE"
COLOR_HEADING = "#EFF3FA"
COLOR_TEXT = "#1A2333"
COLOR_MUTED = "#66748B"
COLOR_PRIMARY = "#2657EB"
COLOR_PRIMARY_HOVER = "#1B43C6"
COLOR_PRIMARY_SOFT = "#E8EFFE"
COLOR_WRITE = "#7C3AED"
COLOR_WRITE_HOVER = "#6529D8"
COLOR_WRITE_SOFT = "#F2ECFE"
COLOR_SUCCESS = "#14804A"
COLOR_SUCCESS_SOFT = "#E6F6EC"
COLOR_WARNING = "#B45309"
COLOR_WARNING_SOFT = "#FDF1DC"
COLOR_DANGER = "#C0362C"
COLOR_DANGER_HOVER = "#9E2A22"
COLOR_DANGER_SOFT = "#FCEAE8"
COLOR_MANAGER = "#315B7D"
COLOR_MANAGER_SOFT = "#E6F0F7"
COLOR_DISABLED = "#9AA7B8"
# Dark console look for the live activity stream.
COLOR_CONSOLE_BG = "#0E1626"
COLOR_CONSOLE_PANEL = "#16233A"
COLOR_CONSOLE_TEXT = "#C9D4E6"
COLOR_CONSOLE_MUTED = "#6B7C96"


def local_timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def bundled_asset(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "assets" / name

HELP_TEXT = """GGAds 协议管理器使用帮助

一、四个核心概念

1. 指纹浏览器窗口
   就是比特浏览器或 AdsPower 中相互隔离的浏览器环境；每个窗口下面可以添加多个 Google Ads CID。
   本软件默认使用比特浏览器：在“批量任务”点击“导入比特浏览器窗口”，可批量读取窗口编号、名称和运行状态。
   比特浏览器 7.1.5 的本地接口在「系统设置 → Local API」里查看。当前这版优先连接 http://127.0.0.1:54346，连不上再试 54345。
   「开启鉴权控制」是关掉的时候，不用填 API Token。只有打开那个开关后，才把 Token 填到“比特 Local API 设置”，请求会带 X-API-KEY。
   导入只保存窗口资料，不会启动窗口；后续账户刷新、预检或业务任务才会按需打开选中的已停止窗口。
   AdsPower 继续兼容：在“兼容操作”中选择“添加 AdsPower 窗口”或“读取 AdsPower 窗口编号”。
   AdsPower 请在左侧“API & MCP”页面启用 Local API（默认端口 50325）；API 暂时不可用时，已有编号会保留。

2. CID
   Google Ads 界面显示的 10 位客户 ID，例如 532-793-9237。
   软件界面、提示和运行记录统一使用 532-793-9237 格式；协议内部自动转换为 5327939237。
   软件内部会通过协议自动换算成 Google Ads 内部账户 ID，不需要你填写内部 ID。

3. 本地内容库
   位于“本地配置 → 本地内容库”，保存在本机的独立 content-library.json 文件中。
   编辑本地内容库不会自动上传或修改 Google Ads。
   每个方案包含 app、web、xq、ss 四类内容，并可被多个 CID 引用。
   每个方案还可分别设置四个“Google Ads 对象名称”：app/web 对应排除列表名称，xq/ss 对应自定义受众名称。

4. Google Ads 数据（原“云端管理”）
   指选中 CID 在 Google Ads 服务器中已经真实存在的数据：
   · 共享库中的展示位置排除列表
   · 受众群体管理器中的自定义受众
   在这个页面点击“新建、编辑/替换、删除”会真实修改 Google Ads，操作后软件会重新读取校验。

5. 协议同步与安全级别
   当前内置协议目录同步自 ggadscode V1 Display 清单（33 项，清单指纹 770f48b45ad1…）。
   状态枚举统一为：广告系列 3=启用、5=暂停、7=移除；广告组/广告 1=启用、2=暂停、3=移除。
   只有 live_read_verified / live_write_verified 项可进入对应执行链；blocked_write 和未知协议一律拒绝。
   recommendation.apply 当前仍是 blocked_write，不会因为存在推荐卡片就猜测或发送应用协议。
   程序只内置脱敏后的服务、路径、枚举和作用域；抓包中的账户 ID、Cookie、XSRF 和请求正文不会打包进 EXE。
   状态写入要求原生整数枚举和规范数字 ID，并绑定当前选中的窗口/CID；来源项目的单账户硬锁不会限制本软件的多窗口结构。

二、推荐使用流程

1. 启动并登录比特浏览器 7.1.5。打开「系统设置 → Local API」，确认本地接口是 http://127.0.0.1:54346。鉴权控制关着就不要填 Token。
2. 在“批量任务”页点击“导入比特浏览器窗口”；导入时窗口可以是运行中或已停止。
   使用 AdsPower 时，请从“兼容操作”选择“添加 AdsPower 窗口”。
3. 选中窗口，点击“添加 CID”，填写 CID 和引用的内容方案。
4. 点击“刷新账户列表与状态”；软件会按需打开所选窗口，再核对账户名称、CID 和状态。
5. 在“本地配置 → 本地内容库”建立或编辑方案。
6. 回到“批量任务”，选中目标普通 CID，先点击“预检选中 CID（不修改 Google Ads）”。
7. 检查右侧“实时活动流”，确认即将新增的项目数量。
8. 再点击“执行选中 CID”。
   所有后台任务点击后都会立即在顶部状态条显示任务名称、当前步骤、动态进度和已用时间；
   完成显示绿色，失败显示红色和失败原因。任务运行中相关按钮会暂时禁用，防止重复提交。

三、各页面说明

【批量任务】
   按“指纹浏览器窗口 → 多个 CID”的树形结构组织任务。
   窗口根节点显示浏览器类型、窗口编号/名称和窗口 ID，例如“比特 #12 · 窗口名 · 窗口 ID”或“AdsPower #12 · 窗口 ID”。
   账户树可用 Ctrl/Shift 多选普通 CID。“预检选中 CID（不修改 Google Ads）”只读取当前蓝色高亮的 CID，并计算这些 CID 准备新增的内容。
   “执行选中 CID”同样只处理这些 CID；其他已分配内容方案的账户不会随批写入。
   自动发现的 CID 默认显示“未分配，不执行”；编辑 CID 并选择内容方案后才会参与预检和写入。
   字段包括账户类型、账户名称、CID、内部账户 ID、父 MCC、层级、内容方案、状态、状态说明、备注、新建时间和更新时间。
   扫描会递归展开子 MCC：窗口 → MCC/CID → 子 MCC/CID，直到没有更深层 MCC；CID 和 MCC 不会混淆。
   MCC 行只用于继续读取下级账户，不会作为内容写入任务执行。
   列表默认使用简洁视图，并按 MCC 在前、CID 在后排列；点击 MCC 左侧箭头可展开/收起子账户。
   勾选“详细字段”可查看内部账户 ID、父 MCC、层级、原始状态说明和新建时间等信息。
   备注保存在本机；新增和编辑 CID 时都可以填写多行备注。
   新建时间记录任务建立时间；更新时间在编辑任务或刷新账户协议信息后自动更新。
   “刷新账户列表与状态”只获取账户名称和状态，不检查内容方案，也不会修改 Google Ads。
   预检已经包含账户发现，因此预检前不必先刷新账户列表。

【定时启动】
   “每日定时启动”会在 Windows 任务计划程序中创建或修改每天一次的启动任务。
   时间使用 24 小时制 HH:MM，例如 09:30、21:05。
   “登录后自动启动”会在当前用户登录 Windows 后启动本软件。
   普通定时启动只启动软件，不会自动执行 Google Ads 写入、复制、排除或养号任务。
   “账户申诉”页有独立的每日任务；默认全自动读取封禁原因并提交，也可关闭“到时自动提交”改为只生成草稿。
   软件移动到其他目录后，请重新保存定时设置，让 Windows 使用新的 EXE 路径。
   “读取 CID 余额”通过临时 Billing 页面只读获取账户级余额，速度比普通状态刷新慢，因此独立执行。
   余额优先读取“剩余账号预算”；结算概览只返回预算和支出时，显示二者差额并明确标为“计算值”。
   这不是广告系列每日预算。MCC 会跳过；不支持账户余额的结算类型显示“—”并在详细字段说明原因。
   消耗查询已放到独立的“每日消耗”页；查询结果会同步回批量任务的“日期消耗”列。
   可用：账户状态和 MCC 关联正常。
   不可用：账户已停用、取消、关闭，或 MCC 关联已停用。
   封禁：Google Ads 返回账户已暂停状态。
   风控封禁优先级最高：即使基础状态仍为正常，只要协议返回 is_fraudster=true，也显示红色“封禁”。
   正式执行只写入明确“可用”的账户；封禁、不可用或状态未知都会跳过，并把原因写入报告。
   右键窗口、MCC、CID 会按对象类型显示对应操作；右键会先选中鼠标所在行，避免误操作旧选中项。
   双击窗口可编辑窗口；双击普通 CID 可编辑任务；双击 MCC 可展开或收起。
   状态未知/读取失败：登录权限不足、CID 不属于该窗口，或协议读取失败；不会误标为封禁。
   颜色：可用为绿色，不可用为橙色，封禁为红色，未知/读取失败为灰色。
   app/web 已存在时只补充缺项；xq/ss 已存在同名对象时默认不覆盖。

【每日消耗】
   在“开始日期 / 结束日期”输入 YYYY-MM-DD 或“全部”，点击“查询全部 CID”，汇总每个 CID 全部广告系列在该范围内的 stats.cost。
   默认查询今天，“今天”“昨天”可快速切换；两端都填“全部”查询 2000-01-01 至今天的历史汇总，开始日期不能晚于结束日期，结束日期不允许是未来。
   上方独立列表显示窗口编号、窗口 ID、CID、账户名称、账户状态、日期、合计消耗、币种、系列数、读取状态和失败原因。
   选中一行后，下方显示该 CID 每个广告系列的名称、系列 ID、状态和六位小数消耗。
   列表支持多选，点击“查询选中 CID”只刷新选择项；查询结果同时更新批量任务页的“日期消耗”字段。
   读取失败显示“—”，不会当作 0；整个页面只发送读取协议，不修改 Google Ads。

【展示位置】
   选择指纹浏览器窗口、普通 CID 和 YYYY-MM-DD 日期，只读查询当天实际展示广告的位置及表现数据；“全部”查询 2000-01-01 至今天。
   全部日期的展示位置会通过协议持续分页，直到最后一页；若服务端忽略分页 offset 会明确失败，不会把重复第一页当成完整数据。
   结果按“广告系列 → 广告组 → 展示位置”展开，显示展示次数、互动、互动率、平均费用、费用和转化。
   “导入网站名单并选择”支持 TXT/CSV/JSON，按域名匹配当前报表；“导出展示位置”可导出选中项，未选择时导出全部网站。
   “检测可访问 + 智能分类”使用本机网络检查 HTTP/HTTPS，记录状态、最终网址和标题；不经过所选指纹浏览器的代理。
   “兴趣/内容分类”结合域名、网页标题和公开摘要进行本地评分，可区分游戏、博彩、成人、视频、新闻、社交、购物等；它不是 Google 官方分类，写入前请核对。
   “查看/筛选分类”下拉框只决定当前统计、高亮和批量选择哪一类，不会把所有网站强制识别成所选分类；分类时仍会同时判断全部类别。
   检测完成弹窗分别显示当前筛选分类的唯一域名数、展示位置数和全部类别分布；切换下拉框会立即更新高亮与摘要。
   例如在分类框选择“游戏”，点击“选中此分类网站”，即可批量选择游戏类网站，再走预检和系列排除。
   支持 Ctrl/Shift 多选网站，也可一键选择当前广告系列下的全部网站；应用和其他类型暂不发送系列排除。
   “系列排除预检”会重新读取当前选择的日期范围、广告系列和已有排除，不会修改 Google Ads。
   “从广告系列中排除”通过 CampaignCriterionService 创建 negative placement，不会写入共享库或广告组。
   多个系列会按展示位置真实所属系列分别处理；已有排除自动跳过，新增项必须逐系列写后回读才算成功。
   窗口、CID 或日期改变后必须重新查询，避免把旧报表中的展示位置排除到错误账户。

【本地配置 → 本地内容库】
   管理这台电脑上的本地模板。可以新增、重命名、复制、删除方案。
   文件位置：%APPDATA%\\GGAdsProtocolTool\\local-config\\content-library.json。
   旧版 config.json 首次启动时会自动复制到新位置，旧文件保留作备份。
   “方案名称”只用于本软件内引用；“映射到 Google Ads 的对象名称”才是实际查找或创建到 Google Ads 中的名称。
   右键可新增、保存、重命名、复制、删除或导入导出；双击方案可重命名。
   例如方案“韩国”可以映射为 韩国-app、韩国-web、韩国-xq、韩国-ss。
   app：应用类别，可直接全选 Apple App Store 84 类和 Google Play 56 类，共 140 类。
   web：网站排除，每行一个域名，例如 example.com。
   xq：兴趣或购买意向，每行一个词，例如 home improvement。
   ss：Google 搜索字词，每行一个词组，例如 home renovation ideas。
   xq 和 ss 都写入“受众群体管理器 → 自定义受众”：xq 是兴趣/购买意向，ss 是曾在 Google 搜索这些字词的用户；ss 不是普通搜索关键词。
   @名字：引用另一个字典，例如 @屏蔽网站、@家装兴趣。
   注释：支持 /* ... */、// 注释、# 注释和行尾注释；发送协议前会自动删除。
   web 还会忽略“展示位置网址、排除网址、域名”等表头，以及“备注：”“说明：”、【标题】和分隔线。

【本地配置 → 广告素材库】
   图片方案与文案方案独立管理，支持新增、查看、编辑、复制、删除、右键菜单和双击编辑。
   配置保存在 ad-creative-library.json；导入的图片副本保存在 local-config\\creative-assets，并按内容去重。
   图片方案分别保存横图 1.91:1、方图 1:1 和竖图 9:16/4:5；保存与应用时都会重新检查文件、格式、大小和比例。
   新建或编辑图片方案时可一次批量添加图片并按真实宽高自动分类；表格显示真实尺寸与识别依据。
   文案方案保存最终网址、商家名称、短标题、长标题和描述，也可以只保存部分字段。
   “导入模板”接受 GGAds 导出的 JSON；图片路径相对于 JSON 文件解析，适合整套模板跨电脑移动。
   “导出选中/导出全部”会生成 JSON 和同目录的“文件名-assets”图片文件夹；导入遇到重名自动改为“(2)”，不会覆盖已有方案。
   选择图片方案、指纹浏览器窗口和普通 CID 后，可直接上传到 Google Ads 素材库；执行前要求账户可用且 Billing 余额大于 0。
   每张图片必须拿到 Asset ID，并通过 AssetFolderService 回读账户、类型、状态、尺寸和文件大小才报告成功；结果保存在方案上传历史与运行记录。
   点击“应用到广告创建”会自动跳到广告创建页；图片或文案仍需经过协议预检，选择方案本身不会上传或写入 Google Ads。

【Google Ads 数据】
   先选择指纹浏览器窗口，再选择该窗口下的 CID，点击“查询 Google Ads 数据”。
   查询：只读，不会修改账户。
   新建：创建新的排除列表或自定义受众。
   导入网站排除：读取 TXT/CSV/JSON 域名名单，去重后创建真实的网站排除列表；写入前会显示目标 CID、列表名和数量并要求确认。
   导出选中对象：网站/应用排除列表和自定义受众均可导出为 JSON、CSV 或 TXT。
   编辑/替换：以编辑器中的内容为准；新增缺项，同时删除不再包含的旧项。
   删除：删除选中的 Google Ads 对象，执行前会二次确认。
   右键可查询、新建、编辑、删除或复制对象信息；双击对象进入编辑/替换。

【广告创建】
   “层级复制”支持广告系列、广告组、广告三个范围；来源窗口/CID 与目标窗口/CID 分开选择。
   来源和目标相同可复制自己的对象；来源和目标不同可跨 CID 复制。
   两种真实创建都要求目标 CID 状态可用、Billing 余额可识别且大于 0；发送写入协议前会重新读取余额。
   零余额、余额不可识别或读取失败都会停止创建；只读结构查询和协议预检不受余额限制。
   复制广告系列时粘贴到目标 CID；复制广告组时选择目标广告系列；复制广告时选择目标广告组。
   同一 Google 权限会话优先使用原生复制协议；两个独立浏览器窗口的登录会话则自动使用“本地快照重建”。
   本地快照会保存广告系列、预算、广告组、广告、文案和图片；粘贴时切换到目标窗口，重新上传图片并按新 ID 逐层创建。
   所有新系列、广告组和广告均以暂停状态创建并回读；本地快照位于软件数据目录的 copy-snapshots 文件夹。
   “修改文案创建广告”支持从当前选择的可用 CID 读取响应式展示广告作为模板，不再绑定固定测试户。
   点击“读取广告结构（只读）”后，依次选择参考广告、目标展示广告系列和目标广告组。
   读取结果按“广告系列 → 广告组 → 多个广告”三级树展示；系列和广告组即使没有响应式展示广告也会保留。
   点击系列或广告组可定位对应层级；点击具体广告才会加载该广告的文案、网址和素材。
   可从本地广告素材库分别选择图片方案和文案方案；也可把当前文案或手工选择的图片直接存为新方案。
   文案方案只覆盖其中非空字段；图片方案按横图、方图、竖图三类替换，未提供的类别继续沿用参考广告。
   最终网址、商家名称、短标题、长标题和描述可以修改；横图、方图和竖图可沿用参考广告，也可分别选择本地图片上传。
   “批量选择图片（自动识别尺寸）”可一次选择多张图片：1.91:1 自动归横图，1:1 归方图，9:16/4:5 归竖图。
   选择参考广告后会通过素材协议读取横图、方图和竖图的真实预览地址，下载到本地缓存并显示缩略图；双击缩略图可打开原图。
   批量选择遇到坏文件或不支持比例时只跳过该文件，不中断其他图片；相同内容按哈希自动去重。
   本地图片支持 PNG/JPG/JPEG，单张不超过 5 MB；横图要求 1.91:1，方图要求 1:1，竖图要求 9:16 或 4:5。
   选择某一类本地图片会替换该类参考素材，未选择的类别继续沿用参考广告 Asset ID。
   先点“协议预检”检查字段长度、父级关系、图片格式/尺寸/比例；预检不会上传图片或创建广告。
   “创建暂停广告”会发送 AdGroupAdService/Mutate 协议，新广告始终强制为暂停。
   正式创建先通过 Google resumable 协议上传本地图片并取得 Asset ID，再创建广告；若后续创建失败，已上传素材仍保留在目标素材库。
   只有写后读到唯一的新广告 ID，且网址、文案、父级、图片 Asset ID 和暂停状态完全一致，软件才会报告成功。
   修改文案模式不会跨 CID 直接复用素材 ID；跨 CID 原样复制请使用“层级复制”。
   Display-3 当前页面显示“所有广告均已被拒登”。复制结构不会自动解决政策拒登，创建后仍需查看政策原因。

【一键白量】
   在“一键白量”页显式填写可用参考 CID，并使用其中真实的 App-1 应用广告系列；目标日预算可编辑，默认 USD 2.00，不是固定上限。
   先填写目标日预算，再在左侧按指纹浏览器窗口多选普通 CID，用“扫描选中 CID”只读检查账户状态、Billing 余额、今日消耗、币种、现有 App 系列和当前预算。
   只有账户状态可用、余额可识别且大于 0、币种与参考系列一致时才能勾选；扫描表会显示“当前 → 目标”预算。
   目标没有 App-1 时，通过 CampaignCopyService 原生复制完整系列；已有暂停的 App-1 时不重复复制。
   Google 原生跨账户复制要求参考 CID 与目标 CID 位于当前登录账号可访问的权限范围；无权限的窗口会跳过并在运行记录写明原因。
   复制或选中暂停系列后，先通过 BudgetService 改到目标日预算并回读金额，再启用并回读状态；任一校验失败都会停止该 CID。
   扫描后若修改目标预算，必须重新扫描；最终确认框显示每个 CID 的目标预算和理论合计日预算。
   已启用系列会跳过，避免意外修改正在投放的预算；不处理 MCC、封禁账户、零余额账户或余额不可识别账户。

【账户申诉】
   只列出“批量任务”账户树中已识别为封禁的普通 CID；MCC 不是投放账号，不进入申诉列表。
   “封禁”状态来自 CustomerService 的账户状态字段，只证明账号已被中止，不等于具体政策原因。
   先多选 CID，点击“读取选中封禁原因（只读）”：软件通过对应浏览器登录会话打开临时 Google Ads 官方账号中止页，读取横幅中明确写出的政策名称后立即关闭临时页。
   原因、官方来源、读取时间和原文片段保存到本地 suspension-reasons.json；读取不到时显示失败或“官方页未提供具体政策”，绝不会让 AI 或规则猜测原因。
   只有已经读到明确政策名称的 CID 才能一键提交；提交前仍须复核账号、原因和真实整改资料。
   申诉文案只允许使用公开 CID、Google Ads 账户名称和官方政策原因；指纹浏览器窗口、内部账户 ID、MCC 关联、状态码和风控判断只用于本机诊断，禁止进入申诉文案。
   旧模板若包含旧版通用原因句式会自动切换到新版模板；保存、复制和正式提交前都会再次检查内部字段。
   运行模式默认是“自动”，每天在设定时间执行一次；软件会创建独立 Windows 任务，因此主界面未打开时也能运行。
   全自动流程为：刷新全部窗口状态 → 筛选封禁普通 CID → 排除已有提交/处理中账号 → 读取官方政策原因 → 按所选依据提交 → 保存申诉编号和报告。
   GUI 与 Windows 任务使用原子运行锁；即使同时启动，也只有一个流程可以提交，避免并发重复申诉。
   关闭“到时自动提交”后，每日任务只生成待审核草稿；切换到“手动”会停用 Windows 每日任务，“立即执行一次”仍可手工触发当前配置的完整流程。
   一键提交按语义控件执行“查看详情 → 开始行动/联系我们 → 选择申诉依据 → 填写文案 → 提交”，不用鼠标坐标。
   遇到问卷式表单（投放国家/地区、业务活动描述、网站、所有者等是/否问题）会按账号资料与申诉文案逐题填写并自动翻页到确认提交；网站等未知信息留空不编造。
   文案含“请填写/TODO”、内容过短或内部诊断字段时会阻止提交；点击确认框即确认资料真实且已完成所述整改。
   只有 Google 页面返回“已提交/申诉处理中”才记为完成；重新登录、身份验证或安全验证会保留官方页面并显示等待状态，完成后可再次续提。
   模板、原因缓存、官方回执片段和申诉历史只保存在“本地配置”目录；已提交或官方处理中会持续保护，不再被次日草稿覆盖，也不会重复申诉；标记“申诉被拒”后才允许重提。
   单次最多提交 10 个账号；Google 要求一次只处理一份申诉，软件不会绕过登录、身份或安全验证。

【运行记录】
   每次预检、写入或失败都会生成 JSON 报告，可查看处理窗口、CID、动作和错误原因。
   列表直接显示失败原因；选中记录可在下方查看账户、对象名称、处理数量和步骤，无需打开 JSON。
   多窗口批量任务中单个窗口失败不会阻断其他窗口，完成后会汇总成功与失败数量。
   右键可查看详情、复制原因、打开原始 JSON 或删除记录；双击记录查看详情。

【运行日志颜色】
   每行自动添加本机时间：绿色表示成功，红色表示失败/异常，橙色表示警告/跳过。
   蓝色表示只读查询，紫色表示创建、复制、启用等写入动作，灰色表示普通信息。
   失败判断优先级最高，例如“读取失败”一定显示红色，不会因为包含“读取”而显示蓝色。

四、四类对象与固定用途

app：共享库 → 排除列表 → 应用类别，不是应用包名。
web：共享库 → 排除列表 → 网站。
xq：自定义受众 → 具有任意此类兴趣或购买意向的用户。
ss：自定义受众 → 曾在 Google 上搜索任意这些字词的用户。

五、安全规则

· “预检”只发送读取协议。
· 所有写入必须收到正常协议响应，并在写后重新读取确认，才会显示成功。
· 删除、替换内容都会二次确认。
· 多个指纹浏览器窗口依次处理，窗口间会话不会混用。
· 如果 Google Ads 更新内部协议，软件会明确报错并停止，不会改用鼠标模拟点击。
· 修改文案创建只支持响应式展示广告；层级复制支持系列、广告组和广告。所有新对象写后必须回读暂停状态。
· 本地图片上传只在目标 CID 状态可用且余额大于 0 后执行；预检不会上传。上传会话仅接受 Google HTTPS 地址。
· 素材库直接上传同样要求余额大于 0；Finalize 返回 ID 后还必须从目标账户的素材库回读验证，不能只凭 HTTP 200 判成功。
· 一键白量启用前会再次读取余额，余额不大于 0 或读取失败时立即跳过；不会把“—”当成有余额。
· 广告系列展示位置排除只接受当前日期范围报表中重新确认的网站，并在写后回读 CampaignCriterion。
· 定时申诉任务只生成本地草稿；用户确认真实资料后，“一键提交申诉”才会发送，并要求官方回执。登录、身份和安全验证不会被绕过。

六、常见问题

查询不到 CID：确认当前指纹浏览器窗口中已登录 Google Ads，并打开该 CID 或所属 MCC 的页面。
内容为空被跳过：先到“本地配置 → 本地内容库”填写对应内容并保存。
已有同名受众：批量任务默认跳过；如需覆盖，请在“Google Ads 数据”中查询后使用“编辑/替换”。
删除失败：对象可能仍被广告系列引用，请先解除引用后再删除。
"""


class ObjectDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, title: str, item: dict[str, Any] | None = None):
        super().__init__(parent)
        self.configure(background=COLOR_SURFACE)
        self.title(title)
        self.geometry("680x590")
        self.transient(parent)
        self.grab_set()
        self.result: tuple[str, str, list[Any]] | None = None
        self.item = item
        self.columnconfigure(1, weight=1)
        self.rowconfigure(3, weight=1)

        ttk.Label(self, text="类型").grid(row=0, column=0, padx=12, pady=(12, 6), sticky="w")
        self.kind = tk.StringVar(value=str((item or {}).get("kind") or "app"))
        self.kind_box = ttk.Combobox(self, textvariable=self.kind, values=list(TYPE_LABELS), state="readonly", width=18)
        self.kind_box.grid(row=0, column=1, padx=12, pady=(12, 6), sticky="w")
        if item:
            self.kind_box.configure(state="disabled")
        self.kind_box.bind("<<ComboboxSelected>>", lambda _event: self._show_editor())

        ttk.Label(self, text="名称").grid(row=1, column=0, padx=12, pady=6, sticky="w")
        self.name = ttk.Entry(self)
        self.name.grid(row=1, column=1, padx=12, pady=6, sticky="ew")
        self.name.insert(0, str((item or {}).get("name") or self.kind.get()))

        self.hint = ttk.Label(self, foreground=COLOR_MUTED)
        self.hint.grid(row=2, column=0, columnspan=2, padx=12, pady=6, sticky="w")

        self.editor = ttk.Frame(self)
        self.editor.grid(row=3, column=0, columnspan=2, padx=12, pady=6, sticky="nsew")
        self.editor.rowconfigure(1, weight=1)
        self.editor.columnconfigure(0, weight=1)
        app_buttons = ttk.Frame(self.editor)
        app_buttons.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(app_buttons, text="全选 140 类", command=self._select_all_apps).pack(side="left")
        ttk.Button(app_buttons, text="清空选择", command=lambda: self.app_list.selection_clear(0, "end")).pack(side="left", padx=8)
        self.app_list = tk.Listbox(self.editor, selectmode="extended", exportselection=False)
        self.app_list.grid(row=1, column=0, sticky="nsew")
        for category_id, name in APP_CATEGORIES:
            store = "Apple" if str(category_id).startswith("605") else "Google Play"
            self.app_list.insert("end", f"{store}  |  {name}  |  {category_id}")

        self.text = tk.Text(self.editor, wrap="none", font=("Consolas", 10))
        self.text.grid(row=1, column=0, sticky="nsew")
        self.content_vscroll = ttk.Scrollbar(
            self.editor,
            orient="vertical",
            command=lambda *args: (self.app_list if self.kind.get() == "app" else self.text).yview(*args),
        )
        self.content_vscroll.grid(row=1, column=1, sticky="ns")
        self.content_hscroll = ttk.Scrollbar(self.editor, orient="horizontal", command=self.text.xview)
        self.content_hscroll.grid(row=2, column=0, sticky="ew")
        self.app_list.configure(yscrollcommand=self.content_vscroll.set)
        self.text.configure(yscrollcommand=self.content_vscroll.set, xscrollcommand=self.content_hscroll.set)

        actions = ttk.Frame(self)
        actions.grid(row=4, column=0, columnspan=2, padx=12, pady=12, sticky="e")
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(actions, text="确定", command=self._accept, style="Accent.TButton").pack(side="right", padx=8)
        self._load_values(list((item or {}).get("values") or []))
        self._show_editor()
        self.wait_window(self)

    def _load_values(self, values: list[Any]) -> None:
        selected = {int(value) for value in values if str(value).isdigit()}
        for index, (category_id, _name) in enumerate(APP_CATEGORIES):
            if category_id in selected:
                self.app_list.selection_set(index)
        self.text.insert("1.0", "\n".join(str(value) for value in values))

    def _show_editor(self) -> None:
        if self.kind.get() == "app":
            self.text.grid_remove()
            self.app_list.grid()
            self.content_hscroll.grid_remove()
            self.hint.configure(text="选择要排除的应用类别；Ctrl/Shift 可多选。")
            if not self.app_list.curselection() and self.item is None:
                self._select_all_apps()
        else:
            self.app_list.grid_remove()
            self.text.grid()
            self.content_hscroll.grid()
            label = "网站域名" if self.kind.get() == "web" else "词语"
            self.hint.configure(text=f"每行填写一个{label}。{CONTENT_EXAMPLES.get(self.kind.get(), '')}")

    def _select_all_apps(self) -> None:
        self.app_list.selection_set(0, "end")

    def _accept(self) -> None:
        kind = self.kind.get()
        name = self.name.get().strip()
        if not name:
            messagebox.showerror("输入有误", "名称不能为空", parent=self)
            return
        if kind == "app":
            values = [APP_CATEGORIES[index][0] for index in self.app_list.curselection()]
        else:
            values = [line.strip() for line in self.text.get("1.0", "end").splitlines() if line.strip()]
        if not values:
            messagebox.showerror("输入有误", "内容不能为空", parent=self)
            return
        self.result = (kind, name, values)
        self.destroy()


class CidTaskDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        plans: list[str],
        item: dict[str, str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.configure(background=COLOR_SURFACE)
        self.title("CID 任务")
        self.geometry("570x330")
        self.minsize(500, 300)
        self.transient(parent)
        self.grab_set()
        self.result: tuple[str, str, str] | None = None
        self.columnconfigure(1, weight=1)
        self.rowconfigure(2, weight=1)
        item = item or {}

        ttk.Label(self, text="Google Ads CID").grid(row=0, column=0, padx=12, pady=(14, 7), sticky="w")
        self.cid = ttk.Entry(self)
        self.cid.grid(row=0, column=1, padx=12, pady=(14, 7), sticky="ew")
        initial_cid = str(item.get("cid") or "")
        self.cid.insert(0, format_cid(initial_cid) if initial_cid else "")

        ttk.Label(self, text="内容方案").grid(row=1, column=0, padx=12, pady=7, sticky="w")
        initial_plan = item.get("dictionary", "")
        if initial_plan not in plans:
            initial_plan = plans[0]
        self.dictionary = tk.StringVar(value=initial_plan)
        ttk.Combobox(
            self,
            textvariable=self.dictionary,
            values=plans,
            state="readonly",
        ).grid(row=1, column=1, padx=12, pady=7, sticky="ew")

        ttk.Label(self, text="备注").grid(row=2, column=0, padx=12, pady=7, sticky="nw")
        note_frame = ttk.Frame(self)
        note_frame.grid(row=2, column=1, padx=12, pady=7, sticky="nsew")
        note_frame.columnconfigure(0, weight=1)
        note_frame.rowconfigure(0, weight=1)
        self.note = tk.Text(note_frame, height=7, wrap="word", font=("Microsoft YaHei UI", 10))
        self.note.grid(row=0, column=0, sticky="nsew")
        note_scroll = ttk.Scrollbar(note_frame, orient="vertical", command=self.note.yview)
        note_scroll.grid(row=0, column=1, sticky="ns")
        self.note.configure(yscrollcommand=note_scroll.set)
        self.note.insert("1.0", item.get("note", ""))

        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, columnspan=2, padx=12, pady=14, sticky="e")
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(actions, text="确定", command=self._accept, style="Accent.TButton").pack(side="right", padx=8)
        self.cid.focus_set()
        self.wait_window(self)

    def _accept(self) -> None:
        try:
            cid = normalize_cid(self.cid.get())
        except Exception as exc:
            messagebox.showerror("输入有误", str(exc), parent=self)
            return
        dictionary = self.dictionary.get().strip()
        if not dictionary:
            messagebox.showerror("输入有误", "请选择内容方案", parent=self)
            return
        note = self.note.get("1.0", "end-1c").strip()
        self.result = (cid, dictionary, note)
        self.destroy()


class WindowDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        item: dict[str, str] | None = None,
        metadata_resolver: Callable[..., dict[str, str]] | None = None,
        *,
        browser_type: str = BITBROWSER,
    ) -> None:
        super().__init__(parent)
        self.browser_type = normalize_browser_type(browser_type, default=BITBROWSER)
        browser_label = "比特浏览器" if self.browser_type == BITBROWSER else "AdsPower"
        self.configure(background=COLOR_SURFACE)
        self.title(f"{browser_label}窗口")
        self.geometry("590x245")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result: tuple[str, str] | None = None
        self.resolved_metadata: dict[str, str] | None = None
        self.metadata_resolver = metadata_resolver
        self.lookup_results: queue.Queue[tuple[bool, Any]] = queue.Queue()
        self.columnconfigure(1, weight=1)
        item = item or {}

        ttk.Label(self, text="窗口 ID").grid(row=0, column=0, padx=14, pady=(16, 8), sticky="w")
        self.profile = ttk.Entry(self)
        self.profile.grid(row=0, column=1, columnspan=2, padx=14, pady=(16, 8), sticky="ew")
        self.profile.insert(0, item.get("profile_id", ""))

        ttk.Label(self, text="窗口编号").grid(row=1, column=0, padx=14, pady=8, sticky="w")
        self.window_number = ttk.Entry(self)
        self.window_number.grid(row=1, column=1, padx=(14, 7), pady=8, sticky="ew")
        self.window_number.insert(0, item.get("window_number", ""))
        self.lookup_button = ttk.Button(
            self, text="自动读取", command=self._lookup, style="Accent.TButton"
        )
        self.lookup_button.grid(row=1, column=2, padx=(7, 14), pady=8)

        self.hint = ttk.Label(
            self,
            text=f"{browser_label}窗口 ID；编号可留空，保存后也会自动读取。",
            foreground=COLOR_MUTED,
        )
        self.hint.grid(row=2, column=0, columnspan=3, padx=14, pady=(4, 8), sticky="w")

        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, columnspan=3, padx=14, pady=(14, 12), sticky="e")
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(actions, text="确定", command=self._accept, style="Accent.TButton").pack(side="right", padx=8)
        self.profile.focus_set()
        self.after(100, self._poll_lookup)
        self.wait_window(self)

    def _lookup(self) -> None:
        profile = self.profile.get().strip()
        if not profile:
            messagebox.showerror("输入有误", "请先填写窗口 ID", parent=self)
            return
        self.lookup_button.configure(state="disabled")
        browser_label = "比特浏览器" if self.browser_type == BITBROWSER else "AdsPower"
        self.hint.configure(text=f"正在从{browser_label}读取窗口编号……")

        def worker() -> None:
            try:
                if self.metadata_resolver is None:
                    payload = normalize_window_metadata(
                        profile,
                        query_browser_profile_info(
                            profile, browser_type=self.browser_type
                        ),
                    )
                else:
                    payload = self.metadata_resolver(
                        profile,
                        browser_type=self.browser_type,
                        force_refresh=True,
                        timeout=2.0,
                    )
                self.lookup_results.put((True, payload))
            except Exception as exc:
                self.lookup_results.put((False, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_lookup(self) -> None:
        try:
            ok, payload = self.lookup_results.get_nowait()
        except queue.Empty:
            if self.winfo_exists():
                self.after(100, self._poll_lookup)
            return
        self.lookup_button.configure(state="normal")
        if ok:
            self.resolved_metadata = dict(payload)
            self.window_number.delete(0, "end")
            self.window_number.insert(0, str(payload.get("serial_number") or ""))
            name = str(payload.get("name") or "")
            self.hint.configure(text=f"读取成功：{name or '未命名窗口'}")
        else:
            self.hint.configure(text="自动读取失败；可手工填写窗口编号后保存。", foreground=COLOR_DANGER)
            messagebox.showwarning("读取失败", str(payload), parent=self)
        if self.winfo_exists():
            self.after(100, self._poll_lookup)

    def _accept(self) -> None:
        profile = self.profile.get().strip()
        if not profile:
            messagebox.showerror("输入有误", "窗口 ID 不能为空", parent=self)
            return
        self.result = (profile, self.window_number.get().strip())
        self.destroy()


class _ManagedBitBrowserDialog(tk.Toplevel):
    """Own Tk callbacks so late worker results cannot touch a closed dialog."""

    def _init_lifecycle(self) -> None:
        self.closed = False
        self._after_ids: set[str] = set()

    def _is_open(self) -> bool:
        if self.closed:
            return False
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def _schedule(self, delay_ms: int, callback: Callable[[], None]) -> str | None:
        if not self._is_open():
            return None
        after_id = ""

        def guarded_callback() -> None:
            self._after_ids.discard(after_id)
            if self._is_open():
                callback()

        after_id = str(self.after(delay_ms, guarded_callback))
        self._after_ids.add(after_id)
        return after_id

    def _discard_results(self) -> None:
        while True:
            try:
                self.results.get_nowait()
            except queue.Empty:
                return

    def _close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for after_id in tuple(self._after_ids):
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
        self._after_ids.clear()
        self._discard_results()
        self.destroy()


class BitBrowserSettingsDialog(_ManagedBitBrowserDialog):
    """Edit the loopback BitBrowser Local API address and 7.1.5 API token."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self._init_lifecycle()
        self.title("比特浏览器 Local API 设置")
        self.geometry("700x360")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.results: queue.Queue[tuple[bool, str]] = queue.Queue()
        self.columnconfigure(1, weight=1)
        self._token_visible = tk.BooleanVar(value=False)

        ttk.Label(self, text="Local API 地址").grid(
            row=0, column=0, padx=14, pady=(18, 8), sticky="w"
        )
        self.api_base = ttk.Entry(self)
        self.api_base.grid(row=0, column=1, padx=14, pady=(18, 8), sticky="ew")
        self.api_base.insert(0, load_bitbrowser_api_base())
        ttk.Label(
            self,
            text="7.1.5 的系统设置里本地接口是 http://127.0.0.1:54346。程序会先连这个端口。",
            foreground=COLOR_MUTED,
            wraplength=660,
        ).grid(row=1, column=0, columnspan=2, padx=14, pady=5, sticky="w")
        ttk.Label(self, text="API Token").grid(row=2, column=0, padx=14, pady=(8, 4), sticky="w")
        self.api_token = ttk.Entry(self, show="*")
        self.api_token.grid(row=2, column=1, padx=14, pady=(8, 4), sticky="ew")
        self.api_token.insert(0, load_bitbrowser_api_token())
        token_hint = ttk.Frame(self)
        token_hint.grid(row=3, column=0, columnspan=2, padx=14, pady=5, sticky="ew")
        ttk.Label(
            token_hint,
            text="鉴权控制关着就留空。只有打开那个开关后才填写，请求会带上 X-API-KEY。",
            foreground=COLOR_MUTED,
        ).pack(side="left")
        ttk.Checkbutton(
            token_hint,
            text="显示",
            variable=self._token_visible,
            command=self._toggle_token,
        ).pack(side="right")
        self.status = ttk.Label(
            self, text="尚未测试连接", foreground=COLOR_MUTED, wraplength=660
        )
        self.status.grid(row=4, column=0, columnspan=2, padx=14, pady=5, sticky="w")
        actions = ttk.Frame(self)
        actions.grid(row=5, column=0, columnspan=2, padx=14, pady=(14, 12), sticky="e")
        ttk.Button(actions, text="取消", command=self._close).pack(side="right")
        ttk.Button(
            actions, text="保存", command=self._save, style="Success.TButton"
        ).pack(side="right", padx=8)
        self.test_button = ttk.Button(
            actions,
            text="保存并测试连接",
            command=self._test,
            style="Accent.TButton",
        )
        self.test_button.pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._schedule(100, self._poll_results)
        self.api_base.focus_set()
        self.wait_window(self)

    def _toggle_token(self) -> None:
        self.api_token.configure(show="" if self._token_visible.get() else "*")

    def _save(self) -> bool:
        try:
            save_bitbrowser_local_api(self.api_base.get(), self.api_token.get())
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return False
        self.status.configure(text="设置已保存", foreground=COLOR_SUCCESS)
        return True

    def _test(self) -> None:
        if not self._is_open():
            return
        if not self._save():
            return
        self.test_button.configure(state="disabled")
        self.status.configure(text="正在测试比特浏览器 Local API……", foreground=COLOR_MUTED)

        def worker() -> None:
            try:
                bitbrowser_health(timeout=2.0)
                self.results.put((True, "连接正常"))
            except Exception as exc:
                self.results.put((False, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_results(self) -> None:
        if not self._is_open():
            self._discard_results()
            return
        try:
            ok, detail = self.results.get_nowait()
        except queue.Empty:
            self._schedule(100, self._poll_results)
            return
        if not self._is_open():
            return
        self.test_button.configure(state="normal")
        self.status.configure(
            text="连接正常" if ok else f"连接失败：{detail}",
            foreground=COLOR_SUCCESS if ok else COLOR_DANGER,
        )
        self._schedule(100, self._poll_results)


class BitBrowserImportDialog(_ManagedBitBrowserDialog):
    """Read-only BitBrowser profile picker; it never starts a browser window."""

    def __init__(self, parent: tk.Misc, existing_keys: set[str]) -> None:
        super().__init__(parent)
        self._init_lifecycle()
        self.title("导入比特浏览器窗口")
        self.geometry("980x620")
        self.minsize(800, 500)
        self.transient(parent)
        self.grab_set()
        self.existing_keys = set(existing_keys)
        self.all_rows: list[dict[str, Any]] = []
        self.selected_keys: set[str] = set()
        self.result: list[dict[str, Any]] | None = None
        self.results: queue.Queue[tuple[bool, Any]] = queue.Queue()
        self.search = tk.StringVar()
        self.status = tk.StringVar(value="正在连接比特浏览器并读取窗口列表……")

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        search_row = ttk.Frame(body)
        search_row.grid(row=0, column=0, sticky="ew")
        search_row.columnconfigure(1, weight=1)
        ttk.Label(search_row, text="搜索").grid(row=0, column=0, padx=(0, 8))
        search_entry = ttk.Entry(search_row, textvariable=self.search)
        search_entry.grid(row=0, column=1, sticky="ew")
        ttk.Button(search_row, text="刷新列表", command=self._start_load).grid(
            row=0, column=2, padx=(8, 0)
        )
        ttk.Label(
            body,
            text="按名称、序号或备注搜索；读取列表不会启动、关闭、创建或删除浏览器窗口。",
            foreground=COLOR_MUTED,
        ).grid(row=1, column=0, sticky="w", pady=(8, 6))

        tree_frame = ttk.Frame(body)
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        columns = ("selected", "sequence", "name", "remark", "status", "imported", "profile")
        self.tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", selectmode="extended"
        )
        headings = {
            "selected": "选择",
            "sequence": "序号",
            "name": "名称",
            "remark": "备注",
            "status": "运行状态",
            "imported": "导入状态",
            "profile": "原始窗口 ID",
        }
        widths = {
            "selected": 58,
            "sequence": 75,
            "name": 180,
            "remark": 240,
            "status": 90,
            "imported": 95,
            "profile": 220,
        }
        for key in columns:
            self.tree.heading(key, text=headings[key])
            self.tree.column(
                key,
                width=widths[key],
                anchor="center" if key in {"selected", "sequence", "status", "imported"} else "w",
            )
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.bind("<Button-1>", self._click_checkbox)
        self.tree.bind("<space>", self._toggle_selected)

        controls = ttk.Frame(body)
        controls.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(controls, text="全选可导入", command=self._select_all).pack(side="left")
        ttk.Button(controls, text="反选", command=self._invert).pack(side="left", padx=8)
        ttk.Button(controls, text="切换所选", command=self._toggle_selected).pack(side="left")
        ttk.Label(controls, textvariable=self.status, foreground=COLOR_MUTED).pack(
            side="left", padx=14
        )
        ttk.Button(controls, text="取消", command=self._close).pack(side="right")
        ttk.Button(
            controls,
            text="导入选中窗口",
            command=self._accept,
            style="Write.TButton",
        ).pack(side="right", padx=8)

        self.search.trace_add("write", lambda *_args: self._render_rows())
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._schedule(50, self._start_load)
        self._schedule(100, self._poll_results)
        search_entry.focus_set()
        self.wait_window(self)

    def _start_load(self) -> None:
        if not self._is_open():
            return
        self.status.set("正在连接比特浏览器并读取窗口列表……")

        def worker() -> None:
            try:
                bitbrowser_health(timeout=2.0)
                self.results.put((True, list_bitbrowser_profiles(timeout=10.0)))
            except Exception as exc:
                self.results.put((False, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_results(self) -> None:
        if not self._is_open():
            self._discard_results()
            return
        try:
            ok, payload = self.results.get_nowait()
        except queue.Empty:
            self._schedule(100, self._poll_results)
            return
        if not self._is_open():
            return
        if ok:
            self.all_rows = filter_importable_windows(payload, self.existing_keys)
            self.selected_keys.intersection_update(
                window_key(row["browser_type"], row["profile_id"])
                for row in self.all_rows
            )
            self._render_rows()
        else:
            self.status.set(f"读取失败：{payload}")
            messagebox.showwarning("读取失败", str(payload), parent=self)
        self._schedule(100, self._poll_results)

    def _visible_rows(self) -> list[dict[str, Any]]:
        return filter_importable_windows(
            self.all_rows, self.existing_keys, self.search.get()
        )

    def _render_rows(self) -> None:
        if not self._is_open() or not hasattr(self, "tree"):
            return
        self.tree.delete(*self.tree.get_children())
        rows = self._visible_rows()
        for row in rows:
            key = window_key(row["browser_type"], row["profile_id"])
            self.tree.insert(
                "",
                "end",
                iid=key,
                values=(
                    "☑" if key in self.selected_keys else "☐",
                    row["serial_number"],
                    row["name"],
                    row["remark"],
                    row["running_status"],
                    "已导入" if row["already_imported"] else "可导入",
                    row["profile_id"],
                ),
            )
        imported = sum(bool(row["already_imported"]) for row in rows)
        self.status.set(f"显示 {len(rows)} 个窗口 · 已导入 {imported} 个 · 已选 {len(self.selected_keys)} 个")

    def _select_all(self) -> None:
        self.selected_keys.update(
            window_key(row["browser_type"], row["profile_id"])
            for row in self._visible_rows()
            if not row["already_imported"]
        )
        self._render_rows()

    def _invert(self) -> None:
        for row in self._visible_rows():
            if row["already_imported"]:
                continue
            key = window_key(row["browser_type"], row["profile_id"])
            if key in self.selected_keys:
                self.selected_keys.remove(key)
            else:
                self.selected_keys.add(key)
        self._render_rows()

    def _toggle_selected(self, _event: tk.Event | None = None) -> str:
        keys = self.tree.selection()
        if not keys and self.tree.focus():
            keys = (self.tree.focus(),)
        imported = {
            window_key(row["browser_type"], row["profile_id"])
            for row in self.all_rows
            if row["already_imported"]
        }
        for key in keys:
            if key in imported:
                continue
            if key in self.selected_keys:
                self.selected_keys.remove(key)
            else:
                self.selected_keys.add(key)
        self._render_rows()
        return "break"

    def _click_checkbox(self, event: tk.Event) -> str | None:
        iid = self.tree.identify_row(event.y)
        if iid and self.tree.identify_column(event.x) == "#1":
            self.tree.selection_set(iid)
            self.tree.focus(iid)
            return self._toggle_selected()
        return None

    def _accept(self) -> None:
        self.result = [
            dict(row)
            for row in self.all_rows
            if window_key(row["browser_type"], row["profile_id"])
            in self.selected_keys
            and not row["already_imported"]
        ]
        if not self.result:
            messagebox.showinfo("没有选择", "请选择至少一个尚未导入的窗口", parent=self)
            return
        self._close()


class CreativePlanDialog(tk.Toplevel):
    """Editor for one local image plan or copy plan."""

    def __init__(
        self,
        parent: tk.Misc,
        kind: str,
        *,
        name: str = "",
        item: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.kind = kind
        self.result: tuple[str, dict[str, Any]] | None = None
        self.title("图片方案" if kind == "image" else "文案方案")
        self.geometry("1180x700" if kind == "image" else "920x720")
        self.minsize(980 if kind == "image" else 760, 600 if kind == "image" else 560)
        self.transient(parent)
        self.grab_set()
        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="方案名称").grid(row=0, column=0, sticky="w", pady=4)
        self.name_entry = ttk.Entry(body)
        self.name_entry.grid(row=0, column=1, sticky="ew", pady=4)
        self.name_entry.insert(0, name)
        ttk.Label(body, text="备注").grid(row=1, column=0, sticky="w", pady=4)
        self.note_entry = ttk.Entry(body)
        self.note_entry.grid(row=1, column=1, sticky="ew", pady=4)
        self.note_entry.insert(0, str((item or {}).get("note") or ""))
        if kind == "image":
            self._build_images(body, item or {})
        else:
            self._build_copy(body, item or {})
        actions = ttk.Frame(body)
        actions.grid(row=9, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(actions, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(
            actions, text="保存方案", command=self._accept, style="Success.TButton"
        ).pack(side="right", padx=8)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.name_entry.focus_set()
        self.wait_window(self)

    def _build_images(self, body: ttk.Frame, item: dict[str, Any]) -> None:
        self.image_preview_photo: ImageTk.PhotoImage | None = None
        self.image_preview_path = ""
        body.rowconfigure(3, weight=1)
        ttk.Label(
            body,
            text="保存时图片会导入软件本地素材目录。横图 1.91:1；方图 1:1；竖图 9:16/4:5；PNG/JPG/JPEG，单张 ≤ 5 MB。",
            foreground=COLOR_MUTED,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 5))
        frame = ttk.Frame(body)
        frame.grid(row=3, column=0, columnspan=2, sticky="nsew")
        frame.columnconfigure(0, weight=3)
        frame.columnconfigure(2, weight=1, minsize=320)
        frame.rowconfigure(0, weight=1)
        self.image_tree = ttk.Treeview(
            frame, columns=("role", "file", "dimensions", "reason", "size", "path"),
            show="headings", selectmode="extended",
        )
        for key, label in (
            ("role", "用途"), ("file", "文件名"), ("dimensions", "尺寸"),
            ("reason", "自动识别依据"), ("size", "大小"), ("path", "本地路径"),
        ):
            self.image_tree.heading(key, text=label)
        self.image_tree.column("role", width=70, anchor="center")
        self.image_tree.column("file", width=190)
        self.image_tree.column("dimensions", width=105, anchor="center")
        self.image_tree.column("reason", width=260)
        self.image_tree.column("size", width=90, anchor="e")
        self.image_tree.column("path", width=390)
        self.image_tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(frame, orient="vertical", command=self.image_tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.image_tree.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.image_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        preview = ttk.LabelFrame(frame, text="选中图片预览", padding=8)
        preview.grid(row=0, column=2, rowspan=2, sticky="nsew", padx=(10, 0))
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(0, weight=1)
        self.image_preview_label = tk.Label(
            preview, text="在左侧选择图片\n即可查看内容",
            background="#F8FAFC", foreground=COLOR_MUTED,
            relief="solid", borderwidth=1, cursor="hand2",
        )
        self.image_preview_label.grid(row=0, column=0, sticky="nsew")
        self.image_preview_detail = ttk.Label(
            preview, text="双击预览图可打开原图", style="Muted.TLabel",
            anchor="center", justify="center", wraplength=290,
        )
        self.image_preview_detail.grid(row=1, column=0, sticky="ew", pady=(7, 4))
        ttk.Button(
            preview, text="打开原图", command=self._open_selected_image,
            style="Accent.TButton",
        ).grid(row=2, column=0, pady=(3, 0))
        self.image_tree.bind("<<TreeviewSelect>>", self._preview_selected_image)
        self.image_tree.bind("<Double-1>", self._open_selected_image)
        self.image_preview_label.bind("<Double-1>", self._open_selected_image)
        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(
            buttons, text="批量添加图片（自动识别）",
            command=self._add_images_auto, style="Accent.TButton",
        ).pack(side="left", padx=(0, 10))
        ttk.Label(buttons, text="选中图片改为：", style="Muted.TLabel").pack(side="left")
        for role in ("landscape", "square", "portrait"):
            ttk.Button(
                buttons, text=ROLE_LABELS[role],
                command=lambda selected_role=role: self._reassign_images(selected_role),
            ).pack(side="left", padx=(5, 0))
        ttk.Button(buttons, text="移除选中", command=self._remove_images).pack(side="left", padx=(12, 0))
        self.image_import_summary = ttk.Label(
            body,
            text="按真实宽高自动识别：1.91:1 横图；1:1 方图；9:16/4:5 竖图。",
            foreground=COLOR_MUTED,
        )
        self.image_import_summary.grid(row=5, column=0, columnspan=2, sticky="w", pady=(7, 0))
        for role, paths in ((item.get("images") or {}).items()):
            if role in ROLE_LABELS:
                for path in paths or []:
                    self._insert_image(role, str(path), validate=False, reason="方案中已有分类")
        children = self.image_tree.get_children()
        if children:
            self.image_tree.selection_set(children[0])
            self.image_tree.focus(children[0])
            self.after_idle(self._preview_selected_image)

    def _preview_selected_image(self, _event: tk.Event | None = None) -> None:
        selected = self.image_tree.selection()
        if not selected:
            self.image_preview_path = ""
            self.image_preview_photo = None
            self.image_preview_label.configure(
                image="", text="在左侧选择图片\n即可查看内容",
                foreground=COLOR_MUTED,
            )
            self.image_preview_detail.configure(text="双击预览图可打开原图")
            return
        path = str(self.image_tree.set(selected[0], "path"))
        self.image_preview_path = path
        try:
            with Image.open(path) as source:
                preview = ImageOps.exif_transpose(source).convert("RGBA")
            preview.thumbnail((360, 430), Image.Resampling.LANCZOS)
            background = Image.new("RGBA", preview.size, "white")
            background.alpha_composite(preview)
            self.image_preview_photo = ImageTk.PhotoImage(background.convert("RGB"))
            self.image_preview_label.configure(
                image=self.image_preview_photo, text="", foreground=COLOR_TEXT,
            )
            dimensions = self.image_tree.set(selected[0], "dimensions")
            role = self.image_tree.set(selected[0], "role")
            selection_note = f" · 已选 {len(selected)} 张" if len(selected) > 1 else ""
            self.image_preview_detail.configure(
                text=f"{Path(path).name}\n{role} · {dimensions}{selection_note}\n双击打开原图"
            )
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            self.image_preview_photo = None
            self.image_preview_label.configure(
                image="", text="图片无法预览", foreground=COLOR_DANGER,
            )
            self.image_preview_detail.configure(text=f"{Path(path).name}\n{exc}")

    def _open_selected_image(self, _event: tk.Event | None = None) -> None:
        path = self.image_preview_path
        if not path or not Path(path).is_file():
            messagebox.showwarning("无法打开", "请先选择一张有效图片。", parent=self)
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc), parent=self)

    def _insert_image(
        self, role: str, path: str, *, validate: bool, reason: str = "手工指定用途",
    ) -> None:
        try:
            info = validate_role_image(path, role)
            dimensions = f"{info.width} × {info.height}"
            size = f"{info.size / 1024:.1f} KB"
            tag = ""
        except Exception:
            if validate:
                raise
            dimensions, size, tag = "文件失效", "—", "missing"
        iid = self.image_tree.insert(
            "", "end",
            values=(ROLE_LABELS[role], Path(path).name, dimensions, reason, size, path),
            tags=(tag,) if tag else (),
        )
        self.image_tree.set(iid, "role", ROLE_LABELS[role])
        self.image_tree.tag_configure("missing", foreground=COLOR_DANGER, background=COLOR_DANGER_SOFT)
        if not self.image_tree.selection():
            self.image_tree.selection_set(iid)
            self.image_tree.focus(iid)
            self.after_idle(self._preview_selected_image)

    def _add_images_auto(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self, title="批量添加图片并自动识别用途",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg"), ("所有文件", "*.*")],
        )
        if not paths:
            return
        grouped, details = classify_image_roles(paths)
        existing = {
            str(Path(self.image_tree.set(iid, "path")).resolve())
            for iid in self.image_tree.get_children()
        }
        reason_by_key = {
            (str(row.get("role")), str(row.get("path"))): str(row.get("reason") or "")
            for row in details if row.get("role") and not row.get("duplicate")
        }
        added = {role: 0 for role in ROLE_LABELS}
        for role, images in grouped.items():
            for info in images:
                if info.path in existing:
                    continue
                self._insert_image(
                    role, info.path, validate=True,
                    reason=reason_by_key.get((role, info.path), "自动识别"),
                )
                existing.add(info.path)
                added[role] += 1
        errors = [row for row in details if row.get("error")]
        duplicates = sum(bool(row.get("duplicate")) for row in details)
        summary = (
            f"本次新增：横图 {added['landscape']}、方图 {added['square']}、竖图 {added['portrait']}"
            f"；重复跳过 {duplicates}；失败 {len(errors)}"
        )
        self.image_import_summary.configure(
            text=summary,
            foreground=COLOR_WARNING if errors else COLOR_SUCCESS,
        )
        if errors:
            messagebox.showwarning(
                "部分图片未导入",
                summary + "\n\n" + "\n".join(str(row["reason"]) for row in errors[:12]),
                parent=self,
            )

    def _reassign_images(self, role: str) -> None:
        selected = self.image_tree.selection()
        if not selected:
            messagebox.showinfo("请选择图片", "请先选择要调整用途的图片。", parent=self)
            return
        changed = 0
        errors: list[str] = []
        for iid in selected:
            path = str(self.image_tree.set(iid, "path"))
            try:
                validate_role_image(path, role)
            except Exception as exc:
                errors.append(str(exc))
                continue
            self.image_tree.set(iid, "role", ROLE_LABELS[role])
            self.image_tree.set(iid, "reason", f"用户手工改为{ROLE_LABELS[role]}")
            changed += 1
        self.image_import_summary.configure(
            text=f"已把 {changed} 张图片改为{ROLE_LABELS[role]}" + (
                f"；{len(errors)} 张比例不符合，未修改" if errors else ""
            ),
            foreground=COLOR_WARNING if errors else COLOR_SUCCESS,
        )
        if errors:
            messagebox.showwarning(
                "部分图片无法调整", "\n".join(errors[:10]), parent=self,
            )

    def _add_images(self, role: str) -> None:
        paths = filedialog.askopenfilenames(
            parent=self, title=f"添加{ROLE_LABELS[role]}",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg"), ("所有文件", "*.*")],
        )
        existing = {str(self.image_tree.set(iid, "path")) for iid in self.image_tree.get_children()}
        try:
            for path in paths:
                resolved = str(Path(path).resolve())
                if resolved not in existing:
                    self._insert_image(role, resolved, validate=True)
                    existing.add(resolved)
        except Exception as exc:
            messagebox.showerror("图片不符合要求", str(exc), parent=self)

    def _remove_images(self) -> None:
        for iid in self.image_tree.selection():
            self.image_tree.delete(iid)
        children = self.image_tree.get_children()
        if children:
            self.image_tree.selection_set(children[0])
            self.image_tree.focus(children[0])
        self._preview_selected_image()

    def _build_copy(self, body: ttk.Frame, item: dict[str, Any]) -> None:
        body.rowconfigure(3, weight=1)
        fields = ttk.Frame(body)
        fields.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 5))
        fields.columnconfigure(1, weight=1)
        self.copy_entries: dict[str, ttk.Entry] = {}
        for row, (key, label) in enumerate((
            ("final_url", "最终网址"), ("business_name", "商家名称"),
            ("long_headline", "长标题"),
        )):
            ttk.Label(fields, text=label).grid(row=row, column=0, sticky="w", pady=4)
            entry = ttk.Entry(fields)
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            entry.insert(0, str(item.get(key) or ""))
            self.copy_entries[key] = entry
        editor = ttk.Frame(body)
        editor.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(4, 0))
        editor.columnconfigure(0, weight=1)
        editor.columnconfigure(1, weight=1)
        editor.rowconfigure(1, weight=1)
        ttk.Label(editor, text="短标题（每行一条，最多 5 条，每条 30 字符）").grid(row=0, column=0, sticky="w")
        ttk.Label(editor, text="描述（每行一条，最多 5 条，每条 90 字符）").grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.copy_headlines = tk.Text(editor, wrap="word", font=("Consolas", 10))
        self.copy_headlines.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        self.copy_descriptions = tk.Text(editor, wrap="word", font=("Consolas", 10))
        self.copy_descriptions.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(4, 0))
        self.copy_headlines.insert("1.0", "\n".join(item.get("headlines") or []))
        self.copy_descriptions.insert("1.0", "\n".join(item.get("descriptions") or []))
        ttk.Label(
            body,
            text="字段可以留空；应用到广告时只覆盖非空字段，其他字段继续使用当前参考广告内容。",
            foreground=COLOR_MUTED,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(7, 0))

    def _accept(self) -> None:
        name = self.name_entry.get().strip()
        if not name:
            messagebox.showerror("保存失败", "方案名称不能为空", parent=self)
            return
        try:
            if self.kind == "image":
                images = {role: [] for role in ROLE_LABELS}
                label_to_role = {label: role for role, label in ROLE_LABELS.items()}
                for iid in self.image_tree.get_children():
                    role = label_to_role[str(self.image_tree.set(iid, "role"))]
                    path = str(self.image_tree.set(iid, "path"))
                    info = validate_role_image(path, role)
                    if info.path not in images[role]:
                        images[role].append(info.path)
                if not any(images.values()):
                    raise ValueError("图片方案至少需要 1 张横图、方图或竖图")
                value = {"note": self.note_entry.get().strip(), "images": images}
            else:
                value = {
                    "note": self.note_entry.get().strip(),
                    **{key: entry.get().strip() for key, entry in self.copy_entries.items()},
                    "headlines": [line.strip() for line in self.copy_headlines.get("1.0", "end").splitlines() if line.strip()],
                    "descriptions": [line.strip() for line in self.copy_descriptions.get("1.0", "end").splitlines() if line.strip()],
                }
                # Reuse the same length and URL rules when the plan is complete;
                # partial plans are intentionally allowed for selective overlays.
                if len(value["business_name"]) > 25:
                    raise ValueError("商家名称最多 25 个字符")
                if len(value["long_headline"]) > 90:
                    raise ValueError("长标题最多 90 个字符")
                if len(value["headlines"]) > 5 or any(len(line) > 30 for line in value["headlines"]):
                    raise ValueError("短标题最多 5 条，每条最多 30 个字符")
                if len(value["descriptions"]) > 5 or any(len(line) > 90 for line in value["descriptions"]):
                    raise ValueError("描述最多 5 条，每条最多 90 个字符")
                if not any(value.get(key) for key in (
                    "final_url", "business_name", "long_headline", "headlines", "descriptions"
                )):
                    raise ValueError("文案方案至少需要填写一项广告内容")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        self.result = (name, value)
        self.destroy()


class GGAdsApp(tk.Tk):
    def __init__(self, *, smoke_test: bool = False) -> None:
        super().__init__()
        self._smoke_test = smoke_test
        self.title(f"GGAds 协议管理器 v{__version__}")
        self.geometry("1500x920")
        self.minsize(1280, 760)
        self._app_icon_image: tk.PhotoImage | None = None
        self._header_icon_image: tk.PhotoImage | None = None
        try:
            self._app_icon_image = tk.PhotoImage(file=str(bundled_asset("ggads-logo.png")))
            self.iconphoto(True, self._app_icon_image)
            if sys.platform == "win32":
                self.iconbitmap(default=str(bundled_asset("ggads.ico")))
        except (OSError, tk.TclError):
            # The source tree remains runnable even when optional branding assets
            # have not been generated yet.
            self._app_icon_image = None
        self._setup_theme()
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.running = False
        try:
            self.config_data = load_config()
        except Exception:
            self.config_data = copy.deepcopy(DEFAULT_CONFIG)
        try:
            self.creative_library = load_creative_library()
        except Exception:
            self.creative_library = copy.deepcopy(DEFAULT_CREATIVE_LIBRARY)
        self.remote_items: dict[str, dict[str, Any]] = {}
        self.remote_profile_by_label: dict[str, tuple[str, str]] = {}
        self.ad_profile_by_label: dict[str, tuple[str, str]] = {}
        self.placement_profile_by_label: dict[str, tuple[str, str]] = {}
        self.creative_upload_profile_by_label: dict[str, tuple[str, str]] = {}
        self.copy_profile_by_label: dict[str, tuple[str, str]] = {}
        self.ga4_profile_by_label: dict[str, tuple[str, str]] = {}
        self.remote_window = tk.StringVar()
        self.remote_cid = tk.StringVar()
        self.ad_window = tk.StringVar()
        self.ad_cid = tk.StringVar()
        self.ad_source_campaign = tk.StringVar()
        self.ad_source_group = tk.StringVar()
        self.ad_source_ad = tk.StringVar()
        self.ad_target_campaign = tk.StringVar()
        self.ad_target_group = tk.StringVar()
        self.ad_image_plan = tk.StringVar(value="（不使用图片方案）")
        self.ad_copy_plan = tk.StringVar(value="（不使用文案方案）")
        self.creative_upload_window = tk.StringVar()
        self.creative_upload_cid = tk.StringVar()
        self.creative_search = tk.StringVar()
        self.creative_type_filter = tk.StringVar(value="全部类型")
        self.creative_page_size = tk.StringVar(value="20")
        self.creative_page_info = tk.StringVar(value="第 1/1 页 · 共 0 个方案")
        self.creative_page_index = 0
        self._creative_thumbnail_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="creative-thumbnail",
        )
        self._creative_thumbnail_generation = 0
        self._creative_thumbnail_futures: set[Future[CreativeThumbnail]] = set()
        self._creative_thumbnail_photos: dict[str, ImageTk.PhotoImage] = {}
        self.remote_asset_search = tk.StringVar()
        self.remote_asset_page_size = tk.StringVar(value="50")
        self.remote_asset_page_info = tk.StringVar(value="第 1/1 页 · 共 0 个素材")
        self.remote_asset_page_index = 0
        self.remote_asset_rows: list[dict[str, Any]] = []
        self.remote_asset_context: dict[str, Any] = {}
        self.remote_loaded_context: tuple[str, str, str] | None = None
        self.ad_catalog: AdCatalog | None = None
        self.ad_catalog_context: tuple[str, str, str] | None = None
        self.ad_local_images: dict[str, list[str]] = {
            "landscape": [], "square": [], "portrait": [],
        }
        self.ad_reference_image_rows: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        self.ad_reference_loading_keys: set[tuple[str, str, str, str]] = set()
        self.ad_reference_request_key: tuple[str, str, str, str] | None = None
        self.ad_reference_photo_images: list[ImageTk.PhotoImage] = []
        self.copy_source_window = tk.StringVar()
        self.copy_source_cid = tk.StringVar()
        self.copy_target_window = tk.StringVar()
        self.copy_target_cid = tk.StringVar()
        self.copy_kind = tk.StringVar(value="广告系列")
        self.copy_source_campaign = tk.StringVar()
        self.copy_source_group = tk.StringVar()
        self.copy_source_ad = tk.StringVar()
        self.copy_target_campaign = tk.StringVar()
        self.copy_target_group = tk.StringVar()
        self.copy_catalogs: dict[str, AdCatalog] | None = None
        self.copy_catalogs_context: tuple[str, str, str, str, str, str] | None = None
        self.snap_export_window = tk.StringVar()
        self.snap_export_cid = tk.StringVar()
        self.snap_export_campaign = tk.StringVar()
        self.snap_export_dir = tk.StringVar(value=str(Path.home() / "Desktop"))
        self.snap_export_catalog: AdCatalog | None = None
        self.snap_export_catalog_context: tuple[str, str, str] | None = None
        self.snap_export_result: dict[str, Any] | None = None
        self.snap_export_summary = tk.StringVar(value="尚未导出")
        self.snap_import_path = tk.StringVar()
        self.snap_import_summary = tk.StringVar(value="尚未选择快照文件")
        self.snap_import_snapshot: dict[str, Any] | None = None
        self.snap_asset_override_dir = tk.StringVar()
        self.snap_initial_status_paused = tk.BooleanVar(value=True)
        self.snap_target_rows: list[dict[str, Any]] = []
        self.snap_target_labels: dict[str, str] = {}
        self.snap_batch_results: list[dict[str, Any]] = []
        self.snap_export_profile_by_label: dict[str, tuple[str, str]] = {}
        self.snap_export_campaign_by_label: dict[str, dict[str, Any]] = {}
        self.snap_target_items: dict[str, dict[str, str]] = {}
        self.snap_target_summary = tk.StringVar(value="尚未选择目标账户")
        self.spend_query_date = tk.StringVar(
            value=datetime.now().astimezone().date().isoformat()
        )
        self.spend_query_end_date = tk.StringVar(
            value=datetime.now().astimezone().date().isoformat()
        )
        self.spend_items: dict[str, dict[str, Any]] = {}
        self.placement_window = tk.StringVar()
        self.placement_cid = tk.StringVar()
        self.placement_query_date = tk.StringVar(
            value=datetime.now().astimezone().date().isoformat()
        )
        self.placement_selection_text = tk.StringVar(value="已选择 0 个网站展示位置")
        self.placement_category = tk.StringVar(value="游戏")
        self.placement_analysis_summary = tk.StringVar(value="尚未检测网站")
        self.placement_analysis_context: dict[str, Any] | None = None
        self.placement_items: dict[str, dict[str, Any]] = {}
        self.placement_nodes: dict[str, dict[str, Any]] = {}
        self.placement_query_context: dict[str, Any] | None = None
        self.campaign_run_today = tk.BooleanVar(value=True)
        self.campaign_white_budget_limit = tk.StringVar(value="2.00")
        self.campaign_white_name_prefix = tk.StringVar(value="App-1")
        self.campaign_policy_summary = tk.StringVar(value="尚未生成今日投放计划")
        self.campaign_policy_decisions: dict[str, CampaignDecision] = {}
        self.warmup_target_items: dict[str, dict[str, str]] = {}
        self.warmup_target_summary = tk.StringVar(value="已选择 0 个 CID")
        self.warmup_source_cid = tk.StringVar(value="")
        self.warmup_daily_budget = tk.StringVar(value=f"{DEFAULT_DAILY_BUDGET:.2f}")
        self.warmup_template_summary = tk.StringVar(value="固定模板：尚未保存")
        self.warmup_items: dict[str, dict[str, Any]] = {}
        self.appeal_items: dict[str, dict[str, Any]] = {}
        self.appeal_summary = tk.StringVar(value="封禁 CID 0 个 · 已选择 0 个")
        self.appeal_basis = tk.StringVar(value=APPEAL_BASIS_LABELS[APPEAL_BASIS_CORRECTED])
        appeal_automation = load_appeal_automation()
        self.appeal_basis.set(APPEAL_BASIS_LABELS.get(
            str(appeal_automation.get("basis") or APPEAL_BASIS_CORRECTED),
            APPEAL_BASIS_LABELS[APPEAL_BASIS_CORRECTED],
        ))
        self.appeal_run_mode = tk.StringVar(value=str(appeal_automation["mode"]))
        self.appeal_run_time = tk.StringVar(value=str(appeal_automation["time"]))
        self.appeal_auto_submit = tk.BooleanVar(
            value=bool(appeal_automation.get("auto_submit", True))
        )
        self.appeal_auto_status = tk.StringVar(value="正在读取自动任务状态…")
        self.schedule_time = tk.StringVar(value="09:00")
        self._build_ui()
        self._load_state()
        self._log("系统就绪：本地配置已加载，等待操作")
        self._refresh_library_tree()
        self._refresh_creative_library_tree()
        self._refresh_reports()
        self.after(180, self._place_workspace_sash)
        if not self._smoke_test:
            self.after(300, self._refresh_schedule_status)
            self.after(700, self._initialize_appeal_automation)
        else:
            # Exercise the packaged GUI/event loop without touching Task Scheduler,
            # AdsPower, or Google Ads. A bounded self-exit makes smoke tests
            # deterministic and leaves no child process behind.
            self.after(2500, self.destroy)
        self.after(100, self._poll_events)

    def _setup_theme(self) -> None:
        """Apply a consistent, high-contrast light theme to ttk and classic widgets."""
        self.configure(background=COLOR_BG)
        self.option_add("*Font", ("Microsoft YaHei UI", 9))
        self.option_add("*Text.background", COLOR_SURFACE_ALT)
        self.option_add("*Text.foreground", COLOR_TEXT)
        self.option_add("*Text.insertBackground", COLOR_TEXT)
        self.option_add("*Text.selectBackground", COLOR_PRIMARY)
        self.option_add("*Text.selectForeground", "#FFFFFF")
        self.option_add("*Text.relief", "flat")
        self.option_add("*Text.highlightThickness", 1)
        self.option_add("*Text.highlightBackground", COLOR_BORDER)
        self.option_add("*Text.highlightColor", COLOR_PRIMARY)
        self.option_add("*Listbox.background", COLOR_SURFACE)
        self.option_add("*Listbox.foreground", COLOR_TEXT)
        self.option_add("*Listbox.selectBackground", COLOR_PRIMARY)
        self.option_add("*Listbox.selectForeground", "#FFFFFF")
        self.option_add("*Listbox.relief", "flat")
        self.option_add("*Listbox.highlightThickness", 1)
        self.option_add("*Listbox.highlightBackground", COLOR_BORDER)
        self.option_add("*Listbox.highlightColor", COLOR_PRIMARY)
        self.option_add("*Menu.background", COLOR_SURFACE)
        self.option_add("*Menu.foreground", COLOR_TEXT)
        self.option_add("*Menu.activeBackground", COLOR_PRIMARY_SOFT)
        self.option_add("*Menu.activeForeground", COLOR_PRIMARY_HOVER)

        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", background=COLOR_BG, foreground=COLOR_TEXT)
        style.configure("TFrame", background=COLOR_SURFACE)
        style.configure("Surface.TFrame", background=COLOR_SURFACE)
        style.configure("Header.TFrame", background=COLOR_SURFACE)
        style.configure("Accent.TFrame", background=COLOR_PRIMARY)
        style.configure("AccentMid.TFrame", background="#5B4FD8")
        style.configure("AccentWrite.TFrame", background=COLOR_WRITE)
        style.configure("AccentSoft.TFrame", background=COLOR_PRIMARY_SOFT)
        style.configure("Bg.TFrame", background=COLOR_BG)
        style.configure("Card.TFrame", background=COLOR_SURFACE, bordercolor=COLOR_BORDER,
                        lightcolor=COLOR_BORDER, darkcolor=COLOR_BORDER,
                        relief="solid", borderwidth=1)
        style.configure("Console.TFrame", background=COLOR_CONSOLE_BG)
        style.configure("TLabel", background=COLOR_SURFACE, foreground=COLOR_TEXT)
        style.configure("BrandMark.TLabel", background=COLOR_PRIMARY, foreground="#FFFFFF",
                        font=("Microsoft YaHei UI", 16, "bold"), padding=(12, 7), anchor="center")
        style.configure("BrandLogo.TLabel", background=COLOR_SURFACE, padding=(2, 0))
        style.configure("HeaderTitle.TLabel", background=COLOR_SURFACE, foreground=COLOR_TEXT,
                        font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("HeaderMuted.TLabel", background=COLOR_SURFACE, foreground=COLOR_MUTED,
                        font=("Microsoft YaHei UI", 9))
        style.configure("Muted.TLabel", background=COLOR_SURFACE, foreground=COLOR_MUTED)
        style.configure("SectionTitle.TLabel", background=COLOR_SURFACE, foreground=COLOR_TEXT,
                        font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("ConsoleTitle.TLabel", background=COLOR_CONSOLE_BG,
                        foreground="#E5ECF7", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("ConsoleMuted.TLabel", background=COLOR_CONSOLE_BG,
                        foreground=COLOR_CONSOLE_MUTED, font=("Microsoft YaHei UI", 8))
        style.configure("Version.TLabel", background=COLOR_PRIMARY_SOFT, foreground=COLOR_PRIMARY_HOVER,
                        font=("Microsoft YaHei UI", 9, "bold"), padding=(10, 5))
        style.configure("Badge.TLabel", background=COLOR_SUCCESS_SOFT, foreground=COLOR_SUCCESS,
                        font=("Microsoft YaHei UI", 9, "bold"), padding=(10, 5))
        style.configure("TLabelframe", background=COLOR_SURFACE, bordercolor=COLOR_BORDER,
                        lightcolor=COLOR_BORDER, darkcolor=COLOR_BORDER, relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=COLOR_SURFACE, foreground=COLOR_TEXT,
                        font=("Microsoft YaHei UI", 9, "bold"))

        button = dict(font=("Microsoft YaHei UI", 9), padding=(13, 7), relief="flat", borderwidth=1)
        style.configure("TButton", **button, background=COLOR_SURFACE, foreground=COLOR_TEXT,
                        bordercolor=COLOR_BORDER, lightcolor=COLOR_BORDER, darkcolor=COLOR_BORDER,
                        focuscolor=COLOR_PRIMARY)
        style.map("TButton",
                  background=[("disabled", COLOR_SURFACE_ALT), ("pressed", "#D9E3F2"),
                              ("active", COLOR_PRIMARY_SOFT)],
                  foreground=[("disabled", COLOR_DISABLED), ("active", COLOR_PRIMARY_HOVER)],
                  bordercolor=[("focus", COLOR_PRIMARY), ("active", "#B4C6DE")],
                  lightcolor=[("focus", COLOR_PRIMARY), ("active", "#B4C6DE")],
                  darkcolor=[("focus", COLOR_PRIMARY), ("active", "#B4C6DE")])
        for name, background, hover in (
            ("Accent.TButton", COLOR_PRIMARY, COLOR_PRIMARY_HOVER),
            ("Write.TButton", COLOR_WRITE, COLOR_WRITE_HOVER),
            ("Success.TButton", COLOR_SUCCESS, "#166534"),
        ):
            style.configure(name, **button, background=background, foreground="#FFFFFF",
                            bordercolor=background, lightcolor=background, darkcolor=background)
            style.map(name,
                      background=[("disabled", "#CBD5E1"), ("pressed", hover), ("active", hover)],
                      foreground=[("disabled", "#F8FAFC"), ("active", "#FFFFFF")],
                      bordercolor=[("disabled", "#CBD5E1"), ("active", hover)])
        style.configure("Danger.TButton", **button, background=COLOR_DANGER_SOFT,
                        foreground=COLOR_DANGER, bordercolor="#F3C6C2",
                        lightcolor="#F3C6C2", darkcolor="#F3C6C2")
        style.map("Danger.TButton",
                  background=[("disabled", COLOR_SURFACE_ALT), ("pressed", COLOR_DANGER_HOVER),
                              ("active", COLOR_DANGER)],
                  foreground=[("disabled", COLOR_DISABLED), ("pressed", "#FFFFFF"),
                              ("active", "#FFFFFF")],
                  bordercolor=[("disabled", COLOR_BORDER), ("active", COLOR_DANGER)])

        style.configure("TNotebook", background=COLOR_BG, borderwidth=0, tabmargins=(2, 4, 2, 0))
        style.configure("TPanedwindow", background=COLOR_SURFACE, sashwidth=6)
        style.configure("TNotebook.Tab", background=COLOR_BG, foreground=COLOR_MUTED,
                        padding=(17, 10), font=("Microsoft YaHei UI", 9, "bold"), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", COLOR_SURFACE), ("active", "#E2E9F4")],
                  foreground=[("selected", COLOR_PRIMARY_HOVER), ("active", COLOR_TEXT)])

        style.configure("Treeview", background=COLOR_SURFACE, fieldbackground=COLOR_SURFACE,
                        foreground=COLOR_TEXT, bordercolor=COLOR_BORDER, lightcolor=COLOR_BORDER,
                        darkcolor=COLOR_BORDER, borderwidth=1, relief="solid", rowheight=32)
        style.configure("Creative.Treeview", rowheight=72)
        style.configure("Treeview.Heading", background=COLOR_HEADING, foreground="#3A4761",
                        font=("Microsoft YaHei UI", 9, "bold"), padding=(9, 9), relief="flat",
                        bordercolor=COLOR_BORDER, lightcolor=COLOR_BORDER, darkcolor=COLOR_BORDER)
        style.map("Treeview",
                  background=[("selected", COLOR_PRIMARY)], foreground=[("selected", "#FFFFFF")])
        style.map("Task.Treeview",
                  background=[("selected", COLOR_PRIMARY)], foreground=[("selected", "#FFFFFF")])
        style.map("Treeview.Heading", background=[("active", "#DFE8F5")])

        style.configure("TEntry", fieldbackground=COLOR_SURFACE, foreground=COLOR_TEXT,
                        bordercolor=COLOR_BORDER, lightcolor=COLOR_BORDER, darkcolor=COLOR_BORDER,
                        insertcolor=COLOR_TEXT, padding=6)
        style.map("TEntry", bordercolor=[("focus", COLOR_PRIMARY)],
                  fieldbackground=[("disabled", COLOR_SURFACE_ALT)],
                  foreground=[("disabled", COLOR_DISABLED)])
        style.configure("TCombobox", fieldbackground=COLOR_SURFACE, background=COLOR_SURFACE,
                        foreground=COLOR_TEXT, arrowcolor=COLOR_MUTED, bordercolor=COLOR_BORDER,
                        lightcolor=COLOR_BORDER, darkcolor=COLOR_BORDER, padding=6)
        style.map("TCombobox", bordercolor=[("focus", COLOR_PRIMARY)],
                  fieldbackground=[("readonly", COLOR_SURFACE), ("disabled", COLOR_SURFACE_ALT)],
                  foreground=[("readonly", COLOR_TEXT), ("disabled", COLOR_DISABLED)])
        for name, foreground, background in (
            ("CidAvailable.TCombobox", COLOR_SUCCESS, COLOR_SUCCESS_SOFT),
            ("CidUnavailable.TCombobox", COLOR_WARNING, COLOR_WARNING_SOFT),
            ("CidBanned.TCombobox", COLOR_DANGER, COLOR_DANGER_SOFT),
            ("CidUnknown.TCombobox", COLOR_MUTED, COLOR_SURFACE_ALT),
        ):
            style.configure(
                name,
                fieldbackground=background,
                background=background,
                foreground=foreground,
                arrowcolor=foreground,
                bordercolor=foreground,
                lightcolor=foreground,
                darkcolor=foreground,
                padding=5,
            )
            style.map(
                name,
                bordercolor=[("focus", foreground)],
                fieldbackground=[("readonly", background), ("disabled", COLOR_SURFACE_ALT)],
                foreground=[("readonly", foreground), ("disabled", COLOR_DISABLED)],
                arrowcolor=[("readonly", foreground), ("disabled", COLOR_DISABLED)],
            )
        style.configure("TCheckbutton", background=COLOR_SURFACE, foreground=COLOR_TEXT, padding=(2, 3))
        style.map("TCheckbutton", background=[("active", COLOR_SURFACE)],
                  indicatorcolor=[("selected", COLOR_PRIMARY), ("!selected", COLOR_SURFACE)],
                  foreground=[("disabled", COLOR_DISABLED)])
        style.configure("TSeparator", background=COLOR_BORDER)
        for orientation in ("Vertical", "Horizontal"):
            style.configure(
                f"{orientation}.TScrollbar", background="#CBD6E4", troughcolor=COLOR_SURFACE_ALT,
                bordercolor=COLOR_SURFACE_ALT, arrowcolor=COLOR_MUTED, relief="flat",
            )
            style.map(
                f"{orientation}.TScrollbar",
                background=[("pressed", "#9FB2CA"), ("active", "#AEBFD4")],
            )
        style.configure(
            "Running.Horizontal.TProgressbar", troughcolor=COLOR_PRIMARY_SOFT,
            background=COLOR_PRIMARY, bordercolor=COLOR_PRIMARY_SOFT,
            lightcolor=COLOR_PRIMARY, darkcolor=COLOR_PRIMARY,
        )
        style.configure(
            "Success.Horizontal.TProgressbar", troughcolor=COLOR_SUCCESS_SOFT,
            background=COLOR_SUCCESS, bordercolor=COLOR_SUCCESS_SOFT,
            lightcolor=COLOR_SUCCESS, darkcolor=COLOR_SUCCESS,
        )
        style.configure(
            "Error.Horizontal.TProgressbar", troughcolor=COLOR_DANGER_SOFT,
            background=COLOR_DANGER, bordercolor=COLOR_DANGER_SOFT,
            lightcolor=COLOR_DANGER, darkcolor=COLOR_DANGER,
        )
        style.configure(
            "TaskRunning.TLabel", background=COLOR_PRIMARY_SOFT,
            foreground=COLOR_PRIMARY_HOVER, font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.configure(
            "TaskSuccess.TLabel", background=COLOR_SUCCESS_SOFT,
            foreground=COLOR_SUCCESS, font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.configure(
            "TaskError.TLabel", background=COLOR_DANGER_SOFT,
            foreground=COLOR_DANGER, font=("Microsoft YaHei UI", 9, "bold"),
        )

    def _build_ui(self) -> None:
        # Slim brand accent strip: blue (read) fading into purple (write).
        strip = ttk.Frame(self, style="Accent.TFrame", height=4)
        strip.pack(fill="x")
        strip.pack_propagate(False)
        ttk.Frame(strip, style="AccentWrite.TFrame", width=200).pack(side="right", fill="y")
        ttk.Frame(strip, style="AccentMid.TFrame", width=140).pack(side="right", fill="y")

        top = ttk.Frame(self, padding=(20, 14, 20, 14), style="Header.TFrame")
        top.pack(fill="x")
        if self._app_icon_image is not None:
            self._header_icon_image = self._app_icon_image.subsample(22, 22)
            ttk.Label(
                top, image=self._header_icon_image, style="BrandLogo.TLabel"
            ).pack(side="left")
        else:
            ttk.Label(top, text="G", style="BrandMark.TLabel", width=2).pack(side="left")
        brand = ttk.Frame(top, style="Header.TFrame")
        brand.pack(side="left", padx=(12, 0))
        ttk.Label(brand, text="GGAds Protocol Workspace", style="HeaderTitle.TLabel").pack(anchor="w")
        ttk.Label(
            brand, text="比特浏览器 / AdsPower × Google Ads · 多窗口、多账户协议工作台",
            style="HeaderMuted.TLabel",
        ).pack(anchor="w", pady=(3, 0))
        ttk.Button(
            top, text="使用帮助", command=lambda: self.notebook.select(self.help_tab)
        ).pack(side="right", padx=(10, 0))
        ttk.Label(top, text=f"v{__version__}", style="Version.TLabel").pack(side="right", padx=(10, 0))
        ttk.Label(top, text="●  协议直连", style="Badge.TLabel").pack(side="right")
        ttk.Separator(self, orient="horizontal").pack(fill="x")

        status_wrap = ttk.Frame(self, style="Bg.TFrame", padding=(18, 10, 18, 10))
        status_wrap.pack(fill="x")
        self.task_status_frame = ttk.Frame(
            status_wrap, padding=(14, 10, 14, 10), style="Card.TFrame"
        )
        self.task_status_frame.pack(fill="x")
        self.task_status_frame.columnconfigure(1, weight=1)
        self.task_state_text = tk.StringVar(value="● 就绪")
        self.task_step_text = tk.StringVar(value="等待操作")
        self.task_elapsed_text = tk.StringVar(value="")
        self.task_state_label = ttk.Label(
            self.task_status_frame, textvariable=self.task_state_text,
            style="TaskSuccess.TLabel", padding=(10, 6), width=34,
        )
        self.task_state_label.grid(row=0, column=0, sticky="w")
        self.task_step_label = ttk.Label(
            self.task_status_frame, textvariable=self.task_step_text,
            style="Muted.TLabel", anchor="w",
        )
        self.task_step_label.grid(row=0, column=1, sticky="ew", padx=(12, 10))
        self.task_elapsed_label = ttk.Label(
            self.task_status_frame, textvariable=self.task_elapsed_text,
            style="Muted.TLabel", anchor="e", width=14,
        )
        self.task_elapsed_label.grid(row=0, column=2, sticky="e")
        self.task_progress = ttk.Progressbar(
            self.task_status_frame, mode="determinate", value=0,
            style="Running.Horizontal.TProgressbar",
        )
        self.task_progress.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.active_task_label = ""
        self.active_task_started = 0.0
        self._task_tick_after: str | None = None

        self.workspace_split = ttk.Panedwindow(self, orient="horizontal")
        self.workspace_split.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        workspace = ttk.Frame(self.workspace_split)
        self.workspace_split.add(workspace, weight=5)
        self.notebook = ttk.Notebook(workspace)
        self.notebook.pack(fill="both", expand=True)
        self.batch_tab = ttk.Frame(self.notebook, padding=14)
        self.spend_tab = ttk.Frame(self.notebook, padding=14)
        self.campaign_tab = ttk.Frame(self.notebook, padding=14)
        self.placement_tab = ttk.Frame(self.notebook, padding=14)
        self.local_tab = ttk.Frame(self.notebook, padding=0)
        self.local_notebook = ttk.Notebook(self.local_tab)
        self.local_notebook.pack(fill="both", expand=True)
        self.library_tab = ttk.Frame(self.local_notebook, padding=14)
        self.creative_tab = ttk.Frame(self.local_notebook, padding=14)
        self.schedule_tab = ttk.Frame(self.local_notebook, padding=14)
        self.remote_tab = ttk.Frame(self.notebook, padding=14)
        self.warmup_tab = ttk.Frame(self.notebook, padding=14)
        self.appeal_tab = ttk.Frame(self.notebook, padding=14)
        self.ad_tab = ttk.Frame(self.notebook, padding=14)
        self.report_tab = ttk.Frame(self.notebook, padding=14)
        self.help_tab = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(self.batch_tab, text="批量任务")
        self.notebook.add(self.spend_tab, text="每日消耗")
        self.notebook.add(self.campaign_tab, text="系列管控")
        self.notebook.add(self.placement_tab, text="展示位置")
        self.notebook.add(self.local_tab, text="本地配置")
        self.notebook.add(self.remote_tab, text="Google Ads 数据")
        self.notebook.add(self.warmup_tab, text="一键白量")
        self.notebook.add(self.appeal_tab, text="账户申诉")
        self.notebook.add(self.ad_tab, text="广告创建")
        self.ga4_tab = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(self.ga4_tab, text="GA4")
        self.notebook.add(self.report_tab, text="运行记录")
        self.notebook.add(self.help_tab, text="使用帮助")
        self.local_notebook.add(self.library_tab, text="本地内容库")
        self.local_notebook.add(self.creative_tab, text="广告素材库")
        self.local_notebook.add(self.schedule_tab, text="定时启动")
        self._build_batch_tab()
        self._build_spend_tab()
        self._build_campaign_tab()
        self._build_placement_tab()
        self._build_library_tab()
        self._build_creative_library_tab()
        self._build_remote_tab()
        self._build_warmup_tab()
        self._build_appeal_tab()
        self._build_ad_tab()
        self._build_ga4_tab()
        self._build_schedule_tab()
        self._build_report_tab()
        self._build_help_tab()

        log_frame = ttk.Frame(self.workspace_split, style="Console.TFrame", padding=(12, 10, 12, 10))
        self.workspace_split.add(log_frame, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        log_header = ttk.Frame(log_frame, style="Console.TFrame")
        log_header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(log_header, text="● 实时活动流", style="ConsoleTitle.TLabel").pack(side="left")
        ttk.Label(
            log_header, text="协议步骤 · 结果与失败原因", style="ConsoleMuted.TLabel"
        ).pack(side="right")
        self.log_box = tk.Text(
            log_frame,
            width=38,
            height=1,
            state="disabled",
            wrap="word",
            background=COLOR_CONSOLE_BG,
            foreground=COLOR_CONSOLE_TEXT,
            insertbackground=COLOR_CONSOLE_TEXT,
            selectbackground=COLOR_PRIMARY,
            selectforeground="#FFFFFF",
            highlightthickness=1,
            highlightbackground=COLOR_CONSOLE_PANEL,
            highlightcolor=COLOR_PRIMARY,
            relief="flat",
            font=("Microsoft YaHei UI", 9),
            padx=10,
            pady=8,
        )
        self.log_box.grid(row=1, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_box.yview)
        log_scroll.grid(row=1, column=1, sticky="ns")
        self.log_box.configure(yscrollcommand=log_scroll.set)
        log_bold = ("Microsoft YaHei UI", 9, "bold")
        self.log_box.tag_configure("log_time", foreground=COLOR_CONSOLE_MUTED)
        self.log_box.tag_configure("log_section", foreground="#7EA6FF", font=log_bold)
        self.log_box.tag_configure("log_error", foreground="#FF8A80", background="#3A1B1B", font=log_bold)
        self.log_box.tag_configure("log_warning", foreground="#F5C26B", background="#3A2E14")
        self.log_box.tag_configure("log_success", foreground="#5BD98A", font=log_bold)
        self.log_box.tag_configure("log_read", foreground="#6EA8FE")
        self.log_box.tag_configure("log_write", foreground="#B794F6")
        self.log_box.tag_configure("log_normal", foreground=COLOR_CONSOLE_TEXT)

    def _place_workspace_sash(self) -> None:
        width = self.workspace_split.winfo_width()
        if width > 900:
            log_width = 300 if width < 1400 else 350
            self.workspace_split.sashpos(0, width - log_width)

    def _build_batch_tab(self) -> None:
        tab = self.batch_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        title_row = ttk.Frame(bar)
        title_row.pack(fill="x")
        ttk.Label(title_row, text="账户任务总览", style="SectionTitle.TLabel").pack(side="left")
        ttk.Label(
            title_row, text="浏览器窗口  →  MCC  →  Google Ads CID",
            style="Muted.TLabel",
        ).pack(side="left", padx=(12, 0))
        self.global_account_summary = tk.StringVar(value="全局账户数据源：尚未查询")
        ttk.Label(
            title_row, textvariable=self.global_account_summary, style="Muted.TLabel",
        ).pack(side="left", padx=(18, 0))
        self.status_button = ttk.Button(
            title_row,
            text="刷新账户列表与状态",
            command=self._refresh_account_statuses,
            style="Accent.TButton",
        )
        self.status_button.pack(side="right")
        self.balance_button = ttk.Button(
            title_row,
            text="读取 CID 余额",
            command=self._refresh_account_balances,
        )
        self.balance_button.pack(side="right", padx=(0, 8))
        self.show_task_details = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            title_row,
            text="详细字段",
            variable=self.show_task_details,
            command=self._toggle_task_columns,
        ).pack(side="right", padx=(8, 12))

        crud_row = ttk.Frame(bar)
        crud_row.pack(fill="x", pady=(10, 0))
        ttk.Label(crud_row, text="浏览器窗口", style="Muted.TLabel").pack(side="left")
        ttk.Button(
            crud_row,
            text="导入比特浏览器窗口",
            command=self._import_bitbrowser_windows,
            style="Write.TButton",
        ).pack(side="left", padx=(10, 4))
        ttk.Button(
            crud_row,
            text="比特 Local API 设置",
            command=self._open_bitbrowser_settings,
        ).pack(side="left", padx=4)
        compatibility = ttk.Menubutton(crud_row, text="兼容操作 ▾")
        compatibility_menu = tk.Menu(compatibility, tearoff=False)
        compatibility_menu.add_command(
            label="手工添加窗口（默认比特浏览器）", command=self._add_window
        )
        compatibility_menu.add_command(
            label="添加 AdsPower 窗口", command=self._add_adspower_window
        )
        compatibility_menu.add_separator()
        compatibility_menu.add_command(label="编辑选中窗口", command=self._edit_window)
        compatibility_menu.add_command(label="删除选中窗口", command=self._delete_window)
        compatibility_menu.add_command(
            label="读取 AdsPower 窗口编号", command=self._refresh_window_metadata
        )
        compatibility.configure(menu=compatibility_menu)
        compatibility.pack(side="left", padx=4)
        self.window_metadata_button = compatibility
        ttk.Separator(crud_row, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Label(crud_row, text="Google Ads CID", style="Muted.TLabel").pack(side="left")
        ttk.Button(crud_row, text="＋ 添加", command=self._add_cid, width=7).pack(side="left", padx=(10, 4))
        ttk.Button(crud_row, text="编辑", command=self._edit_cid, width=6).pack(side="left", padx=4)
        ttk.Button(
            crud_row, text="删除", command=self._delete_cid, style="Danger.TButton", width=6
        ).pack(side="left", padx=4)

        tree_frame = ttk.Frame(tab)
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.task_tree = ttk.Treeview(
            tree_frame,
            columns=(
                "browser_type", "running_status", "profile", "window_number", "window_name",
                "account_type", "account_name", "cid", "cid_display", "ocid",
                "parent_mcc", "parent_mcc_display",
                "depth", "dictionary", "status", "balance", "balance_type", "balance_status",
                "balance_detail", "balance_updated_at", "daily_spend", "daily_spend_status",
                "daily_spend_detail", "daily_spend_date", "daily_spend_updated_at",
                "status_detail", "note", "created_at", "updated_at",
            ),
            displaycolumns=(
                "browser_type", "running_status", "account_name", "cid_display",
                "status", "balance", "daily_spend", "dictionary", "note",
            ),
            show="tree headings",
            selectmode="extended",
            style="Task.Treeview",
        )
        self.task_tree.heading("#0", text="账户层级")
        self.task_tree.heading("browser_type", text="浏览器")
        self.task_tree.heading("running_status", text="窗口状态")
        self.task_tree.heading("profile", text="原始窗口 ID")
        self.task_tree.heading("window_number", text="窗口编号")
        self.task_tree.heading("window_name", text="窗口名称")
        self.task_tree.heading("account_type", text="账户类型")
        self.task_tree.heading("account_name", text="Google Ads 账户名称")
        self.task_tree.heading("cid", text="CID（内部值）")
        self.task_tree.heading("cid_display", text="Google Ads CID")
        self.task_tree.heading("ocid", text="内部账户 ID")
        self.task_tree.heading("parent_mcc", text="父 MCC（内部值）")
        self.task_tree.heading("parent_mcc_display", text="父 MCC")
        self.task_tree.heading("depth", text="层级")
        self.task_tree.heading("dictionary", text="引用内容方案")
        self.task_tree.heading("status", text="CID 状态")
        self.task_tree.heading("balance", text="账户余额")
        self.task_tree.heading("balance_type", text="余额类型")
        self.task_tree.heading("balance_status", text="余额读取状态")
        self.task_tree.heading("balance_detail", text="余额说明")
        self.task_tree.heading("balance_updated_at", text="余额更新时间")
        self.task_tree.heading("daily_spend", text="日期消耗")
        self.task_tree.heading("daily_spend_status", text="消耗读取状态")
        self.task_tree.heading("daily_spend_detail", text="消耗说明")
        self.task_tree.heading("daily_spend_date", text="消耗日期")
        self.task_tree.heading("daily_spend_updated_at", text="消耗更新时间")
        self.task_tree.heading("status_detail", text="状态说明")
        self.task_tree.heading("note", text="备注")
        self.task_tree.heading("created_at", text="新建时间")
        self.task_tree.heading("updated_at", text="更新时间")
        self.task_tree.column("#0", width=230, minwidth=180)
        self.task_tree.column("browser_type", width=90, minwidth=80, anchor="center")
        self.task_tree.column("running_status", width=90, minwidth=80, anchor="center")
        self.task_tree.column("profile", width=180, minwidth=130)
        self.task_tree.column("window_number", width=130, minwidth=105, anchor="center")
        self.task_tree.column("window_name", width=180, minwidth=120)
        self.task_tree.column("account_type", width=85, minwidth=75, anchor="center")
        self.task_tree.column("account_name", width=300, minwidth=180)
        self.task_tree.column("cid", width=0, minwidth=0, stretch=False)
        self.task_tree.column("cid_display", width=145, minwidth=120, anchor="center")
        self.task_tree.column("ocid", width=135, minwidth=110, anchor="center")
        self.task_tree.column("parent_mcc", width=0, minwidth=0, stretch=False)
        self.task_tree.column("parent_mcc_display", width=175, minwidth=130, anchor="center")
        self.task_tree.column("depth", width=60, minwidth=50, anchor="center")
        self.task_tree.column("dictionary", width=150, minwidth=120)
        self.task_tree.column("status", width=125, minwidth=110, anchor="center")
        self.task_tree.column("balance", width=125, minwidth=105, anchor="e")
        self.task_tree.column("balance_type", width=175, minwidth=130)
        self.task_tree.column("balance_status", width=105, minwidth=90, anchor="center")
        self.task_tree.column("balance_detail", width=420, minwidth=220)
        self.task_tree.column("balance_updated_at", width=155, minwidth=145, anchor="center")
        self.task_tree.column("daily_spend", width=120, minwidth=100, anchor="e")
        self.task_tree.column("daily_spend_status", width=105, minwidth=90, anchor="center")
        self.task_tree.column("daily_spend_detail", width=330, minwidth=180)
        self.task_tree.column("daily_spend_date", width=105, minwidth=95, anchor="center")
        self.task_tree.column("daily_spend_updated_at", width=155, minwidth=145, anchor="center")
        self.task_tree.column("status_detail", width=380, minwidth=180)
        self.task_tree.column("note", width=230, minwidth=140)
        self.task_tree.column("created_at", width=155, minwidth=145, anchor="center")
        self.task_tree.column("updated_at", width=155, minwidth=145, anchor="center")
        self.task_tree.tag_configure("available", foreground=COLOR_SUCCESS, background=COLOR_SUCCESS_SOFT)
        self.task_tree.tag_configure("unavailable", foreground=COLOR_WARNING, background=COLOR_WARNING_SOFT)
        self.task_tree.tag_configure("banned", foreground=COLOR_DANGER, background=COLOR_DANGER_SOFT)
        self.task_tree.tag_configure("unknown", foreground=COLOR_MUTED, background=COLOR_SURFACE_ALT)
        self.task_tree.tag_configure(
            "manager",
            foreground=COLOR_MANAGER,
            background=COLOR_MANAGER_SOFT,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.task_tree.grid(row=0, column=0, sticky="nsew")
        task_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.task_tree.yview)
        task_y.grid(row=0, column=1, sticky="ns")
        task_x = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.task_tree.xview)
        task_x.grid(row=1, column=0, sticky="ew")
        self.task_tree.configure(yscrollcommand=task_y.set, xscrollcommand=task_x.set)
        self.task_tree.bind("<Button-3>", self._show_task_context_menu)
        self.task_tree.bind("<Double-1>", self._task_double_click)
        self.task_tree.bind("<<TreeviewSelect>>", self._task_selection_changed)
        self.task_context_menu = tk.Menu(self, tearoff=False)

        options = ttk.LabelFrame(tab, text="执行对象", padding=8)
        options.grid(row=2, column=0, sticky="ew", pady=10)
        self.enabled: dict[str, tk.BooleanVar] = {}
        self.enabled_labels: dict[str, tk.StringVar] = {}
        for key, label in TYPE_LABELS.items():
            variable = tk.BooleanVar(value=True)
            display = tk.StringVar()
            self.enabled[key] = variable
            self.enabled_labels[key] = display
            toggle = tk.Checkbutton(
                options,
                textvariable=display,
                variable=variable,
                onvalue=True,
                offvalue=False,
                indicatoron=False,
                background=COLOR_SURFACE,
                foreground=COLOR_TEXT,
                selectcolor=COLOR_SUCCESS_SOFT,
                activebackground=COLOR_PRIMARY_SOFT,
                activeforeground=COLOR_PRIMARY_HOVER,
                disabledforeground=COLOR_DISABLED,
                font=("Microsoft YaHei UI", 9),
                relief="flat",
                overrelief="solid",
                borderwidth=1,
                highlightthickness=0,
                padx=8,
                pady=4,
                cursor="hand2",
            )

            def sync_toggle(
                *_args: Any,
                current_key: str = key,
                current_label: str = label,
                current_value: tk.BooleanVar = variable,
                current_display: tk.StringVar = display,
                current_toggle: tk.Checkbutton = toggle,
            ) -> None:
                selected = bool(current_value.get())
                current_display.set(
                    execution_object_label(current_key, current_label, selected)
                )
                current_toggle.configure(
                    foreground=COLOR_SUCCESS if selected else COLOR_TEXT,
                )

            variable.trace_add("write", sync_toggle)
            sync_toggle()
            toggle.pack(side="left", padx=(0, 14))

        buttons = ttk.Frame(tab)
        buttons.grid(row=3, column=0, sticky="ew")
        self.preflight_button = ttk.Button(
            buttons,
            text="预检选中 CID（不修改 Google Ads）",
            command=lambda: self._start_batch(True),
            style="Accent.TButton",
        )
        self.preflight_button.pack(side="left")
        self.execute_button = ttk.Button(
            buttons, text="执行选中 CID", command=lambda: self._start_batch(False), style="Write.TButton"
        )
        self.execute_button.pack(side="left", padx=8)
        ttk.Label(
            buttons,
            text="Ctrl/Shift 可多选；只处理蓝色高亮的普通 CID",
            foreground=COLOR_MUTED,
        ).pack(side="right")

    def _build_spend_tab(self) -> None:
        tab = self.spend_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        ttk.Label(
            tab,
            text=(
                "按开始/结束日期范围或全部历史汇总每个普通 CID 的广告系列消耗。"
                "上方列表显示账户合计，下方显示所选 CID 的逐系列明细；全部为只读协议。"
            ),
            foreground=COLOR_MUTED,
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        bar = ttk.Frame(tab)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(bar, text="开始日期").pack(side="left")
        self.spend_date_entry = ttk.Entry(bar, textvariable=self.spend_query_date, width=12)
        self.spend_date_entry.pack(side="left", padx=(6, 4))
        ttk.Label(bar, text="结束日期").pack(side="left", padx=(8, 0))
        self.spend_end_date_entry = ttk.Entry(
            bar, textvariable=self.spend_query_end_date, width=12,
        )
        self.spend_end_date_entry.pack(side="left", padx=(6, 4))
        ttk.Button(bar, text="今天", command=lambda: self._set_spend_date_offset(0)).pack(side="left", padx=3)
        ttk.Button(bar, text="昨天", command=lambda: self._set_spend_date_offset(-1)).pack(side="left", padx=3)
        ttk.Button(bar, text="全部", command=self._set_spend_date_all).pack(side="left", padx=3)
        self.spend_button = ttk.Button(
            bar, text="查询全部 CID", command=self._refresh_account_daily_spends,
            style="Accent.TButton",
        )
        self.spend_button.pack(side="left", padx=(14, 5))
        self.spend_selected_button = ttk.Button(
            bar, text="查询选中 CID", command=self._refresh_selected_spend_rows
        )
        self.spend_selected_button.pack(side="left", padx=5)
        ttk.Label(
            bar, text="格式：YYYY-MM-DD 或 全部；开始 ≤ 结束 ≤ 今天", foreground=COLOR_MUTED
        ).pack(side="right")

        split = ttk.Panedwindow(tab, orient="vertical")
        split.grid(row=2, column=0, sticky="nsew")
        upper = ttk.Frame(split)
        lower = ttk.LabelFrame(split, text="所选 CID 的广告系列消耗明细", padding=6)
        split.add(upper, weight=3)
        split.add(lower, weight=2)
        upper.columnconfigure(0, weight=1)
        upper.rowconfigure(0, weight=1)
        lower.columnconfigure(0, weight=1)
        lower.rowconfigure(0, weight=1)

        columns = (
            "window_number", "profile", "cid", "account_name", "account_status",
            "date", "spend", "currency", "campaign_count", "read_status", "updated_at", "detail",
        )
        self.spend_tree = ttk.Treeview(
            upper, columns=columns, show="headings", selectmode="extended"
        )
        headings = {
            "window_number": "窗口编号", "profile": "指纹浏览器窗口 ID",
            "cid": "Google Ads CID", "account_name": "账户名称",
            "account_status": "CID 状态", "date": "消耗日期", "spend": "日期消耗",
            "currency": "币种", "campaign_count": "系列数", "read_status": "读取状态",
            "updated_at": "更新时间", "detail": "说明 / 失败原因",
        }
        widths = {
            "window_number": 90, "profile": 135, "cid": 125, "account_name": 250,
            "account_status": 85, "date": 105, "spend": 110, "currency": 65,
            "campaign_count": 65, "read_status": 90, "updated_at": 155, "detail": 360,
        }
        for key in columns:
            self.spend_tree.heading(key, text=headings[key])
            self.spend_tree.column(
                key, width=widths[key], minwidth=min(widths[key], 80),
                anchor="center" if key in {
                    "window_number", "cid", "account_status", "date", "spend", "currency",
                    "campaign_count", "read_status", "updated_at",
                } else "w",
            )
        self.spend_tree.tag_configure("ok", foreground=COLOR_SUCCESS, background=COLOR_SUCCESS_SOFT)
        self.spend_tree.tag_configure("zero", foreground=COLOR_MUTED, background=COLOR_SURFACE_ALT)
        self.spend_tree.tag_configure("failed", foreground=COLOR_DANGER, background=COLOR_DANGER_SOFT)
        self.spend_tree.grid(row=0, column=0, sticky="nsew")
        spend_y = ttk.Scrollbar(upper, orient="vertical", command=self.spend_tree.yview)
        spend_y.grid(row=0, column=1, sticky="ns")
        spend_x = ttk.Scrollbar(upper, orient="horizontal", command=self.spend_tree.xview)
        spend_x.grid(row=1, column=0, sticky="ew")
        self.spend_tree.configure(yscrollcommand=spend_y.set, xscrollcommand=spend_x.set)
        self.spend_tree.bind("<<TreeviewSelect>>", lambda _event: self._show_spend_detail())

        self.spend_detail = tk.Text(
            lower, wrap="none", state="disabled", font=("Consolas", 10), height=10
        )
        self.spend_detail.grid(row=0, column=0, sticky="nsew")
        detail_y = ttk.Scrollbar(lower, orient="vertical", command=self.spend_detail.yview)
        detail_y.grid(row=0, column=1, sticky="ns")
        detail_x = ttk.Scrollbar(lower, orient="horizontal", command=self.spend_detail.xview)
        detail_x.grid(row=1, column=0, sticky="ew")
        self.spend_detail.configure(yscrollcommand=detail_y.set, xscrollcommand=detail_x.set)

    # ------------------------------------------------------------ 系列管控
    def _build_campaign_tab(self) -> None:
        tab = self.campaign_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)
        ttk.Label(
            tab,
            text=(
                "按 CID 读取广告系列（状态/预算/出价类型），并批量暂停/启用。"
                "选中账户行 = 操作其全部可改系列；选中系列行 = 只操作选中的系列。"
                "已移除的系列自动跳过；每次改写后逐个读回校验。"
            ),
            foreground=COLOR_MUTED,
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        bar = ttk.Frame(tab)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(
            bar, text="读取全部 CID 系列", style="Accent.TButton",
            command=lambda: self._campaign_load(False),
        ).pack(side="left")
        ttk.Button(
            bar, text="读取选中 CID",
            command=lambda: self._campaign_load(True),
        ).pack(side="left", padx=6)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Button(
            bar, text="暂停选中", style="Danger.TButton",
            command=lambda: self._campaign_set_status(CAMPAIGN_STATUS_PAUSED),
        ).pack(side="left")
        ttk.Button(
            bar, text="启用选中",
            command=lambda: self._campaign_set_status(CAMPAIGN_STATUS_ENABLED),
        ).pack(side="left", padx=6)
        ttk.Label(
            bar, text="暂停=止损；启用=恢复投放。状态实时读回。", foreground=COLOR_MUTED,
        ).pack(side="right")

        policy_bar = ttk.LabelFrame(tab, text="今日投放总开关（先生成计划，不自动写入）", padding=7)
        policy_bar.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Checkbutton(
            policy_bar, text="今天跑广告", variable=self.campaign_run_today,
        ).pack(side="left")
        ttk.Label(policy_bar, text="白量名称前缀").pack(side="left", padx=(16, 5))
        ttk.Entry(
            policy_bar, textvariable=self.campaign_white_name_prefix, width=13,
        ).pack(side="left")
        ttk.Label(policy_bar, text="白量预算上限").pack(side="left", padx=(12, 5))
        ttk.Entry(
            policy_bar, textvariable=self.campaign_white_budget_limit, width=8,
            justify="right",
        ).pack(side="left")
        ttk.Button(
            policy_bar, text="生成今日计划（只读）",
            command=self._campaign_policy_preflight, style="Accent.TButton",
        ).pack(side="left", padx=(16, 5))
        ttk.Button(
            policy_bar, text="执行计划中的暂停",
            command=self._campaign_policy_execute, style="Danger.TButton",
        ).pack(side="left", padx=5)
        ttk.Label(
            policy_bar, textvariable=self.campaign_policy_summary, style="Muted.TLabel",
        ).pack(side="right")

        tree_frame = ttk.Frame(tab)
        tree_frame.grid(row=3, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        columns = (
            "cid", "name", "status", "budget", "bid", "policy_action",
            "policy_reason", "campaign_id",
        )
        self.campaign_tree = ttk.Treeview(
            tree_frame, columns=columns, show="tree headings", selectmode="extended",
        )
        self.campaign_tree.heading("#0", text="账户 / 系列")
        self.campaign_tree.heading("cid", text="CID")
        self.campaign_tree.heading("name", text="系列名称")
        self.campaign_tree.heading("status", text="状态")
        self.campaign_tree.heading("budget", text="日预算")
        self.campaign_tree.heading("bid", text="出价类型")
        self.campaign_tree.heading("policy_action", text="今日计划")
        self.campaign_tree.heading("policy_reason", text="计划说明")
        self.campaign_tree.heading("campaign_id", text="系列 ID")
        self.campaign_tree.column("#0", width=200, anchor="w")
        self.campaign_tree.column("cid", width=110, anchor="w")
        self.campaign_tree.column("name", width=220, anchor="w")
        self.campaign_tree.column("status", width=80, anchor="center")
        self.campaign_tree.column("budget", width=90, anchor="e")
        self.campaign_tree.column("bid", width=110, anchor="w")
        self.campaign_tree.column("policy_action", width=105, anchor="center")
        self.campaign_tree.column("policy_reason", width=330, anchor="w")
        self.campaign_tree.column("campaign_id", width=120, anchor="w")
        self.campaign_tree.tag_configure("enabled", foreground=COLOR_SUCCESS)
        self.campaign_tree.tag_configure("paused", foreground=COLOR_WARNING)
        self.campaign_tree.tag_configure("removed", foreground=COLOR_MUTED)
        self.campaign_tree.tag_configure(
            "policy_pause", foreground=COLOR_DANGER, background=COLOR_DANGER_SOFT,
        )
        self.campaign_tree.tag_configure(
            "policy_allow", foreground=COLOR_SUCCESS, background=COLOR_SUCCESS_SOFT,
        )
        scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.campaign_tree.yview)
        scroll_x = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.campaign_tree.xview)
        self.campaign_tree.configure(
            yscrollcommand=scroll.set, xscrollcommand=scroll_x.set,
        )
        self.campaign_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        self.campaign_items: dict[str, dict[str, Any]] = {}

    def _campaign_wanted(self, selected_only: bool) -> dict[str, set[str]] | None:
        """选中批量任务树的 CID → {window_key: {cid}}；未选中返回 None。"""
        if not selected_only:
            return None
        wanted: dict[str, set[str]] = {}
        for iid in self.task_tree.selection():
            cid = str(self.task_tree.set(iid, "cid") or "")
            window_iid = self._window_for_item(iid)
            profile = str(self.task_tree.set(window_iid, "profile") or "")
            if cid and profile and self.task_tree.parent(iid):
                key = window_key(
                    self.task_tree.set(window_iid, "browser_type"), profile,
                )
                wanted.setdefault(key, set()).add(cid)
        if not wanted:
            messagebox.showinfo("没有选中 CID", "请先在“批量任务”账户树中选中 CID 行", parent=self)
        return wanted or None

    def _campaign_load(self, selected_only: bool) -> None:
        wanted = self._campaign_wanted(selected_only)
        if selected_only and wanted is None:
            return
        identities = (
            [split_window_key(key) for key in wanted]
            if wanted else self._window_identities()
        )
        if not identities:
            messagebox.showinfo("没有窗口", "请先在“批量任务”配置指纹浏览器窗口", parent=self)
            return

        def task() -> dict[str, dict[str, Any]]:
            merged: dict[str, dict[str, Any]] = {}
            for browser_type, profile in identities:
                profile_key = window_key(browser_type, profile)
                runner = ProtocolRunner(
                    profile, log=self._log, browser_type=browser_type,
                )
                try:
                    result = runner.campaigns_overview(
                        {str(c) for c in wanted[profile_key]} if wanted else None
                    )
                    for cid, entry in result.items():
                        merged[f"{profile_key}:{cid}"] = {
                            **entry,
                            "browser_type": browser_type,
                            "profile_id": profile,
                        }
                finally:
                    runner.close()
            return merged

        self._run_async("读取广告系列", task, self._campaign_show)

    def _campaign_show(self, results: dict[str, dict[str, Any]]) -> None:
        self.campaign_tree.delete(*self.campaign_tree.get_children(""))
        self.campaign_items.clear()
        self.campaign_policy_decisions.clear()
        self.campaign_policy_summary.set("尚未生成今日投放计划")
        for key in sorted(results):
            entry = results[key]
            cid = entry["cid"]
            browser_type = entry["browser_type"]
            profile = entry["profile_id"]
            camps = entry.get("campaigns") or []
            error = entry.get("error")
            meta = self._task_account_metadata(browser_type, profile, cid)
            parent = key
            self.campaign_items[parent] = {
                "browser_type": browser_type,
                "profile_id": profile,
                "cid": cid,
                "campaigns": camps,
            }
            status_text = f"{len(camps)} 个系列" + (f" · {error}" if error else "")
            self.campaign_tree.insert(
                "", "end", iid=parent, open=True,
                text=f"{meta['account_name'] or cid}",
                values=(format_cid(cid), "", status_text, "", "", "", "", ""),
            )
            for camp in camps:
                iid = f"{key}:{camp['campaign_id']}"
                self.campaign_items[iid] = {
                    "browser_type": browser_type,
                    "profile_id": profile,
                    "cid": cid,
                    **camp,
                }
                tag = (
                    "enabled" if camp["status"] == CAMPAIGN_STATUS_ENABLED
                    else "paused" if camp["status"] == CAMPAIGN_STATUS_PAUSED
                    else "removed"
                )
                self.campaign_tree.insert(
                    parent, "end", iid=iid, tags=(tag,),
                    text="",
                    values=(
                        "", camp["name"], camp["status_label"],
                        camp["budget"], camp["bid_label"], "", "", camp["campaign_id"],
                    ),
                )

    @staticmethod
    def _campaign_policy_action_label(action: CampaignAction) -> str:
        return {
            CampaignAction.KEEP: "保持",
            CampaignAction.PAUSE: "计划暂停",
            CampaignAction.ALLOW_LOW_BUDGET_WHITE: "保留低预算白量",
            CampaignAction.REVIEW_THEME_CONVERSIONS: "检查主题转化",
            CampaignAction.RECOMMEND_PAUSE: "建议人工暂停",
        }[action]

    def _campaign_policy_preflight(self) -> None:
        campaign_rows = [
            (iid, item) for iid, item in self.campaign_items.items()
            if item.get("campaign_id")
            and int(item.get("status") or 0) != 7
        ]
        if not campaign_rows:
            messagebox.showinfo(
                "没有广告系列", "请先点击“读取全部 CID 系列”或“读取选中 CID”。",
                parent=self,
            )
            return
        try:
            white_limit = Decimal(self.campaign_white_budget_limit.get().strip())
            if not white_limit.is_finite() or white_limit < 0:
                raise ValueError
        except Exception:
            messagebox.showerror("白量预算无效", "白量预算上限必须是大于或等于 0 的数字。")
            return
        prefix = self.campaign_white_name_prefix.get().strip().casefold()
        self._save_state()
        facts = []
        for iid, item in campaign_rows:
            campaign_id = str(item["campaign_id"])
            name = str(item.get("name") or "")
            facts.append(CampaignFacts(
                campaign_id=campaign_id,
                enabled=int(item.get("status") or 0) == CAMPAIGN_STATUS_ENABLED,
                daily_budget=str(item.get("budget") or "0"),
                is_white_volume=bool(prefix and name.casefold().startswith(prefix)),
            ))
        decisions = evaluate_daily_switch(
            facts,
            run_today=bool(self.campaign_run_today.get()),
            white_daily_budget_limit=white_limit,
        )
        self.campaign_policy_decisions = {}
        pause_count = 0
        allow_count = 0
        for (iid, _item), decision in zip(campaign_rows, decisions):
            self.campaign_policy_decisions[iid] = decision
            self.campaign_tree.set(
                iid, "policy_action", self._campaign_policy_action_label(decision.action),
            )
            self.campaign_tree.set(iid, "policy_reason", decision.reason)
            existing_tag = (
                "enabled" if int(self.campaign_items[iid].get("status") or 0) == CAMPAIGN_STATUS_ENABLED
                else "paused"
            )
            if decision.action is CampaignAction.PAUSE:
                tags = ("policy_pause",)
                pause_count += 1
            elif decision.action is CampaignAction.ALLOW_LOW_BUDGET_WHITE:
                tags = ("policy_allow",)
                allow_count += 1
            else:
                tags = (existing_tag,)
            self.campaign_tree.item(iid, tags=tags)
        mode = "今天跑广告" if self.campaign_run_today.get() else "今天不跑广告"
        self.campaign_policy_summary.set(
            f"{mode} · 计划暂停 {pause_count} · 白量保留 {allow_count} · 共 {len(decisions)}"
        )
        self._log(f"今日投放计划（只读）：{self.campaign_policy_summary.get()}")

    def _campaign_policy_execute(self) -> None:
        pause_items = [
            (iid, self.campaign_items[iid], decision)
            for iid, decision in self.campaign_policy_decisions.items()
            if iid in self.campaign_items and decision.action is CampaignAction.PAUSE
        ]
        plans = write_plans_for_decisions(item[2] for item in pause_items)
        if not plans:
            messagebox.showinfo(
                "没有暂停计划", "请先生成今日计划；当前没有需要暂停的系列。", parent=self,
            )
            return
        preview = "\n".join(
            f"- {item.get('name') or plan.target_id} "
            f"({plan.target_id})"
            for plan, (_iid, item, _decision) in zip(plans[:12], pause_items[:12])
        )
        if len(plans) > 12:
            preview += f"\n- 另有 {len(plans) - 12} 个系列"
        if not messagebox.askyesno(
            "确认执行今日暂停计划",
            f"将暂停 {len(plans)} 个广告系列，并逐个读回校验。\n\n{preview}\n\n确认继续？",
            parent=self,
        ):
            return

        def task() -> tuple[Any, ...]:
            pending_items = [item for _iid, item, _decision in pause_items]

            def writer(plan):
                item = pending_items.pop(0)
                if str(item.get("campaign_id") or "") != plan.target_id:
                    raise RuntimeError("今日投放计划与执行对象不一致，已停止写入")
                runner = ProtocolRunner(
                    str(item["profile_id"]), log=self._log,
                    browser_type=str(item["browser_type"]),
                )
                try:
                    result = runner.set_campaigns_status(
                        {str(item["cid"])}, CAMPAIGN_STATUS_PAUSED,
                        campaign_ids={plan.target_id},
                    )
                    return result.get(str(item["cid"]), {})
                finally:
                    runner.close()

            return execute_write_plans(
                plans, writer, dry_run=False, authorized=True,
            )

        def done(records: tuple[Any, ...]) -> None:
            self._log(f"今日投放暂停计划执行完成：{len(records)} 个系列")
            self._campaign_load(False)

        self._run_async("执行今日投放暂停计划", task, done)

    def _campaign_set_status(self, target_status: int) -> None:
        selected = self.campaign_tree.selection()
        if not selected:
            messagebox.showinfo("没有选中", "请先选中账户行或系列行", parent=self)
            return
        # 汇总目标：{profile: {cid: set(campaign_ids 或 None)}}
        plan: dict[tuple[str, str], dict[str, set[str] | None]] = {}
        total = 0
        for iid in selected:
            item = self.campaign_items.get(iid)
            if not item:
                continue
            identity = (str(item["browser_type"]), str(item["profile_id"]))
            cid = item["cid"]
            bucket = plan.setdefault(identity, {})
            if "campaign_id" in item:
                ids = bucket.setdefault(cid, set())
                if ids is not None:
                    ids.add(item["campaign_id"])
                    total += 1
            else:
                bucket[cid] = None  # 整户
                total += len(item.get("campaigns") or [])
        if not plan:
            return
        label = "暂停" if target_status == CAMPAIGN_STATUS_PAUSED else "启用"
        if not messagebox.askyesno(
            f"批量{label}",
            f"将对 {sum(len(v) for v in plan.values())} 个账户的约 {total} 个系列执行{label}。\n确认继续？",
            parent=self,
        ):
            return

        def task() -> dict[str, Any]:
            merged: dict[str, Any] = {}
            for (browser_type, profile), cid_map in plan.items():
                for cid, ids in cid_map.items():
                    runner = ProtocolRunner(
                        profile, log=self._log, browser_type=browser_type,
                    )
                    try:
                        result = runner.set_campaigns_status(
                            {cid}, target_status,
                            campaign_ids=ids if ids else None,
                        )
                        merged[f"{window_key(browser_type, profile)}:{cid}"] = result.get(cid, {})
                    finally:
                        runner.close()
            return merged

        def done(_results: dict[str, Any]) -> None:
            self._campaign_load(False)

        self._run_async(f"批量{label}系列", task, done)

    def _build_placement_tab(self) -> None:
        tab = self.placement_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)
        ttk.Label(
            tab,
            text=(
                "按单日或全部历史范围读取 Google Ads 实际展示位置，按“广告系列 → 广告组 → 展示位置”展开。"
                "可多选网站，并通过 CampaignCriterionService 从各自广告系列中排除；"
                "非网站类型不发送，已有排除会自动跳过。"
            ),
            foreground=COLOR_MUTED,
            wraplength=900,
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        query_bar = ttk.Frame(tab)
        query_bar.grid(row=1, column=0, sticky="ew", pady=(0, 7))
        ttk.Label(query_bar, text="指纹浏览器窗口").pack(side="left")
        self.placement_window_box = ttk.Combobox(
            query_bar, textvariable=self.placement_window, width=20, state="readonly"
        )
        self.placement_window_box.pack(side="left", padx=(6, 10))
        self.placement_window_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._sync_placement_cids()
        )
        ttk.Label(query_bar, text="CID").pack(side="left")
        self.placement_cid_box = ttk.Combobox(
            query_bar, textvariable=self.placement_cid, width=27, state="readonly"
        )
        self.placement_cid_box.pack(side="left", padx=(6, 10))
        ttk.Label(query_bar, text="查询日期").pack(side="left")
        self.placement_date_entry = ttk.Entry(
            query_bar, textvariable=self.placement_query_date, width=12
        )
        self.placement_date_entry.pack(side="left", padx=(6, 4))
        ttk.Button(
            query_bar, text="今天", command=lambda: self._set_placement_date_offset(0)
        ).pack(side="left", padx=3)
        ttk.Button(
            query_bar, text="昨天", command=lambda: self._set_placement_date_offset(-1)
        ).pack(side="left", padx=3)
        ttk.Button(
            query_bar, text="全部", command=self._set_placement_date_all
        ).pack(side="left", padx=3)
        self.placement_query_button = ttk.Button(
            query_bar, text="查询展示位置（只读）", command=self._query_placements,
            style="Accent.TButton",
        )
        self.placement_query_button.pack(side="left", padx=(12, 4))

        action_container = ttk.Frame(tab)
        action_container.grid(row=2, column=0, sticky="ew", pady=(0, 7))
        action_bar = ttk.Frame(action_container)
        action_bar.pack(fill="x")
        ttk.Label(action_bar, textvariable=self.placement_selection_text).pack(side="left")
        self.placement_select_campaign_button = ttk.Button(
            action_bar, text="选择当前广告系列的全部网站", command=self._select_placement_campaign
        )
        self.placement_select_campaign_button.pack(side="left", padx=(12, 4))
        ttk.Button(
            action_bar, text="清空选择", command=self._clear_placement_selection,
        ).pack(side="left", padx=4)
        self.placement_preflight_button = ttk.Button(
            action_bar, text="系列排除预检（不写入）", command=self._placement_exclusion_preflight
        )
        self.placement_preflight_button.pack(side="left", padx=(18, 4))
        self.placement_exclude_button = ttk.Button(
            action_bar, text="从广告系列中排除", command=self._placement_exclusion_execute,
            style="Write.TButton",
        )
        self.placement_exclude_button.pack(side="left", padx=4)

        analysis_bar = ttk.Frame(action_container)
        analysis_bar.pack(fill="x", pady=(6, 0))
        ttk.Button(
            analysis_bar, text="导入网站名单并选择", command=self._import_placement_selection,
            style="Write.TButton",
        ).pack(side="left")
        ttk.Button(
            analysis_bar, text="导出展示位置", command=self._export_placement_rows,
            style="Success.TButton",
        ).pack(side="left", padx=(5, 12))
        self.placement_analyze_button = ttk.Button(
            analysis_bar, text="检测可访问 + 智能分类", command=self._analyze_placement_websites,
            style="Accent.TButton",
        )
        self.placement_analyze_button.pack(side="left")
        ttk.Label(analysis_bar, text="查看/筛选分类").pack(side="left", padx=(12, 4))
        self.placement_category_box = ttk.Combobox(
            analysis_bar, textvariable=self.placement_category,
            values=WEBSITE_CATEGORIES, state="readonly", width=9,
        )
        self.placement_category_box.pack(side="left")
        self.placement_category_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._placement_category_changed()
        )
        ttk.Button(
            analysis_bar, text="选中此分类网站", command=self._select_placement_category,
        ).pack(side="left", padx=5)
        ttk.Label(
            analysis_bar, textvariable=self.placement_analysis_summary, foreground=COLOR_MUTED,
        ).pack(side="right")

        split = ttk.Panedwindow(tab, orient="vertical")
        split.grid(row=3, column=0, sticky="nsew")
        upper = ttk.Frame(split)
        lower = ttk.LabelFrame(split, text="选择 / 预检 / 写后校验详情", padding=6)
        split.add(upper, weight=4)
        split.add(lower, weight=1)
        upper.columnconfigure(0, weight=1)
        upper.rowconfigure(0, weight=1)
        lower.columnconfigure(0, weight=1)
        lower.rowconfigure(0, weight=1)

        columns = (
            "object_type", "object_id", "category", "access_status",
            "impressions", "interactions", "interaction_rate",
            "average_cost", "cost", "conversions", "currency", "exclusion_status",
        )
        self.placement_tree = ttk.Treeview(
            upper, columns=columns, show="tree headings", selectmode="extended"
        )
        self.placement_tree.heading("#0", text="广告系列 / 广告组 / 展示位置")
        headings = {
            "object_type": "类型", "object_id": "对象 ID", "impressions": "展示次数",
            "category": "兴趣/内容分类", "access_status": "可访问性",
            "interactions": "互动", "interaction_rate": "互动率", "average_cost": "平均费用",
            "cost": "费用", "conversions": "转化", "currency": "币种",
            "exclusion_status": "广告系列排除状态",
        }
        widths = {
            "object_type": 65, "object_id": 100, "category": 70, "access_status": 80,
            "impressions": 70, "interactions": 55,
            "interaction_rate": 65, "average_cost": 70, "cost": 70,
            "conversions": 55, "currency": 45, "exclusion_status": 110,
        }
        self.placement_tree.column("#0", width=220, minwidth=190)
        for key in columns:
            self.placement_tree.heading(key, text=headings[key])
            self.placement_tree.column(
                key, width=widths[key], minwidth=min(widths[key], 65),
                anchor="center" if key not in {"exclusion_status"} else "w",
            )
        self.placement_tree.tag_configure("campaign", foreground=COLOR_PRIMARY_HOVER, background=COLOR_PRIMARY_SOFT)
        self.placement_tree.tag_configure("group", foreground=COLOR_WRITE, background=COLOR_WRITE_SOFT)
        self.placement_tree.tag_configure("website", foreground=COLOR_TEXT)
        self.placement_tree.tag_configure("unsupported", foreground=COLOR_MUTED, background=COLOR_SURFACE_ALT)
        self.placement_tree.tag_configure("excluded", foreground=COLOR_WARNING, background=COLOR_WARNING_SOFT)
        self.placement_tree.tag_configure(
            "category_selected", foreground=COLOR_WRITE, background=COLOR_WRITE_SOFT
        )
        self.placement_tree.grid(row=0, column=0, sticky="nsew")
        placement_y = ttk.Scrollbar(upper, orient="vertical", command=self.placement_tree.yview)
        placement_y.grid(row=0, column=1, sticky="ns")
        placement_x = ttk.Scrollbar(upper, orient="horizontal", command=self.placement_tree.xview)
        placement_x.grid(row=1, column=0, sticky="ew")
        self.placement_tree.configure(yscrollcommand=placement_y.set, xscrollcommand=placement_x.set)
        self.placement_tree.bind(
            "<<TreeviewSelect>>", lambda _event: self._placement_selection_changed()
        )

        self.placement_detail = tk.Text(
            lower, wrap="none", state="disabled", font=("Consolas", 9), height=7
        )
        self.placement_detail.grid(row=0, column=0, sticky="nsew")
        detail_y = ttk.Scrollbar(lower, orient="vertical", command=self.placement_detail.yview)
        detail_y.grid(row=0, column=1, sticky="ns")
        detail_x = ttk.Scrollbar(lower, orient="horizontal", command=self.placement_detail.xview)
        detail_x.grid(row=1, column=0, sticky="ew")
        self.placement_detail.configure(yscrollcommand=detail_y.set, xscrollcommand=detail_x.set)

    def _build_library_tab(self) -> None:
        tab = self.library_tab
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(1, weight=1)
        banner = ttk.Frame(tab)
        banner.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        ttk.Label(banner, text="本地内容方案", style="SectionTitle.TLabel").pack(side="left")
        ttk.Label(
            banner,
            text="仅保存在这台电脑，不会自动上传或修改 Google Ads",
            foreground=COLOR_SUCCESS,
        ).pack(side="left", padx=(12, 0))
        ttk.Button(
            banner,
            text="打开本地配置目录",
            command=lambda: os.startfile(config_path().parent),
        ).pack(side="right")
        ttk.Label(
            banner,
            text=r"%APPDATA%\GGAdsProtocolTool\local-config\content-library.json",
            foreground=COLOR_MUTED,
        ).pack(side="right", padx=(0, 10))
        left = ttk.Frame(tab)
        left.grid(row=1, column=0, sticky="ns", padx=(0, 10))
        ttk.Label(left, text="本地内容方案", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 6))
        self.library_tree = ttk.Treeview(left, columns=("name",), show="headings", height=20, selectmode="browse")
        self.library_tree.heading("name", text="方案名称")
        self.library_tree.column("name", width=210)
        self.library_tree.pack(fill="both", expand=True)
        self.library_tree.bind("<<TreeviewSelect>>", lambda _event: self._load_library_selection())
        self.library_tree.bind("<Button-3>", self._show_library_context_menu)
        self.library_tree.bind("<Double-1>", self._library_double_click)
        self.library_context_menu = tk.Menu(self, tearoff=False)
        library_buttons = ttk.Frame(left)
        library_buttons.pack(fill="x", pady=8)
        for label, command in (("新增", self._new_library), ("重命名", self._rename_library), ("复制", self._duplicate_library), ("删除", self._delete_library)):
            ttk.Button(library_buttons, text=label, command=command).pack(fill="x", pady=2)

        right = ttk.Frame(tab)
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        self.library_title = ttk.Label(right, text="请选择方案", font=("Microsoft YaHei UI", 11, "bold"))
        self.library_title.grid(row=0, column=0, sticky="w", pady=(0, 6))

        mapping = ttk.LabelFrame(right, text="映射到 Google Ads 的对象名称", padding=8)
        mapping.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        mapping.columnconfigure(1, weight=1)
        mapping.columnconfigure(3, weight=1)
        self.library_object_names: dict[str, tk.StringVar] = {}
        mapping_labels = {
            "app": "app 排除列表",
            "web": "web 排除列表",
            "xq": "xq 自定义受众",
            "ss": "ss 自定义受众",
        }
        for index, key in enumerate(("app", "web", "xq", "ss")):
            row, group = divmod(index, 2)
            variable = tk.StringVar(value=DEFAULT_OBJECT_NAMES[key])
            self.library_object_names[key] = variable
            ttk.Label(mapping, text=mapping_labels[key]).grid(
                row=row, column=group * 2, padx=(0 if group == 0 else 14, 6), pady=4, sticky="w"
            )
            ttk.Entry(mapping, textvariable=variable).grid(
                row=row, column=group * 2 + 1, pady=4, sticky="ew"
            )
        ttk.Label(
            mapping,
            text="示例：方案“韩国”可设置为 韩国-app、韩国-web、韩国-xq、韩国-ss；批量写入时使用这里的名称。",
            foreground=COLOR_MUTED,
        ).grid(row=2, column=0, columnspan=4, pady=(5, 0), sticky="w")

        editors = ttk.Notebook(right)
        editors.grid(row=2, column=0, sticky="nsew")
        app_frame = ttk.Frame(editors, padding=8)
        editors.add(app_frame, text=" 排除列表 · app 应用类别 ")
        app_frame.rowconfigure(1, weight=1)
        app_frame.columnconfigure(0, weight=1)
        app_bar = ttk.Frame(app_frame)
        app_bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(app_bar, text="全选 140 类", command=lambda: self.library_app_list.selection_set(0, "end")).pack(side="left")
        ttk.Button(app_bar, text="清空", command=lambda: self.library_app_list.selection_clear(0, "end")).pack(side="left", padx=8)
        self.library_app_list = tk.Listbox(app_frame, selectmode="extended", exportselection=False)
        self.library_app_list.grid(row=1, column=0, sticky="nsew")
        app_scroll = ttk.Scrollbar(app_frame, orient="vertical", command=self.library_app_list.yview)
        app_scroll.grid(row=1, column=1, sticky="ns")
        self.library_app_list.configure(yscrollcommand=app_scroll.set)
        for category_id, name in APP_CATEGORIES:
            store = "Apple" if str(category_id).startswith("605") else "Google Play"
            self.library_app_list.insert("end", f"{store}  |  {name}  |  {category_id}")
        self.library_texts: dict[str, tk.Text] = {}
        for key in ("web", "xq", "ss"):
            frame = ttk.Frame(editors, padding=8)
            frame.rowconfigure(1, weight=1)
            frame.columnconfigure(0, weight=1)
            editors.add(frame, text=f" {TYPE_LABELS[key]} · {key} ")
            ttk.Label(
                frame,
                text=f"每行一项。{CONTENT_EXAMPLES[key]}",
                foreground=COLOR_MUTED,
            ).grid(row=0, column=0, sticky="w", pady=(0, 6))
            text = tk.Text(frame, wrap="none", font=("Consolas", 10))
            text.grid(row=1, column=0, sticky="nsew")
            yscroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
            yscroll.grid(row=1, column=1, sticky="ns")
            xscroll = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
            xscroll.grid(row=2, column=0, sticky="ew")
            text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
            self.library_texts[key] = text
        actions = ttk.Frame(right)
        actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(
            actions, text="保存当前方案", command=self._save_current_library, style="Success.TButton"
        ).pack(side="left")
        ttk.Button(actions, text="高级 JSON", command=self._advanced_json).pack(side="left", padx=8)
        ttk.Button(
            actions, text="导入", command=self._import_json, style="Write.TButton"
        ).pack(side="left")
        ttk.Button(
            actions, text="导出", command=self._export_json, style="Success.TButton"
        ).pack(side="left", padx=8)

    def _build_creative_library_tab(self) -> None:
        tab = self.creative_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)
        banner = ttk.Frame(tab)
        banner.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(banner, text="本地广告素材库", style="SectionTitle.TLabel").pack(side="left")
        ttk.Label(
            banner,
            text="图片副本和文案仅保存在本机；选择方案不会自动上传",
            foreground=COLOR_SUCCESS,
        ).pack(side="left", padx=(12, 0))
        ttk.Button(
            banner, text="打开本地配置目录",
            command=lambda: os.startfile(creative_library_path().parent), style="Accent.TButton",
        ).pack(side="right")
        ttk.Label(
            banner, text="ad-creative-library.json", foreground=COLOR_MUTED,
        ).pack(side="right", padx=(0, 10))

        toolbar = ttk.Frame(tab)
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        toolbar_actions = ttk.Frame(toolbar)
        toolbar_actions.pack(fill="x")
        ttk.Button(
            toolbar_actions, text="新增图片方案", command=lambda: self._new_creative_plan("image"),
            style="Write.TButton",
        ).pack(side="left")
        ttk.Button(
            toolbar_actions, text="新增文案方案", command=lambda: self._new_creative_plan("copy"),
            style="Write.TButton",
        ).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar_actions, text="编辑", command=self._edit_creative_plan).pack(side="left", padx=(12, 0))
        ttk.Button(toolbar_actions, text="复制", command=self._duplicate_creative_plan).pack(side="left", padx=6)
        ttk.Button(
            toolbar_actions, text="删除", command=self._delete_creative_plan, style="Danger.TButton",
        ).pack(side="left")
        ttk.Button(
            toolbar_actions, text="应用到广告创建", command=self._apply_selected_creative_plan,
            style="Write.TButton",
        ).pack(side="right")
        toolbar_templates = ttk.Frame(toolbar)
        toolbar_templates.pack(fill="x", pady=(6, 0))
        ttk.Label(toolbar_templates, text="模板文件").pack(side="left")
        ttk.Button(
            toolbar_templates, text="导入模板", command=self._import_creative_template,
            style="Write.TButton",
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            toolbar_templates, text="导出选中",
            command=lambda: self._export_creative_template(True), style="Success.TButton",
        ).pack(side="left", padx=6)
        ttk.Button(
            toolbar_templates, text="导出全部",
            command=lambda: self._export_creative_template(False), style="Success.TButton",
        ).pack(side="left")
        ttk.Label(
            toolbar_templates, text="JSON 中的图片支持相对路径；重名自动改名，不覆盖",
            foreground=COLOR_MUTED,
        ).pack(side="right")
        filter_row = ttk.Frame(toolbar)
        filter_row.pack(fill="x", pady=(6, 0))
        ttk.Label(filter_row, text="查询方案").pack(side="left")
        creative_search_entry = ttk.Entry(filter_row, textvariable=self.creative_search, width=24)
        creative_search_entry.pack(side="left", padx=(6, 4))
        creative_search_entry.bind("<Return>", lambda _event: self._apply_creative_filters())
        ttk.Button(
            filter_row, text="查询", command=self._apply_creative_filters,
            style="Accent.TButton",
        ).pack(side="left")
        ttk.Button(filter_row, text="清空", command=self._clear_creative_filters).pack(side="left", padx=(4, 10))
        ttk.Combobox(
            filter_row, textvariable=self.creative_type_filter, state="readonly", width=10,
            values=("全部类型", "图片方案", "文案方案"),
        ).pack(side="left")
        ttk.Label(filter_row, text="每页").pack(side="left", padx=(10, 4))
        creative_size_box = ttk.Combobox(
            filter_row, textvariable=self.creative_page_size, state="readonly", width=5,
            values=("10", "20", "50", "100"),
        )
        creative_size_box.pack(side="left")
        creative_size_box.bind("<<ComboboxSelected>>", lambda _event: self._apply_creative_filters())
        self.creative_next_button = ttk.Button(
            filter_row, text="下一页", command=lambda: self._move_creative_page(1),
        )
        self.creative_next_button.pack(side="right")
        self.creative_prev_button = ttk.Button(
            filter_row, text="上一页", command=lambda: self._move_creative_page(-1),
        )
        self.creative_prev_button.pack(side="right", padx=(0, 4))
        ttk.Label(filter_row, textvariable=self.creative_page_info, foreground=COLOR_MUTED).pack(
            side="right", padx=(0, 10)
        )

        upload_bar = ttk.LabelFrame(tab, text="上传选中的图片方案到 Google Ads 素材库", padding=6)
        upload_bar.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        upload_target_row = ttk.Frame(upload_bar)
        upload_target_row.pack(fill="x")
        ttk.Label(upload_target_row, text="指纹浏览器窗口").pack(side="left")
        self.creative_upload_window_box = ttk.Combobox(
            upload_target_row, textvariable=self.creative_upload_window, width=17, state="readonly",
        )
        self.creative_upload_window_box.pack(side="left", padx=(6, 12))
        self.creative_upload_window_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._sync_creative_upload_cids()
        )
        ttk.Label(upload_target_row, text="目标 CID").pack(side="left")
        self.creative_upload_cid_box = ttk.Combobox(
            upload_target_row, textvariable=self.creative_upload_cid, width=25,
        )
        self.creative_upload_cid_box.pack(side="left", padx=(6, 12))
        self.creative_upload_cid_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._invalidate_remote_asset_context(),
            add="+",
        )
        ttk.Button(
            upload_target_row, text="上传图片方案（余额 > 0）",
            command=self._upload_selected_creative_plan, style="Write.TButton",
        ).pack(side="left")
        ttk.Button(
            upload_target_row, text="查询全部图片素材（只读）",
            command=self._query_remote_image_assets, style="Accent.TButton",
        ).pack(side="left", padx=(6, 0))
        ttk.Label(
            upload_bar, text="每张图片上传后必须回读 Asset ID、图片类型、状态、尺寸和文件大小；结果写入运行记录。",
            foreground=COLOR_MUTED,
        ).pack(anchor="w", pady=(5, 0))

        self.creative_content_tabs = ttk.Notebook(tab)
        self.creative_content_tabs.grid(row=3, column=0, sticky="nsew")
        local_page = ttk.Frame(self.creative_content_tabs, padding=(0, 6, 0, 0))
        remote_page = ttk.Frame(self.creative_content_tabs, padding=(0, 6, 0, 0))
        self.creative_content_tabs.add(local_page, text="本地图片/文案方案")
        self.creative_content_tabs.add(remote_page, text="Google Ads 图片素材查询")
        local_page.columnconfigure(0, weight=1)
        local_page.rowconfigure(0, weight=1)
        split = ttk.Panedwindow(local_page, orient="vertical")
        split.grid(row=0, column=0, sticky="nsew")
        upper = ttk.Frame(split)
        lower = ttk.LabelFrame(split, text="方案详情", padding=6)
        split.add(upper, weight=3)
        split.add(lower, weight=2)
        upper.columnconfigure(0, weight=1)
        upper.rowconfigure(0, weight=1)
        columns = ("kind", "name", "summary", "note", "created", "updated")
        self.creative_tree = ttk.Treeview(
            upper, columns=columns, show="tree headings", selectmode="browse",
            style="Creative.Treeview",
        )
        self.creative_tree.heading("#0", text="图片预览")
        self.creative_tree.column("#0", width=132, minwidth=132, stretch=False)
        headings = {
            "kind": "类型", "name": "方案名称", "summary": "内容摘要", "note": "备注",
            "created": "新建时间", "updated": "更新时间",
        }
        widths = {"kind": 90, "name": 180, "summary": 360, "note": 220, "created": 165, "updated": 165}
        for key in columns:
            self.creative_tree.heading(key, text=headings[key])
            self.creative_tree.column(key, width=widths[key], minwidth=75)
        self.creative_tree.tag_configure("image", foreground=COLOR_PRIMARY_HOVER, background=COLOR_PRIMARY_SOFT)
        self.creative_tree.tag_configure("copy", foreground=COLOR_WRITE, background=COLOR_WRITE_SOFT)
        self.creative_tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(upper, orient="vertical", command=self.creative_tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(upper, orient="horizontal", command=self.creative_tree.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.creative_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.creative_tree.bind("<<TreeviewSelect>>", lambda _event: self._show_creative_plan_detail())
        self.creative_tree.bind("<Double-1>", self._creative_plan_double_click)
        self.creative_tree.bind("<Button-3>", self._show_creative_plan_context_menu)
        self.creative_context_menu = tk.Menu(self, tearoff=False)
        self._creative_thumbnail_placeholders = {
            "loading": self._make_creative_placeholder("LOADING", "#E8EDF5", "#5B6B82"),
            "copy": self._make_creative_placeholder("COPY", "#EDE9FE", "#6D28D9"),
            "empty": self._make_creative_placeholder("EMPTY", "#F1F5F9", "#64748B"),
            "missing": self._make_creative_placeholder("MISSING", "#FEE2E2", "#B91C1C"),
            "damaged": self._make_creative_placeholder("BROKEN", "#FFEDD5", "#C2410C"),
        }
        lower.columnconfigure(0, weight=1)
        lower.rowconfigure(0, weight=1)
        self.creative_detail = tk.Text(
            lower, state="disabled", wrap="none", font=("Consolas", 10), height=9,
        )
        self.creative_detail.grid(row=0, column=0, sticky="nsew")
        detail_y = ttk.Scrollbar(lower, orient="vertical", command=self.creative_detail.yview)
        detail_y.grid(row=0, column=1, sticky="ns")
        detail_x = ttk.Scrollbar(lower, orient="horizontal", command=self.creative_detail.xview)
        detail_x.grid(row=1, column=0, sticky="ew")
        self.creative_detail.configure(yscrollcommand=detail_y.set, xscrollcommand=detail_x.set)

        remote_page.columnconfigure(0, weight=1)
        remote_page.rowconfigure(1, weight=1)
        remote_filter = ttk.Frame(remote_page)
        remote_filter.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(remote_filter, text="Asset ID / 名称").pack(side="left")
        remote_search_entry = ttk.Entry(
            remote_filter, textvariable=self.remote_asset_search, width=28,
        )
        remote_search_entry.pack(side="left", padx=(6, 4))
        remote_search_entry.bind("<Return>", lambda _event: self._apply_remote_asset_filter())
        ttk.Button(remote_filter, text="筛选", command=self._apply_remote_asset_filter).pack(side="left")
        ttk.Button(remote_filter, text="清空", command=self._clear_remote_asset_filter).pack(side="left", padx=(4, 10))
        ttk.Label(remote_filter, text="每页").pack(side="left")
        remote_size_box = ttk.Combobox(
            remote_filter, textvariable=self.remote_asset_page_size, state="readonly", width=5,
            values=("20", "50", "100"),
        )
        remote_size_box.pack(side="left", padx=(4, 0))
        remote_size_box.bind("<<ComboboxSelected>>", lambda _event: self._apply_remote_asset_filter())
        self.remote_asset_next_button = ttk.Button(
            remote_filter, text="下一页", command=lambda: self._move_remote_asset_page(1),
        )
        self.remote_asset_next_button.pack(side="right")
        self.remote_asset_prev_button = ttk.Button(
            remote_filter, text="上一页", command=lambda: self._move_remote_asset_page(-1),
        )
        self.remote_asset_prev_button.pack(side="right", padx=(0, 4))
        ttk.Label(
            remote_filter, textvariable=self.remote_asset_page_info, foreground=COLOR_MUTED,
        ).pack(side="right", padx=(0, 10))
        remote_table = ttk.Frame(remote_page)
        remote_table.grid(row=1, column=0, sticky="nsew")
        remote_table.columnconfigure(0, weight=1)
        remote_table.rowconfigure(0, weight=1)
        remote_columns = (
            "asset_id", "name", "status", "dimensions", "file_size", "ratio", "preview_url",
        )
        self.remote_asset_tree = ttk.Treeview(
            remote_table, columns=remote_columns, show="headings", selectmode="browse",
        )
        remote_headings = {
            "asset_id": "Asset ID", "name": "名称", "status": "状态",
            "dimensions": "尺寸", "file_size": "文件大小", "ratio": "比例",
            "preview_url": "预览地址",
        }
        remote_widths = {
            "asset_id": 150, "name": 240, "status": 80, "dimensions": 110,
            "file_size": 100, "ratio": 85, "preview_url": 420,
        }
        for key in remote_columns:
            self.remote_asset_tree.heading(key, text=remote_headings[key])
            self.remote_asset_tree.column(key, width=remote_widths[key], minwidth=70)
        self.remote_asset_tree.tag_configure("enabled", foreground=COLOR_SUCCESS, background=COLOR_SUCCESS_SOFT)
        self.remote_asset_tree.tag_configure("unknown", foreground=COLOR_WARNING, background=COLOR_WARNING_SOFT)
        self.remote_asset_tree.grid(row=0, column=0, sticky="nsew")
        remote_y = ttk.Scrollbar(remote_table, orient="vertical", command=self.remote_asset_tree.yview)
        remote_y.grid(row=0, column=1, sticky="ns")
        remote_x = ttk.Scrollbar(remote_table, orient="horizontal", command=self.remote_asset_tree.xview)
        remote_x.grid(row=1, column=0, sticky="ew")
        self.remote_asset_tree.configure(yscrollcommand=remote_y.set, xscrollcommand=remote_x.set)

    def _build_remote_tab(self) -> None:
        tab = self.remote_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        ttk.Label(
            tab,
            text="这里直接读取和修改 Google Ads 服务器中真实存在的排除列表与自定义受众；本机模板请到“本地配置 → 本地内容库”管理。",
            foreground=COLOR_WARNING,
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))
        bar = ttk.Frame(tab)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        context = ttk.Frame(bar)
        context.pack(fill="x")
        actions = ttk.Frame(bar)
        actions.pack(fill="x", pady=(7, 0))
        ttk.Label(context, text="指纹浏览器窗口").pack(side="left")
        self.remote_window_box = ttk.Combobox(
            context, textvariable=self.remote_window, width=20, state="readonly"
        )
        self.remote_window_box.pack(side="left", padx=8)
        self.remote_window_box.bind("<<ComboboxSelected>>", lambda _event: self._sync_remote_cids())
        ttk.Label(context, text="CID").pack(side="left")
        self.remote_cid_box = ttk.Combobox(context, textvariable=self.remote_cid, width=29)
        self.remote_cid_box.pack(side="left", padx=8)
        self.remote_refresh_button = ttk.Button(
            context, text="查询 Google Ads 数据", command=self._refresh_remote,
            style="Accent.TButton",
        )
        self.remote_refresh_button.pack(side="left")
        ttk.Label(actions, text="Google Ads 对象操作", style="Muted.TLabel").pack(side="left")
        ttk.Button(
            actions, text="新建", command=self._create_remote, style="Write.TButton"
        ).pack(side="left", padx=(12, 5))
        ttk.Button(actions, text="编辑/替换", command=self._edit_remote).pack(side="left", padx=5)
        ttk.Button(
            actions, text="删除", command=self._delete_remote, style="Danger.TButton"
        ).pack(side="left", padx=5)
        ttk.Button(
            actions, text="导入网站排除", command=self._import_remote_web_exclusion,
            style="Write.TButton",
        ).pack(side="left", padx=(18, 5))
        ttk.Button(
            actions, text="导出选中对象", command=self._export_remote_object,
            style="Success.TButton",
        ).pack(side="left", padx=5)

        split = ttk.Panedwindow(tab, orient="vertical")
        split.grid(row=2, column=0, sticky="nsew")
        upper = ttk.Frame(split)
        lower = ttk.LabelFrame(split, text="对象内容", padding=6)
        split.add(upper, weight=3)
        split.add(lower, weight=2)
        upper.columnconfigure(0, weight=1)
        upper.rowconfigure(0, weight=1)
        self.remote_tree = ttk.Treeview(upper, columns=("group", "kind", "name", "id", "count"), show="headings", selectmode="browse")
        headings = {"group": "分组", "kind": "类型", "name": "名称", "id": "Google ID", "count": "数量"}
        for key, text in headings.items():
            self.remote_tree.heading(key, text=text)
        self.remote_tree.column("group", width=110, anchor="center")
        self.remote_tree.column("kind", width=150, anchor="center")
        self.remote_tree.column("name", width=280)
        self.remote_tree.column("id", width=180)
        self.remote_tree.column("count", width=80, anchor="center")
        self.remote_tree.grid(row=0, column=0, sticky="nsew")
        self.remote_tree.bind("<<TreeviewSelect>>", lambda _event: self._show_remote_content())
        self.remote_tree.bind("<Button-3>", self._show_remote_context_menu)
        self.remote_tree.bind("<Double-1>", self._remote_double_click)
        self.remote_context_menu = tk.Menu(self, tearoff=False)
        self.remote_content = tk.Text(lower, state="disabled", wrap="none", font=("Consolas", 10), height=9)
        lower.columnconfigure(0, weight=1)
        lower.rowconfigure(0, weight=1)
        self.remote_content.grid(row=0, column=0, sticky="nsew")
        remote_y = ttk.Scrollbar(lower, orient="vertical", command=self.remote_content.yview)
        remote_y.grid(row=0, column=1, sticky="ns")
        remote_x = ttk.Scrollbar(lower, orient="horizontal", command=self.remote_content.xview)
        remote_x.grid(row=1, column=0, sticky="ew")
        self.remote_content.configure(yscrollcommand=remote_y.set, xscrollcommand=remote_x.set)

    def _build_warmup_tab(self) -> None:
        tab = self.warmup_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)
        ttk.Label(
            tab,
            text=(
                "显式选择一个可用普通 CID 中的 App-1 作为参考；软件不再内置旧参考账户。"
                "目标普通 CID 必须状态可用、Billing 余额可识别且大于 0。"
                "目标日预算可编辑；没有 App 系列时先复制，已有暂停系列时复用。"
                "启用前先按协议调整预算并回读金额。"
                "先在左侧按窗口多选 CID；原生复制还要求当前 Google 登录同时具有参考 CID 与目标 CID 权限。"
            ),
            foreground=COLOR_WARNING,
            wraplength=900,
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        bar = ttk.Frame(tab)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.warmup_scan_button = ttk.Button(
            bar, text="扫描选中 CID（只读）", command=self._warmup_scan,
            style="Accent.TButton",
        )
        self.warmup_scan_button.pack(side="left")
        ttk.Button(bar, text="全选符合条件", command=lambda: self._warmup_select_all(True)).pack(
            side="left", padx=8
        )
        ttk.Button(bar, text="清空选择", command=lambda: self._warmup_select_all(False)).pack(side="left")
        self.warmup_execute_button = ttk.Button(
            bar, text="一键复制并启用", command=self._warmup_execute,
            style="Write.TButton",
        )
        self.warmup_execute_button.pack(side="left", padx=(18, 8))
        ttk.Label(bar, text="参考 CID").pack(side="left", padx=(10, 5))
        self.warmup_source_entry = ttk.Entry(
            bar, textvariable=self.warmup_source_cid, width=16, justify="center"
        )
        self.warmup_source_entry.pack(side="left")
        ttk.Label(bar, text="目标日预算 USD").pack(side="left", padx=(10, 5))
        self.warmup_budget_entry = ttk.Entry(
            bar, textvariable=self.warmup_daily_budget, width=10, justify="right"
        )
        self.warmup_budget_entry.pack(side="left")
        ttk.Label(
            bar,
            text="门槛：余额 > 0；修改预算并回读后才启用",
            foreground=COLOR_MUTED,
        ).pack(side="right")

        template_bar = ttk.Frame(tab)
        template_bar.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(
            template_bar,
            textvariable=self.warmup_template_summary,
            style="Muted.TLabel",
        ).pack(side="left")
        ttk.Button(
            template_bar,
            text="保存当前为固定模板（本地）",
            command=self._save_current_warmup_template,
            style="Success.TButton",
        ).pack(side="left", padx=(12, 6))
        ttk.Button(
            template_bar,
            text="选用固定模板",
            command=self._select_saved_warmup_template,
        ).pack(side="left")
        ttk.Label(
            template_bar,
            text="批量任务 CID 右键入口默认只生成演练预览，不连接或写入账户",
            foreground=COLOR_MUTED,
        ).pack(side="right")

        workspace = ttk.Frame(tab)
        workspace.grid(row=3, column=0, sticky="nsew")
        workspace.columnconfigure(0, weight=2, minsize=420)
        workspace.columnconfigure(1, weight=3, minsize=620)
        workspace.rowconfigure(0, weight=1)
        targets = ttk.LabelFrame(
            workspace, text="1. 选择目标 CID（Ctrl / Shift 可多选）", padding=6
        )
        results = ttk.Frame(workspace)
        targets.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        results.grid(row=0, column=1, sticky="nsew")
        targets.columnconfigure(0, weight=1)
        targets.rowconfigure(1, weight=1)
        target_bar = ttk.Frame(targets)
        target_bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Button(
            target_bar, text="全选 CID", command=lambda: self._warmup_select_targets(True)
        ).pack(side="left")
        ttk.Button(
            target_bar, text="清空", command=lambda: self._warmup_select_targets(False)
        ).pack(side="left", padx=6)
        ttk.Label(
            target_bar, textvariable=self.warmup_target_summary, style="Muted.TLabel"
        ).pack(side="right")
        self.warmup_target_tree = ttk.Treeview(
            targets,
            columns=("profile", "cid", "status", "balance"),
            show="headings",
            selectmode="extended",
            height=16,
        )
        for key, label in (
            ("profile", "窗口"), ("cid", "CID"), ("status", "状态"), ("balance", "余额")
        ):
            self.warmup_target_tree.heading(key, text=label)
        self.warmup_target_tree.column("profile", width=125, minwidth=95)
        self.warmup_target_tree.column("cid", width=120, minwidth=110, anchor="center")
        self.warmup_target_tree.column("status", width=75, minwidth=65, anchor="center")
        self.warmup_target_tree.column("balance", width=105, minwidth=85, anchor="e")
        self.warmup_target_tree.tag_configure(
            "available", foreground=COLOR_SUCCESS, background=COLOR_SUCCESS_SOFT
        )
        self.warmup_target_tree.tag_configure(
            "unavailable", foreground=COLOR_WARNING, background=COLOR_WARNING_SOFT
        )
        self.warmup_target_tree.tag_configure(
            "banned", foreground=COLOR_DANGER, background=COLOR_DANGER_SOFT
        )
        self.warmup_target_tree.tag_configure(
            "unknown", foreground=COLOR_MUTED, background=COLOR_SURFACE_ALT
        )
        self.warmup_target_tree.grid(row=1, column=0, sticky="nsew")
        target_y = ttk.Scrollbar(targets, orient="vertical", command=self.warmup_target_tree.yview)
        target_y.grid(row=1, column=1, sticky="ns")
        target_x = ttk.Scrollbar(targets, orient="horizontal", command=self.warmup_target_tree.xview)
        target_x.grid(row=2, column=0, sticky="ew")
        self.warmup_target_tree.configure(
            yscrollcommand=target_y.set, xscrollcommand=target_x.set
        )
        self.warmup_target_tree.bind(
            "<<TreeviewSelect>>", lambda _event: self._warmup_target_selection_changed()
        )

        split = ttk.Panedwindow(results, orient="vertical")
        split.pack(fill="both", expand=True)
        upper = ttk.LabelFrame(split, text="2. 扫描结果与执行选择", padding=4)
        lower = ttk.LabelFrame(split, text="账户检查详情", padding=6)
        split.add(upper, weight=4)
        split.add(lower, weight=2)
        upper.columnconfigure(0, weight=1)
        upper.rowconfigure(0, weight=1)
        lower.columnconfigure(0, weight=1)
        lower.rowconfigure(0, weight=1)
        columns = (
            "selected", "profile", "cid", "account_name", "account_status",
            "balance", "daily_spend", "campaign", "campaign_status", "budget", "action", "reason",
        )
        self.warmup_tree = ttk.Treeview(
            upper, columns=columns, show="headings", selectmode="browse"
        )
        headings = {
            "selected": "执行", "profile": "指纹浏览器窗口", "cid": "CID",
            "account_name": "账户名称", "account_status": "账户状态", "balance": "余额",
            "daily_spend": "今日消耗",
            "campaign": "App 系列", "campaign_status": "系列状态", "budget": "当前 → 目标日预算",
            "action": "计划动作", "reason": "条件说明",
        }
        for key, label in headings.items():
            self.warmup_tree.heading(key, text=label)
        widths = {
            "selected": 55, "profile": 130, "cid": 120, "account_name": 235,
            "account_status": 85, "balance": 110, "daily_spend": 105, "campaign": 155,
            "campaign_status": 85, "budget": 160, "action": 175, "reason": 360,
        }
        for key, width in widths.items():
            self.warmup_tree.column(
                key, width=width, minwidth=min(width, 80),
                anchor="center" if key in {"selected", "cid", "account_status", "balance", "daily_spend", "campaign_status", "budget"} else "w",
            )
        self.warmup_tree.tag_configure("eligible", foreground=COLOR_SUCCESS, background=COLOR_SUCCESS_SOFT)
        self.warmup_tree.tag_configure("blocked", foreground=COLOR_WARNING, background=COLOR_WARNING_SOFT)
        self.warmup_tree.tag_configure("failed", foreground=COLOR_DANGER, background=COLOR_DANGER_SOFT)
        self.warmup_tree.grid(row=0, column=0, sticky="nsew")
        warm_y = ttk.Scrollbar(upper, orient="vertical", command=self.warmup_tree.yview)
        warm_y.grid(row=0, column=1, sticky="ns")
        warm_x = ttk.Scrollbar(upper, orient="horizontal", command=self.warmup_tree.xview)
        warm_x.grid(row=1, column=0, sticky="ew")
        self.warmup_tree.configure(yscrollcommand=warm_y.set, xscrollcommand=warm_x.set)
        self.warmup_tree.bind("<<TreeviewSelect>>", lambda _event: self._show_warmup_detail())
        self.warmup_tree.bind("<Double-1>", self._toggle_warmup_row)
        self.warmup_tree.bind("<Button-3>", self._show_warmup_context_menu)
        self.warmup_context_menu = tk.Menu(self, tearoff=False)

        self.warmup_detail = tk.Text(
            lower, state="disabled", wrap="word", height=8,
            font=("Microsoft YaHei UI", 10), background=COLOR_SURFACE_ALT,
        )
        self.warmup_detail.grid(row=0, column=0, sticky="nsew")
        detail_y = ttk.Scrollbar(lower, orient="vertical", command=self.warmup_detail.yview)
        detail_y.grid(row=0, column=1, sticky="ns")
        self.warmup_detail.configure(yscrollcommand=detail_y.set)

    def _build_appeal_tab(self) -> None:
        tab = self.appeal_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)
        ttk.Label(
            tab,
            text=(
                "“一键申诉”对选中（未选则全部）封禁 CID 连续执行读取官方原因 → 生成资料 → 提交；"
                "模板仍含“请填写”占位时，会按官方政策知识库自动生成完整申诉资料（业务说明、原因、"
                "整改、防复发、证明材料）；未读到明确官方政策原因的账号会跳过，不会猜测。"
                "只有读到“已提交/处理中”官方回执才记为成功；已有提交会持续保护，只有标记被拒后才允许重提。"
                "如 Google 要求重新登录、身份或安全验证，页面会保留并明确提示，完成后再次点击即可续提。"
            ),
            foreground=COLOR_WARNING,
            wraplength=1200,
        ).grid(row=0, column=0, sticky="w", pady=(0, 9))

        automation = ttk.LabelFrame(tab, text="每日全自动账户申诉", padding=8)
        automation.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        automation_controls = ttk.Frame(automation)
        automation_controls.pack(fill="x")
        ttk.Label(automation_controls, text="运行模式").pack(side="left")
        ttk.Radiobutton(
            automation_controls, text="自动（默认，每天一次）", variable=self.appeal_run_mode,
            value=APPEAL_MODE_AUTO,
        ).pack(side="left", padx=(8, 4))
        ttk.Radiobutton(
            automation_controls, text="手动", variable=self.appeal_run_mode,
            value=APPEAL_MODE_MANUAL,
        ).pack(side="left", padx=4)
        ttk.Label(automation_controls, text="执行时间").pack(side="left", padx=(16, 4))
        self.appeal_auto_time_entry = ttk.Entry(
            automation_controls, textvariable=self.appeal_run_time, width=7,
        )
        self.appeal_auto_time_entry.pack(side="left")
        ttk.Checkbutton(
            automation_controls,
            text="到时自动提交（关闭则只生成草稿）",
            variable=self.appeal_auto_submit,
        ).pack(side="left", padx=(12, 4))
        self.appeal_auto_save_button = ttk.Button(
            automation_controls, text="保存模式", command=self._save_appeal_automation_settings,
            style="Success.TButton",
        )
        self.appeal_auto_save_button.pack(side="left", padx=(8, 4))
        self.appeal_auto_run_button = ttk.Button(
            automation_controls, text="立即执行一次", command=self._run_appeal_automation_now,
            style="Write.TButton",
        )
        self.appeal_auto_run_button.pack(side="left", padx=4)
        ttk.Label(
            automation, textvariable=self.appeal_auto_status, style="Muted.TLabel",
            wraplength=1200,
        ).pack(fill="x", pady=(6, 0))

        bar = ttk.Frame(tab)
        bar.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(
            bar, text="刷新封禁 CID", command=self._refresh_appeal_rows,
            style="Accent.TButton",
        ).pack(side="left")
        self.appeal_reason_button = ttk.Button(
            bar,
            text="读取选中封禁原因（只读）",
            command=self._read_selected_suspension_reasons,
            style="Accent.TButton",
        )
        self.appeal_reason_button.pack(side="left", padx=(8, 4))
        ttk.Button(bar, text="全选封禁", command=lambda: self._select_all_appeals(True)).pack(
            side="left", padx=(8, 4)
        )
        ttk.Button(bar, text="清空", command=lambda: self._select_all_appeals(False)).pack(
            side="left", padx=4
        )
        ttk.Label(bar, text="申诉依据").pack(side="left", padx=(14, 4))
        self.appeal_basis_box = ttk.Combobox(
            bar, textvariable=self.appeal_basis, state="readonly", width=23,
            values=(
                APPEAL_BASIS_LABELS[APPEAL_BASIS_CORRECTED],
                APPEAL_BASIS_LABELS[APPEAL_BASIS_ERROR],
            ),
        )
        self.appeal_basis_box.pack(side="left")
        self.appeal_prepare_button = ttk.Button(
            bar,
            text="一键提交申诉",
            command=self._submit_selected_appeals,
            style="Write.TButton",
        )
        self.appeal_prepare_button.pack(side="left", padx=(10, 8))
        self.appeal_one_click_button = ttk.Button(
            bar,
            text="一键申诉（读原因→提交）",
            command=self._one_click_appeals,
            style="Write.TButton",
        )
        self.appeal_one_click_button.pack(side="left", padx=(0, 8))
        ttk.Button(bar, text="复制选中文案", command=self._copy_selected_appeal_text).pack(
            side="left", padx=4
        )
        ttk.Label(bar, textvariable=self.appeal_summary, style="Muted.TLabel").pack(side="right")

        split = ttk.Panedwindow(tab, orient="vertical")
        split.grid(row=3, column=0, sticky="nsew")
        upper = ttk.LabelFrame(split, text="封禁 CID（Ctrl / Shift 可多选）", padding=5)
        lower = ttk.Panedwindow(split, orient="horizontal")
        split.add(upper, weight=3)
        split.add(lower, weight=2)
        upper.columnconfigure(0, weight=1)
        upper.rowconfigure(0, weight=1)
        columns = (
            "window", "profile", "account_name", "cid", "status", "reason",
            "reason_source", "reason_read_at", "appeal_status", "updated_at",
        )
        self.appeal_tree = ttk.Treeview(
            upper, columns=columns, show="headings", selectmode="extended",
        )
        headings = {
            "window": "浏览器编号", "profile": "窗口 ID", "account_name": "Google Ads 账户",
            "cid": "CID", "status": "账号状态", "reason": "封禁原因/状态说明",
            "reason_source": "原因来源", "reason_read_at": "原因读取时间",
            "appeal_status": "本地申诉状态", "updated_at": "申诉更新时间",
        }
        widths = {
            "window": 105, "profile": 125, "account_name": 260, "cid": 125,
            "status": 80, "reason": 300, "reason_source": 200, "reason_read_at": 165,
            "appeal_status": 210, "updated_at": 165,
        }
        for key in columns:
            self.appeal_tree.heading(key, text=headings[key])
            self.appeal_tree.column(
                key, width=widths[key], minwidth=min(widths[key], 90),
                anchor="center" if key in {
                    "window", "cid", "status", "reason_read_at", "updated_at"
                } else "w",
            )
        self.appeal_tree.tag_configure(
            "banned", foreground=COLOR_DANGER, background=COLOR_DANGER_SOFT
        )
        self.appeal_tree.tag_configure(
            "pending", foreground=COLOR_WARNING, background=COLOR_WARNING_SOFT
        )
        self.appeal_tree.tag_configure(
            "resolved", foreground=COLOR_SUCCESS, background=COLOR_SUCCESS_SOFT
        )
        self.appeal_tree.grid(row=0, column=0, sticky="nsew")
        appeal_y = ttk.Scrollbar(upper, orient="vertical", command=self.appeal_tree.yview)
        appeal_y.grid(row=0, column=1, sticky="ns")
        appeal_x = ttk.Scrollbar(upper, orient="horizontal", command=self.appeal_tree.xview)
        appeal_x.grid(row=1, column=0, sticky="ew")
        self.appeal_tree.configure(yscrollcommand=appeal_y.set, xscrollcommand=appeal_x.set)
        self.appeal_tree.bind("<<TreeviewSelect>>", lambda _event: self._appeal_selection_changed())
        self.appeal_tree.bind("<Double-1>", lambda _event: self._copy_selected_appeal_text())
        self.appeal_tree.bind("<Button-3>", self._show_appeal_context_menu)
        self.appeal_context_menu = tk.Menu(self, tearoff=False)

        template_frame = ttk.LabelFrame(
            lower,
            text=(
                "本地申诉文案模板 · 仅可用 {cid} {account_name} "
                "{suspension_policy} {suspension_reason} · 未补全占位时自动生成完整资料"
            ),
            padding=6,
        )
        detail_frame = ttk.LabelFrame(lower, text="账号详情与本地申诉记录", padding=6)
        lower.add(template_frame, weight=3)
        lower.add(detail_frame, weight=2)
        for frame in (template_frame, detail_frame):
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(0, weight=1)
        self.appeal_template_box = tk.Text(
            template_frame, wrap="word", height=10, font=("Microsoft YaHei UI", 10),
        )
        self.appeal_template_box.grid(row=0, column=0, sticky="nsew")
        template_y = ttk.Scrollbar(
            template_frame, orient="vertical", command=self.appeal_template_box.yview
        )
        template_y.grid(row=0, column=1, sticky="ns")
        self.appeal_template_box.configure(yscrollcommand=template_y.set)
        template_bar = ttk.Frame(template_frame)
        template_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(template_bar, text="保存本地模板", command=self._save_appeal_template).pack(
            side="left"
        )
        ttk.Button(template_bar, text="恢复默认", command=self._reset_appeal_template).pack(
            side="left", padx=6
        )
        ttk.Label(
            template_bar, text="模板含“请填写”占位时自动按官方政策生成完整资料",
            foreground=COLOR_DANGER,
        ).pack(side="right")
        self.appeal_template_box.insert("1.0", load_appeal_template())

        self.appeal_detail = tk.Text(
            detail_frame, state="disabled", wrap="word", height=10,
            font=("Microsoft YaHei UI", 10), background=COLOR_SURFACE_ALT,
        )
        self.appeal_detail.grid(row=0, column=0, sticky="nsew")
        detail_y = ttk.Scrollbar(detail_frame, orient="vertical", command=self.appeal_detail.yview)
        detail_y.grid(row=0, column=1, sticky="ns")
        self.appeal_detail.configure(yscrollcommand=detail_y.set)
        status_bar = ttk.Frame(detail_frame)
        status_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        for status in ("已人工提交", "申诉成功", "申诉被拒"):
            ttk.Button(
                status_bar, text=f"标记{status}",
                command=lambda value=status: self._mark_appeal_status(value),
                style="Danger.TButton" if status == "申诉被拒" else "TButton",
            ).pack(side="left", padx=(0, 5))

    def _build_ad_tab(self) -> None:
        outer = self.ad_tab
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        mode_tabs = ttk.Notebook(outer)
        mode_tabs.grid(row=0, column=0, sticky="nsew")
        copy_tab = ttk.Frame(mode_tabs, padding=10)
        tab = ttk.Frame(mode_tabs, padding=10)
        snapshot_copy_tab = ttk.Frame(mode_tabs, padding=10)
        mode_tabs.add(copy_tab, text="  层级复制（同 CID / 跨 CID）  ")
        mode_tabs.add(tab, text="  修改文案创建广告  ")
        mode_tabs.add(snapshot_copy_tab, text="  跨账号快照复制  ")
        self._build_entity_copy_tab(copy_tab)
        self._build_snapshot_copy_tab(snapshot_copy_tab)
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        ttk.Label(
            tab,
            text=(
                "在同一 CID 内读取响应式展示广告作为模板；可修改文案和网址，"
                "图片可沿用参考广告，也可按横图、方图、竖图选择本地文件上传。"
                "目标余额必须大于 0；新广告强制为暂停，写后必须读回新广告 ID 和完整内容才算成功。"
            ),
            foreground=COLOR_WARNING,
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        context = ttk.Frame(tab)
        context.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(context, text="指纹浏览器窗口").pack(side="left")
        self.ad_window_box = ttk.Combobox(
            context, textvariable=self.ad_window, width=20, state="readonly"
        )
        self.ad_window_box.pack(side="left", padx=8)
        self.ad_window_box.bind("<<ComboboxSelected>>", lambda _event: self._sync_ad_cids())
        ttk.Label(context, text="CID").pack(side="left")
        self.ad_cid_box = ttk.Combobox(context, textvariable=self.ad_cid, width=29)
        self.ad_cid_box.pack(side="left", padx=8)
        self.ad_read_button = ttk.Button(
            context, text="读取广告结构（只读）", command=self._read_ad_catalog,
            style="Accent.TButton",
        )
        self.ad_read_button.pack(side="left")
        ttk.Label(
            context,
            text="参考：当前所选可用 CID / 广告系列",
            foreground=COLOR_MUTED,
        ).pack(side="right")

        split = ttk.Panedwindow(tab, orient="horizontal")
        split.grid(row=2, column=0, sticky="nsew")
        left = ttk.Frame(split, padding=(0, 0, 8, 0))
        right = ttk.Frame(split, padding=(8, 0, 0, 0))
        split.add(left, weight=2)
        split.add(right, weight=3)
        left.columnconfigure(1, weight=1)
        left.rowconfigure(7, weight=1)
        right.columnconfigure(1, weight=1)
        right.rowconfigure(4, weight=1)
        right.rowconfigure(9, weight=1)

        ttk.Label(left, text="参考广告", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        ttk.Label(left, text="广告系列").grid(row=1, column=0, sticky="w", pady=3)
        self.ad_source_campaign_box = ttk.Combobox(
            left, textvariable=self.ad_source_campaign, state="readonly"
        )
        self.ad_source_campaign_box.grid(row=1, column=1, sticky="ew", pady=3)
        self.ad_source_campaign_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._sync_ad_source_groups()
        )
        ttk.Label(left, text="广告组").grid(row=2, column=0, sticky="w", pady=3)
        self.ad_source_group_box = ttk.Combobox(
            left, textvariable=self.ad_source_group, state="readonly"
        )
        self.ad_source_group_box.grid(row=2, column=1, sticky="ew", pady=3)
        self.ad_source_group_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._sync_ad_source_ads()
        )
        ttk.Label(left, text="广告").grid(row=3, column=0, sticky="w", pady=3)
        self.ad_source_ad_box = ttk.Combobox(left, textvariable=self.ad_source_ad, state="readonly")
        self.ad_source_ad_box.grid(row=3, column=1, sticky="ew", pady=3)
        self.ad_source_ad_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._load_selected_ad_template()
        )

        ttk.Separator(left, orient="horizontal").grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=8
        )
        ttk.Label(left, text="创建位置", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        target_row = ttk.Frame(left)
        target_row.grid(row=6, column=0, columnspan=2, sticky="ew")
        target_row.columnconfigure(1, weight=1)
        ttk.Label(target_row, text="广告系列").grid(row=0, column=0, sticky="w", pady=3)
        self.ad_target_campaign_box = ttk.Combobox(
            target_row, textvariable=self.ad_target_campaign, state="readonly"
        )
        self.ad_target_campaign_box.grid(row=0, column=1, sticky="ew", pady=3)
        self.ad_target_campaign_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._sync_ad_target_groups()
        )
        ttk.Label(target_row, text="广告组").grid(row=1, column=0, sticky="w", pady=3)
        self.ad_target_group_box = ttk.Combobox(
            target_row, textvariable=self.ad_target_group, state="readonly"
        )
        self.ad_target_group_box.grid(row=1, column=1, sticky="ew", pady=3)

        catalog_frame = ttk.LabelFrame(left, text="广告层级：广告系列 → 广告组 → 多个广告", padding=5)
        catalog_frame.grid(row=7, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        catalog_frame.columnconfigure(0, weight=1)
        catalog_frame.rowconfigure(0, weight=1)
        self.ad_tree = ttk.Treeview(
            catalog_frame,
            columns=("object_type", "object_id", "status", "count"),
            displaycolumns=("object_type", "object_id", "status", "count"),
            show="tree headings",
            selectmode="browse",
            height=11,
        )
        self.ad_tree.heading("#0", text="对象名称 / 层级")
        for key, label in (
            ("object_type", "类型"), ("object_id", "对象 ID"),
            ("status", "状态"), ("count", "下级数量"),
        ):
            self.ad_tree.heading(key, text=label)
        self.ad_tree.column("#0", width=245, minwidth=175)
        self.ad_tree.column("object_type", width=75, minwidth=65, anchor="center")
        self.ad_tree.column("object_id", width=125, minwidth=100)
        self.ad_tree.column("status", width=70, anchor="center")
        self.ad_tree.column("count", width=115, minwidth=90, anchor="center")
        self.ad_tree.tag_configure("campaign", foreground=COLOR_PRIMARY_HOVER, background=COLOR_PRIMARY_SOFT)
        self.ad_tree.tag_configure("group", foreground=COLOR_WRITE, background=COLOR_WRITE_SOFT)
        self.ad_tree.tag_configure("ad_enabled", foreground=COLOR_SUCCESS, background=COLOR_SUCCESS_SOFT)
        self.ad_tree.tag_configure("ad_paused", foreground=COLOR_WARNING, background=COLOR_WARNING_SOFT)
        self.ad_tree.tag_configure("ad_other", foreground=COLOR_TEXT)
        self.ad_tree.tag_configure("empty", foreground=COLOR_MUTED)
        self.ad_tree.grid(row=0, column=0, sticky="nsew")
        ad_tree_y = ttk.Scrollbar(catalog_frame, orient="vertical", command=self.ad_tree.yview)
        ad_tree_y.grid(row=0, column=1, sticky="ns")
        ad_tree_x = ttk.Scrollbar(catalog_frame, orient="horizontal", command=self.ad_tree.xview)
        ad_tree_x.grid(row=1, column=0, sticky="ew")
        self.ad_tree.configure(yscrollcommand=ad_tree_y.set, xscrollcommand=ad_tree_x.set)
        self.ad_tree.bind("<<TreeviewSelect>>", lambda _event: self._select_ad_from_tree())

        ttk.Label(right, text="广告内容", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        ttk.Label(right, text="最终网址").grid(row=1, column=0, sticky="w", pady=3)
        self.ad_final_url = ttk.Entry(right)
        self.ad_final_url.grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Label(right, text="商家名称").grid(row=2, column=0, sticky="w", pady=3)
        self.ad_business_name = ttk.Entry(right)
        self.ad_business_name.grid(row=2, column=1, sticky="ew", pady=3)
        ttk.Label(right, text="长标题").grid(row=3, column=0, sticky="w", pady=3)
        self.ad_long_headline = ttk.Entry(right)
        self.ad_long_headline.grid(row=3, column=1, sticky="ew", pady=3)

        text_area = ttk.Frame(right)
        text_area.grid(row=4, column=0, columnspan=2, sticky="nsew", pady=(5, 0))
        text_area.columnconfigure(0, weight=1)
        text_area.columnconfigure(1, weight=1)
        text_area.rowconfigure(1, weight=1)
        ttk.Label(text_area, text="短标题（每行一条，最多 5 条，每条 30 字符）").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(text_area, text="描述（每行一条，最多 5 条，每条 90 字符）").grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )
        self.ad_headlines = tk.Text(text_area, wrap="word", height=6, font=("Consolas", 10))
        self.ad_headlines.grid(row=1, column=0, sticky="nsew", pady=(3, 0))
        self.ad_descriptions = tk.Text(text_area, wrap="word", height=6, font=("Consolas", 10))
        self.ad_descriptions.grid(row=1, column=1, sticky="nsew", padx=(8, 0), pady=(3, 0))

        self.ad_asset_summary = ttk.Label(
            right,
            text="素材：请先读取并选择参考广告",
            foreground=COLOR_MUTED,
            wraplength=620,
        )
        self.ad_asset_summary.grid(row=5, column=0, columnspan=2, sticky="nw", pady=(8, 4))

        reference_images = ttk.LabelFrame(
            right, text="参考广告图片（协议读取并缓存，双击缩略图打开原图）", padding=6,
        )
        reference_images.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(3, 4))
        reference_header = ttk.Frame(reference_images)
        reference_header.pack(fill="x", pady=(0, 4))
        self.ad_reference_image_status = ttk.Label(
            reference_header, text="请选择参考广告", foreground=COLOR_MUTED,
        )
        self.ad_reference_image_status.pack(side="left")
        self.ad_reference_canvas = tk.Canvas(
            reference_images, height=142, background=COLOR_SURFACE,
            highlightthickness=1, highlightbackground=COLOR_BORDER,
        )
        self.ad_reference_canvas.pack(fill="x", expand=True)
        reference_scroll = ttk.Scrollbar(
            reference_images, orient="horizontal", command=self.ad_reference_canvas.xview,
        )
        reference_scroll.pack(fill="x")
        self.ad_reference_canvas.configure(xscrollcommand=reference_scroll.set)
        self.ad_reference_inner = ttk.Frame(self.ad_reference_canvas)
        self.ad_reference_window = self.ad_reference_canvas.create_window(
            (0, 0), window=self.ad_reference_inner, anchor="nw",
        )
        self.ad_reference_inner.bind(
            "<Configure>",
            lambda _event: self.ad_reference_canvas.configure(
                scrollregion=self.ad_reference_canvas.bbox("all")
            ),
        )

        image_actions = ttk.LabelFrame(right, text="本地图片上传（不选择则沿用参考广告）", padding=6)
        image_actions.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(3, 4))
        plan_row = ttk.Frame(image_actions)
        plan_row.pack(fill="x", pady=(0, 6))
        ttk.Label(plan_row, text="图片方案").pack(side="left")
        self.ad_image_plan_box = ttk.Combobox(
            plan_row, textvariable=self.ad_image_plan, state="readonly", width=13,
        )
        self.ad_image_plan_box.pack(side="left", padx=(6, 4))
        ttk.Button(plan_row, text="应用图片", command=self._apply_ad_image_plan).pack(side="left")
        ttk.Label(plan_row, text="文案方案").pack(side="left", padx=(6, 0))
        self.ad_copy_plan_box = ttk.Combobox(
            plan_row, textvariable=self.ad_copy_plan, state="readonly", width=13,
        )
        self.ad_copy_plan_box.pack(side="left", padx=(6, 4))
        ttk.Button(plan_row, text="应用文案", command=self._apply_ad_copy_plan).pack(side="left")
        library_commands = ttk.Frame(image_actions)
        library_commands.pack(fill="x", pady=(0, 6))
        ttk.Button(
            library_commands, text="存当前文案", command=self._save_current_ad_copy_plan,
        ).pack(side="left")
        ttk.Button(
            library_commands, text="管理素材库", command=self._open_creative_library,
        ).pack(side="left", padx=(6, 0))
        ttk.Label(
            library_commands, text="方案仅本地保存；正式创建时才上传图片",
            foreground=COLOR_MUTED,
        ).pack(side="right")
        auto_file_row = ttk.Frame(image_actions)
        auto_file_row.pack(fill="x", pady=(0, 5))
        ttk.Button(
            auto_file_row, text="批量选择图片（自动识别尺寸）",
            command=self._select_ad_images_auto, style="Accent.TButton",
        ).pack(side="left")
        self.ad_auto_classify_summary = ttk.Label(
            auto_file_row,
            text="1.91:1 → 横图；1:1 → 方图；9:16/4:5 → 竖图",
            foreground=COLOR_MUTED,
        )
        self.ad_auto_classify_summary.pack(side="left", padx=(10, 0))
        file_row = ttk.Frame(image_actions)
        file_row.pack(fill="x")
        ttk.Button(
            file_row, text="手动横图",
            command=lambda: self._select_ad_images("landscape"),
        ).pack(side="left")
        ttk.Button(
            file_row, text="手动方图",
            command=lambda: self._select_ad_images("square"),
        ).pack(side="left", padx=(6, 0))
        ttk.Button(
            file_row, text="手动竖图",
            command=lambda: self._select_ad_images("portrait"),
        ).pack(side="left", padx=(6, 0))
        ttk.Button(
            file_row, text="清空本地图片", command=self._clear_ad_images,
        ).pack(side="left", padx=(10, 0))
        ttk.Button(
            file_row, text="存为图片方案", command=self._save_current_ad_image_plan,
        ).pack(side="left", padx=(6, 0))

        actions = ttk.Frame(right)
        actions.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(6, 4))
        self.ad_preflight_button = ttk.Button(
            actions, text="协议预检（不创建）", command=self._ad_preflight
        )
        self.ad_preflight_button.pack(side="left")
        self.ad_create_button = ttk.Button(
            actions, text="创建暂停广告（余额 > 0）", command=self._ad_create,
            style="Write.TButton",
        )
        self.ad_create_button.pack(side="left", padx=8)
        ttk.Label(
            actions,
            text="需要跨 CID 原样复制时，请使用左侧“层级复制”子页。",
            foreground=COLOR_WARNING,
        ).pack(side="right")

        preview_frame = ttk.LabelFrame(right, text="协议预检 / 写后校验", padding=5)
        preview_frame.grid(row=9, column=0, columnspan=2, sticky="nsew", pady=(4, 0))
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        self.ad_preview = tk.Text(
            preview_frame, wrap="none", height=8, state="disabled", font=("Consolas", 9)
        )
        self.ad_preview.grid(row=0, column=0, sticky="nsew")
        ad_preview_y = ttk.Scrollbar(preview_frame, orient="vertical", command=self.ad_preview.yview)
        ad_preview_y.grid(row=0, column=1, sticky="ns")
        ad_preview_x = ttk.Scrollbar(preview_frame, orient="horizontal", command=self.ad_preview.xview)
        ad_preview_x.grid(row=1, column=0, sticky="ew")
        self.ad_preview.configure(yscrollcommand=ad_preview_y.set, xscrollcommand=ad_preview_x.set)

    def _build_entity_copy_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)
        ttk.Label(
            tab,
            text=(
                "来源与目标完全分开：可复制广告系列、广告组或广告。"
                "来源和目标 CID 相同即复制自己的，不同即跨 CID；"
                "目标余额必须大于 0，副本必须回读为暂停才算成功。"
            ),
            foreground=COLOR_WARNING,
            wraplength=850,
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        contexts = ttk.Frame(tab)
        contexts.grid(row=1, column=0, sticky="ew")
        contexts.columnconfigure(0, weight=1)
        contexts.columnconfigure(1, weight=1)
        source = ttk.LabelFrame(contexts, text="来源账户", padding=8)
        target = ttk.LabelFrame(contexts, text="目标账户", padding=8)
        source.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        target.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        for frame in (source, target):
            frame.columnconfigure(1, weight=1)

        ttk.Label(source, text="指纹浏览器窗口").grid(row=0, column=0, sticky="w", pady=3)
        self.copy_source_window_box = ttk.Combobox(
            source, textvariable=self.copy_source_window, state="readonly"
        )
        self.copy_source_window_box.grid(row=0, column=1, sticky="ew", pady=3)
        self.copy_source_window_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._sync_entity_copy_cids("source")
        )
        ttk.Label(source, text="CID").grid(row=1, column=0, sticky="w", pady=3)
        self.copy_source_cid_box = ttk.Combobox(source, textvariable=self.copy_source_cid, width=27)
        self.copy_source_cid_box.grid(row=1, column=1, sticky="ew", pady=3)

        ttk.Label(target, text="指纹浏览器窗口").grid(row=0, column=0, sticky="w", pady=3)
        self.copy_target_window_box = ttk.Combobox(
            target, textvariable=self.copy_target_window, state="readonly"
        )
        self.copy_target_window_box.grid(row=0, column=1, sticky="ew", pady=3)
        self.copy_target_window_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._sync_entity_copy_cids("target")
        )
        ttk.Label(target, text="CID").grid(row=1, column=0, sticky="w", pady=3)
        self.copy_target_cid_box = ttk.Combobox(target, textvariable=self.copy_target_cid, width=27)
        self.copy_target_cid_box.grid(row=1, column=1, sticky="ew", pady=3)

        toolbar = ttk.Frame(tab)
        toolbar.grid(row=2, column=0, sticky="ew", pady=8)
        self.copy_read_button = ttk.Button(
            toolbar, text="读取来源与目标结构（只读）", command=self._read_entity_copy_catalogs,
            style="Accent.TButton",
        )
        self.copy_read_button.pack(side="left")
        ttk.Label(toolbar, text="复制层级").pack(side="left", padx=(18, 5))
        self.copy_kind_box = ttk.Combobox(
            toolbar,
            textvariable=self.copy_kind,
            values=("广告系列", "广告组", "广告"),
            state="readonly",
            width=12,
        )
        self.copy_kind_box.pack(side="left")
        self.copy_kind_box.bind("<<ComboboxSelected>>", lambda _event: self._copy_kind_changed())
        ttk.Label(
            toolbar,
            text="跨窗口：自动保存本地快照，再用目标窗口权限重新创建",
            foreground=COLOR_MUTED,
        ).pack(side="right")

        body = ttk.Panedwindow(tab, orient="horizontal")
        body.grid(row=3, column=0, sticky="nsew")
        selectors = ttk.Frame(body, padding=(0, 0, 8, 0))
        result = ttk.Frame(body, padding=(8, 0, 0, 0))
        body.add(selectors, weight=2)
        body.add(result, weight=3)
        selectors.columnconfigure(1, weight=1)
        result.columnconfigure(0, weight=1)
        result.rowconfigure(1, weight=1)

        ttk.Label(selectors, text="来源对象", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 5)
        )
        ttk.Label(selectors, text="广告系列").grid(row=1, column=0, sticky="w", pady=3)
        self.copy_source_campaign_box = ttk.Combobox(
            selectors, textvariable=self.copy_source_campaign, state="readonly"
        )
        self.copy_source_campaign_box.grid(row=1, column=1, sticky="ew", pady=3)
        self.copy_source_campaign_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._sync_copy_source_groups()
        )
        ttk.Label(selectors, text="广告组").grid(row=2, column=0, sticky="w", pady=3)
        self.copy_source_group_box = ttk.Combobox(
            selectors, textvariable=self.copy_source_group, state="readonly"
        )
        self.copy_source_group_box.grid(row=2, column=1, sticky="ew", pady=3)
        self.copy_source_group_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._sync_copy_source_ads()
        )
        ttk.Label(selectors, text="广告").grid(row=3, column=0, sticky="w", pady=3)
        self.copy_source_ad_box = ttk.Combobox(
            selectors, textvariable=self.copy_source_ad, state="readonly"
        )
        self.copy_source_ad_box.grid(row=3, column=1, sticky="ew", pady=3)

        ttk.Separator(selectors, orient="horizontal").grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=10
        )
        ttk.Label(selectors, text="粘贴位置", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(0, 5)
        )
        ttk.Label(selectors, text="广告系列").grid(row=6, column=0, sticky="w", pady=3)
        self.copy_target_campaign_box = ttk.Combobox(
            selectors, textvariable=self.copy_target_campaign, state="readonly"
        )
        self.copy_target_campaign_box.grid(row=6, column=1, sticky="ew", pady=3)
        self.copy_target_campaign_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._sync_copy_target_groups()
        )
        ttk.Label(selectors, text="广告组").grid(row=7, column=0, sticky="w", pady=3)
        self.copy_target_group_box = ttk.Combobox(
            selectors, textvariable=self.copy_target_group, state="readonly"
        )
        self.copy_target_group_box.grid(row=7, column=1, sticky="ew", pady=3)

        copy_actions = ttk.Frame(selectors)
        copy_actions.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.copy_preflight_button = ttk.Button(
            copy_actions, text="复制预检（只读）", command=self._entity_copy_preflight
        )
        self.copy_preflight_button.pack(side="left")
        self.copy_execute_button = ttk.Button(
            copy_actions, text="复制并强制暂停（余额 > 0）", command=self._entity_copy_execute,
            style="Write.TButton",
        )
        self.copy_execute_button.pack(side="left", padx=8)

        ttk.Label(result, text="协议预检 / 复制结果", font=("Microsoft YaHei UI", 10, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 5)
        )
        self.copy_preview = tk.Text(
            result, wrap="none", state="disabled", font=("Consolas", 9)
        )
        self.copy_preview.grid(row=1, column=0, sticky="nsew")
        copy_y = ttk.Scrollbar(result, orient="vertical", command=self.copy_preview.yview)
        copy_y.grid(row=1, column=1, sticky="ns")
        copy_x = ttk.Scrollbar(result, orient="horizontal", command=self.copy_preview.xview)
        copy_x.grid(row=2, column=0, sticky="ew")
        self.copy_preview.configure(yscrollcommand=copy_y.set, xscrollcommand=copy_x.set)
        self._copy_kind_changed()

    def _build_snapshot_copy_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        export_box = ttk.LabelFrame(
            tab, text="1. 导出快照（来源账户，只读读取 + 本地落盘）", padding=8
        )
        export_box.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        export_box.columnconfigure(1, weight=1)
        export_box.columnconfigure(3, weight=1)

        ttk.Label(export_box, text="来源窗口").grid(row=0, column=0, sticky="w", pady=3)
        self.snap_export_window_box = ttk.Combobox(
            export_box, textvariable=self.snap_export_window, state="readonly"
        )
        self.snap_export_window_box.grid(row=0, column=1, sticky="ew", pady=3, padx=(0, 10))
        self.snap_export_window_box.bind(
            "<<ComboboxSelected>>", lambda _event: self._sync_snap_export_cids()
        )
        ttk.Label(export_box, text="来源 CID").grid(row=0, column=2, sticky="w", pady=3)
        self.snap_export_cid_box = ttk.Combobox(
            export_box, textvariable=self.snap_export_cid, state="readonly"
        )
        self.snap_export_cid_box.grid(row=0, column=3, sticky="ew", pady=3)

        self.snap_export_read_button = ttk.Button(
            export_box, text="读取广告系列（只读）", command=self._read_snap_export_campaigns,
            style="Accent.TButton",
        )
        self.snap_export_read_button.grid(row=1, column=0, sticky="w", pady=3)
        self.snap_export_campaign_box = ttk.Combobox(
            export_box, textvariable=self.snap_export_campaign, state="readonly"
        )
        self.snap_export_campaign_box.grid(
            row=1, column=1, columnspan=3, sticky="ew", pady=3
        )

        ttk.Label(export_box, text="导出目录").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(export_box, textvariable=self.snap_export_dir).grid(
            row=2, column=1, columnspan=2, sticky="ew", pady=3, padx=(0, 10)
        )
        ttk.Button(
            export_box, text="选择目录…", command=self._choose_snap_export_dir
        ).grid(row=2, column=3, sticky="w", pady=3)

        export_actions = ttk.Frame(export_box)
        export_actions.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        self.snap_export_button = ttk.Button(
            export_actions, text="导出快照", command=self._export_snapshot,
            style="Write.TButton",
        )
        self.snap_export_button.pack(side="left")
        ttk.Label(
            export_actions, textvariable=self.snap_export_summary, foreground=COLOR_MUTED,
        ).pack(side="left", padx=(12, 0))

        import_box = ttk.LabelFrame(
            tab, text="2. 导入快照（目标账户，可批量）", padding=8
        )
        import_box.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        import_box.columnconfigure(1, weight=1)

        ttk.Label(import_box, text="快照文件").grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(
            import_box, textvariable=self.snap_import_path, state="readonly"
        ).grid(row=0, column=1, sticky="ew", pady=3, padx=(0, 10))
        ttk.Button(
            import_box, text="选择文件…", command=self._choose_snap_import_file
        ).grid(row=0, column=2, sticky="w", pady=3)
        ttk.Label(
            import_box, textvariable=self.snap_import_summary, foreground=COLOR_MUTED,
            wraplength=760, justify="left",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 6))

        ttk.Label(import_box, text="素材覆盖目录（可选）").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(import_box, textvariable=self.snap_asset_override_dir).grid(
            row=2, column=1, sticky="ew", pady=3, padx=(0, 10)
        )
        ttk.Button(
            import_box, text="选择目录…", command=self._choose_snap_asset_override_dir
        ).grid(row=2, column=2, sticky="w", pady=3)

        ttk.Checkbutton(
            import_box, text="创建后保持暂停（取消勾选=直接启用）",
            variable=self.snap_initial_status_paused,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(3, 6))

        target_box = ttk.LabelFrame(
            import_box, text="目标账户（Ctrl / Shift 可多选，双击命名标签列可编辑）", padding=6
        )
        target_box.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(0, 6))
        target_box.columnconfigure(0, weight=1)
        target_box.rowconfigure(1, weight=1)
        import_box.rowconfigure(4, weight=1)

        target_bar = ttk.Frame(target_box)
        target_bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Button(
            target_bar, text="全选", command=lambda: self._snap_select_targets(True)
        ).pack(side="left")
        ttk.Button(
            target_bar, text="清空", command=lambda: self._snap_select_targets(False)
        ).pack(side="left", padx=6)
        ttk.Button(
            target_bar, text="刷新目标列表", command=self._sync_snap_target_tree
        ).pack(side="left")
        ttk.Label(
            target_bar, textvariable=self.snap_target_summary, style="Muted.TLabel"
        ).pack(side="right")

        self.snap_target_tree = ttk.Treeview(
            target_box,
            columns=("profile", "cid", "status", "balance", "label"),
            show="headings",
            selectmode="extended",
            height=10,
        )
        for key, label in (
            ("profile", "窗口"), ("cid", "CID"), ("status", "状态"),
            ("balance", "余额"), ("label", "命名标签"),
        ):
            self.snap_target_tree.heading(key, text=label)
        self.snap_target_tree.column("profile", width=125, minwidth=95)
        self.snap_target_tree.column("cid", width=120, minwidth=110, anchor="center")
        self.snap_target_tree.column("status", width=75, minwidth=65, anchor="center")
        self.snap_target_tree.column("balance", width=105, minwidth=85, anchor="e")
        self.snap_target_tree.column("label", width=140, minwidth=100)
        self.snap_target_tree.tag_configure(
            "available", foreground=COLOR_SUCCESS, background=COLOR_SUCCESS_SOFT
        )
        self.snap_target_tree.tag_configure(
            "unavailable", foreground=COLOR_WARNING, background=COLOR_WARNING_SOFT
        )
        self.snap_target_tree.tag_configure(
            "banned", foreground=COLOR_DANGER, background=COLOR_DANGER_SOFT
        )
        self.snap_target_tree.tag_configure(
            "unknown", foreground=COLOR_MUTED, background=COLOR_SURFACE_ALT
        )
        self.snap_target_tree.grid(row=1, column=0, sticky="nsew")
        snap_target_y = ttk.Scrollbar(
            target_box, orient="vertical", command=self.snap_target_tree.yview
        )
        snap_target_y.grid(row=1, column=1, sticky="ns")
        self.snap_target_tree.configure(yscrollcommand=snap_target_y.set)
        self.snap_target_tree.bind(
            "<<TreeviewSelect>>", lambda _event: self._snap_target_selection_changed()
        )
        self.snap_target_tree.bind("<Double-1>", self._snap_target_tree_double_click)

        import_actions = ttk.Frame(import_box)
        import_actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.snap_preflight_button = ttk.Button(
            import_actions, text="复制预检（只读）", command=self._snap_import_preflight
        )
        self.snap_preflight_button.pack(side="left")
        self.snap_import_button = ttk.Button(
            import_actions, text="批量导入并创建（余额 > 0）", command=self._snap_import_execute,
            style="Write.TButton",
        )
        self.snap_import_button.pack(side="left", padx=8)

        result_box = ttk.Frame(tab)
        result_box.grid(row=2, column=0, sticky="nsew")
        result_box.columnconfigure(0, weight=1)
        result_box.rowconfigure(1, weight=1)
        ttk.Label(
            result_box, text="批量导入结果", font=("Microsoft YaHei UI", 10, "bold")
        ).grid(row=0, column=0, sticky="w", pady=(0, 5))
        self.snap_result_preview = tk.Text(
            result_box, wrap="none", state="disabled", font=("Consolas", 9)
        )
        self.snap_result_preview.grid(row=1, column=0, sticky="nsew")
        snap_result_y = ttk.Scrollbar(
            result_box, orient="vertical", command=self.snap_result_preview.yview
        )
        snap_result_y.grid(row=1, column=1, sticky="ns")
        snap_result_x = ttk.Scrollbar(
            result_box, orient="horizontal", command=self.snap_result_preview.xview
        )
        snap_result_x.grid(row=2, column=0, sticky="ew")
        self.snap_result_preview.configure(
            yscrollcommand=snap_result_y.set, xscrollcommand=snap_result_x.set
        )

        self._sync_snap_target_tree()

    def _build_schedule_tab(self) -> None:
        tab = self.schedule_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=1)
        ttk.Label(
            tab,
            text=(
                "使用 Windows 任务计划程序启动本软件。定时任务只负责打开软件，"
                "不会自动执行协议写入、广告复制、展示位置排除或一键白量。"
                "账户申诉页另有独立的每日全自动任务，可切换为只生成草稿。"
            ),
            foreground=COLOR_MUTED,
        ).grid(row=0, column=0, sticky="w", pady=(0, 12))

        daily = ttk.LabelFrame(tab, text="每日定时启动", padding=12)
        daily.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(daily, text="每天启动时间").pack(side="left")
        self.schedule_time_entry = ttk.Entry(daily, textvariable=self.schedule_time, width=9)
        self.schedule_time_entry.pack(side="left", padx=(8, 5))
        ttk.Label(daily, text="24 小时制，例如 09:30", foreground=COLOR_MUTED).pack(side="left")
        self.schedule_daily_save_button = ttk.Button(
            daily, text="保存 / 修改每日启动", command=self._save_daily_schedule,
            style="Accent.TButton",
        )
        self.schedule_daily_save_button.pack(side="right")
        ttk.Button(daily, text="停用每日启动", command=self._disable_daily_schedule).pack(
            side="right", padx=(0, 8)
        )

        logon = ttk.LabelFrame(tab, text="登录 Windows 后自动启动", padding=12)
        logon.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(
            logon,
            text="当前用户每次登录 Windows 后启动一次；不会在未登录桌面时打开界面。",
            foreground=COLOR_MUTED,
        ).pack(side="left")
        self.schedule_logon_enable_button = ttk.Button(
            logon, text="启用登录启动", command=self._enable_logon_schedule,
            style="Accent.TButton",
        )
        self.schedule_logon_enable_button.pack(side="right")
        ttk.Button(logon, text="停用登录启动", command=self._disable_logon_schedule).pack(
            side="right", padx=(0, 8)
        )

        status_frame = ttk.LabelFrame(tab, text="Windows 任务状态", padding=8)
        status_frame.grid(row=3, column=0, sticky="nsew")
        status_frame.columnconfigure(0, weight=1)
        status_frame.rowconfigure(1, weight=1)
        status_bar = ttk.Frame(status_frame)
        status_bar.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        ttk.Button(status_bar, text="刷新状态", command=self._refresh_schedule_status).pack(side="left")
        ttk.Label(
            status_bar,
            text="软件移动或改名后请重新保存，任务路径会更新为当前程序。",
            foreground=COLOR_WARNING,
        ).pack(side="right")
        columns = ("kind", "status", "time", "command", "detail")
        self.schedule_tree = ttk.Treeview(
            status_frame, columns=columns, show="headings", height=5, selectmode="browse"
        )
        headings = {
            "kind": "启动类型", "status": "状态", "time": "启动时间",
            "command": "当前程序路径", "detail": "说明",
        }
        widths = {"kind": 145, "status": 95, "time": 100, "command": 540, "detail": 320}
        for key in columns:
            self.schedule_tree.heading(key, text=headings[key])
            self.schedule_tree.column(
                key, width=widths[key], minwidth=min(widths[key], 90),
                anchor="center" if key in {"kind", "status", "time"} else "w",
            )
        self.schedule_tree.tag_configure(
            "enabled", foreground=COLOR_SUCCESS, background=COLOR_SUCCESS_SOFT
        )
        self.schedule_tree.tag_configure(
            "disabled", foreground=COLOR_MUTED, background=COLOR_SURFACE_ALT
        )
        self.schedule_tree.grid(row=1, column=0, sticky="nsew")
        schedule_x = ttk.Scrollbar(
            status_frame, orient="horizontal", command=self.schedule_tree.xview
        )
        schedule_x.grid(row=2, column=0, sticky="ew")
        self.schedule_tree.configure(xscrollcommand=schedule_x.set)

    def _refresh_schedule_status(self) -> None:
        try:
            statuses = scheduler.schedule_status()
            current_command = scheduler.application_command()
        except Exception as exc:
            self._log(f"定时启动状态读取失败：{exc}")
            return
        self.schedule_tree.delete(*self.schedule_tree.get_children())
        rows = (
            ("daily", "每日定时启动", statuses["daily"]),
            ("logon", "登录后启动", statuses["logon"]),
            ("appeal", "每日账户申诉", statuses["appeal"]),
        )
        for key, label, status in rows:
            if key == "daily" and status.exists and status.start_time:
                self.schedule_time.set(status.start_time)
            self.schedule_tree.insert(
                "", "end",
                values=(
                    label,
                    "已启用" if status.exists else "未启用",
                    status.start_time or ("登录时" if key == "logon" and status.exists else "—"),
                    status.command or (current_command if not status.exists else "未能读取"),
                    status.detail or ("任务已在 Windows 中创建" if status.exists else "尚未创建"),
                ),
                tags=(("enabled" if status.exists else "disabled"),),
            )

    def _schedule_change(self, label: str, action: Callable[[], Any]) -> None:
        try:
            action()
            self._refresh_schedule_status()
            self._log(f"{label}：成功")
            messagebox.showinfo("定时启动", f"{label}成功")
        except Exception as exc:
            self._refresh_schedule_status()
            self._log(f"{label}：失败 - {exc}")
            messagebox.showerror("定时启动失败", str(exc))

    def _save_daily_schedule(self) -> None:
        start_time = self.schedule_time.get().strip()
        self._schedule_change(
            "保存每日定时启动", lambda: scheduler.create_daily_task(start_time)
        )

    def _disable_daily_schedule(self) -> None:
        self._schedule_change(
            "停用每日定时启动", lambda: scheduler.delete_task(scheduler.DAILY_TASK_NAME)
        )

    def _enable_logon_schedule(self) -> None:
        self._schedule_change("启用登录启动", scheduler.create_logon_task)

    def _disable_logon_schedule(self) -> None:
        self._schedule_change(
            "停用登录启动", lambda: scheduler.delete_task(scheduler.LOGON_TASK_NAME)
        )

    def _build_report_tab(self) -> None:
        tab = self.report_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(bar, text="刷新", command=self._refresh_reports).pack(side="left")
        ttk.Button(bar, text="查看详情", command=self._show_report_details).pack(side="left", padx=8)
        ttk.Button(bar, text="复制失败原因", command=self._copy_report_error).pack(side="left")
        ttk.Button(bar, text="打开原始 JSON", command=self._open_report).pack(side="left", padx=8)
        ttk.Button(bar, text="删除记录", command=self._delete_report, style="Danger.TButton").pack(side="left")
        ttk.Button(bar, text="打开目录", command=lambda: os.startfile(data_dir() / "runs")).pack(side="left", padx=8)

        split = ttk.Panedwindow(tab, orient="vertical")
        split.grid(row=1, column=0, sticky="nsew")
        list_frame = ttk.Frame(split)
        detail_frame = ttk.LabelFrame(split, text="运行详情", padding=6)
        split.add(list_frame, weight=3)
        split.add(detail_frame, weight=2)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        detail_frame.columnconfigure(0, weight=1)
        detail_frame.rowconfigure(0, weight=1)

        columns = ("time", "mode", "success", "profile", "accounts", "error", "file")
        self.report_tree = ttk.Treeview(
            list_frame,
            columns=columns,
            displaycolumns=("time", "mode", "success", "profile", "accounts", "error"),
            show="headings",
            selectmode="browse",
        )
        for key, label in (("time", "时间"), ("mode", "模式"), ("success", "结果"), ("profile", "浏览器窗口"), ("accounts", "执行 CID"), ("error", "失败原因"), ("file", "文件")):
            self.report_tree.heading(key, text=label)
        self.report_tree.column("time", width=165, minwidth=145)
        self.report_tree.column("mode", width=85, minwidth=70, anchor="center")
        self.report_tree.column("success", width=115, minwidth=85, anchor="center")
        self.report_tree.column("profile", width=205)
        self.report_tree.column("accounts", width=80, minwidth=70, anchor="center")
        self.report_tree.column("error", width=520, minwidth=240)
        self.report_tree.column("file", width=0, stretch=False)
        self.report_tree.tag_configure("report_success", foreground=COLOR_SUCCESS, background=COLOR_SUCCESS_SOFT)
        self.report_tree.tag_configure("report_warning", foreground=COLOR_WARNING, background=COLOR_WARNING_SOFT)
        self.report_tree.tag_configure("report_failure", foreground=COLOR_DANGER, background=COLOR_DANGER_SOFT)
        self.report_tree.grid(row=0, column=0, sticky="nsew")
        report_y = ttk.Scrollbar(list_frame, orient="vertical", command=self.report_tree.yview)
        report_y.grid(row=0, column=1, sticky="ns")
        report_x = ttk.Scrollbar(list_frame, orient="horizontal", command=self.report_tree.xview)
        report_x.grid(row=1, column=0, sticky="ew")
        self.report_tree.configure(yscrollcommand=report_y.set, xscrollcommand=report_x.set)
        self.report_tree.bind("<<TreeviewSelect>>", lambda _event: self._show_report_details())
        self.report_tree.bind("<Double-1>", self._report_double_click)
        self.report_tree.bind("<Button-3>", self._show_report_context_menu)
        self.report_context_menu = tk.Menu(self, tearoff=False)

        self.report_detail = tk.Text(
            detail_frame,
            state="disabled",
            wrap="word",
            font=("Microsoft YaHei UI", 10),
            background=COLOR_SURFACE_ALT,
            height=10,
        )
        self.report_detail.grid(row=0, column=0, sticky="nsew")
        detail_y = ttk.Scrollbar(detail_frame, orient="vertical", command=self.report_detail.yview)
        detail_y.grid(row=0, column=1, sticky="ns")
        self.report_detail.configure(yscrollcommand=detail_y.set)
        self.report_items: dict[str, tuple[Path, dict[str, Any]]] = {}

    # ------------------------------------------------------------------ GA4
    def _build_ga4_tab(self) -> None:
        tab = self.ga4_tab
        self.ga4_results: queue.Queue = queue.Queue()
        self.ga4_accounts: list[dict[str, Any]] = []
        self.ga4_loaded_context: tuple[str, str] | None = None
        self.ga4_current: dict[str, str] = {}

        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(0, 10))
        ttk.Label(top, text="指纹浏览器窗口").pack(side="left")
        self.ga4_profile = ttk.Combobox(top, width=22, state="readonly")
        self.ga4_profile.pack(side="left", padx=(8, 4))
        self._sync_ga4_windows()
        ttk.Button(top, text="读取 GA4 账号", command=self._ga4_connect, style="Accent.TButton").pack(side="left", padx=8)
        ttk.Button(top, text="创建媒体资源", command=self._ga4_create_property_dialog).pack(side="left", padx=4)
        self.ga4_status = ttk.Label(top, text="未连接", foreground=COLOR_MUTED)
        self.ga4_status.pack(side="left", padx=14)

        body = ttk.Panedwindow(tab, orient="horizontal")
        body.pack(fill="both", expand=True)

        left = ttk.LabelFrame(body, text="GA4 账号 → 媒体资源", padding=8)
        body.add(left, weight=1)
        self.ga4_tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self.ga4_tree.pack(fill="both", expand=True)
        self.ga4_tree.bind("<<TreeviewSelect>>", self._ga4_on_select)

        right = ttk.Frame(body)
        body.add(right, weight=3)
        self.ga4_sub = ttk.Notebook(right)
        self.ga4_sub.pack(fill="both", expand=True)

        # 数据流
        stream_tab = ttk.Frame(self.ga4_sub, padding=10)
        self.ga4_sub.add(stream_tab, text=" 数据流 ")
        stream_bar = ttk.Frame(stream_tab)
        stream_bar.pack(fill="x", pady=(0, 8))
        ttk.Button(stream_bar, text="刷新", command=self._ga4_refresh_streams).pack(side="left")
        ttk.Button(stream_bar, text="新增网站数据流", command=self._ga4_create_stream_dialog, style="Accent.TButton").pack(side="left", padx=8)
        self.ga4_streams = ttk.Treeview(
            stream_tab, columns=("stream_id", "name", "url", "mid"), show="headings", height=8,
        )
        for key, title, width in (
            ("stream_id", "数据流 ID", 110), ("name", "名称", 160),
            ("url", "网址", 240), ("mid", "衡量 ID", 110),
        ):
            self.ga4_streams.heading(key, text=title)
            self.ga4_streams.column(key, width=width, anchor="w")
        self.ga4_streams.pack(fill="both", expand=True)

        # Google Ads 关联
        link_tab = ttk.Frame(self.ga4_sub, padding=10)
        self.ga4_sub.add(link_tab, text=" Google Ads 关联 ")
        ttk.Label(link_tab, text="已关联").pack(anchor="w")
        link_bar = ttk.Frame(link_tab)
        link_bar.pack(fill="x", pady=4)
        ttk.Button(link_bar, text="刷新", command=self._ga4_refresh_links).pack(side="left")
        ttk.Button(link_bar, text="解除选中关联", command=self._ga4_delete_link, style="Danger.TButton").pack(side="left", padx=8)
        self.ga4_links = ttk.Treeview(
            link_tab, columns=("cid", "name", "linked_by"), show="headings", height=4,
        )
        for key, title, width in (("cid", "CID", 110), ("name", "账户名称", 220), ("linked_by", "关联者", 180)):
            self.ga4_links.heading(key, text=title)
            self.ga4_links.column(key, width=width, anchor="w")
        self.ga4_links.pack(fill="x")
        ttk.Separator(link_tab).pack(fill="x", pady=10)
        ttk.Label(link_tab, text="可关联的 Google Ads 账户（选中后建立关联）").pack(anchor="w")
        linkable_bar = ttk.Frame(link_tab)
        linkable_bar.pack(fill="x", pady=4)
        ttk.Button(linkable_bar, text="读取可关联账户", command=self._ga4_refresh_linkable).pack(side="left")
        ttk.Button(linkable_bar, text="关联选中账户", command=self._ga4_create_link, style="Accent.TButton").pack(side="left", padx=8)
        self.ga4_linkable = ttk.Treeview(
            link_tab, columns=("cid", "name", "type"), show="headings", height=7,
        )
        for key, title, width in (("cid", "CID", 110), ("name", "账户名称", 240), ("type", "类型", 70)):
            self.ga4_linkable.heading(key, text=title)
            self.ga4_linkable.column(key, width=width, anchor="w")
        self.ga4_linkable.pack(fill="both", expand=True)

        # 转化报表
        conv_tab = ttk.Frame(self.ga4_sub, padding=10)
        self.ga4_sub.add(conv_tab, text=" 转化报表 ")
        conv_bar = ttk.Frame(conv_tab)
        conv_bar.pack(fill="x", pady=(0, 8))
        ttk.Label(conv_bar, text="日期").pack(side="left")
        self.ga4_range = ttk.Combobox(conv_tab, width=10, state="readonly",
                                      values=["今天", "昨天", "近 7 天", "近 30 天"])
        self.ga4_range.current(2)
        self.ga4_range.pack(in_=conv_bar, side="left", padx=(6, 12))
        ttk.Label(conv_bar, text="维度").pack(side="left")
        self.ga4_dim = ttk.Combobox(conv_tab, width=12, state="readonly",
                                    values=["按事件", "按日期"])
        self.ga4_dim.current(0)
        self.ga4_dim.pack(in_=conv_bar, side="left", padx=(6, 12))
        ttk.Button(conv_bar, text="查询转化", command=self._ga4_query_conversions, style="Accent.TButton").pack(side="left")
        ttk.Button(conv_bar, text="实时在线", command=self._ga4_query_realtime).pack(side="left", padx=8)
        self.ga4_realtime_text = tk.StringVar(value="")
        ttk.Label(conv_bar, textvariable=self.ga4_realtime_text, foreground=COLOR_PRIMARY).pack(side="left", padx=6)
        self.ga4_conv = ttk.Treeview(conv_tab, show="headings", height=12)
        self.ga4_conv.pack(fill="both", expand=True)

        # 关键事件
        ke_tab = ttk.Frame(self.ga4_sub, padding=10)
        self.ga4_sub.add(ke_tab, text=" 关键事件 ")
        ke_bar = ttk.Frame(ke_tab)
        ke_bar.pack(fill="x", pady=(0, 8))
        ttk.Button(ke_bar, text="刷新", command=self._ga4_refresh_key_events).pack(side="left")
        ttk.Button(ke_bar, text="标记新关键事件", command=self._ga4_mark_key_event, style="Accent.TButton").pack(side="left", padx=8)
        ttk.Button(ke_bar, text="取消选中标记", command=self._ga4_unmark_key_event, style="Danger.TButton").pack(side="left")
        ttk.Label(ke_bar, text="关键事件即转化；标记后才会计入 conversions 指标", foreground=COLOR_MUTED).pack(side="left", padx=12)
        self.ga4_key_events = ttk.Treeview(ke_tab, columns=("event_name", "entity_id"), show="headings", height=10)
        self.ga4_key_events.heading("event_name", text="事件名称")
        self.ga4_key_events.column("event_name", width=220, anchor="w")
        self.ga4_key_events.heading("entity_id", text="实体 ID")
        self.ga4_key_events.column("entity_id", width=140, anchor="w")
        self.ga4_key_events.pack(fill="both", expand=True)

        # 事件规则（从现有事件派生新事件，可直接标记为关键事件）
        er_tab = ttk.Frame(self.ga4_sub, padding=10)
        self.ga4_sub.add(er_tab, text=" 事件规则 ")
        er_bar = ttk.Frame(er_tab)
        er_bar.pack(fill="x", pady=(0, 8))
        ttk.Button(er_bar, text="刷新", command=self._ga4_refresh_event_rules).pack(side="left")
        ttk.Button(er_bar, text="新建派生规则", command=self._ga4_event_rule_dialog, style="Accent.TButton").pack(side="left", padx=8)
        ttk.Button(er_bar, text="删除选中", command=self._ga4_delete_event_rule, style="Danger.TButton").pack(side="left")
        ttk.Label(er_bar, text="例：page_location 包含 /thanks 时派生 generate_lead 事件", foreground=COLOR_MUTED).pack(side="left", padx=12)
        self.ga4_event_rules = ttk.Treeview(er_tab, columns=("dest", "conds"), show="headings", height=10)
        self.ga4_event_rules.heading("dest", text="派生事件名")
        self.ga4_event_rules.column("dest", width=200, anchor="w")
        self.ga4_event_rules.heading("conds", text="触发条件")
        self.ga4_event_rules.column("conds", width=380, anchor="w")
        self.ga4_event_rules.pack(fill="both", expand=True)

        # 受众
        aud_tab = ttk.Frame(self.ga4_sub, padding=10)
        self.ga4_sub.add(aud_tab, text=" 受众群体 ")
        aud_bar = ttk.Frame(aud_tab)
        aud_bar.pack(fill="x", pady=(0, 8))
        ttk.Button(aud_bar, text="刷新", command=self._ga4_refresh_audiences).pack(side="left")
        ttk.Button(aud_bar, text="新建事件受众", command=self._ga4_audience_dialog, style="Accent.TButton").pack(side="left", padx=8)
        ttk.Button(aud_bar, text="删除选中", command=self._ga4_delete_audience, style="Danger.TButton").pack(side="left")
        ttk.Label(aud_bar, text="受众通过 Google Ads 关联自动同步为再营销名单", foreground=COLOR_MUTED).pack(side="left", padx=12)
        self.ga4_audiences = ttk.Treeview(aud_tab, columns=("name", "desc", "days"), show="headings", height=10)
        for key, title, width in (("name", "名称", 200), ("desc", "说明", 300), ("days", "资格天数", 80)):
            self.ga4_audiences.heading(key, text=title)
            self.ga4_audiences.column(key, width=width, anchor="w")
        self.ga4_audiences.pack(fill="both", expand=True)

        # 自定义定义
        cd_tab = ttk.Frame(self.ga4_sub, padding=10)
        self.ga4_sub.add(cd_tab, text=" 自定义定义 ")
        cd_bar = ttk.Frame(cd_tab)
        cd_bar.pack(fill="x", pady=(0, 8))
        ttk.Button(cd_bar, text="刷新", command=self._ga4_refresh_custom_defs).pack(side="left")
        ttk.Button(cd_bar, text="新建维度", command=lambda: self._ga4_custom_def_dialog(0), style="Accent.TButton").pack(side="left", padx=(8, 4))
        ttk.Button(cd_bar, text="新建指标", command=lambda: self._ga4_custom_def_dialog(1), style="Accent.TButton").pack(side="left", padx=4)
        ttk.Button(cd_bar, text="归档选中", command=self._ga4_archive_custom_def, style="Danger.TButton").pack(side="left", padx=8)
        self.ga4_custom_defs = ttk.Treeview(cd_tab, columns=("name", "param", "type", "desc"), show="headings", height=10)
        for key, title, width in (("name", "名称", 160), ("param", "参数名", 160), ("type", "类型", 60), ("desc", "说明", 240)):
            self.ga4_custom_defs.heading(key, text=title)
            self.ga4_custom_defs.column(key, width=width, anchor="w")
        self.ga4_custom_defs.pack(fill="both", expand=True)

        # 设置
        set_tab = ttk.Frame(self.ga4_sub, padding=10)
        self.ga4_sub.add(set_tab, text=" 设置 ")
        set_bar = ttk.Frame(set_tab)
        set_bar.pack(fill="x", pady=(0, 10))
        ttk.Button(set_bar, text="读取设置", command=self._ga4_refresh_settings).pack(side="left")
        retention_box = ttk.LabelFrame(set_tab, text="数据保留", padding=10)
        retention_box.pack(fill="x", pady=6)
        ttk.Label(retention_box, text="事件级数据保留期").pack(side="left")
        self.ga4_retention = ttk.Combobox(retention_box, width=12, state="readonly", values=["2 个月", "14 个月"])
        self.ga4_retention.pack(side="left", padx=8)
        ttk.Button(retention_box, text="保存", command=self._ga4_save_retention, style="Success.TButton").pack(side="left")
        signals_box = ttk.LabelFrame(set_tab, text="Google 信号（再营销/ demographics 数据源）", padding=10)
        signals_box.pack(fill="x", pady=6)
        self.ga4_signals_text = tk.StringVar(value="未读取")
        ttk.Label(signals_box, textvariable=self.ga4_signals_text).pack(side="left")
        ttk.Button(signals_box, text="启用", command=lambda: self._ga4_save_signals(True), style="Success.TButton").pack(side="left", padx=8)
        ttk.Button(signals_box, text="停用", command=lambda: self._ga4_save_signals(False), style="Danger.TButton").pack(side="left")
        attr_box = ttk.LabelFrame(set_tab, text="归因设置（只读展示）", padding=10)
        attr_box.pack(fill="x", pady=6)
        self.ga4_attr_text = tk.StringVar(value="未读取")
        ttk.Label(attr_box, textvariable=self.ga4_attr_text, wraplength=560, justify="left").pack(anchor="w")

        # Measurement Protocol 密钥
        mp_tab = ttk.Frame(self.ga4_sub, padding=10)
        self.ga4_sub.add(mp_tab, text=" MP 密钥 ")
        mp_bar = ttk.Frame(mp_tab)
        mp_bar.pack(fill="x", pady=(0, 8))
        ttk.Button(mp_bar, text="刷新", command=self._ga4_refresh_mp).pack(side="left")
        ttk.Button(mp_bar, text="创建密钥", command=self._ga4_mp_dialog, style="Accent.TButton").pack(side="left", padx=8)
        ttk.Button(mp_bar, text="删除选中", command=self._ga4_delete_mp, style="Danger.TButton").pack(side="left")
        self.ga4_mp = ttk.Treeview(mp_tab, columns=("name", "secret", "stream"), show="headings", height=10)
        for key, title, width in (("name", "昵称", 160), ("secret", "密钥值", 220), ("stream", "数据流 ID", 120)):
            self.ga4_mp.heading(key, text=title)
            self.ga4_mp.column(key, width=width, anchor="w")
        self.ga4_mp.pack(fill="both", expand=True)

        # 其他产品关联（Search Console / BigQuery）
        pl_tab = ttk.Frame(self.ga4_sub, padding=10)
        self.ga4_sub.add(pl_tab, text=" 产品关联 ")
        pl_bar = ttk.Frame(pl_tab)
        pl_bar.pack(fill="x", pady=(0, 8))
        ttk.Label(pl_bar, text="产品").pack(side="left")
        self.ga4_product = ttk.Combobox(pl_tab, width=18, state="readonly",
                                        values=["Search Console", "BigQuery"])
        self.ga4_product.current(0)
        self.ga4_product.pack(in_=pl_bar, side="left", padx=(6, 10))
        ttk.Button(pl_bar, text="读取关联与可关联实体", command=self._ga4_refresh_product_links, style="Accent.TButton").pack(side="left")
        ttk.Label(pl_bar, text="当前账号无 SC 资源/GCP 项目时列表为空", foreground=COLOR_MUTED).pack(side="left", padx=12)
        ttk.Label(pl_tab, text="已关联").pack(anchor="w")
        self.ga4_plinks = ttk.Treeview(pl_tab, columns=("info",), show="headings", height=4)
        self.ga4_plinks.heading("info", text="关联信息")
        self.ga4_plinks.column("info", width=640, anchor="w")
        self.ga4_plinks.pack(fill="x")
        ttk.Separator(pl_tab).pack(fill="x", pady=8)
        ttk.Label(pl_tab, text="可关联实体").pack(anchor="w")
        self.ga4_plinkable = ttk.Treeview(pl_tab, columns=("info",), show="headings", height=6)
        self.ga4_plinkable.heading("info", text="实体信息")
        self.ga4_plinkable.column("info", width=640, anchor="w")
        self.ga4_plinkable.pack(fill="both", expand=True)

    def _ga4_run(self, label: str, fn: Callable[[GA4Runner], Any]) -> None:
        browser_type, profile = self._ga4_selected_window()
        if not profile:
            messagebox.showwarning("缺少窗口", "请先在批量任务页添加指纹浏览器窗口", parent=self)
            return
        context = (browser_type, profile)
        if label != "读取 GA4 账号" and getattr(self, "ga4_loaded_context", None) != context:
            self.ga4_loaded_context = None
            self.ga4_accounts = []
            tree = self.__dict__.get("ga4_tree")
            if tree is not None:
                tree.delete(*tree.get_children(""))
            messagebox.showerror(
                "GA4 数据已失效",
                "指纹浏览器窗口已切换，请先重新读取 GA4 账号。",
                parent=self,
            )
            return
        self._start_task_status(label)
        self.ga4_status.configure(text=f"正在{label}……")

        def worker() -> None:
            try:
                with GA4Runner(profile, browser_type=browser_type) as runner:
                    result = fn(runner)
                self.ga4_results.put((label, context, True, result))
            except Exception as exc:
                self.ga4_results.put((label, context, False, str(exc)))

        threading.Thread(target=worker, daemon=True).start()
        self.after(150, self._poll_ga4_results)

    def _poll_ga4_results(self) -> None:
        try:
            label, context, ok, payload = self.ga4_results.get_nowait()
        except queue.Empty:
            if self.winfo_exists():
                self.after(150, self._poll_ga4_results)
            return
        if ok:
            if label == "读取 GA4 账号":
                self.ga4_loaded_context = context
            self._finish_task_status(True, f"{label}完成")
            self.ga4_status.configure(text=f"{label}完成")
            self._ga4_handle_result(label, payload)
        else:
            self._finish_task_status(False, f"{label}失败", str(payload))
            self.ga4_status.configure(text=f"{label}失败", foreground=COLOR_DANGER)
            messagebox.showwarning(f"GA4 {label}失败", str(payload), parent=self)
        if self.winfo_exists():
            self.after(150, self._poll_ga4_results)

    def _ga4_handle_result(self, label: str, payload: Any) -> None:
        if label == "读取 GA4 账号":
            self.ga4_accounts = payload
            self.ga4_tree.delete(*self.ga4_tree.get_children(""))
            for acc in self.ga4_accounts:
                acc_iid = self.ga4_tree.insert(
                    "", "end", text=f"{acc['account_name']}（{acc['account_id']}）",
                    values=(acc["account_id"], ""),
                )
                for prop in acc["properties"]:
                    self.ga4_tree.insert(
                        acc_iid, "end",
                        text=f"{prop['property_name']}（{prop['property_id']}）",
                        values=(acc["account_id"], prop["property_id"]),
                    )
                self.ga4_tree.item(acc_iid, open=True)
        elif label == "载入全部":
            if isinstance(payload.get("streams"), list):
                self._ga4_handle_result("读取数据流", payload["streams"])
            if isinstance(payload.get("links"), list):
                self._ga4_handle_result("读取关联", payload["links"])
            if isinstance(payload.get("linkable"), list):
                self._ga4_handle_result("读取可关联账户", payload["linkable"])
            if isinstance(payload.get("key_events"), list):
                self._ga4_handle_result("读取关键事件", payload["key_events"])
            if isinstance(payload.get("audiences"), list):
                self._ga4_handle_result("读取受众", payload["audiences"])
            if isinstance(payload.get("custom_defs"), list):
                self._ga4_handle_result("读取自定义定义", payload["custom_defs"])
            if isinstance(payload.get("settings"), dict) and "error" not in payload["settings"]:
                self._ga4_handle_result("读取设置", payload["settings"])
            if isinstance(payload.get("mp_secrets"), list):
                self._ga4_handle_result("读取MP密钥", payload["mp_secrets"])
        elif label == "读取数据流":
            self.ga4_streams.delete(*self.ga4_streams.get_children(""))
            for s in payload:
                self.ga4_streams.insert("", "end", values=(s["stream_id"], s["name"], s["url"], s["measurement_id"]))
        elif label in ("创建媒体资源", "创建数据流"):
            self._ga4_connect()
        elif label == "读取关联":
            self.ga4_links.delete(*self.ga4_links.get_children(""))
            for link in payload:
                self.ga4_links.insert(
                    "", "end", iid=link["link_uid"],
                    values=(link["cid"], link["account_name"], link["linked_by"]),
                )
        elif label == "读取可关联账户":
            self.ga4_linkable.delete(*self.ga4_linkable.get_children(""))
            self._ga4_linkable_cache = {acc["cid"]: acc["internal_customer_id"] for acc in payload}
            for acc in payload:
                self.ga4_linkable.insert(
                    "", "end",
                    values=(acc["cid"], acc["account_name"], "经理账号" if acc["is_mcc"] == "True" else "账号"),
                )
        elif label in ("建立关联", "解除关联"):
            self._ga4_refresh_links()
            self._ga4_refresh_linkable()
        elif label == "读取关键事件":
            self.ga4_key_events.delete(*self.ga4_key_events.get_children(""))
            for e in payload:
                self.ga4_key_events.insert("", "end", values=(e["event_name"], e["entity_id"]))
        elif label in ("标记关键事件", "取消关键事件"):
            self._ga4_refresh_key_events()
        elif label == "读取受众":
            self.ga4_audiences.delete(*self.ga4_audiences.get_children(""))
            for a in payload:
                self.ga4_audiences.insert("", "end", iid=a["entity_id"], values=(a["name"], a["description"], a["membership_days"]))
        elif label in ("创建受众", "删除受众"):
            self._ga4_refresh_audiences()
        elif label == "读取自定义定义":
            self.ga4_custom_defs.delete(*self.ga4_custom_defs.get_children(""))
            for d in payload:
                self.ga4_custom_defs.insert("", "end", values=(d["display_name"], d["param_name"], d["type"], d["description"]))
        elif label in ("创建自定义定义", "归档自定义定义"):
            self._ga4_refresh_custom_defs()
        elif label == "读取设置":
            self._ga4_render_settings(payload)
        elif label in ("设置数据保留", "设置信号"):
            self._ga4_refresh_settings()
        elif label == "读取MP密钥":
            self.ga4_mp.delete(*self.ga4_mp.get_children(""))
            for s in payload:
                self.ga4_mp.insert("", "end", iid=s["entity_id"], values=(s["name"], s["secret"], s["stream_id"]))
        elif label in ("创建MP密钥", "删除MP密钥"):
            self._ga4_refresh_mp()
        elif label == "读取事件规则":
            self.ga4_event_rules.delete(*self.ga4_event_rules.get_children(""))
            for rule in payload:
                self.ga4_event_rules.insert("", "end", iid=rule["entity_id"],
                                            values=(rule["destination_event"], rule["conditions"]))
        elif label in ("创建事件规则", "删除事件规则"):
            self._ga4_refresh_event_rules()
        elif label == "查询实时":
            self.ga4_realtime_text.set(f"当前在线 {payload.get('active_users', '0')} 人")
        elif label == "读取产品关联":
            self.ga4_plinks.delete(*self.ga4_plinks.get_children(""))
            for item in payload.get("links", []):
                self.ga4_plinks.insert("", "end", values=(self._ga4_brief(item),))
            self.ga4_plinkable.delete(*self.ga4_plinkable.get_children(""))
            for item in payload.get("linkable", []):
                self.ga4_plinkable.insert("", "end", values=(self._ga4_brief(item),))
        elif label == "查询转化":
            rows = payload["rows"]
            dims = payload["dimensions"]
            mets = payload["metrics"]
            self.ga4_conv.delete(*self.ga4_conv.get_children(""))
            self.ga4_conv["columns"] = dims + mets
            for key in dims + mets:
                self.ga4_conv.heading(key, text=key)
                self.ga4_conv.column(key, width=130, anchor="w")
            for row in rows:
                self.ga4_conv.insert("", "end", values=[row.get(k, "") for k in dims + mets])
            self.ga4_status.configure(text=f"查询转化完成：{len(rows)} 行")

    def _ga4_selected_property(self) -> dict[str, str]:
        selection = self.ga4_tree.selection()
        if not selection:
            raise ValueError("请先在左侧选择 GA4 媒体资源")
        values = self.ga4_tree.item(selection[0], "values")
        if not values or len(values) < 2 or not values[1]:
            raise ValueError("请选择媒体资源节点（不是账号节点）")
        account_id, property_id = str(values[0]), str(values[1])
        return {
            "account_id": account_id,
            "property_id": property_id,
            "dataset": f"a{account_id}p{property_id}",
        }

    def _ga4_on_select(self, _event: Any = None) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError:
            return
        if current.get("dataset") == self.ga4_current.get("dataset"):
            return
        self.ga4_current = current
        dataset = current["dataset"]

        def load_all(runner: GA4Runner) -> dict[str, Any]:
            result: dict[str, Any] = {}
            domains = {
                "streams": lambda: runner.list_streams(dataset),
                "links": lambda: runner.list_ads_links(dataset),
                "linkable": lambda: runner.list_linkable_ads_accounts(dataset),
                "key_events": lambda: runner.list_key_events(dataset),
                "audiences": lambda: runner.list_audiences(dataset),
                "custom_defs": lambda: runner.list_custom_definitions(dataset),
                "settings": lambda: {
                    "retention": runner.get_data_retention(dataset),
                    "signals": runner.get_google_signals(dataset),
                    "attribution": runner.get_attribution(dataset),
                },
                "mp_secrets": lambda: runner.list_mp_secrets(dataset),
            }
            for key, fn in domains.items():
                try:
                    result[key] = fn()
                except Exception as exc:
                    result[key] = {"error": str(exc)[:200]}
            return result

        self._ga4_run("载入全部", load_all)

    def _ga4_connect(self) -> None:
        self._ga4_run("读取 GA4 账号", lambda runner: runner.list_accounts())

    def _ga4_refresh_streams(self) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError:
            return
        self._ga4_run("读取数据流", lambda runner: runner.list_streams(current["dataset"]))

    def _ga4_refresh_links(self) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError:
            return
        self._ga4_run("读取关联", lambda runner: runner.list_ads_links(current["dataset"]))

    def _ga4_refresh_linkable(self) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError:
            return
        self._ga4_run("读取可关联账户", lambda runner: runner.list_linkable_ads_accounts(current["dataset"]))

    def _ga4_create_property_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("创建 GA4 媒体资源")
        dialog.transient(self)
        dialog.grab_set()
        form = ttk.Frame(dialog, padding=16)
        form.pack(fill="both", expand=True)

        ttk.Label(form, text="名称（必填）").grid(row=0, column=0, sticky="w", pady=4)
        name_entry = ttk.Entry(form, width=30)
        name_entry.grid(row=0, column=1, pady=4, padx=(8, 0))

        ttk.Label(form, text="币种").grid(row=1, column=0, sticky="w", pady=4)
        currency = ttk.Combobox(form, width=27, state="readonly", values=["USD", "CNY", "EUR", "JPY"])
        currency.current(0)
        currency.grid(row=1, column=1, pady=4, padx=(8, 0))

        ttk.Label(form, text="报告时区").grid(row=2, column=0, sticky="w", pady=4)
        timezone = ttk.Combobox(form, width=27, state="readonly",
                                values=["Asia/Shanghai", "Asia/Hong_Kong", "America/Los_Angeles", "America/New_York", "Europe/London"])
        timezone.current(0)
        timezone.grid(row=2, column=1, pady=4, padx=(8, 0))

        ttk.Label(form, text="行业类别").grid(row=3, column=0, sticky="w", pady=4)
        industry = ttk.Combobox(form, width=27, state="readonly", values=list(INDUSTRIES.keys()))
        industry.current(0)
        industry.grid(row=3, column=1, pady=4, padx=(8, 0))

        ttk.Label(form, text="业务规模").grid(row=4, column=0, sticky="w", pady=4)
        size = ttk.Combobox(form, width=27, state="readonly", values=list(BUSINESS_SIZES.keys()))
        size.current(0)
        size.grid(row=4, column=1, pady=4, padx=(8, 0))

        ttk.Label(form, text="业务目标").grid(row=5, column=0, sticky="w", pady=4)
        objective = ttk.Combobox(form, width=27, state="readonly", values=list(BUSINESS_OBJECTIVES.keys()))
        objective.current(0)
        objective.grid(row=5, column=1, pady=4, padx=(8, 0))

        accounts = [(a["account_id"], a["account_name"]) for a in self.ga4_accounts]
        if not accounts:
            messagebox.showwarning("未读取账号", "请先点击“读取 GA4 账号”", parent=self)
            dialog.destroy()
            return
        ttk.Label(form, text="所属账号").grid(row=6, column=0, sticky="w", pady=4)
        account_box = ttk.Combobox(form, width=27, state="readonly",
                                   values=[f"{name}（{acc_id}）" for acc_id, name in accounts])
        account_box.current(0)
        account_box.grid(row=6, column=1, pady=4, padx=(8, 0))

        def submit() -> None:
            name = name_entry.get().strip()
            if not name:
                messagebox.showerror("输入有误", "请填写媒体资源名称", parent=dialog)
                return
            acc_id = accounts[account_box.current()][0]
            dialog.destroy()
            self._ga4_run(
                "创建媒体资源",
                lambda runner: runner.create_property(
                    acc_id, name,
                    currency=currency.get(), timezone=timezone.get(),
                    industry=INDUSTRIES[industry.get()],
                    business_size=BUSINESS_SIZES[size.get()],
                    objectives=[BUSINESS_OBJECTIVES[objective.get()]],
                ),
            )

        buttons = ttk.Frame(form)
        buttons.grid(row=7, column=0, columnspan=2, pady=(14, 0), sticky="e")
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="创建", command=submit, style="Accent.TButton").pack(side="right", padx=8)
        name_entry.focus_set()

    def _ga4_create_stream_dialog(self) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        name = simpledialog.askstring("新增网站数据流", "数据流名称：", parent=self)
        if not name or not name.strip():
            return
        url = simpledialog.askstring("新增网站数据流", "网站网址（例如 example.com）：", parent=self)
        if not url or not url.strip():
            return
        dataset = current["dataset"]
        self._ga4_run("创建数据流", lambda runner: runner.create_stream(dataset, name.strip(), url.strip()))

    def _ga4_create_link(self) -> None:
        selection = self.ga4_linkable.selection()
        if not selection:
            messagebox.showwarning("未选择账户", "请先在“可关联账户”列表中选中一行", parent=self)
            return
        values = self.ga4_linkable.item(selection[0], "values")
        cid = re.sub(r"\D", "", str(values[0]))
        name = str(values[1])
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        if not messagebox.askyesno(
            "建立关联",
            f"把媒体资源 {current['property_id']} 关联到 Google Ads 账户 {name}（{cid}）？",
            parent=self,
        ):
            return
        linkable = getattr(self, "_ga4_linkable_cache", {})
        internal_id = linkable.get(cid, "")
        dataset = current["dataset"]
        self._ga4_run(
            "建立关联",
            lambda runner: runner.create_ads_link(dataset, cid, name, internal_id),
        )

    def _ga4_delete_link(self) -> None:
        selection = self.ga4_links.selection()
        if not selection:
            messagebox.showwarning("未选择关联", "请先在“已关联”列表中选中一行", parent=self)
            return
        link_uid = selection[0]
        values = self.ga4_links.item(link_uid, "values")
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        if not messagebox.askyesno("解除关联", f"确定解除与 {values[1]}（{values[0]}）的关联？", parent=self):
            return
        dataset = current["dataset"]
        self._ga4_run("解除关联", lambda runner: runner.delete_ads_link(dataset, link_uid))

    def _ga4_query_conversions(self) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        today = datetime.now().date()
        choice = self.ga4_range.get()
        if choice == "今天":
            start = end = today
        elif choice == "昨天":
            start = end = today - timedelta(days=1)
        elif choice == "近 30 天":
            start, end = today - timedelta(days=29), today
        else:
            start, end = today - timedelta(days=6), today
        by_day = self.ga4_dim.get() == "按日期"
        dimensions = ["nth_day"] if by_day else ["event_name"]
        metrics = ["conversions", "event_count"]
        dataset, property_id = current["dataset"], current["property_id"]

        def task(runner: GA4Runner) -> dict[str, Any]:
            if by_day:
                rows = runner.query_conversions_by_day(dataset, property_id, start.isoformat(), end.isoformat())
            else:
                rows = runner.query_conversions_by_event(dataset, property_id, start.isoformat(), end.isoformat())
            return {"rows": rows, "dimensions": dimensions, "metrics": metrics}

        self._ga4_run("查询转化", task)

    # ------------------------------------------------------------ GA4 关键事件
    def _ga4_refresh_key_events(self) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError:
            return
        self._ga4_run("读取关键事件", lambda runner: runner.list_key_events(current["dataset"]))

    def _ga4_mark_key_event(self) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        name = simpledialog.askstring("标记关键事件", "事件名称（如 purchase、generate_lead）：", parent=self)
        if not name or not name.strip():
            return
        dataset = current["dataset"]
        self._ga4_run("标记关键事件", lambda runner: runner.mark_key_event(dataset, name.strip()))

    def _ga4_unmark_key_event(self) -> None:
        selection = self.ga4_key_events.selection()
        if not selection:
            messagebox.showwarning("未选择", "请先选中一个关键事件", parent=self)
            return
        event_name = str(self.ga4_key_events.item(selection[0], "values")[0])
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        if not messagebox.askyesno("取消标记", f"确定把 {event_name} 从关键事件中移除？", parent=self):
            return
        dataset = current["dataset"]
        self._ga4_run("取消关键事件", lambda runner: runner.unmark_key_event(dataset, event_name))

    # ------------------------------------------------------------ GA4 受众
    def _ga4_refresh_audiences(self) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError:
            return
        self._ga4_run("读取受众", lambda runner: runner.list_audiences(current["dataset"]))

    def _ga4_audience_dialog(self) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title("新建事件受众")
        dialog.transient(self)
        dialog.grab_set()
        form = ttk.Frame(dialog, padding=16)
        form.pack(fill="both", expand=True)
        entries: dict[str, ttk.Entry] = {}
        for row, (key, label, default) in enumerate([
            ("name", "受众名称（必填）", ""),
            ("event", "事件名（必填，如 session_start）", "session_start"),
            ("lookback", "回溯天数（如 7 天）", "7"),
            ("days", "成员资格有效期（天）", "30"),
            ("desc", "说明", ""),
        ]):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            entry = ttk.Entry(form, width=28)
            entry.grid(row=row, column=1, pady=4, padx=(8, 0))
            if default:
                entry.insert(0, default)
            entries[key] = entry
        ttk.Label(form, text="条件方向").grid(row=5, column=0, sticky="w", pady=4)
        direction = ttk.Combobox(form, width=25, state="readonly",
                                 values=["近 N 天未触发（找回流失）", "近 N 天触发过（活跃用户）"])
        direction.current(0)
        direction.grid(row=5, column=1, pady=4, padx=(8, 0))
        ttk.Label(
            form, text="条件：过去 N 天内事件触发次数 = 0（未触发）或 > 0（触发过）的用户",
            foreground=COLOR_MUTED, wraplength=340, justify="left",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))

        def submit() -> None:
            name = entries["name"].get().strip()
            event = entries["event"].get().strip()
            if not name or not event:
                messagebox.showerror("输入有误", "受众名称和事件名不能为空", parent=dialog)
                return
            try:
                lookback = int(entries["lookback"].get() or "7")
                days = int(entries["days"].get() or "30")
            except ValueError:
                messagebox.showerror("输入有误", "天数必须是整数", parent=dialog)
                return
            dataset = current["dataset"]
            desc = entries["desc"].get().strip()
            op = 3 if direction.current() == 0 else 5
            dialog.destroy()
            self._ga4_run(
                "创建受众",
                lambda runner: runner.create_event_audience(
                    dataset, name, event, days=days, description=desc, lookback_days=lookback,
                    event_count_op=op,
                ),
            )

        buttons = ttk.Frame(form)
        buttons.grid(row=7, column=0, columnspan=2, pady=(14, 0), sticky="e")
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right")
        ttk.Button(buttons, text="创建", command=submit, style="Accent.TButton").pack(side="right", padx=8)
        entries["name"].focus_set()

    def _ga4_delete_audience(self) -> None:
        selection = self.ga4_audiences.selection()
        if not selection:
            messagebox.showwarning("未选择", "请先选中一个受众", parent=self)
            return
        entity_id = selection[0]
        name = str(self.ga4_audiences.item(entity_id, "values")[0])
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        if not messagebox.askyesno("删除受众", f"确定删除受众 {name}？", parent=self):
            return
        dataset = current["dataset"]
        self._ga4_run("删除受众", lambda runner: runner.delete_audience(dataset, entity_id))

    # -------------------------------------------------------- GA4 自定义定义
    def _ga4_refresh_custom_defs(self) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError:
            return
        self._ga4_run("读取自定义定义", lambda runner: runner.list_custom_definitions(current["dataset"]))

    def _ga4_custom_def_dialog(self, definition_type: int) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        kind = "指标" if definition_type == 1 else "维度"
        name = simpledialog.askstring(f"新建自定义{kind}", f"{kind}名称：", parent=self)
        if not name or not name.strip():
            return
        param = simpledialog.askstring(f"新建自定义{kind}", "事件参数名（字母/数字/下划线）：", parent=self)
        if not param or not param.strip():
            return
        dataset = current["dataset"]
        self._ga4_run(
            "创建自定义定义",
            lambda runner: runner.create_custom_definition(
                dataset, name.strip(), param.strip(), definition_type=definition_type,
            ),
        )

    def _ga4_archive_custom_def(self) -> None:
        selection = self.ga4_custom_defs.selection()
        if not selection:
            messagebox.showwarning("未选择", "请先选中一条自定义定义", parent=self)
            return
        display_name = str(self.ga4_custom_defs.item(selection[0], "values")[0])
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        if not messagebox.askyesno("归档", f"确定归档 {display_name}？归档后报告不再可用该项。", parent=self):
            return
        dataset = current["dataset"]
        self._ga4_run("归档自定义定义", lambda runner: runner.archive_custom_definition(dataset, display_name))

    # ------------------------------------------------------------ GA4 设置
    def _ga4_refresh_settings(self) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        dataset = current["dataset"]

        def load(runner: GA4Runner) -> dict[str, Any]:
            return {
                "retention": runner.get_data_retention(dataset),
                "signals": runner.get_google_signals(dataset),
                "attribution": runner.get_attribution(dataset),
            }

        self._ga4_run("读取设置", load)

    def _ga4_render_settings(self, payload: dict[str, Any]) -> None:
        retention = payload.get("retention") or {}
        months = retention.get("eventLevelDataTtlMonthsValue")
        if months in (2, 14):
            self.ga4_retention.set("14 个月" if months == 14 else "2 个月")
        signals = payload.get("signals") or {}
        self.ga4_signals_text.set("已启用" if signals.get("isEnabled") else "已停用")
        attr = payload.get("attribution") or {}
        model = attr.get("defaultEventAttributionModel")
        lookback = (attr.get("defaultEngagementConversionsLookbackWindows") or {}).get(
            "clickThroughConversionLookbackWindowSizeDays", "?")
        self.ga4_attr_text.set(f"归因模型代码：{model}；互动转化回溯期：{lookback} 天")

    def _ga4_save_retention(self) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        months = 14 if "14" in self.ga4_retention.get() else 2
        dataset = current["dataset"]
        self._ga4_run("设置数据保留", lambda runner: runner.set_data_retention(dataset, months))

    def _ga4_save_signals(self, enabled: bool) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        if enabled and not messagebox.askyesno(
            "启用 Google 信号", "启用即代表你确认 Google 信号条款，确定启用？", parent=self,
        ):
            return
        dataset = current["dataset"]
        self._ga4_run("设置信号", lambda runner: runner.set_google_signals(dataset, enabled))

    # -------------------------------------------------------- GA4 MP 密钥
    def _ga4_refresh_mp(self) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError:
            return
        self._ga4_run("读取MP密钥", lambda runner: runner.list_mp_secrets(current["dataset"]))

    def _ga4_mp_dialog(self) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        streams = [self.ga4_streams.item(iid, "values") for iid in self.ga4_streams.get_children("")]
        if not streams:
            messagebox.showwarning("没有数据流", "请先在“数据流”页刷新并确认至少有一个数据流", parent=self)
            return
        name = simpledialog.askstring(
            "创建 MP 密钥",
            "密钥昵称：\n（将创建在数据流 " + str(streams[0][1]) + " 上；多个数据流时请先只保留目标流再创建）",
            parent=self,
        )
        if not name or not name.strip():
            return
        dataset = current["dataset"]
        stream_id = str(streams[0][0])
        self._ga4_run("创建MP密钥", lambda runner: runner.create_mp_secret(dataset, stream_id, name.strip()))

    def _ga4_delete_mp(self) -> None:
        selection = self.ga4_mp.selection()
        if not selection:
            messagebox.showwarning("未选择", "请先选中一个密钥", parent=self)
            return
        entity_id = selection[0]
        name = str(self.ga4_mp.item(entity_id, "values")[0])
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        if not messagebox.askyesno("删除密钥", f"确定删除 MP 密钥 {name}？删除后对应服务端打点就失效。", parent=self):
            return
        dataset = current["dataset"]
        self._ga4_run("删除MP密钥", lambda runner: runner.delete_mp_secret(dataset, entity_id))

    # -------------------------------------------------------- GA4 产品关联
    @staticmethod
    def _ga4_brief(item: Any) -> str:
        """把关联/实体对象压成一行可读摘要。"""
        if not isinstance(item, dict):
            return str(item)[:200]
        parts = []
        for key in ("name", "displayName", "propertyName", "descriptiveName", "id",
                    "projectId", "externalCustomerId", "streamId", "linkUid", "type"):
            value = item.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={value}")
        if not parts:
            for sub in item.values():
                if isinstance(sub, dict):
                    return GGAdsApp._ga4_brief(sub)
        return "  ".join(str(p)[:60] for p in parts)[:240]

    def _ga4_refresh_product_links(self) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        link_type = 6 if self.ga4_product.get() == "Search Console" else 3
        dataset = current["dataset"]

        def load(runner: GA4Runner) -> dict[str, Any]:
            return {
                "links": runner.list_product_links(dataset, link_type),
                "linkable": runner.list_linkable_entities(dataset, link_type),
            }

        self._ga4_run("读取产品关联", load)

    # -------------------------------------------------------- GA4 事件规则
    def _ga4_rule_stream_id(self) -> str:
        selection = self.ga4_streams.selection()
        if selection:
            return str(self.ga4_streams.item(selection[0], "values")[0])
        children = self.ga4_streams.get_children("")
        if children:
            return str(self.ga4_streams.item(children[0], "values")[0])
        raise ValueError("请先在“数据流”页刷新出数据流")

    def _ga4_refresh_event_rules(self) -> None:
        try:
            current = self._ga4_selected_property()
            stream_id = self._ga4_rule_stream_id()
        except ValueError:
            return
        dataset = current["dataset"]
        self._ga4_run("读取事件规则", lambda runner: runner.list_event_rules(dataset, stream_id))

    def _ga4_event_rule_dialog(self) -> None:
        try:
            current = self._ga4_selected_property()
            stream_id = self._ga4_rule_stream_id()
        except ValueError as exc:
            messagebox.showwarning("缺少前提", str(exc), parent=self)
            return
        dest = simpledialog.askstring("新建派生规则", "派生事件名（字母/数字/下划线，如 generate_lead）：", parent=self)
        if not dest or not dest.strip():
            return
        cond = simpledialog.askstring("新建派生规则", "触发条件（格式 字段=值，如 page_location=/thanks）：", parent=self)
        if not cond or "=" not in cond:
            return
        field, _, value = cond.partition("=")
        dataset = current["dataset"]
        self._ga4_run(
            "创建事件规则",
            lambda runner: runner.create_event_rule(
                dataset, stream_id, dest.strip(),
                [{"field": field.strip(), "value": value.strip()}],
            ),
        )

    def _ga4_delete_event_rule(self) -> None:
        selection = self.ga4_event_rules.selection()
        if not selection:
            messagebox.showwarning("未选择", "请先选中一条规则", parent=self)
            return
        rule_id = selection[0]
        dest = str(self.ga4_event_rules.item(rule_id, "values")[0])
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        if not messagebox.askyesno("删除规则", f"确定删除派生规则 {dest}？", parent=self):
            return
        dataset = current["dataset"]
        self._ga4_run("删除事件规则", lambda runner: runner.delete_event_rule(dataset, rule_id))

    # ------------------------------------------------------------ GA4 实时
    def _ga4_query_realtime(self) -> None:
        try:
            current = self._ga4_selected_property()
        except ValueError as exc:
            messagebox.showwarning("未选择媒体资源", str(exc), parent=self)
            return
        dataset, property_id = current["dataset"], current["property_id"]
        self._ga4_run("查询实时", lambda runner: runner.query_realtime(dataset, property_id))

    def _build_help_tab(self) -> None:
        tab = self.help_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        text = tk.Text(tab, wrap="word", font=("Microsoft YaHei UI", 10), padx=14, pady=12, spacing1=2, spacing3=5)
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tab, orient="vertical", command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", HELP_TEXT)
        text.configure(state="disabled")

    # ----- state and common helpers -------------------------------------------------
    @staticmethod
    def _select_row_at_pointer(tree: ttk.Treeview, event: tk.Event) -> str:
        iid = tree.identify_row(event.y)
        if iid:
            tree.selection_set(iid)
            tree.focus(iid)
        return iid

    @staticmethod
    def _popup_menu(menu: tk.Menu, event: tk.Event) -> None:
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy_text(self, value: str, label: str = "内容") -> None:
        value = str(value)
        if not value:
            messagebox.showinfo("没有可复制内容", f"当前{label}为空")
            return
        self.clipboard_clear()
        self.clipboard_append(value)
        self._log(f"已复制{label}：{value}")

    def _toggle_task_columns(self) -> None:
        compact = (
            "browser_type", "running_status", "account_name", "cid_display",
            "status", "balance", "daily_spend", "dictionary", "note",
        )
        detailed = (
            "browser_type", "running_status", "window_number", "window_name", "account_type",
            "account_name", "cid_display", "status", "balance", "daily_spend", "balance_type",
            "balance_status", "daily_spend_status", "dictionary", "note",
            "ocid", "parent_mcc_display", "depth", "balance_detail", "balance_updated_at",
            "daily_spend_detail", "daily_spend_date", "daily_spend_updated_at",
            "status_detail", "created_at", "updated_at",
        )
        self.task_tree.configure(
            displaycolumns=detailed if self.show_task_details.get() else compact
        )

    @staticmethod
    def _format_parent_mcc(value: str) -> str:
        formatted = []
        for item in str(value or "").split("/"):
            item = item.strip()
            if not item:
                continue
            try:
                formatted.append(format_cid(item))
            except ValueError:
                formatted.append(item)
        return " / ".join(formatted)

    def _iter_task_rows(self, parent_iid: str):
        for iid in self.task_tree.get_children(parent_iid):
            yield iid
            yield from self._iter_task_rows(iid)

    def _window_rows(self, window_iid: str) -> list[str]:
        return list(self._iter_task_rows(window_iid))

    def _window_for_item(self, iid: str) -> str:
        current = iid
        while self.task_tree.parent(current):
            current = self.task_tree.parent(current)
        return current

    @staticmethod
    def _task_row_tag(account_type: str, status: str) -> str:
        if str(account_type).strip().upper() == "MCC":
            return "manager"
        return {
            "可用": "available",
            "不可用": "unavailable",
            "封禁": "banned",
        }.get(status, "unknown")

    @staticmethod
    def _cid_status_group(status: str) -> str:
        return {
            "可用": "available",
            "不可用": "unavailable",
            "封禁": "banned",
        }.get(str(status).strip(), "unknown")

    @staticmethod
    def _cid_selector_label(cid: str, status: str) -> str:
        display_status = str(status).strip() or "状态未知"
        return f"{format_cid(cid)} · {display_status}"

    @staticmethod
    def _cid_from_selector(value: str) -> str:
        """Return a pure protocol CID from a status-annotated selector value."""
        return normalize_cid(str(value).partition("·")[0].strip())

    def _cid_selector_rows(self, window_iid: str | None) -> list[tuple[str, str]]:
        if not window_iid:
            return []
        browser_type = normalize_browser_type(
            self.task_tree.set(window_iid, "browser_type")
        )
        profile = str(self.task_tree.set(window_iid, "profile") or "").strip()
        profile_key = window_key(browser_type, profile)
        cache = load_account_cache()
        if cache.fresh and profile_key in cache.accounts:
            return [
                (cid, str(snapshot.get("status") or "状态未知"))
                for cid, snapshot in sorted(cache.accounts[profile_key].items())
                if str(snapshot.get("account_type") or "CID").strip().upper() != "MCC"
                and not bool(snapshot.get("is_manager"))
            ]
        rows: list[tuple[str, str]] = []
        seen: set[str] = set()
        for iid in self._window_rows(window_iid):
            cid = str(self.task_tree.set(iid, "cid") or "").strip()
            account_type = str(self.task_tree.set(iid, "account_type") or "CID").strip().upper()
            if not cid or account_type == "MCC" or cid in seen:
                continue
            seen.add(cid)
            rows.append((cid, str(self.task_tree.set(iid, "status") or "状态未知")))
        return rows

    def _configure_cid_selector(
        self,
        box: ttk.Combobox,
        variable: tk.StringVar,
        rows: list[tuple[str, str]],
        *,
        preferred_cid: str = "",
    ) -> None:
        try:
            current_cid = self._cid_from_selector(variable.get())
        except ValueError:
            current_cid = ""
        entries: list[tuple[str, str, str]] = []
        for cid, status in rows:
            try:
                raw_cid = normalize_cid(cid)
            except ValueError:
                continue
            entries.append((raw_cid, self._cid_selector_label(raw_cid, status), status))
        labels = [label for _cid, label, _status in entries]
        label_by_cid = {cid: label for cid, label, _status in entries}
        status_by_cid = {cid: status for cid, _label, status in entries}
        status_by_label = {label: status for _cid, label, status in entries}
        setattr(box, "_cid_status_by_label", status_by_label)
        setattr(box, "_cid_status_by_cid", status_by_cid)
        setattr(box, "_cid_selector_labels", labels)
        box.configure(
            values=labels,
            postcommand=lambda selected_box=box: self._colorize_cid_dropdown(selected_box),
        )
        preferred = ""
        if preferred_cid:
            try:
                preferred = normalize_cid(preferred_cid)
            except ValueError:
                preferred = ""
        selected_cid = (
            current_cid if current_cid in label_by_cid
            else preferred if preferred in label_by_cid
            else next(
                (cid for cid, _label, status in entries if status == "可用"),
                entries[0][0] if entries else "",
            )
        )
        variable.set(label_by_cid.get(selected_cid, ""))
        if not getattr(box, "_cid_status_bound", False):
            box.bind(
                "<<ComboboxSelected>>",
                lambda _event, selected_box=box, selected_var=variable:
                    self._apply_cid_selector_style(selected_box, selected_var),
                add="+",
            )
            setattr(box, "_cid_status_bound", True)
        self._apply_cid_selector_style(box, variable)

    def _apply_cid_selector_style(self, box: ttk.Combobox, variable: tk.StringVar) -> None:
        value = variable.get()
        status = getattr(box, "_cid_status_by_label", {}).get(value, "")
        if not status:
            try:
                cid = self._cid_from_selector(value)
            except ValueError:
                cid = ""
            status = getattr(box, "_cid_status_by_cid", {}).get(cid, "")
        style_name = {
            "available": "CidAvailable.TCombobox",
            "unavailable": "CidUnavailable.TCombobox",
            "banned": "CidBanned.TCombobox",
            "unknown": "CidUnknown.TCombobox",
        }[self._cid_status_group(status)]
        box.configure(style=style_name)

    def _colorize_cid_dropdown(self, box: ttk.Combobox) -> None:
        palette = {
            "available": (COLOR_SUCCESS, COLOR_SUCCESS_SOFT),
            "unavailable": (COLOR_WARNING, COLOR_WARNING_SOFT),
            "banned": (COLOR_DANGER, COLOR_DANGER_SOFT),
            "unknown": (COLOR_MUTED, COLOR_SURFACE_ALT),
        }
        try:
            popdown = box.tk.call("ttk::combobox::PopdownWindow", box._w)
            listbox = f"{popdown}.f.l"
            labels = getattr(box, "_cid_selector_labels", [])
            status_by_label = getattr(box, "_cid_status_by_label", {})
            for index, label in enumerate(labels):
                group = self._cid_status_group(status_by_label.get(label, ""))
                foreground, background = palette[group]
                box.tk.call(
                    listbox,
                    "itemconfigure",
                    index,
                    "-foreground",
                    foreground,
                    "-background",
                    background,
                    "-selectforeground",
                    "#FFFFFF",
                    "-selectbackground",
                    foreground,
                )
        except tk.TclError:
            # Some older Tk builds do not expose per-row listbox colors.  The
            # annotated status text and selected-field color still remain active.
            return

    def _task_selection_changed(self, _event: tk.Event | None = None) -> None:
        selected = self.task_tree.selection()
        is_manager = bool(
            selected
            and str(self.task_tree.set(selected[0], "account_type") or "").strip().upper() == "MCC"
        )
        background = COLOR_MANAGER if is_manager else COLOR_PRIMARY
        ttk.Style(self).map(
            "Task.Treeview",
            background=[("selected", background)],
            foreground=[("selected", "#FFFFFF")],
        )

    def _apply_task_row_semantics(self, iid: str) -> None:
        account_type = str(self.task_tree.set(iid, "account_type") or "CID").strip().upper()
        if account_type == "MCC":
            original_status = str(self.task_tree.set(iid, "status") or "").strip()
            detail = "MCC 管理账户仅用于管理下级账户，不能创建或投放广告。"
            if original_status and original_status not in {"未刷新", "状态未知", "MCC（不可投放）"}:
                detail += f" 协议账户状态：{original_status}。"
            self.task_tree.set(iid, "status", "MCC（不可投放）")
            self.task_tree.set(iid, "status_detail", detail)
            self.task_tree.set(iid, "balance", "不适用")
            self.task_tree.set(iid, "balance_type", "MCC 管理账户")
            self.task_tree.set(iid, "balance_status", "不适用")
            self.task_tree.set(iid, "balance_detail", "MCC 不具备广告投放余额")
            self.task_tree.set(iid, "daily_spend", "不适用")
            self.task_tree.set(iid, "daily_spend_status", "不适用")
            self.task_tree.set(iid, "daily_spend_detail", "MCC 不产生广告投放消耗")
            self.task_tree.item(iid, text="MCC · 管理账户", open=True, tags=("manager",))
            return
        status = str(self.task_tree.set(iid, "status") or "")
        self.task_tree.item(iid, text="CID", tags=(self._task_row_tag(account_type, status),))

    def _rebuild_account_hierarchy(self, window_iid: str) -> None:
        rows = self._window_rows(window_iid)
        if not rows:
            return
        # Flatten deepest rows first, then rebuild from parent_mcc metadata.
        for iid in reversed(rows):
            self.task_tree.move(iid, window_iid, "end")
        rows = list(self.task_tree.get_children(window_iid))
        by_cid = {
            str(self.task_tree.set(iid, "cid")): iid
            for iid in rows
            if self.task_tree.set(iid, "cid")
        }
        def depth_of(iid: str) -> int:
            try:
                return int(self.task_tree.set(iid, "depth") or 0)
            except ValueError:
                return 0
        for iid in sorted(rows, key=depth_of):
            parent_cids = [
                value.strip()
                for value in str(self.task_tree.set(iid, "parent_mcc")).split("/")
                if value.strip()
            ]
            parent_iid = next(
                (by_cid[cid] for cid in parent_cids if cid in by_cid and by_cid[cid] != iid),
                window_iid,
            )
            self.task_tree.move(iid, parent_iid, "end")
            self._apply_task_row_semantics(iid)
        for parent_iid in [window_iid, *self._window_rows(window_iid)]:
            children = list(self.task_tree.get_children(parent_iid))
            children.sort(key=lambda iid: (
                0 if self.task_tree.set(iid, "account_type") == "MCC" else 1,
                str(self.task_tree.set(iid, "account_name")).casefold(),
                str(self.task_tree.set(iid, "cid")),
            ))
            for index, iid in enumerate(children):
                self.task_tree.move(iid, parent_iid, index)

    def _load_state(self) -> None:
        state = load_ui_state()
        self.ad_image_plan.set(str(state.get("ad_image_plan") or "（不使用图片方案）"))
        self.ad_copy_plan.set(str(state.get("ad_copy_plan") or "（不使用文案方案）"))
        self.campaign_run_today.set(bool(state.get("campaign_run_today", True)))
        self.campaign_white_name_prefix.set(
            str(state.get("campaign_white_name_prefix") or "App-1").strip() or "App-1"
        )
        try:
            saved_white_limit = Decimal(
                str(state.get("campaign_white_budget_limit") or "2.00")
            )
            if not saved_white_limit.is_finite() or saved_white_limit < 0:
                raise ValueError
            self.campaign_white_budget_limit.set(str(saved_white_limit))
        except Exception:
            self.campaign_white_budget_limit.set("2.00")
        try:
            saved_budget = validate_daily_budget(
                state.get("warmup_daily_budget") or DEFAULT_DAILY_BUDGET
            )
            self.warmup_daily_budget.set(f"{saved_budget:.2f}")
        except ValueError:
            self.warmup_daily_budget.set(f"{DEFAULT_DAILY_BUDGET:.2f}")
        saved_warmup_source = str(state.get("warmup_source_cid") or "").strip()
        if saved_warmup_source:
            try:
                self.warmup_source_cid.set(format_cid(normalize_cid(saved_warmup_source)))
            except ValueError:
                self.warmup_source_cid.set("")
        selected = state.get("enabled")
        if isinstance(selected, list):
            for key, variable in self.enabled.items():
                variable.set(key in selected)
        windows = state.get("windows")
        if isinstance(windows, list):
            for window in windows:
                profile = str((window or {}).get("profile_id") or "").strip()
                if not profile:
                    continue
                browser_type = normalize_browser_type(
                    (window or {}).get("browser_type")
                )
                window_iid = self._insert_window(
                    profile,
                    str((window or {}).get("window_number") or "").strip(),
                    browser_type=browser_type,
                    window_name=str((window or {}).get("window_name") or "").strip(),
                    running_status=str((window or {}).get("running_status") or "").strip(),
                )
                for row in (window or {}).get("cids") or []:
                    try:
                        self._insert_cid(
                            window_iid,
                            normalize_cid(str(row.get("cid") or "")),
                            str(row.get("dictionary") or self._plan_names()[0]),
                            note=str(row.get("note") or ""),
                            account_type=str(row.get("account_type") or ""),
                            account_name=str(row.get("account_name") or ""),
                            ocid=str(row.get("ocid") or ""),
                            parent_mcc=str(row.get("parent_mcc") or ""),
                            depth=str(row.get("depth") if row.get("depth") is not None else ""),
                            status=str(row.get("status") or "未刷新"),
                            balance=str(row.get("balance") or "未读取"),
                            balance_type=str(row.get("balance_type") or ""),
                            balance_status=str(row.get("balance_status") or "未读取"),
                            balance_detail=str(row.get("balance_detail") or "点击“读取 CID 余额”读取"),
                            balance_updated_at=str(row.get("balance_updated_at") or ""),
                            daily_spend=str(row.get("daily_spend") or "未读取"),
                            daily_spend_status=str(row.get("daily_spend_status") or "未读取"),
                            daily_spend_detail=str(row.get("daily_spend_detail") or "点击“读取日期消耗”读取"),
                            daily_spend_date=str(row.get("daily_spend_date") or ""),
                            daily_spend_updated_at=str(row.get("daily_spend_updated_at") or ""),
                            status_detail=str(row.get("status_detail") or "点击“刷新账户状态”读取"),
                            created_at=str(row.get("created_at") or ""),
                            updated_at=str(row.get("updated_at") or ""),
                        )
                    except Exception:
                        continue
                self._rebuild_account_hierarchy(window_iid)
        else:
            # v0.2/v0.3 single-window state migration.
            profile = str(state.get("profile_id") or "").strip()
            text = str(state.get("cids") or "")
            try:
                if profile:
                    window_iid = self._insert_window(profile, browser_type=ADSPOWER)
                    default = str(state.get("default_dictionary") or (self._plan_names()[0] if self._plan_names() else "默认"))
                    for assignment in parse_assignments(text, default):
                        self._insert_cid(window_iid, assignment.cid, assignment.dictionary)
                    self._rebuild_account_hierarchy(window_iid)
            except Exception:
                pass
        cache = load_account_cache()
        if cache.fresh:
            configured_windows = {
                window_key(
                    self.task_tree.set(iid, "browser_type"),
                    self.task_tree.set(iid, "profile"),
                ): iid
                for iid in self.task_tree.get_children("")
            }
            cached_windows = {
                key: metadata
                for key, iid in configured_windows.items()
                if (
                    metadata := account_cache_window_metadata(
                        cache,
                        str(self.task_tree.set(iid, "profile")),
                        browser_type=str(self.task_tree.set(iid, "browser_type")),
                    )
                ) is not None
            }
            self._apply_window_metadata(
                cached_windows,
                persist_state=False,
                persist_cache=False,
            )
            cached_accounts = {
                profile: rows
                for profile, rows in cache.accounts.items()
                if profile in configured_windows
            }
            self._apply_account_statuses(cached_accounts, persist_state=False)
            self._log(
                f"启动已加载统一账户缓存：{sum(len(rows) for rows in cached_accounts.values())} 个账户；"
                "未触发账户刷新"
            )
        else:
            self._sync_remote_cids()
            if cache.status != "missing":
                self._log(f"统一账户缓存{cache.status}，启动已安全回退到界面保存数据；未触发账户刷新")
        self._refresh_warmup_template_summary()
        self._refresh_global_account_summary(state)

    def _refresh_global_account_summary(self, state: dict[str, Any] | None = None) -> None:
        if not hasattr(self, "global_account_summary"):
            return
        cache = load_account_cache()
        if cache.fresh:
            count = sum(len(rows) for rows in cache.accounts.values())
            updated = str((cache.payload or {}).get("refreshed_at") or "").replace("T", " ")[:19]
            self.global_account_summary.set(
                f"统一账户缓存：{len(cache.accounts)} 窗口 / {count} 账户"
                + (f" · {updated}" if updated else "")
            )
            return
        source = state if state is not None else load_ui_state()
        scope = global_account_scope(source)
        count = sum(len(rows) for rows in scope.values())
        metadata = source.get("global_account_data") or {}
        updated = str(metadata.get("updated_at") or "").replace("T", " ")[:19]
        if not updated:
            updated = max(
                (
                    str(row.get("updated_at") or "")
                    for rows in scope.values() for row in rows.values()
                ),
                default="",
            )
        fallback_label = {
            "missing": "缓存尚未生成",
            "expired": "缓存已过期",
            "corrupt": "缓存损坏",
            "invalid": "缓存无效",
        }.get(cache.status, "缓存不可用")
        self.global_account_summary.set(
            f"界面保存数据（{fallback_label}）：{len(scope)} 窗口 / {count} 账户"
            + (f" · {updated}" if updated else " · 尚未刷新")
        )

    def _plan_names(self) -> list[str]:
        return [name for name, value in self.config_data.get("dictionaries", {}).items() if isinstance(value, dict)]

    def _window_identities(self) -> list[tuple[str, str]]:
        identities = [
            (
                normalize_browser_type(self.task_tree.set(iid, "browser_type")),
                str(self.task_tree.set(iid, "profile") or "").strip(),
            )
            for iid in self.task_tree.get_children("")
        ]
        identities = [identity for identity in identities if identity[1]]
        if not identities:
            raise ValueError("请先新增指纹浏览器窗口")
        return identities

    def _account_cache_windows(
        self,
        resolved: dict[str, dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        resolved = resolved or {}
        previous_cache = load_account_cache()
        windows: list[dict[str, str]] = []
        for iid in self.task_tree.get_children(""):
            profile = str(self.task_tree.set(iid, "profile") or "").strip()
            if not profile:
                continue
            browser_type = str(self.task_tree.set(iid, "browser_type") or ADSPOWER)
            key = window_key(browser_type, profile)
            metadata = (
                resolved.get(key)
                or (resolved.get(profile) if browser_type == ADSPOWER else None)
                or account_cache_window_metadata(
                    previous_cache, profile, browser_type=browser_type
                )
                or {}
            )
            windows.append({
                "browser_type": browser_type,
                "profile_id": profile,
                "window_number": str(
                    metadata.get("serial_number")
                    or self.task_tree.set(iid, "window_number")
                    or ""
                ).strip(),
                "window_name": str(
                    metadata.get("name")
                    or self.task_tree.set(iid, "window_name")
                    or ""
                ).strip(),
                "running_status": str(
                    self.task_tree.set(iid, "running_status") or ""
                ).strip(),
                "metadata_refreshed_at": str(metadata.get("resolved_at") or "").strip(),
            })
        return windows

    def _window_jobs(
        self,
        *,
        required: bool = True,
        selected_only: bool = False,
    ) -> list[tuple[str, str, list[Assignment]]]:
        if selected_only:
            selected = self.task_tree.selection()
            if not selected:
                raise ValueError("请先选中要预检或执行的一个或多个普通 CID")
            plan_names = self._plan_names()
            grouped: dict[tuple[str, str], list[Assignment]] = {}
            for iid in selected:
                cid = str(self.task_tree.set(iid, "cid") or "").strip()
                account_type = str(
                    self.task_tree.set(iid, "account_type") or ""
                ).strip().upper()
                if not self.task_tree.parent(iid) or not cid or account_type == "MCC":
                    raise ValueError(
                        "选中项中包含窗口或 MCC；请只选中普通 CID 账户行"
                    )
                plan = str(self.task_tree.set(iid, "dictionary") or "").strip()
                if plan not in plan_names:
                    raise ValueError(
                        f"选中 CID {format_cid(cid)} 未分配有效内容方案"
                    )
                window_iid = self._window_for_item(iid)
                browser_type = normalize_browser_type(
                    self.task_tree.set(window_iid, "browser_type")
                )
                profile = str(self.task_tree.set(window_iid, "profile") or "").strip()
                if not profile:
                    raise ValueError(
                        f"选中 CID {format_cid(cid)} 所在的指纹浏览器窗口 ID 为空"
                    )
                grouped.setdefault((browser_type, profile), []).append(Assignment(cid, plan))
            return [
                (browser_type, profile, assignments)
                for (browser_type, profile), assignments in grouped.items()
            ]

        jobs: list[tuple[str, str, list[Assignment]]] = []
        for window_iid in self.task_tree.get_children(""):
            browser_type = normalize_browser_type(
                self.task_tree.set(window_iid, "browser_type")
            )
            profile = str(self.task_tree.set(window_iid, "profile"))
            rows = [
                Assignment(str(self.task_tree.set(iid, "cid")), str(self.task_tree.set(iid, "dictionary")))
                for iid in self._window_rows(window_iid)
                if str(self.task_tree.set(iid, "dictionary")) in self._plan_names()
                and str(self.task_tree.set(iid, "account_type")) != "MCC"
            ]
            if rows:
                jobs.append((browser_type, profile, rows))
        if required and not jobs:
            raise ValueError("没有已分配内容方案的 CID；请先编辑目标 CID 并选择内容方案")
        return jobs

    def _save_state(self) -> None:
        windows = []
        for window_iid in self.task_tree.get_children(""):
            windows.append({
                "browser_type": self.task_tree.set(window_iid, "browser_type"),
                "profile_id": self.task_tree.set(window_iid, "profile"),
                "window_number": self.task_tree.set(window_iid, "window_number"),
                "window_name": self.task_tree.set(window_iid, "window_name"),
                "running_status": self.task_tree.set(window_iid, "running_status"),
                "cids": [
                    {
                        "cid": self.task_tree.set(iid, "cid"),
                        "dictionary": self.task_tree.set(iid, "dictionary"),
                        "note": self.task_tree.set(iid, "note"),
                        "account_type": self.task_tree.set(iid, "account_type"),
                        "account_name": self.task_tree.set(iid, "account_name"),
                        "ocid": self.task_tree.set(iid, "ocid"),
                        "parent_mcc": self.task_tree.set(iid, "parent_mcc"),
                        "depth": self.task_tree.set(iid, "depth"),
                        "status": self.task_tree.set(iid, "status"),
                        "balance": self.task_tree.set(iid, "balance"),
                        "balance_type": self.task_tree.set(iid, "balance_type"),
                        "balance_status": self.task_tree.set(iid, "balance_status"),
                        "balance_detail": self.task_tree.set(iid, "balance_detail"),
                        "balance_updated_at": self.task_tree.set(iid, "balance_updated_at"),
                        "daily_spend": self.task_tree.set(iid, "daily_spend"),
                        "daily_spend_status": self.task_tree.set(iid, "daily_spend_status"),
                        "daily_spend_detail": self.task_tree.set(iid, "daily_spend_detail"),
                        "daily_spend_date": self.task_tree.set(iid, "daily_spend_date"),
                        "daily_spend_updated_at": self.task_tree.set(iid, "daily_spend_updated_at"),
                        "status_detail": self.task_tree.set(iid, "status_detail"),
                        "created_at": self.task_tree.set(iid, "created_at"),
                        "updated_at": self.task_tree.set(iid, "updated_at"),
                    }
                    for iid in self._window_rows(window_iid)
                ],
            })
        previous = load_ui_state()
        state = {
            "version": 11,
            "windows": windows,
            "enabled": sorted(key for key, value in self.enabled.items() if value.get()),
            "ad_image_plan": self.ad_image_plan.get(),
            "ad_copy_plan": self.ad_copy_plan.get(),
            "campaign_run_today": bool(self.campaign_run_today.get()),
            "campaign_white_name_prefix": self.campaign_white_name_prefix.get().strip(),
            "campaign_white_budget_limit": self.campaign_white_budget_limit.get().strip(),
            "warmup_source_cid": self.warmup_source_cid.get().strip(),
            "warmup_daily_budget": self.warmup_daily_budget.get(),
            "global_account_data": previous.get("global_account_data") or {},
        }
        save_ui_state(state)
        self._refresh_global_account_summary(state)

    @staticmethod
    def _log_tag(message: str) -> str:
        text = str(message or "")
        folded = text.casefold()
        if text.lstrip().startswith("——"):
            return "log_section"
        if any(word in text for word in ("失败", "错误", "异常", "拒绝", "未能", "禁止", "封禁", "超时")) or any(
            word in folded for word in ("error", "failed", "exception", "timeout")
        ):
            return "log_error"
        if any(word in text for word in ("警告", "跳过", "不可用", "未知", "未找到", "未提供", "取消")):
            return "log_warning"
        if any(word in text for word in ("成功", "完成", "已创建", "已启用", "已新增", "已写入", "已读取", "通过")):
            return "log_success"
        if any(word in text for word in ("创建", "复制", "启用", "写入", "删除", "替换", "暂停", "发送")):
            return "log_write"
        if any(word in text for word in ("只读", "读取", "扫描", "发现账户", "查询", "刷新", "连接", "预检")):
            return "log_read"
        return "log_normal"

    def _log(self, message: str) -> None:
        text = str(message)
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{timestamp}] ", ("log_time",))
        self.log_box.insert("end", text + "\n", (self._log_tag(text),))
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _run_async(self, label: str, task: Callable[[], Any], success: Callable[[Any], None] | None = None) -> None:
        if self.running:
            current = self.active_task_label or "当前协议任务"
            messagebox.showinfo("正在运行", f"正在执行：{current}\n请等待完成后再操作。")
            return
        self.running = True
        self._set_busy(True)
        self._start_task_status(label)
        self._log(f"—— {label} ——")
        self.update_idletasks()
        def worker() -> None:
            try:
                self.events.put(("task_done", (label, task(), success)))
            except Exception as exc:
                self.events.put(("task_error", (label, str(exc))))
        threading.Thread(target=worker, daemon=True).start()

    def _poll_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log(str(payload))
                if self.running:
                    self._update_task_step(str(payload))
            elif kind == "window_metadata":
                self._apply_window_metadata(payload)
            elif kind == "account_statuses":
                self._apply_account_statuses(payload)
            elif kind == "account_balances":
                self._apply_account_balances(payload)
            elif kind == "account_daily_spends":
                self._apply_account_daily_spends(payload)
            elif kind == "ad_reference_images":
                key, rows = payload
                self.ad_reference_loading_keys.discard(key)
                self.ad_reference_image_rows[key] = rows
                if key == self.ad_reference_request_key:
                    self._render_ad_reference_images(rows)
            elif kind == "ad_reference_images_error":
                key, error = payload
                self.ad_reference_loading_keys.discard(key)
                if key == self.ad_reference_request_key:
                    self._render_ad_reference_images([], error=str(error))
                self._log(f"参考广告图片读取失败：{error}")
            elif kind == "creative_thumbnail":
                self._apply_creative_thumbnail(*payload)
            elif kind == "task_done":
                label, result, callback = payload
                self.running = False
                self._set_busy(False)
                self._log(f"{label}：完成")
                self._finish_task_status(True, label)
                if callback:
                    callback(result)
            elif kind == "task_error":
                label, error = payload
                self.running = False
                self._set_busy(False)
                self._log(f"{label}：失败 - {error}")
                self._finish_task_status(False, label, str(error))
                self._refresh_reports()
                messagebox.showerror("协议任务失败", error)
        self.after(100, self._poll_events)

    def _start_task_status(self, label: str) -> None:
        self.active_task_label = str(label)
        self.active_task_started = time.monotonic()
        self.task_state_text.set(f"● 正在执行：{label}")
        self.task_step_text.set("任务已接收，正在启动……")
        self.task_elapsed_text.set("已用 00:00")
        self.task_state_label.configure(style="TaskRunning.TLabel")
        self.task_progress.configure(
            mode="indeterminate", value=0, style="Running.Horizontal.TProgressbar"
        )
        self.task_progress.start(12)
        self.title(f"GGAds 协议管理器 v{__version__} · 正在执行")
        if self._task_tick_after:
            self.after_cancel(self._task_tick_after)
        self._tick_task_status()

    def _tick_task_status(self) -> None:
        if not self.running or not self.active_task_started:
            self._task_tick_after = None
            return
        elapsed = max(0, int(time.monotonic() - self.active_task_started))
        self.task_elapsed_text.set(f"已用 {elapsed // 60:02d}:{elapsed % 60:02d}")
        self._task_tick_after = self.after(250, self._tick_task_status)

    def _update_task_step(self, message: str) -> None:
        text = " ".join(str(message).split())
        if len(text) > 150:
            text = text[:147] + "…"
        if text:
            self.task_step_text.set(f"当前步骤：{text}")

    def _finish_task_status(self, success: bool, summary: str, detail: str = "") -> None:
        elapsed = max(0, int(time.monotonic() - self.active_task_started))
        if self._task_tick_after:
            self.after_cancel(self._task_tick_after)
            self._task_tick_after = None
        self.task_progress.stop()
        self.task_progress.configure(
            mode="determinate", value=100,
            style=(
                "Success.Horizontal.TProgressbar"
                if success else "Error.Horizontal.TProgressbar"
            ),
        )
        self.task_state_text.set(f"● {'执行完成' if success else '执行失败'}：{summary}")
        self.task_state_label.configure(
            style="TaskSuccess.TLabel" if success else "TaskError.TLabel"
        )
        if detail:
            compact = " ".join(str(detail).split())
            self.task_step_text.set(f"失败原因：{compact[:180]}{'…' if len(compact) > 180 else ''}")
        elif not self.task_step_text.get().strip():
            self.task_step_text.set(summary)
        self.task_elapsed_text.set(f"耗时 {elapsed // 60:02d}:{elapsed % 60:02d}")
        self.title(f"GGAds 协议管理器 v{__version__}")
        self.active_task_label = ""
        self.active_task_started = 0.0

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.preflight_button.configure(state=state)
        self.execute_button.configure(state=state)
        self.status_button.configure(state=state)
        self.balance_button.configure(state=state)
        self.spend_button.configure(state=state)
        self.spend_selected_button.configure(state=state)
        self.spend_date_entry.configure(state=state)
        self.spend_end_date_entry.configure(state=state)
        self.placement_query_button.configure(state=state)
        self.placement_select_campaign_button.configure(state=state)
        self.placement_preflight_button.configure(state=state)
        self.placement_exclude_button.configure(state=state)
        self.placement_date_entry.configure(state=state)
        self.placement_window_box.configure(state="disabled" if busy else "readonly")
        self.placement_cid_box.configure(state="disabled" if busy else "readonly")
        self.remote_refresh_button.configure(state=state)
        self.warmup_scan_button.configure(state=state)
        self.warmup_execute_button.configure(state=state)
        self.warmup_source_entry.configure(state=state)
        self.warmup_budget_entry.configure(state=state)
        self.appeal_auto_time_entry.configure(state=state)
        self.appeal_auto_save_button.configure(state=state)
        self.appeal_auto_run_button.configure(state=state)
        self.appeal_reason_button.configure(state=state)
        self.appeal_prepare_button.configure(state=state)
        self.appeal_one_click_button.configure(state=state)
        self.ad_read_button.configure(state=state)
        self.ad_preflight_button.configure(state=state)
        self.ad_create_button.configure(state=state)
        self.copy_read_button.configure(state=state)
        self.copy_preflight_button.configure(state=state)
        self.copy_execute_button.configure(state=state)
        self.snap_export_read_button.configure(state=state)
        self.snap_export_button.configure(state=state)
        self.snap_preflight_button.configure(state=state)
        self.snap_import_button.configure(state=state)
        self.window_metadata_button.configure(state=state)

    # ----- AdsPower window / CID CRUD ----------------------------------------------
    def _show_task_context_menu(self, event: tk.Event) -> None:
        iid = self._select_row_at_pointer(self.task_tree, event)
        menu = self.task_context_menu
        menu.delete(0, "end")
        if not iid:
            menu.add_command(label="导入比特浏览器窗口", command=self._import_bitbrowser_windows)
            menu.add_command(label="比特 Local API 设置", command=self._open_bitbrowser_settings)
            menu.add_separator()
            menu.add_command(label="兼容：手工添加窗口（默认比特）", command=self._add_window)
            menu.add_command(label="兼容：添加 AdsPower 窗口", command=self._add_adspower_window)
            menu.add_command(label="兼容：刷新 AdsPower 窗口编号", command=self._refresh_window_metadata)
            menu.add_command(label="刷新全部账户列表与状态", command=self._refresh_account_statuses)
            self._popup_menu(menu, event)
            return
        is_window = not self.task_tree.parent(iid)
        account_type = str(self.task_tree.set(iid, "account_type") or "CID")
        if is_window:
            browser_type = str(self.task_tree.set(iid, "browser_type") or ADSPOWER)
            menu.add_command(label="添加 CID", command=self._add_cid)
            menu.add_command(label="编辑窗口", command=self._edit_window)
            if browser_type == ADSPOWER:
                menu.add_command(label="刷新此 AdsPower 窗口编号", command=lambda: self._refresh_window_metadata(iid))
            menu.add_separator()
            menu.add_command(label="读取此窗口的 CID 余额", command=lambda: self._refresh_account_balances(True))
            menu.add_command(label="读取此窗口的日期/全部消耗", command=lambda: self._refresh_account_daily_spends(True))
            menu.add_command(label="展开全部账户", command=lambda: self._set_task_branch_open(iid, True))
            menu.add_command(label="收起全部账户", command=lambda: self._set_task_branch_open(iid, False))
            menu.add_separator()
            menu.add_command(label="复制窗口 ID", command=lambda: self._copy_text(self.task_tree.set(iid, "profile"), "窗口 ID"))
            number = str(self.task_tree.set(iid, "window_number"))
            if number:
                menu.add_command(label="复制窗口编号", command=lambda: self._copy_text(number, "窗口编号"))
            menu.add_separator()
            menu.add_command(label="删除窗口", command=self._delete_window)
        elif account_type == "MCC":
            menu.add_command(
                label="收起 MCC" if self.task_tree.item(iid, "open") else "展开 MCC",
                command=lambda: self.task_tree.item(iid, open=not bool(self.task_tree.item(iid, "open"))),
            )
            menu.add_command(label="读取此 MCC 下 CID 余额", command=lambda: self._refresh_account_balances(True))
            menu.add_command(label="读取此 MCC 下的日期/全部消耗", command=lambda: self._refresh_account_daily_spends(True))
            menu.add_separator()
            menu.add_command(label="复制 MCC CID", command=lambda: self._copy_text(format_cid(self.task_tree.set(iid, "cid")), "MCC CID"))
            menu.add_command(label="复制账户名称", command=lambda: self._copy_text(self.task_tree.set(iid, "account_name"), "账户名称"))
        else:
            menu.add_command(label="编辑 CID 任务", command=self._edit_cid)
            plan_menu = tk.Menu(menu, tearoff=False)
            for plan in self._plan_names():
                plan_menu.add_command(label=plan, command=lambda name=plan: self._assign_selected_plan(name))
            if not self._plan_names():
                plan_menu.add_command(label="暂无内容方案", state="disabled")
            self.task_plan_menu = plan_menu
            menu.add_cascade(label="分配内容方案", menu=plan_menu)
            menu.add_command(label="设为未分配", command=lambda: self._assign_selected_plan(UNASSIGNED_PLAN))
            menu.add_separator()
            menu.add_command(
                label="一键白量（固定模板演练）",
                command=self._warmup_template_preview_from_task,
            )
            menu.add_separator()
            menu.add_command(label="读取此 CID 余额", command=lambda: self._refresh_account_balances(True))
            menu.add_command(label="读取此 CID 的日期/全部消耗", command=lambda: self._refresh_account_daily_spends(True))
            menu.add_command(label="转到 Google Ads 数据", command=self._open_selected_cid_in_remote)
            menu.add_separator()
            menu.add_command(label="复制 CID", command=lambda: self._copy_text(format_cid(self.task_tree.set(iid, "cid")), "CID"))
            menu.add_command(label="复制账户名称", command=lambda: self._copy_text(self.task_tree.set(iid, "account_name"), "账户名称"))
            menu.add_command(label="复制余额", command=lambda: self._copy_text(self.task_tree.set(iid, "balance"), "余额"))
            menu.add_command(label="复制日期消耗", command=lambda: self._copy_text(self.task_tree.set(iid, "daily_spend"), "日期消耗"))
            menu.add_separator()
            menu.add_command(label="删除 CID", command=self._delete_cid)
        self.task_tree.selection_set(iid)
        self.task_tree.focus(iid)
        self._popup_menu(menu, event)

    def _task_double_click(self, event: tk.Event) -> str | None:
        iid = self._select_row_at_pointer(self.task_tree, event)
        if not iid or self.task_tree.identify_region(event.x, event.y) not in {"tree", "cell"}:
            return None
        if "indicator" in str(self.task_tree.identify_element(event.x, event.y)):
            return None
        if not self.task_tree.parent(iid):
            self._edit_window()
        elif str(self.task_tree.set(iid, "account_type")) == "MCC":
            self.task_tree.item(iid, open=not bool(self.task_tree.item(iid, "open")))
        else:
            self._edit_cid()
        return "break"

    def _set_task_branch_open(self, iid: str, opened: bool) -> None:
        self.task_tree.item(iid, open=opened)
        for child in self.task_tree.get_children(iid):
            self._set_task_branch_open(child, opened)

    def _assign_selected_plan(self, plan: str) -> None:
        selected = self.task_tree.selection()
        if not selected:
            return
        iid = selected[0]
        if not self.task_tree.parent(iid) or str(self.task_tree.set(iid, "account_type")) == "MCC":
            return
        self.task_tree.set(iid, "dictionary", plan)
        self.task_tree.set(iid, "updated_at", local_timestamp())
        self._save_state()
        self._log(f"CID {format_cid(self.task_tree.set(iid, 'cid'))} 已分配内容方案：{plan}")

    def _open_selected_cid_in_remote(self) -> None:
        selected = self.task_tree.selection()
        if not selected:
            return
        iid = selected[0]
        cid = str(self.task_tree.set(iid, "cid"))
        if not cid or str(self.task_tree.set(iid, "account_type")) == "MCC":
            return
        window_iid = self._window_for_item(iid)
        browser_type = normalize_browser_type(
            self.task_tree.set(window_iid, "browser_type")
        )
        profile = str(self.task_tree.set(window_iid, "profile"))
        self._sync_remote_cids()
        label = next(
            (
                value for value, mapped in self.remote_profile_by_label.items()
                if mapped == (browser_type, profile)
            ),
            "",
        )
        self.remote_window.set(label)
        self._sync_remote_cids()
        raw_cid = normalize_cid(cid)
        selected_label = next(
            (
                value for value in getattr(self.remote_cid_box, "_cid_selector_labels", [])
                if self._cid_from_selector(value) == raw_cid
            ),
            format_cid(raw_cid),
        )
        self.remote_cid.set(selected_label)
        self._apply_cid_selector_style(self.remote_cid_box, self.remote_cid)
        self.notebook.select(self.remote_tab)

    @staticmethod
    def _window_label(
        profile: str,
        window_number: str = "",
        *,
        browser_type: str = ADSPOWER,
        window_name: str = "",
    ) -> str:
        provider = "比特" if normalize_browser_type(browser_type) == BITBROWSER else "AdsPower"
        number = f" #{window_number}" if window_number else ""
        name = f" · {window_name}" if window_name else ""
        return f"{provider}{number}{name} · {profile}"

    def _insert_window(
        self,
        profile: str,
        window_number: str = "",
        *,
        browser_type: str = BITBROWSER,
        window_name: str = "",
        running_status: str = "",
    ) -> str:
        normalized_type = normalize_browser_type(browser_type, default=BITBROWSER)
        iid = self.task_tree.insert(
            "",
            "end",
            text=self._window_label(
                profile,
                window_number,
                browser_type=normalized_type,
                window_name=window_name,
            ),
            values=(
                normalized_type,
                running_status,
                profile,
                window_number,
                window_name,
                *("" for _ in range(24)),
            ),
            open=True,
        )
        return iid

    def _insert_cid(
        self,
        window_iid: str,
        cid: str,
        dictionary: str,
        note: str = "",
        *,
        account_type: str = "",
        account_name: str = "",
        ocid: str = "",
        parent_mcc: str = "",
        depth: str = "",
        status: str = "未刷新",
        balance: str = "未读取",
        balance_type: str = "",
        balance_status: str = "未读取",
        balance_detail: str = "点击“读取 CID 余额”读取",
        balance_updated_at: str = "",
        daily_spend: str = "未读取",
        daily_spend_status: str = "未读取",
        daily_spend_detail: str = "点击“读取日期消耗”读取",
        daily_spend_date: str = "",
        daily_spend_updated_at: str = "",
        status_detail: str = "点击“刷新账户状态”读取",
        created_at: str = "",
        updated_at: str = "",
    ) -> str:
        created_at = created_at or local_timestamp()
        updated_at = updated_at or created_at
        iid = self.task_tree.insert(
            window_iid,
            "end",
            text="CID",
            values=(
                "", "", "", "", "", account_type, account_name, cid, format_cid(cid), ocid,
                parent_mcc, self._format_parent_mcc(parent_mcc), depth,
                dictionary, status, balance, balance_type, balance_status, balance_detail,
                balance_updated_at, daily_spend, daily_spend_status, daily_spend_detail,
                daily_spend_date, daily_spend_updated_at,
                status_detail, note, created_at, updated_at,
            ),
        )
        self._apply_task_row_semantics(iid)
        return iid

    def _selected_window_iid(self) -> str | None:
        selected = self.task_tree.selection()
        if not selected:
            return None
        return self._window_for_item(selected[0])

    def _resolve_window_metadata(
        self,
        profile: str,
        *,
        browser_type: str = ADSPOWER,
        force_refresh: bool = False,
        timeout: float = 2.0,
    ) -> dict[str, str]:
        """Use the fresh unified cache unless the user explicitly refreshes."""
        profile_id = str(profile or "").strip()
        normalized_type = normalize_browser_type(browser_type)
        if not force_refresh:
            cached = account_cache_window_metadata(
                load_account_cache(), profile_id, browser_type=normalized_type
            )
            if cached is not None:
                return cached
        metadata = (
            query_profile_info(profile_id, timeout=timeout)
            if normalized_type == ADSPOWER
            else query_browser_profile_info(
                profile_id, browser_type=normalized_type, timeout=timeout
            )
        )
        return normalize_window_metadata(profile_id, metadata)

    @staticmethod
    def _dialog_window_metadata(
        dialog: WindowDialog,
        profile: str,
        window_number: str,
    ) -> dict[str, str] | None:
        metadata = dialog.resolved_metadata
        if not metadata:
            return None
        if str(metadata.get("profile_id") or "") != str(profile):
            return None
        if str(metadata.get("serial_number") or "") != str(window_number):
            return None
        return metadata

    def _open_bitbrowser_settings(self) -> None:
        BitBrowserSettingsDialog(self)

    def _import_bitbrowser_windows(self) -> None:
        existing = {
            window_key(
                self.task_tree.set(iid, "browser_type"),
                self.task_tree.set(iid, "profile"),
            )
            for iid in self.task_tree.get_children("")
        }
        dialog = BitBrowserImportDialog(self, existing)
        if not dialog.result:
            return
        inserted: list[str] = []
        for row in dialog.result:
            key = window_key(row["browser_type"], row["profile_id"])
            if key in existing:
                continue
            inserted.append(
                self._insert_window(
                    row["profile_id"],
                    row["window_number"],
                    browser_type=row["browser_type"],
                    window_name=row["window_name"],
                    running_status=row["running_status"],
                )
            )
            existing.add(key)
        if not inserted:
            messagebox.showinfo("无需导入", "选中的比特浏览器窗口已经导入")
            return
        self.task_tree.selection_set(inserted)
        self._sync_remote_cids()
        self._save_state()
        self._log(f"已导入 {len(inserted)} 个比特浏览器窗口（仅保存本地元数据）")

    def _add_adspower_window(self) -> None:
        self._add_window(ADSPOWER)

    def _add_window(self, browser_type: str = BITBROWSER) -> None:
        normalized_type = normalize_browser_type(browser_type, default=BITBROWSER)
        dialog = WindowDialog(
            self,
            metadata_resolver=self._resolve_window_metadata,
            browser_type=normalized_type,
        )
        if not dialog.result:
            return
        profile, window_number = dialog.result
        key = window_key(normalized_type, profile)
        if any(
            window_key(
                self.task_tree.set(iid, "browser_type"),
                self.task_tree.set(iid, "profile"),
            ) == key
            for iid in self.task_tree.get_children("")
        ):
            messagebox.showerror("新增失败", "该浏览器窗口已经存在")
            return
        iid = self._insert_window(
            profile, window_number, browser_type=normalized_type
        )
        self.task_tree.selection_set(iid)
        self._sync_remote_cids()
        self._save_state()
        metadata = self._dialog_window_metadata(dialog, profile, window_number)
        if metadata is not None:
            self._apply_window_metadata({key: metadata})
        elif not window_number:
            self._resolve_window_metadata_async(profile, normalized_type)

    def _edit_window(self) -> None:
        iid = self._selected_window_iid()
        if not iid:
            return
        old = str(self.task_tree.set(iid, "profile"))
        browser_type = normalize_browser_type(
            str(self.task_tree.set(iid, "browser_type"))
        )
        dialog = WindowDialog(
            self,
            {
                "profile_id": old,
                "window_number": str(self.task_tree.set(iid, "window_number")),
            },
            metadata_resolver=self._resolve_window_metadata,
            browser_type=browser_type,
        )
        if not dialog.result:
            return
        new, window_number = dialog.result
        new_key = window_key(browser_type, new)
        if any(
            other != iid
            and window_key(
                self.task_tree.set(other, "browser_type"),
                self.task_tree.set(other, "profile"),
            ) == new_key
            for other in self.task_tree.get_children("")
        ):
            messagebox.showerror("编辑失败", "该浏览器窗口已经存在")
            return
        self.task_tree.set(iid, "profile", new)
        self.task_tree.set(iid, "window_number", window_number)
        self.task_tree.item(
            iid,
            text=self._window_label(
                new,
                window_number,
                browser_type=browser_type,
                window_name=str(self.task_tree.set(iid, "window_name")),
            ),
        )
        self._sync_remote_cids()
        self._save_state()
        metadata = self._dialog_window_metadata(dialog, new, window_number)
        if metadata is not None:
            self._apply_window_metadata({new_key: metadata})
        elif not window_number:
            self._resolve_window_metadata_async(new, browser_type)

    def _resolve_window_metadata_async(
        self, profile: str, browser_type: str = ADSPOWER
    ) -> None:
        normalized_type = normalize_browser_type(browser_type)

        def worker() -> None:
            try:
                info = self._resolve_window_metadata(
                    profile,
                    browser_type=normalized_type,
                    force_refresh=False,
                )
                if info.get("source") == "account-cache":
                    self.events.put(("log", f"[{profile}] 已复用统一缓存中的窗口编号"))
                self.events.put(
                    ("window_metadata", {window_key(normalized_type, profile): info})
                )
            except Exception as exc:
                label = "比特浏览器" if normalized_type == BITBROWSER else "AdsPower"
                self.events.put(("log", f"[{profile}] {label}窗口编号读取失败，可编辑窗口后手工填写：{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_window_metadata(self, window_iid: str | None = None) -> None:
        if window_iid:
            window_iids = [window_iid]
        else:
            window_iids = [
                iid
                for iid in self.task_tree.get_children("")
                if self.task_tree.set(iid, "browser_type") == ADSPOWER
            ]
        profiles = [str(self.task_tree.set(iid, "profile")) for iid in window_iids]
        if window_iid and self.task_tree.set(window_iid, "browser_type") != ADSPOWER:
            messagebox.showinfo("兼容操作", "读取窗口编号仅适用于 AdsPower；比特浏览器请重新导入窗口列表。")
            return
        profiles = [profile for profile in profiles if profile]
        if not profiles:
            messagebox.showinfo("没有窗口", "请先通过兼容操作添加 AdsPower 窗口")
            return

        def task() -> tuple[dict[str, dict[str, str]], dict[str, str]]:
            found: dict[str, dict[str, str]] = {}
            failed: dict[str, str] = {}
            for profile in profiles:
                try:
                    info = self._resolve_window_metadata(
                        profile,
                        browser_type=ADSPOWER,
                        force_refresh=True,
                        timeout=5.0,
                    )
                    found[window_key(ADSPOWER, profile)] = info
                    self.events.put(("log", f"[{profile}] AdsPower 窗口编号：#{info['serial_number']}"))
                except Exception as exc:
                    failed[profile] = str(exc)
                    self.events.put(("log", f"[{profile}] 窗口编号读取失败，保留现有编号：{exc}"))
            return found, failed

        def success(result: tuple[dict[str, dict[str, str]], dict[str, str]]) -> None:
            found, failed = result
            self._apply_window_metadata(found)
            if failed:
                details = "\n".join(f"{profile}：{reason}" for profile, reason in list(failed.items())[:5])
                messagebox.showwarning(
                    "窗口编号刷新完成",
                    f"成功 {len(found)} 个，失败 {len(failed)} 个。失败窗口已保留原编号。\n\n{details}",
                )
            else:
                messagebox.showinfo("窗口编号刷新完成", f"已读取并核对 {len(found)} 个 AdsPower 窗口编号")

        self._run_async("刷新 AdsPower 窗口编号", task, success)

    def _apply_window_metadata(
        self,
        metadata: dict[str, dict[str, str]],
        *,
        persist_state: bool = True,
        persist_cache: bool = True,
    ) -> None:
        changed = False
        for iid in self.task_tree.get_children(""):
            browser_type = str(self.task_tree.set(iid, "browser_type"))
            profile = str(self.task_tree.set(iid, "profile"))
            info = metadata.get(window_key(browser_type, profile))
            if info is None and browser_type == ADSPOWER:
                info = metadata.get(profile)
            if not info:
                continue
            window_number = str(info.get("serial_number") or "").strip()
            if not window_number:
                continue
            window_name = str(info.get("name") or "").strip()
            self.task_tree.set(iid, "window_number", window_number)
            self.task_tree.set(iid, "window_name", window_name)
            self.task_tree.item(
                iid,
                text=self._window_label(
                    profile,
                    window_number,
                    browser_type=browser_type,
                    window_name=window_name,
                ),
            )
            changed = True
        if changed:
            self._sync_remote_cids()
            if persist_state:
                self._save_state()
        if persist_cache and metadata:
            try:
                if update_account_cache_window_metadata(metadata):
                    self._refresh_global_account_summary()
            except Exception as exc:
                self._log(f"窗口编号缓存更新失败，已保留界面数据：{exc}")

    def _delete_window(self) -> None:
        iid = self._selected_window_iid()
        if not iid:
            return
        profile = self.task_tree.set(iid, "profile")
        count = len(self._window_rows(iid))
        if messagebox.askyesno("确认删除", f"删除窗口 {profile} 及其 {count} 个 CID 任务？"):
            self.task_tree.delete(iid)
            self._sync_remote_cids()
            self._save_state()

    def _cid_input(self, initial: dict[str, str] | None = None) -> tuple[str, str, str] | None:
        plans = self._plan_names()
        if not plans:
            messagebox.showerror("没有内容方案", "请先在“本地配置 → 本地内容库”中新建方案")
            return None
        dialog = CidTaskDialog(self, plans, initial)
        return dialog.result

    def _add_cid(self) -> None:
        window_iid = self._selected_window_iid()
        if not window_iid:
            messagebox.showinfo("请选择窗口", "请先新增或选中一个指纹浏览器窗口")
            return
        try:
            result = self._cid_input()
            if not result:
                return
            if any(self.task_tree.set(iid, "cid") == result[0] for iid in self._window_rows(window_iid)):
                raise ValueError("该窗口下已经存在此 CID")
            self._insert_cid(window_iid, *result)
            self._sync_remote_cids()
            self._save_state()
        except Exception as exc:
            messagebox.showerror("新增失败", str(exc))

    def _edit_cid(self) -> None:
        selected = self.task_tree.selection()
        if not selected:
            return
        iid = selected[0]
        if not self.task_tree.parent(iid):
            messagebox.showinfo("请选择 CID", "当前选择的是窗口，请选择其下的 CID")
            return
        initial = {
            "cid": str(self.task_tree.set(iid, "cid")),
            "dictionary": str(self.task_tree.set(iid, "dictionary")),
            "note": str(self.task_tree.set(iid, "note")),
        }
        try:
            result = self._cid_input(initial)
            if result:
                window_iid = self._window_for_item(iid)
                if any(
                    other != iid and self.task_tree.set(other, "cid") == result[0]
                    for other in self._window_rows(window_iid)
                ):
                    raise ValueError("该窗口下已经存在此 CID")
                old_cid = str(self.task_tree.set(iid, "cid"))
                self.task_tree.set(iid, "cid", result[0])
                self.task_tree.set(iid, "cid_display", format_cid(result[0]))
                self.task_tree.set(iid, "dictionary", result[1])
                self.task_tree.set(iid, "note", result[2])
                self.task_tree.set(iid, "updated_at", local_timestamp())
                if result[0] != old_cid:
                    self.task_tree.set(iid, "account_type", "")
                    self.task_tree.set(iid, "account_name", "")
                    self.task_tree.set(iid, "ocid", "")
                    self.task_tree.set(iid, "parent_mcc", "")
                    self.task_tree.set(iid, "parent_mcc_display", "")
                    self.task_tree.set(iid, "depth", "")
                    self.task_tree.set(iid, "status", "未刷新")
                    self.task_tree.set(iid, "status_detail", "CID 已修改，请刷新账户状态")
                    self.task_tree.set(iid, "balance", "未读取")
                    self.task_tree.set(iid, "balance_type", "")
                    self.task_tree.set(iid, "balance_status", "未读取")
                    self.task_tree.set(iid, "balance_detail", "CID 已修改，请重新读取余额")
                    self.task_tree.set(iid, "balance_updated_at", "")
                    self.task_tree.set(iid, "daily_spend", "未读取")
                    self.task_tree.set(iid, "daily_spend_status", "未读取")
                    self.task_tree.set(iid, "daily_spend_detail", "CID 已修改，请重新读取日期消耗")
                    self.task_tree.set(iid, "daily_spend_date", "")
                    self.task_tree.set(iid, "daily_spend_updated_at", "")
                    self.task_tree.item(iid, tags=("unknown",))
                self._sync_remote_cids()
                self._rebuild_account_hierarchy(window_iid)
                self._save_state()
        except Exception as exc:
            messagebox.showerror("编辑失败", str(exc))

    def _delete_cid(self) -> None:
        rows = [iid for iid in self.task_tree.selection() if self.task_tree.parent(iid)]
        if not rows:
            return
        labels = ", ".join(format_cid(str(self.task_tree.set(iid, "cid"))) for iid in rows[:5])
        if not messagebox.askyesno("确认删除 CID", f"删除 {len(rows)} 个 CID 任务？\n\n{labels}"):
            return
        for iid in rows:
            self.task_tree.delete(iid)
        self._sync_remote_cids()
        self._save_state()

    def _sync_remote_cids(self) -> None:
        current = self._remote_selected_window()
        windows = self._window_selector_rows()
        self.remote_profile_by_label = {
            label: identity for label, identity, _iid in windows
        }
        identities = [identity for _label, identity, _iid in windows]
        if current not in identities:
            current = identities[0] if identities else ("", "")
        selected_label = next(
            (label for label, identity, _iid in windows if identity == current), ""
        )
        labels = [label for label, _identity, _iid in windows]
        self.remote_window_box.configure(values=labels)
        self.remote_window.set(selected_label)
        selected_window = next(
            (iid for _label, identity, iid in windows if identity == current), None
        )
        self._configure_cid_selector(
            self.remote_cid_box,
            self.remote_cid,
            self._cid_selector_rows(selected_window),
        )
        self._sync_warmup_target_tree()
        self._refresh_appeal_rows()
        self._sync_ad_cids()
        self._sync_creative_upload_cids()
        self._sync_ga4_windows()

    def _window_selector_rows(
        self,
    ) -> list[tuple[str, tuple[str, str], str]]:
        rows: list[tuple[str, tuple[str, str], str]] = []
        for iid in self.task_tree.get_children(""):
            browser_type = normalize_browser_type(
                self.task_tree.set(iid, "browser_type")
            )
            profile = str(self.task_tree.set(iid, "profile") or "").strip()
            if not profile:
                continue
            label = self._window_label(
                profile,
                str(self.task_tree.set(iid, "window_number") or ""),
                browser_type=browser_type,
                window_name=str(self.task_tree.set(iid, "window_name") or ""),
            )
            rows.append((label, (browser_type, profile), iid))
        return rows

    @staticmethod
    def _selected_window_from_mapping(
        value: str,
        mapping: dict[str, tuple[str, str]],
    ) -> tuple[str, str]:
        label = str(value or "").strip()
        if not label:
            return "", ""
        identity = mapping.get(label)
        if identity is None:
            raise ValueError("所选指纹浏览器窗口已失效，请重新选择")
        return identity

    def _remote_selected_window(self) -> tuple[str, str]:
        return self._selected_window_from_mapping(
            self.remote_window.get(), getattr(self, "remote_profile_by_label", {})
        )

    def _sync_ad_cids(self) -> None:
        current = self._ad_selected_window()
        windows = self._window_selector_rows()
        self.ad_profile_by_label = {
            label: identity for label, identity, _iid in windows
        }
        identities = [identity for _label, identity, _iid in windows]
        if current not in identities:
            current = identities[0] if identities else ("", "")
        label = next((label for label, identity, _iid in windows if identity == current), "")
        self.ad_window_box.configure(values=[item[0] for item in windows])
        self.ad_window.set(label)
        selected_window = next(
            (iid for _label, identity, iid in windows if identity == current), None
        )
        self._configure_cid_selector(
            self.ad_cid_box,
            self.ad_cid,
            self._cid_selector_rows(selected_window),
        )
        self._sync_placement_cids()
        self._sync_entity_copy_windows()
        self._sync_snap_export_windows()

    def _ad_selected_window(self) -> tuple[str, str]:
        return self._selected_window_from_mapping(
            self.ad_window.get(), getattr(self, "ad_profile_by_label", {})
        )

    def _placement_selected_window(self) -> tuple[str, str]:
        return self._selected_window_from_mapping(
            self.placement_window.get(), getattr(self, "placement_profile_by_label", {})
        )

    def _sync_placement_cids(self) -> None:
        if not hasattr(self, "placement_window_box"):
            return
        current = self._placement_selected_window()
        windows = self._window_selector_rows()
        self.placement_profile_by_label = {
            label: identity for label, identity, _iid in windows
        }
        identities = [identity for _label, identity, _iid in windows]
        if current not in identities:
            current = identities[0] if identities else ("", "")
        label = next((label for label, identity, _iid in windows if identity == current), "")
        self.placement_window_box.configure(values=[item[0] for item in windows])
        self.placement_window.set(label)
        selected_window = next(
            (iid for _label, identity, iid in windows if identity == current), None
        )
        self._configure_cid_selector(
            self.placement_cid_box,
            self.placement_cid,
            self._cid_selector_rows(selected_window),
        )

    def _entity_copy_window(self, side: str) -> tuple[str, str]:
        variable = self.copy_source_window if side == "source" else self.copy_target_window
        return self._selected_window_from_mapping(
            variable.get(), getattr(self, "copy_profile_by_label", {})
        )

    def _sync_entity_copy_windows(self) -> None:
        if not hasattr(self, "copy_source_window_box"):
            return
        old_source = self._entity_copy_window("source")
        old_target = self._entity_copy_window("target")
        windows = self._window_selector_rows()
        self.copy_profile_by_label = {
            label: identity for label, identity, _iid in windows
        }
        labels = [label for label, _identity, _iid in windows]
        self.copy_source_window_box.configure(values=labels)
        self.copy_target_window_box.configure(values=labels)
        identities = {identity for _label, identity, _iid in windows}
        source = old_source if old_source in identities else (
            windows[0][1] if windows else ("", "")
        )
        target = old_target if old_target in identities else source
        self.copy_source_window.set(next(
            (label for label, identity, _iid in windows if identity == source), ""
        ))
        self.copy_target_window.set(next(
            (label for label, identity, _iid in windows if identity == target), ""
        ))
        self._sync_entity_copy_cids("source")
        self._sync_entity_copy_cids("target")

    def _sync_entity_copy_cids(self, side: str) -> None:
        identity = self._entity_copy_window(side)
        selected_window = next(
            (iid for _label, row_identity, iid in self._window_selector_rows()
             if row_identity == identity),
            None,
        )
        variable = self.copy_source_cid if side == "source" else self.copy_target_cid
        box = self.copy_source_cid_box if side == "source" else self.copy_target_cid_box
        self._configure_cid_selector(
            box,
            variable,
            self._cid_selector_rows(selected_window),
        )

    def _snap_export_selected_window(self) -> tuple[str, str]:
        return self._selected_window_from_mapping(
            self.snap_export_window.get(), self.snap_export_profile_by_label
        )

    def _sync_snap_export_windows(self) -> None:
        if not hasattr(self, "snap_export_window_box"):
            return
        old = self._snap_export_selected_window()
        windows = self._window_selector_rows()
        self.snap_export_profile_by_label = {
            label: identity for label, identity, _iid in windows
        }
        labels = [label for label, _identity, _iid in windows]
        self.snap_export_window_box.configure(values=labels)
        identities = {identity for _label, identity, _iid in windows}
        current = old if old in identities else (windows[0][1] if windows else ("", ""))
        self.snap_export_window.set(next(
            (label for label, identity, _iid in windows if identity == current), ""
        ))
        self._sync_snap_export_cids()

    def _sync_snap_export_cids(self) -> None:
        if not hasattr(self, "snap_export_cid_box"):
            return
        identity = self._snap_export_selected_window()
        selected_window = next(
            (iid for _label, row_identity, iid in self._window_selector_rows()
             if row_identity == identity),
            None,
        )
        self._configure_cid_selector(
            self.snap_export_cid_box,
            self.snap_export_cid,
            self._cid_selector_rows(selected_window),
        )

    def _sync_ga4_windows(self) -> None:
        if not hasattr(self, "ga4_profile"):
            return
        current = self._selected_window_from_mapping(
            self.ga4_profile.get(), getattr(self, "ga4_profile_by_label", {})
        )
        windows = self._window_selector_rows()
        self.ga4_profile_by_label = {
            label: identity for label, identity, _iid in windows
        }
        identities = [identity for _label, identity, _iid in windows]
        if current not in identities:
            current = identities[0] if identities else ("", "")
        labels = [label for label, _identity, _iid in windows]
        self.ga4_profile.configure(values=labels)
        self.ga4_profile.set(next(
            (label for label, identity, _iid in windows if identity == current), ""
        ))

    def _ga4_selected_window(self) -> tuple[str, str]:
        return self._selected_window_from_mapping(
            self.ga4_profile.get(), getattr(self, "ga4_profile_by_label", {})
        )

    # ----- batch -------------------------------------------------------------------
    def _apply_account_statuses(
        self,
        results: dict[str, dict[str, dict[str, Any]]],
        *,
        persist_state: bool = True,
    ) -> None:
        for window_iid in self.task_tree.get_children(""):
            browser_type = normalize_browser_type(
                self.task_tree.set(window_iid, "browser_type")
            )
            profile = str(self.task_tree.set(window_iid, "profile"))
            profile_key = window_key(browser_type, profile)
            by_cid = results.get(profile_key, {})
            existing = {
                str(self.task_tree.set(iid, "cid")): iid
                for iid in self._window_rows(window_iid)
            }
            for cid in sorted(by_cid):
                item = by_cid[cid]
                iid = existing.get(cid)
                if not iid:
                    iid = self._insert_cid(
                        window_iid,
                        cid,
                        UNASSIGNED_PLAN,
                        "协议自动发现；选择内容方案后才会执行",
                    )
                    existing[cid] = iid
                    self._log(f"[{profile}] 自动发现 CID：{format_cid(cid)} · {item.get('name') or '未命名账户'}")
                item = by_cid.get(cid)
                if not item:
                    continue
                if "account_type" in item or "is_manager" in item:
                    self.task_tree.set(
                        iid,
                        "account_type",
                        str(item.get("account_type") or ("MCC" if item.get("is_manager") else "CID")),
                    )
                self.task_tree.set(iid, "account_name", str(item.get("name") or ""))
                self.task_tree.set(iid, "ocid", str(item.get("ocid") or ""))
                if "parent_mcc" in item:
                    parent_mcc = str(item.get("parent_mcc") or "")
                    self.task_tree.set(iid, "parent_mcc", parent_mcc)
                    self.task_tree.set(iid, "parent_mcc_display", self._format_parent_mcc(parent_mcc))
                if "depth" in item:
                    self.task_tree.set(iid, "depth", str(item.get("depth") if item.get("depth") is not None else ""))
                status = str(item.get("status") or "状态未知")
                self.task_tree.set(iid, "status", status)
                self.task_tree.set(iid, "status_detail", str(item.get("status_detail") or ""))
                self.task_tree.set(iid, "updated_at", local_timestamp())
                self._apply_task_row_semantics(iid)
            self._rebuild_account_hierarchy(window_iid)
        self._sync_remote_cids()
        if persist_state:
            self._save_state()

    def _refresh_account_statuses(self) -> None:
        try:
            identities = self._window_identities()
            known = {
                window_key(
                    self.task_tree.set(window_iid, "browser_type"),
                    self.task_tree.set(window_iid, "profile"),
                ): {
                    str(self.task_tree.set(iid, "cid"))
                    for iid in self._window_rows(window_iid)
                }
                for window_iid in self.task_tree.get_children("")
            }
        except Exception as exc:
            messagebox.showerror("输入有误", str(exc))
            return

        def task():
            results: dict[str, dict[str, dict[str, Any]]] = {}
            errors: dict[str, str] = {}
            window_metadata: dict[str, dict[str, str]] = {}
            for browser_type, profile in identities:
                profile_key = window_key(browser_type, profile)
                self.events.put(("log", f"只读扫描全部 CID：{profile}"))
                try:
                    info = self._resolve_window_metadata(
                        profile, browser_type=browser_type, force_refresh=True,
                    )
                    window_metadata[profile_key] = info
                    self.events.put(("window_metadata", {profile_key: info}))
                except Exception as exc:
                    self.events.put(("log", f"[{profile}] 指纹浏览器窗口资料读取失败，保留现有资料：{exc}"))
                runner = ProtocolRunner(
                    profile, lambda text: self.events.put(("log", text)),
                    browser_type=browser_type,
                )
                read_error = ""
                try:
                    found = runner.account_snapshots()
                except Exception as exc:
                    found = {}
                    read_error = str(exc)
                    errors[profile_key] = read_error
                    self.events.put(("log", f"[{profile}] 状态读取失败：{exc}"))
                rows: dict[str, dict[str, Any]] = dict(found)
                for cid in known.get(profile_key, set()):
                    rows[cid] = rows.get(cid) or {
                        "cid": cid,
                        "ocid": "",
                        "name": "",
                        "status": "读取失败" if read_error else "不可用",
                        "status_detail": (
                            f"无法连接该指纹浏览器窗口或读取账户列表：{read_error}"
                            if read_error else "该指纹浏览器窗口/MCC 的协议账户列表中未发现此 CID"
                        ),
                    }
                results[profile_key] = rows
            return results, errors, window_metadata

        def success(result):
            results, errors, window_metadata = result
            self._apply_account_statuses(results)
            count = sum(len(rows) for rows in results.values())
            if errors:
                self._log(
                    "账户刷新存在失败窗口：未替换统一账户缓存，继续保留上次完整成功快照"
                )
                summary = "\n".join(f"{profile}：{error}" for profile, error in list(errors.items())[:5])
                messagebox.showwarning(
                    "部分窗口读取失败",
                    f"已处理 {len(identities)} 个窗口，共列出 {count} 个 CID；其中 {len(errors)} 个窗口读取失败。\n\n{summary}",
                )
            else:
                try:
                    cache_target = save_account_cache(
                        results,
                        windows=self._account_cache_windows(window_metadata),
                    )
                    self._refresh_global_account_summary()
                    self._log(f"统一账户缓存已原子更新：{cache_target}")
                except Exception as exc:
                    self._log(f"统一账户缓存写入失败：{exc}")
                    messagebox.showwarning(
                        "账户缓存写入失败",
                        "账户状态已在当前界面刷新，但未能持久化统一账户缓存。"
                        f"\n后续功能将安全回退到界面保存数据，不会自动重刷账户。\n\n{exc}",
                    )
                messagebox.showinfo("账户状态", f"已扫描 {len(identities)} 个窗口，共列出 {count} 个 CID")

        self._run_async("刷新账户状态（只读）", task, success)

    def _apply_account_balances(self, results: dict[str, dict[str, dict[str, str]]]) -> None:
        for window_iid in self.task_tree.get_children(""):
            browser_type = normalize_browser_type(
                self.task_tree.set(window_iid, "browser_type")
            )
            profile = str(self.task_tree.set(window_iid, "profile"))
            by_cid = results.get(window_key(browser_type, profile), {})
            existing = {
                str(self.task_tree.set(iid, "cid")): iid
                for iid in self._window_rows(window_iid)
            }
            for cid, balance in by_cid.items():
                iid = existing.get(cid)
                if not iid:
                    continue
                self.task_tree.set(iid, "balance", str(balance.get("balance") or "—"))
                self.task_tree.set(iid, "balance_type", str(balance.get("balance_type") or ""))
                self.task_tree.set(iid, "balance_status", str(balance.get("balance_status") or "未知"))
                self.task_tree.set(iid, "balance_detail", str(balance.get("balance_detail") or ""))
                self.task_tree.set(iid, "balance_updated_at", local_timestamp())
        self._sync_warmup_target_tree()
        self._save_state()

    def _refresh_account_balances(self, selected_only: bool = False) -> None:
        try:
            identities = self._window_identities()
            if selected_only and self.task_tree.selection():
                selected_iid = self.task_tree.selection()[0]
                window_iid = self._window_for_item(selected_iid)
                profile = str(self.task_tree.set(window_iid, "profile"))
                browser_type = normalize_browser_type(
                    self.task_tree.set(window_iid, "browser_type")
                )
                profile_key = window_key(browser_type, profile)
                if not self.task_tree.parent(selected_iid):
                    candidates = self._window_rows(window_iid)
                elif str(self.task_tree.set(selected_iid, "account_type")) == "MCC":
                    candidates = list(self._iter_task_rows(selected_iid))
                else:
                    candidates = [selected_iid]
                wanted = {
                    profile_key: {
                        str(self.task_tree.set(iid, "cid"))
                        for iid in candidates
                        if str(self.task_tree.set(iid, "cid"))
                        and str(self.task_tree.set(iid, "account_type")) != "MCC"
                    }
                }
                identities = [(browser_type, profile)]
            else:
                wanted = {
                    window_key(
                        self.task_tree.set(window_iid, "browser_type"),
                        self.task_tree.set(window_iid, "profile"),
                    ): {
                        str(self.task_tree.set(iid, "cid"))
                        for iid in self._window_rows(window_iid)
                        if str(self.task_tree.set(iid, "cid"))
                        and str(self.task_tree.set(iid, "account_type")) != "MCC"
                    }
                    for window_iid in self.task_tree.get_children("")
                }
            total = sum(len(cids) for cids in wanted.values())
            if not total:
                raise ValueError("当前没有可读取余额的 CID；请先刷新账户列表")
        except Exception as exc:
            messagebox.showerror("无法读取余额", str(exc))
            return

        def task():
            results: dict[str, dict[str, dict[str, str]]] = {}
            for browser_type, profile in identities:
                profile_key = window_key(browser_type, profile)
                cids = wanted.get(profile_key, set())
                if not cids:
                    continue
                self.events.put(("log", f"[{profile}] 开始读取 {len(cids)} 个 CID 的 Billing 余额"))
                runner = ProtocolRunner(
                    profile, lambda text: self.events.put(("log", text)),
                    browser_type=browser_type,
                )
                try:
                    rows = runner.account_balances(cids)
                except Exception as exc:
                    rows = {
                        cid: {
                            "balance": "—",
                            "balance_type": "读取失败",
                            "balance_status": "读取失败",
                            "balance_detail": str(exc),
                        }
                        for cid in cids
                    }
                for cid in cids - set(rows):
                    rows[cid] = {
                        "balance": "—",
                        "balance_type": "未找到",
                        "balance_status": "不可用",
                        "balance_detail": "当前指纹浏览器窗口/MCC 未发现此 CID，或该账户是 MCC 管理账户",
                    }
                results[profile_key] = rows
                self.events.put(("account_balances", {profile_key: rows}))
            return results

        def success(results):
            self._apply_account_balances(results)
            rows = [item for by_cid in results.values() for item in by_cid.values()]
            available = sum(item.get("balance_status") == "可用" for item in rows)
            unavailable = len(rows) - available
            if unavailable:
                messagebox.showwarning(
                    "余额读取完成",
                    f"共读取 {len(rows)} 个 CID：成功 {available} 个，未提供或失败 {unavailable} 个。\n"
                    "在“详细字段”中查看余额类型、状态和具体原因。",
                )
            else:
                messagebox.showinfo("余额读取完成", f"已读取 {available} 个 CID 的账户余额")

        self._run_async("读取 CID Billing 余额（只读）", task, success)

    def _set_spend_date_offset(self, days: int) -> None:
        day = (datetime.now().astimezone().date() + timedelta(days=days)).isoformat()
        self.spend_query_date.set(day)
        self.spend_query_end_date.set(day)

    def _set_spend_date_all(self) -> None:
        self.spend_query_date.set("全部")
        self.spend_query_end_date.set("全部")

    def _refresh_selected_spend_rows(self) -> None:
        selected = self.spend_tree.selection()
        if not selected:
            messagebox.showinfo("没有选中 CID", "请先在每日消耗列表中选择一个或多个 CID")
            return
        wanted: dict[str, set[str]] = {}
        for iid in selected:
            item = self.spend_items.get(iid)
            if item:
                key = window_key(item.get("browser_type"), item.get("profile_id"))
                wanted.setdefault(key, set()).add(str(item["cid"]))
        if not wanted:
            messagebox.showinfo("没有选中 CID", "选中行没有可查询的 CID")
            return
        self._refresh_account_daily_spends(explicit_wanted=wanted)

    def _task_account_metadata(
        self, browser_type: str, profile: str, cid: str,
    ) -> dict[str, str]:
        profile_key = window_key(browser_type, profile)
        cached = account_cache_metadata(load_account_cache(), profile_key, cid)
        if cached is not None:
            return {
                "window_number": cached["window_number"],
                "account_name": cached["account_name"],
                "account_status": cached["account_status"],
            }
        for window_iid in self.task_tree.get_children(""):
            if window_key(
                self.task_tree.set(window_iid, "browser_type"),
                self.task_tree.set(window_iid, "profile"),
            ) != profile_key:
                continue
            for iid in self._window_rows(window_iid):
                if str(self.task_tree.set(iid, "cid")) == cid:
                    return {
                        "window_number": str(self.task_tree.set(window_iid, "window_number")),
                        "account_name": str(self.task_tree.set(iid, "account_name")),
                        "account_status": str(self.task_tree.set(iid, "status")),
                    }
        return {"window_number": "", "account_name": "", "account_status": ""}

    def _show_spend_results(self, results: dict[str, dict[str, dict[str, Any]]]) -> None:
        for profile_key, by_cid in results.items():
            browser_type, profile = split_window_key(profile_key)
            for cid, spend in by_cid.items():
                metadata = self._task_account_metadata(browser_type, profile, cid)
                iid = f"{profile_key}:{cid}"
                campaigns = spend.get("daily_spend_campaigns") or []
                item = {
                    **spend,
                    **metadata,
                    "browser_type": browser_type,
                    "profile_id": profile,
                    "cid": cid,
                    "cid_display": format_cid(cid),
                    "updated_at": local_timestamp(),
                }
                self.spend_items[iid] = item
                values = (
                    metadata["window_number"], profile, format_cid(cid),
                    metadata["account_name"], metadata["account_status"],
                    spend.get("daily_spend_date") or "—", spend.get("daily_spend") or "—",
                    spend.get("daily_spend_currency") or "—", len(campaigns),
                    spend.get("daily_spend_status") or "未知", item["updated_at"],
                    spend.get("daily_spend_detail") or "",
                )
                try:
                    amount = float(spend.get("daily_spend_amount") or 0)
                except (TypeError, ValueError):
                    amount = 0
                tag = (
                    "failed" if spend.get("daily_spend_status") != "可用"
                    else "ok" if amount > 0 else "zero"
                )
                if self.spend_tree.exists(iid):
                    self.spend_tree.item(iid, values=values, tags=(tag,))
                else:
                    self.spend_tree.insert("", "end", iid=iid, values=values, tags=(tag,))
        children = list(self.spend_tree.get_children())
        children.sort(key=lambda iid: (
            str(self.spend_tree.set(iid, "window_number")),
            str(self.spend_tree.set(iid, "account_name")).casefold(),
            str(self.spend_tree.set(iid, "cid")),
        ))
        for index, iid in enumerate(children):
            self.spend_tree.move(iid, "", index)

    def _show_spend_detail(self) -> None:
        selected = self.spend_tree.selection()
        item = self.spend_items.get(selected[0]) if selected else None
        if not item:
            text = "请选择一个 CID 查看广告系列明细。"
        else:
            lines = [
                "指纹浏览器窗口："
                + self._window_label(
                    str(item["profile_id"]),
                    str(item.get("window_number") or ""),
                    browser_type=str(item.get("browser_type") or ADSPOWER),
                ),
                f"CID：{item['cid_display']} · {item.get('account_name') or '未命名'} · {item.get('account_status') or '未知'}",
                f"日期：{item.get('daily_spend_date') or '—'}",
                f"账户合计：{item.get('daily_spend') or '—'} · {item.get('daily_spend_status') or '未知'}",
                f"说明：{item.get('daily_spend_detail') or '—'}",
                "",
                "广告系列明细：",
            ]
            campaigns = sorted(
                item.get("daily_spend_campaigns") or [],
                key=lambda row: float(row.get("cost") or 0),
                reverse=True,
            )
            if campaigns:
                currency = item.get("daily_spend_currency") or ""
                status_labels = {3: "已启用", 5: "已暂停", 7: "已移除"}
                for index, row in enumerate(campaigns, 1):
                    lines.append(
                        f"{index:>2}. {row.get('name') or '未命名'} | {row.get('campaign_id')} | "
                        f"{status_labels.get(row.get('status'), row.get('status'))} | "
                        f"{currency} {float(row.get('cost') or 0):.6f}"
                    )
            else:
                lines.append("没有可显示的系列明细；请查看上方失败原因。")
            text = "\n".join(lines)
        self.spend_detail.configure(state="normal")
        self.spend_detail.delete("1.0", "end")
        self.spend_detail.insert("1.0", text)
        self.spend_detail.configure(state="disabled")

    def _apply_account_daily_spends(
        self, results: dict[str, dict[str, dict[str, Any]]]
    ) -> None:
        for window_iid in self.task_tree.get_children(""):
            browser_type = normalize_browser_type(
                self.task_tree.set(window_iid, "browser_type")
            )
            profile = str(self.task_tree.set(window_iid, "profile"))
            by_cid = results.get(window_key(browser_type, profile), {})
            existing = {
                str(self.task_tree.set(iid, "cid")): iid
                for iid in self._window_rows(window_iid)
            }
            for cid, spend in by_cid.items():
                iid = existing.get(cid)
                if not iid:
                    continue
                self.task_tree.set(iid, "daily_spend", str(spend.get("daily_spend") or "—"))
                self.task_tree.set(
                    iid, "daily_spend_status", str(spend.get("daily_spend_status") or "未知")
                )
                self.task_tree.set(
                    iid, "daily_spend_detail", str(spend.get("daily_spend_detail") or "")
                )
                self.task_tree.set(
                    iid, "daily_spend_date", str(spend.get("daily_spend_date") or "")
                )
                self.task_tree.set(iid, "daily_spend_updated_at", local_timestamp())
        self._show_spend_results(results)
        self._save_state()

    def _refresh_account_daily_spends(
        self,
        selected_only: bool = False,
        explicit_wanted: dict[str, set[str]] | None = None,
    ) -> None:
        try:
            report_period = parse_report_range(
                self.spend_query_date.get(), self.spend_query_end_date.get(),
            )
            if report_period.is_all:
                self.spend_query_date.set("全部")
                self.spend_query_end_date.set("全部")
            else:
                self.spend_query_date.set(report_period.start.isoformat())
                self.spend_query_end_date.set(report_period.end.isoformat())
            if explicit_wanted is not None:
                wanted = {
                    str(profile): {str(cid) for cid in cids}
                    for profile, cids in explicit_wanted.items() if cids
                }
                identities = [split_window_key(key) for key in wanted]
            else:
                identities = self._window_identities()
            if explicit_wanted is None and selected_only and self.task_tree.selection():
                selected_iid = self.task_tree.selection()[0]
                window_iid = self._window_for_item(selected_iid)
                profile = str(self.task_tree.set(window_iid, "profile"))
                browser_type = normalize_browser_type(
                    self.task_tree.set(window_iid, "browser_type")
                )
                profile_key = window_key(browser_type, profile)
                if not self.task_tree.parent(selected_iid):
                    candidates = self._window_rows(window_iid)
                elif str(self.task_tree.set(selected_iid, "account_type")) == "MCC":
                    candidates = list(self._iter_task_rows(selected_iid))
                else:
                    candidates = [selected_iid]
                wanted = {
                    profile_key: {
                        str(self.task_tree.set(iid, "cid"))
                        for iid in candidates
                        if str(self.task_tree.set(iid, "cid"))
                        and str(self.task_tree.set(iid, "account_type")) != "MCC"
                    }
                }
                identities = [(browser_type, profile)]
            elif explicit_wanted is None:
                cache = load_account_cache()
                if cache.fresh:
                    wanted = account_cache_wanted(
                        cache,
                        allowed_profiles={
                            window_key(browser_type, profile)
                            for browser_type, profile in identities
                        },
                    )
                    self._log(
                        "每日消耗账户范围与基础状态来自统一账户缓存；未自动刷新账户"
                    )
                else:
                    wanted = {
                        window_key(
                            self.task_tree.set(window_iid, "browser_type"),
                            self.task_tree.set(window_iid, "profile"),
                        ): {
                            str(self.task_tree.set(iid, "cid"))
                            for iid in self._window_rows(window_iid)
                            if str(self.task_tree.set(iid, "cid"))
                            and str(self.task_tree.set(iid, "account_type")) != "MCC"
                        }
                        for window_iid in self.task_tree.get_children("")
                    }
                    self._log(
                        f"统一账户缓存{cache.status}；每日消耗已回退到界面保存数据，"
                        "未自动刷新账户"
                    )
            if not sum(len(cids) for cids in wanted.values()):
                raise ValueError("当前没有可读取消耗的 CID；请先刷新账户列表")
        except Exception as exc:
            messagebox.showerror("无法读取日期消耗", str(exc))
            return

        def task():
            results: dict[str, dict[str, dict[str, Any]]] = {}
            for browser_type, profile in identities:
                profile_key = window_key(browser_type, profile)
                cids = wanted.get(profile_key, set())
                if not cids:
                    continue
                self.events.put((
                    "log",
                    f"[{profile}] 开始读取 {len(cids)} 个 CID 的 {report_period.label}消耗",
                ))
                runner = ProtocolRunner(
                    profile, lambda text: self.events.put(("log", text)),
                    browser_type=browser_type,
                )
                try:
                    rows = runner.account_daily_spends(cids, report_period)
                except Exception as exc:
                    rows = {
                        cid: {
                            "daily_spend": "—",
                            "daily_spend_status": "读取失败",
                            "daily_spend_detail": str(exc),
                            "daily_spend_date": report_period.label,
                        }
                        for cid in cids
                    }
                for cid in cids - set(rows):
                    rows[cid] = {
                        "daily_spend": "—",
                        "daily_spend_status": "不可用",
                        "daily_spend_detail": "当前窗口/MCC 未发现此普通 CID",
                        "daily_spend_date": report_period.label,
                    }
                results[profile_key] = rows
                self.events.put(("account_daily_spends", {profile_key: rows}))
            return results

        def success(results):
            self._apply_account_daily_spends(results)
            rows = [item for by_cid in results.values() for item in by_cid.values()]
            available = sum(item.get("daily_spend_status") == "可用" for item in rows)
            failed = len(rows) - available
            if failed:
                messagebox.showwarning(
                    "日期消耗读取完成",
                    f"共读取 {len(rows)} 个 CID：成功 {available} 个，失败 {failed} 个。"
                    f"\n查询日期：{report_period.label}。可在详细字段查看失败原因。",
                )
            else:
                messagebox.showinfo(
                    "日期消耗读取完成",
                    f"已读取 {available} 个 CID 在 {report_period.label}范围的消耗",
                )

        self._run_async(f"读取 CID {report_period.label}消耗（只读）", task, success)

    # ----- placement performance / campaign exclusions ---------------------------
    def _set_placement_date_offset(self, days: int) -> None:
        self.placement_query_date.set(
            (datetime.now().astimezone().date() + timedelta(days=days)).isoformat()
        )

    def _set_placement_date_all(self) -> None:
        self.placement_query_date.set("全部")

    def _placement_context(self) -> tuple[str, str, str, ReportPeriod]:
        browser_type, profile = self._placement_selected_window()
        cid = self._cid_from_selector(self.placement_cid.get())
        if not profile or not cid:
            raise ValueError("请选择指纹浏览器窗口和普通 CID")
        report_period = parse_report_period(self.placement_query_date.get())
        self.placement_query_date.set(report_period.label)
        return browser_type, profile, cid, report_period

    def _set_placement_detail(self, value: Any) -> None:
        text = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False, indent=2
        )
        self.placement_detail.configure(state="normal")
        self.placement_detail.delete("1.0", "end")
        self.placement_detail.insert("1.0", text)
        self.placement_detail.configure(state="disabled")

    def _query_placements(self) -> None:
        try:
            browser_type, profile, cid, report_period = self._placement_context()
        except Exception as exc:
            messagebox.showerror("无法查询展示位置", str(exc))
            return

        def task():
            with PlacementManager(
                profile, cid, lambda text: self.events.put(("log", text)),
                browser_type=browser_type,
            ) as manager:
                return manager.query(report_period)

        def success(rows):
            self.placement_query_context = {
                "browser_type": browser_type,
                "profile": profile,
                "cid": cid,
                "date": report_period,
                "rows": rows,
            }
            self._show_placement_rows(rows)
            website_count = sum(row.get("eligible_for_campaign_exclusion") for row in rows)
            if rows:
                messagebox.showinfo(
                    "展示位置查询完成",
                    f"{format_cid(cid)} · {report_period.label}：共 {len(rows)} 条展示位置，"
                    f"其中 {website_count} 个网站可执行广告系列排除。",
                )
            else:
                messagebox.showinfo(
                    "没有展示位置",
                    f"{format_cid(cid)} 在 {report_period.label}范围没有展示位置数据。",
                )

        self._run_async(
            f"读取 {format_cid(cid)} · {report_period.label}展示位置（只读）",
            task,
            success,
        )

    def _show_placement_rows(self, rows: list[dict[str, Any]]) -> None:
        self.placement_tree.delete(*self.placement_tree.get_children())
        self.placement_items.clear()
        self.placement_nodes.clear()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            campaign_id = str(row.get("campaign_id") or "")
            group_id = str(row.get("ad_group_id") or "")
            campaign = grouped.setdefault(campaign_id, {
                "name": row.get("campaign_name") or "未命名广告系列",
                "groups": {},
            })
            group = campaign["groups"].setdefault(group_id, {
                "name": row.get("ad_group_name") or "未命名广告组",
                "rows": [],
            })
            group["rows"].append(row)

        for campaign_id, campaign in grouped.items():
            campaign_rows = [
                row for group in campaign["groups"].values() for row in group["rows"]
            ]
            campaign_iid = self.placement_tree.insert(
                "",
                "end",
                text=str(campaign["name"]),
                values=(
                    "广告系列", campaign_id, "", "", "", "", "", "", "", "", "",
                    f"{len(campaign['groups'])} 组 / {len(campaign_rows)} 个位置",
                ),
                open=True,
                tags=("campaign",),
            )
            self.placement_nodes[campaign_iid] = {
                "level": "campaign", "campaign_id": campaign_id,
            }
            for group_id, group in campaign["groups"].items():
                group_iid = self.placement_tree.insert(
                    campaign_iid,
                    "end",
                    text=str(group["name"]),
                    values=(
                        "广告组", group_id, "", "", "", "", "", "", "", "", "",
                        f"{len(group['rows'])} 个位置",
                    ),
                    open=True,
                    tags=("group",),
                )
                self.placement_nodes[group_iid] = {
                    "level": "group", "campaign_id": campaign_id, "ad_group_id": group_id,
                }
                for row in group["rows"]:
                    try:
                        interaction_rate = f"{float(row.get('interaction_rate') or 0) * 100:.2f}%"
                    except (TypeError, ValueError):
                        interaction_rate = "—"
                    status = (
                        "可从系列排除"
                        if row.get("eligible_for_campaign_exclusion")
                        else "类型暂不支持"
                    )
                    iid = self.placement_tree.insert(
                        group_iid,
                        "end",
                        text=str(row.get("display_name") or row.get("placement") or "未知位置"),
                        values=(
                            row.get("placement_type") or "未知",
                            row.get("placement_id") or "",
                            row.get("website_category") or "未分析",
                            row.get("access_status") or "未检测",
                            row.get("impressions") or 0,
                            row.get("interactions") or 0,
                            interaction_rate,
                            f"{float(row.get('average_cost') or 0):.6f}",
                            f"{float(row.get('cost') or 0):.6f}",
                            f"{float(row.get('conversions') or 0):.2f}",
                            row.get("currency") or "—",
                            status,
                        ),
                        tags=(
                            "website" if row.get("eligible_for_campaign_exclusion")
                            else "unsupported",
                        ),
                    )
                    self.placement_items[iid] = row
        self.placement_selection_text.set("已选择 0 个网站展示位置")
        self.placement_analysis_context = None
        self.placement_analysis_summary.set("尚未检测网站")
        self._set_placement_detail(
            f"读取完成：{len(grouped)} 个广告系列，{len(rows)} 条展示位置。\n"
            "按 Ctrl/Shift 多选网站，先执行排除预检，再从广告系列中排除。"
        )

    def _import_placement_selection(self) -> None:
        if not self.placement_query_context:
            messagebox.showinfo("请先查询", "请先查询当前 CID 和日期的展示位置，再导入网站名单进行匹配。")
            return
        path = filedialog.askopenfilename(
            title="导入网站名单并匹配当前展示位置",
            filetypes=[("网站名单", "*.txt *.csv *.json"), ("TXT", "*.txt"), ("CSV", "*.csv"), ("JSON", "*.json")],
        )
        if not path:
            return
        try:
            websites = set(import_website_file(path))
        except Exception as exc:
            messagebox.showerror("网站名单导入失败", str(exc))
            return
        matched = [
            iid for iid, row in self.placement_items.items()
            if row.get("eligible_for_campaign_exclusion")
            and str(row.get("normalized_placement") or "") in websites
        ]
        matched_domains = {
            str(self.placement_items[iid].get("normalized_placement") or "") for iid in matched
        }
        if matched:
            self.placement_tree.selection_set(*matched)
            self.placement_tree.see(matched[0])
        else:
            self._clear_placement_selection()
        missing = sorted(websites - matched_domains)
        self._placement_selection_changed()
        self._set_placement_detail({
            "import_file": path,
            "website_count": len(websites),
            "matched_domain_count": len(matched_domains),
            "selected_row_count": len(matched),
            "not_in_current_report": missing,
        })
        messagebox.showinfo(
            "网站名单匹配完成",
            f"导入 {len(websites)} 个域名；当前报表匹配 {len(matched_domains)} 个域名、"
            f"选中 {len(matched)} 条系列展示位置；未匹配 {len(missing)} 个。",
        )

    def _export_placement_rows(self) -> None:
        if not self.placement_query_context:
            messagebox.showinfo("没有数据", "请先查询展示位置。")
            return
        rows = self._selected_placement_rows()
        scope = "selected"
        if not rows:
            rows = [
                row for row in self.placement_items.values()
                if row.get("eligible_for_campaign_exclusion")
            ]
            scope = "all_websites"
        if not rows:
            messagebox.showinfo("没有网站", "当前结果没有可导出的网站展示位置。")
            return
        path = filedialog.asksaveasfilename(
            title="导出展示位置",
            defaultextension=".csv",
            initialfile=f"placements-{scope}.csv",
            filetypes=[("CSV", "*.csv"), ("JSON", "*.json"), ("TXT 域名", "*.txt")],
        )
        if not path:
            return
        fields = (
            "date", "campaign_name", "campaign_id", "ad_group_name", "ad_group_id",
            "normalized_placement", "display_name", "website_category", "category_confidence",
            "access_status", "http_status", "final_url", "title", "impressions", "interactions",
            "interaction_rate", "average_cost", "cost", "conversions", "currency",
        )
        try:
            target = Path(path)
            if target.suffix.casefold() == ".json":
                payload = {
                    "version": 1, "scope": scope,
                    "rows": [{key: row.get(key, "") for key in fields} for row in rows],
                }
                target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            elif target.suffix.casefold() == ".txt":
                domains = sorted({str(row.get("normalized_placement") or "") for row in rows})
                target.write_text("\n".join(value for value in domains if value) + "\n", encoding="utf-8")
            else:
                with target.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows({key: row.get(key, "") for key in fields} for row in rows)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))
            return
        self._log(f"已导出 {len(rows)} 条展示位置：{path}")
        messagebox.showinfo("导出完成", f"已导出 {len(rows)} 条展示位置。\n{path}")

    def _analyze_placement_websites(self) -> None:
        if not self.placement_query_context:
            messagebox.showinfo("请先查询", "请先查询展示位置，再检测网站。")
            return
        domains = sorted({
            str(row.get("normalized_placement") or "")
            for row in self.placement_items.values()
            if row.get("eligible_for_campaign_exclusion") and row.get("normalized_placement")
        })
        if not domains:
            messagebox.showinfo("没有网站", "当前结果中没有可检测的网站展示位置。")
            return

        def progress(done: int, total: int, result: dict[str, Any]) -> None:
            if done == total or done == 1 or done % 10 == 0:
                self.events.put(("log", f"网站检测进度 {done}/{total}：{result.get('domain')}"))

        def task():
            return analyze_websites(domains, timeout=8.0, workers=10, progress=progress)

        def success(results):
            by_domain = {str(row["domain"]): row for row in results}
            domain_category_counts: dict[str, int] = {}
            placement_category_counts: dict[str, int] = {}
            reachable_domains = sum(bool(row.get("reachable")) for row in results)
            for result in results:
                category = str(result.get("category") or "其他")
                domain_category_counts[category] = domain_category_counts.get(category, 0) + 1
            for iid, placement in self.placement_items.items():
                result = by_domain.get(str(placement.get("normalized_placement") or ""))
                if not result:
                    continue
                placement.update({
                    "website_category": result.get("category") or "其他",
                    "category_confidence": result.get("category_confidence") or 0,
                    "category_reason": result.get("category_reason") or "",
                    "access_status": result.get("access_status") or "未知",
                    "http_status": result.get("http_status") or 0,
                    "final_url": result.get("final_url") or "",
                    "title": result.get("title") or "",
                    "access_error": result.get("access_error") or "",
                })
                category = str(placement["website_category"])
                placement_category_counts[category] = placement_category_counts.get(category, 0) + 1
                self.placement_tree.set(iid, "category", category)
                self.placement_tree.set(iid, "access_status", str(result.get("access_status") or "未知"))
                if placement.get("campaign_exclusion_status"):
                    tags = ("excluded",)
                else:
                    tags = ("website",)
                self.placement_tree.item(iid, tags=tags)
            self.placement_analysis_context = {
                "unique_domains": len(results),
                "reachable_domains": reachable_domains,
                "domain_category_counts": domain_category_counts,
                "placement_category_counts": placement_category_counts,
            }
            self._placement_category_changed()
            self._set_placement_detail({
                "network": "本机网络（不经过 AdsPower 代理）",
                "unique_domains": len(results), "reachable_domains": reachable_domains,
                "domain_category_counts": domain_category_counts,
                "placement_category_counts": placement_category_counts,
                "results": results,
            })
            selected_category = self.placement_category.get().strip() or "其他"
            selected_domains = domain_category_counts.get(selected_category, 0)
            selected_placements = placement_category_counts.get(selected_category, 0)
            distribution = "、".join(
                f"{category} {domain_category_counts[category]}"
                for category in WEBSITE_CATEGORIES
                if domain_category_counts.get(category)
            )
            messagebox.showinfo(
                "网站检测与分类完成",
                f"检测 {len(results)} 个唯一域名，可访问 {reachable_domains} 个。\n"
                f"当前筛选“{selected_category}”：{selected_domains} 个域名、"
                f"{selected_placements} 条展示位置。\n\n"
                f"全部分类：{distribution or '没有命中分类'}\n\n"
                f"点击“选中此分类网站”可选择“{selected_category}”类展示位置。",
            )

        self._run_async(f"检测并分类 {len(domains)} 个网站（本机网络，只读）", task, success)

    def _placement_category_changed(self) -> None:
        category = self.placement_category.get().strip() or "其他"
        context = self.placement_analysis_context
        if not context:
            self.placement_analysis_summary.set(f"尚未检测网站 · 当前筛选 {category}")
            return
        for iid, row in self.placement_items.items():
            if row.get("campaign_exclusion_status"):
                tags = ("excluded",)
            elif not row.get("eligible_for_campaign_exclusion"):
                tags = ("unsupported",)
            elif str(row.get("website_category") or "") == category:
                tags = ("category_selected",)
            else:
                tags = ("website",)
            self.placement_tree.item(iid, tags=tags)
        domain_count = int((context.get("domain_category_counts") or {}).get(category, 0))
        placement_count = int((context.get("placement_category_counts") or {}).get(category, 0))
        self.placement_analysis_summary.set(
            f"检测 {context.get('unique_domains', 0)} 个域名 · "
            f"可访问 {context.get('reachable_domains', 0)} · "
            f"{category} {domain_count} 域名 / {placement_count} 位置"
        )

    def _select_placement_category(self) -> None:
        category = self.placement_category.get().strip()
        matched = [
            iid for iid, row in self.placement_items.items()
            if row.get("eligible_for_campaign_exclusion")
            and str(row.get("website_category") or "") == category
        ]
        if not matched:
            messagebox.showinfo("没有匹配网站", f"没有已分析为“{category}”的网站；请先执行智能分类。")
            return
        self.placement_tree.selection_set(*matched)
        self.placement_tree.see(matched[0])
        self._placement_selection_changed()

    def _selected_placement_rows(self) -> list[dict[str, Any]]:
        rows = []
        seen: set[tuple[str, str]] = set()
        for iid in self.placement_tree.selection():
            row = self.placement_items.get(iid)
            if not row or not row.get("eligible_for_campaign_exclusion"):
                continue
            key = (
                str(row.get("campaign_id") or ""),
                str(row.get("normalized_placement") or row.get("placement") or ""),
            )
            if key not in seen:
                seen.add(key)
                rows.append(row)
        return rows

    def _placement_selection_changed(self) -> None:
        rows = self._selected_placement_rows()
        self.placement_selection_text.set(f"已选择 {len(rows)} 个网站展示位置")
        if not rows:
            return
        lines = [f"已选择 {len(rows)} 个网站展示位置："]
        for row in rows[:80]:
            lines.append(
                f"{row.get('campaign_name')} | {row.get('ad_group_name')} | "
                f"{row.get('normalized_placement') or row.get('placement')} | "
                f"分类 {row.get('website_category') or '未分析'} | "
                f"可访问性 {row.get('access_status') or '未检测'} | "
                f"互动 {row.get('interactions')} | {row.get('currency')} {float(row.get('cost') or 0):.6f}"
            )
        if len(rows) > 80:
            lines.append(f"……其余 {len(rows) - 80} 条未显示")
        self._set_placement_detail("\n".join(lines))

    def _clear_placement_selection(self) -> None:
        selected = self.placement_tree.selection()
        if selected:
            self.placement_tree.selection_remove(*selected)

    def _select_placement_campaign(self) -> None:
        selected = self.placement_tree.selection()
        if not selected:
            messagebox.showinfo("没有定位广告系列", "请先选择一个系列、广告组或展示位置")
            return
        iid = selected[0]
        while iid and self.placement_tree.parent(iid):
            iid = self.placement_tree.parent(iid)
        node = self.placement_nodes.get(iid)
        if not node or node.get("level") != "campaign":
            return
        leaf_ids = []
        for group_iid in self.placement_tree.get_children(iid):
            for placement_iid in self.placement_tree.get_children(group_iid):
                row = self.placement_items.get(placement_iid)
                if row and row.get("eligible_for_campaign_exclusion"):
                    leaf_ids.append(placement_iid)
        if leaf_ids:
            self.placement_tree.selection_set(*leaf_ids)
            self.placement_tree.see(leaf_ids[0])
        else:
            messagebox.showinfo("没有可排除网站", "该广告系列没有支持系列排除的网站展示位置")

    def _placement_query_matches(
        self,
    ) -> tuple[str, str, str, Any, list[dict[str, Any]]]:
        browser_type, profile, cid, report_date = self._placement_context()
        context = self.placement_query_context or {}
        if (
            context.get("browser_type") != browser_type
            or context.get("profile") != profile
            or context.get("cid") != cid
            or context.get("date") != report_date
        ):
            raise ValueError("窗口、CID 或日期已改变，请重新查询展示位置")
        rows = self._selected_placement_rows()
        if not rows:
            raise ValueError("请在层级列表中选择至少一个网站展示位置")
        return browser_type, profile, cid, report_date, rows

    @staticmethod
    def _placement_report_base(
        browser_type: str, profile: str, cid: str, mode: str,
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "mode": mode,
            "browser_type": browser_type,
            "profile_id": profile,
            "started_at": datetime.now().astimezone().isoformat(),
            "success": False,
            "accounts": [{
                "cid": cid, "account_status": "", "actions": [],
            }],
        }

    def _placement_exclusion_task(
        self, *, execute: bool,
    ) -> tuple[tuple[str, str, str, Any, list[dict[str, Any]]], Callable[[], Any]] | None:
        try:
            context = self._placement_query_matches()
        except Exception as exc:
            messagebox.showerror("不能执行广告系列排除", str(exc))
            return None
        browser_type, profile, cid, report_date, rows = context

        def task():
            mode = "placement_exclusion" if execute else "placement_exclusion_preflight"
            report = self._placement_report_base(browser_type, profile, cid, mode)
            try:
                with PlacementManager(
                    profile, cid, lambda text: self.events.put(("log", text)),
                    browser_type=browser_type,
                ) as manager:
                    result = manager.exclude(rows, report_date) if execute else manager.preflight(
                        rows, report_date
                    )
                report.update(result)
                report["success"] = True
                report["accounts"][0]["account_status"] = "可用"
                report["accounts"][0]["actions"] = [{
                    "kind": "campaign_placement_exclusion",
                    "status": "created" if execute else "would_create",
                    "object_name": str(group["campaign_name"]),
                    "reason": (
                        f"新增 {len(group['would_add'])}；已存在 {len(group['already_excluded'])}；"
                        + ("写后回读通过" if execute else "此次未写入")
                    ),
                } for group in result["campaigns"]]
                return result
            except Exception as exc:
                report["error"] = str(exc)
                report["accounts"][0]["actions"] = [{
                    "kind": "campaign_placement_exclusion",
                    "status": "failed",
                    "error": str(exc),
                }]
                raise
            finally:
                report["finished_at"] = datetime.now().astimezone().isoformat()
                save_run_report(report)

        return context, task

    def _placement_exclusion_preflight(self) -> None:
        prepared = self._placement_exclusion_task(execute=False)
        if not prepared:
            return
        context, task = prepared

        def success(result):
            self._set_placement_detail(result)
            self._refresh_reports()
            messagebox.showinfo(
                "广告系列排除预检完成",
                f"涉及 {len(result['campaigns'])} 个广告系列；将新增 {result['would_add_count']} 个，"
                f"已有排除 {result['already_excluded_count']} 个。此次没有写入。",
            )

        self._run_async("广告系列展示位置排除预检（只读）", task, success)

    def _placement_exclusion_execute(self) -> None:
        prepared = self._placement_exclusion_task(execute=True)
        if not prepared:
            return
        context, task = prepared
        _browser_type, profile, cid, report_date, rows = context
        campaign_count = len({str(row.get("campaign_id") or "") for row in rows})
        prompt = (
            f"CID：{format_cid(cid)}\n"
            f"报表日期：{report_date.label}\n"
            f"网站展示位置：{len(rows)} 个\n"
            f"涉及广告系列：{campaign_count} 个\n\n"
            "将为每个网站创建对应广告系列级 negative placement。\n"
            "已有排除会自动跳过；写后必须逐系列回读确认。确定继续？"
        )
        if not messagebox.askyesno("确认从广告系列中排除", prompt):
            return

        def success(result):
            excluded_keys = {
                (str(group["campaign_id"]), str(value))
                for group in result["campaigns"] for value in group["selected"]
            }
            for iid, row in self.placement_items.items():
                key = (
                    str(row.get("campaign_id") or ""),
                    str(row.get("normalized_placement") or ""),
                )
                if key in excluded_keys:
                    row["campaign_exclusion_status"] = "已从系列排除"
                    self.placement_tree.set(iid, "exclusion_status", "已从系列排除")
                    self.placement_tree.item(iid, tags=("excluded",))
            self._clear_placement_selection()
            self._set_placement_detail(result)
            self._refresh_reports()
            created = int(result.get("created_count") or 0)
            existing = int(result.get("already_excluded_count") or 0)
            messagebox.showinfo(
                "广告系列排除完成",
                f"新增 {created} 个系列级展示位置排除；已有并跳过 {existing} 个。\n"
                "全部新增项目均已写后回读确认。",
            )

        self._run_async(
            f"从 {campaign_count} 个广告系列排除 {len(rows)} 个网站展示位置",
            task,
            success,
        )

    def _start_batch(self, dry_run: bool) -> None:
        try:
            jobs = self._window_jobs(required=True, selected_only=True)
            identities = [
                (browser_type, profile)
                for browser_type, profile, _assignments in jobs
            ]
            missing_window_numbers = {
                window_key(
                    self.task_tree.set(window_iid, "browser_type"),
                    self.task_tree.set(window_iid, "profile"),
                )
                for window_iid in self.task_tree.get_children("")
                if (
                    normalize_browser_type(self.task_tree.set(window_iid, "browser_type")),
                    str(self.task_tree.set(window_iid, "profile")),
                ) in identities
                if not str(self.task_tree.set(window_iid, "window_number")).strip()
            }
            enabled = {key for key, value in self.enabled.items() if value.get()}
            if not enabled:
                raise ValueError("请至少勾选一个执行对象")
            save_config(self.config_data)
            self._save_state()
        except Exception as exc:
            messagebox.showerror("输入有误", str(exc))
            return
        total_cids = sum(len(assignments) for _type, _profile, assignments in jobs)
        if not dry_run:
            selected_objects = "、".join(
                f"{key}·{TYPE_LABELS[key]}" for key in TYPE_LABELS if key in enabled
            )
            target_lines = "\n".join(
                f"· {browser_type} / {profile} / {format_cid(item.cid)} / {item.dictionary}"
                for browser_type, profile, assignments in jobs
                for item in assignments
            )
            prompt = (
                f"执行范围：仅当前选中的 {total_cids} 个 CID\n"
                f"{target_lines}\n"
                f"执行对象：{selected_objects}\n\n"
                "其他已分配 CID 不会执行。将在写入后回读校验，确定继续？"
            )
            if not messagebox.askyesno("确认执行选中 CID", prompt):
                return
        def task():
            paths: list[Path] = []
            statuses: dict[str, dict[str, dict[str, Any]]] = {}
            jobs_by_window = {
                window_key(browser_type, profile): assignments
                for browser_type, profile, assignments in jobs
            }
            discovered_count = 0
            failures: list[tuple[str, str]] = []
            for browser_type, profile in identities:
                profile_key = window_key(browser_type, profile)
                assignments = jobs_by_window.get(profile_key, [])
                self.events.put(("log", f"连接指纹浏览器窗口：{browser_type} / {profile}"))
                if profile_key in missing_window_numbers:
                    try:
                        info = self._resolve_window_metadata(
                            profile,
                            browser_type=browser_type,
                            force_refresh=False,
                        )
                        self.events.put(("window_metadata", {profile_key: info}))
                    except Exception as exc:
                        self.events.put(("log", f"[{profile}] 指纹浏览器窗口资料读取失败，保留现有资料：{exc}"))
                runner = ProtocolRunner(
                    profile, lambda text: self.events.put(("log", text)),
                    browser_type=browser_type,
                )
                try:
                    if dry_run and not assignments:
                        found = runner.account_snapshots()
                        report = {
                            "version": 1,
                            "browser_type": browser_type,
                            "profile_id": profile,
                            "window_key": profile_key,
                            "mode": "preflight",
                            "started_at": datetime.now().astimezone().isoformat(),
                            "finished_at": datetime.now().astimezone().isoformat(),
                            "success": True,
                            "accounts": [],
                            "discovered_accounts": list(found.values()),
                            "note": "只刷新账户列表；没有已分配内容方案的 CID",
                        }
                        self.events.put(("log", f"[{profile}] 已列出全部 CID；没有已分配内容方案的 CID，跳过内容预检"))
                    else:
                        report = runner.run(assignments, self.config_data, enabled, dry_run=dry_run)
                    report["browser_type"] = browser_type
                    report["profile_id"] = profile
                    report["window_key"] = profile_key
                except Exception as exc:
                    if runner.report:
                        report = runner.report
                    else:
                        now = datetime.now().astimezone().isoformat()
                        report = {
                            "version": 1,
                            "browser_type": browser_type,
                            "profile_id": profile,
                            "window_key": profile_key,
                            "mode": "preflight" if dry_run else "write",
                            "started_at": now,
                            "finished_at": now,
                            "success": False,
                            "error": str(exc),
                            "accounts": [],
                        }
                    report["browser_type"] = browser_type
                    report["profile_id"] = profile
                    report["window_key"] = profile_key
                    paths.append(save_run_report(report))
                    failures.append((profile_key, str(exc)))
                    self.events.put(("log", f"[{profile}] 任务失败，继续处理其他窗口：{exc}"))
                    discovered = report.get("discovered_accounts") or []
                    if discovered:
                        found = {str(item.get("cid")): item for item in discovered if item.get("cid")}
                        statuses[profile_key] = found
                        discovered_count += len(found)
                        self.events.put(("account_statuses", {profile_key: found}))
                    continue
                paths.append(save_run_report(report))
                if dry_run:
                    discovered = report.get("discovered_accounts") or []
                    found = {str(item.get("cid")): item for item in discovered if item.get("cid")}
                    statuses[profile_key] = found
                    discovered_count += len(found)
                    self.events.put(("account_statuses", {profile_key: found}))
                else:
                    statuses.setdefault(profile_key, {}).update({
                        str(item.get("cid")): {
                            "ocid": item.get("ocid", ""),
                            "name": item.get("account_name", ""),
                            "account_type": item.get("account_type", "CID"),
                            "is_manager": item.get("is_manager", False),
                            "parent_mcc": " / ".join(item.get("parent_mcc_cids") or []),
                            "depth": item.get("depth", ""),
                            "status": item.get("account_status", "状态未知"),
                            "status_detail": item.get("account_status_detail", ""),
                        }
                        for item in report.get("accounts") or []
                    })
            return paths, statuses, discovered_count, failures
        def success(result):
            paths, statuses, discovered_count, failures = result
            self._apply_account_statuses(statuses)
            for path in paths:
                self._log(f"报告：{path}")
            self._refresh_reports()
            self._select_report_from_paths(paths, prefer_failure=bool(failures))
            if failures:
                self.notebook.select(self.report_tab)
                summary = "\n".join(f"{profile}：{reason}" for profile, reason in failures[:5])
                messagebox.showwarning(
                    "预检存在失败" if dry_run else "批量写入存在失败",
                    f"成功处理 {(len(identities) if dry_run else len(jobs)) - len(failures)} 个窗口，失败 {len(failures)} 个。\n\n{summary}\n\n"
                    "运行记录已显示具体原因，其他窗口仍已继续处理。",
                )
                return
            if dry_run:
                messagebox.showinfo(
                    "预检完成",
                    f"已从 {len(identities)} 个指纹浏览器窗口列出 {discovered_count} 个 CID；"
                    f"其中 {total_cids} 个已分配 CID 完成对象预检",
                )
            else:
                messagebox.showinfo("完成", f"已处理 {len(jobs)} 个指纹浏览器窗口、{total_cids} 个 CID")
        self._run_async("预检内容写入（只读）" if dry_run else "批量协议写入", task, success)

    # ----- local library CRUD ------------------------------------------------------
    def _show_library_context_menu(self, event: tk.Event) -> None:
        iid = self._select_row_at_pointer(self.library_tree, event)
        menu = self.library_context_menu
        menu.delete(0, "end")
        menu.add_command(label="新增内容方案", command=self._new_library)
        if iid:
            menu.add_separator()
            menu.add_command(label="保存当前方案", command=self._save_current_library)
            menu.add_command(label="重命名方案", command=self._rename_library)
            menu.add_command(label="复制方案", command=self._duplicate_library)
            menu.add_separator()
            menu.add_command(
                label="复制方案名称",
                command=lambda: self._copy_text(self._selected_library_name() or "", "方案名称"),
            )
            menu.add_command(label="删除方案", command=self._delete_library)
        menu.add_separator()
        menu.add_command(label="导入 JSON", command=self._import_json)
        menu.add_command(label="导出 JSON", command=self._export_json)
        self._popup_menu(menu, event)

    def _library_double_click(self, event: tk.Event) -> str | None:
        iid = self._select_row_at_pointer(self.library_tree, event)
        if not iid:
            return None
        self._rename_library()
        return "break"

    def _refresh_library_tree(self, select_name: str | None = None) -> None:
        self.library_tree.delete(*self.library_tree.get_children())
        for name in self._plan_names():
            iid = self.library_tree.insert("", "end", values=(name,))
            if name == select_name:
                self.library_tree.selection_set(iid)
        if not self.library_tree.selection() and self.library_tree.get_children():
            self.library_tree.selection_set(self.library_tree.get_children()[0])
        self._load_library_selection()

    def _selected_library_name(self) -> str | None:
        selected = self.library_tree.selection()
        return str(self.library_tree.item(selected[0], "values")[0]) if selected else None

    def _load_library_selection(self) -> None:
        name = self._selected_library_name()
        if not name:
            return
        self.library_title.configure(text=f"方案：{name}")
        plan = self.config_data["dictionaries"][name]
        configured_names = plan.get("object_names") or {}
        for key, variable in self.library_object_names.items():
            variable.set(str(configured_names.get(key) or DEFAULT_OBJECT_NAMES[key]))
        self.library_app_list.selection_clear(0, "end")
        app_value = plan.get("app", [])
        if app_value == "@ALL_APP_CATEGORIES":
            selected_ids = set(ALL_APP_CATEGORY_IDS)
        else:
            raw = app_value if isinstance(app_value, list) else [app_value]
            selected_ids = {int(value) for value in raw if str(value).isdigit()}
        for index, (category_id, _label) in enumerate(APP_CATEGORIES):
            if category_id in selected_ids:
                self.library_app_list.selection_set(index)
        for key, text in self.library_texts.items():
            text.delete("1.0", "end")
            value = plan.get(key, [])
            rows = value if isinstance(value, list) else ([value] if value else [])
            text.insert("1.0", "\n".join(str(item) for item in rows))

    def _save_current_library(self) -> None:
        name = self._selected_library_name()
        if not name:
            return
        app_ids = [APP_CATEGORIES[index][0] for index in self.library_app_list.curselection()]
        app_value: Any = "@ALL_APP_CATEGORIES" if set(app_ids) == set(ALL_APP_CATEGORY_IDS) else app_ids
        object_names = {
            key: variable.get().strip()
            for key, variable in self.library_object_names.items()
        }
        empty_names = [key for key, value in object_names.items() if not value]
        if empty_names:
            messagebox.showerror(
                "保存失败",
                "Google Ads 对象名称不能为空：" + ", ".join(empty_names),
            )
            return
        plan = {"app": app_value, "object_names": object_names}
        for key, text in self.library_texts.items():
            plan[key] = [line.strip() for line in text.get("1.0", "end").splitlines() if line.strip()]
        self.config_data["dictionaries"][name] = plan
        try:
            resolve_object_names(self.config_data, name)
            save_config(self.config_data)
            self._log(f"已保存内容方案：{name}")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _new_library(self) -> None:
        name = simpledialog.askstring("新增方案", "方案名称", parent=self)
        if not name:
            return
        name = name.strip()
        if name in self.config_data["dictionaries"]:
            messagebox.showerror("新增失败", "名称已经存在")
            return
        self.config_data["dictionaries"][name] = {
            "app": "@ALL_APP_CATEGORIES",
            "web": [],
            "xq": [],
            "ss": [],
            "object_names": dict(DEFAULT_OBJECT_NAMES),
        }
        save_config(self.config_data)
        self._refresh_library_tree(name)

    def _rename_library(self) -> None:
        old = self._selected_library_name()
        if not old:
            return
        new = simpledialog.askstring("重命名方案", "新名称", initialvalue=old, parent=self)
        if not new or new.strip() == old:
            return
        new = new.strip()
        if new in self.config_data["dictionaries"]:
            messagebox.showerror("重命名失败", "名称已经存在")
            return
        dictionaries = self.config_data["dictionaries"]
        dictionaries[new] = dictionaries.pop(old)
        self._replace_reference(self.config_data, "@" + old, "@" + new)
        for window_iid in self.task_tree.get_children(""):
            for iid in self._window_rows(window_iid):
                if self.task_tree.set(iid, "dictionary") == old:
                    self.task_tree.set(iid, "dictionary", new)
                    self.task_tree.set(iid, "updated_at", local_timestamp())
        save_config(self.config_data)
        self._refresh_library_tree(new)

    @staticmethod
    def _replace_reference(value: Any, old: str, new: str) -> Any:
        if isinstance(value, dict):
            for key, child in value.items():
                value[key] = GGAdsApp._replace_reference(child, old, new)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                value[index] = GGAdsApp._replace_reference(child, old, new)
        elif value == old:
            return new
        return value

    def _duplicate_library(self) -> None:
        old = self._selected_library_name()
        if not old:
            return
        new = simpledialog.askstring("复制方案", "新方案名称", initialvalue=old + "-副本", parent=self)
        if not new:
            return
        new = new.strip()
        if new in self.config_data["dictionaries"]:
            messagebox.showerror("复制失败", "名称已经存在")
            return
        self.config_data["dictionaries"][new] = copy.deepcopy(self.config_data["dictionaries"][old])
        save_config(self.config_data)
        self._refresh_library_tree(new)

    def _delete_library(self) -> None:
        name = self._selected_library_name()
        if not name:
            return
        if any(
            self.task_tree.set(iid, "dictionary") == name
            for window_iid in self.task_tree.get_children("")
            for iid in self._window_rows(window_iid)
        ):
            messagebox.showerror("不能删除", "该方案仍被 CID 任务引用")
            return
        raw = json.dumps(self.config_data, ensure_ascii=False)
        if f'@{name}' in raw.replace(f'"{name}":', "", 1):
            messagebox.showerror("不能删除", "该方案仍被其他字典引用")
            return
        if messagebox.askyesno("确认删除", f"删除本地内容方案“{name}”？"):
            del self.config_data["dictionaries"][name]
            save_config(self.config_data)
            self._refresh_library_tree()

    # ----- local advertising creative library ------------------------------------
    def _selected_creative_plan(self) -> tuple[str, str, dict[str, Any]] | None:
        selected = self.creative_tree.selection()
        if not selected:
            return None
        return getattr(self, "creative_tree_items", {}).get(selected[0])

    @staticmethod
    def _display_time(value: Any) -> str:
        return str(value or "").replace("T", " ")[:19]

    @staticmethod
    def _make_creative_placeholder(
        label: str, background: str, foreground: str,
    ) -> ImageTk.PhotoImage:
        image = Image.new("RGB", THUMBNAIL_SIZE, background)
        draw = ImageDraw.Draw(image)
        box = draw.textbbox((0, 0), label)
        x = max(2, (THUMBNAIL_SIZE[0] - (box[2] - box[0])) // 2)
        y = max(2, (THUMBNAIL_SIZE[1] - (box[3] - box[1])) // 2)
        draw.rectangle((0, 0, THUMBNAIL_SIZE[0] - 1, THUMBNAIL_SIZE[1] - 1), outline=foreground)
        draw.text((x, y), label, fill=foreground)
        return ImageTk.PhotoImage(image)

    def _cancel_creative_thumbnail_jobs(self) -> None:
        self._creative_thumbnail_generation += 1
        for future in self._creative_thumbnail_futures:
            future.cancel()
        self._creative_thumbnail_futures.clear()
        self._creative_thumbnail_photos.clear()

    def _queue_creative_thumbnail(
        self, iid: str, item: dict[str, Any], image_count: int,
    ) -> None:
        images = item.get("images") or {}
        paths = [
            str(path)
            for role in ("landscape", "square", "portrait")
            for path in images.get(role) or []
        ]
        generation = self._creative_thumbnail_generation
        future = self._creative_thumbnail_executor.submit(load_creative_thumbnail, paths)
        self._creative_thumbnail_futures.add(future)

        def completed(done: Future[CreativeThumbnail]) -> None:
            if done.cancelled():
                return
            try:
                result = done.result()
            except Exception as exc:  # defensive boundary around image decoders
                result = CreativeThumbnail("damaged", message=str(exc))
            self.events.put(("creative_thumbnail", (generation, iid, image_count, result)))

        future.add_done_callback(completed)

    def _apply_creative_thumbnail(
        self, generation: int, iid: str, image_count: int, result: CreativeThumbnail,
    ) -> None:
        if generation != self._creative_thumbnail_generation or not self.creative_tree.exists(iid):
            return
        label_by_status = {
            "empty": "无图片",
            "missing": "图片缺失",
            "damaged": "图片损坏",
        }
        if result.status == "ready" and result.png_data:
            try:
                with Image.open(BytesIO(result.png_data)) as source:
                    photo = ImageTk.PhotoImage(source.copy())
                self._creative_thumbnail_photos[iid] = photo
                self.creative_tree.item(iid, image=photo, text=f"{image_count} 张")
                return
            except (OSError, UnidentifiedImageError, tk.TclError):
                result = CreativeThumbnail("damaged", result.path, message="缩略图渲染失败")
        status = result.status if result.status in label_by_status else "damaged"
        self.creative_tree.item(
            iid,
            image=self._creative_thumbnail_placeholders[status],
            text=label_by_status[status],
        )

    def _refresh_creative_library_tree(
        self, select: tuple[str, str] | None = None, *, reset_page: bool = False,
    ) -> None:
        if select is None:
            current = self._selected_creative_plan()
            select = (current[0], current[1]) if current else None
        self._cancel_creative_thumbnail_jobs()
        self.creative_tree.delete(*self.creative_tree.get_children())
        self.creative_tree_items: dict[str, tuple[str, str, dict[str, Any]]] = {}
        entries: list[tuple[str, str, dict[str, Any], str, str]] = []
        for kind, section, label in (
            ("image", "image_plans", "图片方案"),
            ("copy", "copy_plans", "文案方案"),
        ):
            for name, item in sorted(
                self.creative_library.get(section, {}).items(), key=lambda pair: pair[0].casefold()
            ):
                if kind == "image":
                    images = item.get("images") or {}
                    summary = (
                        f"横图 {len(images.get('landscape') or [])} · "
                        f"方图 {len(images.get('square') or [])} · "
                        f"竖图 {len(images.get('portrait') or [])}"
                    )
                else:
                    populated = sum(bool(item.get(key)) for key in (
                        "final_url", "business_name", "long_headline", "headlines", "descriptions"
                    ))
                    summary = (
                        f"已填写 {populated}/5 类字段 · 短标题 {len(item.get('headlines') or [])} · "
                        f"描述 {len(item.get('descriptions') or [])}"
                    )
                entries.append((kind, name, item, label, summary))

        keyword = self.creative_search.get().strip().casefold()
        type_filter = self.creative_type_filter.get()
        filtered = []
        for entry in entries:
            kind, name, item, label, summary = entry
            if type_filter == "图片方案" and kind != "image":
                continue
            if type_filter == "文案方案" and kind != "copy":
                continue
            haystack = "\n".join((
                name, label, summary, str(item.get("note") or ""),
                json.dumps(item, ensure_ascii=False),
            )).casefold()
            if keyword and keyword not in haystack:
                continue
            filtered.append(entry)
        try:
            page_size = int(self.creative_page_size.get())
        except ValueError:
            page_size = 20
        page_size = max(10, min(page_size, 100))
        total = len(filtered)
        total_pages = max(1, (total + page_size - 1) // page_size)
        if reset_page:
            self.creative_page_index = 0
        if select:
            selected_index = next(
                (index for index, entry in enumerate(filtered) if (entry[0], entry[1]) == select),
                None,
            )
            if selected_index is not None:
                self.creative_page_index = selected_index // page_size
        self.creative_page_index = max(0, min(self.creative_page_index, total_pages - 1))
        start = self.creative_page_index * page_size
        page_entries = filtered[start:start + page_size]
        selected_iid = ""
        for kind, name, item, label, summary in page_entries:
            if kind == "image":
                images = item.get("images") or {}
                image_count = sum(len(images.get(role) or []) for role in (
                    "landscape", "square", "portrait",
                ))
                preview_image = self._creative_thumbnail_placeholders["loading"]
                preview_text = "加载中"
            else:
                image_count = 0
                preview_image = self._creative_thumbnail_placeholders["copy"]
                preview_text = "文案"
            iid = self.creative_tree.insert(
                "", "end",
                image=preview_image,
                text=preview_text,
                values=(
                    label, name, summary, str(item.get("note") or ""),
                    self._display_time(item.get("created_at")),
                    self._display_time(item.get("updated_at")),
                ),
                tags=(kind,),
            )
            self.creative_tree_items[iid] = (kind, name, item)
            if kind == "image":
                self._queue_creative_thumbnail(iid, item, image_count)
            if select == (kind, name):
                selected_iid = iid
        if selected_iid:
            self.creative_tree.selection_set(selected_iid)
            self.creative_tree.see(selected_iid)
        elif self.creative_tree.get_children():
            self.creative_tree.selection_set(self.creative_tree.get_children()[0])
        shown_start = start + 1 if total else 0
        shown_end = min(start + page_size, total)
        self.creative_page_info.set(
            f"第 {self.creative_page_index + 1}/{total_pages} 页 · "
            f"显示 {shown_start}-{shown_end} · 共 {total} 个方案"
        )
        self.creative_prev_button.configure(
            state="normal" if self.creative_page_index > 0 else "disabled"
        )
        self.creative_next_button.configure(
            state="normal" if self.creative_page_index + 1 < total_pages else "disabled"
        )
        self._show_creative_plan_detail()
        self._refresh_ad_plan_selectors()

    def _apply_creative_filters(self) -> None:
        self.creative_tree.selection_remove(self.creative_tree.selection())
        self._refresh_creative_library_tree(reset_page=True)

    def _clear_creative_filters(self) -> None:
        self.creative_search.set("")
        self.creative_type_filter.set("全部类型")
        self.creative_tree.selection_remove(self.creative_tree.selection())
        self._refresh_creative_library_tree(reset_page=True)

    def _move_creative_page(self, delta: int) -> None:
        self.creative_page_index += delta
        self.creative_tree.selection_remove(self.creative_tree.selection())
        self._refresh_creative_library_tree()

    def _show_creative_plan_detail(self) -> None:
        selected = self._selected_creative_plan()
        payload: dict[str, Any] = {}
        if selected:
            kind, name, item = selected
            payload = {"type": "图片方案" if kind == "image" else "文案方案", "name": name, **item}
            if kind == "image":
                payload["file_status"] = {
                    role: [
                        {"path": path, "exists": Path(path).is_file()}
                        for path in (item.get("images") or {}).get(role) or []
                    ]
                    for role in ROLE_LABELS
                }
        self.creative_detail.configure(state="normal")
        self.creative_detail.delete("1.0", "end")
        self.creative_detail.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))
        self.creative_detail.configure(state="disabled")

    def _save_creative_plan_dialog(
        self,
        kind: str,
        *,
        old_name: str = "",
        name: str = "",
        item: dict[str, Any] | None = None,
    ) -> None:
        dialog = CreativePlanDialog(self, kind, name=name, item=item)
        if not dialog.result:
            return
        new_name, value = dialog.result
        try:
            if kind == "image":
                value["images"] = import_image_files(value.get("images") or {})
            put_plan(self.creative_library, kind, new_name, value, old_name=old_name)
            save_creative_library(self.creative_library)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self._refresh_creative_library_tree((kind, new_name))
        self._log(f"已保存本地{'图片' if kind == 'image' else '文案'}方案：{new_name}")

    def _new_creative_plan(self, kind: str) -> None:
        self._save_creative_plan_dialog(kind)

    def _edit_creative_plan(self) -> None:
        selected = self._selected_creative_plan()
        if not selected:
            messagebox.showinfo("请选择方案", "请先选择图片方案或文案方案")
            return
        kind, name, item = selected
        self._save_creative_plan_dialog(
            kind, old_name=name, name=name, item=copy.deepcopy(item),
        )

    def _duplicate_creative_plan(self) -> None:
        selected = self._selected_creative_plan()
        if not selected:
            messagebox.showinfo("请选择方案", "请先选择需要复制的方案")
            return
        kind, name, item = selected
        self._save_creative_plan_dialog(
            kind, name=name + "-副本", item=copy.deepcopy(item),
        )

    def _delete_creative_plan(self) -> None:
        selected = self._selected_creative_plan()
        if not selected:
            return
        kind, name, _item = selected
        if not messagebox.askyesno(
            "确认删除", f"删除本地{'图片' if kind == 'image' else '文案'}方案“{name}”？",
        ):
            return
        try:
            delete_plan(self.creative_library, kind, name)
            save_creative_library(self.creative_library)
        except Exception as exc:
            messagebox.showerror("删除失败", str(exc))
            return
        self._refresh_creative_library_tree()
        self._log(f"已删除本地素材方案：{name}")

    def _creative_plan_double_click(self, event: tk.Event) -> str | None:
        if not self._select_row_at_pointer(self.creative_tree, event):
            return None
        self._edit_creative_plan()
        return "break"

    def _show_creative_plan_context_menu(self, event: tk.Event) -> None:
        iid = self._select_row_at_pointer(self.creative_tree, event)
        menu = self.creative_context_menu
        menu.delete(0, "end")
        menu.add_command(label="新增图片方案", command=lambda: self._new_creative_plan("image"))
        menu.add_command(label="新增文案方案", command=lambda: self._new_creative_plan("copy"))
        if iid:
            menu.add_separator()
            menu.add_command(label="应用到广告创建", command=self._apply_selected_creative_plan)
            menu.add_command(label="编辑", command=self._edit_creative_plan)
            menu.add_command(label="复制", command=self._duplicate_creative_plan)
            menu.add_command(label="删除", command=self._delete_creative_plan)
        self._popup_menu(menu, event)

    def _refresh_ad_plan_selectors(self) -> None:
        if not hasattr(self, "ad_image_plan_box"):
            return
        image_values = [
            "（不使用图片方案）", "（手工选择本地图片）",
            *sorted(self.creative_library["image_plans"]),
        ]
        copy_values = ["（不使用文案方案）", *sorted(self.creative_library["copy_plans"])]
        self.ad_image_plan_box.configure(values=image_values)
        self.ad_copy_plan_box.configure(values=copy_values)
        if self.ad_image_plan.get() not in image_values:
            self.ad_image_plan.set(image_values[0])
        if self.ad_copy_plan.get() not in copy_values:
            self.ad_copy_plan.set(copy_values[0])

    def _import_creative_template(self) -> None:
        path = filedialog.askopenfilename(
            title="导入广告素材模板",
            filetypes=[("GGAds 素材模板", "*.json"), ("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            imported = import_creative_template(self.creative_library, path)
            save_creative_library(self.creative_library)
        except Exception as exc:
            messagebox.showerror("模板导入失败", str(exc))
            return
        last = imported[-1]
        self._refresh_creative_library_tree((last["kind"], last["name"]))
        renamed = [row for row in imported if row["source_name"] != row["name"]]
        detail = ""
        if renamed:
            detail = "\n\n重名方案已自动改名：\n" + "\n".join(
                f"{row['source_name']} → {row['name']}" for row in renamed[:10]
            )
        self._log(f"已导入广告素材模板：{path}，共 {len(imported)} 个方案")
        messagebox.showinfo("模板导入完成", f"已导入 {len(imported)} 个图片/文案方案。{detail}")

    def _export_creative_template(self, selected_only: bool) -> None:
        selection = None
        if selected_only:
            selected = self._selected_creative_plan()
            if not selected:
                messagebox.showinfo("请选择方案", "请先选择需要导出的图片或文案方案")
                return
            selection = (selected[0], selected[1])
            suggested = selected[1]
        else:
            suggested = "GGAds-广告素材模板"
        path = filedialog.asksaveasfilename(
            title="导出广告素材模板",
            defaultextension=".json",
            initialfile=f"{suggested}.json",
            filetypes=[("GGAds 素材模板", "*.json"), ("JSON 文件", "*.json")],
        )
        if not path:
            return
        try:
            target = export_creative_template(
                self.creative_library, path, selection=selection,
            )
        except Exception as exc:
            messagebox.showerror("模板导出失败", str(exc))
            return
        self._log(f"已导出广告素材模板：{target}")
        messagebox.showinfo(
            "模板导出完成",
            f"模板：{target}\n图片会放在同目录的“{target.stem}-assets”文件夹。",
        )

    def _creative_upload_window_identity(self) -> tuple[str, str]:
        return self._selected_window_from_mapping(
            self.creative_upload_window.get(),
            getattr(self, "creative_upload_profile_by_label", {}),
        )

    def _sync_creative_upload_cids(self) -> None:
        if not hasattr(self, "creative_upload_window_box"):
            return
        current = self._creative_upload_window_identity()
        windows = self._window_selector_rows()
        self.creative_upload_profile_by_label = {
            label: identity for label, identity, _iid in windows
        }
        identities = [identity for _label, identity, _iid in windows]
        if current not in identities:
            current = identities[0] if identities else ("", "")
        label = next((label for label, identity, _iid in windows if identity == current), "")
        self.creative_upload_window_box.configure(values=[row[0] for row in windows])
        self.creative_upload_window.set(label)
        window_iid = next(
            (iid for _label, identity, iid in windows if identity == current), None
        )
        self._configure_cid_selector(
            self.creative_upload_cid_box,
            self.creative_upload_cid,
            self._cid_selector_rows(window_iid),
        )
        self._invalidate_remote_asset_context()

    def _invalidate_remote_asset_context(self) -> None:
        context = getattr(self, "remote_asset_context", {})
        if not context:
            return
        try:
            current = self._remote_asset_target()
        except Exception:
            current = ("", "", "")
        cached = (
            str(context.get("browser_type") or ADSPOWER),
            str(context.get("profile_id") or ""),
            str(context.get("cid") or ""),
        )
        if current == cached:
            return
        self.remote_asset_rows = []
        self.remote_asset_context = {}
        self.remote_asset_search.set("")
        self._refresh_remote_asset_page(reset_page=True)

    def _remote_asset_target(self) -> tuple[str, str, str]:
        browser_type, profile = self._creative_upload_window_identity()
        if not profile:
            raise ValueError("请先选择指纹浏览器窗口")
        cid = self._cid_from_selector(self.creative_upload_cid.get())
        return browser_type, profile, cid

    def _query_remote_image_assets(self) -> None:
        try:
            browser_type, profile, cid = self._remote_asset_target()
        except Exception as exc:
            messagebox.showerror("不能查询素材", str(exc))
            return

        def task():
            with AdManager(
                profile, cid, lambda text: self.events.put(("log", text)),
                browser_type=browser_type,
            ) as manager:
                return manager.image_assets()

        def success(result):
            self.remote_asset_rows = list(result.get("rows") or [])
            self.remote_asset_page_index = 0
            self.remote_asset_context = {
                "browser_type": browser_type,
                "profile_id": profile,
                "cid": cid,
                "protocol_pages": result.get("protocol_pages"),
                "protocol_page_size": result.get("protocol_page_size"),
                "complete": result.get("complete"),
            }
            self._refresh_remote_asset_page(reset_page=True)
            self.creative_content_tabs.select(1)
            self._log(
                f"[{format_cid(cid)}] Google Ads 图片素材查询完成："
                f"{len(self.remote_asset_rows)} 条，协议 {result.get('protocol_pages')} 页"
            )

        self._run_async("分页查询 Google Ads 图片素材（只读）", task, success)

    def _filtered_remote_asset_rows(self) -> list[dict[str, Any]]:
        keyword = self.remote_asset_search.get().strip().casefold()
        if not keyword:
            return list(self.remote_asset_rows)
        return [
            row for row in self.remote_asset_rows
            if keyword in "\n".join((
                str(row.get("asset_id") or ""),
                str(row.get("name") or ""),
                str(row.get("preview_url") or ""),
            )).casefold()
        ]

    @staticmethod
    def _format_file_size(value: Any) -> str:
        try:
            size = int(value or 0)
        except (TypeError, ValueError):
            return "—"
        if size <= 0:
            return "—"
        if size >= 1024 * 1024:
            return f"{size / (1024 * 1024):.2f} MB"
        return f"{size / 1024:.1f} KB"

    def _refresh_remote_asset_page(self, *, reset_page: bool = False) -> None:
        rows = self._filtered_remote_asset_rows()
        try:
            page_size = int(self.remote_asset_page_size.get())
        except ValueError:
            page_size = 50
        page_size = max(20, min(page_size, 100))
        total = len(rows)
        total_pages = max(1, (total + page_size - 1) // page_size)
        if reset_page:
            self.remote_asset_page_index = 0
        self.remote_asset_page_index = max(
            0, min(self.remote_asset_page_index, total_pages - 1)
        )
        start = self.remote_asset_page_index * page_size
        page_rows = rows[start:start + page_size]
        self.remote_asset_tree.delete(*self.remote_asset_tree.get_children())
        for row in page_rows:
            status_raw = int(row.get("asset_status_raw") or 0)
            status = {1: "可用", 2: "已移除"}.get(status_raw, f"未知({status_raw})")
            width, height = int(row.get("width") or 0), int(row.get("height") or 0)
            self.remote_asset_tree.insert(
                "", "end",
                values=(
                    row.get("asset_id") or "", row.get("name") or "", status,
                    f"{width} × {height}" if width and height else "—",
                    self._format_file_size(row.get("file_size")),
                    f"{float(row.get('aspect_ratio') or 0):.4f}" if row.get("aspect_ratio") else "—",
                    row.get("preview_url") or "",
                ),
                tags=("enabled" if status_raw == 1 else "unknown",),
            )
        shown_start = start + 1 if total else 0
        shown_end = min(start + page_size, total)
        context = getattr(self, "remote_asset_context", {})
        cid = str(context.get("cid") or "")
        target = f"CID {format_cid(cid)} · " if cid else ""
        self.remote_asset_page_info.set(
            f"{target}第 {self.remote_asset_page_index + 1}/{total_pages} 页 · "
            f"显示 {shown_start}-{shown_end} · 共 {total} 个素材"
        )
        self.remote_asset_prev_button.configure(
            state="normal" if self.remote_asset_page_index > 0 else "disabled"
        )
        self.remote_asset_next_button.configure(
            state="normal" if self.remote_asset_page_index + 1 < total_pages else "disabled"
        )

    def _apply_remote_asset_filter(self) -> None:
        self._refresh_remote_asset_page(reset_page=True)

    def _clear_remote_asset_filter(self) -> None:
        self.remote_asset_search.set("")
        self._refresh_remote_asset_page(reset_page=True)

    def _move_remote_asset_page(self, delta: int) -> None:
        self.remote_asset_page_index += delta
        self._refresh_remote_asset_page()

    def _upload_selected_creative_plan(self) -> None:
        selected = self._selected_creative_plan()
        if not selected or selected[0] != "image":
            messagebox.showinfo("请选择图片方案", "只有图片方案可以上传到 Google Ads 素材库")
            return
        _kind, name, item = selected
        try:
            browser_type, profile = self._creative_upload_window_identity()
            if not profile:
                raise ValueError("请先选择指纹浏览器窗口")
            cid = self._cid_from_selector(self.creative_upload_cid.get())
            images = copy.deepcopy(item.get("images") or {})
            count = sum(len(paths) for paths in images.values())
            if not count:
                raise ValueError("图片方案中没有图片")
            for role in ROLE_LABELS:
                for path in images.get(role) or []:
                    validate_role_image(path, role)
        except Exception as exc:
            messagebox.showerror("不能上传", str(exc))
            return
        if not messagebox.askyesno(
            "确认上传图片方案",
            f"方案：{name}\n目标窗口：{profile}\n目标 CID：{format_cid(cid)}\n"
            f"图片数量：{count}\n\n执行前会重新读取账户状态和 Billing 余额；"
            "只有状态可用且余额大于 0 才上传。每张图片上传后会按 Asset ID 回读校验。",
        ):
            return

        def task():
            report = self._ad_report_base(
                browser_type, profile, cid, "creative_asset_upload"
            )
            try:
                with AdManager(
                    profile, cid, lambda text: self.events.put(("log", text)),
                    browser_type=browser_type,
                ) as manager:
                    result = manager.upload_image_plan(images)
                report.update(result)
                report["success"] = True
                report["accounts"][0]["account_status"] = "可用"
                report["accounts"][0]["actions"] = [
                    {
                        "kind": "asset",
                        "status": "created",
                        "object_name": row["asset_id"],
                        "reason": f"{row['role_label']} · {row['filename']} · 上传并回读验证",
                    }
                    for row in result.get("uploaded_images") or []
                ]
                return result
            except Exception as exc:
                report["error"] = str(exc)
                report["accounts"][0]["actions"] = [{
                    "kind": "asset", "status": "failed", "object_name": name, "error": str(exc),
                }]
                raise
            finally:
                report["finished_at"] = datetime.now().astimezone().isoformat()
                save_run_report(report)

        def success(result):
            plan = self.creative_library["image_plans"].get(name)
            if plan is not None:
                history = list(plan.get("uploads") or [])
                history.insert(0, {
                    "uploaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "browser_type": browser_type,
                    "profile_id": profile,
                    "cid": cid,
                    "asset_ids": list(result.get("asset_ids") or []),
                    "post_read_verified": bool(result.get("post_read_verified")),
                })
                plan["uploads"] = history[:50]
                save_creative_library(self.creative_library)
            self._refresh_creative_library_tree(("image", name))
            self._refresh_reports()
            messagebox.showinfo(
                "图片方案上传成功",
                f"目标 CID：{format_cid(cid)}\n已上传并回读验证 {len(result.get('asset_ids') or [])} 张图片。\n\n"
                + "Asset ID：\n" + "\n".join(result.get("asset_ids") or []),
            )
            self.creative_content_tabs.select(1)
            self.after(100, self._query_remote_image_assets)

        self._run_async("上传图片方案到 Google Ads 素材库", task, success)

    def _apply_ad_image_plan(self) -> None:
        name = self.ad_image_plan.get()
        if name == "（手工选择本地图片）":
            return
        plan = self.creative_library["image_plans"].get(name)
        if not plan:
            self._clear_ad_images()
            return
        try:
            selected = {role: [] for role in ROLE_LABELS}
            for role in ROLE_LABELS:
                for path in (plan.get("images") or {}).get(role) or []:
                    selected[role].append(validate_role_image(path, role).path)
        except Exception as exc:
            messagebox.showerror("图片方案不可用", f"方案“{name}”包含失效图片：\n{exc}")
            return
        self.ad_local_images = selected
        self._refresh_ad_asset_summary()
        self._log(f"广告创建已应用图片方案：{name}")

    def _apply_ad_copy_plan(self) -> None:
        name = self.ad_copy_plan.get()
        plan = self.creative_library["copy_plans"].get(name)
        if not plan:
            return
        entry_map = {
            "final_url": self.ad_final_url,
            "business_name": self.ad_business_name,
            "long_headline": self.ad_long_headline,
        }
        for key, entry in entry_map.items():
            value = str(plan.get(key) or "").strip()
            if value:
                entry.delete(0, "end")
                entry.insert(0, value)
        for key, editor in (("headlines", self.ad_headlines), ("descriptions", self.ad_descriptions)):
            values = [str(value).strip() for value in plan.get(key) or [] if str(value).strip()]
            if values:
                editor.delete("1.0", "end")
                editor.insert("1.0", "\n".join(values))
        self._log(f"广告创建已应用文案方案：{name}")

    def _apply_selected_creative_plan(self) -> None:
        selected = self._selected_creative_plan()
        if not selected:
            return
        kind, name, _item = selected
        if kind == "image":
            self.ad_image_plan.set(name)
            self._apply_ad_image_plan()
        else:
            self.ad_copy_plan.set(name)
            self._apply_ad_copy_plan()
        self.notebook.select(self.ad_tab)

    def _open_creative_library(self) -> None:
        self.notebook.select(self.local_tab)
        self.local_notebook.select(self.creative_tab)

    def _save_current_ad_copy_plan(self) -> None:
        item = {
            "final_url": self.ad_final_url.get().strip(),
            "business_name": self.ad_business_name.get().strip(),
            "long_headline": self.ad_long_headline.get().strip(),
            "headlines": [
                line.strip() for line in self.ad_headlines.get("1.0", "end").splitlines()
                if line.strip()
            ],
            "descriptions": [
                line.strip() for line in self.ad_descriptions.get("1.0", "end").splitlines()
                if line.strip()
            ],
            "note": "从广告创建页保存",
        }
        self._save_creative_plan_dialog("copy", item=item)

    def _save_current_ad_image_plan(self) -> None:
        if not any(self.ad_local_images.values()):
            messagebox.showinfo(
                "没有本地图片",
                "请先选择本地横图、方图或竖图。参考广告的 Google Asset ID 不是本地文件，不能直接存入本地图片库。",
            )
            return
        self._save_creative_plan_dialog(
            "image",
            item={
                "images": copy.deepcopy(self.ad_local_images),
                "note": "从广告创建页保存",
            },
        )

    def _advanced_json(self) -> None:
        window = tk.Toplevel(self)
        window.title("高级 JSON 编辑")
        window.geometry("850x650")
        editor_frame = ttk.Frame(window, padding=(10, 10, 10, 0))
        editor_frame.pack(fill="both", expand=True)
        editor_frame.columnconfigure(0, weight=1)
        editor_frame.rowconfigure(0, weight=1)
        text = tk.Text(editor_frame, wrap="none", font=("Consolas", 10))
        text.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(editor_frame, orient="vertical", command=text.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(editor_frame, orient="horizontal", command=text.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        text.insert("1.0", json.dumps(self.config_data, ensure_ascii=False, indent=2))
        def apply():
            try:
                value = json.loads(text.get("1.0", "end"))
                validate_config(value)
                self.config_data = value
                save_config(value)
                self._refresh_library_tree()
                window.destroy()
            except Exception as exc:
                messagebox.showerror("JSON 错误", str(exc), parent=window)
        ttk.Button(window, text="应用并保存", command=apply, style="Write.TButton").pack(pady=(0, 10))

    def _import_json(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
            validate_config(value)
            self.config_data = value
            save_config(value)
            self._refresh_library_tree()
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc))

    def _export_json(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if path:
            Path(path).write_text(json.dumps(self.config_data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ----- remote CRUD -------------------------------------------------------------
    def _show_remote_context_menu(self, event: tk.Event) -> None:
        iid = self._select_row_at_pointer(self.remote_tree, event)
        menu = self.remote_context_menu
        menu.delete(0, "end")
        menu.add_command(label="查询 / 刷新 Google Ads 对象", command=self._refresh_remote)
        menu.add_command(label="新建 Google Ads 对象", command=self._create_remote)
        menu.add_command(label="从文件导入网站排除列表", command=self._import_remote_web_exclusion)
        if iid and self._selected_remote():
            item = self._selected_remote() or {}
            menu.add_separator()
            menu.add_command(label="编辑 / 替换对象", command=self._edit_remote)
            menu.add_command(label="删除对象", command=self._delete_remote)
            menu.add_command(label="导出选中对象", command=self._export_remote_object)
            menu.add_separator()
            menu.add_command(label="复制对象名称", command=lambda: self._copy_text(str(item.get("name") or ""), "对象名称"))
            menu.add_command(label="复制 Google ID", command=lambda: self._copy_text(str(item.get("id") or ""), "Google ID"))
            menu.add_command(
                label="复制全部内容",
                command=lambda: self._copy_text("\n".join(str(value) for value in item.get("values") or []), "对象内容"),
            )
        self._popup_menu(menu, event)

    def _remote_double_click(self, event: tk.Event) -> str | None:
        iid = self._select_row_at_pointer(self.remote_tree, event)
        if not iid:
            return None
        self._edit_remote()
        return "break"

    def _remote_context(self) -> tuple[str, str, str]:
        browser_type, profile = self._remote_selected_window()
        cid = self._cid_from_selector(self.remote_cid.get())
        if not profile:
            raise ValueError("请先在批量任务页新增指纹浏览器窗口")
        return browser_type, profile, cid

    def _remote_write_context(self) -> tuple[str, str, str]:
        context = self._remote_context()
        if getattr(self, "remote_loaded_context", None) != context:
            self.remote_loaded_context = None
            self.remote_items.clear()
            tree = self.__dict__.get("remote_tree")
            if tree is not None:
                tree.delete(*tree.get_children())
            raise ValueError("指纹浏览器窗口或 CID 已切换，请先重新查询 Google Ads 对象")
        return context

    def _refresh_remote(self) -> None:
        try:
            context = self._remote_context()
            browser_type, profile, cid = context
        except Exception as exc:
            messagebox.showerror("输入有误", str(exc))
            return
        def task():
            with RemoteManager(
                profile, cid, lambda text: self.events.put(("log", text)),
                browser_type=browser_type,
            ) as manager:
                return manager.list_objects()
        def success(objects: list[dict[str, Any]]) -> None:
            self.remote_loaded_context = context
            self._show_remote_rows(objects)

        self._run_async("查询 Google Ads 对象", task, success)

    def _show_remote_rows(self, objects: list[dict[str, Any]]) -> None:
        self.remote_tree.delete(*self.remote_tree.get_children())
        self.remote_items.clear()
        for item in objects:
            group = "排除列表" if item["group"] == "exclusion" else "自定义受众"
            kind = TYPE_LABELS.get(item["kind"], item["kind"])
            iid = self.remote_tree.insert("", "end", values=(group, kind, item["name"], item["id"], item["count"]))
            self.remote_items[iid] = item
        self._log(f"Google Ads 中共 {len(objects)} 个对象")

    def _selected_remote(self) -> dict[str, Any] | None:
        selected = self.remote_tree.selection()
        return self.remote_items.get(selected[0]) if selected else None

    def _show_remote_content(self) -> None:
        item = self._selected_remote()
        if not item:
            return
        if item["kind"] == "app":
            names = {category_id: name for category_id, name in APP_CATEGORIES}
            rows = [f"{value}\t{names.get(int(value), '')}" for value in item["values"]]
        else:
            rows = [str(value) for value in item["values"]]
        self.remote_content.configure(state="normal")
        self.remote_content.delete("1.0", "end")
        self.remote_content.insert("1.0", "\n".join(rows))
        self.remote_content.configure(state="disabled")

    def _import_remote_web_exclusion(self) -> None:
        try:
            context = self._remote_write_context()
            browser_type, profile, cid = context
        except Exception as exc:
            messagebox.showerror("输入有误", str(exc))
            return
        path = filedialog.askopenfilename(
            title="导入网站排除列表",
            filetypes=[("网站名单", "*.txt *.csv *.json"), ("TXT", "*.txt"), ("CSV", "*.csv"), ("JSON", "*.json")],
        )
        if not path:
            return
        try:
            websites = import_website_file(path)
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc))
            return
        default_name = Path(path).stem[:80] or "web"
        name = simpledialog.askstring(
            "Google Ads 排除列表名称",
            f"已读取并去重 {len(websites)} 个网站。\n请输入要在 CID {format_cid(cid)} 创建的排除列表名称：",
            initialvalue=default_name,
            parent=self,
        )
        if not name or not name.strip():
            return
        name = name.strip()
        if not messagebox.askyesno(
            "确认导入网站排除",
            f"目标 CID：{format_cid(cid)}\n列表名称：{name}\n网站数量：{len(websites)}\n\n"
            "将创建真实 Google Ads 网站排除列表，并在写入后重新读取校验。确定继续？",
        ):
            return

        def task():
            with RemoteManager(
                profile, cid, lambda text: self.events.put(("log", text)),
                browser_type=browser_type,
            ) as manager:
                manager.create_object("web", name, websites)
                return manager.list_objects()

        def success(objects: list[dict[str, Any]]) -> None:
            self.remote_loaded_context = context
            self._show_remote_rows(objects)

        self._run_async(f"导入网站排除列表 {name}", task, success)

    def _export_remote_object(self) -> None:
        item = self._selected_remote()
        if not item:
            messagebox.showinfo("请选择", "请先选择一个排除列表或自定义受众。")
            return
        safe_name = re.sub(r'[<>:"/\\|?*]+', "_", str(item.get("name") or "google-object"))[:80]
        path = filedialog.asksaveasfilename(
            title="导出 Google Ads 对象",
            defaultextension=".json",
            initialfile=f"{safe_name}.json",
            filetypes=[("JSON", "*.json"), ("CSV", "*.csv"), ("TXT", "*.txt")],
        )
        if not path:
            return
        values = list(item.get("values") or [])
        try:
            target = Path(path)
            if target.suffix.casefold() == ".json":
                target.write_text(json.dumps({
                    "version": 1,
                    "group": item.get("group"), "kind": item.get("kind"),
                    "name": item.get("name"), "google_id": item.get("id"),
                    "values": values,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            elif target.suffix.casefold() == ".csv":
                with target.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=("kind", "name", "google_id", "value"))
                    writer.writeheader()
                    writer.writerows({
                        "kind": item.get("kind"), "name": item.get("name"),
                        "google_id": item.get("id"), "value": value,
                    } for value in values)
            else:
                target.write_text("\n".join(str(value) for value in values) + "\n", encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))
            return
        self._log(f"已导出 Google Ads 对象“{item.get('name')}”：{path}")
        messagebox.showinfo("导出完成", f"已导出 {len(values)} 项内容。\n{path}")

    def _create_remote(self) -> None:
        try:
            context = self._remote_write_context()
            browser_type, profile, cid = context
        except Exception as exc:
            messagebox.showerror("输入有误", str(exc))
            return
        dialog = ObjectDialog(self, "新建 Google Ads 对象")
        if not dialog.result:
            return
        kind, name, values = dialog.result
        if not messagebox.askyesno("确认创建", f"在 CID {format_cid(cid)} 创建 {kind}“{name}”，共 {len(values)} 项？"):
            return
        def task():
            with RemoteManager(
                profile, cid, lambda text: self.events.put(("log", text)),
                browser_type=browser_type,
            ) as manager:
                manager.create_object(kind, name, values)
                return manager.list_objects()
        def success(objects: list[dict[str, Any]]) -> None:
            self.remote_loaded_context = context
            self._show_remote_rows(objects)

        self._run_async("创建 Google Ads 对象", task, success)

    def _edit_remote(self) -> None:
        item = self._selected_remote()
        if not item:
            messagebox.showinfo("请选择", "请先选择一个 Google Ads 对象")
            return
        if item["kind"] not in TYPE_LABELS:
            messagebox.showerror("不能编辑", "混合或未知类型对象暂不支持直接替换")
            return
        try:
            context = self._remote_write_context()
            browser_type, profile, cid = context
        except Exception as exc:
            messagebox.showerror("输入有误", str(exc))
            return
        dialog = ObjectDialog(self, "编辑并替换 Google Ads 对象", item)
        if not dialog.result:
            return
        _kind, name, values = dialog.result
        if not messagebox.askyesno("确认替换", f"将“{item['name']}”替换为当前内容，并删除不再包含的旧项。确定继续？"):
            return
        def task():
            with RemoteManager(
                profile, cid, lambda text: self.events.put(("log", text)),
                browser_type=browser_type,
            ) as manager:
                manager.update_object(item, name, values)
                return manager.list_objects()
        def success(objects: list[dict[str, Any]]) -> None:
            self.remote_loaded_context = context
            self._show_remote_rows(objects)

        self._run_async("编辑 Google Ads 对象", task, success)

    def _delete_remote(self) -> None:
        item = self._selected_remote()
        if not item:
            return
        try:
            context = self._remote_write_context()
            browser_type, profile, cid = context
        except Exception as exc:
            messagebox.showerror("输入有误", str(exc))
            return
        if not messagebox.askyesno("危险操作确认", f"永久删除 CID {format_cid(cid)} 中的“{item['name']}”及其 {item['count']} 项内容？\n\n此操作无法在本工具中撤销。"):
            return
        def task():
            with RemoteManager(
                profile, cid, lambda text: self.events.put(("log", text)),
                browser_type=browser_type,
            ) as manager:
                manager.delete_object(item)
                return manager.list_objects()
        def success(objects: list[dict[str, Any]]) -> None:
            self.remote_loaded_context = context
            self._show_remote_rows(objects)

        self._run_async("删除 Google Ads 对象", task, success)

    # ----- suspended-account appeal assistant -------------------------------------
    def _refresh_appeal_automation_status(self) -> dict[str, Any]:
        settings = load_appeal_automation()
        mode = "自动" if settings.get("mode") == APPEAL_MODE_AUTO else "手动"
        action = "全自动提交" if settings.get("auto_submit", True) else "只生成草稿"
        last_at = str(settings.get("last_run_at") or "—").replace("T", " ")[:19]
        summary = str(settings.get("last_summary") or "尚未执行")[:180]
        self.appeal_auto_status.set(
            f"{mode} · {action} · {settings.get('time')} · 上次 {last_at} "
            f"{settings.get('last_status') or ''} · {summary}"
        )
        return settings

    def _initialize_appeal_automation(self) -> None:
        settings = self._refresh_appeal_automation_status()
        self.appeal_run_mode.set(str(settings.get("mode") or APPEAL_MODE_AUTO))
        self.appeal_run_time.set(str(settings.get("time") or "09:00"))
        self.appeal_auto_submit.set(bool(settings.get("auto_submit", True)))
        self.appeal_basis.set(APPEAL_BASIS_LABELS.get(
            str(settings.get("basis") or APPEAL_BASIS_CORRECTED),
            APPEAL_BASIS_LABELS[APPEAL_BASIS_CORRECTED],
        ))
        try:
            if settings.get("mode") == APPEAL_MODE_AUTO:
                scheduler.create_daily_appeal_task(str(settings.get("time") or "09:00"))
            else:
                scheduler.delete_task(scheduler.DAILY_APPEAL_TASK_NAME)
        except Exception as exc:
            self._log(f"每日申诉任务同步失败：{exc}")
            self.appeal_auto_status.set(
                self.appeal_auto_status.get() + f" · Windows 任务同步失败：{exc}"
            )
        if appeal_automation_due(settings):
            self._start_appeal_automation(force=False, quiet=True)

    def _save_appeal_automation_settings(self) -> None:
        try:
            mode = self.appeal_run_mode.get().strip()
            if mode not in {APPEAL_MODE_AUTO, APPEAL_MODE_MANUAL}:
                raise ValueError("请选择自动或手动模式")
            start_time = validate_appeal_time(self.appeal_run_time.get())
            auto_submit = bool(self.appeal_auto_submit.get())
            basis = self._selected_appeal_basis()
            if mode == APPEAL_MODE_AUTO and auto_submit and not messagebox.askyesno(
                "确认开启每日全自动申诉",
                f"每天 {start_time} 将按第一屏“批量任务”查询保存的全局账户池刷新状态，并向"
                "符合条件且未提交过的封禁 CID "
                f"正式发送申诉。\n\n申诉依据：{APPEAL_BASIS_LABELS[basis]}\n\n"
                "已有提交/处理中记录会永久保护，只有标记“申诉被拒”后才允许重新提交。确定开启？",
            ):
                return
            settings = load_appeal_automation()
            settings.update({
                "mode": mode,
                "time": start_time,
                "auto_submit": auto_submit,
                "basis": basis,
            })
            save_appeal_automation(settings)
            if mode == APPEAL_MODE_AUTO:
                scheduler.create_daily_appeal_task(start_time)
            else:
                scheduler.delete_task(scheduler.DAILY_APPEAL_TASK_NAME)
            self._refresh_appeal_automation_status()
        except Exception as exc:
            messagebox.showerror("申诉运行模式保存失败", str(exc))
            return
        action = "全自动提交" if auto_submit else "只生成草稿"
        label = (
            f"自动：每天执行一次（{action}）"
            if mode == APPEAL_MODE_AUTO else "手动：不执行每日任务"
        )
        self._log(f"账户申诉运行模式已保存：{label}，时间 {start_time}")
        messagebox.showinfo(
            "账户申诉运行模式",
            f"已保存为{label}。\n\n"
            + (
                "到时会真实提交；已提交/处理中状态不会被新草稿覆盖，也不会重复申诉。"
                if mode == APPEAL_MODE_AUTO and auto_submit
                else "不会自动向 Google 提交。"
            ),
        )

    def _run_appeal_automation_now(self) -> None:
        self._start_appeal_automation(force=True, quiet=False)

    def _start_appeal_automation(self, *, force: bool, quiet: bool) -> None:
        def task():
            return run_daily_appeal_workflow(
                force=force, log=lambda text: self.events.put(("log", text)),
            )

        def success(report: dict[str, Any]) -> None:
            profiles = report.get("profiles")
            if isinstance(profiles, dict) and profiles:
                self._apply_account_statuses(profiles)
            self._refresh_appeal_rows()
            self._refresh_appeal_automation_status()
            status = str(report.get("status") or "未知")
            summary = str(report.get("summary") or report.get("reason") or "")
            self._log(f"每日账户申诉：{status}；{summary}")
            if quiet or status == "未执行":
                return
            if status in {"失败", "部分完成"}:
                errors = "\n".join(str(value) for value in report.get("errors", [])[:8])
                messagebox.showwarning(
                    "每日账户申诉任务",
                    f"{status}：{summary}" + (f"\n\n{errors}" if errors else ""),
                )
            else:
                messagebox.showinfo(
                    "每日账户申诉任务",
                    summary,
                )

        self._run_async(
            "立即执行每日账户申诉" if force else "每日账户申诉",
            task,
            success,
        )

    def _refresh_appeal_rows(self) -> None:
        if not hasattr(self, "appeal_tree"):
            return
        selected_keys = {
            (
                window_key(
                    self.appeal_items[iid].get("browser_type"),
                    self.appeal_items[iid].get("profile_id"),
                ),
                str(self.appeal_items[iid]["cid"]),
            )
            for iid in self.appeal_tree.selection()
            if iid in self.appeal_items
        }
        records = load_appeal_history()
        reason_records = latest_suspension_reasons()
        latest = effective_appeal_history_by_key(records)
        self.appeal_tree.delete(*self.appeal_tree.get_children())
        self.appeal_items.clear()
        restore: list[str] = []
        for window_iid in self.task_tree.get_children(""):
            browser_type = normalize_browser_type(
                self.task_tree.set(window_iid, "browser_type")
            )
            profile = str(self.task_tree.set(window_iid, "profile") or "").strip()
            if not profile:
                continue
            profile_key = window_key(browser_type, profile)
            number = str(self.task_tree.set(window_iid, "window_number") or "").strip()
            for task_iid in self._window_rows(window_iid):
                cid = str(self.task_tree.set(task_iid, "cid") or "").strip()
                status = str(self.task_tree.set(task_iid, "status") or "").strip()
                account_type = str(
                    self.task_tree.set(task_iid, "account_type") or "CID"
                ).strip().upper()
                if not profile or not cid or status != "封禁" or account_type == "MCC":
                    continue
                record = latest.get((profile_key, cid), {})
                appeal_status = str(record.get("status") or "未申诉")
                updated_at = str(record.get("updated_at") or "").replace("T", " ")[:19]
                generic_status_detail = str(
                    self.task_tree.set(task_iid, "status_detail") or "封禁状态已识别"
                )
                reason_record = reason_records.get((profile_key, cid), {})
                reason_status = str(
                    reason_record.get("suspension_reason_status") or "未读取"
                )
                reason = str(reason_record.get("suspension_reason") or reason_status)
                reason_source = str(reason_record.get("suspension_reason_source") or "未读取")
                reason_read_at = str(reason_record.get("reason_read_at") or "").replace(
                    "T", " "
                )[:19]
                row = {
                    "browser_type": browser_type,
                    "profile_id": profile,
                    "window_number": number,
                    "account_type": account_type,
                    "account_name": str(self.task_tree.set(task_iid, "account_name") or ""),
                    "cid": cid,
                    "ocid": str(self.task_tree.set(task_iid, "ocid") or ""),
                    "status": status,
                    "status_detail": generic_status_detail,
                    "suspension_reason": reason,
                    "suspension_policy": str(reason_record.get("suspension_policy") or ""),
                    "suspension_reason_status": reason_status,
                    "suspension_reason_source": reason_source,
                    "suspension_page_excerpt": str(
                        reason_record.get("suspension_page_excerpt") or ""
                    ),
                    "reason_read_at": reason_read_at,
                    "reason_error": str(reason_record.get("error") or ""),
                    "appeal_status": appeal_status,
                    "appeal_updated_at": updated_at,
                    "history": record,
                }
                tag = (
                    "resolved" if appeal_status in {APPEAL_SUBMITTED, "申诉成功"}
                    else "pending" if appeal_status != "未申诉" else "banned"
                )
                iid = self.appeal_tree.insert(
                    "", "end",
                    values=(
                        f"#{number}" if number else "—", profile, row["account_name"],
                        format_cid(cid), status, reason, reason_source, reason_read_at,
                        appeal_status, updated_at,
                    ),
                    tags=(tag,),
                )
                self.appeal_items[iid] = row
                if (profile_key, cid) in selected_keys:
                    restore.append(iid)
        if restore:
            self.appeal_tree.selection_set(restore)
        self._appeal_selection_changed()

    def _select_all_appeals(self, selected: bool) -> None:
        rows = self.appeal_tree.get_children("")
        if selected:
            self.appeal_tree.selection_set(rows)
        elif self.appeal_tree.selection():
            self.appeal_tree.selection_remove(*self.appeal_tree.selection())
        self._appeal_selection_changed()

    def _selected_appeal_rows(self) -> list[dict[str, Any]]:
        return [
            self.appeal_items[iid]
            for iid in self.appeal_tree.selection()
            if iid in self.appeal_items
        ]

    def _appeal_template_text(self) -> str:
        value = self.appeal_template_box.get("1.0", "end-1c").strip()
        if not value:
            raise ValueError("申诉文案模板不能为空")
        return value

    def _selected_appeal_basis(self) -> str:
        basis_by_label = {label: key for key, label in APPEAL_BASIS_LABELS.items()}
        return basis_by_label.get(self.appeal_basis.get(), APPEAL_BASIS_CORRECTED)

    def _save_appeal_template(self) -> None:
        try:
            value = self._appeal_template_text()
            validate_appeal_template(value)
            path = save_appeal_template(value)
        except Exception as exc:
            messagebox.showerror("模板保存失败", str(exc))
            return
        self._log(f"已保存本地申诉文案模板：{path}")
        messagebox.showinfo("已保存", "申诉文案模板已保存到本地配置。")

    def _reset_appeal_template(self) -> None:
        if not messagebox.askyesno("恢复默认模板", "用内置默认申诉模板替换当前编辑内容？"):
            return
        self.appeal_template_box.delete("1.0", "end")
        self.appeal_template_box.insert("1.0", DEFAULT_APPEAL_TEMPLATE)

    def _set_appeal_detail(self, text: str) -> None:
        self.appeal_detail.configure(state="normal")
        self.appeal_detail.delete("1.0", "end")
        self.appeal_detail.insert("1.0", text)
        self.appeal_detail.configure(state="disabled")

    def _appeal_selection_changed(self) -> None:
        rows = self._selected_appeal_rows()
        total = len(self.appeal_items)
        self.appeal_summary.set(f"封禁 CID {total} 个 · 已选择 {len(rows)} 个")
        if not rows:
            self._set_appeal_detail(
                "请选择封禁 CID。\n\n操作流程：刷新账户状态 → 选择 CID → "
                "读取选中封禁原因（只读）→ 选择申诉依据 → 一键提交"
                "（模板未补全时自动按官方政策生成完整申诉资料）。\n\n"
                "红色封禁状态来自 CustomerService；具体政策名称只接受 Google Ads 官方账号中止页的明确文本。"
            )
            return
        row = rows[0]
        history = row.get("history") or {}
        if row.get("suspension_reason_status") != REASON_READ:
            rendered = "尚未从官方账号中止页读到明确原因，暂不生成申诉文案。"
        else:
            try:
                rendered, auto_used = build_appeal_text(
                    self._appeal_template_text(), row, basis=self._selected_appeal_basis(),
                )
                if auto_used:
                    rendered = "（模板未补全，以下为按官方政策自动生成的完整申诉资料）\n\n" + rendered
            except Exception as exc:
                rendered = f"模板无法生成：{exc}"
        self._set_appeal_detail(
            f"指纹浏览器：{row.get('browser_type')} · #{row.get('window_number') or '—'} · {row.get('profile_id')}\n"
            f"账号：{row.get('account_name') or '未命名'}\n"
            f"CID：{format_cid(str(row.get('cid') or ''))}\n"
            f"内部账户 ID：{row.get('ocid') or '缺失，请刷新状态'}\n"
            f"状态识别说明（仅软件诊断，不进入申诉）：{row.get('status_detail') or '—'}\n"
            f"具体封禁原因：{row.get('suspension_reason') or '未读取'}\n"
            f"原因读取状态：{row.get('suspension_reason_status') or '未读取'}\n"
            f"原因来源：{row.get('suspension_reason_source') or '未读取'}\n"
            f"读取时间：{row.get('reason_read_at') or '—'}\n"
            f"读取错误：{row.get('reason_error') or '—'}\n"
            f"官方页原文片段：{row.get('suspension_page_excerpt') or '—'}\n"
            f"本地申诉状态：{row.get('appeal_status') or '未申诉'}\n"
            f"最近记录：{history.get('updated_at') or '—'}\n"
            f"最近错误：{history.get('error') or '—'}\n\n"
            f"当前生成文案：\n{rendered}"
            + (f"\n\n另有 {len(rows) - 1} 个已选 CID。" if len(rows) > 1 else "")
        )

    def _copy_selected_appeal_text(self) -> None:
        rows = self._selected_appeal_rows()
        if len(rows) != 1:
            messagebox.showinfo("请选择一个 CID", "复制文案时请只选择一个封禁 CID。")
            return
        if (
            rows[0].get("suspension_reason_status") != REASON_READ
            or rows[0].get("suspension_reason_source") != OFFICIAL_REASON_SOURCE
            or not rows[0].get("suspension_policy")
            or not rows[0].get("suspension_reason")
        ):
            messagebox.showerror(
                "尚未读到具体封禁原因",
                "请先点击“读取选中封禁原因（只读）”；软件不会用通用状态说明生成申诉文案。",
            )
            return
        try:
            text, auto_used = build_appeal_text(
                self._appeal_template_text(), rows[0], basis=self._selected_appeal_basis(),
            )
        except Exception as exc:
            messagebox.showerror("文案生成失败", str(exc))
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        source = "自动生成的申诉资料" if auto_used else "本地模板文案"
        self._log(f"[{format_cid(rows[0]['cid'])}] 已复制申诉文案（{source}）")

    def _read_selected_suspension_reasons(self) -> None:
        rows = self._selected_appeal_rows()
        if not rows:
            messagebox.showinfo("请选择 CID", "请至少选择一个封禁 CID。")
            return
        if len(rows) > MAX_APPEALS_PER_BATCH:
            messagebox.showerror(
                "选择过多", f"单次最多读取 {MAX_APPEALS_PER_BATCH} 个账号，请分批操作。"
            )
            return
        missing_ocid = [format_cid(row["cid"]) for row in rows if not row.get("ocid")]
        if missing_ocid:
            messagebox.showerror(
                "缺少内部账户 ID",
                "以下 CID 尚未读取内部账户 ID，请先在“批量任务”刷新账户列表与状态：\n"
                + "、".join(missing_ocid),
            )
            return

        def task():
            return read_suspension_reasons(
                rows, log=lambda text: self.events.put(("log", text)),
            )

        def success(results: list[dict[str, Any]]) -> None:
            read_rows = [
                row for row in results
                if row.get("suspension_reason_status") == REASON_READ
            ]
            failed = [row for row in results if row not in read_rows]
            self._refresh_appeal_rows()
            detail = "\n".join(
                f"{row.get('cid_display')}："
                f"{row.get('suspension_reason') or row.get('suspension_reason_status')}"
                + (f"（{row.get('error')}）" if row.get("error") else "")
                for row in results
            )
            if failed:
                messagebox.showwarning(
                    "部分原因未读到",
                    f"明确读到 {len(read_rows)} 个，未读到 {len(failed)} 个。\n\n{detail}\n\n"
                    "未读到的账号不会自动猜测原因，也不能直接生成申诉文案。",
                )
            else:
                messagebox.showinfo(
                    "封禁原因读取完成",
                    f"已从 Google Ads 官方账号中止页读到 {len(read_rows)} 个明确原因。\n\n{detail}",
                )

        self._run_async(f"读取 {len(rows)} 个账号的官方封禁原因", task, success)

    def _submit_selected_appeals(self) -> None:
        rows = self._selected_appeal_rows()
        if not rows:
            messagebox.showinfo("请选择 CID", "请至少选择一个封禁 CID。")
            return
        if len(rows) > MAX_APPEALS_PER_BATCH:
            messagebox.showerror(
                "选择过多", f"单次最多处理 {MAX_APPEALS_PER_BATCH} 个账号，请分批操作。"
            )
            return
        missing_ocid = [format_cid(row["cid"]) for row in rows if not row.get("ocid")]
        if missing_ocid:
            messagebox.showerror(
                "缺少内部账户 ID",
                "以下 CID 尚未读取内部账户 ID，请先在“批量任务”刷新账户列表与状态：\n"
                + "、".join(missing_ocid),
            )
            return
        missing_reason = [
            format_cid(row["cid"])
            for row in rows
            if row.get("suspension_reason_status") != REASON_READ
            or row.get("suspension_reason_source") != OFFICIAL_REASON_SOURCE
            or not row.get("suspension_policy")
            or not row.get("suspension_reason")
        ]
        if missing_reason:
            messagebox.showerror(
                "尚未读到具体封禁原因",
                "以下 CID 尚未从 Google Ads 官方账号中止页读到明确政策名称：\n"
                + "、".join(missing_reason)
                + "\n\n请先点击“读取选中封禁原因（只读）”。读取失败时不会自动猜测原因。",
            )
            return
        try:
            template = self._appeal_template_text()
            for row in rows:
                build_appeal_text(template, row, basis=self._selected_appeal_basis())
        except Exception as exc:
            messagebox.showerror("申诉模板无效", str(exc))
            return
        repeated = [
            format_cid(row["cid"])
            for row in rows
            if row.get("appeal_status") not in {
                "", "未申诉", "打开失败", APPEAL_SUBMIT_FAILED, "申诉被拒",
                APPEAL_LOGIN_REQUIRED, APPEAL_VERIFICATION_REQUIRED,
                APPEAL_USER_ACTION_REQUIRED,
            }
        ]
        warning = (
            f"\n\n其中已有本地申诉记录：{'、'.join(repeated)}。请确认不是重复提交。"
            if repeated else ""
        )
        if not messagebox.askyesno(
            "确认一键提交账号申诉",
            f"即将向 Google 正式提交 {len(rows)} 个封禁 CID 的申诉。\n\n"
            f"申诉依据：{self.appeal_basis.get()}\n\n"
            "我确认：文案中的业务、网站、问题原因、整改、防复发措施和证明材料均真实完整；"
            "已完成文案中声明的整改；本操作会产生真实外部提交。\n\n"
            "Google 要求登录、身份验证或安全验证时，软件会停在该页面，不会绕过验证。"
            + warning,
        ):
            return
        # Save the exact template used for this preparation.
        try:
            save_appeal_template(template)
        except Exception as exc:
            messagebox.showerror("无法保存模板", str(exc))
            return

        basis_by_label = {label: key for key, label in APPEAL_BASIS_LABELS.items()}
        basis = basis_by_label.get(self.appeal_basis.get(), "")

        def task():
            return submit_appeals(
                rows, template, basis, truth_confirmed=True,
                allow_resubmit=True,
                log=lambda text: self.events.put(("log", text)),
            )

        def success(results: list[dict[str, Any]]) -> None:
            submitted = [row for row in results if row.get("status") == APPEAL_SUBMITTED]
            pending = [row for row in results if row.get("status") == APPEAL_OFFICIAL_PENDING]
            blocked = [
                row for row in results
                if row.get("status") in {
                    APPEAL_LOGIN_REQUIRED, APPEAL_VERIFICATION_REQUIRED,
                    APPEAL_USER_ACTION_REQUIRED,
                }
            ]
            failed = [row for row in results if row.get("status") == APPEAL_SUBMIT_FAILED]
            skipped = [row for row in results if row.get("skipped")]
            self._refresh_appeal_rows()
            detail = "\n".join(
                f"{row.get('cid_display')}：{row.get('status')}"
                + (f"（{row.get('error')}）" if row.get("error") else "")
                for row in results
            )
            summary = (
                f"已提交 {len(submitted)}，官方处理中 {len(pending)}，"
                f"等待验证 {len(blocked)}，失败 {len(failed)}，跳过 {len(skipped)}。"
            )
            if failed or blocked:
                messagebox.showwarning(
                    "一键申诉未全部完成",
                    f"{summary}\n\n{detail}\n\n等待验证的官方页面已保留；完成 Google 要求后，再次点击一键提交即可续提。",
                )
            else:
                messagebox.showinfo(
                    "一键申诉完成",
                    f"{summary}\n\n{detail}\n\n只有取得 Google 官方提交/处理中状态的账号才按完成记录。",
                )

        self._run_async(f"一键提交 {len(rows)} 个账号申诉", task, success)

    def _one_click_appeals(self) -> None:
        selected = self._selected_appeal_rows()
        rows = selected if selected else list(self.appeal_items.values())
        if not rows:
            messagebox.showinfo(
                "没有封禁 CID",
                "当前没有可申诉的封禁 CID；请先在“批量任务”刷新账户列表与状态。",
            )
            return
        protected_rows = [
            row for row in rows
            if appeal_submission_is_protected(row.get("history"), allow_resubmit=True)
        ]
        action_rows = [
            row for row in rows
            if not appeal_submission_is_protected(row.get("history"), allow_resubmit=True)
        ]
        if not action_rows:
            detail = "、".join(
                f"{format_cid(row['cid'])}（{row.get('appeal_status') or '已提交'}）"
                for row in protected_rows
            )
            messagebox.showinfo(
                "无需重复申诉",
                "所选 CID 均已有提交回执或处于官方处理中，软件不会重新读取原因或重复提交。\n\n"
                + detail,
            )
            return
        if len(action_rows) > MAX_APPEALS_PER_BATCH:
            messagebox.showerror(
                "数量过多",
                f"一键申诉单次最多处理 {MAX_APPEALS_PER_BATCH} 个待申诉账号"
                f"（当前 {len(action_rows)} 个）；"
                "请勾选要处理的 CID 后重试。",
            )
            return
        missing_ocid = [
            format_cid(row["cid"]) for row in action_rows if not row.get("ocid")
        ]
        if missing_ocid:
            messagebox.showerror(
                "缺少内部账户 ID",
                "以下 CID 尚未读取内部账户 ID，请先在“批量任务”刷新账户列表与状态：\n"
                + "、".join(missing_ocid),
            )
            return
        try:
            template = self._appeal_template_text()
        except Exception as exc:
            messagebox.showerror("申诉模板无效", str(exc))
            return
        scope = "选中的" if selected else "全部"
        if not messagebox.askyesno(
            "确认一键申诉",
            f"将对{scope} {len(action_rows)} 个待申诉 CID 连续执行："
            "读取官方封禁原因 → 校验资料 → 提交申诉。\n"
            f"已有提交记录并自动保护：{len(protected_rows)} 个。\n\n"
            f"申诉依据：{self.appeal_basis.get()}\n\n"
            "未读到明确官方政策原因的账号会自动跳过，不会猜测原因。\n"
            "我确认：文案中的业务、网站、问题原因、整改、防复发措施和证明材料均真实完整；"
            "已完成文案中声明的整改；本操作会产生真实外部提交。\n\n"
            "Google 要求登录、身份验证或安全验证时，软件会停在该页面，不会绕过验证。",
        ):
            return
        try:
            save_appeal_template(template)
        except Exception as exc:
            messagebox.showerror("无法保存模板", str(exc))
            return
        basis_by_label = {label: key for key, label in APPEAL_BASIS_LABELS.items()}
        basis = basis_by_label.get(self.appeal_basis.get(), "")

        def task():
            return run_one_click_appeals(
                rows, template, basis, truth_confirmed=True,
                allow_resubmit=True,
                log=lambda text: self.events.put(("log", text)),
            )

        def success(payload: dict[str, Any]) -> None:
            results = payload.get("results") or []
            protected = payload.get("protected") or []
            no_reason = payload.get("no_reason") or []
            errors = payload.get("errors") or []
            submitted = [row for row in results if row.get("status") == APPEAL_SUBMITTED]
            pending = [row for row in results if row.get("status") == APPEAL_OFFICIAL_PENDING]
            blocked = [
                row for row in results
                if row.get("status") in {
                    APPEAL_LOGIN_REQUIRED, APPEAL_VERIFICATION_REQUIRED,
                    APPEAL_USER_ACTION_REQUIRED,
                }
            ]
            failed = [row for row in results if row.get("status") == APPEAL_SUBMIT_FAILED]
            self._refresh_appeal_rows()
            cid_results = payload.get("cid_results") or []
            detail = "\n".join(
                f"{row.get('cid_display') or format_cid(str(row.get('cid') or ''))}："
                f"{row.get('status') or row.get('outcome') or '未知'}"
                + (f"（{row.get('message')}）" if row.get("message") else "")
                + (
                    f" [重试分类：{row.get('retry_class')}]"
                    if row.get("retry_class") else ""
                )
                for row in cid_results
            )
            summary = str(payload.get("summary") or (
                f"已保护 {len(protected)}，新提交 {len(submitted)}，官方处理中 {len(pending)}，"
                f"等待验证 {len(blocked)}，失败 {len(failed)}，未读到原因 {len(no_reason)}。"
            ))
            self._log(f"一键申诉结果：{summary}")
            for row in cid_results:
                self._log(
                    f"[{row.get('cid_display') or row.get('cid')}] "
                    f"{row.get('status') or row.get('outcome')}"
                    + (f" - {row.get('message')}" if row.get("message") else "")
                )
            if failed or blocked or no_reason or errors:
                messagebox.showwarning(
                    "一键申诉未全部完成",
                    f"{summary}\n\n{detail}\n\n"
                    "等待验证的官方页面已保留；完成 Google 要求后再次点击即可续提。",
                )
            else:
                messagebox.showinfo(
                    "一键申诉完成",
                    f"{summary}\n\n{detail}\n\n只有取得 Google 官方提交/处理中状态的账号才按完成记录。",
                )

        self._run_async(f"一键申诉 {len(rows)} 个账号（读原因→提交）", task, success)

    def _mark_appeal_status(self, status: str) -> None:
        rows = self._selected_appeal_rows()
        if not rows:
            messagebox.showinfo("请选择 CID", "请先选择要更新本地申诉状态的 CID。")
            return
        if not messagebox.askyesno(
            "更新本地申诉状态",
            f"把选中的 {len(rows)} 个 CID 标记为“{status}”？\n"
            "此操作只更新本地记录，不会向 Google 发送请求。",
        ):
            return
        try:
            for row in rows:
                update_appeal_status(
                    str(row["cid"]), status, profile_id=str(row["profile_id"]),
                    browser_type=normalize_browser_type(row["browser_type"]),
                )
        except Exception as exc:
            messagebox.showerror("状态更新失败", str(exc))
            return
        self._refresh_appeal_rows()
        self._log(f"已将 {len(rows)} 个本地申诉记录标记为：{status}")

    def _show_appeal_context_menu(self, event: tk.Event) -> None:
        iid = self.appeal_tree.identify_row(event.y)
        if iid and iid not in self.appeal_tree.selection():
            self.appeal_tree.selection_set(iid)
        self.appeal_context_menu.delete(0, "end")
        self.appeal_context_menu.add_command(
            label="复制该 CID 申诉文案", command=self._copy_selected_appeal_text
        )
        self.appeal_context_menu.add_command(
            label="读取官方封禁原因（只读）", command=self._read_selected_suspension_reasons
        )
        self.appeal_context_menu.add_command(
            label="一键提交申诉", command=self._submit_selected_appeals
        )
        self.appeal_context_menu.add_separator()
        for status in ("已人工提交", "申诉成功", "申诉被拒"):
            self.appeal_context_menu.add_command(
                label=f"标记为 {status}",
                command=lambda value=status: self._mark_appeal_status(value),
            )
        self.appeal_context_menu.tk_popup(event.x_root, event.y_root)

    # ----- balance-gated App-1 white-traffic workflow ------------------------------
    def _refresh_warmup_template_summary(self) -> None:
        try:
            template = load_warmup_template()
        except ValueError as exc:
            self.warmup_template_summary.set("固定模板：本地文件无效")
            self._log(f"固定白量模板读取失败：{exc}")
            return
        if template is None:
            self.warmup_template_summary.set("固定模板：尚未保存")
            return
        saved_at = str(template.get("saved_at") or "").replace("T", " ")[:19]
        self.warmup_template_summary.set(
            f"固定模板：{template['name']} · 参考 {format_cid(template['source_cid'])} / "
            f"{template['campaign_name']} · USD {template['target_daily_budget']}"
            + (f" · {saved_at}" if saved_at else "")
        )

    def _save_current_warmup_template(self) -> None:
        try:
            source_cid = self._warmup_source_value()
            target_budget = self._warmup_budget_value()
            path = save_warmup_template(
                source_cid,
                target_budget,
                campaign_name=SOURCE_CAMPAIGN_NAME,
            )
        except Exception as exc:
            messagebox.showerror("固定模板保存失败", str(exc))
            return
        self._save_state()
        self._refresh_warmup_template_summary()
        self._log(f"已保存本地固定白量模板：{path}")
        messagebox.showinfo(
            "固定模板已保存",
            "已保存当前参考 CID、App-1 和目标日预算。"
            "\n此操作只修改本地配置，不读取或写入 Google Ads。",
        )

    def _select_saved_warmup_template(self) -> None:
        try:
            template = load_warmup_template()
        except ValueError as exc:
            messagebox.showerror("固定模板不可用", str(exc))
            return
        if template is None:
            messagebox.showinfo("尚无固定模板", "请先填写参考 CID 和目标日预算，再保存固定模板。")
            return
        self.warmup_source_cid.set(format_cid(template["source_cid"]))
        self.warmup_daily_budget.set(str(template["target_daily_budget"]))
        self._save_state()
        self._refresh_warmup_template_summary()
        self._log("已选用本地固定白量模板；未连接或写入 Google Ads")

    def _warmup_template_task_targets(self) -> list[dict[str, str]]:
        targets: list[dict[str, str]] = []
        for iid in self.task_tree.selection():
            if not self.task_tree.parent(iid):
                continue
            account_type = str(
                self.task_tree.set(iid, "account_type") or "CID"
            ).strip().upper()
            if account_type == "MCC":
                continue
            cid = str(self.task_tree.set(iid, "cid") or "").strip()
            if not cid:
                continue
            window_iid = self._window_for_item(iid)
            targets.append({
                "browser_type": normalize_browser_type(
                    self.task_tree.set(window_iid, "browser_type")
                ),
                "profile_id": str(self.task_tree.set(window_iid, "profile") or ""),
                "window_number": str(
                    self.task_tree.set(window_iid, "window_number") or ""
                ),
                "cid": cid,
                "account_type": account_type,
                "account_name": str(self.task_tree.set(iid, "account_name") or ""),
                "account_status": str(self.task_tree.set(iid, "status") or "状态未知"),
            })
        return targets

    def _warmup_template_preview_text(
        self, plan: dict[str, Any] | None = None,
    ) -> str:
        # Keep the historical class-level test/helper call working while the GUI
        # path resolves every target through the composite window identity.
        app = self if plan is not None else None
        if plan is None:
            plan = self  # type: ignore[assignment]
        template = plan["template"]
        lines = [
            "模式：只读 / 演练（未连接账户，未执行复制、预算或启用）",
            f"模板：{template['name']} · 参考 {format_cid(template['source_cid'])} / {template['campaign_name']}",
            f"目标日预算：USD {plan['target_daily_budget']}",
            f"目标数：{plan['target_count']} · 可进入后续只读扫描：{plan['eligible_for_scan_count']}",
            f"所选目标理论总日预算：USD {plan['selected_theoretical_daily_budget']}",
            f"统一账户缓存：{plan['cache']['status']}",
            "",
        ]
        for index, row in enumerate(plan["targets"], 1):
            profile = (
                app._report_profile_display(
                    str(row["profile_id"]), browser_type=row.get("browser_type"),
                )
                if app is not None
                else window_key(row.get("browser_type"), str(row["profile_id"]))
            )
            lines.extend((
                f"{index}. {profile} · {row['cid_display']} · "
                f"{row['account_name'] or '未命名'} · {row['account_status']}",
                f"   数据：{row['data_source']}",
                f"   计划：{row['planned_action']}",
                f"   结论：{row['reason']}",
            ))
        return "\n".join(lines)

    def _warmup_template_preview_from_task(self) -> None:
        targets = self._warmup_template_task_targets()
        if not targets:
            messagebox.showinfo("没有目标 CID", "请在批量任务中右键一个普通 CID。")
            return
        try:
            template = load_warmup_template()
            if template is None:
                raise ValueError("尚未保存固定白量模板；请先到“一键白量”页保存。")
            plan = build_warmup_template_dry_run(
                template,
                targets,
                load_account_cache(),
            )
        except Exception as exc:
            messagebox.showerror("固定模板演练失败", str(exc))
            return
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        blocked = plan["target_count"] - plan["eligible_for_scan_count"]
        report = dict(plan)
        report.update({
            "started_at": timestamp,
            "finished_at": timestamp,
            "success": blocked == 0 and plan["target_count"] > 0,
            "error": f"{blocked} 个目标被本地安全检查阻断" if blocked else "",
            "profile_id": ", ".join(
                sorted({window_key(row.get("browser_type"), row["profile_id"])
                        for row in plan["targets"]})
            ),
            "window_keys": sorted({
                window_key(row.get("browser_type"), row["profile_id"])
                for row in plan["targets"]
            }),
            "accounts": [
                {
                    "browser_type": row["browser_type"],
                    "profile_id": row["profile_id"],
                    "window_key": window_key(row["browser_type"], row["profile_id"]),
                    "cid": row["cid"],
                    "account_name": row["account_name"],
                    "account_status": row["account_status"],
                    "actions": [{
                        "kind": "ad",
                        "status": "would_create" if row["eligible_for_scan"] else "skipped",
                        "object_name": template["campaign_name"],
                        "reason": f"{row['planned_action']}；{row['reason']}",
                    }],
                }
                for row in plan["targets"]
            ],
        })
        path = save_run_report(report)
        self._refresh_reports()
        self._log(
            f"固定模板一键白量演练完成：目标 {plan['target_count']} 个，"
            f"可进入只读扫描 {plan['eligible_for_scan_count']} 个；未连接或写入账户"
        )
        messagebox.showinfo(
            "一键白量 · 固定模板演练",
            self._warmup_template_preview_text(plan) + f"\n\n本地演练报告：{path.name}",
        )

    def _sync_warmup_target_tree(self) -> None:
        if not hasattr(self, "warmup_target_tree"):
            return
        selected_keys = {
            (
                str(self.warmup_target_items[iid]["browser_type"]),
                str(self.warmup_target_items[iid]["profile_id"]),
                str(self.warmup_target_items[iid]["cid"]),
            )
            for iid in self.warmup_target_tree.selection()
            if iid in self.warmup_target_items
        }
        self.warmup_target_tree.delete(*self.warmup_target_tree.get_children())
        self.warmup_target_items.clear()
        restore_selection: list[str] = []
        for window_iid in self.task_tree.get_children(""):
            browser_type = normalize_browser_type(
                self.task_tree.set(window_iid, "browser_type")
            )
            profile = str(self.task_tree.set(window_iid, "profile") or "").strip()
            for task_iid in self._window_rows(window_iid):
                cid = str(self.task_tree.set(task_iid, "cid") or "").strip()
                account_type = str(
                    self.task_tree.set(task_iid, "account_type") or "CID"
                ).strip().upper()
                if not profile or not cid or account_type == "MCC":
                    continue
                status = str(self.task_tree.set(task_iid, "status") or "状态未知")
                balance = str(self.task_tree.set(task_iid, "balance") or "未读取")
                iid = self.warmup_target_tree.insert(
                    "",
                    "end",
                    values=(
                        self._report_profile_display(profile),
                        format_cid(cid),
                        status,
                        balance,
                    ),
                    tags=(self._task_row_tag("CID", status),),
                )
                self.warmup_target_items[iid] = {
                    "browser_type": browser_type,
                    "profile_id": profile,
                    "cid": cid,
                }
                if (browser_type, profile, cid) in selected_keys:
                    restore_selection.append(iid)
        if restore_selection:
            self.warmup_target_tree.selection_set(restore_selection)
        self._warmup_target_selection_changed()

    def _warmup_select_targets(self, selected: bool) -> None:
        if selected:
            self.warmup_target_tree.selection_set(self.warmup_target_tree.get_children(""))
        else:
            self.warmup_target_tree.selection_remove(self.warmup_target_tree.selection())
        self._warmup_target_selection_changed()

    def _warmup_target_selection_changed(self) -> None:
        selected = len(self.warmup_target_tree.selection())
        total = len(self.warmup_target_tree.get_children(""))
        self.warmup_target_summary.set(f"已选择 {selected} / {total} 个 CID")

    @staticmethod
    def _group_warmup_targets(items: list[dict[str, str]]) -> dict[str, set[str]]:
        targets: dict[str, set[str]] = {}
        for item in items:
            browser_type = normalize_browser_type(item.get("browser_type"))
            profile = str(item.get("profile_id") or "").strip()
            cid = str(item.get("cid") or "").strip()
            if profile and cid:
                targets.setdefault(window_key(browser_type, profile), set()).add(cid)
        return targets

    def _warmup_targets(self) -> dict[str, set[str]]:
        targets = self._group_warmup_targets([
            self.warmup_target_items[iid]
            for iid in self.warmup_target_tree.selection()
            if iid in self.warmup_target_items
        ])
        if not targets:
            raise ValueError("请先在左侧 CID 列表中选择一个或多个目标 CID")
        return targets

    def _warmup_budget_value(self) -> Decimal:
        budget = validate_daily_budget(self.warmup_daily_budget.get())
        self.warmup_daily_budget.set(f"{budget:.2f}")
        return budget

    def _warmup_source_value(self) -> str:
        raw = self.warmup_source_cid.get().strip()
        if not raw:
            raise ValueError("请填写当前窗口可访问、状态可用且包含 App-1 的参考 CID")
        cid = normalize_cid(raw)
        self.warmup_source_cid.set(format_cid(cid))
        return cid

    def _warmup_scan(self) -> None:
        try:
            targets = self._warmup_targets()
            target_budget = self._warmup_budget_value()
            source_cid = self._warmup_source_value()
        except Exception as exc:
            messagebox.showerror("无法扫描", str(exc))
            return
        self._save_state()

        def task():
            rows = []
            errors = []
            for profile_key, cids in targets.items():
                browser_type, profile = split_window_key(profile_key)
                self.events.put(("log", f"[{profile}] 扫描 {len(cids)} 个 CID 的余额与 App 系列"))
                try:
                    with WarmupManager(
                        profile,
                        lambda text: self.events.put(("log", text)),
                        source_cid=source_cid,
                        target_daily_budget=target_budget,
                        browser_type=browser_type,
                    ) as manager:
                        result = manager.scan(cids)
                    for row in result["rows"]:
                        row["browser_type"] = browser_type
                        row["profile_id"] = profile
                    rows.extend(result["rows"])
                except Exception as exc:
                    errors.append({
                        "browser_type": browser_type,
                        "profile_id": profile,
                        "error": str(exc),
                    })
            report = {
                "version": 1,
                "mode": "warmup_preflight",
                "profile_id": ", ".join(targets),
                "started_at": datetime.now().astimezone().isoformat(),
                "finished_at": datetime.now().astimezone().isoformat(),
                "success": bool(rows) and not errors,
                "error": "；".join(f"{item['profile_id']}：{item['error']}" for item in errors),
                "accounts": [{
                    "cid": row["cid"],
                    "account_name": row["account_name"],
                    "account_status": row["account_status"],
                    "actions": [{
                        "kind": "account",
                        "status": "would_create" if row["eligible"] else "skipped",
                        "reason": (
                            f"{row['action']}；{row['reason']}；余额={row['balance']}；"
                            f"今日消耗={row['daily_spend']}；目标日预算="
                            f"{row['currency'] or 'USD'} {row['target_daily_budget']}"
                        ),
                    }],
                } for row in rows],
            }
            save_run_report(report)
            return {
                "rows": rows,
                "errors": errors,
                "target_daily_budget": str(target_budget),
                "source_cid": source_cid,
            }

        self._run_async("扫描一键白量条件（只读）", task, self._show_warmup_rows)

    def _show_warmup_rows(self, payload: dict[str, Any]) -> None:
        self.warmup_tree.delete(*self.warmup_tree.get_children())
        self.warmup_items.clear()
        for row in payload.get("rows") or []:
            iid = self.warmup_tree.insert(
                "", "end",
                values=(
                    "是" if row.get("selected") else "否",
                    self._report_profile_display(
                        str(row.get("profile_id") or ""),
                        browser_type=str(row.get("browser_type") or ADSPOWER),
                    ),
                    row.get("cid_display"), row.get("account_name"), row.get("account_status"),
                    row.get("balance"), row.get("daily_spend"), row.get("campaign_name") or "—",
                    row.get("campaign_status"),
                    (
                        f"{row.get('currency') or 'USD'} "
                        f"{row.get('current_daily_budget') or '无'} → "
                        f"{row.get('target_daily_budget') or '—'}"
                    ),
                    row.get("action"), row.get("reason"),
                ),
                tags=("eligible" if row.get("eligible") else "blocked",),
            )
            self.warmup_items[iid] = row
        errors = payload.get("errors") or []
        self._refresh_reports()
        eligible = sum(bool(row.get("eligible")) for row in self.warmup_items.values())
        self._log(f"一键白量扫描：符合条件 {eligible} 个，跳过 {len(self.warmup_items) - eligible} 个")
        if errors:
            messagebox.showwarning(
                "部分窗口无法扫描",
                "\n".join(f"{item['profile_id']}：{item['error']}" for item in errors[:8]),
            )
        elif not self.warmup_items:
            messagebox.showwarning("没有结果", "没有读取到普通 CID")
        else:
            messagebox.showinfo(
                "扫描完成",
                f"符合条件：{eligible} 个 CID；目标日预算 USD "
                f"{payload.get('target_daily_budget') or self.warmup_daily_budget.get()}",
            )

    def _warmup_select_all(self, selected: bool) -> None:
        for iid, row in self.warmup_items.items():
            row["selected"] = bool(selected and row.get("eligible"))
            self.warmup_tree.set(iid, "selected", "是" if row["selected"] else "否")

    def _toggle_warmup_row(self, event: tk.Event | None = None) -> str | None:
        if event is not None:
            iid = self._select_row_at_pointer(self.warmup_tree, event)
        else:
            selected = self.warmup_tree.selection()
            iid = selected[0] if selected else ""
        row = self.warmup_items.get(iid)
        if not row:
            return None
        if not row.get("eligible"):
            messagebox.showinfo("不能选择", str(row.get("reason") or "该 CID 不符合启用条件"))
            return "break"
        row["selected"] = not bool(row.get("selected"))
        self.warmup_tree.set(iid, "selected", "是" if row["selected"] else "否")
        return "break"

    def _show_warmup_context_menu(self, event: tk.Event) -> None:
        iid = self._select_row_at_pointer(self.warmup_tree, event)
        menu = self.warmup_context_menu
        menu.delete(0, "end")
        row = self.warmup_items.get(iid)
        if row:
            menu.add_command(
                label="取消执行" if row.get("selected") else "选择执行",
                command=self._toggle_warmup_row,
                state="normal" if row.get("eligible") else "disabled",
            )
            menu.add_command(
                label="复制 CID",
                command=lambda: self._copy_text(str(row.get("cid_display") or ""), "CID"),
            )
        menu.add_separator()
        menu.add_command(label="重新扫描", command=self._warmup_scan)
        self._popup_menu(menu, event)

    def _show_warmup_detail(self) -> None:
        selected = self.warmup_tree.selection()
        row = self.warmup_items.get(selected[0]) if selected else None
        text = "请选择一个 CID。" if not row else "\n".join((
            "指纹浏览器窗口："
            + self._report_profile_display(
                row["profile_id"], browser_type=row.get("browser_type") or ADSPOWER,
            ),
            f"CID：{row['cid_display']}",
            f"账户：{row['account_name']} · {row['account_status']}",
            f"余额：{row['balance']} · {row['balance_type']}",
            f"余额说明：{row['balance_detail']}",
            f"今日消耗：{row['daily_spend']} · {row['daily_spend_status']} · {row['daily_spend_date']}",
            f"消耗说明：{row['daily_spend_detail']}",
            f"现有 App 系列：{row['campaign_name'] or '不存在'} · {row['campaign_status']}",
            f"日预算：USD {row.get('current_daily_budget') or '无'} → USD {row['target_daily_budget']}",
            f"计划：{row['action']}",
            f"是否执行：{'是' if row['selected'] else '否'}",
            f"条件说明：{row['reason']}",
        ))
        self.warmup_detail.configure(state="normal")
        self.warmup_detail.delete("1.0", "end")
        self.warmup_detail.insert("1.0", text)
        self.warmup_detail.configure(state="disabled")

    def _warmup_execute(self) -> None:
        selected_rows = [row for row in self.warmup_items.values() if row.get("selected")]
        if not selected_rows:
            messagebox.showinfo("没有执行对象", "请先扫描并选择符合条件的 CID")
            return
        try:
            target_budget = self._warmup_budget_value()
            source_cid = self._warmup_source_value()
        except ValueError as exc:
            messagebox.showerror("目标日预算无效", str(exc))
            return
        scanned_budgets = {
            str(row.get("target_daily_budget") or "") for row in selected_rows
        }
        if scanned_budgets != {str(target_budget)}:
            messagebox.showwarning(
                "请重新扫描",
                "目标日预算已改变。请按当前预算重新扫描，确认“当前 → 目标”后再执行。",
            )
            return
        grouped: dict[str, set[str]] = {}
        for row in selected_rows:
            key = window_key(row.get("browser_type"), row.get("profile_id"))
            grouped.setdefault(key, set()).add(str(row["cid"]))
        aggregate = target_budget * len(selected_rows)
        prompt = (
            f"将处理 {len(selected_rows)} 个 CID。\n\n"
            f"参考：{format_cid(source_cid)} / {SOURCE_CAMPAIGN_NAME}\n"
            "执行前会重新读取余额，只有余额 > 0 才启用。\n"
            f"每个 CID 的 App 系列将设为 USD {target_budget:.2f}。\n"
            f"全部同时投放的理论合计日预算：USD {aggregate:.2f}。\n"
            "软件会先写入并回读预算，再启用并回读状态。\n\n"
            "这会开始真实投放并产生费用，确定继续？"
        )
        if not messagebox.askyesno("确认一键复制并启用", prompt):
            return

        def task():
            results = []
            for profile_key, cids in grouped.items():
                browser_type, profile = split_window_key(profile_key)
                try:
                    with WarmupManager(
                        profile,
                        lambda text: self.events.put(("log", text)),
                        source_cid=source_cid,
                        target_daily_budget=target_budget,
                        browser_type=browser_type,
                    ) as manager:
                        window_results = manager.execute(cids)
                    for row in window_results:
                        row["browser_type"] = browser_type
                        row["profile_id"] = profile
                    results.extend(window_results)
                except Exception as exc:
                    results.extend({
                        "cid": cid, "cid_display": format_cid(cid),
                        "browser_type": browser_type,
                        "profile_id": profile, "success": False, "error": str(exc),
                    } for cid in sorted(cids))
            failures = [row for row in results if not row.get("success")]
            report = {
                "version": 1,
                "mode": "warmup_execute",
                "profile_id": ", ".join(grouped),
                "started_at": datetime.now().astimezone().isoformat(),
                "finished_at": datetime.now().astimezone().isoformat(),
                "success": not failures,
                "error": "；".join(
                    f"{row['cid_display']}：{row.get('error')}" for row in failures
                ),
                "accounts": [{
                    "cid": row["cid"],
                    "account_status": "可用" if row.get("success") else "执行失败",
                    "actions": [{
                        "kind": "ad",
                        "status": "created" if row.get("success") else "failed",
                        "object_name": str(row.get("campaign_name") or "App-1"),
                        "reason": (
                            f"系列 {row.get('campaign_id')} 已启用；日预算 "
                            f"{row.get('currency')} {row.get('daily_budget')}；余额 {row.get('balance')}；"
                            f"今日消耗 {row.get('daily_spend')}"
                            if row.get("success") else str(row.get("error") or "未知失败")
                        ),
                    }],
                } for row in results],
            }
            save_run_report(report)
            return results

        def success(results):
            by_target = {
                (
                    str(row.get("browser_type") or ADSPOWER),
                    str(row.get("profile_id") or ""),
                    str(row["cid"]),
                ): row
                for row in results
            }
            for iid, item in self.warmup_items.items():
                result = by_target.get((
                    str(item.get("browser_type") or ADSPOWER),
                    str(item.get("profile_id") or ""),
                    str(item["cid"]),
                ))
                if not result:
                    continue
                item["selected"] = False
                item["eligible"] = False
                item["action"] = "启用成功" if result.get("success") else "执行失败"
                item["reason"] = (
                    f"广告系列 {result.get('campaign_id')} 已按 USD "
                    f"{result.get('daily_budget')} 启用并回读校验"
                    if result.get("success") else str(result.get("error") or "未知失败")
                )
                self.warmup_tree.set(iid, "selected", "否")
                self.warmup_tree.set(iid, "campaign", result.get("campaign_name") or "App-1")
                self.warmup_tree.set(iid, "campaign_status", "已启用" if result.get("success") else "失败")
                if result.get("success"):
                    item["current_daily_budget"] = str(result.get("daily_budget") or "")
                    self.warmup_tree.set(
                        iid,
                        "budget",
                        f"{result.get('currency') or 'USD'} {result.get('daily_budget')} → "
                        f"{result.get('daily_budget')}",
                    )
                if result.get("daily_spend"):
                    self.warmup_tree.set(iid, "daily_spend", result.get("daily_spend"))
                self.warmup_tree.set(iid, "action", item["action"])
                self.warmup_tree.set(iid, "reason", item["reason"])
                self.warmup_tree.item(iid, tags=("eligible" if result.get("success") else "failed",))
            self._refresh_reports()
            ok = sum(bool(row.get("success")) for row in results)
            failed = len(results) - ok
            if failed:
                messagebox.showwarning("一键白量完成", f"成功 {ok} 个，失败 {failed} 个；具体原因已写入运行记录。")
            else:
                messagebox.showinfo("一键白量完成", f"已成功启用 {ok} 个 CID 的 App 系列。")

        self._run_async("一键复制并启用 App-1 白量", task, success)

    # ----- native Campaign / AdGroup / Ad hierarchy copy --------------------------
    def _entity_copy_context(self) -> tuple[str, str, str, str, str, str]:
        source_browser_type, source_profile = self._entity_copy_window("source")
        target_browser_type, target_profile = self._entity_copy_window("target")
        source_cid = self._cid_from_selector(self.copy_source_cid.get())
        target_cid = self._cid_from_selector(self.copy_target_cid.get())
        if not source_profile or not target_profile:
            raise ValueError("请选择来源和目标指纹浏览器窗口")
        if not source_cid or not target_cid:
            raise ValueError("请选择来源和目标 CID")
        return (
            source_browser_type, source_profile, source_cid,
            target_browser_type, target_profile, target_cid,
        )

    def _entity_copy_loaded_context(self) -> tuple[str, str, str, str, str, str]:
        context = self._entity_copy_context()
        if self.copy_catalogs is None or getattr(self, "copy_catalogs_context", None) != context:
            self.copy_catalogs = None
            self.copy_catalogs_context = None
            raise ValueError("来源或目标窗口/CID 已切换，请先重新读取复制结构")
        return context

    def _read_entity_copy_catalogs(self) -> None:
        try:
            context = self._entity_copy_context()
        except Exception as exc:
            messagebox.showerror("输入有误", str(exc))
            return

        def task():
            (
                source_browser_type, source_profile, source_cid,
                target_browser_type, target_profile, target_cid,
            ) = context
            with EntityCopyManager(
                source_profile, source_cid, target_profile, target_cid,
                log=lambda text: self.events.put(("log", text)),
                source_browser_type=source_browser_type,
                target_browser_type=target_browser_type,
            ) as manager:
                return manager.catalogs()

        def success(catalogs: dict[str, AdCatalog]) -> None:
            self.copy_catalogs = catalogs
            self.copy_catalogs_context = context
            self._show_entity_copy_catalogs(catalogs)

        self._run_async("读取复制来源与目标结构（只读）", task, success)

    def _show_entity_copy_catalogs(self, catalogs: dict[str, AdCatalog]) -> None:
        self.copy_catalogs = catalogs
        source = catalogs["source"]
        target = catalogs["target"]
        source_campaigns = [row for row in source.campaigns if row.get("status_raw") != 7]
        target_campaigns = [row for row in target.campaigns if row.get("status_raw") != 7]
        self.copy_source_campaign_by_label = {
            self._campaign_label(row): row for row in source_campaigns
        }
        self.copy_target_campaign_by_label = {
            self._campaign_label(row): row for row in target_campaigns
        }
        source_labels = list(self.copy_source_campaign_by_label)
        target_labels = list(self.copy_target_campaign_by_label)
        self.copy_source_campaign_box.configure(values=source_labels)
        self.copy_target_campaign_box.configure(values=target_labels)
        source_preferred = next((
            label for label, row in self.copy_source_campaign_by_label.items()
            if row.get("name") == "Display-3"
        ), source_labels[0] if source_labels else "")
        if self.copy_source_campaign.get() not in source_labels:
            self.copy_source_campaign.set(source_preferred)
        if self.copy_target_campaign.get() not in target_labels:
            self.copy_target_campaign.set(target_labels[0] if target_labels else "")
        self._sync_copy_source_groups()
        self._sync_copy_target_groups()
        self._copy_kind_changed()
        self._set_entity_copy_preview({
            "mode": "catalog_read",
            "write_performed": False,
            "source": {
                "cid": format_cid(self._cid_from_selector(self.copy_source_cid.get())),
                "campaigns": len(source.campaigns),
                "ad_groups": len(source.ad_groups),
                "ads": len(source.ads),
            },
            "target": {
                "cid": format_cid(self._cid_from_selector(self.copy_target_cid.get())),
                "campaigns": len(target.campaigns),
                "ad_groups": len(target.ad_groups),
                "ads": len(target.ads),
            },
        })
        self._log(
            f"层级复制页：来源 {len(source_campaigns)} 个系列 / 目标 {len(target_campaigns)} 个系列"
        )

    def _sync_copy_source_groups(self) -> None:
        catalog = (self.copy_catalogs or {}).get("source")
        campaign = getattr(self, "copy_source_campaign_by_label", {}).get(
            self.copy_source_campaign.get()
        )
        groups = [
            row for row in (catalog.ad_groups if catalog else [])
            if campaign and row.get("campaign_id") == campaign.get("campaign_id")
            and row.get("status_raw") != 3
        ]
        self.copy_source_group_by_label = {self._group_label(row): row for row in groups}
        labels = list(self.copy_source_group_by_label)
        self.copy_source_group_box.configure(values=labels)
        if self.copy_source_group.get() not in labels:
            self.copy_source_group.set(labels[0] if labels else "")
        self._sync_copy_source_ads()

    def _sync_copy_source_ads(self) -> None:
        catalog = (self.copy_catalogs or {}).get("source")
        group = getattr(self, "copy_source_group_by_label", {}).get(self.copy_source_group.get())
        ads = [
            row for row in (catalog.ads if catalog else [])
            if group and row.get("ad_group_id") == group.get("ad_group_id")
            and row.get("status_raw") != 3
        ]
        self.copy_source_ad_by_label = {self._ad_label(row): row for row in ads}
        labels = list(self.copy_source_ad_by_label)
        self.copy_source_ad_box.configure(values=labels)
        if self.copy_source_ad.get() not in labels:
            self.copy_source_ad.set(labels[0] if labels else "")

    def _sync_copy_target_groups(self) -> None:
        catalog = (self.copy_catalogs or {}).get("target")
        campaign = getattr(self, "copy_target_campaign_by_label", {}).get(
            self.copy_target_campaign.get()
        )
        groups = [
            row for row in (catalog.ad_groups if catalog else [])
            if campaign and row.get("campaign_id") == campaign.get("campaign_id")
            and row.get("status_raw") != 3
        ]
        self.copy_target_group_by_label = {self._group_label(row): row for row in groups}
        labels = list(self.copy_target_group_by_label)
        self.copy_target_group_box.configure(values=labels)
        if self.copy_target_group.get() not in labels:
            self.copy_target_group.set(labels[0] if labels else "")

    def _copy_kind_changed(self) -> None:
        kind = COPY_KIND_BY_LABEL.get(self.copy_kind.get(), COPY_CAMPAIGN)
        self.copy_source_group_box.configure(
            state="readonly" if kind in {COPY_AD_GROUP, COPY_AD} else "disabled"
        )
        self.copy_source_ad_box.configure(state="readonly" if kind == COPY_AD else "disabled")
        self.copy_target_campaign_box.configure(
            state="readonly" if kind in {COPY_AD_GROUP, COPY_AD} else "disabled"
        )
        self.copy_target_group_box.configure(state="readonly" if kind == COPY_AD else "disabled")

    def _entity_copy_parameters(self) -> dict[str, Any]:
        if not self.copy_catalogs:
            raise ValueError("请先读取来源与目标结构")
        kind = COPY_KIND_BY_LABEL.get(self.copy_kind.get())
        source_campaign = getattr(self, "copy_source_campaign_by_label", {}).get(
            self.copy_source_campaign.get()
        )
        source_group = getattr(self, "copy_source_group_by_label", {}).get(
            self.copy_source_group.get()
        )
        source_ad = getattr(self, "copy_source_ad_by_label", {}).get(self.copy_source_ad.get())
        target_campaign = getattr(self, "copy_target_campaign_by_label", {}).get(
            self.copy_target_campaign.get()
        )
        target_group = getattr(self, "copy_target_group_by_label", {}).get(
            self.copy_target_group.get()
        )
        if not kind or not source_campaign:
            raise ValueError("请选择复制层级和来源广告系列")
        if kind in {COPY_AD_GROUP, COPY_AD} and not source_group:
            raise ValueError("请选择来源广告组")
        if kind == COPY_AD and not source_ad:
            raise ValueError("请选择来源广告")
        if kind in {COPY_AD_GROUP, COPY_AD} and not target_campaign:
            raise ValueError("请选择目标广告系列")
        if kind == COPY_AD and not target_group:
            raise ValueError("请选择目标广告组")
        return {
            "kind": kind,
            "source_campaign_id": str(source_campaign["campaign_id"]),
            "source_ad_group_id": str((source_group or {}).get("ad_group_id") or ""),
            "source_ad_id": str((source_ad or {}).get("ad_id") or ""),
            "target_campaign_id": str((target_campaign or {}).get("campaign_id") or ""),
            "target_ad_group_id": str((target_group or {}).get("ad_group_id") or ""),
        }

    def _set_entity_copy_preview(self, value: dict[str, Any]) -> None:
        clean = {key: item for key, item in value.items() if not str(key).startswith("_")}
        self.copy_preview.configure(state="normal")
        self.copy_preview.delete("1.0", "end")
        self.copy_preview.insert("1.0", json.dumps(clean, ensure_ascii=False, indent=2))
        self.copy_preview.configure(state="disabled")

    @staticmethod
    def _entity_copy_result_summary(result: dict[str, Any]) -> str:
        """Return the operator-facing recovery and verification summary."""
        created = result.get("created") or {}
        object_id = next((
            str(created.get(key)) for key in ("campaign_id", "ad_group_id", "ad_id")
            if created.get(key)
        ), "—")
        lines = [f"新对象 ID：{object_id}", "状态：已暂停"]
        if result.get("step_status"):
            lines.append(f"复制进度：{result['step_status']}")
        if result.get("snapshot_path"):
            lines.append(f"本地快照：{result['snapshot_path']}")
        if result.get("state_path"):
            lines.append(f"恢复状态：{result['state_path']}")
        if result.get("idempotent_replay"):
            lines.append("本次为幂等恢复：未重复创建对象")
        verification = result.get("verification") or {}
        if result.get("post_read_verified"):
            lines.append("只读校验：层级、对象数量和暂停状态均通过")
        elif verification.get("reason"):
            lines.append(f"只读校验：{verification['reason']}")
        if result.get("failure_reason"):
            lines.append(f"失败原因：{result['failure_reason']}")
        return "\n".join(lines)

    @staticmethod
    def _entity_copy_report(
        source_browser_type: str,
        source_profile: str,
        source_cid: str,
        target_browser_type: str,
        target_profile: str,
        target_cid: str,
        mode: str,
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "mode": mode,
            "browser_type": target_browser_type,
            "source_browser_type": source_browser_type,
            "target_browser_type": target_browser_type,
            "profile_id": target_profile,
            "source_profile_id": source_profile,
            "target_profile_id": target_profile,
            "source_window_key": window_key(source_browser_type, source_profile),
            "target_window_key": window_key(target_browser_type, target_profile),
            "started_at": datetime.now().astimezone().isoformat(),
            "success": False,
            "source_cid": source_cid,
            "accounts": [{
                "cid": target_cid,
                "account_name": "",
                "account_status": "",
                "actions": [],
            }],
        }

    def _entity_copy_preflight(self) -> None:
        try:
            context = self._entity_copy_loaded_context()
            params = self._entity_copy_parameters()
            catalogs = self.copy_catalogs
        except Exception as exc:
            messagebox.showerror("复制预检失败", str(exc))
            return

        def task():
            report = self._entity_copy_report(*context, "entity_copy_preflight")
            (
                source_browser_type, source_profile, source_cid,
                target_browser_type, target_profile, target_cid,
            ) = context
            try:
                with EntityCopyManager(
                    source_profile, source_cid, target_profile, target_cid,
                    log=lambda text: self.events.put(("log", text)),
                    source_browser_type=source_browser_type,
                    target_browser_type=target_browser_type,
                ) as manager:
                    result = manager.preflight(catalogs["source"], catalogs["target"], **params)
                report.update({key: value for key, value in result.items() if not key.startswith("_")})
                report["success"] = True
                report["accounts"][0]["actions"] = [{
                    "kind": params["kind"],
                    "status": "would_create",
                    "object_name": str((result.get("source") or {}).get("name") or "复制对象"),
                    "reason": (
                        "原生复制；副本写后强制暂停并回读"
                        if (result.get("protocol") or {}).get("native_copy")
                        else "本地快照重建；图片重新上传，全部暂停并回读"
                    ),
                }]
                return result
            except Exception as exc:
                report["error"] = str(exc)
                raise
            finally:
                report["finished_at"] = datetime.now().astimezone().isoformat()
                save_run_report(report)

        def success(result):
            self._set_entity_copy_preview(result)
            self._refresh_reports()
            messagebox.showinfo("复制预检通过", "来源、目标父级和协议字段有效；此次没有写入。")

        self._run_async("层级复制协议预检（只读）", task, success)

    def _entity_copy_execute(self) -> None:
        try:
            context = self._entity_copy_loaded_context()
            params = self._entity_copy_parameters()
            source = getattr(self, "copy_source_campaign_by_label", {}).get(
                self.copy_source_campaign.get()
            ) or {}
        except Exception as exc:
            messagebox.showerror("不能复制", str(exc))
            return
        kind_label = self.copy_kind.get()
        prompt = (
            f"复制范围：{kind_label}\n"
            f"来源：{format_cid(context[2])} · {source.get('name') or source.get('campaign_id')}\n"
            f"目标：{format_cid(context[5])}\n\n"
            "Google 会创建新对象；软件随后强制暂停并回读校验。\n"
            "执行前会重新读取目标 CID 余额，只有余额可识别且大于 0 才创建。\n"
            "跨窗口广告系列会先下载到本地快照，再用目标窗口权限重新创建；"
            "图片会重新上传，所有新对象保持暂停。确定继续？"
        )
        if not messagebox.askyesno("确认层级复制", prompt):
            return

        def task():
            report = self._entity_copy_report(*context, "entity_copy")
            (
                source_browser_type, source_profile, source_cid,
                target_browser_type, target_profile, target_cid,
            ) = context
            try:
                with EntityCopyManager(
                    source_profile, source_cid, target_profile, target_cid,
                    log=lambda text: self.events.put(("log", text)),
                    source_browser_type=source_browser_type,
                    target_browser_type=target_browser_type,
                ) as manager:
                    fresh = manager.catalogs()
                    result = manager.copy(fresh["source"], fresh["target"], **params)
                    refreshed = manager.catalogs()
                report.update(result)
                report["success"] = True
                created = result.get("created") or {}
                object_id = next((
                    str(created.get(key)) for key in ("campaign_id", "ad_group_id", "ad_id")
                    if created.get(key)
                ), "")
                report["accounts"][0]["account_status"] = "可用"
                report["accounts"][0]["actions"] = [{
                    "kind": params["kind"],
                    "status": "created",
                    "object_name": object_id,
                    "reason": (
                        (
                            "本地可移植快照重建成功；图片已重传；全部暂停并回读；目标余额 "
                            if result.get("rebuild_strategy") in {
                                "local_snapshot", "local_portable_snapshot",
                            }
                            else "原生复制成功；暂停状态已回读；目标余额 "
                        )
                        + f"{(result.get('target_balance') or {}).get('balance') or '—'}"
                    ),
                }]
                return result, refreshed
            except Exception as exc:
                report["error"] = str(exc)
                report["accounts"][0]["actions"] = [{
                    "kind": params["kind"], "status": "failed", "error": str(exc),
                }]
                raise
            finally:
                report["finished_at"] = datetime.now().astimezone().isoformat()
                save_run_report(report)

        def success(payload):
            result, refreshed = payload
            self._set_entity_copy_preview(result)
            self.copy_catalogs_context = context
            self._show_entity_copy_catalogs(refreshed)
            self._set_entity_copy_preview(result)
            self._refresh_reports()
            summary = self._entity_copy_result_summary(result)
            self._log("层级复制完成：" + summary.replace("\n", "；"))
            messagebox.showinfo(
                "复制成功",
                f"{kind_label}复制完成\n\n{summary}",
            )

        self._run_async(f"复制{kind_label}并强制暂停", task, success)

    # ----- cross-account snapshot copy ---------------------------------------------
    def _snap_export_context(self) -> tuple[str, str, str]:
        browser_type, profile = self._snap_export_selected_window()
        cid = self._cid_from_selector(self.snap_export_cid.get())
        if not profile:
            raise ValueError("请先在批量任务页新增指纹浏览器窗口")
        return browser_type, profile, cid

    def _choose_snap_export_dir(self) -> None:
        path = filedialog.askdirectory(initialdir=self.snap_export_dir.get() or None)
        if path:
            self.snap_export_dir.set(path)

    def _read_snap_export_campaigns(self) -> None:
        try:
            context = self._snap_export_context()
            browser_type, profile, cid = context
        except Exception as exc:
            messagebox.showerror("输入有误", str(exc))
            return

        def task():
            with AdManager(
                profile, cid, lambda text: self.events.put(("log", text)),
                browser_type=browser_type,
            ) as manager:
                return manager.catalog()

        def success(catalog: AdCatalog) -> None:
            self.snap_export_catalog = catalog
            self.snap_export_catalog_context = context
            rows = [
                row for row in catalog.campaigns
                if row.get("channel_raw") in SUPPORTED_CHANNELS and row.get("status_raw") != 7
            ]
            self.snap_export_campaign_by_label = {
                self._campaign_label(row): row for row in rows
            }
            labels = list(self.snap_export_campaign_by_label)
            self.snap_export_campaign_box.configure(values=labels)
            self.snap_export_campaign.set(labels[0] if labels else "")

        self._run_async("读取广告系列（只读）", task, success)

    def _export_snapshot(self) -> None:
        try:
            browser_type, profile, cid = self._snap_export_context()
            campaign_row = self.snap_export_campaign_by_label.get(
                self.snap_export_campaign.get()
            )
            if not campaign_row:
                raise ValueError("请先读取并选择要导出的广告系列")
            export_dir = Path(self.snap_export_dir.get() or str(Path.home() / "Desktop"))
        except Exception as exc:
            messagebox.showerror("不能导出", str(exc))
            return
        campaign_id = str(campaign_row["campaign_id"])

        def task():
            with AdManager(
                profile, cid, lambda text: self.events.put(("log", text)),
                browser_type=browser_type,
            ) as manager:
                exporter = SnapshotExporter(
                    manager.account, lambda text: self.events.put(("log", text))
                )
                return exporter.export(campaign_id, export_dir)

        def success(result: dict[str, Any]) -> None:
            self.snap_export_result = result
            campaign = result.get("campaign") or {}
            self.snap_export_summary.set(
                f"已导出：{campaign.get('11') or '未命名'} | "
                f"{len(result.get('ad_groups') or [])} 组 / "
                f"{len(result.get('ads') or [])} 广告 / "
                f"{len(result.get('assets') or [])} 图片\n{result.get('snapshot_path')}"
            )
            gaps = result.get("targeting_gaps") or []
            gap_line = (
                f"\n还有 {len(gaps)} 条定向没识别，导入时不会带上。"
                if gaps else ""
            )
            messagebox.showinfo(
                "导出成功",
                f"快照已保存到：\n{result.get('snapshot_path')}"
                f"\n定向：{summarize_targeting(result)}{gap_line}",
            )

        self._run_async("导出快照", task, success)

    def _choose_snap_import_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择快照文件",
            filetypes=[("JSON 快照", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
            snapshot.setdefault("snapshot_path", path)
            validate_snapshot(snapshot)
        except Exception as exc:
            messagebox.showerror("快照无效", str(exc))
            return
        self.snap_import_snapshot = snapshot
        self.snap_import_path.set(path)
        campaign = snapshot.get("campaign") or {}
        currency = str(campaign.get("18") or "").strip().upper() or "未知"
        source_cid = str((snapshot.get("source") or {}).get("cid") or "")
        self.snap_import_summary.set(
            f"广告系列：{campaign.get('11') or '未命名'} | "
            f"来源 CID {format_cid(source_cid) if source_cid else '未知'} | 币种 {currency} | "
            f"{len(snapshot.get('ad_groups') or [])} 组 / "
            f"{len(snapshot.get('ads') or [])} 广告 / "
            f"{len(snapshot.get('assets') or [])} 图片"
        )

    def _choose_snap_asset_override_dir(self) -> None:
        path = filedialog.askdirectory(initialdir=self.snap_asset_override_dir.get() or None)
        if path:
            self.snap_asset_override_dir.set(path)

    def _sync_snap_target_tree(self) -> None:
        if not hasattr(self, "snap_target_tree"):
            return
        selected_keys = {
            (
                str(self.snap_target_items[iid]["browser_type"]),
                str(self.snap_target_items[iid]["profile_id"]),
                str(self.snap_target_items[iid]["cid"]),
            )
            for iid in self.snap_target_tree.selection()
            if iid in self.snap_target_items
        }
        self.snap_target_tree.delete(*self.snap_target_tree.get_children())
        self.snap_target_items.clear()
        restore_selection: list[str] = []
        for window_iid in self.task_tree.get_children(""):
            browser_type = normalize_browser_type(
                self.task_tree.set(window_iid, "browser_type")
            )
            profile = str(self.task_tree.set(window_iid, "profile") or "").strip()
            for task_iid in self._window_rows(window_iid):
                cid = str(self.task_tree.set(task_iid, "cid") or "").strip()
                account_type = str(
                    self.task_tree.set(task_iid, "account_type") or "CID"
                ).strip().upper()
                if not profile or not cid or account_type == "MCC":
                    continue
                status = str(self.task_tree.set(task_iid, "status") or "状态未知")
                balance = str(self.task_tree.set(task_iid, "balance") or "未读取")
                iid = self.snap_target_tree.insert(
                    "", "end",
                    values=(
                        self._report_profile_display(profile),
                        format_cid(cid), status, balance,
                        self.snap_target_labels.get(cid, ""),
                    ),
                    tags=(self._task_row_tag("CID", status),),
                )
                self.snap_target_items[iid] = {
                    "browser_type": browser_type, "profile_id": profile, "cid": cid,
                }
                if (browser_type, profile, cid) in selected_keys:
                    restore_selection.append(iid)
        if restore_selection:
            self.snap_target_tree.selection_set(restore_selection)
        self._snap_target_selection_changed()

    def _snap_select_targets(self, selected: bool) -> None:
        if selected:
            self.snap_target_tree.selection_set(self.snap_target_tree.get_children(""))
        else:
            self.snap_target_tree.selection_remove(self.snap_target_tree.selection())
        self._snap_target_selection_changed()

    def _snap_target_selection_changed(self) -> None:
        selected = len(self.snap_target_tree.selection())
        total = len(self.snap_target_tree.get_children(""))
        self.snap_target_summary.set(f"已选择 {selected} / {total} 个 CID")

    def _snap_target_tree_double_click(self, event: tk.Event) -> None:
        column = self.snap_target_tree.identify_column(event.x)
        row = self.snap_target_tree.identify_row(event.y)
        if column != "#5" or not row or row not in self.snap_target_items:
            return
        cid = self.snap_target_items[row]["cid"]
        label = simpledialog.askstring(
            "命名标签", "请输入该目标账户的命名标签（用于生成新广告系列名称）",
            initialvalue=self.snap_target_labels.get(cid, ""), parent=self,
        )
        if label is None:
            return
        label = label.strip()
        self.snap_target_labels[cid] = label
        self.snap_target_tree.set(row, "label", label)

    def _snap_import_preflight(self) -> None:
        snapshot = self.snap_import_snapshot
        if not snapshot:
            messagebox.showerror("不能预检", "请先选择快照文件")
            return
        try:
            validate_snapshot(snapshot)
            validate_snapshot_files(snapshot)
        except Exception as exc:
            messagebox.showerror("预检失败", str(exc))
            return
        selection = self.snap_target_tree.selection()
        if not selection:
            messagebox.showerror("不能预检", "请先勾选至少一个目标账户")
            return
        missing = [
            self.snap_target_items[iid]["cid"] for iid in selection
            if not self.snap_target_labels.get(self.snap_target_items[iid]["cid"], "").strip()
        ]
        if missing:
            messagebox.showerror(
                "不能预检",
                "以下目标账户尚未设置命名标签（双击命名标签列编辑）：\n"
                + "\n".join(format_cid(cid) for cid in missing),
            )
            return
        campaign = snapshot.get("campaign") or {}
        messagebox.showinfo(
            "预检通过",
            f"快照校验通过：{campaign.get('11') or '未命名'}\n"
            f"目标账户数：{len(selection)}\n"
            f"初始状态：{'暂停' if self.snap_initial_status_paused.get() else '启用'}\n"
            f"素材来源：{self.snap_asset_override_dir.get().strip() or '快照自带素材'}",
        )

    def _snap_import_execute(self) -> None:
        snapshot = self.snap_import_snapshot
        if not snapshot:
            messagebox.showerror("不能执行", "请先选择快照文件")
            return
        try:
            validate_snapshot(snapshot)
            validate_snapshot_files(snapshot)
        except Exception as exc:
            messagebox.showerror("快照校验失败", str(exc))
            return
        selection = list(self.snap_target_tree.selection())
        if not selection:
            messagebox.showerror("不能执行", "请先勾选至少一个目标账户")
            return
        targets: list[dict[str, Any]] = []
        for iid in selection:
            item = self.snap_target_items[iid]
            cid = item["cid"]
            label = self.snap_target_labels.get(cid, "").strip()
            if not label:
                messagebox.showerror(
                    "不能执行",
                    f"目标账户 {format_cid(cid)} 尚未设置命名标签（双击命名标签列编辑）",
                )
                return
            targets.append({
                "cid": cid, "label": label,
                "initial_status": self.snap_initial_status_paused.get(),
                "browser_type": item["browser_type"], "profile_id": item["profile_id"],
            })
        asset_override = self.snap_asset_override_dir.get().strip()
        asset_override_dir = Path(asset_override) if asset_override else None
        campaign = snapshot.get("campaign") or {}
        prompt = (
            f"即将向 {len(targets)} 个目标账户批量重建广告系列"
            f"“{campaign.get('11') or '未命名'}”\n"
            f"初始状态：{'暂停' if self.snap_initial_status_paused.get() else '启用'}\n"
            f"素材来源：{asset_override_dir or '快照自带素材'}\n\n"
            "执行前会重新读取每个目标 CID 余额，只有余额可识别且大于 0 才创建；"
            "币种不同时会尝试按日预算自动换算汇率（出价目标金额不换算，需人工核对）。"
            "不会新建预算：目标账号里要先有一条金额相同的日预算。"
            "系列先暂停。最后请你在网页上点发布，弹出密钥时摸一下 U 盘。"
            "单个账户失败不影响其余账户继续执行。确定继续？"
        )
        if not messagebox.askyesno("确认批量导入", prompt):
            return

        def task():
            report: dict[str, Any] = {
                "version": 1,
                "mode": "snapshot_batch_import",
                "started_at": datetime.now().astimezone().isoformat(),
                "success": False,
                "snapshot_id": snapshot.get("snapshot_id"),
                "snapshot_path": snapshot.get("snapshot_path"),
                "source_cid": (snapshot.get("source") or {}).get("cid"),
                "targets": [{"cid": t["cid"], "label": t["label"]} for t in targets],
            }
            source_currency = str(campaign.get("18") or "").strip().upper()
            original_campaign = copy.deepcopy(campaign)
            opened: list[AdManager] = []

            def importer_factory(cid: str) -> SnapshotImporter:
                if opened:
                    opened.pop().close()
                target = next(t for t in targets if t["cid"] == cid)
                manager = AdManager(
                    target["profile_id"], cid,
                    lambda text: self.events.put(("log", text)),
                    browser_type=target["browser_type"],
                )
                opened.append(manager)
                balance = manager.runner.require_positive_account_balance(
                    manager.account, action="跨账号快照导入",
                )
                target_currency = str(balance.get("currency") or "").strip().upper()
                snapshot["campaign"] = copy.deepcopy(original_campaign)
                if source_currency and target_currency and source_currency != target_currency:
                    try:
                        rate = fetch_rate(source_currency, target_currency)
                        original_budget = int(snapshot["campaign"].get("17") or 0)
                        snapshot["campaign"]["17"] = str(
                            convert_micros(
                                original_budget, source_currency, target_currency,
                                manual_rate=rate,
                            )
                        )
                        self.events.put((
                            "log",
                            f"[{format_cid(cid)}] 日预算已按汇率 "
                            f"{source_currency}->{target_currency}（{rate}）换算；"
                            "出价目标金额未随汇率换算，请人工核对",
                        ))
                    except Exception as exc:
                        self.events.put((
                            "log",
                            f"[{format_cid(cid)}] 汇率换算失败，日预算将按原始数值创建，"
                            f"请人工核对：{exc}",
                        ))
                return SnapshotImporter(
                    manager.account, manager.runner, manager._manager_ocid(),
                    lambda text: self.events.put(("log", text)),
                )

            try:
                results = batch_recreate(
                    importer_factory, snapshot, targets,
                    asset_override_dir=asset_override_dir,
                    log=lambda text: self.events.put(("log", text)),
                )
                report["results"] = results
                report["success"] = any(item.get("success") for item in results)
                return results
            finally:
                if opened:
                    opened.pop().close()
                snapshot["campaign"] = original_campaign
                report["finished_at"] = datetime.now().astimezone().isoformat()
                save_run_report(report)

        def success(results: list[dict[str, Any]]) -> None:
            self.snap_batch_results = results
            self._render_snap_batch_results(results)
            ok = sum(1 for item in results if item.get("success"))
            lines = [f"成功 {ok} / {len(results)} 个目标账户"]
            for item in results:
                label = str(item.get("cid_display") or item.get("cid") or "")
                if item.get("success"):
                    if item.get("next_step"):
                        lines.append(f"{label}：{item['next_step']}")
                    for warning in item.get("targeting_warnings") or []:
                        lines.append(f"{label}：{warning}")
                elif item.get("error"):
                    lines.append(f"{label}：{item['error']}")
            messagebox.showinfo("批量导入完成", "\n".join(lines))

        self._run_async("批量导入快照", task, success)

    def _render_snap_batch_results(self, results: list[dict[str, Any]]) -> None:
        self.snap_result_preview.configure(state="normal")
        self.snap_result_preview.delete("1.0", "end")
        self.snap_result_preview.insert(
            "1.0", json.dumps(results, ensure_ascii=False, indent=2)
        )
        self.snap_result_preview.configure(state="disabled")

    # ----- responsive Display ad creation -----------------------------------------
    def _ad_context(self) -> tuple[str, str, str]:
        browser_type, profile = self._ad_selected_window()
        cid = self._cid_from_selector(self.ad_cid.get())
        if not profile:
            raise ValueError("请先在批量任务页新增指纹浏览器窗口")
        return browser_type, profile, cid

    def _ad_loaded_catalog_context(self) -> tuple[str, str, str]:
        context = self._ad_context()
        if self.ad_catalog is None or getattr(self, "ad_catalog_context", None) != context:
            self.ad_catalog = None
            self.ad_catalog_context = None
            raise ValueError("指纹浏览器窗口或 CID 已切换，请先重新读取广告结构")
        return context

    @staticmethod
    def _campaign_label(row: dict[str, Any]) -> str:
        return f"{row.get('name') or '未命名'} | {row.get('campaign_id')} | {row.get('status')}"

    @staticmethod
    def _group_label(row: dict[str, Any]) -> str:
        return f"{row.get('name') or '未命名'} | {row.get('ad_group_id')} | {row.get('status')}"

    @staticmethod
    def _ad_label(row: dict[str, Any]) -> str:
        return f"{row.get('ad_type')} | {row.get('ad_id')} | {row.get('status')}"

    def _read_ad_catalog(self) -> None:
        try:
            context = self._ad_context()
            browser_type, profile, cid = context
        except Exception as exc:
            messagebox.showerror("输入有误", str(exc))
            return

        def task():
            with AdManager(
                profile, cid, lambda text: self.events.put(("log", text)),
                browser_type=browser_type,
            ) as manager:
                return manager.catalog()

        def success(catalog: AdCatalog) -> None:
            self.ad_catalog = catalog
            self.ad_catalog_context = context
            self._show_ad_catalog(catalog)

        self._run_async("读取广告结构（只读）", task, success)

    def _show_ad_catalog(self, catalog: AdCatalog) -> None:
        self.ad_catalog = catalog
        display_campaigns = [
            row for row in catalog.campaigns
            if row.get("channel_raw") == 2 and row.get("status_raw") != 7
        ]
        self.ad_campaign_by_label = {
            self._campaign_label(row): row for row in display_campaigns
        }
        labels = list(self.ad_campaign_by_label)
        self.ad_source_campaign_box.configure(values=labels)
        self.ad_target_campaign_box.configure(values=labels)
        preferred = next(
            (label for label, row in self.ad_campaign_by_label.items() if row.get("name") == "Display-3"),
            labels[0] if labels else "",
        )
        if self.ad_source_campaign.get() not in labels:
            self.ad_source_campaign.set(preferred)
        if self.ad_target_campaign.get() not in labels:
            self.ad_target_campaign.set(preferred)
        self._sync_ad_source_groups()
        self._sync_ad_target_groups()

        self.ad_tree.delete(*self.ad_tree.get_children())
        self.ad_tree_items: dict[str, dict[str, Any]] = {}
        hierarchy = display_ad_hierarchy(catalog)
        group_count = 0
        ad_count = 0
        selected_ad_iid = ""
        selected_ad = getattr(self, "ad_source_ad_by_label", {}).get(self.ad_source_ad.get())
        for campaign_node in hierarchy:
            campaign = campaign_node["campaign"]
            groups = campaign_node["ad_groups"]
            campaign_ad_count = sum(len(node["ads"]) for node in groups)
            campaign_iid = self.ad_tree.insert(
                "",
                "end",
                text=str(campaign.get("name") or "未命名广告系列"),
                values=(
                    "广告系列",
                    campaign.get("campaign_id"),
                    campaign.get("status"),
                    f"{len(groups)} 组 / {campaign_ad_count} 广告",
                ),
                open=True,
                tags=("campaign",),
            )
            self.ad_tree_items[campaign_iid] = {
                "level": "campaign", "campaign": campaign,
            }
            for group_node in groups:
                group = group_node["ad_group"]
                ads = group_node["ads"]
                group_count += 1
                ad_count += len(ads)
                group_iid = self.ad_tree.insert(
                    campaign_iid,
                    "end",
                    text=str(group.get("name") or "未命名广告组"),
                    values=(
                        "广告组", group.get("ad_group_id"), group.get("status"),
                        f"{len(ads)} 个广告",
                    ),
                    open=True,
                    tags=("group",),
                )
                self.ad_tree_items[group_iid] = {
                    "level": "group", "campaign": campaign, "group": group,
                }
                if not ads:
                    self.ad_tree.insert(
                        group_iid,
                        "end",
                        text="（无响应式展示广告）",
                        values=("提示", "", "", ""),
                        tags=("empty",),
                    )
                    continue
                for index, ad in enumerate(ads, start=1):
                    status_raw = ad.get("status_raw")
                    tag = "ad_enabled" if status_raw == 1 else (
                        "ad_paused" if status_raw == 2 else "ad_other"
                    )
                    ad_iid = self.ad_tree.insert(
                        group_iid,
                        "end",
                        text=f"广告 {index} · {ad.get('ad_type') or '未知类型'}",
                        values=("广告", ad.get("ad_id"), ad.get("status"), ""),
                        tags=(tag,),
                    )
                    self.ad_tree_items[ad_iid] = {
                        "level": "ad", "campaign": campaign, "group": group, "ad": ad,
                    }
                    if selected_ad and ad.get("ad_id") == selected_ad.get("ad_id"):
                        selected_ad_iid = ad_iid
        if selected_ad_iid:
            self.ad_tree.selection_set(selected_ad_iid)
            self.ad_tree.focus(selected_ad_iid)
            self.ad_tree.see(selected_ad_iid)
        self._log(
            f"广告创建页层级读取完成：{len(hierarchy)} 个展示广告系列，"
            f"{group_count} 个广告组，{ad_count} 个响应式展示广告"
        )
        if not labels:
            messagebox.showwarning("没有展示广告系列", "该 CID 中没有可用的展示广告系列")

    def _sync_ad_source_groups(self, *, select_default: bool = True) -> None:
        catalog = self.ad_catalog
        campaign = getattr(self, "ad_campaign_by_label", {}).get(self.ad_source_campaign.get())
        groups = [
            row for row in (catalog.ad_groups if catalog else [])
            if campaign and row.get("campaign_id") == campaign.get("campaign_id")
            and row.get("status_raw") != 3
        ]
        self.ad_source_group_by_label = {self._group_label(row): row for row in groups}
        labels = list(self.ad_source_group_by_label)
        self.ad_source_group_box.configure(values=labels)
        if self.ad_source_group.get() not in labels:
            self.ad_source_group.set(labels[0] if select_default and labels else "")
        self._sync_ad_source_ads(select_default=select_default)

    def _sync_ad_source_ads(self, *, select_default: bool = True) -> None:
        catalog = self.ad_catalog
        group = getattr(self, "ad_source_group_by_label", {}).get(self.ad_source_group.get())
        ads = [
            row for row in (catalog.ads if catalog else [])
            if group and row.get("ad_group_id") == group.get("ad_group_id")
            and row.get("ad_type") == "响应式展示广告"
            and row.get("status_raw") != 3
        ]
        self.ad_source_ad_by_label = {self._ad_label(row): row for row in ads}
        labels = list(self.ad_source_ad_by_label)
        self.ad_source_ad_box.configure(values=labels)
        if self.ad_source_ad.get() not in labels:
            self.ad_source_ad.set(labels[0] if select_default and labels else "")
        self._load_selected_ad_template()

    def _sync_ad_target_groups(self) -> None:
        catalog = self.ad_catalog
        campaign = getattr(self, "ad_campaign_by_label", {}).get(self.ad_target_campaign.get())
        groups = [
            row for row in (catalog.ad_groups if catalog else [])
            if campaign and row.get("campaign_id") == campaign.get("campaign_id")
            and row.get("status_raw") != 3
        ]
        self.ad_target_group_by_label = {self._group_label(row): row for row in groups}
        labels = list(self.ad_target_group_by_label)
        self.ad_target_group_box.configure(values=labels)
        if self.ad_target_group.get() not in labels:
            source = getattr(self, "ad_source_group_by_label", {}).get(self.ad_source_group.get())
            preferred = next(
                (label for label, row in self.ad_target_group_by_label.items()
                 if source and row.get("ad_group_id") == source.get("ad_group_id")),
                labels[0] if labels else "",
            )
            self.ad_target_group.set(preferred)

    def _select_ad_images(self, role: str) -> None:
        paths = filedialog.askopenfilenames(
            title=f"选择{ROLE_LABELS[role]}图片",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg"), ("所有文件", "*.*")],
        )
        if not paths:
            return
        try:
            validated = [validate_role_image(path, role) for path in paths]
        except Exception as exc:
            messagebox.showerror("图片不符合要求", str(exc))
            return
        unique: dict[str, str] = {}
        for item in validated:
            unique.setdefault(item.content_sha256, item.path)
        self.ad_local_images[role] = list(unique.values())
        self.ad_image_plan.set("（手工选择本地图片）")
        self._refresh_ad_asset_summary()

    def _select_ad_images_auto(self) -> None:
        paths = filedialog.askopenfilenames(
            title="批量选择图片并自动识别用途",
            filetypes=[("图片文件", "*.png *.jpg *.jpeg"), ("所有文件", "*.*")],
        )
        if not paths:
            return
        grouped, details = classify_image_roles(paths)
        counts = {role: len(images) for role, images in grouped.items()}
        if any(counts.values()):
            for role, images in grouped.items():
                if images:
                    self.ad_local_images[role] = [info.path for info in images]
            self.ad_image_plan.set("（批量自动识别本地图片）")
            self._refresh_ad_asset_summary()
        errors = [row for row in details if row.get("error")]
        duplicates = sum(bool(row.get("duplicate")) for row in details)
        summary = (
            f"已识别：横图 {counts['landscape']}、方图 {counts['square']}、"
            f"竖图 {counts['portrait']}；重复 {duplicates}；失败 {len(errors)}"
        )
        self.ad_auto_classify_summary.configure(
            text=summary,
            foreground=COLOR_WARNING if errors else COLOR_SUCCESS,
        )
        self._log(f"广告图片批量自动识别：{summary}")
        if errors:
            messagebox.showwarning(
                "部分图片未使用",
                summary + "\n\n" + "\n".join(str(row["reason"]) for row in errors[:12]),
            )

    def _clear_ad_images(self) -> None:
        for role in self.ad_local_images:
            self.ad_local_images[role] = []
        self.ad_image_plan.set("（不使用图片方案）")
        self._refresh_ad_asset_summary()

    def _refresh_ad_asset_summary(self) -> None:
        ad = getattr(self, "ad_source_ad_by_label", {}).get(self.ad_source_ad.get())
        if not ad:
            self.ad_asset_summary.configure(text="素材：请选择层级树中的具体广告")
            return
        names = ad.get("asset_names") or {}
        lines = []
        for role in ("landscape", "square", "portrait"):
            local_paths = self.ad_local_images.get(role) or []
            if local_paths:
                local_names = [Path(path).name for path in local_paths]
                lines.append(
                    f"{ROLE_LABELS[role]}：本地 {len(local_paths)} 张（将替换参考素材）— "
                    f"{', '.join(local_names)}"
                )
            else:
                source_names = names.get(role) or []
                lines.append(
                    f"{ROLE_LABELS[role]}：沿用参考广告 {len(ad.get(f'{role}_assets') or [])} 张 — "
                    f"{', '.join(source_names) or '无'}"
                )
        self.ad_asset_summary.configure(text="\n".join(lines))

    def _render_ad_reference_images(
        self, rows: list[dict[str, Any]], *, loading: bool = False, error: str = "",
    ) -> None:
        for child in self.ad_reference_inner.winfo_children():
            child.destroy()
        self.ad_reference_photo_images.clear()
        if loading:
            self.ad_reference_image_status.configure(
                text="正在通过当前浏览器登录会话读取并缓存图片……",
                foreground=COLOR_PRIMARY,
            )
            ttk.Label(self.ad_reference_inner, text="图片读取中，请稍候……").pack(
                side="left", padx=14, pady=45,
            )
            return
        if error:
            self.ad_reference_image_status.configure(
                text=f"读取失败：{error}", foreground=COLOR_DANGER,
            )
            ttk.Label(
                self.ad_reference_inner, text=error, foreground=COLOR_DANGER,
                wraplength=560,
            ).pack(side="left", padx=12, pady=35)
            return
        if not rows:
            self.ad_reference_image_status.configure(
                text="该参考广告没有可显示的图片", foreground=COLOR_MUTED,
            )
            ttk.Label(self.ad_reference_inner, text="没有图片素材").pack(
                side="left", padx=14, pady=45,
            )
            return
        success_count = sum(bool(row.get("local_path")) for row in rows)
        failed_count = len(rows) - success_count
        self.ad_reference_image_status.configure(
            text=f"已读取 {success_count}/{len(rows)} 张"
            + (f" · 失败 {failed_count} 张" if failed_count else " · 全部已缓存"),
            foreground=COLOR_WARNING if failed_count else COLOR_SUCCESS,
        )
        for row in rows:
            card = ttk.Frame(self.ad_reference_inner, padding=5)
            card.pack(side="left", padx=(0, 6), pady=2)
            ttk.Label(
                card,
                text=f"{row.get('role_label') or '图片'} · {row.get('asset_id') or '—'}",
                foreground=COLOR_PRIMARY_HOVER,
            ).pack(anchor="w")
            local_path = str(row.get("local_path") or "")
            if local_path:
                try:
                    with Image.open(local_path) as source:
                        source.load()
                        preview = source.convert("RGBA")
                    preview.thumbnail((168, 88), Image.Resampling.LANCZOS)
                    background = Image.new("RGBA", (168, 88), "white")
                    offset = ((168 - preview.width) // 2, (88 - preview.height) // 2)
                    background.alpha_composite(preview, offset)
                    photo = ImageTk.PhotoImage(background.convert("RGB"))
                    self.ad_reference_photo_images.append(photo)
                    image_label = ttk.Label(card, image=photo, cursor="hand2")
                    image_label.pack(pady=(3, 2))
                    image_label.bind(
                        "<Double-Button-1>",
                        lambda _event, path=local_path: self._open_reference_image(path),
                    )
                except (OSError, UnidentifiedImageError, ValueError) as exc:
                    ttk.Label(
                        card, text=f"缓存图片无效\n{exc}", foreground=COLOR_DANGER,
                        width=24, anchor="center",
                    ).pack(pady=(12, 10))
            else:
                ttk.Label(
                    card, text=f"下载失败\n{row.get('error') or '未知原因'}",
                    foreground=COLOR_DANGER, width=24, anchor="center",
                    wraplength=165,
                ).pack(pady=(12, 10))
            dimensions = (
                f"{row.get('width')}×{row.get('height')}"
                if row.get("width") and row.get("height") else "尺寸未知"
            )
            ttk.Label(
                card, text=f"{dimensions} · {row.get('name') or '未命名'}",
                foreground=COLOR_MUTED, wraplength=168,
            ).pack(anchor="w")
        self.ad_reference_canvas.update_idletasks()
        self.ad_reference_canvas.configure(
            scrollregion=self.ad_reference_canvas.bbox("all")
        )
        self.ad_reference_canvas.xview_moveto(0)

    def _open_reference_image(self, path: str) -> None:
        target = Path(path)
        if not target.is_file():
            messagebox.showerror("无法打开图片", f"缓存文件不存在：{target}")
            return
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("无法打开图片", str(exc))

    def _request_ad_reference_images(self, ad: dict[str, Any]) -> None:
        try:
            browser_type, profile, cid = self._ad_context()
        except Exception as exc:
            self._render_ad_reference_images([], error=str(exc))
            return
        ad_id = str(ad.get("ad_id") or "")
        key = (browser_type, profile, cid, ad_id)
        self.ad_reference_request_key = key
        if key in self.ad_reference_image_rows:
            self._render_ad_reference_images(self.ad_reference_image_rows[key])
            return
        self._render_ad_reference_images([], loading=True)
        if key in self.ad_reference_loading_keys:
            return
        self.ad_reference_loading_keys.add(key)
        ad_copy = copy.deepcopy(ad)

        def worker() -> None:
            try:
                with AdManager(
                    profile, cid, lambda text: self.events.put(("log", text)),
                    browser_type=browser_type,
                ) as manager:
                    rows = manager.reference_images(ad_copy)
                self.events.put(("ad_reference_images", (key, rows)))
            except Exception as exc:
                self.events.put(("ad_reference_images_error", (key, str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    def _load_selected_ad_template(self) -> None:
        ad = getattr(self, "ad_source_ad_by_label", {}).get(self.ad_source_ad.get())
        if not ad:
            for entry in (self.ad_final_url, self.ad_business_name, self.ad_long_headline):
                entry.delete(0, "end")
            self.ad_headlines.delete("1.0", "end")
            self.ad_descriptions.delete("1.0", "end")
            self._refresh_ad_asset_summary()
            self.ad_reference_request_key = None
            self._render_ad_reference_images([])
            return
        self.ad_final_url.delete(0, "end")
        self.ad_final_url.insert(0, str((ad.get("final_urls") or [""])[0]))
        self.ad_business_name.delete(0, "end")
        self.ad_business_name.insert(0, str(ad.get("business_name") or ""))
        self.ad_long_headline.delete(0, "end")
        self.ad_long_headline.insert(0, str(ad.get("long_headline") or ""))
        self.ad_headlines.delete("1.0", "end")
        self.ad_headlines.insert("1.0", "\n".join(ad.get("headlines") or []))
        self.ad_descriptions.delete("1.0", "end")
        self.ad_descriptions.insert("1.0", "\n".join(ad.get("descriptions") or []))
        self._refresh_ad_asset_summary()
        self._request_ad_reference_images(ad)

    def _select_ad_from_tree(self) -> None:
        selected = self.ad_tree.selection()
        node = getattr(self, "ad_tree_items", {}).get(selected[0]) if selected else None
        if not node:
            return
        campaign = node.get("campaign") or {}
        campaign_label = next((
            label for label, row in getattr(self, "ad_campaign_by_label", {}).items()
            if row.get("campaign_id") == campaign.get("campaign_id")
        ), "")
        if campaign_label:
            self.ad_source_campaign.set(campaign_label)
            self.ad_source_group.set("")
            self.ad_source_ad.set("")
            self._sync_ad_source_groups(select_default=False)
        if node.get("level") == "campaign":
            return
        group = node.get("group") or {}
        group_label = next((
            label for label, row in getattr(self, "ad_source_group_by_label", {}).items()
            if row.get("ad_group_id") == group.get("ad_group_id")
        ), "")
        if group_label:
            self.ad_source_group.set(group_label)
            self.ad_source_ad.set("")
            self._sync_ad_source_ads(select_default=False)
        if node.get("level") == "group":
            return
        ad = node.get("ad") or {}
        ad_label = next((
            label for label, row in getattr(self, "ad_source_ad_by_label", {}).items()
            if row.get("ad_id") == ad.get("ad_id")
        ), "")
        if ad_label:
            self.ad_source_ad.set(ad_label)
            self._load_selected_ad_template()

    def _ad_parameters(self) -> dict[str, Any]:
        if not self.ad_catalog:
            raise ValueError("请先读取广告结构")
        source = getattr(self, "ad_source_ad_by_label", {}).get(self.ad_source_ad.get())
        campaign = getattr(self, "ad_campaign_by_label", {}).get(self.ad_target_campaign.get())
        group = getattr(self, "ad_target_group_by_label", {}).get(self.ad_target_group.get())
        if not source:
            raise ValueError("请选择参考广告")
        if not campaign or not group:
            raise ValueError("请选择目标广告系列和广告组")
        fields = {
            "final_url": self.ad_final_url.get().strip(),
            "business_name": self.ad_business_name.get().strip(),
            "headlines": self.ad_headlines.get("1.0", "end").splitlines(),
            "long_headline": self.ad_long_headline.get().strip(),
            "descriptions": self.ad_descriptions.get("1.0", "end").splitlines(),
        }
        return {
            "source_ad_id": str(source["ad_id"]),
            "target_campaign_id": str(campaign["campaign_id"]),
            "target_ad_group_id": str(group["ad_group_id"]),
            "creative_fields": fields,
            "image_paths": {
                role: list(paths) for role, paths in self.ad_local_images.items()
            },
        }

    def _set_ad_preview(self, value: dict[str, Any]) -> None:
        self.ad_preview.configure(state="normal")
        self.ad_preview.delete("1.0", "end")
        self.ad_preview.insert("1.0", json.dumps(value, ensure_ascii=False, indent=2))
        self.ad_preview.configure(state="disabled")

    @staticmethod
    def _ad_report_base(
        browser_type: str, profile: str, cid: str, mode: str,
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "mode": mode,
            "browser_type": browser_type,
            "profile_id": profile,
            "started_at": datetime.now().astimezone().isoformat(),
            "success": False,
            "accounts": [{
                "cid": cid,
                "account_name": "",
                "account_status": "",
                "actions": [],
            }],
        }

    def _ad_preflight(self) -> None:
        try:
            browser_type, profile, cid = self._ad_loaded_catalog_context()
            params = self._ad_parameters()
            catalog = self.ad_catalog
        except Exception as exc:
            messagebox.showerror("预检失败", str(exc))
            return

        def task():
            report = self._ad_report_base(browser_type, profile, cid, "ad_preflight")
            try:
                with AdManager(
                    profile, cid, lambda text: self.events.put(("log", text)),
                    browser_type=browser_type,
                ) as manager:
                    result = manager.preflight(catalog, **params)
                report.update(result)
                report["success"] = True
                report["accounts"][0]["actions"] = [{
                    "kind": "ad",
                    "status": "would_create",
                    "object_name": "响应式展示广告（暂停）",
                }]
                return result
            except Exception as exc:
                report["error"] = str(exc)
                raise
            finally:
                report["finished_at"] = datetime.now().astimezone().isoformat()
                save_run_report(report)

        def success(result):
            self._set_ad_preview(result)
            self._refresh_reports()
            messagebox.showinfo(
                "预检通过",
                "协议字段、目标父级和图片尺寸/比例已通过检查。"
                "此次只读预检没有上传图片，也没有创建广告。",
            )

        self._run_async("广告创建协议预检（只读）", task, success)

    def _ad_create(self) -> None:
        try:
            context = self._ad_loaded_catalog_context()
            browser_type, profile, cid = context
            params = self._ad_parameters()
            campaign = getattr(self, "ad_campaign_by_label", {}).get(self.ad_target_campaign.get()) or {}
            group = getattr(self, "ad_target_group_by_label", {}).get(self.ad_target_group.get()) or {}
        except Exception as exc:
            messagebox.showerror("不能创建", str(exc))
            return
        local_count = sum(len(paths) for paths in params.get("image_paths", {}).values())
        image_note = (
            f"将先上传 {local_count} 张本地图片，取得 Asset ID 后替换对应类别；"
            "其他类别沿用参考广告。"
            if local_count else "图片素材全部沿用参考广告。"
        )
        prompt = (
            f"将在 CID {format_cid(cid)} 创建 1 个响应式展示广告：\n\n"
            f"广告系列：{campaign.get('name')} ({campaign.get('campaign_id')})\n"
            f"广告组：{group.get('name')} ({group.get('ad_group_id')})\n"
            "初始状态：暂停（不会立即投放）\n\n"
            "执行前会重新读取该 CID 的 Billing 余额，只有余额可识别且大于 0 才创建。\n"
            f"{image_note}\n"
            "上传成功但后续创建失败时，已上传素材仍会保留在目标 CID 素材库。确定发送创建协议？"
        )
        if not messagebox.askyesno("确认创建暂停广告", prompt):
            return

        def task():
            report = self._ad_report_base(browser_type, profile, cid, "ad_create")
            try:
                with AdManager(
                    profile, cid, lambda text: self.events.put(("log", text)),
                    browser_type=browser_type,
                ) as manager:
                    fresh = manager.catalog()
                    result = manager.create_paused(fresh, **params)
                    refreshed = manager.catalog()
                report.update(result)
                report["success"] = True
                report["accounts"][0]["account_status"] = "可用"
                report["accounts"][0]["actions"] = [{
                    "kind": "ad",
                    "status": "created",
                    "object_name": str((result.get("created_ad") or {}).get("ad_id") or ""),
                    "reason": (
                        "已强制暂停并完成写后回读校验；创建前余额 "
                        f"{(result.get('target_balance') or {}).get('balance') or '—'}"
                    ),
                }]
                return result, refreshed
            except Exception as exc:
                report["error"] = str(exc)
                report["accounts"][0]["actions"] = [{
                    "kind": "ad", "status": "failed", "error": str(exc),
                }]
                raise
            finally:
                report["finished_at"] = datetime.now().astimezone().isoformat()
                save_run_report(report)

        def success(payload):
            result, refreshed = payload
            self._set_ad_preview(result)
            self.ad_catalog_context = context
            self._show_ad_catalog(refreshed)
            self._refresh_reports()
            created_id = str((result.get("created_ad") or {}).get("ad_id") or "")
            uploaded = result.get("uploaded_images") or []
            messagebox.showinfo(
                "广告创建成功",
                f"新广告 ID：{created_id}\n状态：已暂停\n"
                f"本次上传图片：{len(uploaded)} 张\n"
                "写后文案、父级和图片 Asset ID 已回读校验。",
            )

        self._run_async("创建响应式展示广告（强制暂停）", task, success)

    # ----- reports -----------------------------------------------------------------
    def _show_report_context_menu(self, event: tk.Event) -> None:
        iid = self._select_row_at_pointer(self.report_tree, event)
        menu = self.report_context_menu
        menu.delete(0, "end")
        menu.add_command(label="刷新运行记录", command=self._refresh_reports)
        if iid:
            menu.add_separator()
            menu.add_command(label="查看详情", command=self._show_report_details)
            menu.add_command(label="复制失败原因 / 结果", command=self._copy_report_error)
            menu.add_command(label="打开原始 JSON", command=self._open_report)
            menu.add_separator()
            menu.add_command(label="删除运行记录", command=self._delete_report)
        menu.add_command(label="打开报告目录", command=lambda: os.startfile(self._reports_dir()))
        self._popup_menu(menu, event)

    def _report_double_click(self, event: tk.Event) -> str | None:
        iid = self._select_row_at_pointer(self.report_tree, event)
        if not iid:
            return None
        self._show_report_details()
        return "break"

    def _reports_dir(self) -> Path:
        folder = data_dir() / "runs"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _refresh_reports(self) -> None:
        selected_path = self._selected_report_path() if self.report_tree.get_children() else None
        self.report_tree.delete(*self.report_tree.get_children())
        self.report_items.clear()
        selected_iid = ""
        for path in sorted(self._reports_dir().glob("run-*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            started = str(data.get("started_at") or "").replace("T", " ")[:19]
            mode = self._report_mode_label(str(data.get("mode") or ""), compact=True)
            success = self._report_result_label(data)
            reason = self._report_failure_reason(data)
            tag = "report_failure" if not data.get("success") else (
                "report_warning" if success == "完成（有跳过）" else "report_success"
            )
            iid = self.report_tree.insert(
                "",
                "end",
                values=(
                    started,
                    mode,
                    success,
                    self._report_profile_display(
                        str(data.get("profile_id") or ""),
                        browser_type=data.get("browser_type"),
                    ),
                    len(data.get("accounts") or []),
                    reason,
                    str(path),
                ),
                tags=(tag,),
            )
            self.report_items[iid] = (path, data)
            if selected_path and path == selected_path:
                selected_iid = iid
        children = self.report_tree.get_children()
        if not selected_iid and children:
            selected_iid = children[0]
        if selected_iid:
            self.report_tree.selection_set(selected_iid)
            self.report_tree.focus(selected_iid)
            self._show_report_details()
        else:
            self._set_report_detail("暂无运行记录。")

    def _report_profile_display(
        self, profile: str, *, browser_type: str | None = None,
    ) -> str:
        wanted_type = normalize_browser_type(browser_type)
        for iid in self.task_tree.get_children(""):
            row_type = normalize_browser_type(self.task_tree.set(iid, "browser_type"))
            if (
                str(self.task_tree.set(iid, "profile")) == profile
                and row_type == wanted_type
            ):
                return self._window_label(
                    profile,
                    str(self.task_tree.set(iid, "window_number")),
                    browser_type=row_type,
                    window_name=str(self.task_tree.set(iid, "window_name")),
                )
        return profile

    @staticmethod
    def _report_mode_label(mode: str, *, compact: bool = False) -> str:
        labels = {
            "preflight": "预检" if compact else "内容预检（只读）",
            "write": "写入" if compact else "内容正式写入",
            "ad_preflight": "广告预检" if compact else "广告创建预检（只读）",
            "ad_create": "创建广告" if compact else "创建暂停广告",
            "creative_asset_upload": "素材上传" if compact else "上传图片方案到 Google Ads 素材库",
            "entity_copy_preflight": "层级复制预检" if compact else "广告层级复制预检（只读）",
            "entity_copy": "层级复制" if compact else "复制广告层级并暂停",
            "placement_exclusion_preflight": "位置排除预检" if compact else "广告系列展示位置排除预检（只读）",
            "placement_exclusion": "位置排除" if compact else "从广告系列中排除展示位置",
            "warmup_preflight": "白量预检" if compact else "白量余额与系列预检（只读）",
            "warmup_execute": "一键白量" if compact else "复制并启用 App 白量",
        }
        return labels.get(mode, mode or "未知")

    @staticmethod
    def _report_failure_reason(data: dict[str, Any]) -> str:
        error = str(data.get("error") or "").strip()
        if error:
            return error
        if data.get("success"):
            return ""
        for account in data.get("accounts") or []:
            for action in account.get("actions") or []:
                reason = str(action.get("reason") or action.get("error") or "").strip()
                if reason:
                    return reason
        return "任务未完成，报告中没有记录具体错误"

    @staticmethod
    def _report_result_label(data: dict[str, Any]) -> str:
        if not data.get("success"):
            return "失败"
        has_skipped = any(
            str(action.get("result") or "") == "skipped"
            for account in data.get("accounts") or []
            for action in account.get("actions") or []
        )
        return "完成（有跳过）" if has_skipped else "成功"

    def _set_report_detail(self, text: str) -> None:
        self.report_detail.configure(state="normal")
        self.report_detail.delete("1.0", "end")
        self.report_detail.insert("1.0", text)
        self.report_detail.configure(state="disabled")

    def _selected_report(self) -> tuple[Path, dict[str, Any]] | None:
        selected = self.report_tree.selection()
        return self.report_items.get(selected[0]) if selected else None

    def _show_report_details(self) -> None:
        selected = self._selected_report()
        if not selected:
            self._set_report_detail("请选择一条运行记录。")
            return
        path, data = selected
        mode = self._report_mode_label(str(data.get("mode") or ""))
        result = self._report_result_label(data)
        lines = [
            f"结果：{result}",
            f"模式：{mode}",
            "指纹浏览器窗口："
            + self._report_profile_display(
                str(data.get("profile_id") or "未记录"),
                browser_type=data.get("browser_type"),
            ),
            f"开始时间：{str(data.get('started_at') or '').replace('T', ' ')[:19]}",
            f"完成时间：{str(data.get('finished_at') or '').replace('T', ' ')[:19]}",
            f"报告文件：{path}",
        ]
        if data.get("source_profile_id"):
            lines.insert(
                3,
                "复制来源窗口：" + self._report_profile_display(
                    str(data["source_profile_id"]),
                    browser_type=data.get("source_browser_type"),
                ),
            )
        reason = self._report_failure_reason(data)
        if reason:
            lines.extend(("", f"失败原因：{reason}"))
        accounts = data.get("accounts") or []
        discovered = data.get("discovered_accounts") or []
        if discovered:
            lines.extend(("", f"本次发现账户：{len(discovered)} 个（含 MCC/CID）"))
        lines.extend(("", f"账户结果（{len(accounts)} 个）："))
        for account in accounts:
            cid = str(account.get("cid") or "未记录")
            name = str(account.get("account_name") or "未命名账户")
            status = str(account.get("account_status") or "状态未记录")
            try:
                cid = format_cid(cid)
            except ValueError:
                pass
            lines.append(f"  CID {cid} · {name} · {status}")
            status_detail = str(account.get("account_status_detail") or "").strip()
            if status_detail:
                lines.append(f"    状态说明：{status_detail}")
            actions = account.get("actions") or []
            if not actions:
                lines.append("    未产生对象处理结果")
            for action in actions:
                kind = str(action.get("kind") or "unknown")
                label = (
                    "账户" if kind == "account" else
                    "广告" if kind == "ad" else
                    "图片素材" if kind == "asset" else
                    TYPE_LABELS.get(kind, kind)
                )
                object_name = str(action.get("object_name") or kind)
                raw_status = str(action.get("status") or action.get("result") or "未记录")
                action_status = ACTION_STATUS_LABELS.get(raw_status, raw_status)
                desired = action.get("desired")
                added = action.get("added")
                summary = f"    {label} · 结果：{action_status}"
                if kind != "account":
                    summary = f"    {label} · Google Ads 名称：{object_name} · 结果：{action_status}"
                if desired is not None:
                    summary += f" · 目标 {desired} 项"
                if added is not None:
                    summary += f" · 新增 {added} 项"
                lines.append(summary)
                action_reason = str(action.get("reason") or action.get("error") or "").strip()
                if action_reason:
                    lines.append(f"      原因：{action_reason}")
        self._set_report_detail("\n".join(lines))

    def _copy_report_error(self) -> None:
        selected = self._selected_report()
        if not selected:
            messagebox.showinfo("请选择记录", "请先选择一条运行记录")
            return
        reason = self._report_failure_reason(selected[1]) or "该记录执行成功，没有失败原因"
        self.clipboard_clear()
        self.clipboard_append(reason)
        self._log(f"已复制运行记录原因：{reason}")

    def _selected_report_path(self) -> Path | None:
        selected = self._selected_report()
        return selected[0] if selected else None

    def _select_report_from_paths(self, paths: list[Path], *, prefer_failure: bool) -> None:
        wanted = {path.resolve() for path in paths}
        candidates = [
            (iid, path, data)
            for iid, (path, data) in self.report_items.items()
            if path.resolve() in wanted
        ]
        if prefer_failure:
            candidates.sort(key=lambda row: bool(row[2].get("success")))
        if not candidates:
            return
        iid = candidates[0][0]
        self.report_tree.selection_set(iid)
        self.report_tree.focus(iid)
        self.report_tree.see(iid)
        self._show_report_details()

    def _open_report(self) -> None:
        path = self._selected_report_path()
        if path and path.exists():
            os.startfile(path)

    def _delete_report(self) -> None:
        path = self._selected_report_path()
        if path and path.exists() and messagebox.askyesno("确认删除", f"删除本地运行记录 {path.name}？"):
            path.unlink()
            self._refresh_reports()

    def destroy(self) -> None:
        executor = getattr(self, "_creative_thumbnail_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
            self._creative_thumbnail_executor = None
        super().destroy()


def launch() -> None:
    GGAdsApp().mainloop()


def launch_smoke_test() -> None:
    GGAdsApp(smoke_test=True).mainloop()
