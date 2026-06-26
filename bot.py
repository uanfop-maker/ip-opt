#!/usr/bin/env python3
"""ip-opt: Fly.io Xray VPN management Telegram bot (multi-machine)"""
import os, json, uuid, base64, subprocess, requests, tempfile, qrcode
from io import BytesIO
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes

# Config
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
FLY_API_TOKEN = os.environ['FLY_API_TOKEN']

# Machine C (original Xray CN, ganamia-xray-cn)
MACHINE_C_APP = os.environ.get('FLY_APP', 'ganamia-xray-cn')
MACHINE_C_ID = os.environ.get('MACHINE_C_ID', '080d12db59e978')

# Machine AB (combined WireGuard + Xray, ganamia-wg-vpn)
MACHINE_AB_APP = os.environ.get('MACHINE_AB_APP', 'ganamia-wg-vpn')
MACHINE_AB_ID = os.environ.get('MACHINE_AB_ID', '080d396c039238')

# Legacy aliases for backward compatibility
MACHINE_B_APP = MACHINE_AB_APP
MACHINE_B_ID = MACHINE_AB_ID
MACHINE_A_APP = MACHINE_AB_APP
MACHINE_A_ID = MACHINE_AB_ID

ADMIN_FILE = '/app/data/admins.json'
SETTINGS_LOCAL = '/app/data/settings.json'

# VLESS connection info for Machine C
XRAY_C_SNI = 'www.apple.com'
XRAY_C_FP = 'chrome'
XRAY_C_FLOW = 'xtls-rprx-vision'
XRAY_C_PORT = 443

# VLESS connection info for Machine B
XRAY_B_SNI = 'addons.mozilla.org'
XRAY_B_FP = 'chrome'
XRAY_B_FLOW = 'xtls-rprx-vision'
XRAY_B_PORT = 443
XRAY_B_PUBKEY = 'MPO9W-mBflxhkz8HFKZR65rSVKwIPUhMCzTKIwm5ug'   # derived from MP4jWvcOrKCp1wB2y0TuhBSq52C4Gu_FmwTzmFCjCWM
XRAY_B_SHORTID = '43fad4af'

DEFAULT_ADMINS = [8272893083]

os.makedirs('/app/data', exist_ok=True)

def load_admins():
    try:
        with open(ADMIN_FILE) as f:
            return json.load(f)
    except:
        return DEFAULT_ADMINS[:]

def save_admins(admins):
    with open(ADMIN_FILE, 'w') as f:
        json.dump(admins, f)

def is_admin(user_id):
    return int(user_id) in load_admins()

