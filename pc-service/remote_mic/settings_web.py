"""设置页（pywebview 弹窗，内嵌本地 HTML）。

界面参考 Windows「应用和功能」：应用列表带图标 / 发布者 / 版本 / 安装时间 / 大小，
发送快捷键改为下拉选择（无 / enter / ctrl+enter）。
"""

from __future__ import annotations

import base64
import io
import logging
import threading

import webview

from . import applist, store

logger = logging.getLogger(__name__)

_window_open = False

WINDOW_TITLE = "手机遥控麦 · 设置"


def show_hidden_windows(title_keyword: str = WINDOW_TITLE, tries: int = 40, interval: float = 0.2) -> None:
    """把本进程里被 STARTUPINFO(SW_HIDE) 连带隐藏的顶层窗口强制显示。

    服务是由 wscript 以隐藏窗口方式起的 python.exe，进程启动时的
    STARTUPINFO.wShowWindow = SW_HIDE 会让进程创建的第一个窗口也被隐藏，
    表现就是点了「设置…」没有反应。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    pid = ctypes.windll.kernel32.GetCurrentProcessId()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect() -> list[int]:
        hwnds: list[int] = []

        def cb(hwnd, _lparam):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                buf = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, buf, 512)
                if title_keyword in buf.value:
                    hwnds.append(hwnd)
            return True

        user32.EnumWindows(callback_type(cb), 0)
        return hwnds

    for _ in range(tries):
        targets = collect()
        if targets:
            for hwnd in targets:
                user32.ShowWindow(hwnd, 5)  # SW_SHOW
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
            logger.info("forced show window: %s", targets)
            return
        threading.Event().wait(interval)
    logger.warning("settings window not found for force-show (%s)", title_keyword)

HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>手机遥控麦 · 设置</title>
<style>
:root { --accent:#0067c0; --line:#e6e6e6; --muted:#616161; }
* { box-sizing:border-box; }
body {
  margin:0; background:#f3f3f3; color:#1b1b1b;
  font-family:"Segoe UI","Microsoft YaHei UI","Microsoft YaHei",sans-serif; font-size:13.5px;
}
header {
  position:sticky; top:0; z-index:5; background:#fff; border-bottom:1px solid var(--line);
  padding:14px 24px; display:flex; align-items:center; justify-content:space-between;
}
header h1 { font-size:17px; font-weight:600; margin:0; }
header .sub { color:var(--muted); font-size:12.5px; margin-top:2px; }
.card { background:#fff; border:1px solid var(--line); border-radius:8px; margin:14px 24px; padding:14px 18px; }
.card h2 { font-size:14px; font-weight:600; margin:0 0 10px; }
.grid2 { display:grid; grid-template-columns:120px 1fr; gap:10px 12px; align-items:center; }
input[type=text], select {
  font:inherit; padding:6px 10px; border:1px solid #d8d8d8; border-radius:6px; background:#fff; color:#1b1b1b;
}
input[type=text]:focus, select:focus { outline:2px solid #c7e0ff; border-color:var(--accent); }
button {
  font:inherit; padding:6px 14px; border-radius:6px; border:1px solid #d0d0d0; background:#fbfbfb; cursor:pointer;
}
button:hover { background:#f0f0f0; }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
button.primary:hover { background:#0b57a4; }
button.danger { color:#b3261e; }
table { width:100%; border-collapse:collapse; }
thead th {
  text-align:left; font-size:12px; color:var(--muted); font-weight:600;
  padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap;
}
tbody td { padding:7px 8px; border-bottom:1px solid #f3f3f3; vertical-align:middle; }
tbody tr:hover { background:#f0f6ff; }
td.num { color:var(--muted); white-space:nowrap; }
.icon { width:20px; height:20px; vertical-align:-4px; border-radius:3px; object-fit:contain; }
.appcell { display:flex; align-items:center; gap:10px; }
.appname { font-size:13px; }
.apppath { color:var(--muted); font-size:11.5px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:520px; }
.qrwrap { display:none; margin-top:10px; padding-top:10px; border-top:1px dashed var(--line); align-items:center; gap:16px; }
.qrwrap img { width:190px; height:190px; image-rendering:pixelated; border:1px solid var(--line); border-radius:6px; }
footer {
  position:sticky; bottom:0; background:#fff; border-top:1px solid var(--line);
  padding:12px 24px; display:flex; justify-content:flex-end; gap:10px;
}
/* 应用选择弹层 */
.mask { position:fixed; inset:0; background:rgba(0,0,0,.28); display:none; z-index:20; }
.panel {
  position:absolute; left:50%; top:50%; transform:translate(-50%,-50%);
  width:820px; max-width:94vw; height:620px; max-height:88vh; background:#fff;
  border-radius:10px; box-shadow:0 16px 48px rgba(0,0,0,.28); display:flex; flex-direction:column; overflow:hidden;
}
.panel .ptitle { padding:14px 18px; border-bottom:1px solid var(--line); font-weight:600; }
.panel .psearch { padding:10px 18px; border-bottom:1px solid var(--line); }
.panel .psearch input { width:100%; }
.panel .plist { flex:1; overflow:auto; }
.panel .plist table { width:100%; }
.panel .plist thead th { position:sticky; top:0; background:#fff; z-index:1; }
tbody tr.pick { cursor:pointer; }
tbody tr.sel { background:#cfe6ff !important; }
.panel .pfoot { padding:10px 18px; border-top:1px solid var(--line); display:flex; justify-content:space-between; align-items:center; }
.hint { color:var(--muted); font-size:12px; }
</style>
</head>
<body>
<header>
  <div>
    <h1>手机遥控麦 · 设置</h1>
    <div class="sub" id="subline">—</div>
  </div>
  <div class="hint">配置保存在电脑端 config.json</div>
</header>

<div class="card">
  <h2>本机</h2>
  <div class="grid2">
    <label>电脑名称</label><div><input type="text" id="computerName" style="width:280px"></div>
    <label>局域网地址</label><div id="addrline" class="hint">—</div>
  </div>
</div>

<div class="card">
  <h2>已绑定遥控器</h2>
  <table>
    <thead><tr><th style="width:34%">名称</th><th style="width:22%">上次 IP</th><th style="width:20%">绑定时间</th><th style="width:14%">状态</th><th></th></tr></thead>
    <tbody id="remotes"></tbody>
  </table>
  <div style="margin-top:12px; display:flex; gap:10px; align-items:center;">
    <button class="primary" onclick="createBinding()">生成绑定二维码</button>
    <span class="hint">手机扫码即完成绑定；删除后需重新扫码</span>
  </div>
  <div class="qrwrap" id="qrwrap">
    <img id="qrimg" alt="绑定二维码">
    <div>
      <div id="qrtext" class="hint"></div>
      <div class="hint" style="margin-top:6px">二维码含电脑名称、IP、端口与绑定 token</div>
    </div>
  </div>
</div>

<div class="card">
  <h2>四个应用栏位</h2>
  <table>
    <thead><tr><th style="width:64px">栏位</th><th>应用</th><th style="width:180px">发送快捷键</th><th style="width:118px"></th></tr></thead>
    <tbody id="slots"></tbody>
  </table>
  <div class="hint" style="margin-top:10px">发送键留空=按前台程序自动匹配，匹配不到用 Enter。</div>
</div>

<footer>
  <button onclick="closeWin()">关闭</button>
  <button class="primary" onclick="saveAll()">保存</button>
</footer>

<!-- 应用选择弹层 -->
<div class="mask" id="mask">
  <div class="panel">
    <div class="ptitle">选择应用</div>
    <div class="psearch"><input type="text" id="search" placeholder="搜索应用名称 / 路径" oninput="filterApps()"></div>
    <div class="plist">
      <table>
        <thead><tr><th style="width:44px"></th><th style="width:30%">名称</th><th style="width:26%">发布者</th><th style="width:14%">版本</th><th style="width:14%">安装时间</th><th style="width:12%">大小</th></tr></thead>
        <tbody id="approws"></tbody>
      </table>
    </div>
    <div class="pfoot">
      <div class="hint" id="appcount">—</div>
      <div style="display:flex; gap:10px; align-items:center;">
        <span class="hint" id="pickedLabel">未选择</span>
        <button onclick="browseExe()">浏览 exe…</button>
        <button onclick="closePicker()">取消</button>
        <button class="primary" onclick="confirmPick()">确定</button>
      </div>
    </div>
  </div>
</div>

<script>
let state = null, apps = null, pickTarget = null, picked = null;

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function iconTag(icon) {
  return icon ? `<img class="icon" src="${icon}">` : `<span class="icon" style="display:inline-block;background:#e8eef6;border-radius:3px"></span>`;
}
function hotkeyOptions(current) {
  const base = ['', 'enter', 'ctrl+enter'];
  const opts = base.includes(current) ? base : base.concat([current]);
  return opts.map(v => `<option value="${esc(v)}"${v === current ? ' selected' : ''}>${v === '' ? '无' : esc(v)}</option>`).join('');
}

async function boot() {
  state = await pywebview.api.get_state();
  document.getElementById('subline').textContent = '电脑名称与绑定信息仅在局域网内使用';
  document.getElementById('computerName').value = state.computer_name || '';
  document.getElementById('addrline').textContent = `${state.ip}:${state.port}`;
  renderRemotes();
  renderSlots();
}

function renderRemotes() {
  const tb = document.getElementById('remotes');
  tb.innerHTML = '';
  if (!state.remotes.length) {
    tb.innerHTML = '<tr><td colspan="5" class="hint">还没有绑定的遥控器，点下方按钮生成二维码</td></tr>';
    return;
  }
  state.remotes.forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${esc(r.name)}</td><td class="num">${esc(r.last_ip || '—')}</td>
      <td class="num">${esc(r.added || '—')}</td>
      <td>${r.online ? (r.active ? '控制中' : '在线·待命') : '离线'}</td>
      <td style="text-align:right"><button class="danger" onclick="delRemote('${esc(r.id)}')">删除</button></td>`;
    tb.appendChild(tr);
  });
}

function renderSlots() {
  const tb = document.getElementById('slots');
  tb.innerHTML = '';
  state.slots.forEach(s => {
    const tr = document.createElement('tr');
    const name = s.name || '未设置';
    tr.innerHTML = `
      <td class="num">栏位 ${esc(s.slot)}</td>
      <td><div class="appcell">${iconTag(s.icon)}<div>
        <div class="appname">${esc(name)}</div>
        <div class="apppath">${esc(s.path || '')}</div>
      </div></div></td>
      <td><select id="hk-${esc(s.slot)}" style="width:100%">${hotkeyOptions(s.hotkey)}</select></td>
      <td><button onclick="openPicker('${esc(s.slot)}')" style="white-space:nowrap">选择应用</button></td>`;
    tb.appendChild(tr);
  });
}

async function createBinding() {
  const res = await pywebview.api.create_binding();
  state.remotes = res.remotes;
  state.qr = res.qr;
  state.qr_text = res.qr_text;
  renderRemotes();
  document.getElementById('qrwrap').style.display = 'flex';
  document.getElementById('qrimg').src = res.qr;
  document.getElementById('qrtext').innerHTML = res.qr_text;
}

async function delRemote(id) {
  const res = await pywebview.api.delete_binding(id);
  state.remotes = res.remotes;
  renderRemotes();
}

async function openPicker(slot) {
  pickTarget = slot;
  picked = null;
  document.getElementById('pickedLabel').textContent = '未选择';
  document.getElementById('mask').style.display = 'block';
  if (!apps) {
    document.getElementById('appcount').textContent = '正在读取已安装应用…';
    apps = await pywebview.api.list_apps();
  }
  filterApps();
}

function filterApps() {
  const kw = document.getElementById('search').value.trim().toLowerCase();
  const tb = document.getElementById('approws');
  tb.innerHTML = '';
  const list = apps.filter(a => !kw || a.name.toLowerCase().includes(kw) || (a.exe || '').toLowerCase().includes(kw));
  list.forEach(a => {
    const tr = document.createElement('tr');
    tr.className = 'pick';
    tr.dataset.exe = a.exe;
    tr.dataset.name = a.name;
    tr.innerHTML = `<td>${iconTag(a.icon)}</td><td>${esc(a.name)}</td><td>${esc(a.publisher || '')}</td>
      <td class="num">${esc(a.version || '')}</td><td class="num">${esc(a.install_date || '')}</td><td class="num">${esc(a.size || '')}</td>`;
    tr.onclick = () => {
      picked = a;
      [...tb.children].forEach(c => c.classList.remove('sel'));
      tr.classList.add('sel');
      document.getElementById('pickedLabel').textContent = '已选：' + a.name;
    };
    tr.ondblclick = () => { picked = a; confirmPick(); };
    tb.appendChild(tr);
  });
  document.getElementById('appcount').textContent = `共 ${list.length} 个应用`;
}

function closePicker() {
  document.getElementById('mask').style.display = 'none';
  document.getElementById('search').value = '';
}

async function confirmPick() {
  if (!picked) return;
  const res = await pywebview.api.set_slot_app(pickTarget, picked.exe, picked.name);
  state.slots = res.slots;
  renderSlots();
  closePicker();
}

async function browseExe() {
  const res = await pywebview.api.browse_exe();
  if (!res) return;
  picked = { name: res.name, exe: res.exe };
  const res2 = await pywebview.api.set_slot_app(pickTarget, res.exe, res.name);
  state.slots = res2.slots;
  renderSlots();
  closePicker();
}

async function saveAll() {
  const payload = {
    computer_name: document.getElementById('computerName').value.trim(),
    slots: state.slots.map(s => ({ slot: s.slot, hotkey: document.getElementById('hk-' + s.slot).value }))
  };
  const res = await pywebview.api.save(payload);
  document.getElementById('subline').textContent = res.message;
  state = res.state;
  renderRemotes();
  renderSlots();
}

function closeWin() { pywebview.api.close_win(); }

function showErr(e) {
  document.getElementById('subline').textContent = '加载失败：' + (e && e.message ? e.message : e);
}

function init() {
  const run = () => boot().catch(showErr);
  if (window.pywebview && window.pywebview.api) run();
  else window.addEventListener('pywebviewready', run);
}
init();
</script>
</body>
</html>
"""


