"""推送指令（字段键以 $ 开头）：不写进飞书，按"当前值"求值后再写（缓存库第 3、4 步）。

同一套求值同时用于两处，保证本地视图与最终推到飞书的结果一致：
- 推送线程：以推送前飞书上的当前值求值；
- 本地视图：以镜像叠加前序操作后的值求值（页面立即看到的结果）。
"""
from . import attendance, normalize


def _rollover(current, arg):
    """$rollover：换周结算列。用本次增量**之前**的累计时长，已换到本周时不写任何列。"""
    upd = attendance.rollover_updates(
        normalize._num(current.get(attendance.COL_WORK)),
        normalize._num(current.get(attendance.COL_WEEK_BASE)),
        normalize._plain(current.get(attendance.COL_WEEK_KEY)),
        arg["this"], arg["last"])
    return upd or {}


DIRECTIVES = {"$rollover": _rollover}


def evaluate(directives, current):
    """求值一组指令，返回要写的普通列；未知指令抛 ValueError。"""
    out = {}
    for name, arg in (directives or {}).items():
        fn = DIRECTIVES.get(name)
        if fn is None:
            raise ValueError("未知的推送指令: " + name)
        out.update(fn(current or {}, arg))
    return out
