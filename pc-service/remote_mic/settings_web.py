"""设置页（pywebview 弹窗，内嵌本地 HTML）。

界面参考 Windows「应用和功能」：应用列表带图标 / 发布者 / 版本 / 安装时间 / 大小，
发送快捷键改为下拉选择（无 / enter / ctrl+enter）。
"""

from __future__ import annotations

import base64
import io
import logging
import os
import threading

import webview

from . import applist, store, driver
from . import REPO_URL, __version__
from . import files as filex

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
.icoWrap { width:20px; height:20px; display:inline-block; background:#e8eef6; border-radius:3px; flex:0 0 20px; }
.icoWrap img { width:20px; height:20px; border-radius:3px; object-fit:contain; display:block; }
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
    <label>电脑端版本</label><div id="verline" class="hint">—</div>
    <label>项目仓库</label><div class="hint"><a href="#" id="repoLink" style="color:#1a73e8">—</a></div>
  </div>
</div>

<div class="card">
  <h2>音频输出（虚拟声卡）</h2>
  <div class="grid2">
    <label>当前设备</label><div id="audioDevice" class="hint">读取中…</div>
    <label>匹配关键字</label><div id="audioKeyword" class="hint">—</div>
  </div>
  <div id="driverRow" style="display:none; margin-top:12px; padding:10px 12px; border-radius:8px; background:#fff4e5;">
    <div style="color:#8a5300"><b>未检测到 VB-CABLE 虚拟声卡</b>：手机语音必须写进虚拟声卡才能当成麦克风用，装好驱动前语音功能是关掉的。</div>
    <div style="margin-top:10px; display:flex; gap:10px; align-items:center;">
      <button class="primary" onclick="installDriver(this)">安装虚拟声卡</button>
      <span class="hint" id="driverHint">需要管理员权限，会弹一次 UAC</span>
    </div>
  </div>
  <div style="margin-top:12px; display:flex; gap:10px; align-items:center;">
    <button onclick="refreshAudio(this)">重新探测声卡</button>
    <span class="hint">手机语音从虚拟声卡进电脑；插拔过声卡、改了关键字或忽然没声音时点这里重开一次输出流</span>
  </div>
</div>

<div class="card">
  <h2>文件互传</h2>
  <div class="grid2">
    <label>接收目录</label><div id="fileDir" class="hint">读取中…</div>
    <label>限制</label><div class="hint" id="fileLimits">—</div>
  </div>
  <div style="margin-top:12px; display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
    <button onclick="chooseDir()">更改目录</button>
    <button onclick="resetDir()">恢复默认</button>
    <button onclick="openDir()">打开目录</button>
    <button class="primary" onclick="sendFiles(this)">发送文件到手机…</button>
    <span class="hint">手机传来的文件默认存系统「下载」目录；改目录立即生效，不用点保存</span>
  </div>
</div>

<div class="card">
  <h2>已绑定遥控器</h2>
  <table>
    <thead><tr><th style="width:28%">名称</th><th style="width:18%">上次 IP</th><th style="width:16%">绑定时间</th><th style="width:12%">状态</th><th style="width:14%">接收文件</th><th></th></tr></thead>
    <tbody id="remotes"><tr><td colspan="6" class="hint">读取中…</td></tr></tbody>
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
    <tbody id="slots">
      <tr><td class="num">栏位 1</td><td><div class="appcell"><span class="icoWrap"></span><div><div class="appname">读取中…</div></div></div></td><td><span class="hint">—</span></td><td></td></tr>
      <tr><td class="num">栏位 2</td><td><div class="appcell"><span class="icoWrap"></span><div><div class="appname">读取中…</div></div></div></td><td><span class="hint">—</span></td><td></td></tr>
      <tr><td class="num">栏位 3</td><td><div class="appcell"><span class="icoWrap"></span><div><div class="appname">读取中…</div></div></div></td><td><span class="hint">—</span></td><td></td></tr>
      <tr><td class="num">栏位 4</td><td><div class="appcell"><span class="icoWrap"></span><div><div class="appname">读取中…</div></div></div></td><td><span class="hint">—</span></td><td></td></tr>
    </tbody>
  </table>
  <div class="hint" style="margin-top:10px">发送键留空=按前台程序自动匹配，匹配不到用 Enter。</div>
</div>

<footer>
  <button onclick="closeWin()">关闭</button>
  <button class="primary" onclick="saveAll(this)">保存</button>
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
// 用户在下拉框里改过、但还没保存的快捷键：重渲染时必须保留，否则"选了应用回来就丢了"
const pendingHotkeys = {};

function hotkeyOf(slot) {
  if (pendingHotkeys[slot] !== undefined) return pendingHotkeys[slot];
  const el = document.getElementById('hk-' + slot);
  if (el) return el.value;
  const s = state && state.slots.find(x => x.slot === slot);
  return s ? s.hotkey : '';
}

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
  document.getElementById('verline').textContent = 'v' + (state.version || '?');
  const repo = document.getElementById('repoLink');
  repo.textContent = state.repo || '';
  // pywebview 里 target=_blank 不一定能拉起系统浏览器，走 bridge 用默认浏览器打开
  repo.onclick = async (e) => {
    e.preventDefault();
    const res = await pywebview.api.open_repo();
    if (!res.ok) document.getElementById('subline').textContent = '打开仓库失败：' + (res.reason || '');
  };
  renderRemotes();
  renderSlots();
  renderAudio();
  renderFiles();
  loadIcons();          // 图标异步补，不挡首屏
}