class SettingsApi:
    def __init__(self, service):
        self._service = service

    # ---------------------------------------------------------------- 状态

    def _slots(self) -> list[dict]:
        cfg = self._service.config
        apps = cfg.get("apps") or {}
        slot_hotkey = cfg.get("slot_send_hotkey") or {}
        applist.ensure_icons([str(p) for p in apps.values() if p])
        out = []
        for slot in ["1", "2", "3", "4"]:
            path = str(apps.get(slot) or "")
            name = ""
            icon = ""
            if path:
                info = applist.describe_app(path)
                name = info.get("name") or ""
                icon = info.get("icon") or ""
            hk = slot_hotkey.get(slot) or []
            if isinstance(hk, list):
                hk = "+".join(str(k).lower() for k in hk)
            out.append({"slot": slot, "path": path, "name": name, "icon": icon, "hotkey": str(hk)})
        return out

    def _remotes(self) -> list[dict]:
        online = {c[0]: c[2] for c in self._service.active_remote_names()}
        rows = []
        for r in self._service.config.get("remotes", []):
            rid = str(r.get("id", ""))
            rows.append(
                {
                    "id": rid,
                    "name": str(r.get("name", "")),
                    "last_ip": str(r.get("last_ip", "")),
                    "added": str(r.get("added", "")),
                    "online": rid in online,
                    "active": bool(online.get(rid)),
                }
            )
        return rows

    def get_state(self) -> dict:
        cfg = self._service.config
        return {
            "computer_name": str(cfg.get("computer_name") or store.computer_name()),
            "ip": store.lan_ip(),
            "port": int(cfg.get("port", 8765)),
            "remotes": self._remotes(),
            "slots": self._slots(),
        }

    def list_apps(self) -> list[dict]:
        return applist.cached_entries()

    # ---------------------------------------------------------------- 遥控器

    def create_binding(self) -> dict:
        remote = self._service.create_binding("新遥控器")
        payload = store.qr_payload(self._service.config, remote["token"])
        image = store.make_qr_image(payload)
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        name = self._service.config.get("computer_name") or store.computer_name()
        text = f"<b>{name}</b><br>{store.lan_ip()}:{self._service.config.get('port', 8765)}"
        return {"remotes": self._remotes(), "qr": data_url, "qr_text": text}

    def delete_binding(self, remote_id: str) -> dict:
        self._service.delete_binding(remote_id)
        return {"remotes": self._remotes()}

    # ---------------------------------------------------------------- 栏位

    def set_slot_app(self, slot: str, exe: str, name: str) -> dict:
        cfg = self._service.config
        cfg.setdefault("apps", {})[str(slot)] = exe
        return {"slots": self._slots(), "name": name}

    def browse_exe(self) -> dict | None:
        result = webview.windows[0].create_file_dialog(webview.OPEN_DIALOG, file_types=("可执行文件 (*.exe)",))
        if not result:
            return None
        import os

        path = result[0] if isinstance(result, (list, tuple)) else result
        return {"exe": path, "name": os.path.splitext(os.path.basename(path))[0]}

    # ---------------------------------------------------------------- 保存

    def save(self, payload: dict) -> dict:
        cfg = self._service.config
        name = str(payload.get("computer_name") or "").strip()
        if name:
            cfg["computer_name"] = name
        slot_hotkey = cfg.setdefault("slot_send_hotkey", {})
        for item in payload.get("slots", []):
            slot = str(item.get("slot", ""))
            raw = str(item.get("hotkey") or "").strip().lower()
            if raw:
                slot_hotkey[slot] = [p for p in raw.replace("+", " ").split() if p]
            else:
                slot_hotkey.pop(slot, None)
        store.save_config(cfg)
        self._service.reload_config()
        logger.info("settings saved: %s", cfg.get("slot_send_hotkey"))
        return {
            "state": self.get_state(),
            "message": "已保存，电脑端已热重载",
        }

    def close_win(self) -> None:
        try:
            webview.windows[0].destroy()
        except Exception:  # noqa: BLE001
            pass


def open_settings_window(service) -> None:
    """必须在主线程调用（pywebview 限制）。"""
    global _window_open
    if _window_open:
        return
    _window_open = True
    try:
        api = SettingsApi(service)
        webview.create_window(
            WINDOW_TITLE,
            html=HTML,
            js_api=api,
            width=1000,
            height=780,
            min_size=(880, 620),
        )
        threading.Thread(target=show_hidden_windows, daemon=True).start()
        webview.start()
    finally:
        _window_open = False
