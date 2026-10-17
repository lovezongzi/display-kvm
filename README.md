# display-kvm

用软件切换显示器的输入源，替代伸手去按显示器上的实体按钮。

针对的场景：一台显示器同时接了 Windows 主机和 Mac 主机，两台**各有独立的键鼠**。  
你碰哪台的键鼠，显示器就自动跳到哪台。

---

## ⚡ 下载与安装

不碰 Python 的最短路径：到 [Releases](https://github.com/lovezongzi/display-kvm/releases) 下载对应平台的产物。

| 平台 | 文件 | 是什么 |
| --- | --- | --- |
| Windows | `display-kvm.exe` | **免安装执行文件**（绿色版）。双击直接运行，不用装、不用管理员权限，托盘常驻；配置生成在 `%APPDATA%\display-kvm\kvm.json`。适合先试一下，或者不想往系统里写东西。 |
| Windows | `Display-KVM-Setup-1.0.11.exe` | **安装包**。双击安装，会写开始菜单快捷方式、可选桌面图标与开机自启，卸载入口也在开始菜单里。长期用推荐这个。 |
| macOS | `Display-KVM-1.0.11.dmg` | **苹果 M 芯片（Apple Silicon：M1/M2/M3/M4）Mac 的安装包**。打开后把 `Display KVM` 拖进 Applications 即可。 |

dmg 只面向 Apple 芯片的 Mac：它内置的 `m1ddc` 依赖 Apple 的私有 CoreDisplay 接口，
**Intel Mac 上拿不到 DDC 通道**（需要把配置里的 DDC 命令换成 `ddcctl`）。
另外 dmg 无法在 Windows/Linux 上交叉构建，如需自己打，在 Mac 上跑
`bash packaging/build_mac.sh` 即可（自动装依赖、打 .app、做 dmg）。

Windows 安装包做了什么：

- 装到 `%LOCALAPPDATA%\Programs\display-kvm`，每用户安装，**不需要管理员权限**
- 开始菜单快捷方式 + 可选桌面快捷方式 + 可选开机自启（卸载时自动清理）
- 托盘常驻：右键图标 → 看状态 / 立即切到本机 / 自动切换开关 / 打开配置 / 打开日志 / 退出
- 首次启动自动生成配置到 `%APPDATA%\display-kvm\kvm.json`，  
  并**自动探测**共用显示器的分辨率和当前输入源——通常只需改 `token` 和 `peer_input_source` 两个值
- 自带单实例保护，重复双击只会提示"已在运行"

双端各装好后，必做三步：

1. 让两端 `token` 一致——二选一：
   - **懒人法（推荐）**：托盘菜单点「配对两端 …」，两端会自动在局域网里协商出同一个 `token` 并各自写盘，全程不用手动复制。
   - **手工法**：两端 `token` 改成**完全一致**的随机串（Windows 端首次生成的是随机值，复制到 Mac 端即可）
2. 改对两端的 `my_input_source` / `peer_input_source`（见下面第四节「找输入口」）
3. 先保持 `dry_run: true` 跑几轮看日志，确认无误后改 `false`

> 配对安全性：协商出的 `token` 在局域网里是**明文**广播的，家用无妨；公司 / 公共网络请勿用配对，改用手工法并自行保管随机串。已配对的两端会忽略任何新的配对请求，不会被同网段其他实例劫持。

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
| `kvmctl.py`        | 本地控制客户端，`status` / `switch` / `switch-peer` / `autodetect`      |
| `hotkeys.py`       | 全局热键：macOS 走 Carbon `RegisterEventHotKey`（免授权），Windows 走 `RegisterHotKey` |                                              |
| `kvm.windows.json` | Windows 端配置模板（命令行方式用）                                     |
| `kvm.mac.json`     | macOS 端配置模板（命令行方式用）                                       |
| `selftest/`        | 单机自测，用假实例验证网络与委托逻辑                                        |
| `packaging/`       | 打包：spec、Inno Setup 脚本、图标生成、Mac 一键构建脚本                     |
| `.github/workflows/build.yml` | 推 tag 自动在 macOS runner 出 dmg、Windows runner 出 exe/安装包   |

---

## 三、Windows 端安装（命令行方式）

> 用了安装包（`Display-KVM-Setup-1.0.11.exe`）或免安装版 `display-kvm.exe` 的话，**跳过这一节**，  
> 直接看「⚡ 下载与安装」里的必做三步。以下是从源码跑的方式。

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

### 0. 从 Releases 拿 dmg（推荐）

下载 `Display-KVM-1.0.11.dmg`，双击打开，把 `Display KVM` 拖进 Applications。
首次打开若提示「无法验证开发者」，右键 → 打开，或执行：

```bash
xattr -cr "/Applications/Display KVM.app"
```

之后按上面「⚡ 下载与安装」里的必做三步配置即可。

### 0.1 想自己构建 dmg

dmg 无法在 Windows 上构建。把整个 `display-kvm/` 文件夹拷到 Mac 上，跑一条命令：

```bash
bash packaging/build_mac.sh
```

脚本会自动：建隔离虚拟环境 → 装 pyinstaller/pystray/pyobjc → 找或下载 m1ddc 并打包进 app →  
构建 `Display KVM.app`（菜单栏应用，不占 Dock）→ ad-hoc 签名 → 产出 `Display-KVM-1.0.11.dmg`  
（带拖拽到 Applications 的布局）。产物在 `dist/` 下。

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

**菜单栏图标 →「参数设置 …」**：一个窗口填完，保存即时生效，不用重启。
macOS 上是原生窗口，Windows 上是系统表单；万一两边都弹不出来，会自动退化成逐项输入框。

| 窗口里的名字            | 配置字段                     | 默认    | 说明                                                    |
| ---------------- | ------------------------ | ----- | ----------------------------------------------------- |
| 启用自动切换           | `auto_switch`            | true  | 关掉就只剩手动切换                                             |
| 切换后的保护时间（秒）      | `cooldown_seconds`       | 6.0   | 刚切完画面的这段时间内，碰鼠标键盘都不会再触发自动切换。托盘菜单和快捷键的手工切换**不受它限制** |
| 离开多久后才允许被唤醒（秒）   | `arm_after_idle_seconds` | 3.0   | 画面在对端时，本机键鼠要空闲超过这么久才进入待命状态。调大可减少误触                   |
| 判定为「有操作」的阈值（秒）   | `fire_below_idle_seconds` | 0.45 | 待命状态下，空闲时长低于这个值就认为你碰了键鼠，立刻切过来                         |
| 状态检测周期（秒）        | `role_poll_seconds`      | 2.0   | 多久探测一次「画面现在在谁那儿」。越小越灵敏，DDC 查询也越频繁                    |
| 对端失联判定（秒）        | `peer_timeout_seconds`   | 12.0  | 超过这么久收不到对端心跳就认为它下线了                                   |
| 演练模式             | `dry_run`                | false | 只写日志，不真的切                                             |

填进窗口的值会自动做范围钳制（比如保护时间最长 120 秒），填了非法值就忽略该项。

也可以直接改配置 JSON 里的同名字段，但**改完要退出再重开才生效**——走设置窗口则不用。

两个常见调法：

- **觉得太容易误触**（走过碰一下桌子就被切走）：把「离开多久后才允许被唤醒」提到 8–10 秒。
- **切完显示器还没亮又被弹回去**：把「切换后的保护时间」提到 8–10 秒。

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

### 全局快捷键（内置，不用外挂工具）

不用再装 AutoHotkey / skhd 了，程序自带。在菜单栏图标的菜单里直接设：

- **快捷键 · 切到本机：‹当前键›** → 弹输入框，写 `ctrl+alt+1`
- **快捷键 · 切到对端：‹当前键›** → 写 `ctrl+alt+2`
- **启用快捷键** → 总开关（勾掉就完全不抢按键）

写法是 `修饰键 + 主键`，例如 `ctrl+alt+1`、`cmd+shift+m`、`ctrl+alt+f13`。
修饰键可用 `ctrl`（Mac 上是 ⌃）、`alt`（⌥）、`shift`（⇧）、`cmd`（⌘）；
主键可用字母、数字、`f1`~`f20`、方向键、`space` 等。**至少要带一个修饰键** ——
单独一个字母键会和正常打字打架，程序会直接拒绝并提示。留空 = 取消该快捷键。

设完立即生效，并写回 `kvm.json`：

```json
"hotkeys_enabled": true,
"hotkey_to_me": "ctrl+alt+1",
"hotkey_to_peer": "ctrl+alt+2"
```

macOS 侧用的是 Carbon 的 `RegisterEventHotKey`，**不需要「辅助功能」权限**。
这一点是特意选的：CGEventTap 那条路必须在系统设置里授权，而 ad-hoc 签名
（`--sign -`）的 app 每次重新打包 cdhash 都会变、旧授权立刻失配，等于每构建
一次就要用户重新勾一次权限，没法用。Windows 侧用 `RegisterHotKey`，同样不
需要管理员权限。

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
8. **Windows 端的全局热键尚未在真机跑过**（`RegisterHotKey` + 消息循环那段）。
   macOS 侧的 Carbon 实现已实测（⌃⌥1 / ⌃⌥2 双向切换都验证过）。
   另外 macOS 菜单栏可能把状态图标当 template 渲染成黑白，所以「在不在本机」
   同时用菜单文本里的 🟢/🔴 表示 —— 那个一定是彩色的。

---

## 九、可选的下一步

- **消除掉屏重排**：Windows 装虚拟显示驱动、macOS 用 BetterDisplay 建虚拟屏，  
  让被切走的一侧仍保留桌面坐标。
- **打包成免 Python 环境的可执行文件**：PyInstaller 打一个 exe / .app，  
  部署时不用管 Python 解释器。
- **托盘图标 / 菜单栏图标**：现在靠命令行，加个 UI 更好点。

---

## 十、更新日志

### v1.0.13

**重构：把三个上帝文件拆成有边界的包**（功能与配置格式完全兼容，升级不用改任何设置）

- `platforms.py`（约 800 行）→ `kvm_platform/`：`constants.py`（VCP 表 / 输入源命名）、
  `base.py`（`PlatformBase`）、`windows.py`、`mac.py`，`create()` 工厂保留原公开 API。
  包名故意不叫 `platform` —— 那样会遮蔽标准库。
- `agent.py`（1275 行）→ `kvm_core/` 领域模块：`config` / `common` / `transport` /
  `pairing` / `sync` / `discovery` / `switch` / `control` / `probe`。
  `Agent` 只剩 `__init__`（建 socket、共享字段）、`run` 与对外 facade，302 行。
- `app.py`（1180 行）→ `kvm_app/`：`paths` / `logsetup` / `config_store` / `autostart` /
  `dialogs` / `tray`（把原来的层层闭包收成 `TrayApp` 类）/ `entry`。`app.py` 只做门面，134 行。
- 建立 `selftest/` 测试安全网（pytest，30 个用例）：配对协议、自动切换同步、配置归一化、
  托盘菜单重建。重构期间靠它保证行为不变。
- PyInstaller spec 的 `hiddenimports` 同步到新包名，否则打包后会缺模块。

**修复：Windows 托盘右键菜单滑几次后不再高亮**

- 根因是托盘图标那套 2 秒定时 `update_menu()`：`pystray` 的 win32 后端是快照式菜单
  （`DestroyMenu` + `CreatePopupMenu` 重建），而 `TrackPopupMenuEx` 在消息循环线程里阻塞显示，
  重建正好落在菜单显示期间 → 菜单项变成没有 id 的死项，能点但不再高亮。
- 改法：菜单显示期间只置脏标记、绝不重建；在 `WM_RBUTTONUP` 弹出之前重建一次，
  弹出返回后再补一次脏刷新。图标与标题仍按 2 秒刷新，但那部分不碰 HMENU。

**Windows 版本资源**

- `filevers` / `prodvers` 修正为跟随实际版本（之前只改了字符串，元组还停在 1.0.11）。

**构建：版本号改成单一来源**

- 之前版本号散在 5 个地方（`build_mac.sh`、`build_win.sh`、`installer.iss`、
  `display-kvm-mac.spec`、`version_info.txt`），发版时漏改就出现「tag 是 1.0.13、
  产物却叫 1.0.12」这种事（本版第一轮构建就踩了）。
- 现在只认仓库根的 `VERSION` 文件：两个构建脚本读它（`KVM_VERSION` 可覆盖），
  mac spec 从它取 `CFBundleShortVersionString`，`installer.iss` 用
  `/DMyAppVersion=` 注入，`version_info.txt` 由 `build_win.sh` 就地重写。
- 且在带 tag 的提交上构建时，tag 与 `VERSION` 不一致会**直接失败**，不再默默出错包。

### v1.0.12

**新增：自动切换状态两端网络同步**

- 之前「自动切换」是纯本地开关，两端各管各的：Windows 关了、Mac 没关，Mac 一碰鼠标照样把显示器抢过来。
- 现在任一端改「自动切换」（菜单开关 / 设置面板保存 / `kvmctl auto on|off`），都会通过新的 `config_sync` 报文广播给对端，对端镜像并写回自己的 `kvm.json`。
- **防回声环路**：接收端只镜像、不回播；且「值相同直接跳过」，所以即便两边同时改也只会收敛到同一个值，不会无限互发。
- 只有**已配对**（token 一致）的对端才会生效；未配对的报文会被 token 校验直接丢弃，不会误同步。

### v1.0.11

**修复**

1. **「碰一下鼠标，屏幕却不切」——自动切换几乎抓不到键鼠活动**  
   键鼠空闲时长原本只在角色轮询里顺带取一次，而角色轮询要跑一次 `m1ddc`
   子进程、默认 **2 秒**才一轮；触发判据却又要求采样到「距上次输入不足
   **0.45 秒**」。等于要求采样点正好落在你动手后的 0.45 秒窗口里 —— 随手
   点一下鼠标几乎必然错过，表现就是明明动了鼠标、屏幕毫无反应。  
   改法两层：① 空闲查询从角色轮询里拆出来单独采样，Mac 走 Quartz、
   Windows 走 `GetLastInputInfo`，都是一次 API 调用不碰显示器，因此可以按
   **200ms** 采；平台层若只剩子进程查询（`idle_is_cheap=False`）则自动退回
   低频，避免每 200ms 起一个进程。② 触发判据加上「空闲计时器被重置过」
   （这一拍明显小于上一拍），不再依赖采样时机 —— 只要这两拍之间发生过任何
   一次输入就一定能看到。  
   配套修掉一条一直在坏的后备路径：`ioreg -c IOHIDSystem -d 1` 打印不到
   `HIDIdleTime`（必须加 `-r`），Quartz 万一失败时回退查询永远返回空。
2. **配对状态不会落盘**（`paired` 只改内存不写回 `kvm.json`）  
   配对成功后重启程序，配置文件里 `paired` 仍是 `false`，需要重新配对。
   现在每次改 `paired` 都同步写回配置。

### v1.0.10

**修复**

1. **配对死锁：卡住的那一端，另一端对它完全无能为力**  
   一端停留在 `paired=true` 而 token 与对端不一致时（v1.0.7 的遗留状态），它会
   拒绝一切新的配对请求，而对端**没有任何手段请它松口** —— 唯一出路是走到那台
   机器前手动点「解除配对」，偏偏那一端常常正是看不见异常的那台。  
   改法：新增 `pairing_reset` 报文作为带外恢复通道。一端解除配对时会请对端一并
   解除；发起配对前也先重置一次，确保对端处于可协商状态。  
   安全性：reset 只认「知道当前或上一个共享 token」的对端，且只发给已知的对端
   地址、**不广播**。不知道 token 的第三方拆不散已配对的两端。
2. **token 不一致是静默故障，界面上完全看不出来**  
   旧代码对校验不过的报文只打一行日志就丢掉，日志会无限刷（实测累计 6000+ 条），
   而状态查询和托盘菜单都不显示「两端已经对不上」，用户只会觉得切换没反应。  
   改法：日志按对端 IP 节流；`status` 与托盘「配对状态」行都会提示 token 不一致。
3. **自动自愈**  
   已配对却在 30 秒内累计 10 次 token 校验失败，即判定两端错乱，自动解除配对并
   请对端一并解除，之后在**任意一端**点「配对两端」就能恢复。  
   判据刻意取「收到了对端报文但校验失败」，而不是「没收到心跳」—— 对端只是关机
   时，不该把配对解掉。

**新功能**

1. **控制口新增 `pair` / `unpair` / `reset-peer`**  
   此前配对只能从托盘菜单点，脚本、快捷键、远程诊断都介入不了，出问题时也没有
   命令行出口。现在 `python kvmctl.py unpair` 就能解开死锁。
2. **自测脚本 `selftest/test_pairing.py`**  
   同机起两个实例，端到端验证死锁可复现、reset 能解开、伪造 token 被拒、
   关机不误判、解除后能重新配对。
3. **Windows 版本资源里的数字版本号补上**  
   exe 属性里的 `filevers` / `prodvers` 一直停在 1.0.2，只有字符串版本号跟着走，
   右键看「详细信息」会显示错误的版本。

### v1.0.9

**修复**

1. **保存一次设置后，自动切换就彻底失效，且日志被错误刷爆**  
   设置窗口保存时把**所有非布尔字段一律转成字符串**写回 `kvm.json`，于是
   `arm_after_idle_seconds` 变成 `"3"`、`fire_below_idle_seconds` 变成 `"0.45"`。
   主循环每轮执行 `idle >= cfg["arm_after_idle_seconds"]` 时就抛
   `TypeError: '>=' not supported between instances of 'float' and 'str'`，
   异常被主循环的 `except` 吞掉只留一行日志 —— 结果是**自动切换完全不再工作**，
   而使用者只会看到日志每秒多两行「自动检测出错」（实测 30 分钟刷了 7000+ 行、
   654KB）。  
   改法分三层：① 设置窗口按字段类型写回（数值还原成 float/int，填了非数字则保留
   原值）；② 主循环比较处用带默认值的转换兜底，任何脏值都不会再让自动切换停摆；
   ③ **启动时自动归一化配置里的数值字段**，已经被写坏的配置文件会自行修复，
   不需要手工删配置。
2. 上一条的 Windows 端同样问题一并修复（PowerShell 表单返回的都是字符串，
   现在也按字段类型还原）。

### v1.0.8

**修复**

1. **配对超时会假装成功，导致两端 token 永久不一致**（表现为日志里刷「token 不匹配」）  
   发起配对的一端会先把新 token 写进本地再广播，如果对端没收到（防火墙拦截 UDP
   47821、广播不可达、两端不同网段、或一端已配对而忽略请求），3 秒超时后**旧版本会
   直接把本机置为「已配对」** —— 本机 token 换了、对端没换，两端从此永久不一致；
   而且已配对的一端会忽略后续所有配对请求，**再点「配对两端」也没用，形成死局**。  
   改法：超时即判定失败，**回滚到发起前的 token**、`paired` 保持 `false`，并给出明确
   失败提示。两端因此始终保持一致，修好网络后可以重试。
2. **配对失败时 Windows 端看不到任何反馈**  
   Windows 的系统通知常被静默抑制（pystray 用的是托盘气球提示，可能被通知设置或
   专注助手挡掉，且不抛异常），而旧代码的兜底是 `print`，在打包成无控制台的 exe 后
   完全不可见。改法：配对结果（尤其是失败原因）会显示在**托盘菜单的「配对状态」行**，
   不依赖系统通知。

### v1.0.7

**新功能**

1. **局域网自动配对**（不用再手动复制 `token`）  
   菜单栏新增「配对两端 …」：一端发起，两端会在局域网里自动协商出同一个
   `token` 并各自写回配置文件。另加了「解除配对」。  
   注意：协商过程在局域网里是**明文广播**的，家用无妨，公司 / 公共网络请用手工法。
   已配对的两端会忽略新的配对请求，不会被同网段其他实例劫持。

**修复**

2. **macOS 点「参数设置 → 保存」后程序直接退出**（崩溃，非主动退出）  
   崩溃点：`AppHelper.callAfter` 回调里释放 PyObjC 对象时 `objc_release`
   到一个已被 AppKit 释放的地址（`EXC_BAD_ACCESS / SIGSEGV`）。  
   根因之一是 `NSWindow` 的 `releasedWhenClosed` **默认为 `YES`** —— 窗口一关闭
   AppKit 就自己把它 release 掉，而 Python 侧还握着引用，必然二次释放。  
   改法：显式 `setReleasedWhenClosed_(False)`，并且**设置窗口只创建一次、永久复用**
   （关闭只是隐藏，再打开时刷新控件值后重新显示），**全程不做任何释放动作**——
   没有释放，就不存在双重释放。顺带修掉了「点保存 / 取消后窗口不消失」。
3. **macOS 菜单栏点完菜单项后图标失效 / 闪退**  
   菜单追踪会话还在进行时就同步调用 `update_menu()`，会让旧的 `NSMenuItem`
   被过度释放。改为把刷新推迟到菜单会话结束后的下一轮 run loop
   （macOS 走 `AppHelper.callAfter`）。
4. **Windows 参数设置界面打开是空白、保存 / 取消按钮乱码**  
   中文 Windows 的 PowerShell 5.1 默认按 GBK 读无 BOM 的 UTF-8 脚本，导致整段中文
   变乱码。改为脚本与临时 JSON 都写 `utf-8-sig`（带 BOM），读取时显式
   `-Encoding utf8`。
5. **参数设置保存后立即生效**（不用重启）  
   保存后同步把改动推给主循环里 agent 持有的那份配置副本。