def require_admin(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("❌ 無權限")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper

def fly_api(query, variables=None):
    resp = requests.post('https://api.fly.io/graphql',
        headers={'Authorization': f'Bearer {FLY_API_TOKEN}'},
        json={'query': query, 'variables': variables or {}},
        timeout=30)
    return resp.json()

def fly_machines_api(method, path, data=None):
    url = f'https://api.machines.dev/v1{path}'
    resp = requests.request(method, url,
        headers={'Authorization': f'Bearer {FLY_API_TOKEN}'},
        json=data, timeout=30)
    return resp.json() if resp.content else {}

def get_current_ip(app=None):
    target_app = app or MACHINE_C_APP
    result = fly_api('{ app(name: "' + target_app + '") { ipAddresses { nodes { address type } } } }')
    try:
        ips = result['data']['app']['ipAddresses']['nodes']
        for ip in ips:
            if ip['type'] == 'v4':
                return ip['address']
    except:
        pass
    return None

def ssh_run(cmd, app=None, machine_id=None):
    """Run command on a specific Fly.io machine via SSH"""
    fly_path = '/root/.fly/bin/flyctl'
    target_app = app or MACHINE_C_APP
    args = [fly_path, 'ssh', 'console', '--app', target_app, '-C', cmd]
    if machine_id:
        args = [fly_path, 'ssh', 'console', '--app', target_app, '--machine', machine_id, '-C', cmd]
    result = subprocess.run(
        args,
        capture_output=True, text=True, timeout=30,
        env={**os.environ, 'FLY_API_TOKEN': FLY_API_TOKEN}
    )
    return result.stdout + result.stderr

def machine_exec(app, machine_id, cmd):
    """Run command via Machines API exec endpoint (faster than SSH)"""
    resp = requests.post(
        f'https://api.machines.dev/v1/apps/{app}/machines/{machine_id}/exec',
        headers={'Authorization': f'Bearer {FLY_API_TOKEN}'},
        json={'cmd': cmd, 'timeout': 15},
        timeout=20
    )
    if resp.ok:
        d = resp.json()
        return d.get('stdout', '') + d.get('stderr', '')
    return f'exec error: {resp.status_code}'

def generate_qr(vless_uri):
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=8, border=4)
    qr.add_data(vless_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf

def make_vless_uri_c(ip, client_uuid, name="VPN-C"):
    from urllib.parse import quote
    pubkey = os.environ.get('XRAY_PUBKEY', 'jAxEkKnMiqU_7y-Qev9rvgjiKd8t7usngjAbSbQgAnw')
    shortid = os.environ.get('XRAY_SHORTID', '7dd35619b1caf795')
    return (f"vless://{client_uuid}@{ip}:{XRAY_C_PORT}"
            f"?encryption=none&flow={XRAY_C_FLOW}&security=reality"
            f"&sni={XRAY_C_SNI}&fp={XRAY_C_FP}&pbk={pubkey}&sid={shortid}"
            f"&type=tcp&headerType=none#{quote(name)}")

def make_vless_uri_b(ip, client_uuid, name="VPN-B"):
    from urllib.parse import quote
    pubkey = os.environ.get('XRAY_B_PUBKEY', XRAY_B_PUBKEY)
    shortid = XRAY_B_SHORTID
    return (f"vless://{client_uuid}@{ip}:{XRAY_B_PORT}"
            f"?encryption=none&flow={XRAY_B_FLOW}&security=reality"
            f"&sni={XRAY_B_SNI}&fp={XRAY_B_FP}&pbk={pubkey}&sid={shortid}"
            f"&type=tcp&headerType=none#{quote(name)}")

# Commands

@require_admin
async def cmd_start(update, context):
    await update.message.reply_text(
        "🔐 ip-opt VPN 管理機器人（2 機器版）\n\n"
        "AB: WireGuard + Xray VLESS (ganamia-wg-vpn, addons.mozilla.org)\n"
        "C: Xray VLESS (ganamia-xray-cn, www.apple.com)\n\n"
        "輸入 / 查看所有指令"
    )

@require_admin
async def cmd_ipnow(update, context):
    ip_ab = get_current_ip(MACHINE_AB_APP)
    ip_c = get_current_ip(MACHINE_C_APP)
    await update.message.reply_text(
        f"🌐 目前 IP\n"
        f"Machine AB (WG+Xray): {ip_ab or '無法取得'}\n"
        f"Machine C (Xray CN): {ip_c or '無法取得'}"
    )

@require_admin
async def cmd_ipswitching(update, context):
    args = context.args
    # Support /ipswitching [ab|b|c] [country]
    target = 'c'
    if args and args[0].lower() in ('ab', 'b', 'c'):
        target = args[0].lower()
        args = args[1:]

    if target in ('ab', 'b'):
        app = MACHINE_AB_APP
        machine_id = MACHINE_AB_ID
        label = 'AB'
        target = 'b'  # use b uri maker for AB
    else:
        app = MACHINE_C_APP
        machine_id = MACHINE_C_ID
        label = 'C'

    msg = await update.message.reply_text(f"🔄 Machine {label} 換 IP...")
    old_ip = get_current_ip(app)

    result = fly_api('''
    mutation($input: AllocateIPAddressInput!) {
        allocateIpAddress(input: $input) { ipAddress { address } }
    }''', {'input': {'appId': app, 'type': 'v4', 'region': ''}})

    new_ip = None
    try:
        new_ip = result['data']['allocateIpAddress']['ipAddress']['address']
    except:
        await msg.edit_text(f"❌ Machine {label} 換 IP 失敗：{result}")
        return

    if old_ip and old_ip != new_ip:
        fly_api('''
        mutation($input: ReleaseIPAddressInput!) {
            releaseIpAddress(input: $input) { app { name } }
        }''', {'input': {'appId': app, 'ip': old_ip}})

    clients_json = machine_exec(app, machine_id, 'cat /data/clients.json 2>/dev/null || echo "{}"')
    try:
        clients = json.loads(clients_json.strip())
    except:
        clients = {}

    text = f"✅ Machine {label} IP 已切換\n舊 IP：{old_ip}\n新 IP：{new_ip}\n\n掃碼更新設定："
    await msg.edit_text(text)

    for name, info in clients.items():
        if info.get('active', True):
            if target == 'b':
                uri = make_vless_uri_b(new_ip, info['uuid'], name)
            else:
                uri = make_vless_uri_c(new_ip, info['uuid'], name)
            qr_buf = generate_qr(uri)
            await update.message.reply_photo(qr_buf, caption=f"[{label}] 用戶：{name}")

@require_admin
async def cmd_iprotation(update, context):
    args = context.args
    target = 'c'
    if args and args[0].lower() in ('ab', 'b', 'c'):
        target = args[0].lower()
        args = args[1:]

    if target in ('ab', 'b'):
        app = MACHINE_AB_APP
        machine_id = MACHINE_AB_ID
        label = 'AB'
    else:
        app = MACHINE_C_APP
        machine_id = MACHINE_C_ID
        label = 'C'

    if not args:
        settings_raw = machine_exec(app, machine_id, 'cat /data/settings.json 2>/dev/null || echo "{}"')
        settings = json.loads(settings_raw.strip() or '{}')
        rotation = settings.get('rotation', 'on')
        threshold = settings.get('threshold', 90)
        await update.message.reply_text(f"[Machine {label}] 自動換 IP：{rotation}\n門檻：{threshold}%")
        return

    param = args[0].lower()
    if param in ('on', 'off'):
        machine_exec(app, machine_id, f"python3 -c \"import json; d=json.load(open('/data/settings.json')); d['rotation']='{param}'; json.dump(d, open('/data/settings.json','w'))\"")
        await update.message.reply_text(f"[Machine {label}] ✅ 自動換 IP 已{'開啟' if param=='on' else '關閉'}")
    elif param.isdigit():
        val = int(param)
        if 1 <= val <= 99:
            machine_exec(app, machine_id, f"python3 -c \"import json; d=json.load(open('/data/settings.json')); d['threshold']={val}; json.dump(d, open('/data/settings.json','w'))\"")
            await update.message.reply_text(f"[Machine {label}] ✅ 門檻設為 {val}%")
        else:
            await update.message.reply_text("❌ 請輸入 1-99 之間的數字")
    else:
        await update.message.reply_text("用法：/iprotation [b|c] on|off|數字")

@require_admin
async def cmd_setlimit(update, context):
    args = context.args
    target = 'c'
    if args and args[0].lower() in ('ab', 'b', 'c'):
        target = args[0].lower()
        args = args[1:]

    if not args or not args[0].isdigit():
        await update.message.reply_text("用法：/setlimit [ab|c] 100 (GB)")
        return

    if target in ('ab', 'b'):
        app = MACHINE_AB_APP
        machine_id = MACHINE_AB_ID
        label = 'AB'
    else:
        app = MACHINE_C_APP
        machine_id = MACHINE_C_ID
        label = 'C'
    gb = int(args[0])
    machine_exec(app, machine_id, f"python3 -c \"import json; d=json.load(open('/data/settings.json')); d['limit_gb']={gb}; json.dump(d, open('/data/settings.json','w'))\"")
    await update.message.reply_text(f"[Machine {label}] ✅ 月流量上限設為 {gb} GB")

@require_admin
async def cmd_usage(update, context):
    import datetime
    month = datetime.datetime.utcnow().strftime('%Y-%m')
    lines = ["📊 流量統計\n"]

    for label, app, machine_id in [('AB', MACHINE_AB_APP, MACHINE_AB_ID), ('C', MACHINE_C_APP, MACHINE_C_ID)]:
        data_raw = machine_exec(app, machine_id, 'cat /data/traffic.json 2>/dev/null || echo "{}"')
        settings_raw = machine_exec(app, machine_id, 'cat /data/settings.json 2>/dev/null || echo "{}"')
        try:
            data = json.loads(data_raw.strip())
        except:
            data = {}
        try:
            settings = json.loads(settings_raw.strip())
        except:
            settings = {}

        monthly = data.get('monthly', {})
        total = monthly.get(month, 0)
        limit_gb = settings.get('limit_gb', 100)
        limit_b = limit_gb * 1024**3
        pct = total / limit_b * 100 if limit_b > 0 else 0
        cost = total / 1024**3 * 0.04

        lines.append(f"[Machine {label}]")
        lines.append(f"  本月：{total/1024**3:.2f} GB / {limit_gb} GB ({pct:.1f}%)")
        lines.append(f"  流量費：${cost:.2f}")
        lines.append("")

    await update.message.reply_text('\n'.join(lines))

@require_admin
async def cmd_status(update, context):
    lines = ["🖥 系統狀態\n"]

    # Machine AB - WireGuard + Xray (combined)
    ip_ab = get_current_ip(MACHINE_AB_APP)
    ps_ab = machine_exec(MACHINE_AB_APP, MACHINE_AB_ID, 'ps aux')
    xray_ab = 'xray' in ps_ab
    wg_ab = 'node /app/server.js' in ps_ab
    uptime_ab = machine_exec(MACHINE_AB_APP, MACHINE_AB_ID, 'uptime').strip()

    # WireGuard peers via wg show
    try:
        wg_out = machine_exec(MACHINE_AB_APP, MACHINE_AB_ID, 'wg show all dump')
        peer_count = len([l for l in wg_out.splitlines() if len(l.split('\t')) >= 9])
        handshake_lines = []
        wg_json_raw = machine_exec(MACHINE_AB_APP, MACHINE_AB_ID, 'cat /etc/wireguard/wg0.json 2>/dev/null || echo "{}"')
        try:
            wg_json = json.loads(wg_json_raw.strip())
            clients = wg_json.get('clients', {})
            names = {v.get('publicKey'): v.get('name', k) for k, v in clients.items()}
        except:
            names = {}
        for line in wg_out.splitlines():
            parts = line.split('\t')
            if len(parts) >= 9:
                pubkey = parts[1]
                hs = int(parts[5]) if parts[5].isdigit() else 0
                name = names.get(pubkey, pubkey[:12] + '...')
                if hs > 0:
                    import time
                    age = int(time.time()) - hs
                    status = '✅' if age < 180 else '⏸'
                    handshake_lines.append(f"  {status} {name} ({age}s ago)")
                else:
                    handshake_lines.append(f"  ⭕ {name} (未連線)")
    except Exception as e:
        peer_count = 0
        handshake_lines = [f"  WG 錯誤: {e}"]

    lines.append(f"[Machine AB] WireGuard + Xray (ganamia-wg-vpn)")
    lines.append(f"  IP：{ip_ab or '無'}")
    lines.append(f"  wg-easy：{'✅ 運行中' if wg_ab else '❌ 已停止'}")
    lines.append(f"  Xray：{'✅ 運行中' if xray_ab else '❌ 已停止'}")
    lines.append(f"  WG Peers：{peer_count}")
    for h in handshake_lines[:5]:
        lines.append(h)
    lines.append(f"  {uptime_ab}")
    lines.append("")

    # Machine C - Xray
    ip_c = get_current_ip(MACHINE_C_APP)
    ps_c = machine_exec(MACHINE_C_APP, MACHINE_C_ID, 'ps aux')
    xray_c = 'xray' in ps_c
    uptime_c = machine_exec(MACHINE_C_APP, MACHINE_C_ID, 'uptime').strip()
    lines.append(f"[Machine C] Xray (www.apple.com)")
    lines.append(f"  Xray：{'✅ 運行中' if xray_c else '❌ 已停止'}")
    lines.append(f"  IP：{ip_c or '無'}")
    lines.append(f"  {uptime_c}")

    await update.message.reply_text('\n'.join(lines))

@require_admin
async def cmd_whoisusing(update, context):
    args = context.args
    target = 'both'
    if args and args[0].lower() in ('ab', 'b', 'c'):
        target = args[0].lower()

    lines = []

    if target in ('ab', 'b', 'both'):
        log_ab = machine_exec(MACHINE_AB_APP, MACHINE_AB_ID, 'tail -10 /var/log/xray/access.log 2>/dev/null || echo "暫無記錄"')
        lines.append("[Machine AB] Xray 最近連線：")
        connections = [l for l in log_ab.split('\n') if 'accepted' in l.lower() or '>>>' in l]
        lines.append('\n'.join(connections[-5:]) if connections else "  無記錄")
        lines.append("")

        # Also show WireGuard status
        wg_dump = machine_exec(MACHINE_AB_APP, MACHINE_AB_ID, 'wg show all dump')
        lines.append("[Machine AB] WireGuard peers：")
        wg_json_raw = machine_exec(MACHINE_AB_APP, MACHINE_AB_ID, 'cat /etc/wireguard/wg0.json 2>/dev/null || echo "{}"')
        try:
            wg_json = json.loads(wg_json_raw.strip())
            names = {v.get('publicKey'): v.get('name', k) for k, v in wg_json.get('clients', {}).items()}
        except:
            names = {}
        for line in wg_dump.splitlines():
            parts = line.split('\t')
            if len(parts) >= 9:
                pubkey = parts[1]
                name = names.get(pubkey, pubkey[:16])
                lines.append(f"  {name}: endpoint={parts[3]}")
        lines.append("")

    if target in ('c', 'both'):
        log_c = machine_exec(MACHINE_C_APP, MACHINE_C_ID, 'tail -10 /var/log/xray/access.log 2>/dev/null || echo "暫無記錄"')
        lines.append("[Machine C] 最近連線：")
        connections = [l for l in log_c.split('\n') if 'accepted' in l.lower() or '>>>' in l]
        lines.append('\n'.join(connections[-5:]) if connections else "  無記錄")

    await update.message.reply_text('\n'.join(lines) or "暫無連線記錄")

@require_admin
async def cmd_vpnallowlist(update, context):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("用法：/vpnallowlist [b|c] add 名稱 | /vpnallowlist [b|c] remove 名稱")
        return

    target = 'c'
    if args[0].lower() in ('ab', 'b', 'c'):
        target = args[0].lower()
        args = args[1:]

    if len(args) < 2:
        await update.message.reply_text("用法：/vpnallowlist [ab|c] add 名稱 | /vpnallowlist [ab|c] remove 名稱")
        return

    if target in ('ab', 'b'):
        app = MACHINE_AB_APP
        machine_id = MACHINE_AB_ID
        label = 'AB'
    else:
        app = MACHINE_C_APP
        machine_id = MACHINE_C_ID
        label = 'C'
    action = args[0].lower()
    name = args[1]

    clients_raw = machine_exec(app, machine_id, 'cat /data/clients.json 2>/dev/null || echo "{}"')
    try:
        clients = json.loads(clients_raw.strip())
    except:
        clients = {}

    if action == 'add':
        if name in clients:
            await update.message.reply_text(f"[Machine {label}] ❌ 用戶 {name} 已存在")
            return

        new_uuid = str(uuid.uuid4())
        clients[name] = {'uuid': new_uuid, 'active': True, 'email': name}

        clients_b64 = base64.b64encode(json.dumps(clients).encode()).decode()
        machine_exec(app, machine_id, f"echo '{clients_b64}' | base64 -d > /data/clients.json")

        await update.message.reply_text(f"[Machine {label}] ⏳ 新增 {name}，更新 Xray 設定...")
        update_xray_clients(clients, app, machine_id)

        ip = get_current_ip(app)
        if target == 'b':
            uri = make_vless_uri_b(ip, new_uuid, name)
        else:
            uri = make_vless_uri_c(ip, new_uuid, name)
        qr_buf = generate_qr(uri)
        await update.message.reply_photo(qr_buf,
            caption=f"[Machine {label}] ✅ 已新增用戶：{name}\nUUID：{new_uuid}\n\n掃碼連線")

    elif action == 'remove':
        if name not in clients:
            await update.message.reply_text(f"[Machine {label}] ❌ 用戶 {name} 不存在")
            return

        clients[name]['active'] = False
        clients_b64 = base64.b64encode(json.dumps(clients).encode()).decode()
        machine_exec(app, machine_id, f"echo '{clients_b64}' | base64 -d > /data/clients.json")
        update_xray_clients(clients, app, machine_id)
        await update.message.reply_text(f"[Machine {label}] ✅ 已停用用戶：{name}")

def update_xray_clients(clients, app, machine_id):
    """Update Xray config with current active clients and reload"""
    active = [{'id': v['uuid'], 'flow': 'xtls-rprx-vision', 'email': k}
              for k, v in clients.items() if v.get('active', True)]

    config_raw = machine_exec(app, machine_id, 'cat /etc/xray/config.json')
    try:
        config = json.loads(config_raw)
        config['inbounds'][0]['settings']['clients'] = active
        config_b64 = base64.b64encode(json.dumps(config).encode()).decode()
        machine_exec(app, machine_id, f"echo '{config_b64}' | base64 -d > /etc/xray/config.json")
        machine_exec(app, machine_id, 'kill -HUP $(pgrep xray) 2>/dev/null || true')
    except Exception as e:
        print(f"Error updating Xray config: {e}")

@require_admin
async def cmd_clients(update, context):
    lines = ["👥 VPN 用戶列表\n"]

    for label, app, machine_id in [('AB', MACHINE_AB_APP, MACHINE_AB_ID), ('C', MACHINE_C_APP, MACHINE_C_ID)]:
        clients_raw = machine_exec(app, machine_id, 'cat /data/clients.json 2>/dev/null || echo "{}"')
        try:
            clients = json.loads(clients_raw.strip())
        except:
            clients = {}

        lines.append(f"[Machine {label}]")
        if not clients:
            lines.append("  目前無用戶")
        else:
            for name, info in clients.items():
                status = "✅" if info.get('active', True) else "❌"
                lines.append(f"  {status} {name}")
        lines.append("")

    await update.message.reply_text('\n'.join(lines))

@require_admin
async def cmd_getconfig(update, context):
    args = context.args
    target = 'c'
    name = 'default'

    if args and args[0].lower() in ('ab', 'b', 'c'):
        target = args[0].lower()
        args = args[1:]
    if args:
        name = args[0]

    if target in ('ab', 'b'):
        app = MACHINE_AB_APP
        machine_id = MACHINE_AB_ID
        label = 'AB'
        target = 'b'  # use b uri maker
    else:
        app = MACHINE_C_APP
        machine_id = MACHINE_C_ID
        label = 'C'

    clients_raw = machine_exec(app, machine_id, 'cat /data/clients.json 2>/dev/null || echo "{}"')
    try:
        clients = json.loads(clients_raw.strip())
    except:
        clients = {}

    if name not in clients:
        await update.message.reply_text(f"[Machine {label}] ❌ 找不到用戶 {name}\n用戶列表：{', '.join(clients.keys())}")
        return

    ip = get_current_ip(app)
    if target == 'b':
        uri = make_vless_uri_b(ip, clients[name]['uuid'], name)
    else:
        uri = make_vless_uri_c(ip, clients[name]['uuid'], name)
    qr_buf = generate_qr(uri)
    await update.message.reply_photo(qr_buf, caption=f"[Machine {label}] 用戶：{name}\nIP：{ip}")

@require_admin
async def cmd_ping(update, context):
    args = context.args
    target = 'c'
    if args and args[0].lower() in ('ab', 'b', 'c'):
        target = args[0].lower()
        args = args[1:]

    if not args:
        await update.message.reply_text("用法：/ping [ab|c] 8.8.8.8")
        return

    host = args[0].replace(';', '').replace('&', '').replace('|', '')

    if target in ('ab', 'b'):
        result = machine_exec(MACHINE_AB_APP, MACHINE_AB_ID, f'ping -c 4 {host} 2>&1')
    else:
        result = machine_exec(MACHINE_C_APP, MACHINE_C_ID, f'ping -c 4 {host} 2>&1')

    await update.message.reply_text(f"```\n{result[:1000]}\n```", parse_mode='Markdown')

@require_admin
async def cmd_traceroute(update, context):
    args = context.args
    target = 'c'
    if args and args[0].lower() in ('ab', 'b', 'c'):
        target = args[0].lower()
        args = args[1:]

    if not args:
        await update.message.reply_text("用法：/traceroute [ab|c] 8.8.8.8")
        return

    host = args[0].replace(';', '').replace('&', '').replace('|', '')

    if target in ('ab', 'b'):
        result = machine_exec(MACHINE_AB_APP, MACHINE_AB_ID, f'traceroute -m 15 {host} 2>&1')
    else:
        result = machine_exec(MACHINE_C_APP, MACHINE_C_ID, f'traceroute -m 15 {host} 2>&1')

    await update.message.reply_text(f"```\n{result[:1500]}\n```", parse_mode='Markdown')

@require_admin
async def cmd_restart(update, context):
    args = context.args
    target = 'c'
    if args and args[0].lower() in ('ab', 'b', 'c'):
        target = args[0].lower()

    if target in ('ab', 'b'):
        app = MACHINE_AB_APP
        machine_id = MACHINE_AB_ID
        label = 'AB'
    else:
        app = MACHINE_C_APP
        machine_id = MACHINE_C_ID
        label = 'C'

    msg = await update.message.reply_text(f"⏳ Machine {label} 重啟 Xray...")
    result = machine_exec(app, machine_id, "sh -c 'kill -HUP $(pgrep xray) 2>/dev/null && echo OK || echo restarted'")
    await msg.edit_text(f"[Machine {label}] ✅ Xray 已重啟\n{result[:200]}")

@require_admin
async def cmd_adminlist(update, context):
    args = context.args
    if len(args) < 2:
        admins = load_admins()
        await update.message.reply_text(f"管理員列表：\n{admins}")
        return

    action = args[0].lower()
    try:
        target_id = int(args[1])
    except:
        await update.message.reply_text("❌ 請輸入有效的 TG 用戶 ID")
        return

    admins = load_admins()
    if action == 'add':
        if target_id not in admins:
            admins.append(target_id)
            save_admins(admins)
        await update.message.reply_text(f"✅ 已新增管理員 {target_id}")
    elif action == 'remove':
        if target_id in admins:
            admins.remove(target_id)
            save_admins(admins)
        await update.message.reply_text(f"✅ 已移除管理員 {target_id}")

@require_admin
async def cmd_dlmode(update, context):
    args = context.args
    target = 'c'
    if args and args[0].lower() in ('ab', 'b', 'c'):
        target = args[0].lower()
        args = args[1:]

    if target in ('ab', 'b'):
        app = MACHINE_AB_APP
        machine_id = MACHINE_AB_ID
        label = 'AB'
    else:
        app = MACHINE_C_APP
        machine_id = MACHINE_C_ID
        label = 'C'

    if not args:
        settings_raw = machine_exec(app, machine_id, 'cat /data/settings.json 2>/dev/null || echo "{}"')
        settings = json.loads(settings_raw.strip() or '{}')
        mode = "開啟" if settings.get('rotation', 'on') != 'off' else "關閉（下載模式）"
        await update.message.reply_text(f"[Machine {label}] 自動換 IP：{mode}")
        return

    param = args[0].lower()
    if param == 'on':
        machine_exec(app, machine_id, "python3 -c \"import json; d=json.load(open('/data/settings.json')); d['rotation']='off'; json.dump(d, open('/data/settings.json','w'))\"")
        await update.message.reply_text(f"[Machine {label}] ✅ 下載模式開啟（自動換 IP 已暫停）")
    elif param == 'off':
        machine_exec(app, machine_id, "python3 -c \"import json; d=json.load(open('/data/settings.json')); d['rotation']='on'; json.dump(d, open('/data/settings.json','w'))\"")
        await update.message.reply_text(f"[Machine {label}] ✅ 下載模式關閉（自動換 IP 恢復）")

async def post_init(app):
    """Set bot commands menu"""
    commands = [
        BotCommand("status", "全部機器狀態（AB WG+Xray + C Xray）"),
        BotCommand("ipnow", "查目前 IP (AB 和 C)"),
        BotCommand("usage", "本月流量與費用（AB 和 C）"),
        BotCommand("ipswitching", "換 IP，例：/ipswitching ab 或 /ipswitching c"),
        BotCommand("iprotation", "自動換 IP 設定，例：/iprotation ab on"),
        BotCommand("setlimit", "設定月流量上限，例：/setlimit ab 100"),
        BotCommand("whoisusing", "目前連線用戶，例：/whoisusing ab"),
        BotCommand("vpnallowlist", "管理 VPN 用戶，例：/vpnallowlist ab add 名稱"),
        BotCommand("clients", "列出所有 VPN 用戶（AB 和 C）"),
        BotCommand("getconfig", "重新取得 QR Code，例：/getconfig ab default"),
        BotCommand("ping", "Ping 測試，例：/ping ab 8.8.8.8"),
        BotCommand("traceroute", "路由追蹤，例：/traceroute c 8.8.8.8"),
        BotCommand("restart", "重啟 Xray，例：/restart ab"),
        BotCommand("adminlist", "管理管理員"),
        BotCommand("dlmode", "下載模式，例：/dlmode ab on"),
    ]
    await app.bot.set_my_commands(commands)

def main():
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    handlers = [
        ('start', cmd_start),
        ('ipswitching', cmd_ipswitching),
        ('ipnow', cmd_ipnow),
        ('iprotation', cmd_iprotation),
        ('setlimit', cmd_setlimit),
        ('usage', cmd_usage),
        ('status', cmd_status),
        ('whoisusing', cmd_whoisusing),
        ('vpnallowlist', cmd_vpnallowlist),
        ('clients', cmd_clients),
        ('getconfig', cmd_getconfig),
        ('ping', cmd_ping),
        ('traceroute', cmd_traceroute),
        ('restart', cmd_restart),
        ('adminlist', cmd_adminlist),
        ('dlmode', cmd_dlmode),
    ]

    for cmd, handler in handlers:
        application.add_handler(CommandHandler(cmd, handler, filters=None))

    print("ip-opt bot starting (AB+C combined mode, Machine AB=080d396c039238)...")
    application.run_polling()

if __name__ == '__main__':
    main()
