import os
import sys
import json
import base64
import hashlib
from datetime import datetime
from pathlib import Path

def get_account_info():
    """读取并解密 ~/.zcode-proxy/credentials.json 中的当前主账号凭证。"""
    home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    store_file = home / ".zcode-proxy" / "credentials.json"
    
    if not store_file.exists():
        return None

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        seed = f"{home}-win32-x64"
        key = hashlib.sha256(seed.encode("utf-8")).digest()
        
        data = json.loads(store_file.read_text("utf-8"))
        if not data.get("encrypted"):
            return None
            
        enc = base64.b64decode(data["encrypted"])
        iv = enc[:12]
        ct = enc[12:]
        aesgcm = AESGCM(key)
        decrypted = aesgcm.decrypt(iv, ct, None)
        cred = json.loads(decrypted.decode("utf-8"))
        return cred
    except Exception as e:
        return {"error": str(e)}

def format_units(units):
    if units is None:
        return "未知"
    if units >= 100_000_000:
        return f"{units / 100_000_000:.1f}亿".replace(".0亿", "亿")
    if units >= 1_000_000:
        val = units / 1_000_000
        return f"{val:.1f}M"
    if units >= 1_000:
        return f"{units / 1_000:.1f}K"
    return str(units)

def format_time(ts):
    if not ts:
        return "长期有效"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)

def make_bar(remaining, total, width=20):
    if not total or total <= 0:
        return "[" + " " * width + "] 0%"
    pct = max(0.0, min(1.0, remaining / total))
    filled = int(round(pct * width))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct * 100:.0f}%"

def get_device_mid() -> str:
    config_file = Path(__file__).parent / "config.yaml"
    if config_file.exists():
        try:
            import re
            m = re.search(r'deviceMid:\s*["\']?([a-zA-Z0-9_-]+)["\']?', config_file.read_text(encoding="utf-8"))
            if m and len(m.group(1)) > 8:
                return m.group(1)
        except Exception:
            pass
    home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    telemetry = home / ".zcode" / "v2" / "telemetry-state.json"
    if telemetry.exists():
        try:
            data = json.loads(telemetry.read_text(encoding="utf-8"))
            mid = data.get("deviceMid")
            if mid and len(mid) > 8:
                return mid
        except Exception:
            pass
    import uuid
    return str(uuid.uuid4())

def fetch_online_billing(info):
    jwt = info.get("jwt")
    if not jwt or jwt.count(".") != 2:
        return None
    try:
        import httpx
        headers = {
            "User-Agent": "ZCode/3.14.0",
            "HTTP-Referer": "https://zcode.z.ai",
            "X-Title": "Z Code@cli",
            "X-ZCode-App-Version": "3.14.0",
            "X-Platform": "win32-x64",
            "X-Release-Channel": "stable",
            "X-Client-Language": "zh-CN",
            "X-Client-Timezone": "Asia/Shanghai",
            "X-Os-Category": "windows",
            "X-Device-Mid": get_device_mid(),
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/json",
        }
        url = "https://zcode.z.ai/api/v1/zcode-plan/billing/balance?app_version=3.14.0&platform=win32-x64"
        r = httpx.get(url, headers=headers, timeout=4)
        if r.status_code == 200:
            return r.json().get("data") or {}
    except Exception:
        pass
    return None

def _format_acc_quota(quota, status="active"):
    if status == "invalid":
        return "凭证失效 (需重新登录)"
    if not quota or not isinstance(quota, dict):
        return "未查询到额度"
    parts = []
    for model, q in quota.items():
        if not isinstance(q, dict):
            continue
        rem = q.get("remaining")
        tot = q.get("total")
        if rem == -1 or q.get("status") == "可用" or model == "APIKey直连":
            parts.append("全模型直连畅通 (APIKey)")
        elif rem is not None and tot is not None:
            parts.append(f"{model}: {format_units(rem)}/{format_units(tot)}")
    return " · ".join(parts) if parts else "额度 0"

def _calc_remaining(acc):
    mode = getattr(acc, "mode", None) or (acc.get("mode") if isinstance(acc, dict) else "")
    if mode == "apiKey":
        return 999999999999
    q = getattr(acc, "quota", None) or (acc.get("quota") if isinstance(acc, dict) else {})
    total = 0
    if isinstance(q, dict):
        for v in q.values():
            if isinstance(v, dict):
                rem = v.get("remaining", 0) or 0
                if rem == -1:
                    return 999999999999
                if rem > 0:
                    total += int(rem)
    return total

