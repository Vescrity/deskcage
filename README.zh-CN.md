# deskcage

> 项目状态：vibe-coding 产物, 未完整审查

基于 bubblewrap 的配置化沙盒管理器，支持长期存活的实例和私有会话总线。

一份**配置（profile）**描述一个容器能看到什么：家目录、额外的 bind 挂载、
设备、桌面 socket、环境变量，以及 D-Bus 会话的工作方式。一个**沙盒
（sandbox / instance）**就是按配置启动起来的一个容器。多个进程可以共享同一个
沙盒（先 `dcage start`，再多次 `dcage exec`，或者让应用通过私有总线互相通信）。
沙盒由每个用户各自的小守护进程 `dcaged` 持有，因此启动它的命令退出后沙盒仍然
存在。

D-Bus 的边界取决于 profile 的 `bus` 模式：

* `isolated`：沙盒里是自己的一条私有会话总线，与宿主总线无关，沙盒内应用可以
  不受限制地互相通信；
* `bridge`：同样是这条私有总线，另外由 `bus-bridge` 把通知、托盘
  （StatusNotifierItem）和 MPRIS 接到宿主；
* `none`：不提供会话总线。

三种模式都不提供 portal；宿主总线本身不会挂进沙盒，因此其他宿主服务都不可见。

## 依赖

* Linux，内核支持用户命名空间（bubblewrap）
* `bwrap`、`dbus-daemon`、Python 3.11+
* PyGObject（`gi`），守护进程、CLI 和 GUI 都需要
* PyQt6（仅 GUI 需要，可用 `pip install PyQt6` 或 `pip install '.[gui]'`）
* 使用 `bus = "bridge"` 时需要 `bus-bridge`（pipx 或发行版包）

`dcage` 按以下顺序查找 bus-bridge：

1. 环境变量 `DESKCAGE_BUS_BRIDGE`
2. `~/.config/dcage/config.toml` 里的 `[bus] bridge = "..."`
3. `$PATH` 中的 `bus-bridge`（pipx 和发行版包都装在这里）

## 安装

在 `dcage` 目录中执行：

```sh
python3 -m venv --system-site-packages ~/.venvs/deskcage
. ~/.venvs/deskcage/bin/activate
pip install '.[gui]'      # 不装 GUI 就去掉 [gui]
```

`--system-site-packages` 让 venv 能使用发行版提供的 PyGObject。`bus-bridge` 单独
用 pipx 或发行版包安装，只有 `bus = "bridge"` 的配置才需要。安装后 `dcage`
会先看 `DESKCAGE_BUS_BRIDGE` 和 `[bus] bridge`，再在 `PATH` 中查找 bus-bridge。

## 快速开始

```sh
export XDG_RUNTIME_DIR=/run/user/$(id -u)   # 桌面会话里通常已经设置
dcage doctor

# 配置是普通 TOML，不会自动创建
dcage profile create work --command firefox
dcage profile show work

# 沙盒是环境而不是单应用：--reuse 命中已有实例就再起一个进程，
# 没有该实例时按名字新建
dcage start work --instance browser --reuse -- firefox --new-instance
dcage start work --instance browser --reuse -- gimp

# 长期存活的沙盒，之后再往里加进程
id=$(dcage start work -- sleep infinity)
dcage ps
dcage exec "$id" -- bash -l
dcage logs -f "$id"
dcage keep "$id" off
dcage stop "$id"
```

守护进程会作为 `org.deskcage.Manager1` 自动启动在会话总线上。调试时可以用
`dcage daemon` 让它在前台运行；后台守护进程的日志默认写在
`$XDG_RUNTIME_DIR/dcage/dcaged.log`（内存盘，不落持久磁盘）。

## 命令

