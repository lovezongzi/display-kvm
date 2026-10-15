# display-kvm

用软件切换显示器的输入源，替代伸手去按显示器上的实体按钮。

针对的场景：一台显示器同时接了 Windows 主机和 Mac 主机，两台**各有独立的键鼠**。  
你碰哪台的键鼠，显示器就自动跳到哪台。

---

## ⚡ 快速开始（安装包版）

不碰 Python 的最短路径。安装包位于 `dist/`：

| 平台      | 文件                                           | 说明                                                                                     |
| ------- | -------------------------------------------- | -------------------------------------------------------------------------------------- |
| Windows | `dist/installer/Display-KVM-Setup-1.0.0.exe` | 双击安装。**已在本机完整实测**（安装→运行→切换→卸载全链路）                                                      |
| macOS   | `Display-KVM-1.0.0.dmg`                      | **无法在 Windows 上交叉构建**，需在 Mac 上跑 `bash packaging/build_mac.sh` 一键生成（自动装依赖、打 .app、做 dmg） |

Windows 安装包做了什么：

- 装到 `%LOCALAPPDATA%\Programs\display-kvm`，每用户安装，**不需要管理员权限**
- 开始菜单快捷方式 + 可选桌面快捷方式 + 可选开机自启（卸载时自动清理）
- 托盘常驻：右键图标 → 看状态 / 立即切到本机 / 自动切换开关 / 打开配置 / 打开日志 / 退出
- 首次启动自动生成配置到 `%APPDATA%\display-kvm\kvm.json`，  
  并**自动探测**共用显示器的分辨率和当前输入源——通常只需改 `token` 和 `peer_input_source` 两个值
- 自带单实例保护，重复双击只会提示"已在运行"
- 附带 `display-kvm-ctl.exe`（控制台版控制客户端，绑热键用）

双端各装好后，必做三步：

1. 两端 `token` 改成**完全一致**的随机串（Windows 端首次生成的是随机值，复制到 Mac 端即可）
2. 改对两端的 `my_input_source` / `peer_input_source`（见下面第四节「找输入口」）
3. 先保持 `dry_run: true` 跑几轮看日志，确认无误后改 `false`

> 配置里带 `_readme` 字段，打开配置文件就有说明。

---

## 一、它凭什么能行

显示器内部有颗控制芯片。所有输入口切换、亮度、对比度，都走 VESA 的 **MCCS 协议**，  
通过视频线里的 **DDC 通道**传命令 —— 具体是 `VCP 0x60 (Input Source)`。  
你按显示器实体按钮，是让显示器的 MCU 执行这条命令；软件做的事，  
只是把同一条命令替你说出去。所以本质上不是"绕过硬件"，而是"换个手按"。

已在本机实测（Windows 端，2560×1440 主屏）：

| 项目          | 结果                                                        |
| ----------- | --------------------------------------------------------- |
| DDC/CI 可读可写 | 是                                                         |
| 当前输入源       | `0x10` (DisplayPort-2)                                    |
| 可选输入源       | `0x11` HDMI-1 / `0x12` HDMI-2 / `0x0F` DP-1 / `0x10` DP-2 |
| 切换耗时        | 显示器重锁信号 1–3 秒（物理必然，无法消除）                                  |

### 一个反直觉的关键点

**切换命令只能由「当前正被显示器选中」的那台机器发出。**

DDC 通道挂在活跃的视频链路上。显示器一旦切走，另一台机器的链路就静默了，  
它发不出任何命令。所以：

> 想切过去的机器，往往正好是发不出命令的那台。

本项目的解法是**委托**：

- 我检测到本机键鼠有活动，但我当前不在显示器上 → 通过网络请对端（它正被显示）替我说
- 我当前正在显示器上，要切走 → 本地直接发 DDC

因此**两台都必须装**，且必须能互相通信。

---

## 二、文件说明