async function loadIcons() {
  try {
    const icons = await pywebview.api.get_slot_icons();
    icons.forEach(x => {
      const wrap = document.getElementById('ico-' + x.slot);
      if (wrap && x.icon) wrap.innerHTML = `<img src="${x.icon}" alt="">`;
    });
  } catch (e) { /* 图标失败不影响使用 */ }
}

function renderRemotes() {
  const tb = document.getElementById('remotes');
  tb.innerHTML = '';
  if (!state.remotes.length) {
    tb.innerHTML = '<tr><td colspan="6" class="hint">还没有绑定的遥控器，点下方按钮生成二维码</td></tr>';
    return;
  }
  state.remotes.forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${esc(r.name)}</td><td class="num">${esc(r.last_ip || '—')}</td>
      <td class="num">${esc(r.added || '—')}</td>
      <td>${r.online ? (r.active ? '控制中' : '在线·待命') : '离线'}</td>
      <td><label class="hint" style="cursor:pointer"><input type="checkbox" ${r.receive_files ? 'checked' : ''}
        onchange="toggleRemoteFiles('${esc(r.id)}', this.checked)" style="vertical-align:-2px"> 接收</label></td>
      <td style="text-align:right"><button class="danger" onclick="delRemote('${esc(r.id)}')">删除</button></td>`;
    tb.appendChild(tr);
  });
}

async function toggleRemoteFiles(id, allow) {
  const res = await pywebview.api.set_remote_files(id, allow);
  state.remotes = res.remotes;
  renderRemotes();
  document.getElementById('subline').textContent = allow ? '已允许该遥控器传文件' : '已禁止该遥控器传文件';
}

function renderFiles() {
  const f = state && state.files;
  if (!f) return;
  document.getElementById('fileDir').textContent = f.dir + (f.custom ? '（自定义）' : '（默认下载目录）');
  document.getElementById('fileLimits').textContent =
    `单文件 ≤ ${f.max_file}，一次最多 ${f.max_count} 个`;
}

async function chooseDir() {
  const res = await pywebview.api.choose_dir();
  if (res.files) { state.files = res.files; renderFiles(); }
  document.getElementById('subline').textContent = res.ok ? '保存目录已更新' : '未更改保存目录';
}

async function resetDir() {
  const res = await pywebview.api.reset_dir();
  state.files = res.files; renderFiles();
  document.getElementById('subline').textContent = '已恢复为系统下载目录';
}

async function openDir() {
  const res = await pywebview.api.open_dir();
  if (!res.ok) document.getElementById('subline').textContent = '打开目录失败：' + (res.reason || '');
}

async function sendFiles(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '选择中…'; }
  try {
    const res = await pywebview.api.send_files();
    if (res.ok) document.getElementById('subline').textContent = res.message || '已开始发送';
    else if (res.reason) document.getElementById('subline').textContent = '发送失败：' + res.reason;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '发送文件到手机…'; }
  }
}

function renderSlots() {
  const tb = document.getElementById('slots');
  tb.innerHTML = '';
  state.slots.forEach(s => {
    const tr = document.createElement('tr');
    const name = s.name || '未设置';
    const current = hotkeyOf(s.slot);
    tr.innerHTML = `
      <td class="num">栏位 ${esc(s.slot)}</td>
      <td><div class="appcell"><span class="icoWrap" id="ico-${esc(s.slot)}">${s.icon ? `<img src="${s.icon}" alt="">` : ''}</span><div>
        <div class="appname">${esc(name)}</div>
        <div class="apppath">${esc(s.path || '')}</div>
      </div></div></td>
      <td><select id="hk-${esc(s.slot)}" style="width:100%" onchange="pendingHotkeys['${esc(s.slot)}']=this.value">${hotkeyOptions(current)}</select></td>
      <td><button onclick="openPicker('${esc(s.slot)}')" style="white-space:nowrap">选择应用</button></td>`;
    tb.appendChild(tr);
  });
}

function renderAudio() {
  const a = state && state.audio;
  const dev = document.getElementById('audioDevice');
  const kw = document.getElementById('audioKeyword');
  if (!a || !dev || !kw) return;
  kw.textContent = a.keyword || '—';
  if (a.ready) {
    dev.innerHTML = `<span style="color:#1b7f3a">●</span> ${esc(a.device || '已就绪')}`;
  } else {
    dev.innerHTML = `<span style="color:#b3261e">○</span> 未就绪（没找到匹配设备，语音已禁用）`;
  }
  const row = document.getElementById('driverRow');
  const hint = document.getElementById('driverHint');
  if (row) {
    // 驱动没装才提示；装了但流没打开（关键字/采样率问题）走「重新探测声卡」，不重复引导装驱动
    const needDriver = a.driver_installed === false;
    row.style.display = needDriver ? 'block' : 'none';
  }
  if (hint && a.driver_can_install === false) {
    hint.textContent = '当前环境没有随附 VB-CABLE 安装包，请重新运行完整安装包';
  }
}

async function installDriver(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '安装中…'; }
  document.getElementById('driverHint').textContent = '正在安装，可能需要 10~30 秒；如弹出 UAC 请点“是”';
  try {
    const res = await pywebview.api.install_vbcable();
    state.audio = res.audio || state.audio;
    renderAudio();
    document.getElementById('subline').textContent = res.ok
      ? (res.already ? '虚拟声卡已就绪' : '虚拟声卡已安装，语音应该可以用了')
      : ('安装失败：' + (res.reason || '未知原因'));
    if (res.ok) { state.audio = Object.assign({}, state.audio, { driver_installed: true }); renderAudio(); }
  } catch (e) {
    document.getElementById('subline').textContent = '安装失败：' + (e && e.message ? e.message : e);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '安装虚拟声卡'; }
  }
}

async function refreshAudio(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '探测中…'; }
  try {
    const res = await pywebview.api.refresh_audio();
    state.audio = res;
    renderAudio();
    document.getElementById('subline').textContent = res.ready
      ? `已重开声卡：${res.device}`
      : `重新探测后仍未找到包含「${res.keyword}」的输出设备，语音已禁用`;
  } catch (e) {
    document.getElementById('subline').textContent = '探测失败：' + (e && e.message ? e.message : e);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '重新探测声卡'; }
  }
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

async function saveAll(btn) {
  const payload = {
    computer_name: document.getElementById('computerName').value.trim(),
    slots: state.slots.map(s => ({ slot: s.slot, hotkey: hotkeyOf(s.slot) }))
  };
  if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
  try {
    const res = await pywebview.api.save(payload);
    state = res.state;
    // 保存成功直接关窗口（用户要求：点保存就关，不用再点一次关闭）
    await pywebview.api.close_win();
  } catch (e) {
    document.getElementById('subline').textContent = '保存失败：' + (e && e.message ? e.message : e);
    if (btn) { btn.disabled = false; btn.textContent = '保存'; }
  }
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

    def _slots(self, with_icons: bool = False) -> list[dict]:
        """栏位信息。默认**不扫应用列表、不导出图标**，保证设置页秒开。

        图标由前端拿到骨架后再异步调 get_slot_icons() 补上。
        """
        cfg = self._service.config
        apps = cfg.get("apps") or {}
        slot_hotkey = cfg.get("slot_send_hotkey") or {}
        if with_icons:
            applist.ensure_icons([str(p) for p in apps.values() if p])
        out = []
        for slot in ["1", "2", "3", "4"]:
            path = str(apps.get(slot) or "")
            name = ""
            icon = ""
            if path:
                info = applist.describe_app(path, allow_scan=with_icons)
                name = info.get("name") or ""
                if with_icons:
                    icon = info.get("icon") or ""
            if not name and path:
                name = os.path.splitext(os.path.basename(path))[0]
            hk = slot_hotkey.get(slot) or []
            if isinstance(hk, list):
                hk = "+".join(str(k).lower() for k in hk)
            out.append({"slot": slot, "path": path, "name": name, "icon": icon, "hotkey": str(hk)})
        return out

    def get_slot_icons(self) -> list[dict]:
        """骨架渲染完之后异步补图标（可能要花几百毫秒导图标）。"""
        cfg = self._service.config
        apps = cfg.get("apps") or {}
        paths = [str(p) for p in apps.values() if p]
        applist.ensure_icons(paths)
        return [{"slot": slot, "icon": applist.icon_data_url(str(apps.get(slot) or ""))} for slot in ["1", "2", "3", "4"]]

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
                    "receive_files": bool(r.get("receive_files", True)),
                }
            )
        return rows

    def get_state(self) -> dict:
        cfg = self._service.config
        return {
            "computer_name": str(cfg.get("computer_name") or store.computer_name()),
            "ip": store.lan_ip(),
            "port": int(cfg.get("port", 8765)),
            "version": __version__,
            "repo": REPO_URL,
            "remotes": self._remotes(),
            "slots": self._slots(),
            "audio": self._service.audio_status(),
            "files": self._files_state(),
        }

    def open_repo(self) -> dict:
        """用系统默认浏览器打开项目仓库（设置页里的链接）。"""
        import webbrowser

        try:
            webbrowser.open(REPO_URL)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}

    # ---------------------------------------------------------------- 文件互传

    def _files_state(self) -> dict:
        cfg = self._service.config
        return {
            "dir": str(filex.target_dir(cfg)),
            "default": str(filex.default_dir()),
            "custom": bool(str(cfg.get("download_dir") or "").strip()),
            "max_file": filex.describe_size(filex.MAX_FILE_BYTES),
            "max_count": filex.MAX_FILES_PER_BATCH,
        }

    def choose_dir(self) -> dict:
        """弹系统目录选择框，把手机传来文件的保存位置改掉（立即生效，不用点保存）。"""
        try:
            import webview

            result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as exc:  # noqa: BLE001
            logger.warning("choose dir failed: %s", exc)
            return {"ok": False, "reason": str(exc), "files": self._files_state()}
        if not result:
            return {"ok": False, "files": self._files_state()}
        chosen = result[0] if isinstance(result, (list, tuple)) else str(result)
        cfg = store.load_config()
        cfg["download_dir"] = str(chosen)
        store.save_config(cfg)
        self._service.reload_config()
        logger.info("download dir set to %s", chosen)
        return {"ok": True, "files": self._files_state()}

    def reset_dir(self) -> dict:
        cfg = store.load_config()
        cfg["download_dir"] = ""
        store.save_config(cfg)
        self._service.reload_config()
        return {"ok": True, "files": self._files_state()}

    def open_dir(self) -> dict:
        path = filex.target_dir(self._service.config)
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}

    def set_remote_files(self, remote_id: str, allow: bool) -> dict:
        cfg = store.load_config()
        for r in cfg.get("remotes", []):
            if str(r.get("id")) == str(remote_id):
                r["receive_files"] = bool(allow)
        store.save_config(cfg)
        self._service.reload_config()
        return {"ok": True, "remotes": self._remotes()}

    def send_files(self) -> dict:
        """设置页：选本地文件后推给控制中的遥控器（手机端落到系统下载目录）。"""
        try:
            import webview

            result = webview.windows[0].create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}
        if not result:
            return {"ok": False}
        paths = list(result) if isinstance(result, (list, tuple)) else [str(result)]
        res = self._service.send_files_to_remote(paths)
        if res.get("ok"):
            res["message"] = f"已开始发送 {res.get('count', 0)} 个文件到 {res.get('remote', '手机')}"
        return res

    # ---------------------------------------------------------------- 音频

    def refresh_audio(self) -> dict:
        if hasattr(self._service, "refresh_audio"):
            return self._service.refresh_audio()
        return {"ready": False, "device": "", "keyword": "", "driver_installed": None}

    def install_vbcable(self) -> dict:
        """「安装虚拟声卡」：调 VB-CABLE 静默安装，装完立刻重新探测一次输出流。"""
        res = driver.install()
        if res.get("ok"):
            # 驱动节点出来后再打开一次输出流，省得用户还要手动点「重新探测声卡」
            try:
                self._service.refresh_audio()
            except Exception as exc:  # noqa: BLE001
                logger.warning("refresh audio after driver install failed: %s", exc)
        res["audio"] = self._service.audio_status()
        return res

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
        # 栏位应用可能变了，把新的名称/图标推给已连的手机
        if hasattr(self._service, "push_apps"):
            self._service.push_apps()
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
