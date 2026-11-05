"""把模型写的文本变成 Telegram HTML，并切成不超过上限的几段。

只认几样 Markdown：``` 代码块、`行内代码`、**粗体**、[文字](链接)。其余原样转义。
"""

import html
import re

LIMIT = 4096
CAPTION_LIMIT = 1024

_FENCE = re.compile(r"^```([\w+-]*)[^\n]*\n(.*?)^```[ \t]*$", re.S | re.M)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def _inline(raw: str) -> str:
    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    s = html.escape(_INLINE_CODE.sub(stash, raw), quote=False)
    # 整段已经转义过，链接里的 & 已是 &amp;，只需再处理引号
    s = _LINK.sub(lambda m: f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>', s)
    s = _BOLD.sub(r"<b>\1</b>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], s)


def _pre(code: str, lang: str) -> str:
    body = html.escape(code.rstrip("\n"), quote=False)
    if lang:
        return f'<pre><code class="language-{lang}">{body}</code></pre>'
    return f"<pre>{body}</pre>"


def _blocks(text: str) -> list[tuple[str, str, str]]:
    """切成 (kind, raw, lang)：代码块整块，普通文字按空行分段。"""
    out: list[tuple[str, str, str]] = []
    pos = 0
    for m in _FENCE.finditer(text):
        out += [("p", p, "") for p in re.split(r"\n\s*\n", text[pos:m.start()]) if p.strip()]
        out.append(("code", m.group(2), m.group(1)))
        pos = m.end()
    out += [("p", p, "") for p in re.split(r"\n\s*\n", text[pos:]) if p.strip()]
    return out


def _render(kind: str, raw: str, lang: str) -> str:
    return _pre(raw, lang) if kind == "code" else _inline(raw.strip("\n"))


def _split_block(kind: str, raw: str, lang: str, limit: int) -> list[str]:
    """一块本身就超长：按行切；单行还超长就按字符硬切。"""
    lines: list[str] = []
    for line in raw.split("\n"):
        while len(line) > limit // 2:
            lines.append(line[:limit // 2])
            line = line[limit // 2:]
        lines.append(line)
    pieces, cur = [], ""
    for line in lines:
        cand = f"{cur}\n{line}" if cur else line
        if cur and len(_render(kind, cand, lang)) > limit:
            pieces.append(_render(kind, cur, lang))
            cur = line
        else:
            cur = cand
    if cur:
        pieces.append(_render(kind, cur, lang))
    return pieces


def render(text: str, limit: int = LIMIT) -> list[str]:
    chunks, cur = [], ""
    for kind, raw, lang in _blocks(text.strip()):
        h = _render(kind, raw, lang)
        parts = [h] if len(h) <= limit else _split_block(kind, raw, lang, limit)
        for part in parts:
            if cur and len(cur) + 2 + len(part) > limit:
                chunks.append(cur)
                cur = part
            else:
                cur = f"{cur}\n\n{part}" if cur else part
    if cur:
        chunks.append(cur)
    return chunks or [html.escape(text or " ", quote=False)[:limit]]