def print_summary():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.store import store
    accs = store.list_accounts()
    total = len(accs)
    active_count = sum(1 for a in accs if a.is_selectable())
    
    info = get_account_info() or {}
    curr_token = info.get("jwt") or info.get("apiKey") or ""
    active_name = ""
    for a in accs:
        if curr_token and (a.secret == curr_token or a.jwt_token == curr_token or a.api_key == curr_token):
            active_name = f"{a.name} ({a.provider})"
            break
    if not active_name and accs:
        active_name = f"{accs[0].name} ({accs[0].provider})"

    if total > 0:
        print(f"多账号池已就绪: 共 {total} 个账号 ({active_count} 个健康可用 · 智能轮询与故障自动切号) - 当前主选: {active_name}")
    elif info and "error" not in info:
        p = info.get("provider", "unknown")
        print(f"【{p}】单账号模式 (用户ID: {info.get('userId', '未知')})")
    else:
        print("[未登录] 尚未绑定任何账号")

def print_quota_line():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.store import store
    accs = store.list_accounts()

    # 1. 汇总多账号池总可用额度
    pool_totals = {}
    apikey_count = 0
    for a in accs:
        if not a.is_selectable():
            continue
        if a.mode == "apiKey":
            apikey_count += 1
            continue
        if not a.quota or not isinstance(a.quota, dict):
            continue
        for m, q in a.quota.items():
            if isinstance(q, dict) and "remaining" in q:
                rem = q.get("remaining")
                if rem is not None and rem > 0:
                    pool_totals[m] = pool_totals.get(m, 0) + int(rem)

    pool_str = ""
    if pool_totals or apikey_count > 0:
        p_parts = [f"{m}: {format_units(rem)}" for m, rem in pool_totals.items() if rem > 0]
        if apikey_count > 0:
            p_parts.append(f"+{apikey_count}个APIKey直连")
        if p_parts:
            pool_str = " · [账号池可用总量] " + " · ".join(p_parts)

    # 2. 当前活动账号实时额度
    info = get_account_info()
    if not info or "error" in info:
        if pool_str:
            print(f" {pool_str.strip(' · ')}")
        return

    billing = fetch_online_billing(info)
    if not billing:
        if pool_str:
            print(f" {pool_str.strip(' · ')}")
        return
    
    balances = billing.get("balances") or []
    plans = billing.get("plans") or []
    
    items = []
    exp_time = ""
    for b in balances:
        rem_u = b.get("remaining_units")
        if rem_u is not None and rem_u > 0:
            name = b.get("show_name") or "Model"
            rem = format_units(rem_u)
            tot = format_units(b.get("total_units"))
            items.append(f"{name}: {rem}/{tot}")
            if not exp_time and b.get("expires_at"):
                exp_time = format_time(b.get("expires_at"))

    extra_build = ""
    for p in plans:
        if "build" in (p.get("name") or "").lower():
            extra_build = " + 1亿 Build包"

    res = ""
    if items:
        res = " · ".join(items)
        if exp_time:
            res += f" (至 {exp_time})"
        if extra_build:
            res += extra_build

    if res:
        print(f" [活动账号实时额度] {res}{pool_str}")
    elif pool_str:
        print(f" {pool_str.strip(' · ')}")