| 文件                 | 作用                                                        |
| ------------------ | --------------------------------------------------------- |
| `app.py`           | **打包入口 / 托盘外壳**：自动配置、托盘常驻、开机自启、日志落盘                       |
| `ddc_win.py`       | Windows 端 DDC/CI 控制核心。纯 ctypes 调系统 `dxva2.dll`，**零第三方依赖** |
| `platforms.py`     | 跨平台适配层：DDC 控制 / 显示器在不在 / 键鼠空闲时长                           |
| `agent.py`         | 两端主程序：角色判定、输入活动检测、局域网委托、本地控制口                             |
| `kvmctl.py`        | 本地控制客户端，可绑热键                                              |
| `kvm.windows.json` | Windows 端配置模板（命令行方式用）                                     |
| `kvm.mac.json`     | macOS 端配置模板（命令行方式用）                                       |
| `selftest/`        | 单机自测，用假实例验证网络与委托逻辑                                        |
| `packaging/`       | 打包：spec、Inno Setup 脚本、图标生成、Mac 一键构建脚本                     |

---

## 三、Windows 端安装（命令行方式）

> 用了安装包（`Display-KVM-Setup-1.0.0.exe`）的话，**跳过这一节**，  
> 直接看「⚡ 快速开始」里的必做三步。以下是从源码跑的方式。

不需要 `pip install` 任何东西。

### 1. 先确认显示器支持

```bash
python ddc_win.py list
```

应当列出你的显示器、当前输入源、以及可选的输入源列表。  
如果这里读不到，后面全都不用试了（先去显示器 OSD 里把 `DDC/CI` 打开）。

### 2. 找出 Mac 接在哪个输入口

`peer_input_source` 是「显示器显示 Mac 时用的输入源」，你必须知道它。

最直接的办法：手动按显示器按钮切到 Mac，然后看显示器 OSD 里高亮的是哪个口  
（HDMI-1 / HDMI-2 / DP-1 / DP-2），对照下表填：

| 输入口           | 值      |
| ------------- | ------ |
| DisplayPort-1 | `0x0F` |
| DisplayPort-2 | `0x10` |
| HDMI-1        | `0x11` |
| HDMI-2        | `0x12` |

或者用命令行试着切（**会真的切换画面，慎用**）：

```bash
python ddc_win.py set 1 0x11 --restore-after 8
```

`--restore-after 8` 会在 8 秒后自动切回，是安全网 —— 万一那个口没信号，  
你也不用去摸显示器按钮。

### 3. 填配置

复制 `kvm.windows.json` 改成 `kvm.json`，改这几个字段：

- `my_input_source`：显示器显示**本机**时用的输入源（你刚才 `list` 看到的当前值）
- `peer_input_source`：Mac 那个口
- `token`：随便一串只有你知道的字符，**两端要一致**
- `match_width` / `match_height`：共用显示器的分辨率，用来定位它
- `peer_ip`：留空会自动局域网广播发现；跨网段或发现不了就手填对端 IP
- `dry_run`：**先保持 `true`**

### 4. 先干跑

```bash
python agent.py --config kvm.json
```

另开一个终端：

```bash
python kvmctl.py status
python kvmctl.py switch        # 切到本机
python kvmctl.py switch-peer   # 切到对端
```

看日志里打的是 `[dry-run] 本应把输入源切到 0xXX` —— 逻辑对了。

### 5. 关掉 dry_run 正式跑

把 `"dry_run": false`，重跑。

### 6. 开机自启

用任务计划程序，登录时启动：

```
schtasks /create /tn "display-kvm" ^
  /tr "\"C:\Python313\pythonw.exe\" \"C:\path\to\display-kvm\agent.py\" --config \"C:\path\to\display-kvm\kvm.json\"" ^
  /sc onlogon /rl highest /f
```

`pythonw.exe` 是无窗口版本，不会弹黑框。

---

## 四、macOS 端安装

### 0. 有 dmg 的话（推荐）

dmg 无法在 Windows 上构建。把整个 `display-kvm/` 文件夹拷到 Mac 上，跑一条命令：

```bash
bash packaging/build_mac.sh
```

脚本会自动：建隔离虚拟环境 → 装 pyinstaller/pystray/pyobjc → 找或下载 m1ddc 并打包进 app →  
构建 `Display KVM.app`（菜单栏应用，不占 Dock）→ ad-hoc 签名 → 产出 `Display-KVM-1.0.0.dmg`  
（带拖拽到 Applications 的布局）。产物在 `dist/` 下。

之后按「⚡ 快速开始」的必做三步配置即可。

