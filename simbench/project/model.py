"""点表数据模型。本文件不得 import pymodbus 或 PySide6。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

BOOL, INT16, UINT16, REG16 = "bool", "int16", "uint16", "reg16"
INT32, UINT32, INT64, UINT64 = "int32", "uint32", "int64", "uint64"
FLOAT32, FLOAT64, STRING = "float32", "float64", "string"

DATATYPES = (BOOL, INT16, UINT16, INT32, UINT32, INT64, UINT64,
             FLOAT32, FLOAT64, STRING, REG16)
AREAS = ("co", "di", "hr", "ir")
BIT_AREAS = ("co", "di")
WAVEFORMS = ("const", "ramp", "sine", "random", "step")

# ---------- S7（S7comm / ISO-on-TCP）----------

# 区域：`mk`/`pe`/`pa` 三个字母名 + `db<号>` 写在区名里（S7 的 DB 号是地址的一部分，
# 不像 Modbus 的区那样只有四个字母）。类型用 **S7 自己的名字**，与 Modbus 那套明确分开：
# 同一条通道上的点位类型必须与传输匹配（validate 拦"Modbus 类型落在 S7 通道上"这类混填）。
S7_AREAS = ("mk", "pe", "pa")
S7_MAX_DB = 4095                      # 区域编号是 16 位的，留足现场余量即可
# `string` = 裸 ASCII（长度按初值算，无长度头）；`s7string` = **真 S7 STRING**
# （前两字节是"最大长度/当前长度"，组态软件按这个读）。两种并存：点表里写哪个就是哪个。
S7_TYPES = ("bool", "byte", "char", "word", "int", "dword", "dint", "real",
            "string", "s7string")
# 真 S7 STRING 的长度头只有 1 字节：最大长度 254 是硬上界（255=0xFF 是保留/无效值）
S7_STRING_MAX = 254
# 类型 -> 字节数（string 按初值长度另算；bool 占 1 位、落在 1 个字节里，见 `Point.bit`）
S7_TYPE_BYTES = {"byte": 1, "char": 1, "word": 2, "int": 2, "dword": 4,
                 "dint": 4, "real": 4}
S7_DEFAULTS: dict[str, Any] = {"host": "0.0.0.0", "port": 102, "rack": 0, "slot": 1}

WORD_ABCD, WORD_CDAB, WORD_BADC, WORD_DCBA = "ABCD", "CDAB", "BADC", "DCBA"
WORD_ORDERS = (WORD_ABCD, WORD_CDAB, WORD_BADC, WORD_DCBA)

# 串口通道的默认值（spec §5 的 project.json 示例就是这几个）。判定（白名单、范围）不在
# 这里，在 validate.py。
#
# `port` 的默认值是**空串**而不是 COM1（全面审查后定的口径）：`TcpChannel(port=502)` 那种
# "默认指向一个能用就行的端口"在串口上不成立——COM1 在本机很可能是**别人真设备的串口**，
# 少写一个键就静默去开它，等于替操作员猜了一个会动硬件的值。空串会被 validate 的
# `serial_port` 拦下（"must be a non-empty string"），于是"没写串口名"当场变成一条看得懂的
# 错，而不是一次神秘的端口占用。
# 值的类型本来就不同（串口名是 str、波特率是 int、超时是 float），所以这张表按
# `dict[str, Any]` 标注：不标的话每个取值点都会被静态检查当成 object——那些取值点
# 里写的类型（`port: str` / `baudrate: int`）才是真正的合同。
SERIAL_DEFAULTS: dict[str, Any] = {"port": "", "baudrate": 9600, "parity": "N",
                                   "bytesize": 8, "stopbits": 1, "timeout": 0.3}
# 帧格式的**书写**语法：`8N1`（数据位 5..8 / 校验 N/E/O / 停止位 1 或 1.5 不行，见 validate）。
# 这一份解析/渲染给两处用：CSV 的通道名（`com-485-1@COM3,9600,8E1`）与界面那一格。
# 只做转换，不判合法——`9N1` 解析得出 (9,'N',1)，由 validate 报"数据位只能是 7 或 8"。
_FRAME_FIELDS = ("bytesize", "parity", "stopbits")
# CSV 里"这一列写的是串口通道"的标记：`com-485-1@COM3 19200 8E1`。
# 段之间用**空格**而不是逗号：逗号在 CSV 里会让整格必须加引号（Excel 里手改一格就出错），
# 而串口名本身不许含空格，所以空格当分隔符没有歧义。放在模型层是因为两个地方都要认它
# （`csv_io` 解析/渲染名字，`validate` 拦"通道名/串口名里不许出现它"）。
SERIAL_MARK = "@"
# CSV 里"这一列写的是串口通道"的标记：`com-485-1@COM3,9600,8E1`。放在模型层是因为两个
# 地方都要认它（`csv_io` 解析/渲染名字，`validate` 拦"通道名里不许出现它"），
# 而它是数据形状的一部分，不是某个 IO 的私有约定。
SERIAL_MARK = "@"

# struct 大端码 -> 占用寄存器数（bool/string 另外处理）
_STRUCT: dict[str, str] = {
    INT16: "h", UINT16: "H", REG16: "H",
    INT32: "i", UINT32: "I", FLOAT32: "f",
    INT64: "q", UINT64: "Q", FLOAT64: "d",
}
_REG_COUNT: dict[str, int] = {
    INT16: 1, UINT16: 1, REG16: 1,
    INT32: 2, UINT32: 2, FLOAT32: 2,
    INT64: 4, UINT64: 4, FLOAT64: 4,
}
_INT_BOUNDS: dict[str, tuple[int, int]] = {
    INT16: (-32768, 32767), UINT16: (0, 65535), REG16: (0, 65535),
    INT32: (-2**31, 2**31 - 1), UINT32: (0, 2**32 - 1),
    INT64: (-2**63, 2**63 - 1), UINT64: (0, 2**64 - 1),
}
_FLOAT_LIMIT = {FLOAT32: 3.4028234663852886e38, FLOAT64: 1.7976931348623157e308}
# bool 初值只认白名单里的写法，其余（含空串、`TURE`、`01`、`-1`）一律 ValueError：
# 默默当 False 的错值会被当成平台 bug 去调，宁可启动时报错也不许猜（裁决 A）。
# 这是全仓库**唯一**一份 bool 白名单（裁决 E）：`parse_bool` 判，`parse_value(BOOL)` 与
# CSV 的 readonly 列都走它；消息里的 token 清单也从同一张表派生，不再各抄一遍。
# 成对写是为了让展示顺序固定（1/0、true/false……），集合本身是无序的。第三个字段是
# "这个词进不进用户可见的提示"：`y`/`n` 照认，但两个单字母混在清单里更难读，所以历史上
# 就没写进消息（`readonly 'TURE' is not a boolean, use one of 1/0/...` 是要钉住的对外
# 句式），这里把它显式标出来而不是让文案和判定各说一套。
_BOOL_PAIRS: tuple[tuple[str, str, bool], ...] = (
    ("1", "0", True), ("true", "false", True), ("yes", "no", True),
    ("y", "n", False), ("on", "off", True))
_BOOL_TRUE: set[str] = {pair[0] for pair in _BOOL_PAIRS}
_BOOL_FALSE: set[str] = {pair[1] for pair in _BOOL_PAIRS}
BOOL_TOKENS = "/".join(f"{t}/{f}" for t, f, shown in _BOOL_PAIRS if shown)
if _BOOL_TRUE & _BOOL_FALSE:
    # 一个词同时映射到真和假时，判定结果取决于分支顺序——这种表本身就不该存在
    raise RuntimeError("bool whitelist maps one token to both True and False")
if not set(BOOL_TOKENS.split("/")) <= (_BOOL_TRUE | _BOOL_FALSE):
    # 消息里承诺的写法必须真认（照裁决 H 对 WAVE_SPEC 的做法：两个来源就一定会漂移）
    raise RuntimeError("bool whitelist drifted from the token list in its messages")

# 用户原值进错误消息要限长：401 位的脏值原样打出来能把整屏刷掉。
_BRIEF_LIMIT = 80


def brief(value, limit: int = _BRIEF_LIMIT) -> str:
    """原值 repr 的截断形式，只砍尾巴：超过 `limit` 个字符取前段并补 `...`。

    放在 model 层是因为截断口径只有一个：codec 与 model 自己抛的消息都会被
    validate.py 一次收集起来打印（Task 5），任何一条不带限长的原值都能把真正的
    错误挤出屏幕。判定口径不受影响——异常类型与消息前缀由调用方保持不变；
    短的（≤limit）原样输出，现场排查主站写来的脏值靠的就是这些原值。
    """
    text = repr(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def brief_name(value, limit: int = _BRIEF_LIMIT) -> str:
    """身份名（通道名/设备名/点位名/host）的限长形式：只砍尾巴，**不加引号**。

    和 `brief()` 的分工：`brief` 打的是"用户填进去的值"，带 repr 的引号才有用（空串和
    两个空格看得出区别）；身份名是拼进 `Issue.where` 与消息里给人定位用的，套上引号就把
    已钉死的对外句式改掉了（`[overlap] c1/D1/q: ...`、`port 502 on host 0.0.0.0 already
    used by channel tcp-502`）。厂商把整段说明塞进通道名时，一条 1244 字符的错误行能把
    "一次报全"的清单真正那条挤出去，所以这里同样限长，旋钮与 `brief` 共用一个。

    非字符串走 `str()`，与原来 f-string 拼出来的样子一致（`{ch.name}` 本来就是 str()）。
    """
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def area_is_bit(area: str) -> bool:
    """co/di 是位区：一个点位 = 一个线圈/离散输入。"""
    return area in BIT_AREAS


def _ascii_bytes(value: str) -> bytes:
    """string 初值的编码口径：非 ascii 抛领域 ValueError，不外泄 UnicodeEncodeError。

    codec 与 validate 都按 ValueError 收集（validate 算跨度那段有 try，裁决 I），消息会被
    原样列进“一次报全”的清单：换成领域错误才带得上 `string must be ascii` 这个能读懂的
    前缀（裁决 D），底层那句 'ascii' codec can't encode characters 会被当成模拟器的 bug。
    """
    try:
        return value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"string must be ascii: {brief(value)}") from exc


def register_span(datatype: str, value: str = "0") -> int:
    if datatype == BOOL:
        return 1
    if datatype == STRING:
        if not isinstance(value, str):
            raise ValueError(
                f"string needs a str value, got {type(value).__name__}: {brief(value)}")
        return max(1, -(-len(_ascii_bytes(value)) // 2))
    if datatype not in _REG_COUNT:
        raise ValueError(f"unknown datatype {datatype!r}")
    return _REG_COUNT[datatype]


def struct_format(datatype: str) -> str:
    """大端 struct 串，codec 直接用。"""
    if datatype not in _STRUCT:
        raise ValueError(f"unknown datatype {datatype!r}")
    return ">" + _STRUCT[datatype]


def word_orders_allowed(datatype: str) -> tuple[str, ...]:
    if datatype in (INT32, UINT32, FLOAT32):
        return WORD_ORDERS
    if datatype in (INT64, UINT64, FLOAT64):
        return (WORD_ABCD, WORD_DCBA)
    return (WORD_ABCD,)


def parse_bool(text) -> bool:
    """全仓库唯一的 bool 判定（裁决 E）：白名单认不出来就抛 ValueError，绝不猜 False。

    单独成一个公共函数是为了让"判 bool"这件事只有一个入口：`parse_value(BOOL, ...)`
    走它，CSV 的 `readonly` 列也走它。`readonly` 不是 datatype 列，让 csv_io 去调
    `parse_value(BOOL, ...)` 等于把"列名"和"类型名"两句话混在一处，两边都得改口径。

    消息句式逐字保持原样：Python `bool` 原样返回（裁决 A），非字符串报
    `bool needs a string value, got {type}: {brief}`，未识别 token 报
    `bool needs one of {BOOL_TOKENS}, got {brief}`。
    """
    if isinstance(text, bool):
        return text
    if not isinstance(text, str):
        raise ValueError(
            f"bool needs a string value, got {type(text).__name__}: {brief(text)}")
    key = text.strip().lower()
    if key in _BOOL_TRUE:
        return True
    if key in _BOOL_FALSE:
        return False
    raise ValueError(f"bool needs one of {BOOL_TOKENS}, got {brief(text)}")


def parse_value(datatype: str, text: str):
    """字符串 -> 原生 Python 值；类型不对或越界抛 ValueError，消息里带类型名。

    只收字符串（BOOL 额外允许直接传 Python bool）：CSV 和 project.json 里的初值一律是
    字符串，收到 int/float/None 说明调用方传错了类型；截断或 str() 出来的值看着合法，
    静默错值会被当成平台 bug 去调。未知与越界同样报错，不替用户猜。
    """
    if datatype == BOOL:
        return parse_bool(text)             # 判定不在这里，见 parse_bool（裁决 E）
    if not isinstance(text, str):
        raise ValueError(
            f"{datatype} needs a string value, got {type(text).__name__}: {brief(text)}")
    if datatype == STRING:
        _ascii_bytes(text)                        # 只为校验，口径与 register_span 一致
        return text
    fmt = struct_format(datatype)
    if fmt.endswith(("f", "d")):
        try:
            num = float(text)
        except ValueError:
            raise ValueError(f"{datatype} needs a number, got {brief(text)}") from None
        if num != num:
            raise ValueError(f"{datatype} rejects nan: {brief(text)}")
        limit = _FLOAT_LIMIT[datatype]
        if abs(num) > limit:                      # inf 溢出后自然落到这条
            raise ValueError(
                f"{datatype} out of range, |value| must be <= {limit}: {brief(text)}")
        return num
    try:
        num = int(text)
    except ValueError:
        raise ValueError(f"{datatype} needs an integer, got {brief(text)}") from None
    lo, hi = _INT_BOUNDS[datatype]
    if not lo <= num <= hi:
        raise ValueError(f"{datatype} out of range [{lo}, {hi}]: {brief(text)}")
    return num


@dataclass
class Point:
    name: str
    area: str
    address: int
    datatype: str
    initial: str = "0"
    word_order: str = WORD_ABCD
    waveform: str = "const"
    wave_param: dict[str, str] = field(default_factory=dict)
    readonly: bool = False
    row: int | None = None       # CSV 行号，只在校验报错时用，不进 project.json
    # S7 的位地址（`DBX0.3` 里的那个 3）：只有 S7 通道上的 bool 点会填它，Modbus 点恒为 None。
    # 不把"位"塞进 `address`（那会变成 `字节*8+位` 的裸位号），因为 S7 的地址显示、点表填写
    # 与重叠判定看的都是"字节 + 位"这一对。
    bit: int | None = None

    @property
    def span(self) -> int:
        return 1 if area_is_bit(self.area) else register_span(self.datatype, self.initial)

    def typed_initial(self):
        return parse_value(self.datatype, self.initial)


@dataclass
class Device:
    name: str
    unit_id: int
    points: list[Point] = field(default_factory=list)


def parse_frame(text: str) -> dict | None:
    """`8E1` -> `{"bytesize": 8, "parity": "E", "stopbits": 1}`；写不成这个样子回 None。

    写不成形（`8X1`、`9N1`、`81`、空串）一律 None：**判定不在这里**，调用方（CSV 那一侧抛带
    行号的值错误、界面那一侧当场拒）各自把 None 换成人话；而"数据位 9"这种**形状对、值不合法**
    的留给 validate——两件事混在一个函数里，界面上就会出现"格式错"和"值错"两种说法对不上。
    """
    text = (text or "").strip().upper()
    if len(text) != 3 or not text[0].isdigit() or text[1] not in "NOE" \
            or text[2] not in "12":
        return None
    return {"bytesize": int(text[0]), "parity": text[1], "stopbits": int(text[2])}


def format_frame(bytesize: int, parity: str, stopbits: int) -> str:
    """`(8, "E", 1)` -> `"8E1"`。与 `parse_frame` 是同一份语法的两个方向。"""
    return f"{bytesize}{parity}{stopbits}"


def serial_summary(channel) -> str:
    """串口通道的"一句话摘要"：`COM3 19200 8E1`。

    命令行（`--list` 那一行、启动那一行）与后端的状态 detail（`describe()`）用的是同一句，
    所以它住在这里而不是各写一处——两处一共就这两行代码，但它们说的是**同一台设备**，
    各写一份就会出现"命令行列 COM3 9600 8N1、状态栏列 COM3 19200 8E1"。
    """
    return (f"{channel.port} {channel.baudrate} "
            f"{format_frame(channel.bytesize, channel.parity, channel.stopbits)}")


def parse_s7_area(text) -> tuple[str, int | None] | None:
    """`db3` -> `("db", 3)`；`mk`/`pe`/`pa` -> `(区名, None)`；不是 S7 区名回 None。

    只做**转换**：`db0`、`db99999` 这种"形状对、值不合法"的照样解析出来（`("db", 0)`），
    由 validate 报；形状不对的回 None。
    """
    if not isinstance(text, str):
        return None
    lowered = text.strip().lower()
    if lowered in S7_AREAS:
        return lowered, None
    if lowered.startswith("db") and lowered[2:].isdigit():
        return "db", int(lowered[2:])
    return None


def s7_area_name(area: str, db: int | None) -> str:
    """反方向：`("db", 3)` -> `"db3"`；`("mk", None)` -> `"mk"`。与 `parse_s7_area` 同一份语法。"""
    return area if db is None else f"db{db}"


def parse_s7_address(text) -> tuple[int, int | None] | None:
    """S7 的地址格：`5` -> `(5, None)`（字节偏移）、`0.3` -> `(0, 3)`（`DBX0.3` 那一位）。

    同样只做转换：`0.9`（位越界）解析成 `(0, 9)` 交给 validate；形状不对（`a.b`、`1.2.3`、
    空串）回 None——那不是值，是点表里写坏的一格。
    """
    raw = str(text if text is not None else "").strip()
    if not raw:
        return None
    head, dot, tail = raw.partition(".")
    if not head.isdigit():
        return None
    if not dot:
        return int(head), None
    if not tail.isdigit():
        return None
    return int(head), int(tail)


def s7_span_bytes(datatype: str, initial: str) -> int:
    """这个 S7 类型占几个**字节**（bool 也回 1：它占的那一位落在一个字节里）。

    string 按初值长度算——与 Modbus 那边的 string 同一条口径（跨度归点表初值管），
    与"真 S7 STRING"（前两字节是最大长度/当前长度）不同，这一点在 `codec/s7_codec.py`
    与 spec 里都要写明。
    """
    if datatype == STRING:
        return max(1, len(initial or ""))
    if datatype == "s7string":
        # 真 STRING 占"2 字节长度头 + 最大长度"。**最大长度取点表初值的长度**——与裸 string
        # 同一条口径（跨度归点表初值管），点表上一个 `s7string` 点就自带它的最大长度。
        return 2 + max(1, len(initial or ""))
    return S7_TYPE_BYTES.get(datatype, 1)


def s7_summary(channel) -> str:
    """S7 通道的"一句话摘要"：`192.168.0.10:102 rack 0 slot 1`。

    与 `serial_summary` 并列：命令行那一行与后端的状态 detail 用同一句，`s7` 后端
    `describe()` 直接调它。
    """
    return (f"{channel.host}:{channel.port} "
            f"rack {channel.rack} slot {channel.slot}")


@dataclass
class TcpChannel:
    name: str
    host: str = "0.0.0.0"
    port: int = 502
    devices: list[Device] = field(default_factory=list)
    type: str = "tcp"


@dataclass
class SerialChannel:
    """一路串口（RTU）通道。字段就是 spec §5 那份 project.json 的键，一个不多一个不少。

    `type` **不设字段**：串口通道的类型恒为 `"rtu"`，多一个可写字段就多一处能与事实不符的
    地方（`channel_transport()` 才是唯一判定）。写盘时由 `json_io` 按 `channel_transport()`
    补上这个键。
    """
    name: str
    port: str = SERIAL_DEFAULTS["port"]
    # 串口上跑哪种帧：`rtu`（默认，绝大多数现场设备）或 `ascii`（Modbus ASCII）。
    # 它决定 `channel_transport()` 回 `rtu` 还是 `ascii`，进而决定后端用哪个 framer。
    framing: str = "rtu"
    baudrate: int = SERIAL_DEFAULTS["baudrate"]
    parity: str = SERIAL_DEFAULTS["parity"]
    bytesize: int = SERIAL_DEFAULTS["bytesize"]
    stopbits: int = SERIAL_DEFAULTS["stopbits"]
    timeout: float = SERIAL_DEFAULTS["timeout"]
    devices: list[Device] = field(default_factory=list)


@dataclass
class S7Channel:
    """一路 S7（ISO-on-TCP / S7comm）通道。`type` 同样不设字段，写盘时由 `json_io` 补 `"s7"`。"""
    name: str
    host: str = S7_DEFAULTS["host"]
    port: int = S7_DEFAULTS["port"]
    # rack/slot 是客户端**连上来时报的**（不是我们校验的对象）：留着是为了在 102 上区分
    # "谁连了哪台"的那类现场问题（报文窗/日志里带出来），以及给界面那一行一个可读的摘要。
    rack: int = S7_DEFAULTS["rack"]
    slot: int = S7_DEFAULTS["slot"]
    devices: list[Device] = field(default_factory=list)


# 三种通道的**联合类型**：spec 里一直把它叫 "Channel"（§5 的数据模型：Project/Channel/
# Device/Point），代码里原先在 `Project.channels` 与 `find_channel` 各手写一遍。给它一个
# 名字，注解才写得下（`__main__._serve` 那份 `list[tuple[Channel, ...]]` 是第三处）。
Channel = TcpChannel | SerialChannel | S7Channel


# ---------- 传输（2026-10-14 E7：从两种扩到五种）----------
#
# 一份**事实表**：哪种传输跑在哪条载体上、帧是什么。做这一轮之前，"加一种协议要改哪 16 处"
# 的清单是靠人读代码数出来的（见 `plans/2026-10-14-import-export-formats.md` §4）；这一轮真
# 加三种传输，把"载体/帧"这两件事收在这里，其余各处问它，别再散成字符串比较。
#
# 命名口径：**transport 名就是事实**，两种载体共用一个名字空间：
#   `tcp`（Modbus TCP，MBAP）、`udp`（Modbus UDP，MBAP）、`rtutcp`（TCP 上跑 RTU 帧——
#   现场那些以太网转串口的网关就是这种）、`rtu`（串口 RTU）、`ascii`（串口 ASCII）、
#   `s7`（ISO-on-TCP / S7comm）。
SOCKET_TRANSPORTS = ("tcp", "udp", "rtutcp")
SERIAL_TRANSPORTS = ("rtu", "ascii")
MODBUS_TRANSPORTS = (*SOCKET_TRANSPORTS, *SERIAL_TRANSPORTS)
SERIAL_FRAMINGS = ("rtu", "ascii")
TRANSPORT_LABELS = {"tcp": "Modbus TCP", "udp": "Modbus UDP",
                    "rtutcp": "Modbus RTU over TCP", "rtu": "串口 RTU",
                    "ascii": "串口 ASCII", "s7": "S7"}


def is_socket(transport: str) -> bool:
    """这一路有没有 `host:port`（tcp/udp/rtutcp）。"""
    return transport in SOCKET_TRANSPORTS


def is_serial(transport: str) -> bool:
    """这一路是不是串口（rtu/ascii）：参数是串口名 + 波特率那一串。"""
    return transport in SERIAL_TRANSPORTS


def is_s7(transport: str) -> bool:
    """这一路是不是 S7：**读写与跨度都按 S7 的语义**（字节 + 位，不是寄存器格）。"""
    return transport == "s7"


def channel_transport(channel: Channel) -> str:
    """这一路用哪种传输（`tcp` / `udp` / `rtutcp` / `rtu` / `ascii` / `s7`）。

    未知的手写 `type` 原样回，由 validate 报（那种 `type` 我们不认识，也不许按 TCP 处理）。

    **全仓库唯一一处**这类判定：`session` 选后端、`validate` 选判定、`csv_io` 烘名字、
    `gui/channel_panel` 选那几格的含义，都走这里——散成若干 `isinstance` 的那种写法，
    加第三种通道时必然漏掉一处，而漏掉的表现是"某一处按 TCP 处理了一路串口"。
    """
    if isinstance(channel, SerialChannel):
        # 串口两种帧：RTU（默认）与 ASCII。`framing` 是串口通道自己的字段（见那个类）。
        return "ascii" if getattr(channel, "framing", "rtu") == "ascii" else "rtu"
    if isinstance(channel, S7Channel):
        return "s7"
    return getattr(channel, "type", "tcp")


@dataclass
class Project:
    channels: list[Channel] = field(default_factory=list)
    tick_ms: int = 100

    def iter_points(self):
        for ch in self.channels:
            for dev in ch.devices:
                for pt in dev.points:
                    yield ch, dev, pt

    def find_channel(self, name: str) -> Channel:
        for ch in self.channels:
            if ch.name == name:
                return ch
        raise KeyError(name)