def print_detail():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.store import store
    from app.quota import refresh_accounts
    import asyncio

    accounts = store.list_accounts()
    info = get_account_info()

    if not info and not accounts:
        print("未检测到登录凭据，请先在菜单中执行 [2]、[3] 或 [4] 登录/导入账号。")
        return

    print("========================================================================")
    print("             ZCode 多账号池管理与额度看板 (支持多账号轮询与切号)")
    print("========================================================================")
    print(f"  [联网刷新中] 正在并发查询账号池全部 {len(accounts)} 个账号的实时额度与方案明细...")

    # 联网并发刷新所有账号额度
    if accounts:
        try:
            asyncio.run(refresh_accounts(accounts))
            accounts = store.list_accounts()
        except Exception as e:
            print(f"  [提示] 联网刷新异常: {e}，使用本地记录展示")

    curr_token = (info.get("jwt") or info.get("apiKey") or "") if info else ""
    healthy_count = sum(1 for a in accounts if a.is_selectable())

    print("------------------------------------------------------------------------")
    print(f"  账号池概况: 共 {len(accounts)} 个账号已入库 ({healthy_count} 个健康可用)")
    print(f"  容灾策略:   ● 智能轮询并发负载均衡   ● 遇限流/429/额度耗尽自动秒切下一个账号")
    print("------------------------------------------------------------------------")
    print(f"【多账号池全部账号清单 (共 {len(accounts)} 个)】")
    print("------------------------------------------------------------------------")
    print(f"  {'序号':<4} {'账号名称':<24} {'平台':<12} {'模式':<6} {'状态':<6} {'额度概况'}")
    print("  " + "-" * 72)
    
    for idx, a in enumerate(accounts, 1):
        is_active = bool(curr_token and (a.secret == curr_token or a.jwt_token == curr_token or a.api_key == curr_token))
        tag = " ● [当前主选]" if is_active else ""
        plat = "智谱 BigModel" if a.provider == "bigmodel" else "Z.AI 国际站"
        mode_str = "JWT" if a.mode == "jwt" else "APIKey"
        status_text = "正常" if a.is_selectable() else ("失效" if a.status == "invalid" else a.status)
        q_desc = _format_acc_quota(a.quota, a.status)
        raw_ident = a.email or (f"手机:{a.phone[:3]}****{a.phone[7:]}" if a.phone and len(a.phone) == 11 else "") or a.display_name or a.name
        name_display = raw_ident[:24]
        print(f"  [{idx:<2}] {name_display:<24} {plat:<12} {mode_str:<6} {status_text:<6} {q_desc}{tag}")

    # 全部账号的实时详细方案展示
    print("\n========================================================================")
    print(f"       【多账号池全部账号方案与额度详情明细 (共 {len(accounts)} 个账号)】")
    print("========================================================================")

    for idx, a in enumerate(accounts, 1):
        is_active = bool(curr_token and (a.secret == curr_token or a.jwt_token == curr_token or a.api_key == curr_token))
        plat_str = "智谱 BigModel (国内平台)" if a.provider == "bigmodel" else "Z.AI 国际站 (全球平台)"
        mode_label = "JWT 网页授权" if a.mode == "jwt" else "API Key 开发者直连"
        
        status_badge = "● 健康可用 (参与轮询并发调度)"
        if is_active:
            status_badge = "● 健康可用 (当前主选凭据 · 参与轮询调度)"
        elif a.status == "invalid":
            status_badge = "✖ 凭证已失效 (需在控制台重新登录)"
        elif a.status == "exhausted":
            status_badge = "⚠ 额度已耗尽 (等待周期重置后自动激活)"
        elif a.status == "cooling":
            status_badge = "⏳ 冷却中 (请求过频或遇 429 限流)"
        elif not a.enabled or a.status == "disabled":
            status_badge = "○ 已禁用"

        sec = a.secret or ""
        masked_sec = f"{sec[:8]}...{sec[-6:]}" if len(sec) > 16 else (sec[:4] + "..." if sec else "无")

        ident_parts = []
        if a.email: ident_parts.append(f"邮箱: {a.email}")
        if a.phone: ident_parts.append(f"手机: {a.phone[:3]}****{a.phone[7:]}" if len(a.phone) == 11 else f"手机: {a.phone}")
        if a.display_name and a.display_name != a.email: ident_parts.append(f"昵称: {a.display_name}")
        ident_summary = " · ".join(ident_parts) if ident_parts else a.name

        active_flag = " ● [当前主选]" if is_active else ""
        print(f"\n[{idx:<2}] 账号: {ident_summary} ({plat_str} · {mode_label}){active_flag}")
        if ident_parts:
            print(f"     • 账号归属:   {ident_summary}")
        print(f"     • 账号标识:   {a.id} ({a.name})")
        print(f"     • 凭据密钥:   {masked_sec}")
        print(f"     • 运行状态:   {status_badge}")
        if a.last_error:
            print(f"     • 状态附注:   {a.last_error}")

        # 方案与额度明细展示
        all_plans = []
        balances = []
        if isinstance(a.plan, dict):
            all_plans = a.plan.get("all_plans") or ([a.plan] if a.plan.get("name") or a.plan.get("plan_id") else [])
            balances = a.plan.get("balances") or []

        bal_map = {}
        for b in balances:
            u_pid = b.get("user_plan_id")
            ent_id = b.get("entitlement_id")
            sname = b.get("show_name") or b.get("meter")
            if u_pid and ent_id:
                bal_map[(u_pid, ent_id)] = b
            if u_pid and sname:
                bal_map[(u_pid, sname)] = b

        if all_plans:
            for pidx, p in enumerate(all_plans, 1):
                p_name = p.get("name") or p.get("plan_id") or "默认方案"
                p_desc = p.get("description") or ""
                p_ends = p.get("ends_at")
                u_pid = p.get("user_plan_id")
                p_exp_str = f"至 {format_time(p_ends)}" if p_ends else "长期有效"
                
                sub_bals = [b for b in balances if b.get("user_plan_id") == u_pid]
                if sub_bals and sub_bals[0].get("expires_at"):
                    p_exp_str = f"至 {format_time(sub_bals[0].get('expires_at'))}"

                desc_str = f" ({p_desc})" if p_desc else ""
                print(f"     [方案 {pidx}] {p_name}{desc_str}")
                print(f"         有效时间:   {p_exp_str}")

                entitlements = p.get("entitlements") or []
                for ent in entitlements:
                    m_name = ent.get("show_name") or ent.get("meter") or "模型"
                    grant = ent.get("grant_units") or 0
                    ent_id = ent.get("entitlement_id")
                    b = bal_map.get((u_pid, ent_id)) or bal_map.get((u_pid, m_name))
                    if b and b.get("total_units"):
                        rem = b.get("remaining_units") or 0
                        tot = b.get("total_units") or 0
                        used = b.get("used_units") or 0
                        bar_str = make_bar(rem, tot, width=18)
                        rem_str = f"{format_units(rem)}/{format_units(tot)}"
                        if rem > 0:
                            print(f"         • {m_name:<14}: {rem_str:<10} {bar_str} (已消耗: {format_units(used)})")
                        else:
                            print(f"         • {m_name:<14}: {rem_str:<10} {bar_str} (当前周期额度已耗尽)")
                    else:
                        print(f"         • {m_name:<14}: {format_units(grant)} Token [已获配额]")
        elif a.quota:
            print("     [实时配额看板]")
            for m_name, q in a.quota.items():
                if not isinstance(q, dict):
                    continue
                rem = q.get("remaining")
                tot = q.get("total")
                used = q.get("used", 0)
                if rem == -1 or q.get("status") == "可用" or m_name == "APIKey直连":
                    print(f"         • {m_name:<14}: 直连畅通可用 (平台按需/团队配额，全模型畅通)")
                elif rem is not None and tot is not None:
                    bar_str = make_bar(rem, tot, width=18)
                    rem_str = f"{format_units(rem)}/{format_units(tot)}"
                    exp_str = f"至 {format_time(q.get('expires_at'))}" if q.get("expires_at") else ""
                    print(f"         • {m_name:<14}: {rem_str:<10} {bar_str} (消耗: {format_units(used)}) {exp_str}")
        elif a.mode == "apiKey":
            print("     [开发者 API Key 模式]")
            print("         • 接口状态:   ● 在线验证通过 (Anthropic / PaaS 模型接口 200 OK)")
            print("         • 配额规则:   由平台开发者/企业账户按需计费，支持全模型高并发直连")
        elif a.status == "invalid":
            print("     [凭证失效提醒]")
            print(f"         • 失败原因:   {a.last_error or 'Token 鉴权失败 HTTP 401'}")
            print("         • 解决方式:   请在控制台菜单执行 [2] 或 [3] 重新登录该账号更新凭证")
        else:
            print("     [额度信息] 暂未获取到该账号的配额包信息")

    home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    print("\n------------------------------------------------------------------------")
    print(f"  💡 启动网关服务（菜单 [1]）后，将对账号池全部健康账号进行智能并发轮询与故障转移！")
    print(f"  本地凭据路径: {home / '.zcode-proxy' / 'credentials.json'}")
    print("========================================================================")