**前置条件：Python ≥ 3.10。** 脚本优先找 `/opt/homebrew/bin/python3`，找不到就回退 PATH 里的
`python3`，然后硬校验版本。系统自带的 `/usr/bin/python3` 是 3.9，会直接报错退出。补一个即可：

```bash
brew install python        # 或把任意 3.10+ 的解释器放到 PATH 前面
```

> **macOS 预发布版本（如 27.x）上 Homebrew 常常没有现成 bottle**，`brew install` 会退化成长时间
> 源码编译、甚至因 Command Line Tools 版本判定直接失败。这时不必跟 brew 较劲 —— m1ddc 只是
> Objective-C + CoreDisplay，用本机 Command Line Tools 就能编：
>
> ```bash
> curl -sL -o m1ddc.zip https://codeload.github.com/waydabber/m1ddc/zip/refs/tags/v1.2.0
> unzip -q m1ddc.zip && cd m1ddc-1.2.0 && make
> install -m 0755 m1ddc /opt/homebrew/bin/m1ddc
> ```

### 1. 装 DDC 工具（命令行方式）

```bash
brew install m1ddc          # Apple Silicon
# Intel Mac 用 ddcctl:  brew install ddcctl
```

验证（**必须在显示器正显示 Mac 的时候做**）：

```bash
m1ddc display list
m1ddc display 1 get input   # 能返回数字 = DDC 通了
```

**注意：`get input` 返回的是「显示器此刻正在显示谁的口」，而不是「本机接在哪个口」。**
Apple Silicon 上 m1ddc 走的是私有 CoreDisplay 代理，显示器切到对端之后**依然能读能写**，
所以这个数字必须结合「现在屏幕上是谁」一起解读 —— 详见下一节。

**如果这条报错，说明你的转接设备吃掉了 DDC 通道。**  
雷电/USB4 扩展坞一般没问题；二三十块的 USB-C 转 HDMI 小线大概率不行。  
这种情况下本项目对 Mac 侧无效，只能从 Windows 侧单向切过去。

### 2. 填配置

复制 `kvm.mac.json` 改成 `kvm.json`：

- `my_input_source`：Mac 那个口（和 Windows 端 `peer_input_source` 一致）
- `peer_input_source`：Windows 那个口
- `token`：和 Windows 端一致
- `display_index`：`m1ddc display list` 里的序号
- `ddc_set_cmd` 里的 `{value}` 是十进制，已处理好

> **这两个口填反了，表现是「点『切到本机』反而切到对端去」。**
> 由于上面说的原因，`m1ddc get input` **不能**直接当作「本机的口」：如果读的时候显示器正显示
> 对端，读到的就是对端的口。所以务必按这个顺序确认：
>
> 1. 手动按显示器按钮，让屏幕显示 Mac；
> 2. 这时执行 `m1ddc get input`，得到的才是 Mac 的口，写进 `my_input_source`；
> 3. 对端那个口 = 显示器上剩下的另一个。
>
> 还有个跨平台通用的判活技巧：**往一个口下发 `set input` 后观察读数是否自己退回**。
> 空的输入口会在几秒内被显示器自动退回上一个有信号的口；有信号的活口会稳稳停住。

### 3. 跑起来

```bash
python3 agent.py --config kvm.json          # 先 dry_run: true
python3 kvmctl.py status
```

### 4. 开机自启（launchd）

存成 `~/Library/LaunchAgents/cn.displaykvm.agent.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>cn.displaykvm.agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/你的用户名/display-kvm/agent.py</string>
    <string>--config</string>
    <string>/Users/你的用户名/display-kvm/kvm.json</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/display-kvm.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/display-kvm.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/cn.displaykvm.agent.plist
```

---

## 五、参数怎么调

都在配置 JSON 里：

| 字段                        | 默认   | 说明                              |
| ------------------------- | ---- | ------------------------------- |
| `arm_after_idle_seconds`  | 3.0  | 本机键鼠静默多久之后，才进入「待触发」状态。调大可减少误触   |
| `fire_below_idle_seconds` | 0.45 | 空闲时长低于这个值就判定为「刚有输入」。调小更灵敏但要小心噪声 |
| `cooldown_seconds`        | 6.0  | 两次切换的最小间隔，防止来回抖                 |
| `role_poll_seconds`       | 2.0  | 多久探测一次「显示器在不在我这儿」               |
| `auto_switch`             | true | 关掉就只剩手动 `kvmctl switch`         |

