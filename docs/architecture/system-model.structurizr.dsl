// simbench 现状架构模型（C4 / Structurizr DSL）
//
// 生成日期：2026-10-21　｜　取证基线：git HEAD 6bf4ff3（== origin/main，已 fetch 核对）
// 产出流程：architecture-visualization 的 system-modeler + c4model
//
// 口径：本文件只描述**现状**（current state），不含目标态、不含整改建议。
// 每个元素的 description 末尾用 [证据: 路径:行] 标出处；标 [推断] 的是从文档或
// 命名推出来的、没有代码直接证据的部分，配套说明见同目录 system-model.md。
//
// 用 Qoder 的 Structurizr DSL 查看器打开本文件即可看图；四张视图分别是
// L0-Landscape / L1-Context / L2-Container / L3-Component。

workspace "simbench" "设备通讯模拟仿真工具的现状架构模型（HEAD 6bf4ff3，2026-10-21）" {

    model {

        // ================= 人 =================
        // 两个角色都是从 README 的章节划分推出来的（"跑起来"/"现场" 对 "改代码的人要先装的"），
        // 代码里没有任何角色、权限或身份的概念 —— 所以标 [推断]。
        fieldEngineer = person "现场调试工程师" "在隧道机电现场用打包好的 exe 起模拟设备、拿点表核对真设备、按需配专用网卡。不装 Python、不看代码。[推断，依据 README:6-60 与 packaging/build.ps1]"
        developer = person "改代码的人" "跑 ruff / mypy / pytest，按 docs/superpowers/ 下的 spec 与里程碑计划改实现。[推断，依据 README:233-260]"

        // ================= 外部系统 =================
        upstreamMaster = softwareSystem "上游主站" "Modbus Poll / QModMaster / 组态软件 / 隧道机电平台。**连进来**读我们模拟的设备。[证据: README:72-84, simbench/__main__.py:161-164, packaging/smoke.py]" {
            tags "External"
        }

        realDevice = softwareSystem "真实现场设备" "真 PLC / Modbus 从设备。主站（核对）模式下 simbench **连出去**只读它，用来验证点表与设备是否对得上。[证据: simbench/master/modbus.py:108-113 —— 全仓 master/ 下无任何 write_* 调用]" {
            tags "External"
        }

        serialBus = softwareSystem "USB-485 串口总线" "一条串口挂多个 unit 的现场总线。RTU 与 ASCII 两种帧。[证据: simbench/project/model.py:384-402, simbench/backend/modbus_rtu.py:27-36]" {
            tags "External"
        }

        windowsNetwork = softwareSystem "Windows 网络栈" "网卡、IP 地址、以及为设备 IP 新建的虚拟环回网卡。simbench 既读它也**改**它（改的部分要提权）。[证据: 读 simbench/net/state.py:142-163；写 simbench/net/cli.py:277-300 与 simbench/net/win32.py:154-233]" {
            tags "External"
        }

        windowsUac = softwareSystem "Windows UAC" "提权路径：用 Start-Process -Verb RunAs 把**自己**再启一遍，参数经 %TEMP% 里一个带 pid 的 JSON 任务文件传递，结果经另一个 JSON 文件回传。[证据: simbench/net/elevate.py:56-93]" {
            tags "External"
        }

        nativeLibraries = softwareSystem "协议与界面原生库" "pymodbus、pyserial、PySide6 是硬依赖；snap7.dll（随 python-snap7 1.3 来）、openpyxl 与 paho-mqtt（`mqtt_client.py` 里延迟 import）是**用到才加载**的可选依赖。[证据: 硬 simbench/backend/simdev.py:5, raw_writer.py:9-10；可选 simbench/backend/s7_server.py:47-59, simbench/project/xlsx_io.py:43-52, simbench/backend/mqtt_client.py:63]" {
            tags "External"
        }

        // ================= simbench 本体 =================
        simbench = softwareSystem "simbench" "点表驱动的多协议设备模拟器：平时**当**从站被上游主站读，必要时反过来**当**主站去核对真设备。六种传输（Modbus TCP / UDP / RTU-over-TCP / 串口 RTU / 串口 ASCII / S7）可在一张点表里混装；另有 **MQTT 通道**（模拟侧连 broker 发布、订阅 /set）。[证据: README:1-4, 86-99, simbench/backend/mqtt_client.py:1-14]" {

            // ---------- L2 容器 ----------
            app = container "simbench 主程序" "同一份 Python 包树，三种打包形态：simbench.exe（控制台）、simbench-gui.exe（无控制台）、simbench-onefile.exe（单文件，README 明说**不是**发布形态）。哪个 exe 走界面还是命令行由 packaging/entry.py 判：stdout 为 None 或文件名以 -gui 结尾就走界面。[证据: packaging/simbench.spec:51-107, packaging/entry.py:17-39]" "PyInstaller 冻结的 Python 3.13 进程" {

                // ---------- L3 组件（八个包 + 根下横切） ----------
                gui = component "gui/" "PySide6 桌面界面：通道面板 + 设备树 + 点位表 + 报文窗 + 日志页 + 核对页（Modbus / MQTT）+ 网络页。27 个文件 5926 行。主窗体用三个 mixin 拆开（window_menus / window_edit / window_io），拆分动机是打断 mixin→main_window 的循环依赖。[证据: simbench/gui/main_window.py:65, simbench/gui/window_consts.py:1-4]" "PySide6 · 5926 行" {
                    tags "LayerUI"
                }

                engine = component "engine/" "Session 是**唯一**对 GUI 暴露的门面（启停 / 改值 / 拉行 / 订阅事件）；EngineRunner 持有那条 asyncio 线程。12 个文件 1784 行。[证据: simbench/engine/session.py:27-43, simbench/engine/runner.py:39-87]" "asyncio · 1784 行" {
                    tags "LayerEngine"
                }

                backend = component "backend/" "Modbus 五种帧 + S7 + **MQTT 客户端**的后端；与传输无关的那一半（波形 tick、值归属、报文钩子、启停顺序）都在 BackendBase 里；__init__.py 是门面，按 channel_transport() 选后端。12 个文件 2829 行。[证据: simbench/backend/__init__.py:36-62, simbench/backend/base.py:100-617, simbench/backend/mqtt_client.py:1-14]" "pymodbus · pyserial · snap7 · paho · 2829 行" {
                    tags "LayerBackend"
                }

                codec = component "codec/" "纯换算：point_codec（点位值 ↔ 寄存器序列，数据类型 × 字序）、s7_codec（S7 值 ↔ 字节，大端无字序）、waveform（const/ramp/sine/random/step 的纯函数发生器）。4 个文件 623 行。[证据: simbench/codec/point_codec.py:34-60, simbench/codec/waveform.py:1-6]" "纯函数 · 623 行" {
                    tags "LayerCore"
                }

                project = component "project/" "领域模型 Project→Channel→Device→Point，加上三种文件格式（csv / xlsx / json）的读写、校验、与带碰撞回避的编辑，以及 **MQTT topic 模板**（转义与 payload 规则只此一处）。**实测不 import 任何其它 simbench 包**，是最底层的纯核。10 个文件 3033 行。[证据: simbench/project/model.py:247-490, 依赖图见 module-deps.dot]" "纯数据 · 3033 行" {
                    tags "LayerCore"
                }

                master = component "master/" "主站（核对）模式：拿点表当「要读什么」的清单去读别人的设备，只读不写；Modbus 扫描 + **MQTT 核对**（订阅 status topic 判在线/离线），产出四类结论与一份报告。5 个文件 514 行。[证据: simbench/master/scan.py:1-48, simbench/master/modbus.py:108-113, simbench/master/mqtt.py]" "pymodbus / paho 客户端 · 514 行" {
                    tags "LayerBackend"
                }

                net = component "net/" "系统网络层，也是全仓**唯一**会改变现场机器状态的地方：读网卡现状（PowerShell 只读，不提权）、算差异（纯函数）、按需增删 IP 与新建/移除虚拟环回网卡（提权）。记账本只回收自己加过的东西。12 个文件 2680 行。[证据: simbench/net/__init__.py:1-25, simbench/net/ledger.py:30-38, simbench/net/plan.py:1-26]" "PowerShell · SetupAPI ctypes · pnputil · 2680 行" {
                    tags "LayerSystem"
                }

                crosscut = component "根下横切模块" "events.py（engine 与 GUI 之间的事件契约，2026-10-16 从 backend/ 挪到根上以封掉 gui→backend 的口子）、swallow.py（吞异常的唯一出口）、settings.py、diagnostics.py（崩溃也落盘）、csv_io_entry.py、__main__.py、__about__.py（版本号唯一来源）。8 个文件 1005 行。[证据: simbench/events.py:1-11, simbench/diagnostics.py:1-45]" "8 文件 · 1005 行" {
                    tags "LayerCore"
                }

                // 组件之间的边 —— 全部由 AST 实测得出，不是按目录猜的
                gui -> engine "调 Session 门面：装载点表、启停通道、拉行快照、订阅事件" "in-process"
                gui -> project "读写点表模型、跑校验、走三种文件格式的导入导出" "in-process"
                gui -> master "核对页在自己的后台线程里直接调 master（不经 engine）" "in-process"
                gui -> net "网络页直接调 net.startup，同步跑在 GUI 线程上" "in-process"
                gui -> crosscut "订阅 events 里的四类事件；读 settings；启动时挂 diagnostics" "in-process"
                engine -> backend "**只经门面** backend/__init__.py 取 make_backend / BackendBase 等名字，不许摸实现文件（守卫 G4）" "in-process"
                engine -> codec "写值时编码、渲染时解码" "in-process"
                engine -> project "装载与校验点表模型" "in-process"
                engine -> net "取这一路通道还要额外监听哪些地址（endpoints.for_channel）。**函数体内延迟 import**，是 engine→net 唯一的一条边" "in-process (lazy)"
                engine -> crosscut "swallow 是吞异常的唯一出口" "in-process"
                backend -> codec "tick 里算波形值、钳位、编码成寄存器" "in-process"
                backend -> project "读通道/设备/点位模型，按 channel_transport() 分派" "in-process"
                backend -> crosscut "产出 PacketEvent / ChannelState / LogLine" "in-process"
                master -> project "拿点表当读取清单" "in-process"
                master -> codec "把读回来的寄存器解码成值再与初值比对（延迟 import）" "in-process (lazy)"
                master -> backend "MQTT 核对复用后端的 MqttSession 连 broker（模块级静态 import；目前第一条 master→backend 实现级依赖，没有守卫）" "in-process"
                net -> project "从点表推出需要哪些设备 IP" "in-process"
                codec -> project "只依赖 Point 的形状" "in-process"
                crosscut -> project "csv_io_entry 复用 formats / model / validate" "in-process"
            }

            elevatedChild = container "提权子进程" "不是另一个程序：net 的写操作（device create|remove / up / clean）把**同一个 exe** 用 RunAs 再启一遍，在提权上下文里执行真正的增删，然后把结果写成 JSON 交回父进程。[证据: simbench/net/elevate.py:27-93, simbench/net/cli.py:256-262,385-394,439-446,553-563]" "同一个 exe，RunAs 重启" {
                tags "Elevated"
            }

            // ---------- 落盘的五个存储 ----------
            tableStore = container "点表 / 工程文件" ".csv（12 列点表）、.xlsx（同一张表，表头冻结，可能不在第一行）、.json（整份工程，含 tick_ms 与串口参数）。按**后缀**分派，后缀不认识直接拒绝。写入一律「临时文件 + os.replace」原子替换，覆盖前留一份同目录 .bak（只留一代）。[证据: simbench/project/formats.py:30-87, simbench/project/fsio.py:35-91]" "文件 · CSV/XLSX/JSON" {
                tags "Store"
            }

            netSpecStore = container "网络声明文件" "点表旁边那份 x.net.json：设备名 ↔ ip/port/prefix。由 net init 生成骨架。同样走原子写。[证据: simbench/net/spec.py:37-71,124]" "文件 · JSON" {
                tags "Store"
            }

            settingsStore = container "界面设置" "%APPDATA%\\simbench\\settings.json：主题等偏好，启动时恢复。[证据: simbench/settings.py:36-45, simbench/gui/app.py:73-76]" "文件 · JSON" {
                tags "Store"
            }

            ledgerStore = container "网络账本" "%LOCALAPPDATA%\\simbench\\net-ledger.json：记「我们加过哪些 IP、建过哪些网卡」。这是 clean 只回收自己东西的唯一依据；账本读不出来就当「什么都不拥有」，绝不删。[证据: simbench/net/ledger.py:13-15,30-38,92-95,117-150]" "文件 · JSON" {
                tags "Store"
            }

            logStore = container "日志与崩溃栈" "%LOCALAPPDATA%\\simbench\\logs\\simbench-<日期>.log。未捕获异常（含引擎线程里的）写完整调用栈，解释器级崩溃靠 faulthandler 记原生栈。界面版 exe 没有控制台，出事只有这里和界面日志页留得下证据。可用 SIMBENCH_LOG_DIR 换目录。[证据: simbench/diagnostics.py:1-45, README:153-166]" "文件 · 文本日志" {
                tags "Store"
            }

            // ---------- 容器之间的边 ----------
            app -> tableStore "读 / 原子写（覆盖前留 .bak）" "文件"
            app -> netSpecStore "读声明 / init 时写骨架" "文件"
            app -> settingsStore "启动时读、切换时写" "文件"
            app -> logStore "运行期持续追加；崩溃钩子也写这里" "文件"
            app -> elevatedChild "net 的写操作把任务写成 JSON 交给它执行" "子进程 + %TEMP% JSON 任务文件"
            elevatedChild -> windowsNetwork "在提权上下文里真正执行 New-/Remove-NetIPAddress、SetupAPI 建网卡、pnputil 移除" "PowerShell / ctypes / pnputil"
            elevatedChild -> windowsUac "RunAs 触发一次 UAC 询问" "Start-Process -Verb RunAs"
            elevatedChild -> ledgerStore "把这次加过的 IP 与建过的网卡记进账本" "文件"
            app -> windowsNetwork "只读现状：Get-NetAdapter / Get-NetIPAddress / Get-PnpDevice，外加 IsUserAnAdmin 判要不要提权。**只读不提权**" "PowerShell / ctypes"
            app -> nativeLibraries "pymodbus 与 pyserial 是硬依赖；snap7 与 openpyxl 用到才加载，缺了只在起那一路时报一句带安装提示的错" "in-process"
        }

        // ---------- 人与外部系统之间的边 ----------
        fieldEngineer -> app "双击 simbench-gui.exe 开界面，或用 simbench.exe 跑 --check / --list / 无头监听 / net 子命令" "GUI / CLI"
        fieldEngineer -> windowsUac "配网卡时被问一次；只读命令不会问" "UAC 对话框"
        developer -> app "python -m simbench …，以及 ruff / mypy / pytest 三条门禁" "源码运行"

        upstreamMaster -> simbench "**连进来**读模拟设备：Modbus TCP 5020 / UDP / RTU-over-TCP / 串口 RTU / 串口 ASCII / S7 102。读的范围必须落在点表声明过的格上，有空洞则整条请求回异常码 2" "Modbus TCP·UDP·RTU·ASCII / ISO-on-TCP"
        simbench -> realDevice "主站（核对）模式下**连出去**只读：扫 unit（只读 hr 0 一格）、按点位跨度逐点读回与初值比对。一个寄存器都不写" "Modbus TCP 客户端"
        simbench -> serialBus "串口通道在这条总线上收发 RTU / ASCII 帧；一条串口多个 unit" "RS-485 串口"
        simbench -> windowsNetwork "读网卡现状；写操作全部转交提权子进程" "PowerShell / SetupAPI / pnputil"
        simbench -> nativeLibraries "调协议库收发帧、调 snap7.dll 起 S7 服务端" "in-process"
    }

    views {

        systemLandscape "L0-Landscape" "simbench 在现场联调链路里的位置：它夹在上游主站与真实现场设备之间，既当从站也当主站。" {
            include *
            autolayout tb
        }

        systemContext simbench "L1-Context" "系统边界：谁连进来、我们连出去到哪、哪些东西会被我们改动。" {
            include *
            autolayout tb
        }

        container simbench "L2-Container" "三种打包形态 + 一个提权子进程 + 五个落盘存储。注意提权子进程不是另一个程序，是同一个 exe 用 RunAs 再启一遍。" {
            include *
            autolayout tb
        }

        component app "L3-Component" "八个包 + 根下横切。边全部由 AST 实测得出（见 module-deps.dot）：project 不 import 任何内部包，是最底层的纯核；gui 对 backend 的引用数是 0。" {
            include *
            autolayout tb
        }

        styles {
            element "Person" {
                shape person
                background #1168bd
                color #ffffff
            }
            element "External" {
                background #8a8a8a
                color #ffffff
                shape hexagon
            }
            element "Store" {
                background #4f7942
                color #ffffff
                shape cylinder
            }
            element "Elevated" {
                background #b3261e
                color #ffffff
                border solid
            }
            element "LayerUI" {
                background #e07b00
                color #ffffff
            }
            element "LayerEngine" {
                background #2e6da4
                color #ffffff
            }
            element "LayerBackend" {
                background #5b7fa6
                color #ffffff
            }
            element "LayerCore" {
                background #4f7942
                color #ffffff
            }
            element "LayerSystem" {
                background #b3261e
                color #ffffff
            }
        }
    }
}