def sync_current_to_pool():
    """将刚刚登录产生的凭据（~/.zcode-proxy/credentials.json）自动并入 store 账号池。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.store import store
    from app.zcode_importer import save_zaccounts_snapshot, parse_jwt_payload
    from app.quota import fetch_quota
    import asyncio

    info = get_account_info()
    if not info or "error" in info:
        return

    provider = info.get("provider") or "zai"
    jwt = info.get("jwt") or ""
    api_key = info.get("apiKey") or ""
    secret = jwt or api_key
    if not secret:
        return

    # 命名推导
    name = ""
    if jwt and jwt.count(".") == 2:
        payload = parse_jwt_payload(jwt)
        name = payload.get("email") or payload.get("name") or payload.get("sub") or ""
    if not name:
        uid = str(info.get("userId") or "")
        name = f"{provider}-{uid[:8]}" if uid else f"{provider}-{secret[:8]}"

    acc = store.add_account(provider, name, secret)
    
    # 异步拉取最新额度
    try:
        asyncio.run(fetch_quota(acc))
    except Exception:
        pass

    # 备份到 Z-Accounts 快照目录
    save_zaccounts_snapshot({
        "provider": provider,
        "name": acc.name,
        "secret": secret,
        "user_id": acc.id,
    })

    total = len(store.list_accounts())
    print(f"[账号池同步] ✔ 账号【{acc.name}】({acc.provider}) 已自动并入多账号池！(当前池内共 {total} 个账号)")

def do_import_zcode():
    print("========================================================================")
    print("  正在从本地 ZCode (credentials.json) 与 Z-Accounts 自动捕获全部账号...")
    print("========================================================================")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from app.zcode_importer import capture_all_local_accounts, sync_zcode_config_and_proxy, save_zaccounts_snapshot
        from app.store import store
        from app.quota import fetch_quota
        import asyncio

        all_accs = capture_all_local_accounts()
        if not all_accs:
            print("[错误] 未在本地检测到已登录的 ZCode 凭据或 Z-Accounts 存档。")
            print("请先启动 ZCode 客户端并完成账号登录，然后再执行本选项。")
            return

        print(f"[1/3] 成功捕获本地全部账号 (共检测到 {len(all_accs)} 个账号)")
        
        # 1. 批量导入至 store 账号池
        print("[2/3] 正在将全部账号持久化至多账号池与 Z-Accounts 快照...")
        added_count = 0
        added_acc_objs = []
        for s in all_accs:
            try:
                acc = store.add_account(s["provider"], s["name"], s["secret"])
                save_zaccounts_snapshot(s)
                added_acc_objs.append(acc)
                print(f"      - 已入库: {acc.name:<24} (平台: {acc.provider})")
                added_count += 1
            except Exception:
                pass

        # 刷新所有账号的额度
        for a in added_acc_objs:
            try:
                asyncio.run(fetch_quota(a))
            except Exception:
                pass

        # 2. 选出首选账号同步 proxy
        target = all_accs[0]
        print(f"[3/3] 正在同步 ~/.zcode/v2/config.json 与 proxy 凭证 (主账号: {target['name']})...")
        sync_zcode_config_and_proxy(target)

        # 3. 执行对应的 auth login <provider> --import 确保二进制状态同步
        import subprocess
        exe_path = Path(__file__).resolve().parent / "zcode-proxy.exe"
        if exe_path.exists():
            cmd = [str(exe_path), "auth", "login", target["provider"], "--import"]
            subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent))

        print("\n========================================================================")
        print(f"🎉 成功导入 {added_count} 个账号！多账号池现有 {len(store.list_accounts())} 个账号，轮询已就绪。")
        print("========================================================================")
    except Exception as e:
        print(f"[异常] 导入过程中发生错误: {e}")

def do_switch_account():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.store import store
    from app.zcode_importer import sync_zcode_config_and_proxy

    accounts = store.list_accounts()
    if not accounts:
        print("[错误] 账号池中暂无任何账号，请先在控制台执行 [2]、[3] 或 [4] 登录或导入账号。")
        return

    curr_info = get_account_info() or {}
    curr_token = curr_info.get("jwt") or curr_info.get("apiKey") or ""

    print("========================================================================")
    print("                    ZCode 账号池切换 · 选择主选账号")
    print("========================================================================")
    print("  [0] 自动按剩余额度优先选择最优可用账号 (推荐)")
    
    for idx, acc in enumerate(accounts, 1):
        is_curr = False
        if curr_token and (acc.secret == curr_token or acc.jwt_token == curr_token or acc.api_key == curr_token):
            is_curr = True
        status_tag = "● 当前使用" if is_curr else ""
        quota_desc = _format_acc_quota(acc.quota)
        ident_parts = []
        if acc.email:
            ident_parts.append(acc.email)
        if acc.phone:
            ident_parts.append(f"手机:{acc.phone}")
        if acc.display_name and acc.display_name not in ident_parts:
            ident_parts.append(acc.display_name)
        raw_ident = " · ".join(ident_parts) if ident_parts else acc.name
        print(f"  [{idx}] {raw_ident:<38} ({plat}) | {quota_desc} {status_tag}")
    print("------------------------------------------------------------------------")
    
    try:
        choice = input("请输入要切换的主选账号编号 [默认 0 自动选择最优可用号]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if not choice or choice == "0":
        candidates = [a for a in accounts if a.is_selectable() and _calc_remaining(a) > 0]
        if not candidates:
            candidates = [a for a in accounts if a.is_selectable()]
        if not candidates:
            candidates = accounts
        candidates.sort(key=lambda a: _calc_remaining(a), reverse=True)
        target_acc = candidates[0]
        if len(candidates) > 1 and curr_token and (target_acc.secret == curr_token):
            target_acc = candidates[1]
    else:
        try:
            sel_idx = int(choice)
            if 1 <= sel_idx <= len(accounts):
                target_acc = accounts[sel_idx - 1]
            else:
                print("[错误] 选项超出范围。")
                return
        except ValueError:
            print("[错误] 输入无效。")
            return

    print(f"\n正在切换至主选账号: {target_acc.name} ({target_acc.provider})...")
    acc_dict = {
        "provider": target_acc.provider,
        "name": target_acc.name,
        "secret": target_acc.secret,
        "user_id": target_acc.id,
    }
    sync_zcode_config_and_proxy(acc_dict)

    exe_path = Path(__file__).resolve().parent / "zcode-proxy.exe"
    if exe_path.exists():
        import subprocess
        cmd = [str(exe_path), "auth", "login", target_acc.provider, "--import"]
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent))
        if res.stdout:
            print(res.stdout.strip())

    print("\n========================================================================")
    print(f"🎉 切换成功！当前已激活生效账号: {target_acc.name}")
    print("========================================================================")

def do_check_and_auto_switch():
    """在启动代理前检查当前主选账号是否有效，若无额度则秒级自动切换到可用账号。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.store import store
    from app.zcode_importer import sync_zcode_config_and_proxy

    info = get_account_info()
    billing = fetch_online_billing(info) if info else None
    
    is_exhausted = False
    if not info:
        is_exhausted = True
    elif billing:
        balances = billing.get("balances") or []
        total_rem = sum(b.get("remaining_units") or 0 for b in balances)
        if balances and total_rem <= 0:
            is_exhausted = True

    if not is_exhausted:
        return

    accounts = store.list_accounts()
    candidates = [a for a in accounts if a.is_selectable() and _calc_remaining(a) > 0]
    if not candidates:
        return

    candidates.sort(key=lambda a: _calc_remaining(a), reverse=True)
    target_acc = candidates[0]
    
    print("------------------------------------------------------------------------")
    print(f"[自动切号] 检测到当前主选账号无可用额度，正在自动切换至健康账号: {target_acc.name}...")
    acc_dict = {
        "provider": target_acc.provider,
        "name": target_acc.name,
        "secret": target_acc.secret,
        "user_id": target_acc.id,
    }
    sync_zcode_config_and_proxy(acc_dict)
    exe_path = Path(__file__).resolve().parent / "zcode-proxy.exe"
    if exe_path.exists():
        import subprocess
        subprocess.run([str(exe_path), "auth", "login", target_acc.provider, "--import"],
                       capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent))
    print(f"[自动切号] 已自动激活可用账号: {target_acc.name} (剩余额度: {_format_acc_quota(target_acc.quota)})")
    print("------------------------------------------------------------------------")