| 命令 | 作用 |
| --- | --- |
| `dcage daemon [--detach] [--stop] [--replace]` | 运行、后台启动或停止守护进程 |
| `dcage profile list/show/path` | 查看配置 |
| `dcage profile command ID` | 打印该配置将执行的 bwrap 命令，每行一组参数（`--one-line`、`--json`） |
| `dcage profile create <id> [--from P] [--command CMD] [--bus MODE]` | 新建配置 |
| `dcage profile duplicate <id> [new-id]` | 复制配置 |
| `dcage profile rename <id> <new-id>` | 重命名配置；运行中的实例会跟着改用新名字 |
| `dcage profile edit <id>` | 用 `$EDITOR` 编辑配置 TOML |
| `dcage profile delete <id>` | 删除配置 |
| `dcage start <profile> [--instance NAME] [--keep\|--no-keep] [--reuse] [-- cmd...]` | 启动长期实例并打印实例 id；`--reuse` 把命令加进同名运行实例 |
| `dcage exec <instance> [--detach] -- cmd...` | 在已有实例中再启动一个进程 |
| `dcage ps` | 查看运行中的实例 |
| `dcage inspect <instance>` | 以 JSON 输出实例详情和进程列表 |
| `dcage stop <instance>` / `--all` | 销毁沙盒 |
| `dcage keep <instance> on\|off` | 设置最后一个进程退出后是否保留沙盒 |
| `dcage logs [-f] [instance]` | 查看进程输出，可按实例名过滤；每个文件在 stderr 上单独标注 |
| `dcage doctor` | 检查环境与守护进程 |
| `dcage completion bash\|zsh` | 打印对应 shell 的补全脚本 |

实例 id 可以缩写成任意唯一前缀；`dcage ps`、`exec`、`stop`、`keep` 只认活着的实例，
`dcage logs` 还能按名字读取已经释放的实例留下的日志。`start` 和 `exec` 都支持
`--env KEY=VALUE` 和 `--cwd DIR`。`--` 之后的参数会原样传给沙盒内的命令。

实例退出或被 stop 时立刻释放：名字可以马上重用，`instances/<id>` 一并删除。
日志不在实例目录里，所以 `dcage logs <id>` 在实例消失后仍然可读。`--reuse` 把沙盒
当作环境：它永远不会建第二个沙盒，也不会改动已有实例的 keep-alive；名字属于另一个
profile 时直接报错，而不是悄悄落进错误的环境。

`dcage logs -f` 只打印之后新出现的文件内容，实例结束时退出；不带实例时跟踪所有实例，
全部结束后退出。守护进程消失时同样会结束，不会一直挂着等信号。

## 命令补全

装包即可用：补全脚本会落到 `/usr/share/bash-completion/completions/dcage` 和
`/usr/share/zsh/site-functions/_dcage`，bash 和 zsh 都会自动加载。不装包时也可以手工
source 同一份脚本：

```sh
# bash，仅当前会话
source <(dcage completion bash)
# bash，对所有会话生效
dcage completion bash > ~/.local/share/bash-completion/completions/dcage

# zsh（写到一个在 $fpath 里且有写权限的目录）
dcage completion zsh > "${fpath[1]}/_dcage"
```

两个脚本都只是转调隐藏命令 `dcage _complete`，候选来自真正的解析器而不是手抄的一份
命令表：子命令、选项、`--bus`/`--home`/`keep` 的取值、profile 名，以及 `exec`、
`inspect`、`stop`、`keep`、`logs` 的实例名（`logs` 还能补已经退出的实例，因为它们的
日志还在）。`start --instance` 只有在命令行里已经出现 `--reuse` 时才补现有实例名——没
有 `--reuse` 时这个参数的意思是“用这个名字新建沙盒”。`--env` 补 `KEY=`（补完后光标
停在 `=` 后等值，不会多插一个空格），`--cwd` 补目录并保留结尾的 `/`，`--` 之后交回
shell 补文件名。

补全不会启动守护进程：只有在守护进程已经在跑时才走 D-Bus 读实例列表，否则退回到读
日志目录。

## 配置

配置保存在 `~/.config/dcage/profiles/<id>.toml`（遵循
`$XDG_CONFIG_HOME`）。它就是普通 TOML：CLI、GUI 和手工编辑器改的是同一份文件。

