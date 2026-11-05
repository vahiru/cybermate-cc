"""agent（Codex / Antigravity）用来在群里说话的 stdio MCP server。

不持有 bot token：每个工具只是把参数 POST 给主进程的本地接口，带上这次 run 的一次性 token。
由 CLI 按本轮的 MCP 配置拉起，CGM_API / CGM_RUN_TOKEN 由主进程每次 run 传进去。
"""

import json
import os
import urllib.error
import urllib.request

from mcp.server.mcpserver import MCPServer

API = os.environ["CGM_API"]
TOKEN = os.environ["CGM_RUN_TOKEN"]

mcp = MCPServer("telegram")


def _call(name: str, **args) -> str:
    args = {k: v for k, v in args.items() if v is not None}
    req = urllib.request.Request(
        f"{API}/tool/{name}", data=json.dumps(args).encode(),
        headers={"Content-Type": "application/json", "X-Run-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        body = json.load(e)
    if not body.get("ok"):
        if body.get("code"):
            raise RuntimeError(json.dumps(body, ensure_ascii=False))
        raise RuntimeError(body.get("error", "失败"))
    return json.dumps(body["result"], ensure_ascii=False)


@mcp.tool()
def send_message(text: str, reply_to: int | None = None) -> str:
    """在群里发一条消息。reply_to 填要回复的消息 id（不带 #）。返回新消息的 message_id。
    支持 ``` 代码块、`行内代码`、**粗体**、[文字](链接)，其余按纯文本发。"""
    return _call("send_message", text=text, reply_to=reply_to)


@mcp.tool()
def edit_message(message_id: int, text: str) -> str:
    """把你自己之前发的一条消息改成新内容。用于把「等我看看」换成结果。"""
    return _call("edit_message", message_id=message_id, text=text)


@mcp.tool()
def send_file(path: str, caption: str | None = None, reply_to: int | None = None) -> str:
    """把本次 run 的 out/ 目录下的文件发到群里。png/jpg/webp 按图片发，其他按文件发。
    path 可以写 out/xxx.png 这样的相对路径。"""
    return _call("send_file", path=path, caption=caption, reply_to=reply_to)


@mcp.tool()
def generate_image(prompt: str, filename: str = "image.png", images: list[str] | None = None) -> str:
    """用 AI 画一张图（插画、头像、梗图、场景），存到 out/ 并返回路径，之后用 send_file 发出。
    prompt 写清楚画面内容和风格。改图或参考图片时，images 传本次 run 的图片路径列表，
    如 ["in/photo.jpg", "out/image.png"]；支持 PNG/JPEG/WebP，最多 16 张，每张小于 50MB。
    先用 view_image 看参考图。不传 images 就按文字生图。
    大约要 40 秒；每次最多 2 张。数据图、函数图用 matplotlib，不用这个。"""
    return _call("generate_image", prompt=prompt, filename=filename, images=images)


@mcp.tool()
def get_profile(message_id: int | None = None, username: str | None = None) -> str:
    """查一个人的公开资料：名字、用户名、简介（bio）、是不是 bot。
    用那人发过的任意一条消息的 id 指人（不带 #），或者给 @用户名。
    有头像的话会存到 in/，返回路径，用 view_image 看。每次最多查 3 个人。"""
    return _call("get_profile", message_id=message_id, username=username)


@mcp.tool()
def get_history(before_id: int) -> str:
    """往前翻更早的聊天记录：返回 #before_id 之前的 100 条，格式和 prompt 里一样。
    more 为 true 说明还能接着用最早那条的 id 往前翻。每次最多翻 3 页。"""
    return _call("get_history", before_id=before_id)


@mcp.tool()
def react(message_id: int, emoji: str) -> str:
    """给某条消息点一个表情回应（如 🎉 🔥 🤯 😢 ❤）。偶尔用，一次 run 最多一个。"""
    return _call("react", message_id=message_id, emoji=emoji)


if __name__ == "__main__":
    mcp.run("stdio")
