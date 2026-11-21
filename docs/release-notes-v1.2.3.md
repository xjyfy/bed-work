# v1.2.3 · 修「反复断线重连 / 截屏、传文件没反应」

发布日期：2026-11-21

## 下载

- **手机端**：`RemoteMic-1.2.3.apk`（Android 8.0+，约 6 MB，`versionCode=6`）
- **电脑端**：`RemoteMicSetup-1.1.2.exe`（Windows 10/11 x64，约 28 MB）

> **两端必须一起升**：电脑端旧版（1.1.1 及更早）不认识"被顶掉"的 close code，也没有回包兜底重发，只升手机端仍会复现。

## 修的是什么现象

按键能控制电脑，但**截屏点了没反应、传文件提示未连接/超时**；电脑端日志里能看到每分钟好几次 `remote reconnect`，每次会话恰好 17～20 秒，`replace stale connection` 与 `send failed (screenshot)` 交替出现。

## 根因

手机端**同时维持了多条 WebSocket 连接**（实测一台手机同时 4 条 ESTABLISHED；`force-stop` 后 4 条同时消失，证明都在客户端进程里）。而电脑端对同一台遥控器是「踢旧保新」，于是：

1. 多条连接互相把对方顶掉，被顶掉的那条收不到任何 pong（`after 0 successful ping/pongs`）→ 客户端判定断线 → 重连 → 再顶别人，形成稳定复现的重连风暴；
2. 截屏/文件回包要等电脑端处理完再发，此时目标连接刚被顶掉 → `send failed ... replaced; no close frame received` → 手机端永远收不到，表现为"没反应"；
3. 键盘消息碰巧落在活着的那条上，所以按键看起来是好的。

多连接的来源：`open()` 只是 `cancel()` 旧 socket 再建新的，而 cancel 是异步的；`wakeUp()` 里那个 3 秒探活回调是匿名 Runnable、从不清除，几轮亮屏解锁后叠了好几个，各自都会触发一次建连；再叠加 `onFailure → 3 秒固定重连`。

## 三处修改

**手机端（`RemoteWsClient.kt`）**

- **建连单飞 + 代际号**：`AtomicBoolean connecting` 保证同一时刻只有一次建连在飞，`AtomicInteger generation` 让旧 socket 的迟到回调全部作废；非强制重连时只要已有一条 socket 就直接跳过。
- **收旧 socket 用 `cancel()`**：不再用优雅 `close()`（那要等对端 close 帧，socket 会以 ESTABLISHED 挂很久）。
- **重连指数退避** 3s→5s→10s→20s→30s，连上后归零；收到服务端的「被顶掉」close code（4001）时退让 10 秒再重连，主动掐断互踢。
- **探活回调收成字段**并 `removeCallbacks`，不再随亮屏次数累积。

**电脑端（`server.py`）**

- 踢旧连接改用专用 close code **4001 / "replaced"**，客户端据此识别"是被顶掉"而不是网络故障；`_close_quietly` 超时后直接 `transport.abort()`，不留半死的 ESTABLISHED。
- `_send_json` 增加**回包兜底**：截屏、文件握手这类「请求-回包」消息发送失败时，重发一次到当前最新连接，兜住"发送前一刻被顶掉"的窗口。

## 验证（一加 13 / ColorOS 17 + Windows 11，真机）

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 连接数 | 4 条 ESTABLISHED | **1 条** |
| 静置 60 秒 | 每分钟 5～11 次重连 | **0 次重连 / 0 次踢旧 / 0 次发送失败** |
| 连点 5 次截屏 | 大量 `send failed (screenshot)` | 5 次全部 `screenshot sent`，手机端正常弹出查看器 |
| 反复熄屏→亮屏解锁 3 轮 | 触发风暴 | 每轮 1:1 重连（`session 5.9 s` 后正常重连），始终 1 条连接 |

## 升级方式

- 手机端：1.2.1 起同 release 签名，**直接覆盖安装**，绑定与 token 保留。
- 电脑端：覆盖安装即可，`config.json` 与绑定 token 不动；安装器会自动关掉正在运行的托盘进程。
- 熄屏后连接仍会断（不自建后台保活），**亮屏即时重连一次**属设计行为，不是故障。

## 已知限制

- 断网/熄屏期间的操作不会补发（协议无重放）。
- 若两台手机绑同一台电脑的同一个 token，仍会互相把对方顶掉——这是「一个 token 只有一个控制端」的既有设计。
