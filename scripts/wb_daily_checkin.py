#!/usr/bin/env python3
"""自动领取每日积分，并汇总本次 / 累计 / 余额。"""

import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

# 服务地址与各个业务接口
HOST = "https://www.codebuddy.cn"
SIGN_URL = f"{HOST}/v2/billing/meter/daily-checkin"
STATUS_URLS = (
    f"{HOST}/v2/billing/meter/checkin-activity-status",
    f"{HOST}/v2/billing/meter/checkin-status",
)
BALANCE_URL = f"{HOST}/v2/billing/meter/get-user-resource"

# 禁止自动跳转，避免凭证随重定向地址外泄
_NO_REDIRECT = HTTPRedirectHandler()
_NO_REDIRECT.redirect_request = lambda *a, **k: None


# 按当前系统找本地登录凭据文件，返回多个候选路径
def _login_path(home=None, platform=sys.platform):
    home = Path.home() if home is None else Path(home)
    if platform == "darwin":
        bases = [home / "Library/Application Support"]
    elif platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        roaming = os.environ.get("APPDATA")
        bases = [Path(p) for p in (local, roaming) if p]
        bases += [home / "AppData" / "Local", home / "AppData" / "Roaming"]
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        bases = [Path(xdg)] if xdg else [home / ".local" / "share"]
    return [b / "CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info" for b in bases]


