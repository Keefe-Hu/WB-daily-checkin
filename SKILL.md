---
name: WB-daily-checkin
description: 自动完成每日积分签到，并汇总本次获得、累计获得、当前余额、连续天数等信息。当用户要求“每日签到”“领取每日积分”“查积分余额”或需要把签到接入定时任务时使用。
---

# 每日积分签到

调用积分服务的签到接口，自动完成当日签到并汇总结果，输出人类可读的中文摘要和机器可解析的 JSON。

## 执行指令（重要）

当用户要求签到 / 领取积分 / 查询余额时，**必须用 Bash 直接运行脚本**，不要只描述流程：

```bash
python3 ~/.workbuddy/skills/WB-daily-checkin/scripts/wb_daily_checkin.py
```

脚本自带执行权限，也可：

```bash
~/.workbuddy/skills/WB-daily-checkin/scripts/wb_daily_checkin.py
```

## 凭据来源（两种，自动选择）

1. **零配置（推荐）**：本机已登录对应客户端时，脚本自动从本地登录文件读取令牌，无需任何参数。
2. **手动令牌**：未安装客户端 / 文件不存在时，用以下任一方式传入令牌即可运行：
   - 环境变量：`export WB_DAILY_CHECKIN_TOKEN="你的令牌"` 后运行脚本
   - 命令行参数：`python3 scripts/wb_daily_checkin.py --token "你的令牌"`

> 若两者都没有且本地文件也不存在，脚本会提示「未找到登录文件」并退出。

## 执行流程
1. 读取登录凭据（本地文件优先，其次参数 / 环境变量）
2. 查询当前签到状态
3. 未签到则执行签到，已签到则复用状态响应
4. 补查活动信息，拉取真实账户余额
5. 打印中文摘要，并以 `---` 分隔输出 JSON

## 输出说明
- 中文摘要：本次获得 / 累计获得 / 当前余额 / 连续天数 / 当前活动
- 结构化结果：`---` 之后为 JSON，字段含 `status` / `success` / `today_credit` / `total_credits` / `balance` / `streak_days` / `activity_name`

## 备注
- 跨平台：macOS / Windows / Linux 均可；Windows 下自动将标准输出切到 UTF-8。
- 登录凭据文件做了基本安全校验（拒绝符号链接、校验属主与权限）。
- 网络请求带简单重试；余额 / 活动查询失败不影响主签到流程。
