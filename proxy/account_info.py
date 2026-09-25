import os
import sys
import json
import base64
import hashlib
from datetime import datetime
from pathlib import Path

def get_account_info():
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
    if not jwt:
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

def print_summary():
    info = get_account_info()
    if not info:
        print("[未登录] 尚未绑定任何账号")
        return
    if "error" in info:
        print(f"[读取异常] {info['error']}")
        return

    provider = info.get("provider", "unknown")
    user_id = info.get("userId") or info.get("user_id") or "未知"
    
    if provider == "bigmodel":
        region_desc = "【国内】智谱开放平台 (BigModel)"
    elif provider == "zai":
        region_desc = "【国外/国际版】Z.AI 平台"
    else:
        region_desc = f"【其他】{provider}"
        
    print(f"{region_desc} (用户ID: {user_id}) ● 使用中")

def print_quota_line():
    info = get_account_info()
    if not info or "error" in info:
        return
    billing = fetch_online_billing(info)
    if not billing:
        return
    
    balances = billing.get("balances") or []
    plans = billing.get("plans") or []
    
    items = []
    exp_time = ""
    for b in balances:
        name = b.get("show_name") or "Model"
        rem = format_units(b.get("remaining_units"))
        tot = format_units(b.get("total_units"))
        items.append(f"{name}: {rem}/{tot}")
        if not exp_time and b.get("expires_at"):
            exp_time = format_time(b.get("expires_at"))

    extra_build = ""
    for p in plans:
        if "build" in (p.get("name") or "").lower():
            extra_build = " + 1亿 Build包"

    if items:
        res = " · ".join(items)
        if exp_time:
            res += f" (至 {exp_time})"
        if extra_build:
            res += extra_build
        print(f" [实时额度] {res}")

def print_detail():
    info = get_account_info()
    if not info:
        print("未检测到登录凭据，请先在菜单中执行 [2] 或 [3] 登录账号。")
        return
    if "error" in info:
        print(f"解密凭据失败: {info['error']}")
        return

    provider = info.get("provider", "unknown")
    user_id = info.get("userId") or info.get("user_id") or "未知"
    api_key = info.get("apiKey") or ""
    masked_key = f"{api_key[:12]}..." if len(api_key) > 12 else (api_key or "无")
    
    print("========================================================================")
    print("                      当前登录账号详细信息与额度看板")
    print("========================================================================")
    if provider == "bigmodel":
        print("  平台归属:   【国内平台】智谱开放平台 (BigModel)")
        print("  平台网址:   https://open.bigmodel.cn / https://bigmodel.cn")
        print("  网络要求:   国内直连 (无需代理/无需海外节点)")
    elif provider == "zai":
        print("  平台归属:   【国外/国际平台】Z.AI 全球平台")
        print("  平台网址:   https://zcode.z.ai / https://api.z.ai")
        print("  网络要求:   国际网络节点")
    else:
        print(f"  平台归属:   {provider}")
        
    print(f"  用户账户:   用户唯一数字 ID: {user_id}")
    print(f"  当前状态:   ● 使用中 (✓ 当前已激活生效)")
    print(f"  凭据密钥:   {masked_key}")

    billing = fetch_online_billing(info)
    if billing:
        plans = billing.get("plans") or []
        balances = billing.get("balances") or []
        
        bal_map = {}
        for b in balances:
            u_pid = b.get("user_plan_id")
            ent_id = b.get("entitlement_id")
            name = b.get("show_name") or b.get("meter")
            if u_pid and ent_id:
                bal_map[(u_pid, ent_id)] = b
            if u_pid and name:
                bal_map[(u_pid, name)] = b

        print("\n------------------------------------------------------------------------")
        print("【已生效方案与实时额度看板】")
        print("------------------------------------------------------------------------")
        
        for idx, p in enumerate(plans, 1):
            p_name = p.get("name") or "Plan"
            p_desc = p.get("description") or ""
            p_ends = p.get("ends_at")
            u_pid = p.get("user_plan_id")
            
            p_exp_str = f"至 {format_time(p_ends)}" if p_ends else "长期有效"
            
            sub_bals = [b for b in balances if b.get("user_plan_id") == u_pid]
            if sub_bals and sub_bals[0].get("expires_at"):
                p_exp_str = f"至 {format_time(sub_bals[0].get('expires_at'))}"

            print(f"\n  [{idx}] {p_name} ({p_desc})")
            print(f"      有效时间:   {p_exp_str}")
            
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
                    bar_str = make_bar(rem, tot, width=20)
                    rem_str = f"{format_units(rem)}/{format_units(tot)}"
                    print(f"      • {m_name:<14}: {rem_str:<10} {bar_str} (已消耗: {used} units)")
                else:
                    grant_str = f"{format_units(grant)} Token"
                    print(f"      • {m_name:<14}: {grant_str:<10} [已获配额，服务端未提供剩余量]")

    home = Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))
    print("\n------------------------------------------------------------------------")
    print(f"  凭据存储路径: {home / '.zcode-proxy' / 'credentials.json'}")
    print("========================================================================")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "detail":
        print_detail()
    elif len(sys.argv) > 1 and sys.argv[1] == "quota":
        print_quota_line()
    else:
        print_summary()
