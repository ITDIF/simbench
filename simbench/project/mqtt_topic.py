"""MQTT 通用模板：topic 与 payload 的唯一约定。

MQTT 没有 Modbus 那种"unit + 区 + 地址"的线上地址，值放在 topic 里、payload 里带值。
本模块把这件事收在一处，模拟侧（发布值 / 接收写入）与主站侧（订阅值 / 核对）都问这里，
避免两边的 topic 规则各写一份、漂移后现场怎么也对不上。

约定（默认 topic 前缀 `simbench`，通道可用 `topic_prefix` 改）：

* 值 topic：`{prefix}/{channel}/{device}/{area}/{address}`
  例：`simbench/mqtt-1/隧道风机-1/hr/0`
* 写入 topic：值 topic + `/set`
* payload：`{"value": 25.5}`；解析时也兼容裸标量（`25.5`、`true`、`FAN-1`）
* 值按点位类型（datatype/word_order）编码成寄存器序列，与核对页现有的比较逻辑共用；
  也就是说 MQTT 侧看到的是"人写的值"，不是原始寄存器字节。

topic 段里的 `/`、`+`、`#` 会替换成 `_`：它们是 MQTT 的分隔符/通配符，原样保留会让
一条点位的 topic 变成另一条的子树。点表里的通道名/设备名照旧保留，topic 只做转义。
"""

from __future__ import annotations

import json

DEFAULT_PREFIX = "simbench"


def segment(text) -> str:
    """一个 topic 段：去掉 MQTT 的分隔符与通配符，空串给 `_`。"""
    out = str(text or "").strip()
    for mark in ("/", "+", "#", "\0"):
        out = out.replace(mark, "_")
    return out or "_"


def value_topic(prefix: str, channel: str, device: str, area: str,
                address: int) -> str:
    """一个点位的值 topic。"""
    return (f"{segment(prefix)}/{segment(channel)}/{segment(device)}/"
            f"{segment(area)}/{int(address)}")


def set_topic(prefix: str, channel: str, device: str, area: str,
              address: int) -> str:
    """一个点位的写入 topic。"""
    return value_topic(prefix, channel, device, area, address) + "/set"


def encode_payload(value) -> str:
    """人写的值 -> 线上 payload。紧凑 JSON，中文不转义。"""
    return json.dumps({"value": value}, ensure_ascii=False, separators=(",", ":"))


def decode_payload(raw) -> object:
    """线上 payload -> Python 值；坏 JSON 按裸标量解析，解析不了就原样回字符串。"""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    text = text.strip()
    try:
        body = json.loads(text)
    except ValueError:
        return _scalar(text)
    if isinstance(body, dict) and "value" in body:
        return body["value"]
    return body


def _scalar(text: str):
    low = text.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text