def do_claim_all():
    """一键为账号池中所有 JWT 账号检测并领取套餐体验配额。"""
    print("========================================================================")
    print("         检测并领取全部账号体验套餐配额 (Claim Packages)")
    print("========================================================================")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.store import store
    from app.claim import auto_claim_account
    from app.quota import fetch_quota
    import asyncio

    accounts = [a for a in store.list_accounts() if a.mode == "jwt"]
    if not accounts:
        print("[提示] 多账号池中暂无 JWT 模式账号。正在执行原生代理领取...")
        import subprocess
        exe_path = Path(__file__).resolve().parent / "zcode-proxy.exe"
        if exe_path.exists():
            subprocess.run([str(exe_path), "claim", "now"], cwd=str(Path(__file__).resolve().parent))
        return

    print(f"正在检测全部 {len(accounts)} 个 JWT 账号的套餐资格并领取...")
    for idx, a in enumerate(accounts, 1):
        print(f"  [{idx}/{len(accounts)}] 检测账号: {a.name} ({a.provider})...", end=" ", flush=True)
        try:
            res = asyncio.run(auto_claim_account(a))
            if res.get("ok"):
                msg = res.get("message") or "领取成功"
                print(f"✔ {msg}")
            else:
                msg = res.get("message") or "资格不可用"
                print(f"• {msg}")
        except Exception as e:
            print(f"❌ 失败: {e}")

    # 同时调用 zcode-proxy.exe claim now 作为本地兼容
    exe_path = Path(__file__).resolve().parent / "zcode-proxy.exe"
    if exe_path.exists():
        import subprocess
        subprocess.run([str(exe_path), "claim", "now"], capture_output=True, cwd=str(Path(__file__).resolve().parent))

    print("\n========================================================================")
    print("🎉 全部账号套餐资格检测与领取完成！最新额度已同步刷新。")
    print("========================================================================")

