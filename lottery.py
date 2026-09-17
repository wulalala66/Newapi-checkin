#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
哈基米中转站专属抽奖模块

流程：获取绑定 key → 绑定 openid → 抽奖
绑定映射需要持久化到本地文件（lottery_bindings.json），因此只在本地运行有效。
"""

import json
import os
import random
import string
import time

_BINDINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'lottery_bindings.json')

# 已知绑定关系（openid → display_name）
_DEFAULT_BINDINGS = {
    "miniapp-local-1782926819491-56teo16l": "1558148381",
    "miniapp-local-1782926819491-5nteo16l": "810803860",
}

# 固定参数（接口文档明确：无需更改）
_BIND_CODE = "0c1CbnFa1jXHZL08pvFa174qAQ3CbnFo"
_DRAW_CODE = "0a1zrnFa1tLHZL0JR1Ga1oj0aG0zrnFf"


def _load_bindings() -> dict:
    """加载持久化的 openid → display_name 映射"""
    bindings = dict(_DEFAULT_BINDINGS)
    if os.path.isfile(_BINDINGS_FILE):
        with open(_BINDINGS_FILE, 'r', encoding='utf-8') as f:
            bindings.update(json.load(f))
    return bindings


def _save_bindings(bindings: dict):
    """持久化映射到本地文件"""
    with open(_BINDINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(bindings, f, indent=2, ensure_ascii=False)


def _generate_openid() -> str:
    """生成唯一 openid: miniapp-local-{timestamp}-{随机8位}"""
    ts = str(int(time.time() * 1000))
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"miniapp-local-{ts}-{suffix}"


def run_for_account(session, base_url: str, display_name: str) -> tuple:
    """
    为单个账号执行抽奖全流程

    步骤：
    1. GET /api/user/token → 获取绑定 key
    2. 检查已有映射，无则 POST /api/miniapp/bind
    3. POST /api/miniapp/lottery/draw

    Args:
        session: 带 session cookie 的 requests.Session
        base_url: 站点地址
        display_name: 账号的 display_name（即 username/login_username）

    Returns:
        (result_dict | None, error_msg | None)
    """
    # 1. 获取绑定 key
    base_url = base_url.rstrip('/')  # ponytail: account['url'] may have trailing /
    try:
        r = session.get(f"{base_url}/api/user/token", timeout=30)
        d = r.json()
        if not d.get("success"):
            return None, d.get("message", "获取绑定key失败")
        binding_key = d["data"]
    except Exception as e:
        return None, f"获取绑定key异常: {e}"

    # 2. 查找 / 创建绑定
    bindings = _load_bindings()
    openid = next((oid for oid, dn in bindings.items() if dn == display_name), None)

    if openid is None:
        openid = _generate_openid()
        try:
            r = session.post(f"{base_url}/api/miniapp/bind", json={
                "access_token": binding_key,
                "code": _BIND_CODE,
                "openid": openid,
            }, timeout=30)
            d = r.json()
            if not d.get("success"):
                return None, f"绑定失败: {d.get('message', '')}"
            bindings[openid] = display_name
            _save_bindings(bindings)
        except Exception as e:
            return None, f"绑定请求异常: {e}"

    # 3. 抽奖
    try:
        r = session.post(f"{base_url}/api/miniapp/lottery/draw", json={
            "code": _DRAW_CODE,
            "openid": openid,
            "ad_watched": False,
            "ad_provider": "free",
            "ad_proof": "",
            "ad_simulated": False,
        }, timeout=30)
        d = r.json()
        if d.get("success"):
            data = d["data"]
            return {
                "prize_name": data.get("prize_name"),
                "quota_awarded": data.get("quota_awarded"),
                "remaining_times": data.get("remaining_times", 0),
            }, None
        msg = d.get("message", "抽奖失败")
        if any(k in msg for k in _DONE_MATCHES):
            msg = _DONE_AD_MSG
        return None, msg
    except Exception as e:
        return None, f"抽奖请求异常: {e}"


_DONE_AD_MSG = "今日已抽奖完成"
_DONE_MATCHES = ["需要先完整观看广告"]


def run_gwent_for_account(session, base_url: str) -> tuple:
    """
    维云中转站专属翻卡接口

    与哈基米不同：不需要绑定流程，直接用 session 调 POST /api/gwent/draw

    Args:
        session: 带 session cookie 的 requests.Session
        base_url: 站点地址

    Returns:
        (result_dict | None, error_msg | None)
    """
    base_url = base_url.rstrip('/')
    try:
        r = session.post(f"{base_url}/api/gwent/draw", timeout=30)
        d = r.json()
    except Exception as e:
        return None, f"翻卡请求异常: {e}"

    if d.get("success"):
        data = d["data"]
        prize = data.get("prize", {})
        # 每次抽完读取剩余次数(charges_current + extra_draws_left),避免超抽触发风控
        try:
            charges_current = int(data.get("charges_current", 0) or 0)
        except (TypeError, ValueError):
            charges_current = 0
        try:
            extra_left = int(data.get("extra_draws_left", 0) or 0)
        except (TypeError, ValueError):
            extra_left = 0
        remaining = charges_current + extra_left
        return {
            "prize_name": prize.get("name", "?"),
            "quota_awarded": prize.get("quota", 0),
            "remaining_times": remaining,
        }, None

    msg = d.get("message", "翻卡失败")
    # ponytail: cooldown = today done, same treatment as "今日已抽奖完成"
    if "冷却" in msg or "cd" in msg.lower() or "already" in msg.lower():
        msg = "今日已抽奖完成"
    return None, msg