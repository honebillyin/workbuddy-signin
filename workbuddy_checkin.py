#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy（腾讯 AI 编程助手）每日自动签到脚本
==============================================
功能：
  - 读取环境变量 WORKBUDDY，支持多账号自动签到
  - 幂等：今日已签到则跳过，重复运行无副作用

环境变量：
  WORKBUDDY          必填。格式：ACCESS_TOKEN#UID#备注
                     多账号用换行分隔
  WORKBUDDY_ENDPOINT 可选。默认 https://copilot.tencent.com
  WORKBUDDY_TOKEN_WARN_DAYS 可选。Token 过期预警天数，默认 2

接口说明（官方 copilot.tencent.com，仅携带用户自己的 Bearer Token）：
  POST /v2/billing/meter/checkin-activity-status  查询今日签到状态/积分/连续天数
  POST /v2/billing/meter/daily-checkin            执行签到领取积分
"""
import os
import re
import sys
import json
import base64
import time
import urllib.request
import urllib.error

ENDPOINT = os.environ.get("WORKBUDDY_ENDPOINT", "https://copilot.tencent.com").rstrip("/")
WARN_DAYS = int(os.environ.get("WORKBUDDY_TOKEN_WARN_DAYS", "2") or "2")


def get_accounts():
    """解析环境变量为 [(token, uid, remark), ...]"""
    accounts = []
    raw = os.environ.get("WORKBUDDY", "").strip()
    if raw:
        for line in re.split(r"[\r\n;；,]+", raw):
            line = line.strip()
            if not line:
                continue
            parts = line.split("#")
            token = parts[0].strip()
            uid = parts[1].strip() if len(parts) > 1 else ""
            remark = parts[2].strip() if len(parts) > 2 else (uid or token[:8])
            if token:
                accounts.append((token, uid, remark))

    if not accounts:
        single = os.environ.get("WB_ACCESS_TOKEN", "").strip()
        multi = os.environ.get("WB_ACCESS_TOKENS", "").strip()
        for item in filter(None, [single, multi]):
            for part in item.split(","):
                part = part.strip()
                if not part:
                    continue
                if ":" in part:
                    uid, token = part.split(":", 1)
                else:
                    token, uid = part, ""
                if token:
                    accounts.append((token.strip(), uid.strip(), token[:8]))
    return accounts


def decode_jwt_uid(token):
    """从 JWT payload 中解析 sub 作为 uid"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return str(data.get("sub", ""))
    except Exception:
        return ""


def jwt_expire_days(token):
    """计算 Token 剩余有效天数，解析失败返回 None"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        if not exp:
            return None
        return (int(exp) - time.time()) / 86400.0
    except Exception:
        return None


def http_request(path, token, uid, body=None):
    url = ENDPOINT + path
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": "Bearer " + token,
        "User-Agent": "Mozilla/5.0 (WorkBuddy-signin)",
    }
    if uid:
        headers["X-User-Id"] = uid
        headers["X-Domain"] = "www.codebuddy.cn"
    data = json.dumps(body if body is not None else {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "ignore")
        except Exception:
            raw = ""
        return {"code": e.code, "msg": "HTTP %d" % e.code, "raw": raw}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def checkin_one(token, uid, remark):
    """单个账号签到，返回多行结果字符串"""
    lines = []
    lines.append("===== 账号: %s =====" % remark)

    if not uid:
        uid = decode_jwt_uid(token)

    days_left = jwt_expire_days(token)
    if days_left is not None and days_left <= WARN_DAYS:
        lines.append("⚠️ Token 将在 %.1f 天后过期，请尽快更新" % max(days_left, 0))

    st = http_request("/v2/billing/meter/checkin-activity-status", token, uid)
    if st.get("code") not in (0, None):
        if st.get("code") in (401, 403) or "401" in str(st.get("raw", "")):
            lines.append("❌ 登录态失效（HTTP 401），请更新 Token")
        else:
            lines.append("❌ 状态查询失败: code=%s msg=%s" % (st.get("code"), st.get("msg")))
        return "\n".join(lines)

    d = st.get("data") or {}
    checked = bool(d.get("today_checked_in", False))
    streak = d.get("streak_days", 0)
    total = d.get("total_credits", 0)
    activity = d.get("activity_name") or d.get("theme_name") or "签到活动"
    lines.append("📅 %s | 连续签到 %s 天 | 本期累计 %s 积分" % (activity, streak, total))

    if checked:
        lines.append("✅ 今日已签到（连续 %s 天，本期累计 %s 积分），无需重复" % (streak, total))
        return "\n".join(lines)

    res = http_request("/v2/billing/meter/daily-checkin", token, uid)
    if res.get("code") == 0:
        rd = res.get("data") or {}
        credit = rd.get("credit", 0)
        new_streak = rd.get("streak_days", streak)
        lines.append("🎉 签到成功！本次获得 %s 积分，连续签到 %s 天" % (credit, new_streak))
        st2 = http_request("/v2/billing/meter/checkin-activity-status", token, uid)
        if st2.get("code") == 0:
            total2 = (st2.get("data") or {}).get("total_credits", total)
            lines.append("💰 本期累计积分: %s" % total2)
        return "\n".join(lines)

    msg = str(res.get("msg", ""))
    if res.get("code") in (10001, 10002) or "已签" in msg or "已领取" in msg:
        lines.append("✅ 今日已签到过（%s）" % (msg or res.get("code")))
    else:
        lines.append("❌ 签到失败: code=%s msg=%s" % (res.get("code"), msg))
    return "\n".join(lines)


def main():
    accounts = get_accounts()
    if not accounts:
        print("❌ 未配置账号。请在 GitHub Secrets 中设置 WORKBUDDY")
        print("   格式：ACCESS_TOKEN#UID#备注")
        sys.exit(1)

    print("🦴 WorkBuddy 每日自动签到开始\n")
    outputs = []
    for token, uid, remark in accounts:
        try:
            outputs.append(checkin_one(token, uid, remark))
        except Exception as e:
            outputs.append("===== 账号: %s =====\n❌ 异常: %s" % (remark, e))
    print("\n\n".join(outputs))


if __name__ == "__main__":
    main()