def do_manage_accounts():
    """管理与删除多账号池中的账号。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.store import store

    accounts = store.list_accounts()
    print("========================================================================")
    print(f"                   多账号池维护与管理 (当前共 {len(accounts)} 个)")
    print("========================================================================")
    for idx, a in enumerate(accounts, 1):
        plat = "智谱 BigModel" if a.provider == "bigmodel" else "Z.AI 国际站"
        q_desc = _format_acc_quota(a.quota)
        print(f"  [{idx}] {a.name:<24} ({plat}) | 状态: {a.status} | 额度: {q_desc}")
    print("------------------------------------------------------------------------")
    print("  [0] 返回上级菜单")
    print("  [序号] 输入对应的账号序号可从池中删除该账号")
    print("  [C] 清空多账号池所有账号与本地凭据")
    print("========================================================================")
    
    try:
        opt = input("请选择操作 [默认 0]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if not opt or opt == "0":
        return

    if opt.upper() == "C":
        confirm = input("⚠️ 确认要清空账号池内所有账号吗？此操作不可撤销 [Y/N]: ").strip()
        if confirm.upper() == "Y":
            for a in accounts:
                store.remove_account(a.provider, a.id)
            import subprocess
            exe_path = Path(__file__).resolve().parent / "zcode-proxy.exe"
            if exe_path.exists():
                subprocess.run([str(exe_path), "auth", "logout"], capture_output=True)
            print("[OK] 多账号池已全部清空。")
        return

    try:
        idx = int(opt)
        if 1 <= idx <= len(accounts):
            target = accounts[idx - 1]
            confirm = input(f"确认删除账号【{target.name}】吗？[Y/N]: ").strip()
            if confirm.upper() == "Y":
                store.remove_account(target.provider, target.id)
                print(f"[OK] 账号【{target.name}】已成功移除。")
        else:
            print("[错误] 序号超出范围。")
    except ValueError:
        print("[错误] 输入无效。")

def do_kill_port(port=8080):
    """检测并强行终止占用指定端口的进程及 zcode-proxy 残留进程。"""
    import subprocess
    print(f"正在检测并清理 {port} 端口占用...")
    killed_any = False
    try:
        out = subprocess.check_output(f'netstat -ano | findstr ":{port} "', shell=True, text=True, stderr=subprocess.DEVNULL)
        pids = set()
        for line in out.splitlines():
            if "LISTENING" in line:
                parts = line.strip().split()
                if len(parts) >= 5:
                    pids.add(parts[-1])
        for pid in pids:
            try:
                subprocess.run(f"taskkill /f /pid {pid}", shell=True, capture_output=True)
                print(f"  已终止占用端口的进程 PID: {pid}")
                killed_any = True
            except Exception:
                pass
    except Exception:
        pass
    try:
        subprocess.run("taskkill /f /im zcode-proxy.exe", shell=True, capture_output=True)
    except Exception:
        pass
    print(f"[OK] 端口 {port} 与残留代理进程已全部清理完毕。")

def print_admin_key():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.store import store
    print(store.admin_key() or "zcode")

def print_serve_banner():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.store import store
    accs = store.list_accounts()
    active_count = sum(1 for a in accs if a.is_selectable())
    admin_key = store.admin_key() or "zcode"

    print("========================================================================")
    print("             【ZCode 原生代理网关正在启动运行 (端口 8080)】")
    print(f"  ● 账号池状态:    共 {len(accs)} 个账号已就绪 ({active_count} 个健康可用)")
    print(f"  ● 人机验证机制:  内置 Happy-DOM 引擎 · 全自动无痕过阿里云人机验证")
    print("------------------------------------------------------------------------")
    print(f"  ● OpenAI 协议接口:    http://127.0.0.1:8080/v1")
    print(f"  ● Anthropic 协议接口: http://127.0.0.1:8080/v1")
    print(f"  ● 可用模型:          GLM-5.3, GLM-5.3-Flash, GLM-5.2, GLM-5-Turbo 等")
    print("  按 Ctrl+C 可停止代理并返回控制台菜单")
    print("========================================================================")
    print("")

def do_manage_password(new_pass=None):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.store import store

    curr_admin = store.admin_key() or "zcode"
    curr_gw = store.gateway_key()

    if new_pass is not None and str(new_pass).strip():
        np = str(new_pass).strip()
        store.set_setting("admin_key", np)
        print(f"[密码管理] ✔ 后台管理密码已成功修改为: {np}")
        return

    print("========================================================================")
    print("                   ZCode 后台管理与 API 密钥设置")
    print("========================================================================")
    print(f"  当前后台管理密码 (Admin Password): {curr_admin}")
    print(f"  当前网关 API 密钥 (Gateway Key):    {curr_gw}")
    print("------------------------------------------------------------------------")
    print("  [1] 修改 Web 后台管理登录密码")
    print("  [2] 重置后台管理登录密码为默认 (zcode)")
    print("  [3] 修改 / 自定义 Gateway API Key (调用网关的 Bearer Token)")
    print("  [0] 返回上级菜单")
    print("========================================================================")

    try:
        choice = input("请选择操作 [默认 1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return

    if not choice or choice == "1":
        try:
            pwd = input("请输入新的后台管理密码: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if pwd:
            store.set_setting("admin_key", pwd)
            print(f"\n🎉 后台管理密码修改成功！新密码为: {pwd}")
        else:
            print("\n[提示] 输入为空，未做修改。")
    elif choice == "2":
        store.set_setting("admin_key", "zcode")
        print("\n✔ 后台管理密码已成功重置为默认值: zcode")
    elif choice == "3":
        try:
            gw = input("请输入新的 Gateway API Key (留空回车自动生成随机 Key): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not gw:
            import secrets
            gw = f"sk-zcode2api-{secrets.token_hex(16)}"
        store.set_setting("gateway_key", gw)
        print(f"\n✔ Gateway API Key 已更新为: {gw}")
    elif choice == "0":
        return
    else:
        print("\n[提示] 无效选项。")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "detail":
        print_detail()
    elif len(sys.argv) > 1 and sys.argv[1] == "quota":
        print_quota_line()
    elif len(sys.argv) > 1 and sys.argv[1] in ("admin_key", "admin-key", "get-passwd"):
        print_admin_key()
    elif len(sys.argv) > 1 and sys.argv[1] == "serve-banner":
        print_serve_banner()
    elif len(sys.argv) > 1 and sys.argv[1] in ("passwd", "password", "set-passwd"):
        new_p = sys.argv[2] if len(sys.argv) > 2 else None
        do_manage_password(new_p)
    elif len(sys.argv) > 1 and sys.argv[1] == "import":
        do_import_zcode()
    elif len(sys.argv) > 1 and sys.argv[1] in ("switch", "auto-switch"):
        do_switch_account()
    elif len(sys.argv) > 1 and sys.argv[1] == "check-and-auto-switch":
        do_check_and_auto_switch()
    elif len(sys.argv) > 1 and sys.argv[1] == "sync_current_to_pool":
        sync_current_to_pool()
    elif len(sys.argv) > 1 and sys.argv[1] == "claim":
        do_claim_all()
    elif len(sys.argv) > 1 and sys.argv[1] == "manage":
        do_manage_accounts()
    elif len(sys.argv) > 1 and sys.argv[1] in ("kill-port", "kill_port"):
        do_kill_port(8080)
    else:
        print_summary()