```toml
description = "家目录使用临时 tmpfs 的 Firefox"
command = [
    "firefox",
    "--new-instance",
]
keep_alive = true
network = true
home = "tmp"                 # tmp | bind | ro | tmpbind
hostname = ""                # 默认 deskcage-<id>
extra_args = []

[bus]
mode = "bridge"              # none | isolated | bridge
pid_hint = false

[files]
readonly = [
    "~/.config/fontconfig",
    "~/.local/share/fonts",
]
readwrite = [
    "~/Downloads",
    "~/.config/dconf",
]
tmpfs = []
# 宿主只读可见，写入退出即丢弃
tmpbind = [
    "~/.config/QQ",
]
tmpbind_strict = false       # true: 源路径缺失时拒绝启动

[devices]
dri = true                   # /dev/dri + /sys/dev/{char,devices}
snd = true                   # /dev/snd
video = false                # /dev/video*
nvidia = false               # /dev/nvidia*（CUDA）

[desktop]
wayland = true               # $XDG_RUNTIME_DIR/$WAYLAND_DISPLAY，只读绑定
x11 = true                   # /tmp/.X11-unix + $XAUTHORITY
pipewire = true              # pipewire-0 / pipewire-0-manager
pulse = true                 # $XDG_RUNTIME_DIR/pulse
a11y = false                 # at-spi 无障碍总线

[env-preset]
xdg = true                   # 透传 XDG 目录相关变量
session = true               # 语言、会话类型、SHELL、TERM

[env]
MOZ_ENABLE_WAYLAND = "1"     # 字符串：固定值
```

生成的配置会把每个非空列表写成一行一项，空列表保持 `[]`，增删一条路径只需改一行；
手写时这种写法和紧凑的 `["a", "b"]` 都合法。

### 环境变量

沙盒从空环境开始，只会自动设置 `HOME`、`PATH`、`XDG_RUNTIME_DIR`、
`DBUS_SESSION_BUS_ADDRESS`，以及已启用桌面通道对应的显示变量
（`DISPLAY`/`XAUTHORITY`、`WAYLAND_DISPLAY`、`PULSE_SERVER`、`PIPEWIRE_*`）。

`[env-preset]` 整组开关宿主变量（`xdg`、`session`、`theme`、`proxy`），
新建的配置默认打开 `xdg` 和 `session`。其余全部来自 `[env]`：字符串按原样写入，
`true` 表示从启动沙盒的那个进程（`dcage` 或 `dcage-gui`，而不是常驻的
`dcaged`）继承同名变量；该进程没有定义就不设置。显示变量以及配置里 `~`/`$VAR`
的展开同样使用这份环境快照，所以不会读到守护进程里已经过期的值。

沙盒会一直保留启动时的环境；修改了 `WAYLAND_DISPLAY` 之类的变量后，请重新启动实例。

展开时先应用变量组，再应用单条条目，所以 `LANG = "C"` 这样的条目会覆盖预设里
的值。

图形界面里预设就是开关：勾选后只把组名记进 `[env-preset]`，不在表格里列出这些
变量；「追加为自定义条目」会把它们复制成可编辑的行，用来单独覆盖某个值。

`dcage start` 和 `exec` 支持 `--env KEY`（从当前 shell 透传）和
`--env KEY=VALUE`（仅对该命令生效）。

### 文件挂载

| 模式 | 行为 | 退出后宿主机 |
| --- | --- | --- |
| `readonly` | `--ro-bind-try`，只读可见 | 不变 |
| `readwrite` | `--bind-try`，可写 | 改动保留 |
| `tmpfs` | `--tmpfs`，宿主内容不可见 | 不变 |
| `tmpbind` | `--overlay-src PATH --tmp-overlay PATH`，宿主内容只读可见，写入进临时层 | 不变 |