**觉得太容易误触**：把 `arm_after_idle_seconds` 提到 8–10 秒。

---

## 六、控制命令

```bash
python kvmctl.py status        # 看状态
python kvmctl.py switch        # 立即把显示器切到本机
python kvmctl.py switch-peer   # 立即把显示器切到对端（把画面交出去）
python kvmctl.py autodetect    # 自动探测本机接在哪个输入口上
python kvmctl.py auto off      # 暂停自动，只保留手动
python kvmctl.py auto on       # 恢复自动
```

`autodetect` 会弹一个三选一的对话框（本机 / 另一台 / 无信号）请你确认，
这不是偷懒：DDC 里显示器只知道自己哪个口上有信号，并不知道那个信号属于哪台
机器 —— 没有反向通道可问。实测过"切到空口看它会不会自动跳回"这条路：
空口要 12 秒才跳回，等 3 秒去读会把空口全判成有信号（四个口全中），
等够 14 秒虽然准，但一轮要一分多钟。所以问一句最快也最准，
而且通常一次就完事（当前显示的正好是本机时，连画面都不用切）。

如果 agent 改过 `control_port`，加 `--port 47824`。

想绑热键，就让热键去执行 `python kvmctl.py switch` 即可。  
也可以绑两条：`switch` 拉过来，`switch-peer` 交出去。
Windows 上可以用 AutoHotkey，macOS 上可以用快捷指令或 skhd。

---

## 七、自测

不接第二台机器也能验证网络与委托逻辑：

```bash
bash selftest/run.sh
```

会在同机起三个实例（一个真实 Windows、一个模拟 Mac、一个模拟「不在显示器上」），  
全程 dry-run，验证：控制口连通性 → 正向委托 → 无效委托兜底 → 自动开关。  
Windows 上请在 Git Bash 里跑。

---

## 八、已知限制

1. **切换有 1–3 秒黑屏**，这是显示器重锁信号 + 重新协商分辨率/HDCP 的物理开销，消不掉。
2. **切走的机器会掉屏**：Windows 上窗口可能重排。本项目不处理这一点  
   （如需彻底解决，要给两台各配一块虚拟屏，见下方"可选的下一步"）。
3. **必须视频线直连**，或经过雷电/USB4 扩展坞。廉价转接器会吃掉 DDC 通道。
4. **显示器 OSD 里的 `DDC/CI` 必须开着**，否则一切无效。
5. **两端都已在真机验证**。Windows 端 + 下面这套 macOS 环境实测通过：
   MacBook Air（Apple Silicon / macOS 27）+ 共享屏 HKC H27T22C3（Mac 在 DP1 = `0x0F`，
   Windows 在 DP2 = `0x10`，USB-C 转 DP 直连）。验证覆盖：角色判定、手动切换、
   以及「显示器在对端时碰本机键鼠自动切回」的完整闭环。
6. **macOS 上 m1ddc 的 `get input` 返回的是「显示器此刻在显示谁」**，而不是「本机接在哪个口」；
   且显示器切到对端后 DDC 依然可读可写（Apple Silicon 私有代理）。因此 macOS 的角色判定
   必须是 `current_input == my_input_source`，**不能**用「读得到就算在显示我」——
   后者会让角色恒为 active，自动切换永不触发。同时这也意味着 **Mac 端可以独立工作**：
   显示器在对端时 Mac 自己就能把它切回来，不需要 Windows 端在线；
   反过来「碰 Windows 键鼠切到 Windows」仍必须由 Windows 端发起。
7. 自动切换靠「键鼠输入活动」判定，所以**如果你用纯触屏或纯 KVM 切换器操作，  
   检测不到输入**，只能用 `kvmctl switch` 手动切。

---

## 九、可选的下一步

- **消除掉屏重排**：Windows 装虚拟显示驱动、macOS 用 BetterDisplay 建虚拟屏，  
  让被切走的一侧仍保留桌面坐标。
- **打包成免 Python 环境的可执行文件**：PyInstaller 打一个 exe / .app，  
  部署时不用管 Python 解释器。
- **托盘图标 / 菜单栏图标**：现在靠命令行，加个 UI 更好点。
