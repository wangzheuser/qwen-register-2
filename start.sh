#!/usr/bin/env bash

set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQUIREMENTS_PATH="$PROJECT_ROOT/requirements.txt"
VENV_PATH="$PROJECT_ROOT/.venv"
VENV_PYTHON="$VENV_PATH/bin/python"
PYTHON_INTERRUPT_GRACE_SECONDS=15

dry_run=false
skip_dependency_install=false
interrupt_cleanup_self_test=false
browser_mode=""
config_path=""

# 输出统一的启动日志前缀。
info() {
    printf '[start] %s\n' "$*"
}

# 显示命令行帮助。
usage() {
    cat <<'EOF'
用法: bash start.sh [选项]

选项:
  --mode playwright|camoufox     跳过浏览器模式交互选择
  --dry-run                      只显示最终命令，不启动注册流程
  --skip-dependency-install      跳过虚拟环境及依赖安装检测
  --interrupt-cleanup-self-test  测试停止文件清理协议
  --config-path PATH             指定配置文件路径
  -h, --help                     显示帮助
EOF
}

# 解析启动脚本参数。
parse_options() {
    while (($#)); do
        case "$1" in
            --mode)
                [[ $# -ge 2 ]] || { printf '缺少 --mode 参数\n' >&2; exit 2; }
                browser_mode="$2"
                shift 2
                ;;
            --dry-run)
                dry_run=true
                shift
                ;;
            --skip-dependency-install)
                skip_dependency_install=true
                shift
                ;;
            --interrupt-cleanup-self-test)
                interrupt_cleanup_self_test=true
                shift
                ;;
            --config-path)
                [[ $# -ge 2 ]] || { printf '缺少 --config-path 参数\n' >&2; exit 2; }
                config_path="$2"
                shift 2
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                printf '未知参数: %s\n' "$1" >&2
                usage >&2
                exit 2
                ;;
        esac
    done
}

# 选择 Playwright 或 Camoufox 启动模式。
select_browser_mode() {
    case "${browser_mode,,}" in
        playwright|chromium|1) browser_mode="playwright" ;;
        camoufox|2) browser_mode="camoufox" ;;
        "")
            printf '浏览器模式:\n  1) Playwright Chromium\n  2) Camoufox\n'
            local raw
            read -r -p "请选择浏览器模式 [1]: " raw || raw=""
            case "${raw,,}" in
                ""|1|playwright|chromium) browser_mode="playwright" ;;
                2|camoufox) browser_mode="camoufox" ;;
                *) printf '请输入 1/2 或 playwright/camoufox。\n' >&2; exit 2 ;;
            esac
            ;;
        *)
            printf '不支持的浏览器模式: %s\n' "$browser_mode" >&2
            exit 2
            ;;
    esac
}