`home` 的四种模式对应 `tmpfs $HOME`、读写 bind、只读 bind、以及整个家目录的
`tmpbind`（真实家目录可见但写入丢弃）。`~` 和环境变量在宿主机上展开。

挂载按固定顺序生成，后挂载的覆盖先挂载的：

1. 基础骨架（`/dev`、`/proc`、`/tmp`、`/var`、`/usr`、`/etc`、`/run`）
2. home
3. 运行时目录与 Wayland / PipeWire / Pulse socket
4. X11
5. 设备
6. `[files]`：`readonly` → `readwrite` → `tmpfs` → `tmpbind`
7. `/run/dcage` 控制通道，然后是其内部的 `/run/dcage/logs`（始终最后，且必须是
   这个顺序，因为后一个挂载点在前一个里面）
8. `extra_args`、环境变量、agent

同一个目标路径出现在多个列表里时只保留最后一次挂载。`--overlay-src` 只作用于紧跟
其后的 overlay 参数，所以每一对始终相邻。`tmpbind` 源路径不存在时默认跳过，并写入
实例的 `logs/<id>/sandbox.log`；`tmpbind_strict = true` 时改为启动失败。`tmpbind` 需要宿主
内核提供 overlayfs，且模块必须在启动沙盒前已经加载（user namespace 内无法触发模块
自动加载）；`dcage doctor` 会显示其状态。

### 总线模式

| 模式 | 沙盒看到的内容 |
| --- | --- |
| `none` | 不设置 `DBUS_SESSION_BUS_ADDRESS` |
| `isolated` | 一个私有 `dbus-daemon`，只在沙盒内可见 |
| `bridge` | 私有总线，并通过 `bus-bridge` 接入通知、托盘和 MPRIS |

启用总线时（`isolated` / `bridge`），socket 都 bind 在 `/run/dcage/bus`，因此
沙盒内可统一使用 `unix:path=/run/dcage/bus`；`none` 模式不设置该地址。

### 沙盒内部布局

* `/run/dcage` —— 控制通道和总线 socket（读写 bind 挂载）
* `/run/dcage/agent.py` —— PID 1，一个小型 Python 进程代理
* `/run/dcage/control.sock` —— 宿主与代理之间的通信 socket
* `/run/dcage/logs/` —— 该实例的日志目录，每个进程一个文件
* `/tmp`、`/var`、`/dev/shm` —— 全新的 tmpfs
* `/usr`、`/etc`、`/opt`、`/sys/module` —— 宿主目录的只读挂载；指向别处的
  `/etc/resolv.conf` 会连同真实目标一起绑定

代理是 PID 命名空间里的 PID 1：它负责回收孤儿进程、按请求启动进程、上报退出
状态，并在最后一个进程退出后结束命名空间（除非开启 keep-alive）。`dcaged` 用
`--die-with-parent` 启动 bwrap，因此沙盒不会比守护进程活得更久。

## 日志与磁盘占用

默认所有日志都写在 tmpfs 里，注销或重启后自然消失，不会往持久目录写东西：

| 内容 | 位置 |
| --- | --- |
| 守护进程 | `$XDG_RUNTIME_DIR/dcage/dcaged.log` |
| 实例的 bwrap 输出 | `$XDG_RUNTIME_DIR/dcage/logs/<id>/sandbox.log` |
| 私有总线 / bus-bridge | `$XDG_RUNTIME_DIR/dcage/logs/<id>/bus.log` |
| 沙盒内进程输出 | `$XDG_RUNTIME_DIR/dcage/logs/<id>/<ns>-<seq>.log`（`dcage logs` 从这里读） |

每个实例一个日志目录，在沙盒里挂到 `/run/dcage/logs`。除守护进程日志外上面这些
文件都在里面，所以实例消失时不会跟着丢：退出或 stop 只删 `instances/<id>`，
`logs/<id>` 保留；同名实例重新创建时先清空这个目录。`dcage logs` 只输出进程日志，
`bus.log` 和 `sandbox.log` 留在同目录里供排查。`XDG_RUNTIME_DIR` 未设置时，这些路径
退化为 `/tmp/dcage-<uid>/`。