# 读取并校验凭据文件内容
def _read_token(path):
    # 拒绝符号链接，防止指向伪造文件
    if path.is_symlink():
        raise ValueError("登录文件不能是符号链接")
    try:
        info = path.stat()
    except FileNotFoundError:
        raise ValueError(f"未找到登录文件：{path}")
    # 必须是存在的普通文件
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("登录路径不是普通文件")
    # 仅允许属主本人持有（类 Unix 系统）
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("登录文件属于其他用户")
    # 其他用户不可写（类 Unix 系统）
    if os.name != "nt" and info.st_mode & 0o022:
        raise ValueError("登录文件权限过于开放")
    # 解析 JSON，要求包含 auth 字段
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("登录文件不是有效 JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("auth"), dict):
        raise ValueError("登录文件缺少 auth 字段")
    return data


# 整理出本次请求需要的身份字段
def _credentials(token=None):
    # 直接传入令牌时跳过本地文件读取，便于未安装客户端的用户使用
    if token:
        return {"token": token.strip(), "uid": "", "org": "", "domain": ""}
    data = None
    for path in _login_path():
        if path.is_symlink():
            continue
        # 已退出登录的标记文件存在时直接提示
        if Path(f"{path}.logged-out").exists():
            raise ValueError("已退出登录，请先在客户端登录")
        if not path.exists():
            continue
        try:
            data = _read_token(path)
        except ValueError:
            continue
        break
    if data is None:
        tried = " | ".join(str(p) for p in _login_path())
        raise ValueError(f"未找到登录文件，已尝试：{tried}")
    auth = data.get("auth") if isinstance(data.get("auth"), dict) else {}
    # 兼容 token 内携带 uid 的写法（uid+token）
    raw = (auth.get("accessToken") or auth.get("access_token") or "").strip()
    if "+" in raw:
        uid_part, raw = (p.strip() for p in raw.split("+", 1))
    else:
        uid_part = ""
    if not raw:
        raise ValueError("登录文件缺少 access token")
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    return {
        "token": raw,
        "uid": str(account.get("uid") or account.get("id") or uid_part).strip(),
        "org": str(account.get("enterpriseId") or account.get("enterprise_id") or "").strip(),
        "domain": str(auth.get("domain") or data.get("domain") or "").strip(),
    }


# 组装请求头，按需带上组织 / 租户信息
def _headers(cred):
    head = {
        "Authorization": f"Bearer {cred['token']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    for key, names in (
        ("uid", ("X-User-Id",)),
        ("org", ("X-Enterprise-Id", "X-Tenant-Id")),
        ("domain", ("X-Domain",)),
    ):
        if cred[key]:
            for name in names:
                head[name] = cred[key]
    return head


# 统一 POST 封装，带简单退避重试
def _post(url, headers, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            req = Request(url, data=b"{}", headers=headers, method="POST")
            with build_opener(_NO_REDIRECT).open(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            # HTTP 错误时尽量提取后端返回的可读信息
            try:
                body = json.loads(exc.read().decode("utf-8"))
                msg = body.get("message") if isinstance(body, dict) else None
            except (json.JSONDecodeError, UnicodeDecodeError):
                msg = None
            raise ValueError(f"HTTP {exc.code}：{msg or exc.reason}") from exc
        except (URLError, TimeoutError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(1 + attempt)
                continue
    raise ValueError(f"网络请求失败：{last}")


# 把各种写法（布尔 / 数字 / 字符串）归一为「是否已签到」
def _checked_flag(data):
    value = data.get("today_checked_in", data.get("todayCheckedIn"))
    if value is None:
        return None
    if value is True or value == 1 or (
        isinstance(value, str) and value.strip().lower() in {"true", "1"}
    ):
        return True
    if value is False or value == 0 or (
        isinstance(value, str) and value.strip().lower() in {"false", "0"}
    ):
        return False
    return None


# 查询当前签到状态，多个接口逐个兜底
def _probe_status(headers):
    for url in STATUS_URLS:
        try:
            body = _post(url, headers)
            payload = body.get("data")
            if body.get("code") == 0 and isinstance(payload, dict) \
                    and _checked_flag(payload) is not None:
                return payload
        except (URLError, TimeoutError, ValueError):
            continue
    return None


# 解析签到接口的返回结果
def _parse_sign(body):
    if body.get("code") != 0:
        raise ValueError(body.get("message") or body.get("msg") or "签到失败")
    data = body.get("data")
    if not isinstance(data, dict):
        raise ValueError("响应缺少 data 字段")
    return {
        "success": data.get("success", True),
        "message": data.get("message") or body.get("message") or "签到成功",
        "credit": data.get("credit", data.get("today_credit")),
        "streak": data.get("streak_days"),
        "balance": data.get("total_credits", data.get("totalCredits")),
        "reward": data.get("reward"),
    }


# 拉取真实账户余额
def _balance(headers):
    try:
        body = _post(BALANCE_URL, headers)
        resp = body.get("data", {}).get("Response", {}).get("Data")
        if isinstance(resp, dict):
            return resp.get("TotalDosage")
    except Exception:
        pass
    return None


def run():
    # 支持通过参数或环境变量直接传入令牌
    parser = argparse.ArgumentParser(description="每日积分签到")
    parser.add_argument("--token", help="直接传入访问令牌（可选，缺省从本地登录文件读取）")
    args = parser.parse_args()
    token = args.token or os.environ.get("WB_DAILY_CHECKIN_TOKEN")

    # Windows 下把标准输出切到 UTF-8，避免中文 / 表情乱码崩溃
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass

    headers = _headers(_credentials(token))

    # 先判断是否已签到
    status = _probe_status(headers)
    if status and _checked_flag(status) is True:
        # 已签过：直接复用状态响应，省一次请求
        result = {
            "status": "already",
            "success": True,
            "message": "今天已经签到过了",
            "credit": status.get("today_credit", status.get("todayCredit")),
            "streak": status.get("streak_days", status.get("streakDays")),
            "balance": status.get("total_credits", status.get("totalCredits")),
            "reward": None,
        }
        activity = status
    else:
        # 未签到：执行签到并解析返回
        result = _parse_sign(_post(SIGN_URL, headers))
        activity = None

    # 签到后补查一次活动信息，取最新积分数据
    if activity is None:
        try:
            body = _post(STATUS_URLS[0], headers)
            activity = body.get("data") if body.get("code") == 0 else None
        except Exception:
            activity = None
    activity = activity if isinstance(activity, dict) else {}

    balance = _balance(headers)

    ok = result.get("success")
    icon = "✅" if ok else "❌"
    label = "签到完成" if result.get("status") != "already" else "今天已签到过"
    today = activity.get("today_credit") or activity.get("daily_credit") \
        or result.get("credit") or "-"
    total = activity.get("total_credits") or "-"
    bal = balance or "-"
    streak = result.get("streak") or "-"
    act = activity.get("activity_name", "") or "-"

    # 中文摘要
    print(f"{icon} {label}")
    print(f"• 本次获得：{today} 积分")
    print(f"• 累计获得：{total} 积分")
    print(f"• 当前余额：{bal} 积分")
    print(f"• 连续天数：{streak} 天")
    print(f"• 当前活动：{act}")
    # 结构化结果供程序读取
    print("---")
    print(json.dumps({
        "status": result.get("status"),
        "success": ok,
        "today_credit": today,
        "total_credits": total,
        "balance": bal,
        "streak_days": streak,
        "activity_name": act,
    }, ensure_ascii=False))


if __name__ == "__main__":
    run()
