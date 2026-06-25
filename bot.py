#!/usr/bin/env python3
"""ip-opt: Fly.io Xray VPN management Telegram bot"""
import os, json, uuid, base64, subprocess, requests, tempfile, qrcode
from io import BytesIO
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes

# Config
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
FLY_API_TOKEN = os.environ['FLY_API_TOKEN']
FLY_APP = os.environ.get('FLY_APP', 'ganamia-xray-cn')
ADMIN_FILE = '/app/data/admins.json'  # local storage for admins
SETTINGS_LOCAL = '/app/data/settings.json'

# VLESS connection info
XRAY_SNI = 'www.apple.com'
XRAY_FP = 'chrome'
XRAY_FLOW = 'xtls-rprx-vision'
XRAY_PORT = 443

# Default admin
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

def get_current_ip():
    result = fly_api('{ app(name: "' + FLY_APP + '") { ipAddresses { nodes { address type } } } }')
    try:
        ips = result['data']['app']['ipAddresses']['nodes']
        for ip in ips:
            if ip['type'] == 'v4':
                return ip['address']
    except:
        pass
    return None

def ssh_run(cmd):
    """Run command on Fly.io machine via SSH"""
    fly_path = '/root/.fly/bin/flyctl'
    result = subprocess.run(
        [fly_path, 'ssh', 'console', '--app', FLY_APP, '-C', cmd],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, 'FLY_API_TOKEN': FLY_API_TOKEN}
    )
    return result.stdout + result.stderr