确实想保留守护进程日志时显式指定路径：

```sh
DESKCAGE_DAEMON_LOG=~/.local/state/dcage/dcaged.log dcage daemon --detach
```

`DESKCAGE_DAEMON_LOG=off`（或 `-`）表示直接丢弃守护进程输出；启动失败时错误信息会说明
日志已被丢弃，而不是指向一个不存在的文件。

## 图形界面

```sh
dcage-gui
```

Qt 窗口分为配置列表和实例列表两部分。右键配置可以新建、编辑、复制、重命名、删除、启动
或查看完整命令；右键实例可以查看日志、详情、追加进程、切换 keep-alive 和停止。
实例只在存活期间显示，退出后从列表消失。配置文件由 GUI 直接读写
`~/.config/dcage/profiles`，即使连不上 `dcaged`，窗口也能正常打开、编辑
和保存配置；只有实例相关操作才走守护进程。

* 文件页每种模式每行一个路径，可以直接编辑，也可以用「添加文件…」「添加目录…」
  选择；缺失的 Tmpbind 路径会标红，并在页面上提示启动时会被跳过；
* 设备页不再分组，`Wayland`、`X11`、`GPU / DRI` 等直接列出，tooltip 是自然语言
  说明；
* 编辑器底部的文本框是**跟随鼠标的**：鼠标移到哪个选项（尤其是设备页的
  `Wayland`、`PipeWire`、`X11` 等），就只显示那个选项生成的 bind 参数；悬停在
  尚未勾选的选项上会显示「勾选后会加什么」，没有悬停时是提示文字。要关闭某个
  选项，看悬停结果就不会再加那一组参数；
* `Full command...` 和配置右键菜单里的 `Show command` 查看完整 bwrap 命令（每行一组参数，
  可直接复制运行）；CLI 对应 `dcage profile command ID`。

界面使用 Qt 原生的翻译机制，没有自制的翻译表：

* 源码里用 `self.tr()` / `QCoreApplication.translate()` 包裹界面文字；
* 翻译源文件是 `src/deskcage/translations/deskcage_zh_CN.ts`（Qt Linguist 可直接打开）；
* `lrelease6` 把 `.ts` 编译成 `deskcage_zh_CN.qm`，随 wheel 一起安装；
* 启动时由 `QTranslator` 按 `QLocale.system()` 加载，没有对应翻译就显示英文原文。

工具栏右侧可以在「系统语言 / English / 简体中文」之间切换，选择会记住
（`QSettings`）。默认跟随系统语言，所以系统是中文时界面直接是中文；想临时用别的
语言，改 `LANG` / `LC_ALL` 即可，例如：

```sh
LANG=zh_CN.UTF-8 dcage-gui
```

修改界面文字后重新生成翻译：

```sh
cd src/deskcage/translations
make            # pylupdate6 更新 .ts，lrelease6 重新编译 .qm
```

需要 `qt6-tools` 提供的 `pylupdate6` 和 `lrelease6`；`.ts` 也可以用 Qt Linguist
编辑。

新字符串会以英文原文出现，并在 `.ts` 里留下未完成条目，补完翻译再执行一次
`make` 即可。切换语言时窗口会就地重建，不会重启守护进程或影响正在运行的沙盒。

## D-Bus 接口

`dcaged` 在会话总线上持有 `org.deskcage.Manager1`，对象路径为
`/org/deskcage/Manager1`：

* 配置：`ListProfiles`、`GetProfile`、`SaveProfile`、`DeleteProfile`、
  `DuplicateProfile`、`RenameProfile`
* 实例：`Launch(profile, id, keep, argv, env, cwd, reuse, caller_env)`、
  `ListInstances`、`GetInstance`、`Spawn`、`StopInstance`、`SetKeepAlive`、`Shutdown`
* 信号：`InstanceChanged(id, state)`、`ProcessExited(instance, pid, status)`