# 根据浏览器模式设置入口、配置和日志路径。
configure_mode() {
    if [[ "$browser_mode" == "camoufox" ]]; then
        entry_script="qwenv4_camoufox.py"
        default_config_name=".start-camoufox-config.json"
        log_file="$PROJECT_ROOT/logs/qwenv4-camoufox.log"
    else
        entry_script="qwenv4.py"
        default_config_name=".start-config.json"
        log_file="$PROJECT_ROOT/logs/qwenv4.log"
    fi
    script_path="$PROJECT_ROOT/$entry_script"
    if [[ -z "$config_path" ]]; then
        resolved_config_path="$PROJECT_ROOT/$default_config_name"
    elif [[ "$config_path" = /* ]]; then
        resolved_config_path="$config_path"
    else
        resolved_config_path="$PROJECT_ROOT/$config_path"
    fi
}

# 查找可用的 Python 3 解释器。
find_python() {
    if command -v python3 >/dev/null 2>&1; then
        command -v python3
    elif command -v python >/dev/null 2>&1; then
        command -v python
    else
        printf '未找到 Python 3。\n' >&2
        return 1
    fi
}

# 创建并返回项目虚拟环境解释器。
ensure_venv_python() {
    local launcher
    launcher="$(find_python)" || exit 1
    if $skip_dependency_install; then
        python_exe="$launcher"
        return
    fi
    if [[ ! -x "$VENV_PYTHON" ]]; then
        info "创建本地虚拟环境: $VENV_PATH"
        "$launcher" -m venv "$VENV_PATH" || { printf '创建虚拟环境失败\n' >&2; exit 1; }
    fi
    python_exe="$VENV_PYTHON"
}

# 安装 requirements.txt 中尚未满足的依赖。
ensure_python_dependencies() {
    $skip_dependency_install && { info '已跳过依赖安装检测 (--skip-dependency-install)'; return; }
    [[ -f "$REQUIREMENTS_PATH" ]] || { printf '缺少 requirements.txt: %s\n' "$REQUIREMENTS_PATH" >&2; exit 1; }
    local modules=(httpx bs4 lxml portalocker playwright ddddocr)
    [[ "$browser_mode" == "camoufox" ]] && modules+=(camoufox)
    if ! "$python_exe" - "${modules[@]}" <<'PY'
import importlib.util
import importlib.metadata
import sys
missing = [name for name in sys.argv[1:] if importlib.util.find_spec(name) is None]
if "camoufox" in sys.argv[1:] and "playwright" not in missing:
    version = importlib.metadata.version("playwright")
    if tuple(int(part) for part in version.split(".")[:2]) >= (1, 60):
        missing.append(f"playwright {version}（需要 <1.60.0）")
if missing:
    print(",".join(missing))
    raise SystemExit(1)
PY
    then
        info '检测到缺失或版本不兼容的依赖，安装 requirements.txt'
        "$python_exe" -m pip install -r "$REQUIREMENTS_PATH" || { printf '安装 Python 依赖失败\n' >&2; exit 1; }
    else
        info 'Python 依赖已满足'
    fi
}

# 检查并安装所选浏览器运行时。
ensure_browser_runtime() {
    $skip_dependency_install && return
    if [[ "$browser_mode" == "camoufox" ]]; then
        if ! "$python_exe" - <<'PY'
from camoufox.pkgman import installed_verstr
try:
    installed_verstr()
except FileNotFoundError:
    raise SystemExit(1)
PY
        then
            info '首次安装需下载约 298 MiB 的 Camoufox 浏览器'
            local fetch_proxy="http://127.0.0.1:7890"
            local github_token="${GITHUB_TOKEN:-}"
            if [[ -z "$github_token" ]] && command -v gh >/dev/null 2>&1; then
                github_token="$(gh auth token 2>/dev/null || true)"
            fi
            info "通过代理下载 Camoufox 浏览器: $fetch_proxy"
            GITHUB_TOKEN="$github_token" HTTPS_PROXY="$fetch_proxy" HTTP_PROXY="$fetch_proxy" RICH_FORCE_TERMINAL=1 \
                "$python_exe" -m camoufox fetch || { printf '安装 Camoufox 浏览器失败\n' >&2; exit 1; }
        else
            info 'Camoufox 浏览器已可用'
        fi
    elif ! "$python_exe" - <<'PY'
from pathlib import Path
from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    raise SystemExit(0 if Path(playwright.chromium.executable_path).exists() else 1)
PY
    then
        info '安装 Playwright Chromium'
        "$python_exe" -m playwright install chromium || { printf '安装 Playwright Chromium 失败\n' >&2; exit 1; }
    else
        info 'Playwright Chromium 已可用'
    fi
}

# 读取配置；异常时保留内置默认值。
load_config() {
    count=1
    email_provider="mailtm"
    api_proxy=""
    browser_proxy=""
    concurrency=1
    captcha_timeout=600
    captcha_solver="ddddocr"
    verbose=false
    sync_qwen2api=false
    qwen2api_base_url="http://127.0.0.1:7860"
    qwen2api_admin_key="admin"
    qwen2api_timeout=30
    strict=true
    [[ -f "$resolved_config_path" ]] || return
    local assignments
    if ! assignments="$("$python_exe" - "$resolved_config_path" <<'PY'
import json
import shlex
import sys

keys = {
    "count", "email_provider", "api_proxy", "browser_proxy", "concurrency",
    "captcha_timeout", "captcha_solver", "verbose", "sync_qwen2api",
    "qwen2api_base_url", "qwen2api_admin_key", "qwen2api_timeout", "strict",
}
with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
for key in keys & data.keys():
    value = data[key]
    if isinstance(value, bool):
        value = str(value).lower()
    print(f"{key}={shlex.quote(str(value))}")
PY
    )"; then
        printf '⚠️  配置文件读取失败，将使用内置默认值。\n'
        return
    fi
    eval "$assignments"
}

# 循环读取正整数。
prompt_positive_int() {
    local label="$1" default="$2" raw
    while true; do
        read -r -p "$label [$default]: " raw || raw=""
        [[ -z "$raw" ]] && { printf '%s' "$default"; return; }
        [[ "$raw" =~ ^[1-9][0-9]*$ ]] && { printf '%s' "$raw"; return; }
        printf '请输入正整数。\n' >&2
    done
}

# 循环读取指定范围内的整数。
prompt_ranged_int() {
    local label="$1" default="$2" min="$3" max="$4" raw
    while true; do
        read -r -p "$label [$default]: " raw || raw=""
        [[ -z "$raw" ]] && { printf '%s' "$default"; return; }
        if [[ "$raw" =~ ^[0-9]+$ ]] && ((raw >= min && raw <= max)); then
            printf '%s' "$raw"
            return
        fi
        printf '请输入 %s 到 %s 之间的整数。\n' "$min" "$max" >&2
    done
}

# 规范化邮箱服务名称。
normalize_provider() {
    case "${1,,}" in
        1|generator|generator.email) printf 'generator.email' ;;
        2|mailtm|mail.tm) printf 'mailtm' ;;
        3|mailporary) printf 'mailporary' ;;
        4|gonebox) printf 'gonebox' ;;
        5|tempmail_lol|tempmail.lol) printf 'tempmail_lol' ;;
        6|freecustom|freecustom.email) printf 'freecustom' ;;
        *) return 1 ;;
    esac
}

# 交互选择邮箱服务。
prompt_provider() {
    local default raw normalized
    default="$(normalize_provider "$1" 2>/dev/null || printf 'generator.email')"
    while true; do
        printf '邮箱服务:\n  1) generator.email\n  2) mailtm\n  3) mailporary\n  4) gonebox\n  5) tempmail_lol\n  6) freecustom\n' >&2
        read -r -p "请选择邮箱服务 [$default]: " raw || raw=""
        [[ -z "$raw" ]] && { printf '%s' "$default"; return; }
        if normalized="$(normalize_provider "$raw")"; then
            printf '%s' "$normalized"
            return
        fi
        printf '请输入 1-6 或有效的邮箱服务名称。\n' >&2
    done
}

# 读取字符串，并支持显式清空。
prompt_string() {
    local label="$1" default="$2" raw
    read -r -p "$label [$default]: " raw || raw=""
    [[ -z "$raw" ]] && { printf '%s' "$default"; return; }
    case "${raw,,}" in none|clear|null) printf '' ;; *) printf '%s' "$raw" ;; esac
}

# 交互读取布尔值。
prompt_bool() {
    local label="$1" default="$2" default_text="n" raw
    [[ "$default" == "true" ]] && default_text="Y"
    while true; do
        read -r -p "$label [$default_text]: " raw || raw=""
        [[ -z "$raw" ]] && { printf '%s' "$default"; return; }
        case "${raw,,}" in
            y|yes|true|1) printf 'true'; return ;;
            n|no|false|0) printf 'false'; return ;;
            *) printf '请输入 y/n。\n' >&2 ;;
        esac
    done
}

# 交互选择验证码处理方式。
prompt_captcha_solver() {
    local default="$1" raw
    case "${default,,}" in ddddocr|ai|manual) ;; *) default="ddddocr" ;; esac
    while true; do
        printf '滑块处理方式:\n  1) ddddocr（本地自动，默认）\n  2) 滑块AI\n  3) 人工\n' >&2
        read -r -p "请选择滑块处理方式 [$default]: " raw || raw=""
        [[ -z "$raw" ]] && { printf '%s' "$default"; return; }
        case "${raw,,}" in
            1|ddddocr|ocr) printf 'ddddocr'; return ;;
            2|ai|滑块ai) printf 'ai'; return ;;
            3|manual|人工) printf 'manual'; return ;;
            *) printf '请输入 1/2/3 或 ddddocr/ai/manual。\n' >&2 ;;
        esac
    done
}

# 收集本次运行参数。
prompt_run_config() {
    count="$(prompt_positive_int '账号数量' "$count")"
    email_provider="$(prompt_provider "$email_provider")"
    api_proxy="$(prompt_string 'API 代理（输入 none 可清空）' "$api_proxy")"
    browser_proxy="$(prompt_string '浏览器代理（输入 none 可清空）' "$browser_proxy")"
    verbose="$(prompt_bool '启用详细日志?' "$verbose")"
    concurrency="$(prompt_ranged_int '并发数量' "$concurrency" 1 10)"
    captcha_solver="$(prompt_captcha_solver "$captcha_solver")"
    if [[ "$captcha_solver" == "ai" || "$captcha_solver" == "manual" ]]; then
        captcha_timeout="$(prompt_positive_int '滑块验证等待/超时秒数' "$captcha_timeout")"
    fi
    sync_qwen2api="$(prompt_bool '同步到 qwen2API?' "$sync_qwen2api")"
    if [[ "$sync_qwen2api" == "true" ]]; then
        qwen2api_base_url="$(prompt_string 'qwen2API 地址' "$qwen2api_base_url")"
        qwen2api_admin_key="$(prompt_string 'qwen2API 管理密钥' "$qwen2api_admin_key")"
        qwen2api_timeout="$(prompt_positive_int 'qwen2API 同步超时秒数' "$qwen2api_timeout")"
    fi
    strict="$(prompt_bool '严格模式?' "$strict")"
    if [[ "$strict" != "true" ]]; then
        printf '⚠️  %s 当前不支持关闭严格模式，将继续启用。\n' "$entry_script"
        strict=true
    fi
}

# 将本次参数保存为与 PowerShell 启动器兼容的 JSON。
save_config() {
    CONFIG_COUNT="$count" CONFIG_EMAIL_PROVIDER="$email_provider" \
    CONFIG_API_PROXY="$api_proxy" CONFIG_BROWSER_PROXY="$browser_proxy" \
    CONFIG_CONCURRENCY="$concurrency" CONFIG_CAPTCHA_TIMEOUT="$captcha_timeout" \
    CONFIG_CAPTCHA_SOLVER="$captcha_solver" CONFIG_VERBOSE="$verbose" \
    CONFIG_SYNC_QWEN2API="$sync_qwen2api" CONFIG_QWEN2API_BASE_URL="$qwen2api_base_url" \
    CONFIG_QWEN2API_ADMIN_KEY="$qwen2api_admin_key" CONFIG_QWEN2API_TIMEOUT="$qwen2api_timeout" \
    "$python_exe" - "$resolved_config_path" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
data = {
    "count": int(os.environ["CONFIG_COUNT"]),
    "email_provider": os.environ["CONFIG_EMAIL_PROVIDER"],
    "api_proxy": os.environ["CONFIG_API_PROXY"],
    "browser_proxy": os.environ["CONFIG_BROWSER_PROXY"],
    "concurrency": int(os.environ["CONFIG_CONCURRENCY"]),
    "captcha_timeout": int(os.environ["CONFIG_CAPTCHA_TIMEOUT"]),
    "captcha_solver": os.environ["CONFIG_CAPTCHA_SOLVER"],
    "verbose": os.environ["CONFIG_VERBOSE"] == "true",
    "sync_qwen2api": os.environ["CONFIG_SYNC_QWEN2API"] == "true",
    "qwen2api_base_url": os.environ["CONFIG_QWEN2API_BASE_URL"],
    "qwen2api_admin_key": os.environ["CONFIG_QWEN2API_ADMIN_KEY"],
    "qwen2api_timeout": int(os.environ["CONFIG_QWEN2API_TIMEOUT"]),
    "strict": True,
}
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

# 构造与所选 Python 入口匹配的参数数组。
build_script_args() {
    script_args=("$script_path" "$count" --email-provider "$email_provider")
    [[ -n "$api_proxy" ]] && script_args+=(--api-proxy "$api_proxy")
    [[ -n "$browser_proxy" ]] && script_args+=(--browser-proxy "$browser_proxy")
    script_args+=(--concurrency "$concurrency" --account-retries 3)
    if [[ "$captcha_solver" == "ddddocr" ]]; then
        script_args+=(--captcha-solver ddddocr --captcha-ai-attempts 3 --no-captcha-ai-fallback-manual --captcha-drag-backend playwright --captcha-drag-strategy fast_quadratic)
    elif [[ "$captcha_solver" == "ai" ]]; then
        [[ -n "${CAPTCHA_AI_API_KEY:-}" ]] || { printf '选择滑块AI模式需要先设置环境变量 CAPTCHA_AI_API_KEY\n' >&2; exit 1; }
        script_args+=(--captcha-solver ai --captcha-timeout "$captcha_timeout" --captcha-ai-attempts 3 --no-captcha-ai-fallback-manual --captcha-drag-backend playwright --captcha-drag-strategy fast_quadratic)
    else
        script_args+=(--captcha-solver manual --captcha-timeout "$captcha_timeout")
    fi
    if [[ "$sync_qwen2api" == "true" ]]; then
        script_args+=(--sync-qwen2api --qwen2api-base-url "$qwen2api_base_url" --qwen2api-admin-key "$qwen2api_admin_key" --qwen2api-timeout "$qwen2api_timeout")
    fi
    [[ "$verbose" == "true" ]] && script_args+=(--verbose)
    script_args+=(--log-file "$log_file" --strict)
}

# Camoufox 启动前验证浏览器代理，避免故障代理拖垮整批任务。
check_browser_proxy() {
    [[ "$browser_mode" == "camoufox" && -n "$browser_proxy" ]] || return
    command -v curl >/dev/null 2>&1 || return

    local probe_proxy
    for _ in 1 2 3; do
        probe_proxy="${browser_proxy//\{uuid\}/$("$python_exe" -c 'import uuid; print(uuid.uuid4().hex)')}"
        curl --silent --output /dev/null --max-time 8 --proxy "$probe_proxy" "https://chat.qwen.ai/" && return
    done
    printf '浏览器代理连续 3 次检测失败，停止启动，避免批量注册全部失败。\n' >&2
    exit 1
}

# 递归终止指定进程及其子进程。
stop_process_tree() {
    local pid="$1" child
    while read -r child; do
        [[ -n "$child" ]] && stop_process_tree "$child"
    done < <(pgrep -P "$pid" 2>/dev/null || true)
    kill -TERM "$pid" 2>/dev/null || true
    sleep 0.1
    kill -KILL "$pid" 2>/dev/null || true
}

# 启动 Python，并通过停止文件完成 Ctrl+C 清理。
run_python() {
    local temp_dir stop_file child_pid cancel_requested=false deadline exit_code
    temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/qwen-register.XXXXXX")" || exit 1
    stop_file="$temp_dir/stop.signal"

    on_interrupt() {
        cancel_requested=true
        printf 'stop' > "$stop_file"
        printf '\n'
        info "收到 Ctrl+C，已通知 Python 停止；等待 Python 停止新任务并关闭当前浏览器..."
    }
    trap on_interrupt INT TERM

    PYTHONUNBUFFERED=1 QWEN_REGISTER_MANAGED_BY_START=1 QWEN_REGISTER_STOP_FILE="$stop_file" \
        "$python_exe" "${script_args[@]}" &
    child_pid=$!
    while kill -0 "$child_pid" 2>/dev/null; do
        if [[ "$cancel_requested" == "true" ]]; then
            deadline=$((SECONDS + PYTHON_INTERRUPT_GRACE_SECONDS))
            while kill -0 "$child_pid" 2>/dev/null && ((SECONDS < deadline)); do sleep 0.2; done
            if kill -0 "$child_pid" 2>/dev/null; then
                info "Python 未在 ${PYTHON_INTERRUPT_GRACE_SECONDS}s 内退出，强制终止进程树..."
                stop_process_tree "$child_pid"
            else
                info 'Python 已完成中断清理。'
            fi
            break
        fi
        sleep 0.2
    done
    wait "$child_pid"
    exit_code=$?
    trap - INT TERM
    rm -rf "$temp_dir"
    return "$exit_code"
}

# 验证停止文件能够通知 Python 子进程干净退出。
run_interrupt_cleanup_self_test() {
    local temp_dir stop_file marker child_pid
    temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/qwen-start-selftest.XXXXXX")" || return 1
    stop_file="$temp_dir/stop.signal"
    marker="$temp_dir/graceful.txt"
    "$python_exe" - "$stop_file" "$marker" <<'PY' &
import os
import sys
import time
while not os.path.exists(sys.argv[1]):
    time.sleep(0.05)
open(sys.argv[2], "w", encoding="utf-8").write("ok")
PY
    child_pid=$!
    sleep 0.1
    printf 'stop' > "$stop_file"
    wait "$child_pid"
    if [[ -f "$marker" ]]; then
        info 'Python 已完成中断清理'
        rm -rf "$temp_dir"
        return 0
    fi
    rm -rf "$temp_dir"
    return 1
}

# 输出可复制执行的最终命令。
display_command() {
    printf '最终命令:'
    printf ' %q' "$python_exe" "${script_args[@]}"
    printf '\n'
}

# 组织完整启动流程。
main() {
    parse_options "$@"
    select_browser_mode
    configure_mode
    [[ -f "$script_path" ]] || { printf '缺少 %s: %s\n' "$entry_script" "$script_path" >&2; exit 1; }
    ensure_venv_python
    if $interrupt_cleanup_self_test; then
        run_interrupt_cleanup_self_test
        exit $?
    fi
    ensure_python_dependencies
    load_config
    ensure_browser_runtime
    prompt_run_config
    save_config || exit 1
    info "已保存本次参数: $resolved_config_path"
    $dry_run || check_browser_proxy
    build_script_args
    display_command
    if $dry_run; then
        info "试运行模式已启用，不启动 $entry_script"
        exit 0
    fi
    cd "$PROJECT_ROOT" || exit 1
    run_python
}

main "$@"