def generate_qr(vless_uri):
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=8, border=4)
    qr.add_data(vless_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf

def make_vless_uri(ip, client_uuid, name="VPN"):
    from urllib.parse import quote
    pubkey = os.environ.get('XRAY_PUBKEY', 'jAxEkKnMiqU_7y-Qev9rvgjiKd8t7usngjAbSbQgAnw')
    shortid = os.environ.get('XRAY_SHORTID', '7dd35619b1caf795')
    return (f"vless://{client_uuid}@{ip}:{XRAY_PORT}"
            f"?encryption=none&flow={XRAY_FLOW}&security=reality"
            f"&sni={XRAY_SNI}&fp={XRAY_FP}&pbk={pubkey}&sid={shortid}"
            f"&type=tcp&headerType=none#{quote(name)}")

# Commands

@require_admin
async def cmd_start(update, context):
    await update.message.reply_text(
        "🔐 ip-opt VPN 管理機器人\n\n"
        "輸入 / 查看所有指令"
    )

@require_admin
async def cmd_ipnow(update, context):
    ip = get_current_ip()
    await update.message.reply_text(f"🌐 目前 IP：{ip or '無法取得'}")

@require_admin
async def cmd_ipswitching(update, context):
    args = context.args
    country = args[0].upper() if args else 'JP'

    msg = await update.message.reply_text("🔄 正在換 IP...")

    old_ip = get_current_ip()

    # Allocate new IPv4
    result = fly_api('''
    mutation($input: AllocateIPAddressInput!) {
        allocateIpAddress(input: $input) { ipAddress { address } }
    }''', {'input': {'appId': FLY_APP, 'type': 'v4', 'region': ''}})

    new_ip = None
    try:
        new_ip = result['data']['allocateIpAddress']['ipAddress']['address']
    except:
        await msg.edit_text(f"❌ 換 IP 失敗：{result}")
        return

    # Release old IP
    if old_ip and old_ip != new_ip:
        fly_api('''
        mutation($input: ReleaseIPAddressInput!) {
            releaseIpAddress(input: $input) { app { name } }
        }''', {'input': {'appId': FLY_APP, 'ip': old_ip}})

    # Generate new QR code
    clients_json = ssh_run('cat /data/clients.json 2>/dev/null || echo "{}"')
    try:
        clients = json.loads(clients_json.strip())
    except:
        clients = {}

    text = f"✅ IP 已切換\n舊 IP：{old_ip}\n新 IP：{new_ip}\n\n掃碼更新設定："
    await msg.edit_text(text)

    # Send QR codes for all active clients
    for name, info in clients.items():
        if info.get('active', True):
            uri = make_vless_uri(new_ip, info['uuid'], name)
            qr_buf = generate_qr(uri)
            await update.message.reply_photo(qr_buf, caption=f"用戶：{name}")

@require_admin
async def cmd_iprotation(update, context):
    args = context.args
    if not args:
        settings = json.loads(ssh_run('cat /data/settings.json 2>/dev/null || echo "{}"').strip() or '{}')
        rotation = settings.get('rotation', 'on')
        threshold = settings.get('threshold', 90)
        await update.message.reply_text(f"自動換 IP：{rotation}\n門檻：{threshold}%")
        return

    param = args[0].lower()
    if param in ('on', 'off'):
        ssh_run(f"python3 -c \"import json; d=json.load(open('/data/settings.json')); d['rotation']='{param}'; json.dump(d, open('/data/settings.json','w'))\"")
        await update.message.reply_text(f"✅ 自動換 IP 已{'開啟' if param=='on' else '關閉'}")
    elif param.isdigit():
        val = int(param)
        if 1 <= val <= 99:
            ssh_run(f"python3 -c \"import json; d=json.load(open('/data/settings.json')); d['threshold']={val}; json.dump(d, open('/data/settings.json','w'))\"")
            await update.message.reply_text(f"✅ 門檻設為 {val}%")
        else:
            await update.message.reply_text("❌ 請輸入 1-99 之間的數字")
    else:
        await update.message.reply_text("用法：/iprotation on|off|數字")

@require_admin
async def cmd_setlimit(update, context):
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("用法：/setlimit 100 (GB)")
        return
    gb = int(args[0])
    ssh_run(f"python3 -c \"import json; d=json.load(open('/data/settings.json')); d['limit_gb']={gb}; json.dump(d, open('/data/settings.json','w'))\"")
    await update.message.reply_text(f"✅ 月流量上限設為 {gb} GB")

@require_admin
async def cmd_usage(update, context):
    result = ssh_run('cat /data/traffic.json 2>/dev/null || echo "{}"')
    try:
        data = json.loads(result.strip())
    except:
        data = {}

    settings_raw = ssh_run('cat /data/settings.json 2>/dev/null || echo "{}"')
    try:
        settings = json.loads(settings_raw.strip())
    except:
        settings = {}

    import datetime
    month = datetime.datetime.utcnow().strftime('%Y-%m')
    monthly = data.get('monthly', {})

    lines = ["📊 本月流量統計\n"]
    total = 0
    for m, b in sorted(monthly.items(), reverse=True)[:3]:
        gb = b / 1024**3
        cost = gb * 0.04
        lines.append(f"{m}：{gb:.2f} GB (${cost:.2f})")
        if m == month:
            total = b

    limit_gb = settings.get('limit_gb', 100)
    limit_b = limit_gb * 1024**3
    pct = total / limit_b * 100 if limit_b > 0 else 0
    current_cost = total / 1024**3 * 0.04 + 2  # +$2 for IP

    lines.append(f"\n本月：{total/1024**3:.2f} GB / {limit_gb} GB ({pct:.1f}%)")
    lines.append(f"IP 費：$2.00")
    lines.append(f"流量費：${total/1024**3*0.04:.2f}")
    lines.append(f"合計：${current_cost:.2f}")

    await update.message.reply_text('\n'.join(lines))

@require_admin
async def cmd_status(update, context):
    ip = get_current_ip()
    # Check if xray is running
    ps_out = ssh_run('ps | grep xray | grep -v grep')
    xray_running = 'xray' in ps_out
    uptime = ssh_run('uptime').strip()

    status = "✅ 運行中" if xray_running else "❌ 已停止"
    await update.message.reply_text(
        f"🖥 系統狀態\n\n"
        f"Xray：{status}\n"
        f"IP：{ip or '無'}\n"
        f"Uptime：{uptime}"
    )

@require_admin
async def cmd_whoisusing(update, context):
    # Read recent Xray access log
    log_out = ssh_run('tail -20 /var/log/xray/access.log 2>/dev/null || echo "暫無記錄"')
    if not log_out.strip() or log_out.strip() == '暫無記錄':
        await update.message.reply_text("暫無連線記錄")
        return

    # Parse connections
    connections = []
    for line in log_out.split('\n'):
        if 'accepted' in line.lower() or '>>>' in line:
            connections.append(line[:100])

    if connections:
        await update.message.reply_text("🔌 最近連線：\n" + '\n'.join(connections[-10:]))
    else:
        await update.message.reply_text("目前無活躍連線")

@require_admin
async def cmd_vpnallowlist(update, context):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("用法：/vpnallowlist add 名稱 | /vpnallowlist remove 名稱")
        return

    action = args[0].lower()
    name = args[1]

    clients_raw = ssh_run('cat /data/clients.json 2>/dev/null || echo "{}"')
    try:
        clients = json.loads(clients_raw.strip())
    except:
        clients = {}

    if action == 'add':
        if name in clients:
            await update.message.reply_text(f"❌ 用戶 {name} 已存在")
            return

        new_uuid = str(uuid.uuid4())
        clients[name] = {'uuid': new_uuid, 'active': True, 'email': name}

        # Update clients.json on machine
        clients_b64 = base64.b64encode(json.dumps(clients).encode()).decode()
        ssh_run(f"echo '{clients_b64}' | base64 -d > /data/clients.json")

        # Update Xray config
        await update.message.reply_text(f"⏳ 新增 {name}，更新 Xray 設定...")
        update_xray_clients(clients)

        # Generate QR code
        ip = get_current_ip()
        uri = make_vless_uri(ip, new_uuid, name)
        qr_buf = generate_qr(uri)

        await update.message.reply_photo(qr_buf,
            caption=f"✅ 已新增用戶：{name}\nUUID：{new_uuid}\n\n掃碼連線")

    elif action == 'remove':
        if name not in clients:
            await update.message.reply_text(f"❌ 用戶 {name} 不存在")
            return

        clients[name]['active'] = False
        clients_b64 = base64.b64encode(json.dumps(clients).encode()).decode()
        ssh_run(f"echo '{clients_b64}' | base64 -d > /data/clients.json")
        update_xray_clients(clients)
        await update.message.reply_text(f"✅ 已停用用戶：{name}")

def update_xray_clients(clients):
    """Update Xray config with current active clients and restart"""
    active = [{'id': v['uuid'], 'flow': 'xtls-rprx-vision', 'email': k}
              for k, v in clients.items() if v.get('active', True)]

    # Read current config
    config_raw = ssh_run('cat /etc/xray/config.json')
    try:
        config = json.loads(config_raw)
        config['inbounds'][0]['settings']['clients'] = active
        config_b64 = base64.b64encode(json.dumps(config).encode()).decode()
        ssh_run(f"echo '{config_b64}' | base64 -d > /etc/xray/config.json")
        ssh_run('kill -HUP $(pgrep xray) 2>/dev/null || true')
    except Exception as e:
        print(f"Error updating Xray config: {e}")

@require_admin
async def cmd_clients(update, context):
    clients_raw = ssh_run('cat /data/clients.json 2>/dev/null || echo "{}"')
    try:
        clients = json.loads(clients_raw.strip())
    except:
        clients = {}

    if not clients:
        await update.message.reply_text("目前無用戶")
        return

    lines = ["👥 VPN 用戶列表\n"]
    for name, info in clients.items():
        status = "✅" if info.get('active', True) else "❌"
        lines.append(f"{status} {name}")

    await update.message.reply_text('\n'.join(lines))

@require_admin
async def cmd_getconfig(update, context):
    args = context.args
    name = args[0] if args else 'default'

    clients_raw = ssh_run('cat /data/clients.json 2>/dev/null || echo "{}"')
    try:
        clients = json.loads(clients_raw.strip())
    except:
        clients = {}

    if name not in clients:
        await update.message.reply_text(f"❌ 找不到用戶 {name}\n用戶列表：{', '.join(clients.keys())}")
        return

    ip = get_current_ip()
    uri = make_vless_uri(ip, clients[name]['uuid'], name)
    qr_buf = generate_qr(uri)
    await update.message.reply_photo(qr_buf, caption=f"用戶：{name}\nIP：{ip}")

@require_admin
async def cmd_ping(update, context):
    args = context.args
    if not args:
        await update.message.reply_text("用法：/ping 8.8.8.8")
        return
    host = args[0].replace(';', '').replace('&', '').replace('|', '')
    result = ssh_run(f'ping -c 4 {host} 2>&1')
    await update.message.reply_text(f"```\n{result[:1000]}\n```", parse_mode='Markdown')

@require_admin
async def cmd_traceroute(update, context):
    args = context.args
    if not args:
        await update.message.reply_text("用法：/traceroute 8.8.8.8")
        return
    host = args[0].replace(';', '').replace('&', '').replace('|', '')
    result = ssh_run(f'traceroute -m 15 {host} 2>&1')
    await update.message.reply_text(f"```\n{result[:1500]}\n```", parse_mode='Markdown')

@require_admin
async def cmd_restart(update, context):
    msg = await update.message.reply_text("⏳ 重啟 Xray...")
    result = ssh_run("sh -c 'killall -HUP xray && echo OK || (killall xray; sleep 1; echo restarted)')")
    await msg.edit_text(f"✅ Xray 已重啟\n{result[:200]}")

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
    if not args:
        settings_raw = ssh_run('cat /data/settings.json 2>/dev/null || echo "{}"')
        settings = json.loads(settings_raw.strip() or '{}')
        mode = "開啟" if settings.get('rotation', 'on') != 'off' else "關閉（下載模式）"
        await update.message.reply_text(f"自動換 IP：{mode}")
        return

    param = args[0].lower()
    if param == 'on':
        # dlmode on = disable auto rotation
        ssh_run("python3 -c \"import json; d=json.load(open('/data/settings.json')); d['rotation']='off'; json.dump(d, open('/data/settings.json','w'))\"")
        await update.message.reply_text("✅ 下載模式開啟（自動換 IP 已暫停）")
    elif param == 'off':
        ssh_run("python3 -c \"import json; d=json.load(open('/data/settings.json')); d['rotation']='on'; json.dump(d, open('/data/settings.json','w'))\"")
        await update.message.reply_text("✅ 下載模式關閉（自動換 IP 恢復）")

async def post_init(app):
    """Set bot commands menu"""
    commands = [
        BotCommand("ipswitching", "換 IP（可指定國家，如 /ipswitching us）"),
        BotCommand("ipnow", "查目前 IP"),
        BotCommand("iprotation", "自動換 IP 設定（on/off/數字%）"),
        BotCommand("setlimit", "設定月流量上限（GB）"),
        BotCommand("usage", "本月流量與費用"),
        BotCommand("status", "系統狀態"),
        BotCommand("whoisusing", "目前連線用戶"),
        BotCommand("vpnallowlist", "管理 VPN 用戶（add/remove 名稱）"),
        BotCommand("clients", "列出所有 VPN 用戶"),
        BotCommand("getconfig", "重新取得 QR Code"),
        BotCommand("ping", "Ping 測試"),
        BotCommand("traceroute", "路由追蹤"),
        BotCommand("restart", "重啟 Xray"),
        BotCommand("adminlist", "管理管理員"),
        BotCommand("dlmode", "下載模式（on=暫停自動換IP，off=恢復）"),
    ]
    await app.bot.set_my_commands(commands)

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Register handlers
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
        app.add_handler(CommandHandler(cmd, handler, filters=None))

    print("ip-opt bot starting...")
    app.run_polling()

if __name__ == '__main__':
    main()
