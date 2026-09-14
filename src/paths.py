"""路径安全：把不可信的文件名/相对路径落到受控目录内。

这个模块**刻意不含任何沙箱语义**，只做两件纯路径的事，因此两个项目可以逐字共用：

- :func:`safe_target_name` —— 把用户提供的文件名归一化成**纯文件名**；
- :func:`safe_join` —— 把相对路径拼到根目录下，**越界一律返回 None**。

> 与项目二 `src/sandbox/analysis.py` 的同名函数是**有意的同构**：那边原本自带一份
> `safe_target_name`，现在改为从本模块导入再导出，保证全仓只有**一处实现**。
> 各留一份的话，两边的归一化规则迟早分叉 —— 而这类分叉的后果是"一边拦得住、
> 一边拦不住"，属于最难发现的那种漏洞。
"""

from __future__ import annotations

from pathlib import Path


def safe_target_name(source: Path) -> str:
    """把源文件名归一化成一个可安全放进受控目录的**纯文件名**。

    被处理的文件名可能来自用户上传，形如 `../../etc/passwd`。如果直接拼接，
    `..` 会让落点跑到目录之外，等于白做了隔离。取 basename 之后，无论传进来
    什么路径，落点都只会在目标目录内部。

    名字为空、或者就是 `.` / `..` 时退化成 `"data"`；basename 里若还残留分隔符
    也一并清掉。
    """
    name = source.name.strip()
    if not name or name in {".", ".."}:
        name = "data"
    # 再兜一层：即便 basename 里还带着分隔符也要清掉
    name = name.replace("/", "_").replace("\\", "_")
    return name or "data"


def safe_join(root: Path, relative: str) -> Path | None:
    """把 ``relative`` 拼到 ``root`` 下；**落点越界返回 ``None``**。

    与 :func:`safe_target_name` 的区别很重要：这个函数**允许子目录**
    （`nested/deep.py` 是合法的），所以不能只取 basename —— 那会指向错误的文件。
    它做的是"先拼再校验落点"：`resolve()` 之后必须仍在 ``root`` 之内。

    为什么需要它：`data_dir / rel_path` 这种写法在 `rel_path` 失控时会把落点带到
    目录之外，而"失控"不一定是攻击 —— 一个手写的清单、一次数据迁移、一个符号
    链接都足够。这里提供的是**纵深防御**：即便上游某天漏了一处校验，落点仍然
    被钉在目录内。

    路径不存在也算合法（调用方自己判断存在性）；只有越界或解析失败才返回 None。
    """
    root = Path(root)
    try:
        resolved_root = root.resolve()
        resolved = (root / relative).resolve()
    except (OSError, RuntimeError):
        return None
    if resolved == resolved_root or resolved_root in resolved.parents:
        return resolved
    return None


__all__ = ["safe_join", "safe_target_name"]