`caller_env` 是启动沙盒的进程的环境，`[env] KEY = true` 和各个预设都按它解析。
旧版本启动的守护进程无法使用它，升级后执行一次 `dcage daemon --replace`。

`GetInstance` 返回 `a{sv}`；其中 `processes` 是一个 JSON 字符串，包含 pid、
argv、启动时间、日志路径和退出状态。错误以 `org.deskcage.Manager1.Error` 返回。

## 测试

```sh
tests/run_all.sh
```

每个测试都会创建自己的 XDG 目录和私有会话总线，因此不会碰开发者的桌面会话。
测试覆盖：

* 配置 TOML 的序列化与校验
* bwrap 参数生成、挂载顺序与 `tmpbind` 覆盖参数
* 沙盒内代理协议
* 三种总线模式下的真实沙盒生命周期（包括 bus-bridge 名字可见性）
* 真实 `tmpbind` overlay（宿主无 overlayfs 时跳过）
* CLI 全流程
* 无显示环境下的 GUI 冒烟测试（含中英文切换）
* 翻译目录与 GUI 字符串的一致性、`.qm` 加载和运行时翻译
* 守护进程被 `SIGKILL` 后的恢复与清理
* 模拟安装布局（site-packages）下的模块入口、守护进程自动启动和沙盒运行

## 常见问题

**提示 “no session bus”**
：  确认 `DBUS_SESSION_BUS_ADDRESS` 或 `XDG_RUNTIME_DIR` 指向会话总线。
    纯 SSH 环境通常没有会话总线，需要先启动一个。

**提示 “bus-bridge not found”**
：  设置 `DESKCAGE_BUS_BRIDGE=~/.local/bin/bus-bridge`，或在
    `~/.config/dcage/config.toml` 中写：

    ```toml
    [bus]
    bridge = "~/.local/bin/bus-bridge"
    ```

    也可以把 `bus = "isolated"` 改成不需要 bus-bridge 的模式。

**实例启动失败 / 沙盒内程序立即退出**
：  查看实例日志：

    ```sh
    dcage inspect <instance>
    dcage logs <instance>
    ```

    宿主侧的 bwrap 输出在 `$XDG_RUNTIME_DIR/dcage/logs/<id>/sandbox.log` 中，
    bus-bridge 的输出在 `$XDG_RUNTIME_DIR/dcage/logs/<id>/bus.log` 中。

**守护进程没有响应**
：  查看 `$XDG_RUNTIME_DIR/dcage/dcaged.log`（用 `DESKCAGE_DAEMON_LOG` 改过路径的看对应
    文件，设成 `off` 则没有日志），或用 `dcage daemon --stop` 停掉后让 CLI/GUI 自动
    重新启动。

**Wayland / 音频 socket 不可用**
：  这些 socket 从宿主 `$XDG_RUNTIME_DIR` 绑定。没有桌面会话时该变量通常为空，
    沙盒内自然也就没有对应 socket。

**Tmpbind 启动失败（overlayfs）**
：  `--tmp-overlay` 需要宿主内核的 overlayfs。检查 `dcage doctor` 的 `overlayfs`
    一行；如果显示 `MISSING`，先加载模块（`sudo modprobe overlay`）或改用内核
    内置 overlayfs。user namespace 内无法触发模块自动加载。缺失的 Tmpbind 源路径
    默认会被跳过；设置 `tmpbind_strict = true` 可改为启动失败。

## 已知边界

* 用户命名空间是实用的隔离手段，但不是对抗本机恶意攻击者的安全边界。
* X11 socket 是一条很宽的通道；需要真正隔离时优先使用 Wayland。
* 不桥接 portal，也不暴露任意宿主服务；`bus = "bridge"` 只开放通知、托盘和
  MPRIS 三条通道。
* 配置按用户隔离，没有 root 模式。
* 宿主缺少通知/托盘服务时，bus-bridge 仍会持有沙盒侧名字，但调用会返回错误。
